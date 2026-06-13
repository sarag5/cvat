# Copyright (C) CVAT.ai Corporation
#
# SPDX-License-Identifier: MIT

"""
URL patterns that power the Azure Entra ID OpenID Connect flow.

The view names (``openid_connect_login`` / ``openid_connect_callback``) match the
ones django-allauth's generic OpenID Connect provider expects, so the provider's
``get_login_url`` / ``get_callback_url`` helpers resolve to these custom views,
which use :class:`cvat.apps.iam.sso.EntraOIDCAdapter` (decodes app roles from the
ID token) instead of the stock adapter.
"""

from django.urls import include, path

from cvat.apps.iam import sso

oidc_urlpatterns = [
    path("oidc/<provider_id>/login/", sso.oidc_login, name="openid_connect_login"),
    path(
        "oidc/<provider_id>/login/callback/",
        sso.oidc_callback,
        name="openid_connect_callback",
    ),
]

urlpatterns = [
    # Support views used by allauth while completing a social login
    # (signup, login error/cancelled pages, account connections).
    path("accounts/", include("allauth.urls")),
    path("accounts/", include(oidc_urlpatterns)),
]
