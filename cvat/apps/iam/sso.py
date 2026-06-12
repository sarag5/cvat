# Copyright (C) CVAT.ai Corporation
#
# SPDX-License-Identifier: MIT

"""
Single Sign-On (SSO) integration with Microsoft Azure Entra ID via OpenID Connect.

The flow is built on top of django-allauth's generic ``openid_connect`` provider.
Azure publishes the application roles assigned to a user in the ``roles`` claim of
the *ID token* (not via the OIDC userinfo endpoint), so a custom OAuth2 adapter is
used to decode the ID token and expose those roles to the rest of CVAT. The roles
are then mapped to CVAT permissions:

* a user that has one of ``settings.SSO_ADMIN_ROLES`` becomes a Django
  superuser/staff member (CVAT administrator);
* every other authenticated user receives the default ("user") role.
"""

import base64
import binascii
import json

import requests
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from allauth.socialaccount.providers.oauth2.views import OAuth2CallbackView, OAuth2LoginView
from allauth.socialaccount.providers.openid_connect.views import OpenIDConnectAdapter
from django.conf import settings
from django.contrib.auth.models import Group, User
from django.shortcuts import redirect
from django.urls import reverse
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView


def _decode_jwt_payload(token: str) -> dict:
    """
    Decode the payload (claims) of a JWT without verifying its signature.

    The ID token is obtained directly from the Azure token endpoint over a
    server-to-server TLS connection during the OAuth2 code exchange, so its
    integrity is already guaranteed by the transport. This mirrors how
    django-allauth itself trusts the data returned by the provider.
    """
    try:
        payload_segment = token.split(".")[1]
    except (AttributeError, IndexError):
        return {}

    padding = "=" * (-len(payload_segment) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload_segment + padding)
        return json.loads(decoded)
    except (binascii.Error, ValueError, TypeError):
        return {}


def _extract_roles(extra_data: dict) -> list[str]:
    roles = (extra_data or {}).get("roles")
    if isinstance(roles, str):
        return [roles]
    if isinstance(roles, (list, tuple)):
        return [str(role) for role in roles]
    return []


def sync_sso_user_roles(user: User, extra_data: dict) -> None:
    """
    Map Azure Entra ID application roles to CVAT permissions.

    Entra ID is treated as the source of truth: a user holding one of the
    configured admin roles is granted superuser rights, while everyone else is
    restricted to the default role. This runs on every login so that role
    changes made in Entra ID propagate to CVAT.
    """
    if user is None:
        return

    roles = _extract_roles(extra_data)
    is_admin = bool(set(roles) & set(settings.SSO_ADMIN_ROLES))

    fields_to_update = []
    if user.is_superuser != is_admin:
        user.is_superuser = is_admin
        fields_to_update.append("is_superuser")
    if user.is_staff != is_admin:
        user.is_staff = is_admin
        fields_to_update.append("is_staff")
    if fields_to_update:
        user.save(update_fields=fields_to_update)

    admin_group = Group.objects.get(name=settings.IAM_ADMIN_ROLE)
    default_group = Group.objects.get(name=settings.IAM_DEFAULT_ROLE)
    if is_admin:
        user.groups.add(admin_group)
        user.groups.remove(default_group)
    else:
        user.groups.remove(admin_group)
        user.groups.add(default_group)


class EntraOIDCAdapter(OpenIDConnectAdapter):
    """
    OpenID Connect adapter that additionally reads the ``roles`` claim (and a few
    other useful claims) from the ID token returned by Azure Entra ID.
    """

    def complete_login(self, request, app, token, response):
        extra_data = {}

        # Best-effort call to the userinfo endpoint (Azure returns a minimal set
        # of claims here and notably does not include application roles).
        try:
            resp = requests.get(
                self.profile_url,
                headers={"Authorization": "Bearer " + str(token)},
                timeout=10,
            )
            resp.raise_for_status()
            extra_data = resp.json()
        except (requests.RequestException, ValueError):
            extra_data = {}

        id_token = response.get("id_token") if isinstance(response, dict) else None
        claims = _decode_jwt_payload(id_token) if id_token else {}

        # Fill in any claims missing from userinfo and always trust the ID token
        # for the application roles.
        for key in ("sub", "oid", "name", "preferred_username", "email", "upn"):
            if claims.get(key) is not None and extra_data.get(key) is None:
                extra_data[key] = claims[key]
        if claims.get("roles") is not None:
            extra_data["roles"] = claims["roles"]

        if not extra_data.get("email"):
            extra_data["email"] = extra_data.get("upn") or extra_data.get("preferred_username")

        return self.get_provider().sociallogin_from_response(request, extra_data)


class SSOSocialAccountAdapter(DefaultSocialAccountAdapter):
    """Auto-provision users coming from Entra ID and apply role mapping."""

    def pre_social_login(self, request, sociallogin):
        # Connect the incoming identity to an existing local account that shares
        # the same e-mail address instead of creating a duplicate user.
        if sociallogin.is_existing or not settings.SSO_CONNECT_BY_EMAIL:
            return

        email = (sociallogin.account.extra_data or {}).get("email")
        if not email:
            return

        try:
            user = User.objects.get(email__iexact=email)
        except (User.DoesNotExist, User.MultipleObjectsReturned):
            return

        sociallogin.connect(request, user)

    def save_user(self, request, sociallogin, form=None):
        user = super().save_user(request, sociallogin, form)
        sync_sso_user_roles(user, sociallogin.account.extra_data)
        return user


def _on_social_account_changed(sender, request=None, sociallogin=None, **kwargs):
    if sociallogin is not None:
        sync_sso_user_roles(sociallogin.user, sociallogin.account.extra_data)


def register_sso_signals():
    from allauth.socialaccount.signals import social_account_added, social_account_updated

    social_account_added.connect(
        _on_social_account_changed, dispatch_uid="cvat.apps.iam.sso.social_account_added"
    )
    social_account_updated.connect(
        _on_social_account_changed, dispatch_uid="cvat.apps.iam.sso.social_account_updated"
    )


def oidc_login(request, provider_id):
    view = OAuth2LoginView.adapter_view(EntraOIDCAdapter(request, provider_id))
    return view(request)


def oidc_callback(request, provider_id):
    view = OAuth2CallbackView.adapter_view(EntraOIDCAdapter(request, provider_id))
    return view(request)


class SSOLoginRedirectView(APIView):
    """
    Entry point used by the frontend "Sign in with Microsoft" button.

    It simply starts the OpenID Connect authorization code flow by redirecting the
    browser to the identity provider. Having it as a documented API endpoint also
    lets the UI detect (via the API schema) that SSO is available.
    """

    authentication_classes = []
    permission_classes = [AllowAny]
    serializer_class = None

    @extend_schema(
        summary="Start the Azure Entra ID single sign-on flow",
        responses={302: OpenApiResponse(description="Redirect to the identity provider")},
    )
    def get(self, request):
        login_url = reverse(
            "openid_connect_login",
            kwargs={"provider_id": settings.SSO_AZURE_PROVIDER_ID},
        )
        query = request.GET.urlencode()
        if query:
            login_url = f"{login_url}?{query}"
        return redirect(login_url)
