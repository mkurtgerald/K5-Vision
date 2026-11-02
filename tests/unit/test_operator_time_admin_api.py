from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from k5vision.domain.users import UserAccount, UserRole
from k5vision.operator_time_admin import TimeAdministrationError
from k5vision.operator_time_admin_api import install_operator_time_admin_api

_PREFIX = "/api/v1/operator"


@pytest.fixture
def time_api():
    app = FastAPI()
    administrator = UserAccount(
        username="time-admin", display_name="Time administrator", role=UserRole.ADMINISTRATOR
    )
    viewer = UserAccount(username="time-viewer", display_name="Time viewer", role=UserRole.VIEWER)
    camera = SimpleNamespace(binding=SimpleNamespace(device_id=uuid4()))
    state = SimpleNamespace(active=True, camera=camera, calls=[], failure=None, after=None)
    mutations = asyncio.Lock()

    async def authenticate(token):
        if token not in {"admin-token", "viewer-token"} or not state.active:
            raise HTTPException(401, "authentication_required")
        return (administrator if token == "admin-token" else viewer), token

    async def require_admin(request: Request):
        account, token = await authenticate(
            request.headers.get("Authorization", "").removeprefix("Bearer ")
        )
        if account.role is not UserRole.ADMINISTRATOR:
            raise HTTPException(403, "administrator_required")
        if request.headers.get("Origin", "http://127.0.0.1:8000") != "http://127.0.0.1:8000":
            raise HTTPException(403, "invalid_origin")
        return account, token

    def selection(account, identifier):
        if state.camera is None or state.camera.binding.device_id != identifier:
            raise HTTPException(404, "camera_not_found")
        return state.camera

    class Service:
        async def host_status(self):
            state.calls.append(("host",))
            if state.failure:
                raise state.failure
            if state.after:
                state.after()
            return {"host": {"status": "available"}}

        async def host_operation(self, operation, **values):
            assert mutations.locked()
            state.calls.append((operation, values))
            if state.failure:
                raise state.failure
            if state.after:
                state.after()
            return {"operation": {"action": operation, "verified": False}}

        async def camera(self, source, action="read", **values):
            if action != "read":
                assert mutations.locked()
            state.calls.append((action, values))
            if state.failure:
                raise state.failure
            if state.after:
                state.after()
            return {"camera_id": str(source.binding.device_id), "operation": {"action": action}}

    install_operator_time_admin_api(
        app,
        time_admin=Service(),
        require_admin=require_admin,
        authenticate=authenticate,
        authorized_selection=selection,
        mutations=mutations,
    )
    with TestClient(app) as client:
        yield SimpleNamespace(
            client=client,
            state=state,
            headers={"Authorization": "Bearer admin-token"},
            camera=str(camera.binding.device_id),
        )


def test_host_time_controls_are_graphical_api_actions_with_current_admin_and_readback(time_api):
    value = time_api
    before = value.client.get(_PREFIX + "/admin/time", headers=value.headers)
    assert before.status_code == 200 and before.headers["cache-control"] == "no-store"
    configured = value.client.patch(
        _PREFIX + "/admin/time",
        headers=value.headers,
        json={"mode": "manual", "ntp_servers": ["TIME.Example."]},
    )
    assert configured.status_code == 200
    assert value.state.calls[-1] == (
        "configure",
        {"mode": "manual", "ntp_servers": ["time.example"]},
    )
    synchronized = value.client.post(
        _PREFIX + "/admin/time/synchronize", headers=value.headers, json={}
    )
    assert synchronized.status_code == 200 and synchronized.json()["operation"]["verified"] is False
    assert value.state.calls[-1] == ("resync", {})


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer viewer-token"},
        {"Authorization": "Bearer admin-token", "Origin": "https://foreign.invalid"},
    ],
)
def test_non_administrators_and_cross_origin_requests_cannot_trigger_uac(time_api, headers):
    value = time_api
    for method, path, payload in [
        ("patch", "/admin/time", {"mode": "manual", "ntp_servers": ["time.example"]}),
        ("post", "/admin/time/synchronize", {}),
        ("patch", f"/cameras/{value.camera}/time", {"from_dhcp": True}),
        ("post", f"/cameras/{value.camera}/time/synchronize", {"mode": "host"}),
    ]:
        response = getattr(value.client, method)(_PREFIX + path, headers=headers, json=payload)
        assert response.status_code in {401, 403}
    assert value.state.calls == []


@pytest.mark.parametrize(
    "payload",
    [
        {"mode": "manual", "ntp_servers": []},
        {"mode": "manual", "ntp_servers": ["server /config"]},
        {"mode": "manual", "ntp_servers": ["time.example"], "command": "private-value"},
        {"mode": "unrestricted", "ntp_servers": ["time.example"]},
    ],
)
def test_invalid_host_time_configuration_cannot_reach_native_helper(time_api, payload):
    response = time_api.client.patch(
        _PREFIX + "/admin/time", headers=time_api.headers, json=payload
    )
    assert response.status_code == 422
    assert time_api.state.calls == []


def test_camera_ntp_and_explicit_host_copy_are_separate_actions(time_api):
    value = time_api
    url = _PREFIX + f"/cameras/{value.camera}/time"
    assert value.client.get(url, headers=value.headers).status_code == 200
    configured = value.client.patch(
        url, headers=value.headers, json={"from_dhcp": False, "ntp_servers": ["time.example"]}
    )
    assert configured.status_code == 200
    assert value.state.calls[-1] == (
        "configure",
        {"from_dhcp": False, "ntp_servers": ["time.example"]},
    )
    copied = value.client.post(url + "/synchronize", headers=value.headers, json={"mode": "host"})
    assert copied.status_code == 200 and value.state.calls[-1] == ("synchronize", {})
    assert (
        value.client.post(
            url + "/synchronize", headers=value.headers, json={"mode": "auto"}
        ).status_code
        == 422
    )


@pytest.mark.parametrize("action", ["read", "configure", "sync"])
def test_camera_removal_during_await_discards_response(time_api, action):
    value = time_api
    value.state.after = lambda: setattr(value.state, "camera", None)
    url = _PREFIX + f"/cameras/{value.camera}/time"
    response = (
        value.client.get(url, headers=value.headers)
        if action == "read"
        else value.client.patch(url, headers=value.headers, json={"from_dhcp": True})
        if action == "configure"
        else value.client.post(url + "/synchronize", headers=value.headers, json={"mode": "host"})
    )
    assert response.status_code == 404


@pytest.mark.parametrize("action", ["read", "configure", "sync"])
def test_revocation_during_host_operation_does_not_return_admin_result(time_api, action):
    value = time_api
    value.state.after = lambda: setattr(value.state, "active", False)
    url = _PREFIX + "/admin/time"
    response = (
        value.client.get(url, headers=value.headers)
        if action == "read"
        else value.client.patch(
            url, headers=value.headers, json={"mode": "manual", "ntp_servers": ["time.example"]}
        )
        if action == "configure"
        else value.client.post(url + "/synchronize", headers=value.headers, json={})
    )
    assert response.status_code == 401


def test_time_backend_failures_are_finite_and_do_not_leak_private_values(time_api):
    value = time_api
    value.state.failure = TimeAdministrationError("camera_time_auth_failed")
    response = value.client.get(_PREFIX + f"/cameras/{value.camera}/time", headers=value.headers)
    assert response.status_code == 422 and response.json() == {"detail": "camera_time_auth_failed"}
    value.state.failure = TimeAdministrationError("time_elevation_cancelled")
    response = value.client.post(
        _PREFIX + "/admin/time/synchronize", headers=value.headers, json={}
    )
    assert response.status_code == 422 and response.json() == {"detail": "time_elevation_cancelled"}


def test_ephemeral_management_credentials_reach_only_selected_camera_operation(time_api):
    value = time_api
    url = _PREFIX + f"/cameras/{value.camera}/time"
    credentials = {"username": "management-admin", "password": "private-management-secret"}
    response = value.client.post(url + "/status", headers=value.headers, json=credentials)
    assert response.status_code == 200
    assert value.state.calls[-1] == ("read", credentials)
    assert "private-management-secret" not in response.text
    response = value.client.patch(
        url,
        headers=value.headers,
        json={**credentials, "from_dhcp": False, "ntp_servers": ["time.example"]},
    )
    assert response.status_code == 200
    assert value.state.calls[-1] == (
        "configure",
        {**credentials, "from_dhcp": False, "ntp_servers": ["time.example"]},
    )
    response = value.client.post(
        url + "/synchronize", headers=value.headers, json={**credentials, "mode": "host"}
    )
    assert response.status_code == 200
    assert value.state.calls[-1] == ("synchronize", credentials)
    assert "private-management-secret" not in response.text
    # An omitted override is not silently retained for the next operation.
    response = value.client.get(url, headers=value.headers)
    assert response.status_code == 200 and value.state.calls[-1] == ("read", {})


@pytest.mark.parametrize(
    "credentials",
    [
        {"username": "admin"},
        {"password": "private-value"},
        {"username": 123, "password": "private-value"},
        {"username": "admin", "password": 123},
        {"username": "admin\nuser", "password": "private-value"},
    ],
)
def test_partial_or_invalid_management_credentials_cannot_reach_camera(time_api, credentials):
    value = time_api
    url = _PREFIX + f"/cameras/{value.camera}/time/status"
    assert value.client.post(url, headers=value.headers, json=credentials).status_code == 422
    assert value.state.calls == []
