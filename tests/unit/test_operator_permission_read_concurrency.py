"""Durable permission reads must preserve liveness and current authority."""

from __future__ import annotations

import asyncio
import concurrent.futures
import socket
import threading
import time
from pathlib import Path
from urllib.request import Request, urlopen

import httpx
import pytest
import test_operator_product_api as helpers
import uvicorn

from k5vision.domain.users import UserCreate, UserPatch, UserRole
from k5vision.operator_role_policy import PERMISSIONS

product = helpers.product


async def _entered(predicate):
    deadline = time.monotonic() + 2
    while not predicate() and time.monotonic() < deadline:
        await asyncio.sleep(0.005)
    assert predicate(), "Controlled permission read did not start"


@pytest.mark.parametrize("product", [{"roles": True}], indirect=True)
def test_slow_history_permission_does_not_stall_unrelated_http_status(product, monkeypatch):
    camera = product.enroll()
    policy = product.app.state.operator_roles
    original = policy._read
    entered, release = threading.Event(), threading.Event()
    first = threading.Lock()
    observed = {}

    def blocked_read():
        if first.acquire(blocking=False):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                observed["event_loop_thread"] = False
            else:
                observed["event_loop_thread"] = True
            entered.set()
            assert release.wait(5)
        return original()

    monkeypatch.setattr(policy, "_read", blocked_read)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            product.app, loop="asyncio", lifespan="off", access_log=False, log_level="critical"
        )
    )
    owner = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    owner.start()
    deadline = time.monotonic() + 3
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.005)
    assert server.started

    def get(path, timeout):
        request = Request(f"http://127.0.0.1:{port}" + path, headers=product.headers)
        with urlopen(request, timeout=timeout) as response:
            response.read(65536)
            return response.status

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as requests:
            history = requests.submit(get, f"/api/v1/operator/cameras/{camera}/history", 3)
            try:
                assert entered.wait(2)
                # This is a separate real HTTP request, without a role read.
                assert get("/api/v1/operator/status", 0.4) == 200
                assert observed["event_loop_thread"] is False
                assert not product.service.provider.history_queries
            finally:
                release.set()
            assert history.result(timeout=3) == 200
    finally:
        release.set()
        server.should_exit = True
        owner.join(3)
        listener.close()
        assert not owner.is_alive()


@pytest.mark.parametrize("product", [{"roles": True}], indirect=True)
def test_cancelled_permission_reads_retain_four_slots_without_blocking_status(product, monkeypatch):
    camera = product.enroll()
    policy = product.app.state.operator_roles
    original = policy.permission_snapshot
    release = threading.Event()
    lock = threading.Lock()
    observed = {"entered": 0}

    def blocked_snapshot(account):
        with lock:
            observed["entered"] += 1
        assert release.wait(5)
        return original(account)

    monkeypatch.setattr(policy, "permission_snapshot", blocked_snapshot)

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=product.app), base_url="http://127.0.0.1:8080"
        ) as client:
            route = f"/api/v1/operator/cameras/{camera}/history"
            reads = [
                asyncio.create_task(client.get(route, headers=product.headers)) for _ in range(4)
            ]
            queued = None
            try:
                await _entered(lambda: observed["entered"] == 4)
                queued = asyncio.create_task(client.get(route, headers=product.headers))
                await asyncio.sleep(0.025)
                for pending in reads:
                    pending.cancel()
                await asyncio.sleep(0)
                for pending in reads:
                    pending.cancel()
                await asyncio.sleep(0.025)
                assert observed["entered"] == 4
                assert not queued.done()
                status = await asyncio.wait_for(
                    client.get("/api/v1/operator/status", headers=product.headers), 0.5
                )
                assert status.status_code == 200
                release.set()
                results = await asyncio.gather(*reads, return_exceptions=True)
                assert all(isinstance(value, asyncio.CancelledError) for value in results)
                assert (await asyncio.wait_for(queued, 2)).status_code == 200
            finally:
                release.set()
                for pending in [*reads, *(() if queued is None else (queued,))]:
                    if not pending.done():
                        pending.cancel()
                await asyncio.gather(
                    *reads, *(() if queued is None else (queued,)), return_exceptions=True
                )

    asyncio.run(exercise())


@pytest.mark.parametrize("product", [{"roles": True}], indirect=True)
@pytest.mark.parametrize(
    "change",
    ["logout", "user_revoke", "relogin", "disable", "base_role", "role", "close", "failed_write"],
)
def test_authority_change_while_permission_read_awaits_cannot_admit_history(
    product, monkeypatch, change
):
    camera = product.enroll()
    backup = product.users.create(
        UserCreate(
            username="permission-backup", display_name="Backup", role=UserRole.ADMINISTRATOR
        ),
        actor="test",
    )
    helpers._initialized_session(product, backup)
    policy = product.app.state.operator_roles
    role_value = {
        "name": "Current permission fixture",
        "description": "",
        "base_role": "administrator",
        "permissions": sorted(PERMISSIONS),
    }
    role = policy.upsert(product.users, role_value)
    policy.assign(product.users, product.admin.id, role["id"])
    original = policy.permission_snapshot
    entered, release = threading.Event(), threading.Event()

    def held_snapshot(account):
        snapshot = original(account)
        entered.set()
        assert release.wait(5)
        return snapshot

    monkeypatch.setattr(policy, "permission_snapshot", held_snapshot)

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=product.app), base_url="http://127.0.0.1:8080"
        ) as client:
            pending = asyncio.create_task(
                client.get(f"/api/v1/operator/cameras/{camera}/history", headers=product.headers)
            )
            try:
                await _entered(entered.is_set)
                if change == "logout":
                    await product.sessions.revoke(product.token)
                elif change == "user_revoke":
                    await product.sessions.revoke_user(product.admin.id)
                elif change == "relogin":
                    await product.sessions.issue(product.admin)
                elif change == "disable":
                    product.users.update(product.admin.id, UserPatch(enabled=False), actor="test")
                elif change == "base_role":
                    product.users.update(
                        product.admin.id, UserPatch(role=UserRole.VIEWER), actor="test"
                    )
                elif change == "role":
                    policy.upsert(
                        product.users,
                        {**role_value, "permissions": sorted(PERMISSIONS - {"cameras.playback"})},
                        role_id=role["id"],
                    )
                elif change == "close":
                    policy.close()
                else:
                    import k5vision.operator_role_policy as module

                    original_open = module.os.open

                    def refuse_write(path, *args, **kwargs):
                        if Path(path).name.startswith(".roles-"):
                            raise OSError("Generated failed policy write")
                        return original_open(path, *args, **kwargs)

                    monkeypatch.setattr(module.os, "open", refuse_write)
                    with pytest.raises(OSError, match="Generated failed policy write"):
                        policy._write(policy._read())
                release.set()
                response = await asyncio.wait_for(pending, 2)
                assert response.status_code in {401, 403, 503}
                assert not product.service.provider.history_queries
            finally:
                release.set()
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    asyncio.run(exercise())


@pytest.mark.parametrize("product", [{"roles": True}], indirect=True)
@pytest.mark.parametrize("tamper", ["malformed", "replacement", "unsafe_mode"])
def test_new_permission_request_does_not_reuse_previous_positive_policy(product, tamper):
    camera = product.enroll()
    route = f"/api/v1/operator/cameras/{camera}/history"
    assert product.client.get(route, headers=product.headers).status_code == 200
    policy = product.app.state.operator_roles
    if tamper == "malformed":
        policy._path.write_bytes(b"{}")
    elif tamper == "replacement":
        replacement = policy._path.with_name("replacement-private-role.json")
        replacement.write_bytes(policy._path.read_bytes())
        replacement.chmod(0o600)
        replacement.replace(policy._path)
    else:
        import os

        if os.name != "posix":
            pytest.skip("POSIX mode refusal; native Windows ACL admission has separate tests")
        policy._path.chmod(0o644)
    assert product.client.get(route, headers=product.headers).status_code == 503


@pytest.mark.parametrize("product", [{"roles": True}], indirect=True)
@pytest.mark.parametrize(
    "change", ["permission", "disable", "base_role", "password", "relogin", "expiry"]
)
def test_final_session_await_cannot_revive_an_earlier_permission_snapshot(
    product, monkeypatch, change
):
    camera = product.enroll()
    backup = product.users.create(
        UserCreate(
            username="final-check-backup", display_name="Backup", role=UserRole.ADMINISTRATOR
        ),
        actor="test",
    )
    helpers._initialized_session(product, backup)
    policy = product.app.state.operator_roles
    role_value = {
        "name": "Final permission fixture",
        "description": "",
        "base_role": "administrator",
        "permissions": sorted(PERMISSIONS),
    }
    role = policy.upsert(product.users, role_value)
    policy.assign(product.users, product.admin.id, role["id"])
    # Model the actual installed credential-bound session resolver, rather
    # than the intentionally lightweight default API fixture's resolver.
    product.sessions._resolve_account = product.users.resolve_session
    account, version = product.users.active_session_snapshot(product.admin.id)
    entered, release = asyncio.Event(), asyncio.Event()
    original = product.sessions.resolve
    calls = 0

    async def held_final(token, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            entered.set()
            await release.wait()
        return await original(token, **kwargs)

    monkeypatch.setattr(product.sessions, "resolve", held_final)

    async def exercise():
        token, _ = await product.sessions.issue(account, credential_version=version)
        headers = {"Authorization": "Bearer " + token}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=product.app), base_url="http://127.0.0.1:8080"
        ) as client:
            pending = asyncio.create_task(
                client.get(f"/api/v1/operator/cameras/{camera}/history", headers=headers)
            )
            try:
                await _entered(entered.is_set)
                if change == "permission":
                    policy.upsert(
                        product.users,
                        {**role_value, "permissions": sorted(PERMISSIONS - {"cameras.playback"})},
                        role_id=role["id"],
                    )
                elif change == "disable":
                    product.users.update(product.admin.id, UserPatch(enabled=False), actor="test")
                elif change == "base_role":
                    product.users.update(
                        product.admin.id, UserPatch(role=UserRole.VIEWER), actor="test"
                    )
                elif change == "password":
                    product.users.reset_bootstrap(product.admin.id, actor="test")
                elif change == "relogin":
                    await product.sessions.issue(account, credential_version=version)
                else:
                    import k5vision.auth_sessions as sessions_module

                    previous = sessions_module.monotonic
                    monkeypatch.setattr(
                        sessions_module,
                        "monotonic",
                        lambda: previous() + product.sessions.ttl_seconds + 1,
                    )
                release.set()
                response = await asyncio.wait_for(pending, 2)
                assert response.status_code in {401, 403, 503}
                assert not product.service.provider.history_queries
            finally:
                release.set()
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    asyncio.run(exercise())
