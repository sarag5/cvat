# Copyright (C) 2021 Intel Corporation
# Copyright (C) CVAT.ai Corporation
#
# SPDX-License-Identifier: MIT

from django.apps import AppConfig


class IAMConfig(AppConfig):
    name = "cvat.apps.iam"

    def ready(self):
        from django.conf import settings

        from .signals import register_signals

        register_signals(self)

        if getattr(settings, "SSO_ENABLED", False):
            from .sso import register_sso_signals

            register_sso_signals()
