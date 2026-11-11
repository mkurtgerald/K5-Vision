"""Finite subprocess contract for installed view-only OpenVINO inference."""

from __future__ import annotations

import asyncio
import io
import json
import threading
from types import SimpleNamespace

import pytest

import k5vision.operator_person_view_process as module


def _worker(report):
    worker = object.__new__(module._ViewPersonWorker)
    worker._closing = threading.Event()
    worker._process = SimpleNamespace(
        stdin=io.BytesIO(),
        stdout=io.BytesIO(json.dumps(report).encode("utf-8") + b"\n"),
        poll=lambda: None,
    )
    return worker


def test_person_worker_sends_only_geometry_and_transient_bgrx_to_private_stdin():
    report = {
        "status": "ready",
        "boxes": [{"confidence": 0.93, "x_min": 0.1, "y_min": 0.2, "x_max": 0.7, "y_max": 0.9}],
    }
    worker = _worker(report)
    frame = bytes(range(64))
    boxes = worker.infer(frame, 4, 4, 16, 7)
    assert len(boxes) == 1 and boxes[0].category == "person"
    assert boxes[0].box.x_max == 0.7
    sent = worker._process.stdin.getvalue()
    header, raw = sent.split(b"\n", 1)
    assert json.loads(header) == {"bytes": 64, "width": 4, "height": 4, "stride": 16, "elapsed": 7}
    assert raw == frame
    assert frame not in worker._process.stdout.getvalue()


@pytest.mark.parametrize(
    "box",
    [
        {"confidence": 0.93, "x_min": 0.8, "y_min": 0.2, "x_max": 0.7, "y_max": 0.9},
        {"confidence": float("nan"), "x_min": 0.1, "y_min": 0.2, "x_max": 0.7, "y_max": 0.9},
        {"confidence": 0.1, "x_min": 0.1, "y_min": 0.2, "x_max": 0.7, "y_max": 0.9},
        {"confidence": 0.93, "x_min": 0.1, "y_min": 0.2, "x_max": 0.7, "y_max": 2},
    ],
)
def test_person_worker_rejects_invalid_geometry_without_acceptance_weakening(box):
    worker = _worker({"status": "ready", "boxes": [box]})
    with pytest.raises(RuntimeError, match="response unavailable"):
        worker.infer(bytes(64), 4, 4, 16, 7)


def test_person_worker_refuses_byte_length_or_closed_native_worker():
    worker = _worker({"status": "ready", "boxes": []})
    with pytest.raises(RuntimeError, match="frame refused"):
        worker.infer(bytes(63), 4, 4, 16, 7)
    worker._closing.set()
    with pytest.raises(RuntimeError, match="worker unavailable"):
        worker.infer(bytes(64), 4, 4, 16, 7)


def test_isolated_worker_start_is_bounded_and_does_not_use_app_event_loop(monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    class Worker:
        def __init__(self, _configuration):
            self.closed = False

        def start(self):
            entered.set()
            return release.wait(2)

        def begin_close(self):
            self.closed = True
            release.set()

        def finish_close(self):
            pass

    monkeypatch.setattr(module, "_ViewPersonWorker", Worker)

    async def run():
        session = module.IsolatedPersonSession(object(), lambda: None)
        await asyncio.to_thread(entered.wait, 1)
        # A canceled optional native preparation is terminated, not waited
        # on inside the ASGI request loop.
        await session.aclose()
        assert session._released

    asyncio.run(run())
