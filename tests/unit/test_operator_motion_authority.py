"""Real ProductAPI authority around a controlled motion capture/file boundary.

The camera decoder and gate are injected here. UserRegistry, role policy,
sessions, ProductAPI authentication and the atomic policy writer are real.
"""

from __future__ import annotations

import asyncio
import copy
import threading
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
import test_operator_product_api as helpers

import k5vision.auth_sessions as session_module
from k5vision.domain.users import UserCreate, UserRole
from k5vision.operator_recording_policy import RecordingPolicyStore

product = helpers.product


class MotionFacade:
    """Only the native motion boundary; actual auth is never stubbed."""

    def __init__(self, store):
        self.store = store
        self.generation = 1
        self.preview_calls = 0
        self.before_preview = None
        self.stage_file = False
        self.entered = threading.Event()
        self.release = threading.Event()
        self.jpeg = b"\xff\xd8\xffgenerated-frame-not-a-real-camera\xff\xd9"

    def reference(self, source):
        return {
            "width": 640,
            "height": 360,
            "rotation": 0,
            "source_revision": self.store.source_revision(source),
            "generation": self.generation,
        }

    async def get(self, source):
        value = await asyncio.to_thread(self.store.get, source)
        return {
            **value,
            "status": "armed" if value["mode"] == "motion" else "continuous",
            "observed_reference": self.reference(source),
        }

    async def preview(self, source):
        self.preview_calls += 1
        if self.before_preview is not None:
            await self.before_preview()
        reference = self.reference(source)
        return {
            "jpeg": self.jpeg,
            **{k: v for k, v in reference.items() if k != "generation"},
            "reference_generation": self.generation,
        }

    async def apply(
        self,
        source,
        expected_revision,
        value,
        *,
        before_commit,
        reference_generation=None,
    ):
        fence = await before_commit()

        def final_write():
            if self.stage_file:
                self.entered.set()
                assert self.release.wait(5), "Controlled file boundary was not released"
            if reference_generation is not None:
                assert reference_generation == self.generation
            fence()

        await asyncio.to_thread(
            self.store.save,
            source,
            expected_revision,
            value,
            before_write=final_write,
        )
        return await self.get(source)


@pytest.fixture
def motion(product):
    camera_id = product.enroll()
    source = product.cameras.get(UUID(camera_id))
    store = RecordingPolicyStore(product.cameras._root, product.service.site_id)
    facade = MotionFacade(store)
    product.service.get_motion_policy = facade.get
    product.service.motion_preview = facade.preview
    product.service.current_motion_reference = facade.reference
    product.service.apply_motion_policy = facade.apply
    backup = product.users.create(
        UserCreate(
            username="motion-authority-backup",
            display_name="Backup",
            role=UserRole.ADMINISTRATOR,
        ),
        actor="test",
    )
    helpers._initialized_session(product, backup)
    role_value = {
        "name": "Motion recorder authority",
        "description": "",
        "base_role": "administrator",
        "permissions": ["recording.manage", "cameras.view_live"],
    }
    roles = product.app.state.operator_roles
    role = roles.upsert(product.users, role_value)
    roles.assign(product.users, product.admin.id, role["id"])
    value = SimpleNamespace(
        product=product,
        facade=facade,
        store=store,
        source=source,
        prefix=f"/api/v1/operator/cameras/{camera_id}",
        roles=roles,
        role=role,
        role_value=role_value,
    )
    try:
        yield value
    finally:
        facade.release.set()
        store.close()


def set_permissions(motion, permissions):
    motion.roles.upsert(
        motion.product.users,
        {**motion.role_value, "permissions": sorted(permissions)},
        role_id=motion.role["id"],
    )


def policy_and_body(motion, *, zones=False):
    response = motion.product.client.get(
        motion.prefix + "/recording-policy", headers=motion.product.headers
    )
    assert response.status_code == 200, response.text
    policy = response.json()
    body = {
        "expected_revision": policy["revision"],
        **{
            k: copy.deepcopy(policy[k])
            for k in (
                "mode",
                "post_capture_seconds",
                "sensitivity",
                "minimum_area_percent",
                "zones",
            )
        },
        "mode": "motion",
    }
    if zones:
        response = motion.product.client.post(
            motion.prefix + "/motion-preview", headers=motion.product.headers
        )
        assert response.status_code == 200, response.text
        frame = response.json()
        body.update(
            preview_id=frame["preview_id"],
            zones={
                "reference": {
                    k: frame[k] for k in ("width", "height", "rotation", "source_revision")
                },
                "include": [
                    {
                        "id": str(uuid4()),
                        "name": "Entrance",
                        "points": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]],
                    }
                ],
                "exclude": [],
            },
        )
    return body


async def entered(predicate):
    deadline = asyncio.get_running_loop().time() + 2
    while not predicate() and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.002)
    assert predicate(), "Controlled owned boundary was not reached"


@pytest.mark.parametrize("product", [{"roles": True}], indirect=True)
def test_recording_only_administrator_can_read_and_save_mode_but_cannot_capture_pixels(
    motion,
):
    set_permissions(motion, {"recording.manage"})
    body = policy_and_body(motion)
    saved = motion.product.client.put(
        motion.prefix + "/recording-policy", headers=motion.product.headers, json=body
    )
    assert saved.status_code == 200 and saved.json()["revision"] == 1
    denied = motion.product.client.post(
        motion.prefix + "/motion-preview", headers=motion.product.headers
    )
    assert denied.status_code == 403 and motion.facade.preview_calls == 0


@pytest.mark.parametrize("product", [{"roles": True}], indirect=True)
def test_live_only_administrator_cannot_read_or_mutate_recorder_policy(motion):
    set_permissions(motion, {"cameras.view_live"})
    original = motion.store.path.read_bytes()
    for method, suffix in (("GET", "/recording-policy"), ("POST", "/motion-preview")):
        response = motion.product.client.request(
            method, motion.prefix + suffix, headers=motion.product.headers
        )
        assert response.status_code == 403
    assert motion.facade.preview_calls == 0 and motion.store.path.read_bytes() == original


@pytest.mark.parametrize("product", [{"roles": True}], indirect=True)
@pytest.mark.parametrize("change", ["reissue", "live_permission"])
def test_production_authority_is_rechecked_after_native_preview_capture(motion, change):
    async def exercise():
        reached, release = asyncio.Event(), asyncio.Event()

        async def held():
            reached.set()
            await release.wait()

        motion.facade.before_preview = held
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=motion.product.app),
            base_url="http://127.0.0.1:8080",
        ) as client:
            request = asyncio.create_task(
                client.post(motion.prefix + "/motion-preview", headers=motion.product.headers)
            )
            try:
                await asyncio.wait_for(reached.wait(), 2)
                if change == "reissue":
                    await motion.product.sessions.issue(motion.product.admin)
                else:
                    set_permissions(motion, {"recording.manage"})
                release.set()
                response = await asyncio.wait_for(request, 2)
                assert response.status_code == (401 if change == "reissue" else 403)
                assert motion.facade.jpeg not in response.content
                vault = motion.product.app.state.operator_motion_preview_vault
                assert all(lease.jpeg is None for lease in vault._leases.values())
            finally:
                release.set()
                if not request.done():
                    request.cancel()
                await asyncio.gather(request, return_exceptions=True)

    asyncio.run(exercise())


@pytest.mark.parametrize("product", [{"roles": True}], indirect=True)
@pytest.mark.parametrize(
    "change", ["reissue", "logout", "expiry", "live_permission", "recording_permission"]
)
def test_production_final_file_fence_refuses_changed_authority_without_commit(
    motion, monkeypatch, change
):
    body = policy_and_body(motion, zones=True)
    original = motion.store.path.read_bytes()
    motion.facade.stage_file = True

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=motion.product.app),
            base_url="http://127.0.0.1:8080",
        ) as client:
            request = asyncio.create_task(
                client.put(
                    motion.prefix + "/recording-policy",
                    headers=motion.product.headers,
                    json=body,
                )
            )
            try:
                await entered(motion.facade.entered.is_set)
                if change == "reissue":
                    await motion.product.sessions.issue(motion.product.admin)
                elif change == "logout":
                    await motion.product.sessions.revoke(motion.product.token)
                elif change == "expiry":
                    now = session_module.monotonic()
                    monkeypatch.setattr(
                        session_module,
                        "monotonic",
                        lambda: now + motion.product.sessions.ttl_seconds + 1,
                    )
                else:
                    removed = (
                        "cameras.view_live" if change == "live_permission" else "recording.manage"
                    )
                    set_permissions(motion, {"recording.manage", "cameras.view_live"} - {removed})
                motion.facade.release.set()
                response = await asyncio.wait_for(request, 2)
                expected = (
                    403 if change == "logout" else 503 if change.endswith("permission") else 401
                )
                assert response.status_code == expected, response.text
                assert motion.store.path.read_bytes() == original
                assert not list(motion.store.root.glob(".k5-motion-*.tmp"))
            finally:
                motion.facade.release.set()
                if not request.done():
                    request.cancel()
                await asyncio.gather(request, return_exceptions=True)

    asyncio.run(exercise())


@pytest.mark.parametrize("product", [{"roles": True}], indirect=True)
def test_cancelled_production_save_drains_staged_file_and_preserves_policy(motion):
    body = policy_and_body(motion, zones=True)
    original = motion.store.path.read_bytes()
    motion.facade.stage_file = True

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=motion.product.app),
            base_url="http://127.0.0.1:8080",
        ) as client:
            request = asyncio.create_task(
                client.put(
                    motion.prefix + "/recording-policy",
                    headers=motion.product.headers,
                    json=body,
                )
            )
            try:
                await entered(motion.facade.entered.is_set)
                request.cancel()
                await asyncio.sleep(0.02)
                assert not request.done(), "Cancellation abandoned the owned staged write"
                motion.facade.release.set()
                with pytest.raises(asyncio.CancelledError):
                    await request
                assert motion.store.path.read_bytes() == original
                assert not list(motion.store.root.glob(".k5-motion-*.tmp"))
                assert not motion.product.app.state.operator_motion_preview_vault._mutations
            finally:
                motion.facade.release.set()
                if not request.done():
                    request.cancel()
                await asyncio.gather(request, return_exceptions=True)

    asyncio.run(exercise())
