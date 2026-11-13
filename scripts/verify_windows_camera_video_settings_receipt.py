"""Admit installer-bound Windows ONVIF video controls and automatic wall evidence."""

from __future__ import annotations

import argparse
import json
import math
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path

_TEST = "test_native_windows_camera_video_settings_and_automatic_wall"
_PROPERTY = "camera_video_windows_receipt"
_TRUE = {
    "native_windows_execution",
    "generated_media_only",
    "actual_gui_installer",
    "camera_video_settings",
    "actual_gui_account_setup",
    "embedded_runtime_independent",
    "actual_edge_ui",
    "real_wsse_authenticated",
    "real_encoder_options",
    "real_encoder_set",
    "configuration_readback_verified",
    "delivered_geometry_verified",
    "decoded_pts_frame_rate_verified",
    "prechange_utc_export_bound",
    "actual_stream_changed",
    "unsupported_codec_h264_restore",
    "shared_configuration_ack",
    "unsupported_rtsp_readonly",
    "nonadmin_permission_denied",
    "invalid_codec_rejected",
    "invalid_camera_response_bounded",
    "other_camera_live_continues",
    "other_camera_recording_grows",
    "target_recording_resumes",
    "retained_utc_history",
    "retained_files_preserved",
    "retained_export_decoded",
    "automatic_wall_layout",
    "manual_wall_override",
    "credentials_cleared",
    "public_source_material_absent",
    "no_console_windows",
    "owned_cleanup",
    "uninstall_preserves_profile",
}
_FALSE = {
    "physical_camera_acceptance",
    "capacity_acceptance",
    "endurance_acceptance",
    "motion_recording",
    "camera_trigger_zones",
    "other_codecs_qualified",
    "mocked_gui",
    "user_terminal_required",
}
_FIELDS = {
    "schema_version",
    "head_sha",
    "installer_sha256",
    "platform",
    "browser_channel",
    "browser_version",
    "playwright_version",
    "duration_seconds",
    "writable_video_codecs",
    "changed_fields",
    "streams",
    "auto_camera_counts",
    "auto_layout_sizes",
    "auto_live_player_counts",
    "manual_layout_sizes",
    "manual_live_player_counts",
    "other_live_frames_before",
    "other_live_frames_after",
    "other_recording_bytes_before",
    "other_recording_bytes_after",
    "target_recording_bytes_before",
    "target_recording_bytes_after",
    "retained_file_count",
    "retained_playback_frames",
    "export_decoded_frames",
    "export_bytes",
    "export_sha256",
    "soap_requests",
    "permission_statuses",
    "server_stream_verified",
    "server_observed_fps",
    "retained_clip_start_utc",
    "first_encoder_apply_utc",
}
_HASH = re.compile(r"[a-f0-9]{64}\Z")


def _require(value):
    if not value:
        raise ValueError("Installed Windows camera-video evidence refused")


def _integer(value, minimum, maximum=2**63 - 1):
    _require(type(value) is int and minimum <= value <= maximum)
    return value


def _unique(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def _stream(value, *, codec, width, height, fps):
    _require(type(value) is dict and set(value) == {"codec", "width", "height", "fps", "frames"})
    _require(value["codec"] == codec)
    _require(_integer(value["width"], 1, 4096) == width)
    _require(_integer(value["height"], 1, 4096) == height)
    rate = value["fps"]
    _require(type(rate) in (int, float) and math.isfinite(rate) and abs(rate - fps) < 0.01)
    _integer(value["frames"], 4, 100000)


def verify(report: Path, *, expected_head: str, installer_sha256: str) -> dict:
    _require(re.fullmatch(r"[a-f0-9]{40}", expected_head) is not None)
    _require(_HASH.fullmatch(installer_sha256) is not None)
    _require(0 < report.stat().st_size <= 1024**2)
    root = ET.fromstring(report.read_bytes())
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    _require(len(suites) == 1)
    _require(
        all(
            suites[0].get(k) == v
            for k, v in {"tests": "1", "failures": "0", "errors": "0", "skipped": "0"}.items()
        )
    )
    cases = root.findall(".//testcase")
    _require(len(cases) == 1 and cases[0].get("name") == _TEST)
    _require(not any(root.findall(".//" + name) for name in ("failure", "error", "skipped")))
    properties = root.findall(".//property[@name='" + _PROPERTY + "']")
    _require(len(properties) == 1)
    receipt = json.loads(
        properties[0].get("value", ""), object_pairs_hook=_unique, parse_constant=lambda _: None
    )
    _require(type(receipt) is dict and set(receipt) == _TRUE | _FALSE | _FIELDS)
    _require(type(receipt["schema_version"]) is int and receipt["schema_version"] == 1)
    _require(
        receipt["head_sha"] == expected_head and receipt["installer_sha256"] == installer_sha256
    )
    _require(all(receipt[k] is True for k in _TRUE) and all(receipt[k] is False for k in _FALSE))
    _require(receipt["platform"] == "Windows" and receipt["browser_channel"] == "msedge")
    _require(receipt["playwright_version"] == "1.58.0")
    _require(
        type(receipt["browser_version"]) is str
        and re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", receipt["browser_version"]) is not None
    )
    duration = receipt["duration_seconds"]
    _require(type(duration) in (int, float) and math.isfinite(duration) and 0 < duration < 360)
    for key in ("retained_clip_start_utc", "first_encoder_apply_utc"):
        _require(
            type(receipt[key]) is str and len(receipt[key]) <= 32 and receipt[key].endswith("Z")
        )
    try:
        chosen = datetime.fromisoformat(receipt["retained_clip_start_utc"].replace("Z", "+00:00"))
        applied = datetime.fromisoformat(receipt["first_encoder_apply_utc"].replace("Z", "+00:00"))
        _require(chosen + timedelta(seconds=2) < applied)
    except (TypeError, ValueError):
        raise ValueError("Installed Windows camera-video UTC evidence refused") from None
    _require(receipt["writable_video_codecs"] == ["H264"])
    _require(
        receipt["changed_fields"]
        == ["bitrate_kbps", "codec", "frame_rate", "gop", "quality", "resolution"]
    )
    streams = receipt["streams"]
    _require(
        type(streams) is dict and set(streams) == {"before", "changed", "external", "restored"}
    )
    _stream(streams["before"], codec="h264", width=320, height=240, fps=15)
    _stream(streams["changed"], codec="h264", width=640, height=360, fps=10)
    _stream(streams["external"], codec="mpeg4", width=640, height=360, fps=10)
    _stream(streams["restored"], codec="h264", width=640, height=360, fps=10)
    for name, expected in {
        "auto_camera_counts": [1, 2, 5],
        "auto_layout_sizes": [1, 4, 9],
        "auto_live_player_counts": [1, 2, 5],
        "manual_layout_sizes": [1, 4, 9],
        "manual_live_player_counts": [1, 4, 5],
    }.items():
        value = receipt[name]
        _require(type(value) is list and all(type(v) is int for v in value) and value == expected)
    before = _integer(receipt["other_live_frames_before"], 4, 100000)
    _require(_integer(receipt["other_live_frames_after"], 4, 100000) >= before + 4)
    for prefix in ("other_recording_bytes", "target_recording_bytes"):
        _require(_integer(receipt[prefix + "_after"], 1) > _integer(receipt[prefix + "_before"], 0))
    for field, minimum, maximum in (
        ("retained_file_count", 1, 10000),
        ("retained_playback_frames", 4, 100000),
        ("export_decoded_frames", 1, 100000),
        ("export_bytes", 1, 16 * 1024**2),
    ):
        _integer(receipt[field], minimum, maximum)
    _require(
        type(receipt["export_sha256"]) is str
        and _HASH.fullmatch(receipt["export_sha256"]) is not None
    )
    soap = receipt["soap_requests"]
    _require(
        type(soap) is dict
        and set(soap)
        == {
            "get_configuration_requests",
            "get_options_requests",
            "set_configuration_requests",
            "force_persistence_verified",
            "nonedited_fields_preserved",
            "written_codecs",
        }
    )
    _integer(soap["get_configuration_requests"], 6, 128)
    _integer(soap["get_options_requests"], 3, 128)
    count = _integer(soap["set_configuration_requests"], 3, 8)
    _require(soap["written_codecs"] == ["H264"] * count)
    _require(
        soap["force_persistence_verified"] is True and soap["nonedited_fields_preserved"] is True
    )
    _require(
        receipt["permission_statuses"]
        == {
            "operator_get": 403,
            "operator_read": 403,
            "operator_patch": 403,
            "invalid_codec": 422,
            "shared_without_ack": 409,
            "malformed_camera": 502,
            "rtsp_read": 200,
        }
    )
    _require(all(type(value) is int for value in receipt["permission_statuses"].values()))
    _require(
        type(receipt["server_stream_verified"]) is bool and receipt["server_observed_fps"] is None
    )
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--expected-head", required=True)
    parser.add_argument("--installer-sha256", required=True)
    args = parser.parse_args()
    receipt = verify(
        args.report, expected_head=args.expected_head, installer_sha256=args.installer_sha256
    )
    args.output.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "head_sha": args.expected_head,
                "installer_sha256": args.installer_sha256,
                "lane": "camera_video_windows",
                "receipt": receipt,
            },
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print("Installed Windows camera-video controls and automatic wall evidence admitted")


if __name__ == "__main__":
    main()
