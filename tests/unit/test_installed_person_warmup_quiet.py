"""Deterministic scheduling contract for installed native view-model warmup.

The quiet timer is camera-free and preserves the recorder's model-readiness
gate. These tests never use or retain source URIs, camera pixels or credentials.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

from k5vision.operator_events import OperatorEventService, RecordedFrameSampler
from k5vision.operator_product_api import _NativePersonWarmupGate


class _Analytics:
    def __init__(self):
        self.calls = 0

    def start_preparing(self):
        self.calls += 1


class _Handle:
    def __init__(self, callback):
        self.callback = callback
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


def test_installed_warmup_waits_for_six_second_quiet_period_and_recorder_readiness(
    monkeypatch,
):
    async def journey():
        analytics = _Analytics()
        recorder = {"awaiting": True}
        loop = asyncio.get_running_loop()
        calls = []

        def call_later(delay, callback):
            handle = _Handle(callback)
            calls.append((delay, handle))
            return handle

        monkeypatch.setattr(loop, "call_later", call_later)
        gate = _NativePersonWarmupGate(analytics, defer_preparation=lambda: recorder["awaiting"])
        gate.schedule(enrollment=True)
        assert calls[-1][0] == 6.0
        first = calls[-1][1]
        gate.schedule(enrollment=True)
        assert first.cancelled
        assert calls[-1][0] == 6.0
        assert analytics.calls == 0

        # An unprimed recorder or one still awaiting an event cannot consume
        # the separate view worker's CPU during the original enrollment.
        calls[-1][1].callback()
        assert analytics.calls == 0
        assert calls[-1][0] == 2.0

        # A living native recorder worker permits optional precompilation.
        # This is not person-event or sampled-frame acceptance.
        recorder["awaiting"] = False
        calls[-1][1].callback()
        assert analytics.calls == 1
        gate.schedule(enrollment=True)
        gate.on_first_recorded_person_event()
        assert analytics.calls == 1
        gate.close()

    asyncio.run(journey())


def test_recorded_person_event_still_bypasses_quiet_timer_once(monkeypatch):
    async def journey():
        analytics = _Analytics()
        loop = asyncio.get_running_loop()
        handles = []

        def call_later(delay, callback):
            handle = _Handle(callback)
            handles.append(handle)
            return handle

        monkeypatch.setattr(loop, "call_later", call_later)
        gate = _NativePersonWarmupGate(analytics, defer_preparation=lambda: True)
        gate.schedule(enrollment=True)
        gate.on_first_recorded_person_event()
        assert handles[0].cancelled
        assert analytics.calls == 1
        gate.on_first_recorded_person_event()
        assert analytics.calls == 1
        gate.close()
        assert analytics.calls == 1

    asyncio.run(journey())



def test_early_installed_view_warmup_requires_live_recorder_and_no_active_analytics(
    monkeypatch,
):
    """The optional worker may use post-enrollment quiet time, not first-event CPU."""
    camera_id = uuid4()
    recorder = object.__new__(RecordedFrameSampler)
    recorder._configuration = object()
    child = {"ready": False}
    monkeypatch.setattr(recorder, "native_model_ready", lambda: child["ready"])
    state = {"enabled": False}
    service = object.__new__(OperatorEventService)
    service._closing = False
    service._unavailable = False
    service._available = True
    service._sampler = recorder
    service._cameras = SimpleNamespace(
        list_sources=lambda: (SimpleNamespace(binding=SimpleNamespace(device_id=camera_id)),)
    )
    service.store = SimpleNamespace(settings=lambda _identifier: {"enabled": state["enabled"]})
    service._last_events = {}

    # Neither prior compilation attempts nor camera enrollment alone qualify.
    assert service.awaiting_first_recorded_person_event()
    child["ready"] = True
    assert not service.awaiting_first_recorded_person_event()

    # Once authoritative recording analytics are enabled, avoid CPU contention
    # until a real accepted person event. Readiness is not event admission.
    state["enabled"] = True
    assert service.awaiting_first_recorded_person_event()
    service._last_events[camera_id] = 1.0
    assert not service.awaiting_first_recorded_person_event()
