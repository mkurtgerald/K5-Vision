"""Real operator HTTP remains live while recorded-event state I/O is occupied."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import UUID

import pytest
import test_operator_events_api as event_api

product = event_api.product
event_product = event_api.event_product


@pytest.mark.parametrize("blocked", ["sources", "settings", "record"])
@pytest.mark.parametrize("route", ["status", "history"])
def test_background_event_io_does_not_block_independent_operator_http(
    event_product, monkeypatch, blocked, route
):
    product, events = event_product
    camera_id = product.enroll()
    camera = UUID(camera_id)
    source = product.cameras.get(camera)
    events._cameras = SimpleNamespace(list_sources=lambda: (source,))
    events.configure(camera, enabled=True, confidence_threshold=0.5)
    entered, released = threading.Event(), threading.Event()
    owner = events._cameras if blocked == "sources" else events.store
    method = "list_sources" if blocked == "sources" else blocked
    original = getattr(owner, method)

    def held(*args, **kwargs):
        entered.set()
        assert released.wait(5), "Controlled event-state I/O was not released"
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, method, held)

    async def cycle():
        # Let portal startup return before the deliberately blocked callback.
        await asyncio.sleep(0.01)
        await events.cycle()

    pending = product.client.portal.start_task_soon(cycle)
    try:
        assert entered.wait(2), "Background event callback was not reached"
        path = (
            "/api/v1/operator/status"
            if route == "status"
            else f"/api/v1/operator/cameras/{camera_id}/history"
        )
        with ThreadPoolExecutor(max_workers=1) as executor:
            response = executor.submit(product.client.get, path, headers=product.headers)
            try:
                result = response.result(timeout=0.75)
                assert result.status_code == 200
                assert not pending.done()
            finally:
                released.set()
    finally:
        released.set()
        pending.result(timeout=5)


def _configured(event_product):
    product, events = event_product
    identifier = product.enroll()
    source = product.cameras.get(UUID(identifier))
    sources = [source]
    events._cameras = SimpleNamespace(list_sources=lambda: tuple(sources))
    events.configure(source.binding.device_id, enabled=True, confidence_threshold=0.5)
    return product, events, source, sources


@pytest.mark.parametrize("gate", ["settings", "record"])
@pytest.mark.parametrize("change", ["disable_reenable", "threshold", "remove", "pause", "replace"])
def test_new_durable_awaits_cannot_admit_stale_recorded_events(
    event_product, monkeypatch, gate, change
):
    from dataclasses import replace

    product, events, source, sources = _configured(event_product)
    entered, released = threading.Event(), threading.Event()
    original = getattr(events.store, gate)
    reads = 0

    def held(*args, **kwargs):
        nonlocal reads
        reads += 1
        # Settings gate is the post-inference durable recheck. Record gate
        # pauses before the real SQLite transaction and its admission callback.
        hold = gate == "record" or reads == 2
        result = original(*args, **kwargs) if gate == "settings" else None
        if hold:
            entered.set()
            assert released.wait(5), "Controlled event admission was not released"
        return result if gate == "settings" else original(*args, **kwargs)

    monkeypatch.setattr(events.store, gate, held)

    async def cycle():
        await asyncio.sleep(0.01)
        await events.cycle()

    pending = product.client.portal.start_task_soon(cycle)
    try:
        assert entered.wait(2)
        identifier = source.binding.device_id
        if change == "disable_reenable":
            events.configure(identifier, enabled=False, confidence_threshold=0.5)
            events.configure(identifier, enabled=True, confidence_threshold=0.5)
        elif change == "threshold":
            events.configure(identifier, enabled=True, confidence_threshold=0.95)
        elif change == "remove":
            sources.clear()
        elif change == "pause":
            sources[0] = replace(source, recording_enabled=False)
        else:
            sources[0] = replace(source, source_uri="rtsp://127.0.0.1/replaced-stream")
    finally:
        released.set()
        pending.result(timeout=5)
    assert events.store.summary(source.binding.device_id)["event_count"] == 0
    assert source.binding.device_id not in events._last_events


@pytest.mark.parametrize("blocked", ["sources", "settings", "record"])
def test_cancellation_retains_owned_state_capacity_and_drains_before_store_close(
    event_product, monkeypatch, blocked
):
    product, events, source, _sources = _configured(event_product)
    entered, released, closed = threading.Event(), threading.Event(), threading.Event()
    owner = events._cameras if blocked == "sources" else events.store
    method = "list_sources" if blocked == "sources" else blocked
    original, original_close = getattr(owner, method), events.store.close
    tasks = []

    def held(*args, **kwargs):
        entered.set()
        assert released.wait(5), "Controlled cancellation callback was not released"
        assert not closed.is_set(), "Store closed beneath its owned callback"
        return original(*args, **kwargs)

    def close():
        closed.set()
        return original_close()

    monkeypatch.setattr(owner, method, held)
    monkeypatch.setattr(events.store, "close", close)

    async def cycle():
        tasks.append(asyncio.current_task())
        await asyncio.sleep(0.01)
        await events.cycle()

    pending = product.client.portal.start_task_soon(cycle)
    closing = None
    try:
        assert entered.wait(2)
        product.client.portal.call(tasks[0].cancel)
        product.client.portal.call(tasks[0].cancel)
        closing = product.client.portal.start_task_soon(events.aclose)
        # This real request can only return if cancellation/close are awaiting
        # their worker rather than blocking or abandoning its admission.
        assert product.client.get("/openapi.json").status_code == 200
        assert not closing.done()
        assert not closed.is_set()
        assert events._state_pending is not None and not events._state_pending.done()
    finally:
        released.set()
        if closing is not None:
            closing.result(timeout=5)
    assert pending.cancelled()
    assert closed.is_set()
    assert events._state_pending is None


def test_configure_http_uses_owned_io_without_blocking_other_operator_requests(
    event_product, monkeypatch
):
    product, events = event_product
    identifier = product.enroll()
    entered, released = threading.Event(), threading.Event()
    original = events.store.configure
    thread_ids = []
    loop_thread = product.client.portal.call(threading.get_ident)

    def held(*args, **kwargs):
        thread_ids.append(threading.get_ident())
        entered.set()
        assert released.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(events.store, "configure", held)
    with ThreadPoolExecutor(max_workers=2) as executor:
        pending = executor.submit(
            product.client.patch,
            f"/api/v1/operator/cameras/{identifier}/analytics",
            headers=product.headers,
            json={"enabled": True, "confidence_threshold": 0.5},
        )
        try:
            assert entered.wait(2)
            status = executor.submit(
                product.client.get, "/api/v1/operator/status", headers=product.headers
            )
            assert status.result(timeout=0.75).status_code == 200
            assert not pending.done()
        finally:
            released.set()
        assert pending.result(timeout=5).status_code == 200
    assert thread_ids and all(value != loop_thread for value in thread_ids)


def test_concurrent_and_cancelled_close_share_one_drained_shutdown(event_product, monkeypatch):
    product, events = event_product
    entered, released = threading.Event(), threading.Event()
    original = events.store.close
    calls = []
    tasks = []

    def held_close():
        calls.append(threading.get_ident())
        entered.set()
        assert released.wait(5)
        original()

    monkeypatch.setattr(events.store, "close", held_close)

    async def close():
        tasks.append(asyncio.current_task())
        await events.aclose()

    first = product.client.portal.start_task_soon(close)
    second = None
    try:
        assert entered.wait(2)
        second = product.client.portal.start_task_soon(events.aclose)
        product.client.portal.call(tasks[0].cancel)
        product.client.portal.call(tasks[0].cancel)
        assert product.client.get("/openapi.json").status_code == 200
        assert not first.done() and not second.done()
        assert len(calls) == 1
    finally:
        released.set()
        if second is not None:
            second.result(timeout=5)
    assert first.cancelled()
    product.client.portal.call(events.aclose)
    assert len(calls) == 1
    assert events._state_closed


def test_failed_store_close_retains_worker_for_a_real_cleanup_retry(event_product, monkeypatch):
    product, events = event_product
    original = events.store.close
    calls = []

    def close():
        calls.append(True)
        if len(calls) == 1:
            raise RuntimeError("Synthetic store close failed")
        return original()

    monkeypatch.setattr(events.store, "close", close)
    with pytest.raises(RuntimeError, match="Synthetic store close failed"):
        product.client.portal.call(events.aclose)
    assert not events._state_closed
    product.client.portal.call(events.aclose)
    assert events._state_closed and len(calls) == 2


def test_warmup_hint_is_memory_only_and_requires_a_current_background_snapshot(
    event_product, monkeypatch
):
    product, events, source, _sources = _configured(event_product)
    assert events.defer_view_preparation()
    product.client.portal.call(events.cycle)
    assert events.store.summary(source.binding.device_id)["event_count"] == 1
    assert not events.defer_view_preparation()
    events.configure(source.binding.device_id, enabled=False, confidence_threshold=0.5)
    assert events.defer_view_preparation()
    product.client.portal.call(events.cycle)
    assert not events.defer_view_preparation()
    events.configure(source.binding.device_id, enabled=True, confidence_threshold=0.5)

    def forbidden(*args, **kwargs):
        pytest.fail("Memory-only warmup hint performed durable I/O")

    monkeypatch.setattr(events._cameras, "list_sources", forbidden)
    monkeypatch.setattr(events.store, "settings", forbidden)
    assert events.defer_view_preparation()


@pytest.mark.parametrize("held_at", ["selection", "configure"])
def test_revocation_during_new_configuration_awaits_retains_previous_settings(
    event_product, monkeypatch, held_at
):
    product, events = event_product
    camera = product.enroll()
    owner = product.cameras if held_at == "selection" else events.store
    method = "list_sources" if held_at == "selection" else "configure"
    original = getattr(owner, method)
    entered, released = threading.Event(), threading.Event()

    def held(*args, **kwargs):
        entered.set()
        assert released.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, method, held)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(
            product.client.patch,
            f"/api/v1/operator/cameras/{camera}/analytics",
            headers=product.headers,
            json={"enabled": True, "confidence_threshold": 0.7},
        )
        try:
            assert entered.wait(2)
            product.client.portal.call(product.sessions.revoke, product.token)
        finally:
            released.set()
        assert pending.result(timeout=5).status_code == 401
    monkeypatch.setattr(owner, method, original)
    assert events.store.settings(UUID(camera)) == {"enabled": False, "confidence_threshold": 0.65}


@pytest.mark.parametrize("cancel_at", ["write", "restore"])
def test_cancelled_configuration_drains_compensation_before_releasing_mutations(
    event_product, monkeypatch, cancel_at
):
    import httpx

    product, events = event_product
    camera = product.enroll()
    entered, released, mutation_released = threading.Event(), threading.Event(), threading.Event()
    original = events.store.configure
    calls, tasks, cancellations = [], [], []

    def configure(*args, **kwargs):
        calls.append(dict(kwargs))
        held = len(calls) == (1 if cancel_at == "write" else 2)
        if held:
            entered.set()
            assert released.wait(5)
        result = original(*args, **kwargs)
        if len(calls) == 1 and cancel_at == "restore":
            product.client.portal.call(product.sessions.revoke, product.token)
        return result

    monkeypatch.setattr(events.store, "configure", configure)

    async def patch():
        tasks.append(asyncio.current_task())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=product.app), base_url=str(product.client.base_url)
        ) as client:
            try:
                return await client.patch(
                    f"/api/v1/operator/cameras/{camera}/analytics",
                    headers=product.headers,
                    json={"enabled": True, "confidence_threshold": 0.7},
                )
            except asyncio.CancelledError as error:
                cancellations.append(error.args)
                raise

    async def mutation():
        async with events._mutations:
            mutation_released.set()

    pending = product.client.portal.start_task_soon(patch)
    observer = None
    try:
        assert entered.wait(2)
        product.client.portal.call(tasks[0].cancel, "preserved cancellation reason")
        product.client.portal.call(tasks[0].cancel, "second cancellation")
        observer = product.client.portal.start_task_soon(mutation)
        assert product.client.get("/openapi.json").status_code == 200
        assert not pending.done() and not mutation_released.is_set()
    finally:
        released.set()
        if observer is not None:
            observer.result(timeout=5)
    assert pending.cancelled()
    assert cancellations == [("preserved cancellation reason",)]
    assert mutation_released.is_set()
    assert events.store.settings(UUID(camera)) == {"enabled": False, "confidence_threshold": 0.65}
    assert len(calls) == 2 and calls[-1] == {"enabled": False, "confidence_threshold": 0.65}


@pytest.mark.parametrize("fault", ["write_refused", "unowned_third_state", "readback_mismatch"])
def test_unadmitted_compensation_latches_unavailable_and_invalidates_inflight_events(
    event_product, monkeypatch, fault
):
    from types import SimpleNamespace

    product, events = event_product
    camera = product.enroll()
    identifier = UUID(camera)
    source = product.cameras.get(identifier)
    events._cameras = SimpleNamespace(list_sources=lambda: (source,))
    events.configure(identifier, enabled=True, confidence_threshold=0.5)
    inference_started = threading.Event()
    inference_released = asyncio.Event()
    original = events.store.configure
    calls = []

    class Provider:
        async def __call__(self, _frame):
            inference_started.set()
            await inference_released.wait()
            return (SimpleNamespace(category="person", confidence=0.9),)

        async def aclose(self):
            pass

    events._provider = Provider()
    pending = product.client.portal.start_task_soon(events.cycle)
    try:
        assert inference_started.wait(2)
        generation = events._configuration_generation[identifier]

        def configure(*args, **kwargs):
            calls.append(dict(kwargs))
            if len(calls) == 2:
                if fault == "write_refused":
                    raise event_api.OperatorEventsError("Private compensation error")
                if fault == "readback_mismatch":
                    return {"enabled": True, "confidence_threshold": 0.7}
            result = original(*args, **kwargs)
            if len(calls) == 1:
                if fault == "unowned_third_state":
                    original(identifier, enabled=False, confidence_threshold=0.9)
                product.client.portal.call(product.sessions.revoke, product.token)
            return result

        monkeypatch.setattr(events.store, "configure", configure)
        response = product.client.patch(
            f"/api/v1/operator/cameras/{camera}/analytics",
            headers=product.headers,
            json={"enabled": True, "confidence_threshold": 0.7},
        )
        assert response.status_code == 503
        assert response.json() == {"detail": "analytics_unavailable"}
        assert response.headers["cache-control"] == "no-store"
        assert events._unavailable and events.defer_view_preparation()
        assert events._configuration_generation[identifier] > generation
        assert len(calls) == (1 if fault == "unowned_third_state" else 2)
    finally:
        product.client.portal.call(inference_released.set)
        pending.result(timeout=5)
    assert events.store.summary(identifier)["event_count"] == 0
    if fault == "unowned_third_state":
        assert events.store.settings(identifier) == {"enabled": False, "confidence_threshold": 0.9}
    attempts = events._sampler.index
    product.client.portal.call(events.cycle)
    assert events._sampler.index == attempts


def test_configuration_previous_state_failure_remains_fixed_unavailable(event_product, monkeypatch):
    product, events = event_product
    camera = product.enroll()

    def failed(_camera):
        raise event_api.OperatorEventsError("Private previous-state error")

    monkeypatch.setattr(events.store, "settings", failed)
    response = product.client.patch(
        f"/api/v1/operator/cameras/{camera}/analytics",
        headers=product.headers,
        json={"enabled": True, "confidence_threshold": 0.7},
    )
    assert response.status_code == 503
    assert response.json() == {"detail": "analytics_unavailable"}
    assert response.headers["cache-control"] == "no-store"
