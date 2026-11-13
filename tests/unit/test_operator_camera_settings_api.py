from __future__ import annotations

import asyncio
import copy
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from k5vision.domain.devices import Device, DeviceProtocol
from k5vision.domain.users import UserAccount, UserRole
from k5vision.media.mediamtx_config import MediaMTXSource
from k5vision.media.mediamtx_provider import MediaMTXBinding
from k5vision.operator_camera_settings_api import (
    CameraSettingsVault,
    install_operator_camera_settings_api,
)
from k5vision.operator_role_policy import BASE_PERMISSIONS


class _Error(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__("private camera failure must never be returned")


def _description(source):
    return {
        "camera_id": str(source.device.id),
        "status": "available",
        "protocol": "media1",
        "current": {
            "codec": "H264",
            "frame_rate": 30,
            "resolution": {"width": 640, "height": 360},
            "bitrate_kbps": 1000,
            "gop": 30,
            "quality": 5,
            "encoding_interval": 1,
        },
        "options": {
            "codecs": [
                {"codec": "H264", "device_supported": True, "product_supported": True},
                {"codec": "H265", "device_supported": True, "product_supported": False},
            ],
            "resolutions": [{"width": 640, "height": 360}, {"width": 1280, "height": 720}],
            "frame_rate": {"min": 1, "max": 60, "values": [15, 30, 60], "integer_only": True},
            "bitrate_kbps": {"min": 64, "max": 8192},
            "gop": {"min": 1, "max": 120},
            "quality": {"min": 0, "max": 10},
            "encoding_interval": {"min": 1, "max": 10},
            "writable_fields": [
                "codec",
                "frame_rate",
                "resolution",
                "bitrate_kbps",
                "gop",
                "quality",
                "encoding_interval",
            ],
        },
        "shared_configuration": False,
        "shared_profile_count": 1,
        "writable": True,
        "writable_reason": None,
        "configuration_readback_verified": False,
        "stream_verified": False,
        "observed_fps": None,
    }


class _Inspection:
    def __init__(self, source, username=None, password=None):
        self.source, self.username, self.password = source, username, password
        self.description = _description(source)
        self.cleared = False

    def public(self):
        return copy.deepcopy(self.description)

    def clear(self):
        self.cleared = True
        self.source = self.username = self.password = None


class _Result(_Inspection):
    def __init__(self, source, patch):
        super().__init__(source)
        self.source_uri = source.source_uri + "-fresh"
        self.description["current"].update(
            {k: v for k, v in patch.items() if k != "acknowledge_shared"}
        )
        self.description.update(configuration_readback_verified=True, requested=patch)

    def clear(self):
        super().clear()
        self.source_uri = ""


class _Service:
    def __init__(self):
        self.inspections, self.results, self.writes = [], [], []
        self.read_hook = self.preflight_hook = self.write_hook = None
        self.error = None
        self.closed = False

    async def inspect(self, source, *, username=None, password=None):
        inspection = _Inspection(source, username, password)
        self.inspections.append(inspection)
        if self.read_hook is not None:
            await self.read_hook(inspection)
        return inspection

    def validate_patch(self, inspection, patch):
        if patch.get("frame_rate", 30) not in {15, 30, 60}:
            raise _Error("video_invalid_request")
        if inspection.description["shared_configuration"] and not patch["acknowledge_shared"]:
            raise _Error("video_shared_configuration")
        for field in ("bitrate_kbps", "gop"):
            if field in patch:
                rule = inspection.description["options"][field]
                if not rule["min"] <= patch[field] <= rule["max"]:
                    raise _Error("video_invalid_request")
        return patch

    async def apply(self, inspection, patch, *, before_write=None):
        if self.preflight_hook is not None:
            await self.preflight_hook()
        if before_write is not None:
            await before_write()
        self.writes.append(patch)
        if self.write_hook is not None:
            await self.write_hook()
        if self.error is not None:
            raise self.error
        result = _Result(inspection.source, patch)
        self.results.append(result)
        return result

    async def aclose(self):
        self.closed = True


def _harness():
    app = FastAPI()
    user = UserAccount(
        username="settings-admin", display_name="Settings", role=UserRole.ADMINISTRATOR
    )
    token = "a" * 43
    accounts = {token: user}
    sources = {}
    for _ in range(2):
        device = Device(
            name="Fixture camera",
            host="127.0.0.1",
            protocols={DeviceProtocol.ONVIF, DeviceProtocol.RTSP},
        )
        sources[device.id] = MediaMTXSource(
            MediaMTXBinding("fixture-site", device.id),
            device,
            "rtsp://stream-user:private-stream-password@127.0.0.1/live",
        )
    service = _Service()
    lock = asyncio.Lock()
    permissions = {"manage": True}
    auth_hook = {"callback": None}
    app.state.operator_roles = SimpleNamespace(
        permissions=lambda account: (
            BASE_PERMISSIONS[account.role] if permissions["manage"] else frozenset()
        )
    )

    async def authenticate(bearer):
        if auth_hook["callback"] is not None:
            await auth_hook["callback"]()
        if bearer not in accounts:
            raise HTTPException(401, "Unauthorized")
        return accounts[bearer], bearer

    async def require_admin(request: Request):
        bearer = request.headers.get("authorization", "").removeprefix("Bearer ")
        account, _ = await authenticate(bearer)
        if account.role is not UserRole.ADMINISTRATOR:
            raise HTTPException(403, "Administrator required")
        if request.headers.get("origin", "http://testserver") != "http://testserver":
            raise HTTPException(403, "Invalid origin")
        return account, bearer

    def selection(account, camera_id):
        if camera_id not in sources:
            raise HTTPException(404, "Camera not found")
        return sources[camera_id]

    commits, fallbacks = [], []

    async def commit(source, inspection, result):
        commits.append(source.device.id)
        sources[source.device.id] = replace(source, source_uri=result.source_uri)
        return {
            "stream_verified": False,
            "stream_status": "connecting",
            "recording_state": "waiting",
            "reconnect_requested": True,
        }

    async def reconcile_failed(source, inspection):
        assert not inspection.cleared
        assert lock.locked()
        fallbacks.append(source.device.id)

    vault = install_operator_camera_settings_api(
        app,
        require_admin=require_admin,
        authenticate=authenticate,
        selection=selection,
        mutations=lock,
        commit_settings=commit,
        reconcile_failed_write=reconcile_failed,
        service=service,
    )
    return SimpleNamespace(
        app=app,
        user=user,
        token=token,
        accounts=accounts,
        sources=sources,
        service=service,
        vault=vault,
        lock=lock,
        commits=commits,
        fallbacks=fallbacks,
        permissions=permissions,
        auth_hook=auth_hook,
        headers={"Authorization": "Bearer " + token},
        ids=list(sources),
    )


@pytest.fixture
def product():
    state = _harness()
    with TestClient(state.app) as client:
        state.client = client
        yield state


def _route(product, identifier=None):
    return "/api/v1/operator/cameras/" + str(identifier or product.ids[0]) + "/video-settings"


def _read(product, *, headers=None):
    response = product.client.get(_route(product), headers=headers or product.headers)
    assert response.status_code == 200, response.text
    return response.json()


def _apply(product, handle, **changes):
    return product.client.patch(
        _route(product),
        headers=product.headers,
        json={"settings_handle": handle, "frame_rate": 15, **changes},
    )


def test_read_and_apply_are_private_one_use_and_do_not_invent_stream_fps(product):
    read = _read(product)
    assert read["current"]["frame_rate"] == 30
    assert 0 < read["expires_in_seconds"] <= 120
    assert len(read["source_revision"]) == 64
    assert product.token not in repr(product.vault._leases)
    response = _apply(product, read["settings_handle"])
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    data = response.json()
    assert data["current"]["frame_rate"] == 15
    assert data["configuration_readback_verified"] is True
    assert data["stream_verified"] is False and data["observed_fps"] is None
    assert data["source_revision"] != read["source_revision"]
    assert "settings_handle" not in data
    assert "private-stream-password" not in response.text
    assert len(product.service.writes) == len(product.commits) == 1
    assert _apply(product, read["settings_handle"]).status_code == 410
    assert all(item.cleared for item in product.service.inspections + product.service.results)


@pytest.mark.parametrize("role", [UserRole.VIEWER, UserRole.OPERATOR])
def test_nonadministrators_cannot_read_or_write_settings(product, role):
    product.accounts[product.token] = product.user.model_copy(update={"role": role})
    assert product.client.get(_route(product), headers=product.headers).status_code == 403
    assert not product.service.inspections


def test_lost_canonical_permission_discards_private_lease(product):
    read = _read(product)
    product.permissions["manage"] = False
    assert _apply(product, read["settings_handle"]).status_code == 403
    assert product.service.inspections[0].cleared
    assert not product.vault._leases and not product.service.writes


def test_origin_missing_session_and_management_credential_pair_validation(product):
    assert product.client.get(_route(product)).status_code == 401
    response = product.client.post(
        _route(product) + "/read", headers=product.headers, json={"username": "private-manager"}
    )
    assert response.status_code == 422 and "private-manager" not in response.text
    response = product.client.post(
        _route(product) + "/read",
        headers=product.headers,
        json={"username": "private-manager", "password": "private-management-password"},
    )
    assert response.status_code == 200
    assert product.service.inspections[-1].password == "private-management-password"
    assert "private-management-password" not in response.text
    assert (
        product.client.get(
            _route(product), headers={**product.headers, "Origin": "https://external.invalid"}
        ).status_code
        == 403
    )


def test_expiry_uses_monotonic_deadline_and_clears_credentials(product, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("k5vision.operator_camera_settings_api.monotonic", lambda: clock[0])
    read = _read(product)
    clock[0] = 220.0
    assert _apply(product, read["settings_handle"]).status_code == 410
    assert product.service.inspections[0].cleared
    assert not product.vault._leases


def test_owner_session_camera_and_source_bindings_are_enforced(product):
    read = _read(product)
    other = "b" * 43
    product.accounts[other] = product.user
    body = {"settings_handle": read["settings_handle"], "frame_rate": 15}
    assert (
        product.client.patch(
            _route(product), headers={"Authorization": "Bearer " + other}, json=body
        ).status_code
        == 410
    )
    assert (
        product.client.patch(
            _route(product, product.ids[1]), headers=product.headers, json=body
        ).status_code
        == 410
    )
    source = product.sources[product.ids[0]]
    product.sources[source.device.id] = replace(source, source_uri=source.source_uri + "-changed")
    assert _apply(product, read["settings_handle"]).json()["detail"] == "video_source_changed"
    assert not product.service.writes
    assert product.service.inspections[0].cleared


def test_camera_removal_and_replaced_reads_clear_old_inspections(product):
    first = _read(product)
    second = _read(product)
    assert product.service.inspections[0].cleared
    assert _apply(product, first["settings_handle"]).status_code == 410
    del product.sources[product.ids[0]]
    assert _apply(product, second["settings_handle"]).status_code == 404
    assert product.service.inspections[1].cleared


def test_vault_capacity_is_bounded_and_revisions_are_not_raw_password_hashes(product):
    for _index in range(16):
        source = list(product.sources.values())[0]
        device = source.device.model_copy(update={"id": uuid4()})
        source = replace(source, device=device, binding=MediaMTXBinding("fixture-site", device.id))
        product.vault.reserve((product.user, product.token), device.id, source)
    assert len(product.vault._leases) == 16
    assert product.client.get(_route(product), headers=product.headers).status_code == 429
    alternate = CameraSettingsVault(product.service)
    source = product.sources[product.ids[0]]
    assert product.vault.source_revision(source) != alternate.source_revision(source)


@pytest.mark.parametrize(
    "changes",
    [
        {"frame_rate": True},
        {"frame_rate": "15"},
        {"frame_rate": 61},
        {"codec": "H265"},
        {"password": "private-forbidden"},
    ],
)
def test_invalid_changes_never_write_or_echo_secrets(product, changes):
    read = _read(product)
    response = _apply(product, read["settings_handle"], **changes)
    assert response.status_code == 422
    assert "private-forbidden" not in response.text
    assert not product.service.writes


def test_shared_configuration_requires_explicit_acknowledgment(product):
    async def shared(inspection):
        inspection.description["shared_configuration"] = True
        inspection.description["shared_profile_count"] = 2

    product.service.read_hook = shared
    assert (
        _apply(product, _read(product)["settings_handle"]).json()["detail"]
        == "video_shared_configuration"
    )
    read = _read(product)
    assert _apply(product, read["settings_handle"], shared_config_ack=True).status_code == 200
    assert len(product.service.writes) == 1


@pytest.mark.parametrize("field", ["bitrate_kbps", "gop"])
def test_advertised_zero_encoder_value_is_accepted_without_inventing_a_range(product, field):
    async def zero_option(inspection):
        inspection.description["options"][field]["min"] = 0

    product.service.read_hook = zero_option
    read = _read(product)
    assert read["options"][field]["min"] == 0
    response = _apply(product, read["settings_handle"], **{field: 0})
    assert response.status_code == 200
    assert response.json()["current"][field] == 0
    assert product.service.writes[0][field] == 0


@pytest.mark.parametrize("field", ["bitrate_kbps", "gop"])
@pytest.mark.parametrize("value", [-1, 0])
def test_negative_and_unadvertised_zero_encoder_values_never_dispatch(product, field, value):
    read = _read(product)
    assert read["options"][field]["min"] > 0
    response = _apply(product, read["settings_handle"], **{field: value})
    assert response.status_code == 422
    assert not product.service.writes and not product.commits and not product.fallbacks


def test_revocation_during_device_preflight_prevents_external_write(product):
    read = _read(product)

    async def revoke():
        del product.accounts[product.token]

    product.service.preflight_hook = revoke
    assert _apply(product, read["settings_handle"]).status_code == 401
    assert not product.service.writes and not product.commits
    assert not product.fallbacks
    assert product.service.inspections[0].cleared


@pytest.mark.parametrize("change", ["revoke", "source", "permission"])
def test_inspection_rechecks_authority_and_source_after_await(product, change):
    async def change_during_read(_inspection):
        if change == "revoke":
            del product.accounts[product.token]
        elif change == "permission":
            product.permissions["manage"] = False
        else:
            source = product.sources[product.ids[0]]
            product.sources[source.device.id] = replace(
                source, source_uri=source.source_uri + "-new"
            )

    product.service.read_hook = change_during_read
    response = product.client.get(_route(product), headers=product.headers)
    assert response.status_code == {"revoke": 401, "permission": 403, "source": 409}[change]
    assert not product.vault._leases and product.service.inspections[0].cleared


def test_expiry_during_fresh_preflight_prevents_external_write(product, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("k5vision.operator_camera_settings_api.monotonic", lambda: clock[0])
    read = _read(product)

    async def expire():
        clock[0] = 220.0

    product.service.preflight_hook = expire
    assert _apply(product, read["settings_handle"]).status_code == 410
    assert not product.service.writes


def test_sensitive_unknown_public_fields_are_not_exposed(product):
    async def malicious(inspection):
        inspection.description["options"]["password"] = "private-management-password"

    product.service.read_hook = malicious
    response = product.client.get(_route(product), headers=product.headers)
    assert response.status_code == 502
    assert "private-management-password" not in response.text
    assert product.service.inspections[0].cleared


def test_oversized_and_invalid_management_requests_do_not_echo_credentials(product):
    response = product.client.post(
        _route(product) + "/read",
        headers={**product.headers, "Content-Type": "application/json"},
        content=b'{"password":"private-management-password' + b"x" * 16384,
    )
    assert response.status_code == 413 and "private-management-password" not in response.text
    response = product.client.post(
        _route(product) + "/read",
        headers=product.headers,
        json={"username": "user", "password": {"private-management-password": True}},
    )
    assert response.status_code == 422 and "private-management-password" not in response.text


def test_revocation_after_started_write_finishes_reconciliation_without_returning_payload(product):
    read = _read(product)

    async def revoke():
        del product.accounts[product.token]

    product.service.write_hook = revoke
    assert _apply(product, read["settings_handle"]).status_code == 401
    assert len(product.service.writes) == len(product.commits) == 1
    assert product.service.results[0].cleared


def test_uncertain_device_failure_does_not_retry_or_claim_success(product):
    product.service.error = _Error("video_outcome_unverified")
    read = _read(product)
    response = _apply(product, read["settings_handle"])
    assert response.status_code == 502
    assert response.json()["detail"] == "video_outcome_unverified"
    assert len(product.service.writes) == 1 and not product.commits
    assert product.fallbacks == [product.ids[0]]
    assert _apply(product, read["settings_handle"]).status_code == 410


def test_direct_rtsp_has_honest_read_only_settings(product):
    source = product.sources[product.ids[0]]
    product.sources[source.device.id] = replace(
        source, device=source.device.model_copy(update={"protocols": {DeviceProtocol.RTSP}})
    )
    response = _read(product)
    assert response["writable"] is False and response["current"] is None
    assert response["observed_fps"] is None and "settings_handle" not in response
    assert not product.service.inspections


@pytest.mark.parametrize("uncertain", [False, True])
def test_cancellation_and_shutdown_join_started_write_before_private_state_release(uncertain):
    async def scenario():
        state = _harness()
        entered, release = asyncio.Event(), asyncio.Event()

        async def hold_write():
            entered.set()
            await release.wait()

        state.service.write_hook = hold_write
        state.service.error = _Error("video_outcome_unverified") if uncertain else None
        transport = httpx.ASGITransport(app=state.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get(_route(state), headers=state.headers)
            handle = response.json()["settings_handle"]
            task = asyncio.create_task(
                client.patch(
                    _route(state),
                    headers=state.headers,
                    json={"settings_handle": handle, "frame_rate": 15},
                )
            )
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done() and not state.service.inspections[0].cleared
            competing = await client.patch(
                _route(state),
                headers=state.headers,
                json={"settings_handle": handle, "frame_rate": 30},
            )
            assert competing.status_code == 409
            closing = asyncio.create_task(state.vault.aclose())
            await asyncio.sleep(0)
            assert not closing.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.wait_for(closing, 1)
            assert len(state.service.writes) == 1
            assert len(state.commits) == (0 if uncertain else 1)
            assert len(state.fallbacks) == (1 if uncertain else 0)
            assert all(item.cleared for item in state.service.inspections + state.service.results)
            assert not state.vault._leases

    asyncio.run(scenario())


def test_revoked_actor_still_gets_failed_write_reconciled_without_private_response(product):
    read = _read(product)
    product.service.error = _Error("video_readback_mismatch")

    async def revoke():
        del product.accounts[product.token]

    product.service.write_hook = revoke
    assert _apply(product, read["settings_handle"]).status_code == 401
    assert product.fallbacks == [product.ids[0]] and not product.commits


def test_binding_stream_change_invalidates_settings_before_dispatch(product):
    read = _read(product)
    source = product.sources[product.ids[0]]
    product.sources[source.device.id] = replace(
        source, binding=replace(source.binding, stream_token="other")
    )
    assert _apply(product, read["settings_handle"]).json()["detail"] == "video_source_changed"
    assert not product.service.writes and not product.fallbacks


def test_cancellation_during_preflight_prevents_later_set_dispatch():
    async def scenario():
        state = _harness()
        entered, release = asyncio.Event(), asyncio.Event()

        async def hold_preflight():
            entered.set()
            await release.wait()

        state.service.preflight_hook = hold_preflight
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=state.app), base_url="http://testserver"
        ) as client:
            response = await client.get(_route(state), headers=state.headers)
            handle = response.json()["settings_handle"]
            request = asyncio.create_task(
                client.patch(
                    _route(state),
                    headers=state.headers,
                    json={"settings_handle": handle, "frame_rate": 15},
                )
            )
            await asyncio.wait_for(entered.wait(), 1)
            request.cancel()
            await asyncio.sleep(0)
            assert not request.done() and not state.service.inspections[0].cleared
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await request
            assert not state.service.writes and not state.commits and not state.fallbacks
            assert state.service.inspections[0].cleared and not state.vault._leases
            await state.vault.aclose()

    asyncio.run(scenario())


def test_shutdown_clears_postcritical_inspection_before_request_finally():
    async def scenario():
        state = _harness()
        entered, release = asyncio.Event(), asyncio.Event()

        async def hold_final_auth():
            if state.commits:
                entered.set()
                await release.wait()

        state.auth_hook["callback"] = hold_final_auth
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=state.app), base_url="http://testserver"
        ) as client:
            response = await client.get(_route(state), headers=state.headers)
            request = asyncio.create_task(
                client.patch(
                    _route(state),
                    headers=state.headers,
                    json={"settings_handle": response.json()["settings_handle"], "frame_rate": 15},
                )
            )
            await asyncio.wait_for(entered.wait(), 1)
            assert not state.vault._tasks and len(state.vault._leases) == 1
            assert not state.service.inspections[0].cleared
            await state.vault.aclose()
            assert state.service.inspections[0].cleared and not state.vault._leases
            release.set()
            response = await request
            assert response.status_code == 503 and response.json()["detail"] == "video_closed"
            await state.vault.aclose()

    asyncio.run(scenario())
