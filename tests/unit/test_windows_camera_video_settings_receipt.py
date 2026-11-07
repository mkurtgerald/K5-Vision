"""Fail closed when camera controls lack real installer, media or isolation proof."""

from __future__ import annotations

import importlib.util
import json
import runpy
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
