from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import UUID

import pytest
import test_operator_product_service as service_helpers
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from k5vision.domain.users import UserAccount, UserRole
from k5vision.operator_storage_settings_api import install_operator_storage_settings_api

native = service_helpers.native
_URL = "/api/v1/operator/admin/storage"


@pytest.fixture
def storage_api(native):
    app = FastAPI()
    accounts = {
        role.value: UserAccount(
            id=UUID(int=index), username="storage-" + role.value, display_name=role.value, role=role
        )
        for index, role in enumerate(UserRole, 1)
    }
    observed = {"origin_checked": 0}

    async def authenticate(token):
        account = accounts.get(token)
        if account is None:
            raise HTTPException(401, "unauthorized")
        return account, token

    async def require_admin(request: Request):
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        account, _ = await authenticate(token)
        if account.role is not UserRole.ADMINISTRATOR:
            raise HTTPException(403, "forbidden")
        if request.headers.get("origin") not in (None, "http://testserver"):
            raise HTTPException(403, "invalid_origin")
        observed["origin_checked"] += 1
        return account, token

    install_operator_storage_settings_api(
        app,
        service=native.service,
        require_admin=require_admin,
        authenticate=authenticate,
        mutations=asyncio.Lock(),
    )
    with TestClient(app) as client:
        yield SimpleNamespace(
            service=native.service,
            client=client,
            accounts=accounts,
            observed=observed,
            headers={"Authorization": "Bearer administrator"},
        )


def test_administrator_reads_live_usage_and_saves_persistent_policy(storage_api):
    value = storage_api
    before = value.client.get(_URL, headers=value.headers)
    assert before.status_code == 200 and before.headers["cache-control"] == "no-store"
    assert before.json()["inspection_available"] is False
    assert before.json()["recording_bytes"] is None
    response = value.client.patch(
        _URL,
        headers=value.headers,
        json={"expected_revision": 0, "quota_bytes": 30 * 1024**3, "retention_seconds": 0},
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["quota_bytes"] == value.service.max_recording_bytes == 30 * 1024**3
    assert value.service.storage_settings.snapshot()["retention_seconds"] == 0
    assert value.observed["origin_checked"] == 2


@pytest.mark.parametrize("role", ["viewer", "operator", "missing"])
def test_nonadministrators_cannot_observe_private_storage_path_or_change_policy(storage_api, role):
    value = storage_api
    headers = {"Authorization": "Bearer " + role}
    for method in ("get", "patch"):
        options = (
            {"json": {"expected_revision": 0, "quota_bytes": 100}} if method == "patch" else {}
        )
        response = getattr(value.client, method)(_URL, headers=headers, **options)
        assert response.status_code == (401 if role == "missing" else 403)
        assert str(value.service.recording_root) not in response.text
    assert value.service.storage_settings.snapshot()["revision"] == 0


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"expected_revision": 0},
        {"expected_revision": 0, "quota_bytes": True},
        {"expected_revision": 0, "quota_bytes": 1.0},
        {"expected_revision": 0, "retention_seconds": -1},
        {"expected_revision": 0, "retention_seconds": None},
        {"expected_revision": 0, "minimum_free_bytes": -1},
        {"expected_revision": 0, "recording_directory": None},
        {"expected_revision": 0, "arbitrary": "no"},
    ],
)
def test_invalid_patches_never_mutate(storage_api, payload):
    value = storage_api
    response = value.client.patch(_URL, headers=value.headers, json=payload)
    assert response.status_code == 422
    assert value.service.storage_settings.snapshot()["revision"] == 0


def test_revision_conflict_returns_safe_error_and_does_not_overwrite_newer_policy(storage_api):
    value = storage_api
    assert (
        value.client.patch(
            _URL, headers=value.headers, json={"expected_revision": 0, "retention_seconds": 0}
        ).status_code
        == 200
    )
    stale = value.client.patch(
        _URL, headers=value.headers, json={"expected_revision": 0, "retention_seconds": 300}
    )
    assert stale.status_code == 409 and stale.json()["detail"] == "storage_conflict"
    assert value.service.storage_settings.snapshot()["retention_seconds"] == 0


def test_cross_origin_mutation_is_rejected_before_persistence(storage_api):
    value = storage_api
    response = value.client.patch(
        _URL,
        headers={**value.headers, "Origin": "https://other-origin.example"},
        json={"expected_revision": 0, "retention_seconds": 0},
    )
    assert response.status_code == 403
    assert value.service.storage_settings.snapshot()["revision"] == 0


def test_invalid_directory_has_safe_finite_error(storage_api):
    value = storage_api
    response = value.client.patch(
        _URL,
        headers=value.headers,
        json={"expected_revision": 0, "recording_directory": "relative-private-value"},
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "storage_directory_refused"
    assert "relative-private-value" not in response.text
