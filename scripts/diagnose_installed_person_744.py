"""Observe the unmodified real installed person journey; never release evidence."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import platform
import time
from datetime import UTC, datetime
from pathlib import Path

import psutil
import pytest

HEAD = "744cf89e79c58d0946699ca7af1f03f4425634ca"
SETUP_SHA = "764b86dbf72625e216e864b1ce93a6f6af6d916496c5588d82d2ffd8fc0b598f"
SOURCES = {
    "tests/integration/test_windows_desktop_app.py": "fb04c9c44a52759a127d9388fdc5eda2b45900ed15acff7db3de2e7f2978003b",
    "tests/integration/test_operator_product_runtime.py": "dd8d5590be971ad0db99dbfcaf9bd0964251c84c8b891d0bfe7dfc953daa1a36",
    "src/k5vision/operator_events.py": "d5ff2af2b8ee77e14eb5b83784502b9b5812a4d4e6c8675cf8175ad4cceb522b",
    "src/k5vision/operator_events_decoder.py": "af8ec37345ae83094659c25b09d74a55929d13dccce121ef104ae93d7f4319a3",
    "src/k5vision/operator_analytics_person.py": "282c5453a52e7862c696c2c20545b16d7a12ff98d09d426300199d2598f59d35",
}
ALLOWED_ANALYTICS = {"disabled", "unavailable", "starting", "waiting", "active"}
ALLOWED_CAMERA = {"unavailable", "live", "disconnected", "connecting"}
ALLOWED_RECORDING = {"error", "paused", "waiting", "recording", "stalled"}
ALLOWED_REASONS = {
    "worker_unavailable", "operator_paused", "storage_paused", "camera_unavailable",
    "configuration_mismatch", "storage_inspection", "disk_progress",
    "no_disk_progress", "awaiting_disk_progress",
}
ALLOWED_STAGES = {
    "admitted_installed_model", "enable_continuous_person_gui", "positive_camera_recording",
    "event_recording_navigation", "complete",
}


def integer(value):
    return value if type(value) is int and 0 <= value <= 2**63 - 1 else None


def finite(value, minimum=0, maximum=86400):
    if type(value) not in (int, float) or not math.isfinite(value):
        return None
    return round(min(maximum, max(minimum, value)), 4)


def age(value):
    if type(value) is not str:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return finite((datetime.now(UTC) - parsed).total_seconds(), -86400)
    except (ValueError, OverflowError):
        return None


def error_note(error):
    """Only fixed categories and locations in the public acceptance source."""
    result = {"kind": "assertion" if isinstance(error, pytest.fail.Exception) else "exception"}
    current = error.__traceback__
    while current is not None:
        code = current.tb_frame.f_code
        if Path(code.co_filename).name == "test_windows_desktop_app.py":
            if code.co_name in {
                "_installed_analytics_journey", "positive_event", "_wait", "_require",
                "test_native_windows_click_install_account_browser_tray_restart_uninstall",
            }:
                result.update(function=code.co_name, line=min(10000, max(1, current.tb_lineno)))
        current = current.tb_next
    return result


class ClientObserver:
    """Forward actual events calls; append only finite public status observations."""

    def __init__(self, client, kwargs, result):
        self._client, self._kwargs, self._result = client, kwargs, result
        self._route = "/api/v1/operator/cameras/" + kwargs["camera_id"]
        self._started = time.monotonic()
        self._decoders = {}

    def __getattr__(self, name):
        return getattr(self._client, name)

    def json(self, route, *args, **kwargs):
        # Keep the exact original response and assertions. No model or detector is injected.
        value = self._client.json(route, *args, **kwargs)
        if route == self._route + "/events":
            try:
                self._observe(value)
            except BaseException:
                self._result["observer_failed"] = True
        return value

    def _native(self, observation):
        try:
            children = psutil.Process(self._kwargs["app"].pid).children(recursive=True)
            active = set()
            for process in children:
                try:
                    if "k5vision.operator_events_decoder" not in process.cmdline():
                        continue
                    identity = process.pid, process.create_time()
                    active.add(identity)
                    self._decoders.setdefault(identity, process)
                except (psutil.Error, OSError):
                    continue
            observation["decoder_current_count"] = min(32, len(active))
            observation["decoder_generations_seen"] = min(128, len(self._decoders))
            ended = []
            for identity, process in list(self._decoders.items())[:128]:
                if identity in active:
                    continue
                try:
                    exit_code = process.wait(timeout=0)
                    ended.append(exit_code if type(exit_code) is int else None)
                except (psutil.Error, OSError):
                    ended.append(None)
            observation["decoder_ended_exit_codes"] = ended
        except (psutil.Error, OSError):
            observation["decoder_observer_unavailable"] = True

    def _observe(self, actual_events):
        observations = self._result["observations"]
        if len(observations) >= 256:
            self._result["observations_capped"] = True
            return
        began = time.monotonic()
        stage = self._kwargs["diagnostic"].get("analytics_stage")
        observation = {
            "elapsed_seconds": finite(began - self._started),
            "stage": stage if stage in ALLOWED_STAGES else "other",
        }
        observations.append(observation)
        events = actual_events.get("events") if type(actual_events) is dict else None
        if type(events) is list:
            observation["events_returned"] = min(10000, len(events))
            # Report the actual positive condition, never event IDs, times or camera addresses.
            observation["actual_qualifying_person_event"] = any(
                type(event) is dict
                and event.get("camera_id") == self._kwargs["camera_id"]
                and type(event.get("person_count")) is int
                and 1 <= event["person_count"] <= 32
                and type(event.get("confidence")) in (int, float)
                and 0.5 <= event["confidence"] <= 1
                for event in events[:1]
            )
        transport_failure = getattr(self._client, "transport_failure", None)
        try:
            status = self._client.json(self._route + "/analytics")
            if type(status) is dict:
                observation["analytics"] = {
                    "status": status.get("status") if status.get("status") in ALLOWED_ANALYTICS else "unknown",
                    "enabled": status.get("enabled") is True,
                    "processed_frames": integer(status.get("processed_frames")),
                    "event_count": integer(status.get("event_count")),
                    "confidence_threshold": finite(status.get("confidence_threshold"), 0, 1),
                    "last_sample_age_seconds": age(status.get("last_sample_at")),
                    "last_event_age_seconds": age(status.get("last_event_at")),
                }
        except BaseException:
            observation["analytics_observer_unavailable"] = True
        try:
            response = self._client.json("/api/v1/operator/cameras")
            cameras = response.get("cameras") if type(response) is dict else None
            camera = next(
                (item for item in cameras if type(item) is dict and item.get("device_id") == self._kwargs["camera_id"]),
                None,
            ) if type(cameras) is list else None
            if camera is not None:
                recording = camera.get("recording_status")
                recording = recording if type(recording) is dict else {}
                observation["camera"] = {
                    "status": camera.get("status") if camera.get("status") in ALLOWED_CAMERA else "unknown",
                    "recording": camera.get("recording") is True,
                    "recording_enabled": camera.get("recording_enabled") is True,
                    "recording_state": camera.get("recording_state") if camera.get("recording_state") in ALLOWED_RECORDING else "unknown",
                    "reason": recording.get("reason") if recording.get("reason") in ALLOWED_REASONS else "unknown",
                    "observed_bytes": integer(recording.get("observed_bytes")),
                    "last_progress_age_seconds": finite(recording.get("last_progress_age_seconds")),
                }
            else:
                observation["camera_observer_unavailable"] = True
        except BaseException:
            observation["camera_observer_unavailable"] = True
        finally:
            self._client.transport_failure = transport_failure
        try:
            code = self._kwargs["publishers"][0].poll()
            observation["publisher_running"] = code is None
            observation["publisher_exit_code"] = code if type(code) is int else None
        except BaseException:
            observation["publisher_observer_unavailable"] = True
        self._native(observation)
        observation["observer_seconds"] = finite(time.monotonic() - began)


def run(output):
    started = time.monotonic()
    result = {
        "schema_version": 1, "source_head": HEAD, "platform": platform.system(),
        "diagnostic_only": True, "release_qualification": False, "status": "started",
        "installer_artifact_id": 13588981370, "installer_source_run_id": 42408392892,
        "installer_sha256": SETUP_SHA, "positive_event_timeout_seconds": 25,
        "observations": [],
    }
    try:
        if os.name != "nt" or os.getenv("K5_DESKTOP_HEAD_SHA") != HEAD:
            raise ValueError("admission")
        if os.getenv("K5_DESKTOP_SETUP_SHA256") != SETUP_SHA:
            raise ValueError("installer")
        root = Path(__file__).resolve().parents[1]
        for relative, expected in SOURCES.items():
            raw = (root / relative).read_bytes()
            canonical = raw.replace(b"\r\n", b"\n")
            if hashlib.sha256(canonical).hexdigest() != expected or raw not in (
                canonical, canonical.replace(b"\n", b"\r\n")
            ):
                raise ValueError("source")
        result["source_admitted"] = True
        path = root / "tests/integration/test_windows_desktop_app.py"
        spec = importlib.util.spec_from_file_location("installed_person_744_diagnostic", path)
        if spec is None or spec.loader is None:
            raise ValueError("module")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        original = module._installed_analytics_journey

        def journey(**kwargs):
            observed = dict(kwargs)
            observed["client"] = ClientObserver(kwargs["client"], kwargs, result)
            result["analytics_journey_started"] = True
            try:
                value = original(**observed)
                result["analytics_journey_completed"] = True
                return value
            except BaseException as error:
                result["analytics_error"] = error_note(error)
                stage = kwargs["diagnostic"].get("analytics_stage")
                result["analytics_stage"] = stage if stage in ALLOWED_STAGES else "other"
                raise

        module._installed_analytics_journey = journey

        def property_callback(name, value):
            if name == "desktop_windows_diagnostic":
                diagnostic = json.loads(value)
                result["cleanup_failed"] = diagnostic.get("cleanup_failed") is True
                result["outer_failed"] = diagnostic.get("failure_code") is not None
                result["main_analytics_stage_positive"] = diagnostic.get("analytics_stage") == "positive_camera_recording"
                result["main_failure_is_readiness_deadline"] = diagnostic.get("failure_detail") == "readiness_deadline"
                result["health_http_status"] = integer(diagnostic.get("health_http_status"))
                probe = diagnostic.get("desktop_probe")
                result["backend_ready"] = type(probe) is dict and probe.get("ready") is True
            elif name == "desktop_windows_receipt":
                result["whole_installed_journey_completed"] = True

        # Direct call retains every original assertion, real GUI and 360-second bound.
        # It avoids scheduled pytest traceback dumps, which have disturbed native SDKs.
        module.test_native_windows_click_install_account_browser_tray_restart_uninstall(property_callback)
        result["status"] = "success"
    except BaseException as error:
        result["status"] = "failed"
        result["outer_error"] = error_note(error)
    finally:
        result["duration_seconds"] = round(time.monotonic() - started, 3)
        with Path(output).open("x", encoding="utf-8") as stream:
            json.dump(result, stream, sort_keys=True, indent=2, allow_nan=False)
        print(json.dumps(result, sort_keys=True, allow_nan=False), flush=True)
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    raise SystemExit(run(parser.parse_args().output))
