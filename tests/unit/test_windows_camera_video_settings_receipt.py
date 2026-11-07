"""Fail closed when camera controls lack real installer, media or isolation proof."""

from __future__ import annotations

import importlib.util
import json
import runpy
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPT = Path(__file__).parents[2] / "scripts/verify_windows_camera_video_settings_receipt.py"
_VERIFIER = runpy.run_path(str(_SCRIPT))
_HEAD, _SETUP = "a" * 40, "b" * 64


def _receipt():
    def stream(codec, width, height, fps):
        return {"codec": codec, "width": width, "height": height, "fps": fps, "frames": 10}

    return {
        **dict.fromkeys(_VERIFIER["_TRUE"], True),
        **dict.fromkeys(_VERIFIER["_FALSE"], False),
        "schema_version": 1,
        "head_sha": _HEAD,
        "installer_sha256": _SETUP,
        "platform": "Windows",
        "browser_channel": "msedge",
        "browser_version": "142.0.1.1",
        "playwright_version": "1.58.0",
        "duration_seconds": 120.5,
        "retained_clip_start_utc": "2026-11-02T12:00:00Z",
        "first_encoder_apply_utc": "2026-11-02T12:00:10Z",
        "writable_video_codecs": ["H264"],
        "changed_fields": ["bitrate_kbps", "codec", "frame_rate", "gop", "quality", "resolution"],
        "streams": {
            "before": stream("h264", 320, 240, 15),
            "changed": stream("h264", 640, 360, 10),
            "external": stream("mpeg4", 640, 360, 10),
            "restored": stream("h264", 640, 360, 10),
        },
        "auto_camera_counts": [1, 2, 5],
        "auto_layout_sizes": [1, 4, 9],
        "auto_live_player_counts": [1, 2, 5],
        "manual_layout_sizes": [1, 4, 9],
        "manual_live_player_counts": [1, 4, 5],
        "other_live_frames_before": 10,
        "other_live_frames_after": 20,
        "other_recording_bytes_before": 100,
        "other_recording_bytes_after": 200,
        "target_recording_bytes_before": 100,
        "target_recording_bytes_after": 200,
        "retained_file_count": 2,
        "retained_playback_frames": 5,
        "export_decoded_frames": 10,
        "export_bytes": 4096,
        "export_sha256": "c" * 64,
        "soap_requests": {
            "get_configuration_requests": 9,
            "get_options_requests": 4,
            "set_configuration_requests": 3,
            "force_persistence_verified": True,
            "nonedited_fields_preserved": True,
            "written_codecs": ["H264"] * 3,
        },
        "permission_statuses": {
            "operator_get": 403,
            "operator_read": 403,
            "operator_patch": 403,
            "invalid_codec": 422,
            "shared_without_ack": 409,
            "malformed_camera": 502,
            "rtsp_read": 200,
        },
        "server_stream_verified": True,
        "server_observed_fps": None,
    }


def _report(tmp_path, receipt):
    root = ET.Element("testsuites")
    suite = ET.SubElement(root, "testsuite", tests="1", failures="0", errors="0", skipped="0")
    case = ET.SubElement(suite, "testcase", name=_VERIFIER["_TEST"])
    properties = ET.SubElement(case, "properties")
    ET.SubElement(properties, "property", name=_VERIFIER["_PROPERTY"], value=json.dumps(receipt))
    path = tmp_path / "camera-video.xml"
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
    return path


def _verify(path):
    return _VERIFIER["verify"](path, expected_head=_HEAD, installer_sha256=_SETUP)


def test_camera_video_receipt_binds_actual_controls_and_decode_to_exact_installer(tmp_path):
    receipt = _receipt()
    assert _verify(_report(tmp_path, receipt)) == receipt


@pytest.mark.parametrize(
    "field", sorted(_VERIFIER["_TRUE"] | _VERIFIER["_FALSE"] | _VERIFIER["_FIELDS"])
)
def test_camera_video_receipt_rejects_missing_evidence(tmp_path, field):
    receipt = _receipt()
    receipt.pop(field)
    with pytest.raises(ValueError):
        _verify(_report(tmp_path, receipt))


@pytest.mark.parametrize("field", sorted(_VERIFIER["_TRUE"] | _VERIFIER["_FALSE"]))
def test_camera_video_receipt_rejects_integer_truth_values(tmp_path, field):
    receipt = _receipt()
    receipt[field] = int(receipt[field])
    with pytest.raises(ValueError):
        _verify(_report(tmp_path, receipt))


@pytest.mark.parametrize(
    "mutation",
    [
        {"head_sha": "d" * 40},
        {"installer_sha256": "d" * 64},
        {"writable_video_codecs": ["H264", "H265"]},
        {"server_observed_fps": 10},
        {"other_live_frames_after": 13},
        {"other_recording_bytes_after": 100},
        {"target_recording_bytes_after": 100},
        {"auto_layout_sizes": [1, 4, 4]},
        {"auto_live_player_counts": [1, 2, 0]},
        {"manual_live_player_counts": [1, 5, 5]},
        {"retained_playback_frames": 0},
        {"export_decoded_frames": 0},
        {"duration_seconds": 360},
        {"duration_seconds": True},
        {"unreviewed_secret": "refused"},
    ],
)
def test_camera_video_receipt_rejects_placebo_or_inconsistent_outcomes(tmp_path, mutation):
    receipt = _receipt()
    receipt.update(mutation)
    with pytest.raises(ValueError):
        _verify(_report(tmp_path, receipt))


@pytest.mark.parametrize(
    "phase,field,value",
    [
        ("changed", "width", 320),
        ("changed", "fps", 15),
        ("restored", "codec", "mpeg4"),
        ("restored", "frames", 0),
    ],
)
def test_acknowledgement_cannot_replace_delivered_encoder_changes(tmp_path, phase, field, value):
    receipt = _receipt()
    receipt["streams"][phase][field] = value
    with pytest.raises(ValueError):
        _verify(_report(tmp_path, receipt))


@pytest.mark.parametrize("outcome", ["failure", "error", "skipped"])
def test_failed_or_skipped_native_camera_journey_cannot_qualify(tmp_path, outcome):
    path = _report(tmp_path, _receipt())
    tree = ET.parse(path)
    ET.SubElement(tree.find(".//testcase"), outcome)
    tree.write(path, encoding="utf-8", xml_declaration=True)
    with pytest.raises(ValueError):
        _verify(path)


@pytest.mark.parametrize("metadata_rate", ["15/1", "99/1"])
@pytest.mark.parametrize("leading_untimed", [False, True])
def test_delivered_fps_comes_from_decoded_frame_timestamps_not_declared_rate(
    monkeypatch, metadata_rate, leading_untimed
):
    path = Path(__file__).parents[1] / "integration/test_windows_camera_video_settings.py"
    spec = importlib.util.spec_from_file_location("camera_pts_probe", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    payload = {
        "streams": [
            {"codec_name": "h264", "width": 640, "height": 360, "r_frame_rate": metadata_rate}
        ],
        "frames": [
            {"best_effort_timestamp_time": str(i / 10), "width": 640, "height": 360}
            for i in range(5)
        ],
    }
    if leading_untimed:
        payload["frames"].insert(0, {"width": 640, "height": 360})
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(payload).encode()),
    )
    actual = module._probe(Path("generated-ffprobe"), "generated-only-input", network=False)
    assert actual == {
        "codec": "h264",
        "width": 640,
        "height": 360,
        "fps": 10.0,
        "frames": 6 if leading_untimed else 5,
    }


@pytest.fixture
def probe_module():
    path = Path(__file__).parents[1] / "integration/test_windows_camera_video_settings.py"
    spec = importlib.util.spec_from_file_location("camera_probe_diagnostics", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _probe_payload():
    return {
        "streams": [{"codec_name": "h264", "width": 640, "height": 360}],
        "frames": [
            {"best_effort_timestamp_time": str(i / 10), "width": 640, "height": 360}
            for i in range(5)
        ],
    }


@pytest.mark.parametrize(
    "case,reason",
    [
        ("nonzero", "nonzero_exit"),
        ("oversized", "oversized_output"),
        ("invalid", "invalid_json"),
        ("streams", "stream_count"),
        ("frames", "frame_count"),
        ("untimed", "untimed_frames"),
        ("untimed_middle", "untimed_frames"),
        ("few_timed", "frame_count"),
        ("nonmonotonic", "nonmonotonic_pts"),
        ("geometry", "geometry_mismatch"),
    ],
)
def test_probe_diagnostics_identify_rejection_without_publishing_input(
    probe_module, monkeypatch, case, reason
):
    payload = _probe_payload()
    if case == "streams":
        payload["streams"] = []
    elif case == "frames":
        payload["frames"] = payload["frames"][:3]
    elif case == "untimed":
        payload["frames"][:0] = [{"width": 640, "height": 360}] * 3
    elif case == "untimed_middle":
        payload["frames"][1].pop("best_effort_timestamp_time")
    elif case == "few_timed":
        payload["frames"] = payload["frames"][:4]
        payload["frames"][0].pop("best_effort_timestamp_time")
    elif case == "nonmonotonic":
        payload["frames"][1]["best_effort_timestamp_time"] = "0"
    elif case == "geometry":
        payload["frames"][1]["width"] = 320
    raw = (
        b"private-camera-text"
        if case == "invalid"
        else b"x" * 262145
        if case == "oversized"
        else json.dumps(payload).encode()
    )

    def run(command, **kwargs):
        assert kwargs["timeout"] == 8
        assert kwargs["stderr"] == subprocess.DEVNULL
        assert command[-1] == "rtsp://private-user:private-password@private-camera/stream"
        return SimpleNamespace(returncode=3221225501 if case == "nonzero" else 0, stdout=raw)

    monkeypatch.setattr(probe_module.subprocess, "run", run)
    diagnostic = {"probe_fps": 999, "probe_width": 999}
    actual = probe_module._probe(
        Path("generated-ffprobe"),
        "rtsp://private-user:private-password@private-camera/stream",
        diagnostic=diagnostic,
    )
    assert actual is None and diagnostic["probe_reason"] == reason
    assert diagnostic["probe_attempts"] == 1
    assert 0 <= diagnostic["probe_elapsed_ms"] <= 900000
    assert "probe_fps" not in diagnostic and "probe_width" not in diagnostic
    assert "private" not in json.dumps(diagnostic)
    assert "rtsp://" not in json.dumps(diagnostic)


def test_probe_timeout_records_only_bounded_numeric_progress(probe_module, monkeypatch):
    def timeout(command, **kwargs):
        assert kwargs["timeout"] == 8
        raise subprocess.TimeoutExpired(command, 8, output=b"private partial JSON")

    monkeypatch.setattr(probe_module.subprocess, "run", timeout)
    diagnostic = {}
    for _ in range(2):
        with pytest.raises(subprocess.TimeoutExpired):
            probe_module._probe(
                Path("generated-ffprobe"), "rtsp://private-camera/stream", diagnostic=diagnostic
            )
    assert diagnostic["probe_reason"] == "timeout"
    assert diagnostic["probe_attempts"] == diagnostic["probe_timeouts"] == 2
    assert diagnostic["probe_output_bytes"] == len(b"private partial JSON")
    assert "private" not in json.dumps(diagnostic)


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"fps": 15.0}, "rate_mismatch"),
        ({"width": 320}, "expected_geometry_mismatch"),
        ({"codec": "mpeg4"}, "expected_codec_mismatch"),
    ],
)
def test_stream_mismatch_keeps_exact_cadence_and_geometry_gate(
    probe_module, monkeypatch, change, reason
):
    payload = _probe_payload()
    monkeypatch.setattr(
        probe_module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(payload).encode()),
    )

    def wait(condition, _label, *, timeout, process):
        assert timeout == 20 and process is None
        assert condition() is False
        raise TimeoutError("controlled readiness deadline")

    expected = {"codec": "h264", "width": 640, "height": 360, "fps": 10.0, **change}
    diagnostic = {}
    with pytest.raises(TimeoutError, match="controlled readiness"):
        probe_module._stream_proof(
            Path("generated-ffprobe"),
            "rtsp://private-camera/stream",
            expected,
            SimpleNamespace(_wait=wait),
            diagnostic=diagnostic,
            phase="initial",
        )
    assert diagnostic["probe_reason"] == reason
    assert diagnostic["probe_phase"] == "initial"
    assert diagnostic["probe_fps"] == 10.0
    assert diagnostic["probe_width"] == 640 and diagnostic["probe_height"] == 360
    assert diagnostic["probe_frame_count"] == diagnostic["probe_timed_frames"] == 5
    assert diagnostic["probe_expected_fps"] == expected["fps"]
    assert "private" not in json.dumps(diagnostic)


def test_probe_notes_do_not_publish_nonfinite_or_string_values(probe_module):
    diagnostic = {}
    probe_module._probe_note(
        diagnostic,
        "private invalid reason",
        probe_module.time.monotonic(),
        probe_fps=float("nan"),
        probe_width="private-camera",
        probe_height=float("inf"),
        private_raw_json="refused",
    )
    assert diagnostic["probe_reason"] == "invalid_json"
    assert set(diagnostic) == {"probe_reason", "probe_elapsed_ms"}
    assert "private" not in json.dumps(diagnostic)


@pytest.mark.parametrize("network", [True, False])
def test_probe_bounds_network_analysis_and_preserves_file_export_command(
    probe_module, monkeypatch, network
):
    payload = _probe_payload()
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        assert kwargs["timeout"] == 8
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload).encode())

    monkeypatch.setattr(probe_module.subprocess, "run", run)
    result = probe_module._probe(Path("generated-ffprobe"), "generated-only-input", network=network)
    assert result["fps"] == 10.0
    network_options = (
        [
            "-rtsp_transport",
            "tcp",
            "-analyzeduration",
            "1000000",
            "-probesize",
            "1048576",
            "-read_intervals",
            "%+1",
        ]
        if network
        else []
    )
    assert calls == [
        [
            "generated-ffprobe",
            "-v",
            "error",
            *network_options,
            "-select_streams",
            "v:0",
            "-show_frames",
            "-show_entries",
            "stream=codec_name,width,height:frame=best_effort_timestamp_time,width,height",
            "-of",
            "json",
            "generated-only-input",
        ]
    ]


@pytest.mark.parametrize("leading_untimed", [0, 1, 2])
def test_network_probe_still_rejects_only_three_timed_frames(
    probe_module, monkeypatch, leading_untimed
):
    payload = _probe_payload()
    payload["frames"] = [{"width": 640, "height": 360}] * leading_untimed + payload["frames"][:3]
    monkeypatch.setattr(
        probe_module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(payload).encode()),
    )
    diagnostic = {}
    result = probe_module._probe(
        Path("generated-ffprobe"), "generated-only-input", diagnostic=diagnostic
    )
    assert result is None
    assert diagnostic["probe_reason"] == "frame_count"
    if leading_untimed:
        assert diagnostic["probe_timed_frames"] == 3
