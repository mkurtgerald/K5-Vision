from __future__ import annotations

import ctypes

import pytest

from k5vision.media import native_rtsp_pipeline
from k5vision.media.native_rtsp_pipeline import (
    NativeRtspPipeline,
    NativeRtspPipelineError,
    NativeRtspPipelineUnavailable,
    _GstMessage,
    quote_pipeline_value,
)


class FakeCore:
    def __init__(self) -> None:
        self.description: bytes | None = None
        self.message: _GstMessage | None = None
        self.state_calls: list[int] = []
        self.unrefs = 0

    def gst_init_check(self, *args) -> int:
        return 1

    def gst_parse_launch(self, description: bytes, error) -> int:
        self.description = description
        return 1234

    def gst_element_set_state(self, pipeline, state: int) -> int:
        self.state_calls.append(state)
        return 1

    def gst_element_get_state(self, pipeline, current, pending, timeout: int) -> int:
        return 1

    def gst_element_get_bus(self, pipeline) -> int:
        return 4321

    def gst_bus_timed_pop_filtered(self, bus, timeout: int, types: int):
        if self.message is None:
            return None
        return ctypes.cast(ctypes.pointer(self.message), ctypes.c_void_p).value

    def gst_mini_object_unref(self, message) -> None:
        self.unrefs += 1

    def gst_object_unref(self, item) -> None:
        self.unrefs += 1


def _pipeline(core: FakeCore) -> NativeRtspPipeline:
    pipeline = NativeRtspPipeline.__new__(NativeRtspPipeline)
    pipeline._core = core
    pipeline._pipeline = ctypes.c_void_p()
    pipeline._dll_directory = None
    return pipeline


def test_quote_pipeline_value_escapes_without_argv_contract() -> None:
    value = 'rtsp://user:secret@example.invalid/live?label="door"'
    quoted = quote_pipeline_value(value)

    assert quoted.startswith('"')
    assert quoted.endswith('"')
    assert '\\"door\\"' in quoted


@pytest.mark.parametrize("value", ["", "   ", "rtsp://example.invalid/a\nsecret", "x\x00y"])
def test_quote_pipeline_value_rejects_invalid_input(value: str) -> None:
    with pytest.raises(ValueError):
        quote_pipeline_value(value)


def test_runtime_root_fails_closed_without_configuration(monkeypatch) -> None:
    monkeypatch.delenv("K5_GSTREAMER_ROOT", raising=False)

    with pytest.raises(NativeRtspPipelineUnavailable):
        native_rtsp_pipeline._runtime_root()


def test_native_pipeline_start_wait_and_close_without_retaining_diagnostics() -> None:
    core = FakeCore()
    pipeline = _pipeline(core)

    pipeline._start('rtspsrc location="rtsp://user:secret@example/live" ! fakesink', 0.1)
    assert core.description is not None
    assert b"user:secret" in core.description

    core.message = _GstMessage(type_=1)
    assert pipeline.wait_for_terminal(timeout_seconds=0.1) == "eos"

    core.message = _GstMessage(type_=2)
    assert pipeline.wait_for_terminal(timeout_seconds=0.1) == "error"

    core.message = None
    assert pipeline.wait_for_terminal(timeout_seconds=0.1) == "timeout"

    pipeline.close(timeout_seconds=0.1)
    assert pipeline._pipeline.value is None
    assert core.unrefs >= 3


def test_terminal_wait_rejects_invalid_timeout() -> None:
    pipeline = _pipeline(FakeCore())
    pipeline._pipeline = ctypes.c_void_p(1234)

    with pytest.raises(ValueError):
        pipeline.wait_for_terminal(timeout_seconds=0)


def test_terminal_wait_fails_closed_without_bus() -> None:
    core = FakeCore()
    core.gst_element_get_bus = lambda pipeline: 0  # type: ignore[method-assign]
    pipeline = _pipeline(core)
    pipeline._pipeline = ctypes.c_void_p(1234)

    with pytest.raises(NativeRtspPipelineError):
        pipeline.wait_for_terminal(timeout_seconds=0.1)
