"""Portable checks of the bounded owned-window native pixel witness, never Win32 execution."""

from __future__ import annotations

import ctypes
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

_PATH = Path(__file__).parent / "integration" / "test_current_gate_target_windows.py"
_SPEC = importlib.util.spec_from_file_location("owned_native_pixel_witness", _PATH)
assert _SPEC and _SPEC.loader
witness = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(witness)


def _reference_pixel(geometry: tuple[int, ...], *, missing_box: bool = False):
    frame = witness._generated_box_frame(1)
    _width, _height, left, top, width, height = geometry

    def read(x: int, y: int) -> int:
        if not (left <= x < left + width and top <= y < top + height):
            return 0
        if missing_box:
            return witness._GRAY_COLORREF
        sx, sy = (x - left) * 192 // width, (y - top) * 108 // height
        offset = sy * frame.stride_bytes + sx * 4
        b, g, r, _unused = frame.payload[offset : offset + 4]
        return r | (g << 8) | (b << 16)

    return read


@pytest.mark.parametrize("geometry", witness._PIXEL_CASES.values())
def test_generated_fixture_matches_independent_complete_client_oracle(geometry) -> None:
    stats = witness._pixel_statistics(geometry, _reference_pixel(geometry))
    witness._assert_pixel_statistics(geometry, stats)
    assert stats["sampled_pixels"] == geometry[0] * geometry[1]
    assert stats["green_pixels"] > 0
    assert set(stats) == witness._PIXEL_STAT_FIELDS
    assert all(type(v) is int for v in stats.values())


def test_original_unscaled_clipping_is_rejected_by_pixel_oracle() -> None:
    frame = witness._generated_box_frame(1)
    geometry = witness._PIXEL_CASES["downscale"]

    def unscaled(x: int, y: int) -> int:
        offset = y * frame.stride_bytes + x * 4
        b, g, r, _unused = frame.payload[offset : offset + 4]
        return r | (g << 8) | (b << 16)

    stats = witness._pixel_statistics(geometry, unscaled)
    assert stats["green_pixels"] == 0
    with pytest.raises(AssertionError):
        witness._assert_pixel_statistics(geometry, stats)


@pytest.mark.parametrize(
    "fault", ["missing_box", "stale_letterbox", "solid_green", "invalid_pixel", "no_paint"]
)
def test_pixel_oracle_refuses_false_success(fault: str) -> None:
    geometry = witness._PIXEL_CASES["letterbox"]
    good = _reference_pixel(geometry, missing_box=fault == "missing_box")

    def read(x: int, y: int) -> int:
        if fault == "invalid_pixel":
            return 0xFFFFFFFF
        if fault == "solid_green":
            return witness._GREEN_COLORREF
        if fault == "no_paint":
            return 0x00FFFFFF
        if fault == "stale_letterbox" and (y < 12 or y >= 84):
            return 0x00FFFFFF
        return good(x, y)

    with pytest.raises(AssertionError):
        stats = witness._pixel_statistics(geometry, read)
        witness._assert_pixel_statistics(geometry, stats)


@pytest.mark.parametrize(
    "geometry",
    [(0, 1, 0, 0, 1, 1), (193, 1, 0, 0, 193, 1), (1, 1, -1, 0, 1, 1), (1, 1, 0, 0, 2, 1)],
)
def test_pixel_oracle_refuses_unbounded_coordinates_before_read(geometry) -> None:
    def forbidden(*_args: object) -> int:
        raise RuntimeError("read must not be reached")

    with pytest.raises(AssertionError, match="geometry exceeds"):
        witness._pixel_statistics(geometry, forbidden)


def test_pixel_witness_binds_real_imported_product_bytes() -> None:
    digest = witness._bound_pixel_product()
    assert len(digest) == 64
    assert witness._PIXEL_PRODUCT_REVISION == "6990e51b4101d6738ecb1279cea44df2797dbb07"
    assert len(witness._PIXEL_PRODUCT_FILES) == 6


def test_changed_product_bytes_cannot_be_silently_qualified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    changed = dict(witness._PIXEL_PRODUCT_FILES)
    changed["media/presentation_frame.py"] = "0" * 64
    monkeypatch.setattr(witness, "_PIXEL_PRODUCT_FILES", changed)
    with pytest.raises(AssertionError, match="source binding failed"):
        witness._bound_pixel_product()


@pytest.fixture
def inert_api(monkeypatch: pytest.MonkeyPatch):
    import k5vision.media.windows_presentation_target as target_module

    state = SimpleNamespace(
        size=(128, 72),
        minimized=False,
        get_dc=[],
        release_dc=[],
        destroy=[],
        pixel_calls=0,
        pixel_failure=False,
        flush_failure=False,
    )

    def client(_handle: object, pointer: object) -> int:
        rect = ctypes.cast(pointer, ctypes.POINTER(target_module._ClientRect)).contents
        rect.left = rect.top = 0
        rect.right, rect.bottom = state.size
        return 1

    def get_dc(handle: ctypes.c_void_p) -> int:
        state.get_dc.append(handle.value)
        assert handle.value == 71
        return 501

    def release_dc(handle: ctypes.c_void_p, dc: ctypes.c_void_p) -> int:
        state.release_dc.append((handle.value, dc.value))
        return 1

    def destroy(handle: ctypes.c_void_p) -> int:
        state.destroy.append(handle.value)
        return 1

    def position(handle, after, _x, _y, width, height, _flags) -> int:
        assert handle.value == 71
        state.size = (width, height)
        return 1

    def show(handle: ctypes.c_void_p, command: int) -> int:
        assert handle.value == 71
        state.minimized = command == 6
        return 0  # Previous visibility is not a failure indicator.

    def pixel(dc: ctypes.c_void_p, x: int, y: int) -> int:
        assert dc.value == 501
        assert 0 <= x < state.size[0] and 0 <= y < state.size[1]
        state.pixel_calls += 1
        return 0xFFFFFFFF if state.pixel_failure else witness._GRAY_COLORREF

    callbacks = {
        "GetModuleHandleW": lambda *_: 3,
        "CreateWindowExW": lambda *_: 71,
        "GetAncestor": lambda *_: 71,
        "IsIconic": lambda *_: int(state.minimized),
        "GetClientRect": client,
        "GetDC": get_dc,
        "ReleaseDC": release_dc,
        "DestroyWindow": destroy,
        "GetPixel": pixel,
        "GdiFlush": lambda: int(not state.flush_failure),
        "PatBlt": lambda *_: 1,
        "SetWindowPos": position,
        "ShowWindow": show,
        "UpdateWindow": lambda *_: 1,
    }

    class Library:
        def __getattr__(self, name: str):
            return callbacks[name]

    monkeypatch.setattr(target_module.sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: Library(), raising=False)
    api = witness._owned_pixel_api()
    api.create_target(17, 29, 128, 72)
    return api, state, callbacks


@pytest.mark.parametrize("target", [0, -1, 72, True])
def test_pixel_probe_refuses_desktop_or_unowned_targets(inert_api, target) -> None:
    api, state, _callbacks = inert_api
    with pytest.raises(AssertionError, match="unowned"):
        api.acquire_dc(target)
    assert not state.get_dc
    api.destroy_target(71)


def test_probe_releases_its_dc_after_bad_pixel(inert_api) -> None:
    api, state, _callbacks = inert_api
    state.pixel_failure = True
    with pytest.raises(AssertionError, match="readback is unavailable"):
        api.read_statistics(witness._PIXEL_CASES["downscale"])
    assert state.get_dc == [71]
    assert state.release_dc == [(71, 501)]
    assert api.dc_acquisitions == api.dc_releases == 1
    api.destroy_target(71)
    assert api.owned_target == 0


def test_probe_releases_its_dc_after_flush_failure(inert_api) -> None:
    api, state, _callbacks = inert_api
    state.flush_failure = True
    with pytest.raises(AssertionError, match="flush failed"):
        api.read_statistics(witness._PIXEL_CASES["downscale"])
    assert state.pixel_calls == 0
    assert state.release_dc == [(71, 501)]
    api.destroy_target(71)


def test_probe_refuses_client_mismatch_before_dc_or_pixels(inert_api) -> None:
    api, state, _callbacks = inert_api
    with pytest.raises(AssertionError, match="geometry is invalid"):
        api.read_statistics(witness._PIXEL_CASES["same_size"])
    assert not state.get_dc and state.pixel_calls == 0
    api.destroy_target(71)


def test_probe_cannot_read_while_minimized_and_restore_is_nonfatal(inert_api) -> None:
    api, state, _callbacks = inert_api
    api.minimize_owned()
    assert state.minimized
    with pytest.raises(AssertionError, match="geometry is invalid"):
        api.read_statistics(witness._PIXEL_CASES["downscale"])
    assert not state.get_dc
    api.restore_owned()
    assert not state.minimized
    stats = api.read_statistics(witness._PIXEL_CASES["downscale"])
    assert stats["sampled_pixels"] == 128 * 72
    assert state.get_dc == [71] and api.dc_acquisitions == api.dc_releases == 1
    api.destroy_target(71)


def test_only_owned_dc_can_be_released_and_it_precedes_destroy(inert_api) -> None:
    api, state, _callbacks = inert_api
    dc = api.acquire_dc(71)
    with pytest.raises(AssertionError, match="unowned DC"):
        api.release_dc(71, 502)
    with pytest.raises(AssertionError, match="still owns a DC"):
        api.destroy_target(71)
    assert not state.destroy and not state.release_dc
    api.release_dc(71, dc)
    api.destroy_target(71)
    assert state.release_dc == [(71, 501)] and state.destroy == [71]
    with pytest.raises(AssertionError, match="unowned"):
        api.acquire_dc(71)


def test_pixel_probe_win32_abi_and_no_image_capture_surface(inert_api) -> None:
    _api, _state, calls = inert_api
    assert calls["GetPixel"].argtypes == [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    assert calls["GetPixel"].restype is ctypes.c_uint32
    assert calls["GdiFlush"].argtypes == []
    assert calls["GetDC"].argtypes == [ctypes.c_void_p]
    assert calls["GetDC"].restype is ctypes.c_void_p
    assert (
        not {"GetDesktopWindow", "GetWindowDC", "PrintWindow", "GetDIBits", "BitBlt"} & calls.keys()
    )


def test_scalar_pixel_summary_contains_no_payload_or_window_identity() -> None:
    cases = {}
    for name, geometry in witness._PIXEL_CASES.items():
        stats = witness._pixel_statistics(geometry, _reference_pixel(geometry))
        witness._assert_pixel_statistics(geometry, stats)
        cases[name] = stats
    text = json.dumps(cases)
    assert len(text.encode()) < 4096
    for forbidden in ("payload", "handle", "window_id", "pointer", "rtsp", "path", "screenshot"):
        assert forbidden not in text


def test_existing_hosted_windows_gate_executes_pixel_file_at_exact_head() -> None:
    workflow = Path(".github/workflows/current-gate-target-windows.yml").read_text()
    assert "runs-on: windows-latest" in workflow
    assert "ref: ${{ github.event.pull_request.head.sha || github.sha }}" in workflow
    assert (
        "tests/integration/test_current_gate_target_windows.py"
        in workflow.split("- name: Run exact gate regression", 1)[1]
    )


@pytest.mark.parametrize(
    "fault", ["four_dots", "filled_box", "left_gap", "right_gap", "top_gap", "bottom_gap"]
)
def test_full_outline_oracle_rejects_sparse_filled_or_truncated_box(fault: str) -> None:
    geometry = witness._PIXEL_CASES["downscale"]
    good = _reference_pixel(geometry)
    dots = {(101, 30), (122, 30), (112, 13), (112, 58)}

    def read(x: int, y: int) -> int:
        if fault == "four_dots":
            return witness._GREEN_COLORREF if (x, y) in dots else witness._GRAY_COLORREF
        if fault == "filled_box":
            return (
                witness._GREEN_COLORREF
                if 101 <= x <= 122 and 13 <= y <= 58
                else witness._GRAY_COLORREF
            )
        gap = {
            "left_gap": 102 <= x <= 105 and 30 <= y <= 35,
            "right_gap": 118 <= x <= 121 and 30 <= y <= 35,
            "top_gap": 109 <= x <= 114 and 14 <= y <= 17,
            "bottom_gap": 109 <= x <= 114 and 54 <= y <= 57,
        }[fault]
        return witness._GRAY_COLORREF if gap else good(x, y)

    stats = witness._pixel_statistics(geometry, read)
    assert stats["green_mismatches"] > 0 or stats["gray_mismatches"] > 0
    with pytest.raises(AssertionError):
        witness._assert_pixel_statistics(geometry, stats)
