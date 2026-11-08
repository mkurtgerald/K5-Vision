"""Keep transient media-service readiness probes off the API event loop."""

import asyncio
import threading

from k5vision.operator_product_api import _await_media_ready


def test_media_ready_probe_does_not_block_the_api_event_loop():
    entered = threading.Event()
    released = threading.Event()

    def blocking_native_status():
        entered.set()
        if not released.wait(1.5):
            raise AssertionError("Native status call held the event loop")
        return "ready"

    async def exercise():
        operation = asyncio.create_task(_await_media_ready(blocking_native_status))
        try:
            started = await asyncio.wait_for(asyncio.to_thread(entered.wait, 2), timeout=2.5)
            assert started is True
            assert not operation.done()
            released.set()
            assert await asyncio.wait_for(operation, timeout=2) is False
        finally:
            released.set()

    asyncio.run(exercise())
