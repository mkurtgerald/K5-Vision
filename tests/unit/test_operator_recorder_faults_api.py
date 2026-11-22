"""Durable recorder alerts use current camera and role grants after I/O."""

from __future__ import annotations

import copy
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from test_operator_product_api import _initialized_session
from test_operator_product_api import product as product_fixture

from k5vision.domain.users import UserCreate, UserPatch, UserRole

product = product_fixture
_ROUTE = "/api/v1/operator/status"
_PUBLIC = {"code", "camera_id", "state", "resolution", "occurred_at"}


def _event(identifier=None, *, number=1, state="active"):
    return {
        "id": number,
        "code": "storage_blocked" if identifier is None else "no_new_recording",
        "camera_id": identifier,
        "state": state,
        "resolution": None if state == "active" else "recording_observed",
        "occurred_at": "2026-11-22T06:00:00.000Z",
        "run_id": str(uuid4()),
        "elapsed_ms": number * 1000,
    }


def _faults(product, rows, *, callback=None):
    value = {"available": True, "monitoring": True, "active": rows[:], "history": rows[:]}

    def snapshot():
        if callback is not None:
            callback()
        return copy.deepcopy(value)

    product.app.state.operator_recorder_faults = SimpleNamespace(snapshot=snapshot)
    return value


def _reader(product, *, mode="selected", identifiers=()):
    account, _, _ = product.users.create_with_bootstrap(
        UserCreate(username="fault-reader", display_name="Reader", role=UserRole.OPERATOR),
        actor="test",
    )
    product.users.set_camera_access(
        account.id, mode=mode, camera_ids=[UUID(value) for value in identifiers], actor="test"
    )
    token, _ = _initialized_session(product, account)
    return account, {"Authorization": "Bearer " + token}


def test_status_alerts_require_session_and_public_rows_hide_internal_sequences(product):
    identifier = product.enroll()
    removed = str(uuid4())
    _faults(product, [_event(), _event(identifier, number=2), _event(removed, number=63)])
    assert product.client.get(_ROUTE).status_code == 401
    response = product.client.get(_ROUTE, headers=product.headers)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    alerts = response.json()["recorder_alerts"]
    assert alerts["visible"] and alerts["available"] and len(alerts["active"]) == 3
    assert all(set(row) == _PUBLIC for row in alerts["active"] + alerts["history"])
    assert removed in response.text  # Administrators retain removed-camera history.
    assert all(key not in response.text for key in ("run_id", "elapsed_ms", '"id":'))


@pytest.mark.parametrize("mode", ["selected", "all"])
def test_restricted_operator_never_sees_unknown_camera_rows_or_counts(product, mode):
    identifier = product.enroll()
    inaccessible = str(uuid4())
    _, headers = _reader(product, mode=mode, identifiers=[identifier] if mode == "selected" else [])
    # Restricted history must be filtered before the public 20-row bound.
    own = _event(identifier, state="resolved")
    _faults(product, [own] + [_event(inaccessible, number=n) for n in range(2, 32)])
    response = product.client.get(_ROUTE, headers=headers)
    assert response.status_code == 200
    alerts = response.json()["recorder_alerts"]
    assert alerts["history"] == [{key: own[key] for key in _PUBLIC}]
    assert inaccessible not in response.text
    assert all(set(row) == _PUBLIC for row in alerts["active"] + alerts["history"])
    assert set(alerts) == {"visible", "available", "monitoring", "active", "history"}


def test_operator_with_no_camera_grants_has_no_global_or_camera_fault_leak(product):
    identifier = product.enroll()
    _, headers = _reader(product)
    _faults(product, [_event(), _event(identifier)])
    response = product.client.get(_ROUTE, headers=headers)
    assert response.status_code == 200
    alerts = response.json()["recorder_alerts"]
    assert alerts["active"] == alerts["history"] == []
    assert identifier not in response.text


@pytest.mark.parametrize("during", ["snapshot", "selection"])
def test_grant_revocation_after_alert_or_camera_io_cannot_leak_stale_rows(
    product, monkeypatch, during
):
    identifier = product.enroll()
    account, headers = _reader(product, identifiers=[identifier])

    def revoke():
        product.users.set_camera_access(account.id, mode="selected", camera_ids=[], actor="test")

    _faults(
        product, [_event(), _event(identifier)], callback=revoke if during == "snapshot" else None
    )
    if during == "selection":
        original = product.cameras.list_sources

        def sources():
            result = original()
            revoke()
            return result

        monkeypatch.setattr(product.cameras, "list_sources", sources)
    response = product.client.get(_ROUTE, headers=headers)
    assert response.status_code in {200, 401}
    assert identifier not in response.text
    if response.status_code == 200:
        assert response.json()["recorder_alerts"]["active"] == []


def test_session_disabled_during_snapshot_cannot_receive_faults(product):
    identifier = product.enroll()
    account, headers = _reader(product, identifiers=[identifier])
    _faults(
        product,
        [_event(identifier)],
        callback=lambda: product.users.update(account.id, UserPatch(enabled=False), actor="test"),
    )
    response = product.client.get(_ROUTE, headers=headers)
    assert response.status_code == 401 and identifier not in response.text


@pytest.mark.parametrize("product", [{"roles": True}], indirect=True)
def test_custom_role_without_video_permissions_receives_only_invisible_marker(product):
    identifier = product.enroll()
    account, _ = _reader(product, identifiers=[identifier])
    roles = product.app.state.operator_roles
    profile = roles.upsert(
        product.users,
        {"name": "No video", "description": "", "base_role": "viewer", "permissions": []},
    )
    roles.assign(product.users, account.id, profile["id"])
    account = product.users.get(account.id)
    token, _ = _initialized_session(product, account)
    _faults(product, [_event(), _event(identifier)])
    response = product.client.get(_ROUTE, headers={"Authorization": "Bearer " + token})
    assert response.status_code == 200
    assert response.json()["recorder_alerts"] == {"visible": False}
    assert identifier not in response.text


@pytest.mark.parametrize("product", [{"roles": True}], indirect=True)
@pytest.mark.parametrize("boundary", ["role_policy", "final_camera_grant"])
def test_final_alert_authority_lock_does_not_block_unrelated_http(product, monkeypatch, boundary):
    identifier = product.enroll()
    _, headers = _reader(product, identifiers=[identifier])
    _faults(product, [_event(identifier)])
    entered, released = threading.Event(), threading.Event()
    target = product.app.state.operator_roles if boundary == "role_policy" else product.users
    method = "_read" if boundary == "role_policy" else "get_camera_access"
    original = getattr(target, method)
    calls = 0

    def held(*args, **kwargs):
        nonlocal calls
        calls += 1
        if boundary == "role_policy" or calls == 2:
            entered.set()
            assert released.wait(5), "Final alert authority was not released"
        return original(*args, **kwargs)

    monkeypatch.setattr(target, method, held)
    with ThreadPoolExecutor(max_workers=2) as executor:
        try:
            protected = executor.submit(product.client.get, _ROUTE, headers=headers)
            assert entered.wait(3), "Status did not reach the final durable authority"
            liveness = executor.submit(product.client.get, "/openapi.json")
            assert liveness.result(timeout=1.5).status_code == 200
            assert not protected.done()
        finally:
            released.set()
        assert protected.result(timeout=5).status_code == 200


@pytest.mark.parametrize("product", [{"roles": True}], indirect=True)
@pytest.mark.parametrize("change", ["disable", "password_reset", "grant", "role_policy"])
def test_final_authority_rejects_account_credential_grant_or_role_change(
    product, monkeypatch, change
):
    identifier = product.enroll()
    account, headers = _reader(product, identifiers=[identifier])
    _faults(product, [_event(identifier)])
    roles = product.app.state.operator_roles
    profile = roles.upsert(
        product.users,
        {
            "name": "Changed authority",
            "description": "",
            "base_role": "operator",
            "permissions": [],
        },
    )
    original = roles.permission_snapshot

    def changed(current):
        result = original(current)
        if change == "disable":
            product.users.update(account.id, UserPatch(enabled=False), actor="test")
        elif change == "password_reset":
            product.users.reset_bootstrap(account.id, actor="test")
        elif change == "grant":
            product.users.set_camera_access(
                account.id, mode="selected", camera_ids=[], actor="test"
            )
        else:
            roles.assign(product.users, account.id, profile["id"])
        return result

    monkeypatch.setattr(roles, "permission_snapshot", changed)
    response = product.client.get(_ROUTE, headers=headers)
    assert (
        response.status_code
        == {"disable": 401, "password_reset": 401, "grant": 200, "role_policy": 503}[change]
    )
    assert identifier not in response.text
    if change == "grant":
        assert response.json()["recorder_alerts"]["active"] == []
