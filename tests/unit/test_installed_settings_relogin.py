"""Regression proof for role re-login inventory attribution.

A revoked session may emit a late camera response as a new browser session
authenticates. Only a response authorized by the new bearer can prove GUI
capabilities. All test identities are generated; no tokens reach artifacts.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


def _helper():
    path = Path(__file__).resolve().parents[1] / "integration" / "installed_settings_acceptance.py"
    spec = importlib.util.spec_from_file_location("k5_installed_settings_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Inventory:
    def __init__(self, token, status):
        self.status = status
        self.url = "http://127.0.0.1:5555/api/v1/operator/cameras"
        self.request = SimpleNamespace(
            method="GET",
            all_headers=lambda: {"authorization": "Bearer " + token},
        )

    def json(self):
        return {"capabilities": {"account": {"id": "generated-operator"}}}


class _Page:
    def __init__(self):
        self.handlers = []
        self.security_visible = False

    def on(self, event, handler):
        assert event == "response"
        self.handlers.append(handler)

    def remove_listener(self, event, handler):
        assert event == "response"
        self.handlers.remove(handler)

    def emit(self, response):
        for handler in tuple(self.handlers):
            handler(response)

    def wait_for_timeout(self, milliseconds):
        pytest.fail("The generated login already emitted its inventory response")

    def locator(self, selector):
        assert selector == "#security-tab"

        def visible(*, state):
            assert state == "visible"
            self.security_visible = True

        return SimpleNamespace(wait_for=visible)


class _Helpers:
    def __init__(self, stale_status, current_status):
        self.stale_status = stale_status
        self.current_status = current_status
        self.old_token = "old-" + "a" * 40
        self.new_token = "new-" + "b" * 40

    def _login(self, page, origin, username, password, *, progress=None):
        assert origin == "http://127.0.0.1:5555"
        assert username == "generated-user" and password == "generated-secret"
        assert callable(progress)
        progress("workspace_visible")
        page.emit(_Inventory(self.old_token, self.stale_status))
        page.emit(_Inventory(self.new_token, self.current_status))
        return self.new_token

    def _api(self, page, token, method, route):
        assert token == self.new_token
        assert (method, route) == ("GET", "/api/v1/auth/me")
        return 200, {"id": "generated-operator"}


@pytest.mark.parametrize(
    ("stale_status", "current_status", "succeeds"),
    [(401, 200, True), (200, 401, False)],
)
def test_canonical_inventory_is_attributed_to_current_login(stale_status, current_status, succeeds):
    module = _helper()
    page = _Page()
    helpers = _Helpers(stale_status, current_status)
    account = {
        "id": "generated-operator",
        "username": "generated-user",
        "password": "generated-secret",
    }
    diagnostic = {}
    args = (page, helpers, "http://127.0.0.1:5555", account)
    if succeeds:
        token, capabilities = module._canonical_login(*args, diagnostic=diagnostic)
        assert token == helpers.new_token
        assert capabilities["account"]["id"] == account["id"]
        assert page.security_visible
        assert diagnostic["settings_relogin_stale_inventory_responses"] == 1
    else:
        with pytest.raises(pytest.fail.Exception, match="Canonical camera authority"):
            module._canonical_login(*args, diagnostic=diagnostic)
        assert not page.security_visible
    assert diagnostic["settings_relogin_inventory_http_status"] == current_status
    assert page.handlers == []
