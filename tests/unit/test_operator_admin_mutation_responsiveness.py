"""Installed user edits and password resets must not freeze unrelated HTTP traffic."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

import pytest
import test_operator_product_api as helpers
from test_operator_admin_api import _activate, _create

product = helpers.product
_ADMIN = "/api/v1/operator/admin"


@pytest.mark.parametrize(
    ("storage_method", "http_method", "payload"),
    [
        ("update", "patch", {"enabled": False}),
        ("reset_bootstrap", "post", {}),
    ],
)
def test_durable_user_mutation_does_not_stall_unrelated_http(
    product, monkeypatch, storage_method, http_method, payload
):
    """Hold the actual SQLite mutation while the same ASGI server serves readiness."""
    issue = _create(product, username="held-account-edit")
    token, _ = _activate(product, issue)
    user_id = issue["account"]["id"]
    entered, released = threading.Event(), threading.Event()
    original = getattr(product.users, storage_method)

    def controlled_mutation(*args, **kwargs):
        entered.set()
        assert released.wait(timeout=5), "Controlled user mutation was not released"
        return original(*args, **kwargs)

    monkeypatch.setattr(product.users, storage_method, controlled_mutation)
    path = _ADMIN + f"/users/{user_id}"
    if storage_method == "reset_bootstrap":
        path += "/reset-password"

    # The harness's TestClient context owns a shared ASGI event loop. With a
    # synchronous SQLite call on that loop, the independent GET cannot finish.
    with ThreadPoolExecutor(max_workers=2) as executor:
        try:
            write = executor.submit(
                getattr(product.client, http_method),
                path,
                headers=product.headers,
                json=payload,
            )
            assert entered.wait(timeout=3), "User mutation did not reach durable storage"
            liveness = executor.submit(product.client.get, "/openapi.json")
            assert liveness.result(timeout=1.5).status_code == 200
            assert not write.done(), "Durable mutation finished before release"
        finally:
            released.set()
        result = write.result(timeout=5)

    assert result.status_code == 200, result.text
    assert result.headers["cache-control"] == "no-store"
    if storage_method == "update":
        assert result.json()["enabled"] is False
        assert product.users.get(UUID(user_id)).enabled is False
    else:
        assert result.json()["account"]["credential_status"] == "pending"
        assert result.json()["temporary_credential"]
        assert product.users.credential_status(UUID(user_id)) == "pending"
    # Both mutations change the account's authentication authority.
    assert asyncio.run(product.sessions.resolve(token)) is None
