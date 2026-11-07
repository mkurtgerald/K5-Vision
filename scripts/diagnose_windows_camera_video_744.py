"""Bounded Windows diagnosis of the exact 744 native camera journey; never release evidence."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import time
from pathlib import Path

import pytest

HEAD = "744cf89e79c58d0946699ca7af1f03f4425634ca"
CAMERA_HASH = "58c596da27f1f076c6d057e05e44eeffc63c136eeee5471e3f496b4dc6cad02d"
PROJECT_FUNCTIONS = frozenset(
    {
        "run_camera_video_journey",
        "_rtsp_enroll",
        "test_connection",
        "_open_camera_watch",
        "_open_camera_recordings",
        "test_native_windows_camera_video_settings_and_automatic_wall",
    }
)
SELECTORS = {
    "#wall-tab": "wall_open",
    "[data-wall-action='watch']": "wall_watch",
    "#update-source-button": "reconnect_open",
    "#new-camera-name": "reconnect_name",
    "#camera-connection-type": "rtsp_mode",
    "#new-camera-stream-uri": "rtsp_address",
    "#new-camera-username": "rtsp_username",
    "#new-camera-password": "rtsp_password",
    "#test-camera-connection": "rtsp_probe",
    "#camera-profile": "rtsp_profile",
    "#save-camera": "rtsp_connect",
    "#add-camera-dialog": "rtsp_dialog",
    "#camera-video-button": "readonly_open",
    "#camera-video-values": "readonly_fields",
    "#camera-video-apply": "readonly_apply",
    "#camera-video-message": "readonly_message",
    "#camera-video-cancel": "readonly_close",
}
ACTIONS = frozenset(
    {
        "click",
        "fill",
        "select_option",
        "wait_for",
        "input_value",
        "is_hidden",
        "is_disabled",
        "inner_text",
        "get_attribute",
    }
)
FILES = {
    "test_windows_camera_video_settings.py": "camera",
    "test_operator_beta_browser.py": "browser",
}


def error_note(error):
    """Report only fixed categories and public source locations, never text or locals."""
    kind = (
        "timeout"
        if type(error).__name__ == "TimeoutError"
        else ("assertion" if isinstance(error, pytest.fail.Exception) else "exception")
    )
    result = {"kind": kind}
    current = error.__traceback__
    while current is not None:
        code = current.tb_frame.f_code
        source = FILES.get(Path(code.co_filename).name)
        if source and code.co_name in PROJECT_FUNCTIONS:
            result.update(
                file=source, function=code.co_name, line=min(10000, max(1, current.tb_lineno))
            )
        current = current.tb_next
    return result


class LocatorObserver:
    """Forward real Locator calls; record only a fixed selector/action label."""

    def __init__(self, locator, selector, owner):
        self._locator, self._selector, self._owner = locator, selector, owner

    def __getattr__(self, name):
        attribute = getattr(self._locator, name)
        if name == "locator":

            def locate(selector, *args, **kwargs):
                return LocatorObserver(attribute(selector, *args, **kwargs), selector, self._owner)

            return locate
        if name in ACTIONS:

            def act(*args, **kwargs):
                label = SELECTORS.get(self._selector)
                if label:
                    self._owner.note(label + "_" + name)
                return attribute(*args, **kwargs)

            return act
        return attribute


class PageObserver:
    """Transparent page delegation, active only during the direct RTSP witness."""

    def __init__(self, page, diagnostic, result):
        self._page, self._diagnostic, self._result = page, diagnostic, result

    def note(self, stage):
        if self._diagnostic.get("stage") == "unsupported_direct_rtsp":
            self._result["substage"] = stage

    def locator(self, selector, *args, **kwargs):
        return LocatorObserver(self._page.locator(selector, *args, **kwargs), selector, self)

    def wait_for_function(self, expression, *args, **kwargs):
        if isinstance(expression, str):
            if "camera-video-form" in expression:
                self.note("readonly_busy_wait")
            elif "new-camera-stream-uri" in expression:
                self.note("rtsp_credentials_clear_wait")
        return self._page.wait_for_function(expression, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._page, name)


def run(output):
    started = time.monotonic()
    result = {
        "schema_version": 1,
        "source_head": HEAD,
        "diagnostic_only": True,
        "release_qualification": False,
        "platform": platform.system(),
        "status": "started",
    }
    try:
        if os.name != "nt" or os.getenv("K5_DESKTOP_HEAD_SHA") != HEAD:
            raise ValueError("admission")
        installer_sha = os.getenv("K5_DESKTOP_SETUP_SHA256", "")
        if len(installer_sha) != 64 or any(c not in "0123456789abcdef" for c in installer_sha):
            raise ValueError("installer")
        result.update(
            installer_sha256=installer_sha,
            installer_artifact_id=13588981370,
            installer_source_run_id=42408392892,
        )
        path = (
            Path(__file__).resolve().parents[1]
            / "tests/integration/test_windows_camera_video_settings.py"
        )
        raw_source = path.read_bytes()
        canonical_source = raw_source.replace(b"\r\n", b"\n")
        if hashlib.sha256(canonical_source).hexdigest() != CAMERA_HASH or raw_source not in (
            canonical_source,
            canonical_source.replace(b"\n", b"\r\n"),
        ):
            raise ValueError("source")
        spec = importlib.util.spec_from_file_location("camera_video_744_diagnostic", path)
        if spec is None or spec.loader is None:
            raise ValueError("module")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        original = module.run_camera_video_journey

        def journey(page, **kwargs):
            diagnostic = kwargs.get("diagnostic", {})
            observed = PageObserver(page, diagnostic, result)
            try:
                value = original(observed, **kwargs)
                result["journey_completed"] = True
                return value
            except BaseException as error:
                result["original_error"] = error_note(error)
                stage = diagnostic.get("stage")
                if stage != "unsupported_direct_rtsp":
                    result.pop("substage", None)
                if stage in {
                    "unsupported_direct_rtsp",
                    "retained_utc_playback_export",
                    "automatic_five_camera_wall",
                    "independent_recording_progress",
                    "shared_encoder_acknowledgement",
                    "initial_encoder_stream",
                    "changed_encoder_stream",
                    "restored_encoder_stream",
                    "external_encoder_stream",
                    "encoder_options",
                    "encoder_apply",
                    "unsupported_external_codec",
                    "operator_observer",
                }:
                    result["stage"] = stage
                raise

        module.run_camera_video_journey = journey

        def property_callback(name, value):
            if name == "camera_video_windows_diagnostic":
                diagnostic = json.loads(value)
                result["cleanup_failed"] = diagnostic.get("cleanup_failed") is True
                result["outer_failed"] = diagnostic.get("failed") is True
                result["probe_attempts"] = diagnostic.get("probe_attempts")
                result["probe_valid"] = diagnostic.get("probe_reason") == "valid"
            elif name == "camera_video_windows_receipt":
                result["native_test_completed"] = True

        module.test_native_windows_camera_video_settings_and_automatic_wall(property_callback)
        result["status"] = "success"
    except BaseException as error:
        result["status"] = "failed"
        result["outer_error"] = error_note(error)
    finally:
        result["duration_seconds"] = round(time.monotonic() - started, 3)
        destination = Path(output)
        with destination.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, sort_keys=True, indent=2, allow_nan=False)
        print(json.dumps(result, sort_keys=True, allow_nan=False), flush=True)
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    raise SystemExit(run(parser.parse_args().output))
