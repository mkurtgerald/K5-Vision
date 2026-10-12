"""Authenticated ASGI-only sample routes; all delivery/inference is mocked."""

import asyncio

import httpx
from fastapi import FastAPI
from test_operator_product_analytics import Runner, setup
from test_operator_product_api import product as _product_fixture

from k5vision.domain.users import UserCreate, UserPatch, UserRole
from k5vision.operator_product_api import install_operator_product_api

product = _product_fixture


def install(product, coordinator):
    app = FastAPI()
    for key in ("device_registry", "user_registry", "user_session_manager"):
        setattr(app.state, key, getattr(product.app.state, key))
    product.service.analytics_generation = 1

    def selected(source):
        if source not in product.cameras.list_sources() or product.service.worker_status != "ready":
            raise RuntimeError("private-source")
        return product.service.analytics_generation, "rtsp://private-worker-secret@127.0.0.1/live"

    product.service.analytics_selection = selected
    install_operator_product_api(
        app,
        cameras=product.cameras,
        service=product.service,
        public_port=8080,
        analytics=coordinator,
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8080"
    )


def test_admission_is_202_while_native_blocked_and_poll_is_account_bound(product):
    camera = product.enroll()

    async def check():
        gate = asyncio.Event()
        c, _, _ = setup(runner=Runner(gate))
        async with install(product, c) as client:
            path = f"/api/v1/operator/cameras/{camera}/analytics-samples"
            assert (await client.post(path)).status_code == 401
            admitted = await client.post(path, headers=product.headers)
            assert admitted.status_code == 202 and admitted.headers["cache-control"] == "no-store"
            ticket = admitted.json()["ticket"]
            assert "private" not in admitted.text
            assert (await client.post(path, headers=product.headers)).status_code == 409
            pending = await client.get(path + "/" + ticket, headers=product.headers)
            assert pending.json()["status"] == "pending"
            account, _, _ = product.users.create_with_bootstrap(
                UserCreate(username="reader", display_name="Reader", role=UserRole.VIEWER),
                actor="test",
            )
            token, _ = await product.sessions.issue(account)
            other = {"Authorization": "Bearer " + token}
            assert (await client.get(path + "/" + ticket, headers=other)).status_code == 404
            gate.set()
            await asyncio.shield(c._task)
            ready = await client.get(path + "/" + ticket, headers=product.headers)
            assert ready.status_code == 200
            assert ready.headers["content-type"] == "application/octet-stream"
            assert len(ready.content) == 64
            assert "private" not in ready.text and "rtsp:" not in ready.text
            product.service.analytics_generation += 1
            assert (
                await client.get(path + "/" + ticket, headers=product.headers)
            ).status_code == 404

    asyncio.run(check())


def test_route_rejects_source_pixels_query_and_wrong_origin(product):
    camera = product.enroll()

    async def check():
        c, _, _ = setup()
        async with install(product, c) as client:
            path = f"/api/v1/operator/cameras/{camera}/analytics-samples"
            for payload in ({"source_uri": "rtsp://secret"}, {"pixels": "anything"}, {}):
                response = await client.post(path, headers=product.headers, json=payload)
                assert response.status_code == 422 and "secret" not in response.text
            assert (
                await client.post(path + "?source=x", headers=product.headers)
            ).status_code == 422
            assert (
                await client.post(
                    path, headers={**product.headers, "Origin": "https://evil.example"}
                )
            ).status_code == 403
            assert c._task is None

    asyncio.run(check())


def test_revocation_disabled_account_removal_and_logout_suppress_pixels(product):
    camera = product.enroll()

    async def check():
        c, _, _ = setup()
        async with install(product, c) as client:
            path = f"/api/v1/operator/cameras/{camera}/analytics-samples"
            t = (await client.post(path, headers=product.headers)).json()["ticket"]
            await asyncio.shield(c._task)
            product.users.update(product.admin.id, UserPatch(enabled=False), actor="test")
            assert (await client.get(path + "/" + t, headers=product.headers)).status_code == 401
            product.users.update(product.admin.id, UserPatch(enabled=True), actor="test")
            # Existing product media-session deletion is the logout/expiry clearing path.
            await client.delete("/api/v1/operator/media-session", headers=product.headers)
            assert c._result is None
            assert (await client.get(path + "/" + t, headers=product.headers)).status_code == 404
            await product.sessions.revoke(product.token)
            assert (await client.get(path + "/" + t, headers=product.headers)).status_code == 401

    asyncio.run(check())


def test_camera_mutation_invalidates_pending_result_before_reconciliation(product):
    camera = product.enroll()

    async def check():
        gate = asyncio.Event()
        c, _, _ = setup(runner=Runner(gate))
        async with install(product, c) as client:
            path = f"/api/v1/operator/cameras/{camera}/analytics-samples"
            t = (await client.post(path, headers=product.headers)).json()["ticket"]
            assert (
                await client.delete(f"/api/v1/operator/cameras/{camera}", headers=product.headers)
            ).status_code == 204
            assert c.status() == "busy"
            gate.set()
            await asyncio.shield(c._task)
            assert c._result is None
            assert (await client.get(path + "/" + t, headers=product.headers)).status_code == 404

    asyncio.run(check())


def test_missing_prerequisites_are_truthful_without_starting_work(product):
    from k5vision.operator_product_analytics import AnalyzedSampleCoordinator

    camera = product.enroll()

    async def check():
        c = AnalyzedSampleCoordinator(None)
        async with install(product, c) as client:
            listing = await client.get("/api/v1/operator/cameras", headers=product.headers)
            assert listing.json()["cameras"][0]["analytics_status"] == "unavailable"
            r = await client.post(
                f"/api/v1/operator/cameras/{camera}/analytics-samples", headers=product.headers
            )
            assert r.status_code == 503 and c._task is None

    asyncio.run(check())


def test_current_account_is_revalidated_before_result_storage(product):
    camera = product.enroll()

    async def check():
        gate = asyncio.Event()
        c, _, p = setup(gate=gate)
        async with install(product, c) as client:
            path = f"/api/v1/operator/cameras/{camera}/analytics-samples"
            t = (await client.post(path, headers=product.headers)).json()["ticket"]
            while p.seen is None:
                await asyncio.sleep(0)
            product.users.update(product.admin.id, UserPatch(enabled=False), actor="test")
            gate.set()
            await asyncio.shield(c._task)
            assert p.closed and c._result is None
            assert (await client.get(path + "/" + t, headers=product.headers)).status_code == 401

    asyncio.run(check())


def test_ready_pixels_are_atomically_consumed_once_and_wrong_owners_cannot_consume(product):
    camera = product.enroll()

    async def check():
        c, _, _ = setup()
        async with install(product, c) as client:
            path = f"/api/v1/operator/cameras/{camera}/analytics-samples"
            t = (await client.post(path, headers=product.headers)).json()["ticket"]
            await asyncio.shield(c._task)
            retained = c._result["pixels"]
            account, _, _ = product.users.create_with_bootstrap(
                UserCreate(username="other-reader", display_name="Other", role=UserRole.VIEWER),
                actor="test",
            )
            token, _ = await product.sessions.issue(account)
            wrong = await client.get(path + "/" + t, headers={"Authorization": "Bearer " + token})
            assert wrong.status_code == 404 and c._result["pixels"] is retained
            assert (await client.get(path + "/" + t)).status_code == 401
            product.service.analytics_generation += 1
            stale = await client.get(path + "/" + t, headers=product.headers)
            assert stale.status_code == 404 and c._result["pixels"] is retained
            product.service.analytics_generation -= (
                1  # Restore only this mock for valid consumption.
            )
            responses = await asyncio.gather(
                *(client.get(path + "/" + t, headers=product.headers) for _ in range(4))
            )
            binary = [
                r for r in responses if r.headers["content-type"] == "application/octet-stream"
            ]
            assert len(binary) == 1 and binary[0].content == retained
            assert binary[0].headers["content-length"] == str(len(retained))
            assert binary[0].headers["x-k5-sample-ticket"] == t
            assert all(r.json()["status"] == "consumed" for r in responses if r not in binary)
            assert c._result is None
            assert (await client.get(path + "/" + t, headers=product.headers)).json()[
                "status"
            ] == "consumed"

    asyncio.run(check())
