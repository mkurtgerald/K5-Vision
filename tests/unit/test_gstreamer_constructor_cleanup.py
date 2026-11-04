"""Native constructor ownership tests; no native library or media is executed."""

from __future__ import annotations

import asyncio
import ctypes
from collections import Counter
from unittest.mock import Mock

import pytest

from k5vision.media import gstreamer_playback_decoder as decoder

_PRIVATE_DETAIL = "rtsp://user:secret@192.0.2.44/private"


@pytest.fixture
def native(monkeypatch, tmp_path):
    root = tmp_path / "runtime"
    bin_root = root / "bin"
    bin_root.mkdir(parents=True)
    for name in (decoder._CORE_FILENAMES[0], decoder._APP_FILENAMES[0]):
        (bin_root / name).touch()

    core = Mock()
    app = Mock()
    directory = Mock()
    events = []
    core.gst_init_check.return_value = 1
    core.gst_parse_launch.return_value = 101
    core.gst_parse_launch_full.return_value = 101
    core.gst_bin_get_by_name.side_effect = lambda _pipeline, name: {
        b"k5src": 102,
        b"k5sink": 103,
    }[name]

    def set_state(pipeline, state):
        events.append(("state", pipeline.value, state))
        return 1

    core.gst_element_set_state.side_effect = set_state
    core.gst_object_unref.side_effect = lambda item: events.append(("unref", item.value))
    directory.close.side_effect = lambda: events.append(("directory",))
    loader = Mock(side_effect=[core, app])
    monkeypatch.setattr(decoder.ctypes, "WinDLL", loader, raising=False)
    monkeypatch.setattr(decoder.os, "add_dll_directory", lambda _: directory, raising=False)
    if hasattr(decoder, "select_decoder"):
        monkeypatch.setattr(decoder, "select_decoder", lambda *_args, **_kwargs: "d3d11h264dec")
    return root, core, directory, loader, events


def _initialize(backend, root):
    backend.__init__(
        root,
        96,
        max_frame_bytes=1024,
        max_frames_per_push=4,
        pull_timeout_ms=0,
    )


def _assert_released(backend, core, directory, events, expected, *, cleanup_failed=False):
    assert Counter(item.args[0].value for item in core.gst_object_unref.call_args_list) == Counter(
        expected
    )
    directory.close.assert_called_once_with()
    assert events[-1] == ("directory",)
    if 101 in expected:
        assert ("state", 101, decoder._GST_STATE_NULL) in events
        assert events.index(("state", 101, decoder._GST_STATE_NULL)) < next(
            index for index, event in enumerate(events) if event[0] == "unref"
        )
    before = list(events)
    for _ in range(2):
        if cleanup_failed:
            with pytest.raises(
                decoder.NativePlaybackDecoderError, match="native decoder cleanup failed"
            ):
                backend.close()
        else:
            backend.close()
    assert events == before
    directory.close.assert_called_once_with()


@pytest.mark.parametrize(
    ("stage", "expected", "message"),
    [
        ("load_core", [], "native decoder initialization failed"),
        ("load_app", [], "native decoder initialization failed"),
        ("bind", [], "native decoder initialization failed"),
        ("init", [], "native decoder initialization failed"),
        ("parse", [], "native decoder pipeline could not be created"),
        ("source_lookup", [101], "native decoder initialization failed"),
        ("sink_lookup", [102, 101], "native decoder initialization failed"),
        ("source_missing", [103, 101], "native decoder endpoints are unavailable"),
        ("sink_missing", [102, 101], "native decoder endpoints are unavailable"),
        ("both_missing", [101], "native decoder endpoints are unavailable"),
        ("playing_failed", [102, 103, 101], "native decoder pipeline failed to start"),
        ("playing_raises", [102, 103, 101], "native decoder initialization failed"),
    ],
)
def test_constructor_releases_every_acquired_resource(
    native, monkeypatch, stage, expected, message
):
    root, core, directory, loader, events = native
    fault = OSError(_PRIVATE_DETAIL)
    if stage == "load_core":
        loader.side_effect = fault
    elif stage == "load_app":
        loader.side_effect = [core, fault]
    elif stage == "bind":
        monkeypatch.setattr(
            decoder._CtypesGStreamerBackend, "_bind_signatures", Mock(side_effect=fault)
        )
    elif stage == "init":
        core.gst_init_check.return_value = 0
    elif stage == "parse":
        core.gst_parse_launch.return_value = None
        core.gst_parse_launch_full.return_value = None
    elif stage in {"source_lookup", "sink_lookup"}:
        core.gst_bin_get_by_name.side_effect = fault if stage == "source_lookup" else [102, fault]
    elif stage in {"source_missing", "sink_missing", "both_missing"}:
        core.gst_bin_get_by_name.side_effect = {
            "source_missing": [None, 103],
            "sink_missing": [102, None],
            "both_missing": [None, None],
        }[stage]
    elif stage in {"playing_failed", "playing_raises"}:
        original = core.gst_element_set_state.side_effect

        def set_state(pipeline, state):
            if state == decoder._GST_STATE_PLAYING:
                if stage == "playing_raises":
                    raise fault
                return decoder._GST_STATE_CHANGE_FAILURE
            return original(pipeline, state)

        core.gst_element_set_state.side_effect = set_state

    backend = decoder._CtypesGStreamerBackend.__new__(decoder._CtypesGStreamerBackend)
    with pytest.raises(decoder.NativePlaybackDecoderError) as raised:
        _initialize(backend, root)
    assert raised.value.code is decoder.NativePlaybackDecoderErrorCode.NATIVE_FAILURE
    assert str(raised.value) == message
    assert _PRIVATE_DETAIL not in str(raised.value)
    _assert_released(backend, core, directory, events, expected)


@pytest.mark.parametrize("error_type", [AttributeError, OSError, ValueError, TypeError])
def test_constructor_sanitizes_startup_errors_and_releases_resources(native, error_type):
    root, core, directory, _loader, events = native
    original = core.gst_element_set_state.side_effect

    def set_state(pipeline, state):
        if state == decoder._GST_STATE_PLAYING:
            raise error_type(_PRIVATE_DETAIL)
        return original(pipeline, state)

    core.gst_element_set_state.side_effect = set_state
    backend = decoder._CtypesGStreamerBackend.__new__(decoder._CtypesGStreamerBackend)
    with pytest.raises(decoder.NativePlaybackDecoderError) as raised:
        _initialize(backend, root)
    assert str(raised.value) == "native decoder initialization failed"
    assert raised.value.code is decoder.NativePlaybackDecoderErrorCode.NATIVE_FAILURE
    assert raised.value.__suppress_context__
    _assert_released(backend, core, directory, events, [102, 103, 101])


@pytest.mark.parametrize("cleanup_stage", ["state", "unref", "directory"])
@pytest.mark.parametrize("error_type", [OSError, TypeError, ctypes.ArgumentError, RuntimeError])
def test_cleanup_faults_preserve_primary_error_and_continue(native, cleanup_stage, error_type):
    root, core, directory, _loader, events = native
    primary = decoder.NativePlaybackDecoderError(
        decoder.NativePlaybackDecoderErrorCode.NATIVE_FAILURE,
        "native decoder pipeline failed to start",
    )

    def set_state(pipeline, state):
        if state == decoder._GST_STATE_PLAYING:
            raise primary
        events.append(("state", pipeline.value, state))
        if cleanup_stage == "state":
            raise error_type(_PRIVATE_DETAIL)
        return 1

    def unref(item):
        events.append(("unref", item.value))
        if cleanup_stage == "unref":
            raise error_type(_PRIVATE_DETAIL)

    def close_directory():
        events.append(("directory",))
        if cleanup_stage == "directory":
            raise error_type(_PRIVATE_DETAIL)

    core.gst_element_set_state.side_effect = set_state
    core.gst_object_unref.side_effect = unref
    directory.close.side_effect = close_directory
    backend = decoder._CtypesGStreamerBackend.__new__(decoder._CtypesGStreamerBackend)
    with pytest.raises(decoder.NativePlaybackDecoderError) as raised:
        _initialize(backend, root)
    assert raised.value is primary
    assert _PRIVATE_DETAIL not in str(raised.value)
    _assert_released(backend, core, directory, events, [102, 103, 101], cleanup_failed=True)


def test_success_retains_resources_until_idempotent_close(native):
    root, core, directory, _loader, events = native
    backend = decoder._CtypesGStreamerBackend.__new__(decoder._CtypesGStreamerBackend)
    _initialize(backend, root)
    core.gst_object_unref.assert_not_called()
    directory.close.assert_not_called()
    assert events == [("state", 101, decoder._GST_STATE_PLAYING)]
    backend.close()
    _assert_released(backend, core, directory, events, [102, 103, 101])


@pytest.mark.parametrize("cleanup_stage", ["state", "unref", "directory"])
@pytest.mark.parametrize("error_type", [OSError, TypeError, ctypes.ArgumentError, RuntimeError])
def test_normal_close_reports_failure_after_attempting_all_releases(
    native, cleanup_stage, error_type
):
    root, core, directory, _loader, events = native
    backend = decoder._CtypesGStreamerBackend.__new__(decoder._CtypesGStreamerBackend)
    _initialize(backend, root)
    operation = {
        "state": core.gst_element_set_state,
        "unref": core.gst_object_unref,
        "directory": directory.close,
    }[cleanup_stage]
    original = operation.side_effect

    def fail(*args):
        original(*args)
        raise error_type(_PRIVATE_DETAIL)

    operation.side_effect = fail
    with pytest.raises(decoder.NativePlaybackDecoderError) as raised:
        backend.close()
    assert raised.value.code is decoder.NativePlaybackDecoderErrorCode.NATIVE_FAILURE
    assert str(raised.value) == "native decoder cleanup failed"
    assert raised.value.__suppress_context__
    _assert_released(backend, core, directory, events, [102, 103, 101], cleanup_failed=True)


def test_normal_close_reports_failed_null_state_after_attempting_all_releases(native):
    root, core, directory, _loader, events = native
    backend = decoder._CtypesGStreamerBackend.__new__(decoder._CtypesGStreamerBackend)
    _initialize(backend, root)

    def fail_state(pipeline, state):
        events.append(("state", pipeline.value, state))
        return decoder._GST_STATE_CHANGE_FAILURE

    core.gst_element_set_state.side_effect = fail_state
    with pytest.raises(decoder.NativePlaybackDecoderError, match="native decoder cleanup failed"):
        backend.close()
    _assert_released(backend, core, directory, events, [102, 103, 101], cleanup_failed=True)


def test_early_library_failure_preserves_primary_error_when_directory_close_fails(native):
    root, core, directory, loader, events = native
    loader.side_effect = OSError(_PRIVATE_DETAIL)

    def fail_directory():
        events.append(("directory",))
        raise TypeError(_PRIVATE_DETAIL)

    directory.close.side_effect = fail_directory
    backend = decoder._CtypesGStreamerBackend.__new__(decoder._CtypesGStreamerBackend)
    with pytest.raises(decoder.NativePlaybackDecoderError) as raised:
        _initialize(backend, root)
    assert str(raised.value) == "native decoder initialization failed"
    _assert_released(backend, core, directory, events, [], cleanup_failed=True)


@pytest.mark.parametrize("cleanup_stage", ["state", "unref", "directory"])
def test_subclass_preemptive_close_does_not_replace_startup_error(native, cleanup_stage):
    root, core, directory, _loader, events = native
    operation = {
        "state": core.gst_element_set_state,
        "unref": core.gst_object_unref,
        "directory": directory.close,
    }[cleanup_stage]
    original = operation.side_effect

    def fail(*args):
        original(*args)
        raise TypeError(_PRIVATE_DETAIL)

    operation.side_effect = fail
    primary = decoder.NativePlaybackDecoderError(
        decoder.NativePlaybackDecoderErrorCode.NATIVE_FAILURE,
        "direct live decoder pipeline failed during startup",
    )

    class StartupBackend(decoder._CtypesGStreamerBackend):
        def _build_pipeline(self, payload_type):
            self._pipeline = ctypes.c_void_p(101)
            self._sink = ctypes.c_void_p(103)
            self.close()
            raise primary

    backend = StartupBackend.__new__(StartupBackend)
    with pytest.raises(decoder.NativePlaybackDecoderError) as raised:
        _initialize(backend, root)
    assert raised.value is primary
    assert not backend._initializing
    _assert_released(backend, core, directory, events, [103, 101], cleanup_failed=True)


def test_public_close_retains_failed_state_on_retry_without_double_release(native):
    root, core, directory, _loader, events = native
    backend = decoder._CtypesGStreamerBackend.__new__(decoder._CtypesGStreamerBackend)
    _initialize(backend, root)

    def fail_unref(item):
        events.append(("unref", item.value))
        raise OSError(_PRIVATE_DETAIL)

    core.gst_object_unref.side_effect = fail_unref
    public = decoder.GStreamerNativePlaybackDecoder(96, backend=backend)
    for _ in range(2):
        with pytest.raises(decoder.NativePlaybackDecoderError) as raised:
            asyncio.run(public.close())
        assert str(raised.value) == "native decoder cleanup failed"
        assert public.snapshot.state is decoder.NativePlaybackDecoderState.FAILED
    _assert_released(backend, core, directory, events, [102, 103, 101], cleanup_failed=True)
