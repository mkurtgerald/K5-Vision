"""Read-only prerequisite and mocked service binding/generation regressions."""

import asyncio
from urllib.parse import urlsplit

import pytest
from test_operator_product_service import _fast_maintenance, _source, _until
from test_operator_product_service import native as _native_fixture

from k5vision.operator_product_analytics import configured_sample_coordinator
from k5vision.operator_product_service import OperatorProductError

native = _native_fixture


def test_missing_runtime_does_not_construct_provider_or_decoder(monkeypatch):
    import k5vision.operator_product_analytics as module

    monkeypatch.delenv("K5_ANALYTICS_CONFIG", raising=False)
    monkeypatch.delenv("K5_GSTREAMER_ROOT", raising=False)

    def forbidden(*args, **kwargs):
        raise AssertionError("Native construction must not run")

    monkeypatch.setattr(module, "AnalyticsProviderFactory", forbidden)
    monkeypatch.setattr(module, "GStreamerDirectFrameDelivery", forbidden)
    assert configured_sample_coordinator().status() == "unavailable"


def test_explicit_invalid_runtime_is_unavailable_and_never_exposes_path(monkeypatch):
    monkeypatch.setenv("K5_ANALYTICS_CONFIG", "/private/not-a-valid-config")
    c = configured_sample_coordinator()
    assert c.status() == "unavailable"


def test_source_is_derived_from_current_service_config_only(native):
    async def check():
        s = native.service
        await s.start((_source(1),))
        try:
            generation, uri = s.analytics_selection(_source(1))
            parsed = urlsplit(uri)
            assert parsed.hostname == "127.0.0.1"
            assert parsed.port == s.ports["rtsp_port"]
            assert parsed.path == "/" + _source(1).binding.path_name
            assert parsed.username == s.credentials.username
            assert parsed.password == s.credentials.password
            assert generation == s.analytics_generation
            with pytest.raises(OperatorProductError):
                s.analytics_selection(_source(2))
            s._needs_reconcile = True
            with pytest.raises(OperatorProductError):
                s.analytics_selection(_source(1))
        finally:
            await s.stop()

    asyncio.run(check())


def test_restart_and_reconcile_invalidate_generation_before_native_changes(native, monkeypatch):
    _fast_maintenance(native, monkeypatch)

    async def check():
        s = native.service
        generations = []
        s.analytics_invalidator = lambda: generations.append(s.analytics_generation)
        await s.start((_source(1),))
        try:
            first = s.analytics_generation
            media = s._media
            media.returncode = 17
            await _until(lambda: s._media is not media and s.status() == "ready")
            assert s.analytics_generation > first
            old = s.analytics_generation

            def fail(*args):
                raise RuntimeError("private")

            monkeypatch.setattr(s, "_mutation", fail)
            with pytest.raises(RuntimeError):
                await s.apply_sources((_source(2),))
            assert s.analytics_generation > old
            with pytest.raises(OperatorProductError):
                s.analytics_selection(_source(1))
        finally:
            await s.stop()
        assert len(generations) >= 4

    asyncio.run(check())


def test_composition_owns_coordinator_and_closes_it_before_media_even_on_failure(
    native, tmp_path, monkeypatch
):
    from fastapi.testclient import TestClient
    from test_operator_product_service import _product_application

    import k5vision.operator_product as module
    from k5vision.operator_product_analytics import SampleError

    calls = []

    class Coordinator:
        def status(self):
            return "unavailable"

        def invalidate(self):
            calls.append("invalidate")

        async def aclose(self):
            calls.append("analytics-close")
            raise SampleError(503, "Analyzed sample cleanup incomplete")

    coordinator = Coordinator()
    registry = {}
    monkeypatch.setattr(module, "_PRODUCT_LEASES", registry)
    monkeypatch.setattr(module, "configured_sample_coordinator", lambda: coordinator)
    app, media_calls = _product_application(native, tmp_path, monkeypatch)
    assert app.state.operator_product_analytics is coordinator
    assert app.state.operator_product_service.analytics_invalidator == coordinator.invalidate
    original_stop = app.state.operator_product_service.stop

    async def stop():
        assert calls == ["analytics-close"]
        await original_stop()

    app.state.operator_product_service.stop = stop
    with pytest.raises(SampleError):
        with TestClient(app):
            pass
    assert media_calls == [("start", ()), ("stop", ())]
    assert app.state.operator_camera_store.closed
    assert list(registry.values()) == [app.state.operator_profile_lock]
    # Test-only disposal: this coordinator is a mock with no native resources.
    app.state.operator_profile_lock.close()
    registry.clear()


def test_cancelled_lifespan_retains_media_and_profile_until_owned_sample_drains(
    native, tmp_path, monkeypatch
):
    from test_operator_product_analytics import OWNER, Runner, setup
    from test_operator_product_service import _product_application

    import k5vision.operator_product as module
    from k5vision.operator_profile_lock import OperatorProfileLock, OperatorProfileLockError

    async def check():
        gate = asyncio.Event()
        coordinator, runner, _ = setup(runner=Runner(gate))
        monkeypatch.setattr(module, "configured_sample_coordinator", lambda: coordinator)
        app, calls = _product_application(native, tmp_path, monkeypatch)
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        coordinator.admit(OWNER, "private")
        await asyncio.sleep(0)
        closing = asyncio.create_task(lifespan.__aexit__(None, None, None))
        await asyncio.sleep(0)
        lease = OperatorProfileLock(tmp_path / "identity")
        try:
            for _ in range(3):
                closing.cancel()
                await asyncio.sleep(0)
                assert not closing.done() and not runner.closed
                assert calls == [("start", ())]
                assert not app.state.operator_camera_store.closed
                with pytest.raises(OperatorProfileLockError):
                    lease.acquire()
        finally:
            gate.set()
            with pytest.raises(asyncio.CancelledError):
                await closing
        assert runner.closed and calls == [("start", ()), ("stop", ())]
        assert app.state.operator_camera_store.closed
        lease.acquire()
        lease.close()

    asyncio.run(check())


def test_unresolved_native_cleanup_retains_profile_across_gc_and_repeated_close(
    native, tmp_path, monkeypatch
):
    import gc
    import weakref

    from test_operator_product_analytics import OWNER, Runner
    from test_operator_product_service import _product_application

    import k5vision.operator_product as module
    from k5vision.operator_product_analytics import AnalyzedSampleCoordinator, SampleError
    from k5vision.operator_profile_lock import OperatorProfileLock, OperatorProfileLockError

    registry = {}
    monkeypatch.setattr(module, "_PRODUCT_LEASES", registry)

    async def check():
        async def factory():
            # Represents existing factory's unresolved post-timeout cleanup.
            raise TimeoutError("private")

        coordinator = AnalyzedSampleCoordinator(factory, runner_factory=Runner)
        monkeypatch.setattr(module, "configured_sample_coordinator", lambda: coordinator)
        app, calls = _product_application(native, tmp_path, monkeypatch)
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        lease = app.state.operator_profile_lock
        reference = weakref.ref(lease)
        key = id(lease)
        coordinator.admit(OWNER, "private")
        await asyncio.shield(coordinator._task)
        with pytest.raises(SampleError):
            await lifespan.__aexit__(None, None, None)
        assert app.state.operator_camera_store.closed
        assert calls == [("start", ()), ("stop", ())]
        assert registry[key] is lease
        for _ in range(2):
            with pytest.raises(SampleError):
                await coordinator.aclose()
            with pytest.raises(OperatorProductError):
                module._own_product_lease(lease)
            assert registry[key] is lease
        del app, lifespan, lease
        gc.collect()
        assert reference() is registry[key]
        second = OperatorProfileLock(tmp_path / "identity")
        with pytest.raises(OperatorProfileLockError):
            second.acquire()
        # A separately composed product also cannot claim this retained profile.
        from k5vision.operator_product_analytics import configured_sample_coordinator

        monkeypatch.setattr(module, "configured_sample_coordinator", configured_sample_coordinator)
        other, other_calls = _product_application(
            native, tmp_path, monkeypatch, identity_directory=tmp_path / "identity"
        )
        with pytest.raises(OperatorProfileLockError):
            async with other.router.lifespan_context(other):
                pass
        assert other_calls == [("stop", ())]
        assert list(registry) == [key], "Failed acquisition must roll back only its own slot"
        # Test-only cleanup after a mock with no native work. Production retains
        # this lease until process termination; no recovery hook is exposed.
        retained = registry.pop(key)
        retained.close()

    asyncio.run(check())


def test_lease_registry_cap_refuses_before_acquisition_and_normal_close_releases_once(
    native, tmp_path, monkeypatch
):
    from test_operator_product_service import _product_application

    import k5vision.operator_product as module
    from k5vision.operator_profile_lock import OperatorProfileLock

    async def check():
        # Fill bookkeeping with lease objects that own no descriptors.
        held = [OperatorProfileLock(tmp_path) for _ in range(64)]
        registry = {id(lease): lease for lease in held}
        monkeypatch.setattr(module, "_PRODUCT_LEASES", registry)
        app, calls = _product_application(native, tmp_path, monkeypatch)
        with pytest.raises(OperatorProductError, match="capacity"):
            async with app.router.lifespan_context(app):
                pass
        assert len(registry) == 64 and calls == [("stop", ())]
        probe = OperatorProfileLock(tmp_path / "identity")
        probe.acquire()
        probe.close()
        registry.clear()
        clean, calls = _product_application(
            native, tmp_path, monkeypatch, identity_directory=tmp_path / "identity"
        )
        lease = clean.state.operator_profile_lock
        count = []
        original = lease.close

        def close():
            count.append(True)
            original()

        monkeypatch.setattr(lease, "close", close)
        async with clean.router.lifespan_context(clean):
            assert list(registry.values()) == [lease]
        assert count == [True] and not registry
        assert calls == [("start", ()), ("stop", ())]
        probe.acquire()
        probe.close()

    asyncio.run(check())


@pytest.mark.parametrize("interrupt", [False, True])
def test_failed_or_interrupted_media_stop_retains_lease_and_preserves_primary_error(
    native, tmp_path, monkeypatch, interrupt
):
    from test_operator_product_service import _product_application

    import k5vision.operator_product as module
    from k5vision.operator_profile_lock import OperatorProfileLock, OperatorProfileLockError

    registry = {}
    monkeypatch.setattr(module, "_PRODUCT_LEASES", registry)

    async def check():
        app, _ = _product_application(native, tmp_path, monkeypatch)
        entered, release = asyncio.Event(), asyncio.Event()
        store_attempted = []

        async def stop():
            if interrupt:
                entered.set()
                await release.wait()
            raise OperatorProductError("Native operator cleanup was incomplete")

        def store_close():
            store_attempted.append(True)
            raise RuntimeError("Secondary store cleanup failure")

        app.state.operator_product_service.stop = stop
        app.state.operator_camera_store.close = store_close
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        closing = asyncio.create_task(lifespan.__aexit__(None, None, None))
        if interrupt:
            await entered.wait()
            closing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await closing
        else:
            with pytest.raises(
                OperatorProductError, match="Native operator cleanup was incomplete"
            ):
                await closing
        assert store_attempted == [True]
        lease = OperatorProfileLock(tmp_path / "identity")
        with pytest.raises(OperatorProfileLockError):
            lease.acquire()
        assert list(registry.values()) == [app.state.operator_profile_lock]
        # Test-only disposal after mocked stop; production offers no unlock/reset.
        retained = registry.pop(id(app.state.operator_profile_lock))
        retained.close()

    asyncio.run(check())
