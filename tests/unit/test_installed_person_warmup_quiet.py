"""Deterministic scheduling contract for installed native view-model warmup.

The quiet timer is camera-free and preserves the recorder's model-readiness
gate. These tests never use or retain source URIs, camera pixels or credentials.
"""

from __future__ import annotations

import asyncio

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
        gate = _NativePersonWarmupGate(
            analytics, defer_preparation=lambda: recorder["awaiting"]
        )
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
        gate = _NativePersonWarmupGate(
            analytics, defer_preparation=lambda: True
        )
        gate.schedule(enrollment=True)
        gate.on_first_recorded_person_event()
        assert handles[0].cancelled
        assert analytics.calls == 1
        gate.on_first_recorded_person_event()
        assert analytics.calls == 1
        gate.close()
        assert analytics.calls == 1

    asyncio.run(journey())
