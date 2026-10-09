# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

import pytest


@pytest.fixture(autouse=True)
def _gateway_login_store_off_the_real_home(tmp_path_factory, monkeypatch):
    """Keep every test away from the developer's own gateway login.

    `lager devenv terminal` and `lager exec` create the store before mounting
    it, so a test that reaches them with the real HOME writes to the machine
    it runs on. Tests of the default location unset this and point HOME at a
    temporary directory themselves.
    """
    store = tmp_path_factory.mktemp('gateway_login') / 'gateway_auth.json'
    monkeypatch.setenv('LAGER_GATEWAY_AUTH_FILE', str(store))
