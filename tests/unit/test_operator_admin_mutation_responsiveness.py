"""Installed user edits and password resets must not freeze unrelated HTTP traffic."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

import httpx
import pytest
import test_operator_product_api as helpers
from test_operator_admin_api import _activate, _create

from k5vision.services.user_registry import UserRegistryStorageError

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


@pytest.mark.parametrize(
    ("storage_method", "http_method", "payload"),
    [
        ("update", "patch", {"enabled": False}),
        ("reset_bootstrap", "post", {}),
    ],
)
def test_revocation_precedes_a_failed_post_commit_response_read(
    product, monkeypatch, storage_method, http_method, payload
):
    """A response-row failure after an authority change must not preserve a session."""
    issue = _create(product, username="post-commit-account")
    token, _ = _activate(product, issue)
    user_id = UUID(issue["account"]["id"])
    completed = threading.Event()
    original_mutation = getattr(product.users, storage_method)
    original_status = product.users.credential_status

    def committed_mutation(*args, **kwargs):
        result = original_mutation(*args, **kwargs)
        completed.set()
        return result

    def fail_enrichment(identifier):
        if identifier == user_id and completed.is_set():
            raise UserRegistryStorageError("simulated post-commit storage refusal")
        return original_status(identifier)

    monkeypatch.setattr(product.users, storage_method, committed_mutation)
    monkeypatch.setattr(product.users, "credential_status", fail_enrichment)
    path = _ADMIN + f"/users/{user_id}"
    if storage_method == "reset_bootstrap":
        path += "/reset-password"
    response = getattr(product.client, http_method)(path, headers=product.headers, json=payload)
    assert response.status_code == 503
    assert response.json()["detail"] == "admin_unavailable"
    assert completed.is_set(), "The mutation must have committed before response failure"
    assert asyncio.run(product.sessions.resolve(token)) is None
    monkeypatch.setattr(product.users, "credential_status", original_status)
    if storage_method == "update":
        assert product.users.get(user_id).enabled is False
    else:
        assert product.users.credential_status(user_id) == "pending"


def test_cancelled_password_reset_finishes_commit_and_revocation(product, monkeypatch):
    """HTTP cancellation must not release the mutation lease while SQLite is busy."""
    issue = _create(product, username="cancelled-reset-user")
    token, _ = _activate(product, issue)
    user_id = UUID(issue["account"]["id"])
    entered, released = threading.Event(), threading.Event()
    original = product.users.reset_bootstrap

    def suspended_reset(*args, **kwargs):
        entered.set()
        assert released.wait(timeout=5), "Controlled reset was not released"
        return original(*args, **kwargs)

    monkeypatch.setattr(product.users, "reset_bootstrap", suspended_reset)

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=product.app),
            base_url="http://127.0.0.1:8080",
        ) as client:
            task = asyncio.create_task(
                client.post(
                    _ADMIN + f"/users/{user_id}/reset-password",
                    headers=product.headers,
                    json={},
                )
            )
            try:
                assert await asyncio.to_thread(entered.wait, 3)
                task.cancel()
                await asyncio.sleep(0.05)
                assert not task.done(), "Cancelled request abandoned an in-flight write"
            finally:
                released.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=5)
            assert await product.sessions.resolve(token) is None

    asyncio.run(exercise())
    assert product.users.credential_status(user_id) == "pending"
