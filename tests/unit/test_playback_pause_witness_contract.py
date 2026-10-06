"""Portable oracle checks with inert native APIs. These never establish Win32 acceptance."""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
from pathlib import Path

import pytest

_PATH = Path(__file__).parents[1] / "integration" / "test_current_gate_playback_pause_windows.py"
_SPEC = importlib.util.spec_from_file_location("generated_pause_witness_contract", _PATH)
assert _SPEC is not None and _SPEC.loader is not None
witness = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(witness)


class _InertSurfaceApi:
    def __init__(self):
        self.open = False
        self.copies = self.blits = self.destroyed = 0

    def create_surface(self, width, height):
        assert not self.open and (width, height) == (4, 4)
        self.open = True
        return 101, 102, width * 4

    def copy_frame(self, pointer, stride, frame):
        assert self.open and pointer == 102 and stride == 16
        assert frame.source_elapsed_ms in (0, 40, 80, 120, 160)
        self.copies += 1

    def blit_surface(self, handle, width, height, dc, target_width=None, target_height=None):
        assert self.open and (handle, width, height, dc) == (101, 4, 4, 202)
        assert (target_width, target_height) == (64, 64)
        self.blits += 1

    def destroy_surface(self, handle):
        assert self.open and handle == 101
        self.open = False
        self.destroyed += 1


class _InertTargetApi:
    def __init__(self):
        self.open = False
        self.acquired = self.released = self.destroyed = 0

    def create_target(self, x, y, width, height):
        assert not self.open and (x, y, width, height) == (17, 29, 64, 64)
        self.open = True
        return 201

    def client_size(self, target):
        assert self.open and target == 201
        return 64, 64

    def acquire_dc(self, target):
        assert self.open and target == 201
        self.acquired += 1
        return 202

    def release_dc(self, target, dc):
        assert self.open and (target, dc) == (201, 202)
        self.released += 1

    def destroy_target(self, target):
        assert self.open and target == 201
        self.open = False
        self.destroyed += 1


_FACTORIES = (_InertSurfaceApi, _InertTargetApi)


def _run(tmp_path, *, factories=_FACTORIES, seconds=0.02):
    return asyncio.run(
        witness.run_generated_pause_witness(
            tmp_path, native_factories=factories, pause_seconds=seconds
        )
    )


def test_source_contract_exercises_real_delivery_controls_and_releases_native_boundaries(tmp_path):
    surfaces, targets = [], []

    def surface():
        api = _InertSurfaceApi()
        surfaces.append(api)
        return api

    def target():
        api = _InertTargetApi()
        targets.append(api)
        return api

    receipt = _run(tmp_path, factories=(surface, target))
    assert receipt["evidence_scope"] == "source-contract/fake-native-boundary"
    assert receipt["generated_decode"] and not receipt["native_presentation"]
    assert not receipt["codec_acceptance"] and not receipt["browser_ui_acceptance"]
    assert (
        not receipt["installed_application_acceptance"] and not receipt["pixel_readback_acceptance"]
    )
    assert not receipt["exceeds_default_packet_deadline"]
    assert len(surfaces) == len(targets) == 3
    assert [api.copies for api in surfaces] == [5, 1, 4]
    assert [api.blits for api in surfaces] == [5, 1, 4]
    assert [api.acquired for api in targets] == [5, 1, 4]
    assert all(api.acquired == api.released for api in targets)
    assert all(not api.open and api.destroyed == 1 for api in surfaces + targets)
    assert [(s["pause_count"], s["resume_count"]) for s in receipt["scenarios"]] == [
        (3, 3),
        (1, 0),
        (1, 0),
    ]
    assert not list(tmp_path.rglob("*.k5r")) and not list(tmp_path.rglob("*.k5d"))
    serialized = json.dumps(receipt)
    for forbidden in ("192.0.2.40", "rtsp://", "generated-pause-packet", str(tmp_path)):
        assert forbidden not in serialized


def test_generated_source_contract_survives_actual_default_packet_deadline(tmp_path):
    receipt = _run(tmp_path, seconds=8.05)
    assert receipt["exceeds_default_packet_deadline"]
    assert not receipt["native_presentation"]
    complete = receipt["scenarios"][0]
    assert complete["pause_windows"][0]["observed_ns"] > 8_000_000_000
    assert complete["paused_total_ms"] >= 8000


def test_missing_frame_admission_fails_witness_instead_of_false_success(tmp_path, monkeypatch):
    async def no_admission(_self):
        pass

    monkeypatch.setattr(
        witness.PausablePresentationPlaybackDelivery, "_wait_for_frame_admission", no_admission
    )
    with pytest.raises(AssertionError, match="playback stopped"):
        _run(tmp_path)
    assert not list(tmp_path.rglob("*.k5r"))


@pytest.mark.parametrize("missing", ["pause", "resume"])
def test_missing_acknowledged_control_is_rejected(tmp_path, monkeypatch, missing):
    original = witness.BoundedOperatorPlaybackCoordinator.control

    async def ignore(self, principal, control_id, action):
        if action.value == missing:
            opposite = (
                witness.OperatorPlaybackControlAction.RESUME
                if missing == "pause"
                else witness.OperatorPlaybackControlAction.PAUSE
            )
            return await original(self, principal, control_id, opposite)
        return await original(self, principal, control_id, action)

    monkeypatch.setattr(witness.BoundedOperatorPlaybackCoordinator, "control", ignore)
    with pytest.raises(AssertionError):
        _run(tmp_path)


def test_native_mode_refuses_short_pause_before_any_native_or_file_work(tmp_path):
    with pytest.raises(ValueError, match="eight-second"):
        _run(tmp_path, factories=None, seconds=0.01)
    assert list(tmp_path.iterdir()) == []


def test_native_mode_requires_win32_even_if_source_contract_can_run(tmp_path, monkeypatch):
    monkeypatch.setattr(witness.sys, "platform", "linux")
    with pytest.raises(RuntimeError, match="requires Windows"):
        _run(tmp_path, factories=None, seconds=8.1)


def test_stale_product_revision_cannot_be_silently_qualified(monkeypatch):
    original = witness.subprocess.run

    def wrong_head_blob(args, **kwargs):
        result = original(args, **kwargs)
        if "show" in args and args[-1].endswith("src/k5vision/media/playback_control.py"):
            result.stdout = b"different committed source bytes\n"
        return result

    monkeypatch.setattr(witness.subprocess, "run", wrong_head_blob)
    # Source-only mode reports the mismatch; it cannot turn it into native evidence.
    assert not witness._source_identity(require_clean=False)["head_matches_bound_files"]
    with pytest.raises(AssertionError, match="HEAD source binding failed"):
        witness._source_identity(require_clean=True)


def test_identity_uses_real_checkout_head_not_ci_merge_sha(monkeypatch):
    monkeypatch.setenv("GITHUB_SHA", "0" * 40)
    identity = witness._source_identity(require_clean=False)
    assert identity["checkout_head"] != "0" * 40
    assert identity["product_baseline_tree"] == "4d69121516d6420a0d0563ca7d13d6c52d944557"
    assert len(identity["bound_file_lf_sha256"]) == len(witness._BOUND_MODULES) + 3


def test_native_receipt_refuses_dirty_bound_files(monkeypatch):
    original = witness.subprocess.run

    def dirty(args, **kwargs):
        result = original(args, **kwargs)
        if "status" in args:
            result.stdout = b" M src/k5vision/media/playback_control.py\n"
        return result

    monkeypatch.setattr(witness.subprocess, "run", dirty)
    with pytest.raises(AssertionError, match="HEAD source binding failed|committed, unchanged"):
        witness._source_identity(require_clean=True)


def test_cleanup_failure_cannot_produce_success_receipt(tmp_path):
    class BrokenClose(_InertSurfaceApi):
        def destroy_surface(self, handle):
            super().destroy_surface(handle)
            raise RuntimeError("generated cleanup failure")

    with pytest.raises(witness.OperatorPlaybackError):
        _run(tmp_path, factories=(BrokenClose, _InertTargetApi))
    assert not list(tmp_path.rglob("*.k5r"))


def test_trace_oracle_rejects_paused_presentation_and_nonmonotonic_timestamps():
    trace = [
        {"event": "frame_started", "elapsed_ns": 1, "source_elapsed_ms": 0},
        {"event": "presented", "elapsed_ns": 2, "source_elapsed_ms": 0},
        {"event": "pause_ack", "elapsed_ns": 3},
        {"event": "resume_ack", "elapsed_ns": 5},
    ]
    witness._assert_trace(trace, [0])
    changed = copy.deepcopy(trace)
    changed.insert(3, {"event": "presented", "elapsed_ns": 4, "source_elapsed_ms": 40})
    with pytest.raises(AssertionError, match="acknowledged pause"):
        witness._assert_trace(changed, [0, 40])
    trace[-1]["elapsed_ns"] = 0
    with pytest.raises(AssertionError):
        witness._assert_trace(trace, [0])


def test_failed_fixture_setup_closes_registry_and_removes_partial_packets(tmp_path, monkeypatch):
    registries = []
    original_close = witness.DeviceRegistry.close

    def close(registry):
        registries.append(registry)
        original_close(registry)

    async def broken_write(_sink, _packet):
        raise RuntimeError("generated setup failure")

    monkeypatch.setattr(witness.DeviceRegistry, "close", close)
    monkeypatch.setattr(witness.FramedAtomicRecordingSink, "write", broken_write)
    with pytest.raises(RuntimeError, match="generated setup failure"):
        _run(tmp_path)
    assert len(registries) == 1
    assert list((tmp_path / "complete" / "recordings").iterdir()) == []
