from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import test_operator_recording_policy as store_helpers
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from k5vision.domain.users import UserAccount, UserRole
from k5vision.operator_recording_policy_api import install_operator_recording_policy_api

policy = store_helpers.policy


class _Service:
    def __init__(self, store):
        self.store = store
        self.generation = 1
        self.width, self.height = 1920, 1080
        self.preview_calls = 0
        self.apply_calls = []
        self.before_preview = None
        self.before_save = None
        self.after_authorization = None
        self.preview_bytes = b"\xff\xd8\xfftrusted-camera-jpeg\xff\xd9"

    def reference(self, source):
        return {
            "width": self.width,
            "height": self.height,
            "rotation": 0,
            "source_revision": self.store.source_revision(source),
            "generation": self.generation,
        }

    current_motion_reference = reference

    async def get_motion_policy(self, source):
        result = await asyncio.to_thread(self.store.get, source)
        return {
            **result,
            "status": "armed" if result["mode"] == "motion" else "continuous",
            "observed_reference": self.reference(source),
        }

    async def motion_preview(self, source):
        self.preview_calls += 1
        if self.before_preview is not None:
            await self.before_preview()
        return {
            "jpeg": self.preview_bytes,
            "capture_id": "private-capture-id",
            **{k: v for k, v in self.reference(source).items() if k != "generation"},
            "reference_generation": self.generation,
        }

    async def apply_motion_policy(
        self, source, expected_revision, value, *, before_commit, reference_generation=None
    ):
        self.apply_calls.append(reference_generation)
        if self.before_save is not None:
            await self.before_save()
        guard = await before_commit()
        if self.after_authorization is not None:
            await self.after_authorization()
        if reference_generation is not None and reference_generation != self.generation:
            raise RuntimeError("motion_source_changed")
        await asyncio.to_thread(
            self.store.save, source, expected_revision, value, before_write=guard
        )
        return await self.get_motion_policy(source)


@pytest.fixture
def api(policy):
    store, source, _ = policy
    app = FastAPI()
    account = UserAccount(
        id=uuid4(),
        username="motion-admin",
        display_name="Motion admin",
        role=UserRole.ADMINISTRATOR,
    )
    state = SimpleNamespace(
        recording=True,
        live=True,
        tokens={"valid", "other"},
        source=source,
        auth_calls=[],
        selection_threads=[],
    )
    service = _Service(store)

    async def authenticate(token):
        state.auth_calls.append((token, "recording"))
        if token not in state.tokens:
            raise HTTPException(401, "Unauthorized")
        if not state.recording:
            raise HTTPException(403, "Recording permission denied")
        return account, token

    async def preview_auth(token):
        result = await authenticate(token)
        state.auth_calls.append((token, "live"))
        if not state.live:
            raise HTTPException(403, "Live permission denied")
        return result

    async def require_admin(request: Request):
        values = request.headers.getlist("authorization")
        if len(values) != 1 or not values[0].startswith("Bearer "):
            raise HTTPException(401, "Unauthorized")
        return await authenticate(values[0][7:])

    async def capture_fence(token, *, live=False):
        await (preview_auth(token) if live else authenticate(token))

        def fence():
            if token not in state.tokens:
                raise HTTPException(401, "Unauthorized")
            if not state.recording or (live and not state.live):
                raise HTTPException(403, "Permission changed")

        return fence

    def selection(_account, camera_id):
        import threading

        state.selection_threads.append(threading.get_ident())
        if camera_id != source.binding.device_id:
            raise HTTPException(404, "Camera not found")
        return state.source

    vault = install_operator_recording_policy_api(
        app,
        require_admin=require_admin,
        authenticate=authenticate,
        authenticate_preview=preview_auth,
        selection=selection,
        mutations=asyncio.Lock(),
        service=service,
        capture_authority_fence=capture_fence,
    )

    prefix = f"/api/v1/operator/cameras/{source.binding.device_id}"
    with TestClient(app) as client:
        yield SimpleNamespace(
            app=app,
            client=client,
            prefix=prefix,
            headers={"Authorization": "Bearer valid"},
            state=state,
            service=service,
            store=store,
            source=source,
            vault=vault,
        )


def _get(api):
    response = api.client.get(api.prefix + "/recording-policy", headers=api.headers)
    assert response.status_code == 200, response.text
    return response.json()


def _payload(value, **updates):
    fields = ("mode", "post_capture_seconds", "sensitivity", "minimum_area_percent", "zones")
    return {"expected_revision": value["revision"], **{k: value[k] for k in fields}, **updates}


def _preview(api):
    response = api.client.post(api.prefix + "/motion-preview", headers=api.headers)
    assert response.status_code == 200, response.text
    return response.json()


def _zones(preview):
    return {
        "reference": {k: preview[k] for k in ("width", "height", "rotation", "source_revision")},
        "include": [store_helpers._polygon()],
        "exclude": [],
    }


def test_read_and_mode_only_changes_require_no_live_pixels(api):
    api.state.live = False
    current = _get(api)
    result = api.client.put(
        api.prefix + "/recording-policy", headers=api.headers, json=_payload(current, mode="motion")
    )
    assert result.status_code == 200, result.text
    assert result.json()["mode"] == "motion" and result.json()["revision"] == 1
    assert api.service.preview_calls == 0 and api.service.apply_calls == [None]
    assert "private-password" not in result.text and "rtsp://" not in result.text


def test_preview_is_immutable_owned_no_store_and_never_capability_only(api):
    preview = _preview(api)
    assert preview["width"] == 1920 and preview["height"] == 1080
    assert preview["rotation"] == 0 and 118 <= preview["expires_in_seconds"] <= 120
    result = api.client.get(preview["preview_url"], headers=api.headers)
    assert result.status_code == 200 and result.content == api.service.preview_bytes
    assert result.headers["cache-control"] == "no-store"
    assert result.headers["content-type"] == "image/jpeg"
    assert api.service.preview_calls == 1
    assert api.client.get(preview["preview_url"]).status_code == 401
    assert (
        api.client.get(
            preview["preview_url"], headers={"Authorization": "Bearer other"}
        ).status_code
        == 410
    )


def test_authored_zones_use_exact_trusted_reference_and_generation(api):
    value = _get(api)
    preview = _preview(api)
    zones = _zones(preview)
    response = api.client.put(
        api.prefix + "/recording-policy",
        headers=api.headers,
        json=_payload(value, mode="motion", zones=zones, preview_id=preview["preview_id"]),
    )
    assert response.status_code == 200, response.text
    assert response.json()["zones"] == zones
    assert api.service.apply_calls == [1]
    assert api.client.get(preview["preview_url"], headers=api.headers).status_code == 410


@pytest.mark.parametrize(
    "field,value", [("width", 1280), ("rotation", False), ("source_revision", "f" * 64)]
)
def test_submitted_browser_geometry_cannot_replace_observed_reference(api, field, value):
    current = _get(api)
    preview = _preview(api)
    zones = _zones(preview)
    zones["reference"][field] = value
    response = api.client.put(
        api.prefix + "/recording-policy",
        headers=api.headers,
        json=_payload(current, zones=zones, preview_id=preview["preview_id"]),
    )
    assert response.status_code in {409, 422}
    assert api.store.get(api.source)["revision"] == 0


def test_zone_save_requires_preview_and_current_live_permission(api):
    current = _get(api)
    preview = _preview(api)
    missing = api.client.put(
        api.prefix + "/recording-policy",
        headers=api.headers,
        json=_payload(current, zones=_zones(preview)),
    )
    assert missing.status_code == 410
    api.state.live = False
    denied = api.client.put(
        api.prefix + "/recording-policy",
        headers=api.headers,
        json=_payload(current, zones=_zones(preview), preview_id=preview["preview_id"]),
    )
    assert denied.status_code == 403 and not api.service.apply_calls


def test_geometry_change_retires_frame_and_prevents_zone_write(api):
    current = _get(api)
    preview = _preview(api)
    api.service.generation += 1
    api.service.width = 1280
    assert api.client.get(preview["preview_url"], headers=api.headers).status_code == 409
    result = api.client.put(
        api.prefix + "/recording-policy",
        headers=api.headers,
        json=_payload(current, zones=_zones(preview), preview_id=preview["preview_id"]),
    )
    assert result.status_code == 409 and not api.service.apply_calls


def test_final_preview_auth_await_cannot_deliver_old_geometry(api):
    preview = _preview(api)
    calls = 0
    previous = api.service.get_motion_policy

    async def changing(source):
        nonlocal calls
        result = await previous(source)
        calls += 1
        # The dictionary has already captured the old reference. A subsequent
        # authorization await must not make that stale dictionary sufficient.
        api.service.width = 1280
        api.service.generation += 1
        return result

    api.service.get_motion_policy = changing
    response = api.client.get(preview["preview_url"], headers=api.headers)
    assert calls == 1 and response.status_code == 409


def test_source_change_rejects_even_same_camera_identifier(api):
    preview = _preview(api)
    api.state.source = replace(api.source, source_uri=api.source.source_uri + "-new")
    result = api.client.get(preview["preview_url"], headers=api.headers)
    assert result.status_code == 409


def test_label_and_master_pause_leave_source_preview_basis_intact(api):
    preview = _preview(api)
    api.state.source = replace(
        api.source,
        recording_enabled=False,
        device=api.source.device.model_copy(update={"name": "Renamed"}),
    )
    assert api.client.get(preview["preview_url"], headers=api.headers).status_code == 200


def test_policy_write_retires_other_sessions_existing_preview(api):
    preview = _preview(api)
    current = _get(api)
    response = api.client.put(
        api.prefix + "/recording-policy",
        headers={"Authorization": "Bearer other"},
        json=_payload(current, post_capture_seconds=12),
    )
    assert response.status_code == 200
    assert api.client.get(preview["preview_url"], headers=api.headers).status_code == 410


def test_policy_revision_conflict_prevents_native_apply(api):
    current = _get(api)
    api.store.save(
        api.source,
        0,
        {
            k: current[k]
            for k in (
                "mode",
                "post_capture_seconds",
                "sensitivity",
                "minimum_area_percent",
                "zones",
            )
        },
    )
    result = api.client.put(
        api.prefix + "/recording-policy", headers=api.headers, json=_payload(current, mode="motion")
    )
    assert result.status_code == 409 and not api.service.apply_calls


@pytest.mark.parametrize(
    "value",
    [b"not-a-jpeg", b"\xff\xd8\xffmissing-end", b"x" * (1024**2 + 1)],
    ids=["not-jpeg", "truncated-jpeg", "oversized-jpeg"],
)
def test_invalid_or_oversized_capture_is_not_exposed(api, value):
    api.service.preview_bytes = value
    response = api.client.post(api.prefix + "/motion-preview", headers=api.headers)
    assert response.status_code == 503
    assert not api.vault._leases


def test_preview_capture_rechecks_live_authority_after_native_await(api):
    async def revoke():
        api.state.live = False

    api.service.before_preview = revoke
    response = api.client.post(api.prefix + "/motion-preview", headers=api.headers)
    assert response.status_code == 403 and not api.vault._leases


def test_final_before_commit_rechecks_current_recording_permission(api):
    current = _get(api)

    async def revoke():
        api.state.recording = False

    api.service.before_save = revoke
    result = api.client.put(
        api.prefix + "/recording-policy", headers=api.headers, json=_payload(current, mode="motion")
    )
    assert result.status_code == 403 and api.store.get(api.source)["revision"] == 0


def test_geometry_epoch_is_rechecked_by_service_at_commit(api):
    current = _get(api)
    preview = _preview(api)

    async def change():
        api.service.generation += 1

    api.service.before_save = change
    result = api.client.put(
        api.prefix + "/recording-policy",
        headers=api.headers,
        json=_payload(current, zones=_zones(preview), preview_id=preview["preview_id"]),
    )
    assert result.status_code == 409 and api.store.get(api.source)["revision"] == 0


@pytest.mark.parametrize(
    "change,expected", [("logout", 401), ("recording", 403), ("vault-revocation", 403)]
)
def test_final_file_guard_rejects_revocation_after_async_authorization(api, change, expected):
    current = _get(api)

    async def revoke():
        if change == "logout":
            api.state.tokens.clear()
        elif change == "recording":
            api.state.recording = False
        else:
            operation = next(iter(api.vault._mutations))
            api.vault.clear_user(operation.owner[0])

    api.service.after_authorization = revoke
    result = api.client.put(
        api.prefix + "/recording-policy", headers=api.headers, json=_payload(current, mode="motion")
    )
    assert result.status_code == expected
    assert api.store.get(api.source)["revision"] == 0
    assert not api.vault._mutations


def test_logout_revocation_discards_pixels_even_when_other_token_remains_valid(api):
    preview = _preview(api)
    lease = next(iter(api.vault._leases.values()))
    api.vault.clear_user(lease.owner[0])
    assert lease.jpeg is None
    assert api.client.get(preview["preview_url"], headers=api.headers).status_code == 410


@pytest.mark.parametrize(
    "change",
    [
        {"unknown": "PRIVATE"},
        {"sensitivity": True},
        {"pre_capture_seconds": 10},
        {"expected_revision": True},
    ],
)
def test_validation_errors_do_not_echo_private_input(api, change):
    body = _payload(_get(api))
    body.update(change)
    response = api.client.put(api.prefix + "/recording-policy", headers=api.headers, json=body)
    assert response.status_code == 422 and response.json() == {"detail": "motion_invalid_request"}
    assert "PRIVATE" not in response.text


def test_preview_expiry_uses_monotonic_time_and_releases_pixels(api, monkeypatch):
    import k5vision.operator_recording_policy_api as module

    preview = _preview(api)
    lease = next(iter(api.vault._leases.values()))
    monkeypatch.setattr(module, "monotonic", lambda: lease.expires + 1)
    result = api.client.get(preview["preview_url"], headers=api.headers)
    assert result.status_code == 410 and lease.jpeg is None


def test_selection_reads_run_off_the_asgi_loop(api):
    import threading

    asgi_thread = None

    async def track():
        nonlocal asgi_thread
        asgi_thread = threading.get_ident()

    api.service.before_preview = track
    _preview(api)
    assert asgi_thread is not None
    assert all(thread != asgi_thread for thread in api.state.selection_threads)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_cancelled_mutation_drains_owned_work_without_committing(api):
    current = _get(api)
    entered, release = asyncio.Event(), asyncio.Event()

    async def block():
        entered.set()
        await release.wait()

    api.service.before_save = block
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api.app), base_url="http://testserver"
    ) as client:
        request = asyncio.create_task(
            client.put(
                api.prefix + "/recording-policy",
                headers=api.headers,
                json=_payload(current, mode="motion"),
            )
        )
        await asyncio.wait_for(entered.wait(), 2)
        request.cancel()
        await asyncio.sleep(0)
        assert not request.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(request, 2)
    assert api.store.get(api.source)["revision"] == 0
    assert not api.vault._tasks


@pytest.mark.anyio
async def test_cancellation_during_real_staged_file_write_is_fenced_before_replace(
    api, monkeypatch
):
    import os
    import threading

    current = _get(api)
    original = api.store.path.read_bytes()
    entered, release = threading.Event(), threading.Event()
    real_fsync = os.fsync

    def staged(descriptor):
        real_fsync(descriptor)
        entered.set()
        assert release.wait(3)

    monkeypatch.setattr(os, "fsync", staged)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api.app), base_url="http://testserver"
    ) as client:
        request = asyncio.create_task(
            client.put(
                api.prefix + "/recording-policy",
                headers=api.headers,
                json=_payload(current, mode="motion"),
            )
        )
        assert await asyncio.to_thread(entered.wait, 3)
        request.cancel()
        await asyncio.sleep(0)
        request.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(request, 3)
    assert api.store.path.read_bytes() == original
    assert not list(api.store.root.glob(".k5-motion-*.tmp"))
    assert not api.vault._tasks and not api.vault._mutations
