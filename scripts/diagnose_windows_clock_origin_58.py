"""Diagnose the original clock/live failure; never qualify or alter installed product behavior."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import itertools
import json
import math
import os
import platform
import time
from pathlib import Path

import pytest

HEAD = "58a097370627afa152a36288bc8e0db20ad209a7"
SOURCE_HASHES = {
    "tests/integration/test_windows_desktop_app.py": (
        "cbb561dc1728bdd3e37dd63c735498b9cf8bdaf2496f06aa9dfe33c72fdb393d"
    ),
    "tests/integration/windows_vm_clock.py": (
        "a43fb41cc3ad80ea84040c22691d20703906f884d6b586b6ccd3c86110ea10a5"
    ),
    "tests/integration/test_operator_product_runtime.py": (
        "dd8d5590be971ad0db99dbfcaf9bd0964251c84c8b891d0bfe7dfc953daa1a36"
    ),
    "tests/integration/test_operator_beta_browser.py": (
        "28f823ebfa9d20e6b976fdbcd4ce1d62e54718e6a932a3309afa4b0f08e92c18"
    ),
    "tests/integration/test_operator_admin_browser.py": (
        "060b2083c77c8910a39497a2c2d2c724a348546453f2b9afbe4ba96c6bc6e6dd"
    ),
    "tests/integration/installed_settings_acceptance.py": (
        "029b24408a3bb7d5b21442ec56498ffceba041747915a210ed159a6bcfde799c"
    ),
    "tests/integration/test_mediamtx_donor_runtime.py": (
        "5f43886681713f6bd8931ad5b961139bb0a23d25897a5b999014b844220a51d0"
    ),
    "tests/integration/onvif_camera_fixture.py": (
        "ec739ca0d75cebd6cf9cc13647ed96f125e63c315e3e066f223d0d02456c61f5"
    ),
    "src/k5vision/operator_product.py": (
        "aa72511b7be58e8578b658189c2f8ce6aae2d72c798cdd9130a642ba6908abaa"
    ),
    "src/k5vision/operator_product_api.py": (
        "f365efa690c2c702bc10c566a8f5d0497f2ed5e02b0d9f1b67dfd6fbf4c9c818"
    ),
    "src/k5vision/operator_product_service.py": (
        "7c93f77582785dce0920d6cd23b62557cac4c1bd915529a0ef55e25e771673d0"
    ),
    "src/k5vision/operator_media_gateway.py": (
        "62e242893a42e806211a2c30c8ccda2bfe7824b128cf1f128472ef766944a103"
    ),
    "src/k5vision/operator_time.py": (
        "4af0ca454122ca1d2fadb090d38e17beaa1add56c8a3d093a99206d1d7a8df4d"
    ),
    "src/k5vision/auth_sessions.py": (
        "92a0a0ce6fa8dee07808a6c9db60e6cea7d5eaec7ea91f445a89ee266d8eb5c3"
    ),
    "src/k5vision/operator_events.py": (
        "a9bc8b36dbf75ac6acedffd85b92692fd526858ef7c48843d85b2d49c8b9ed3d"
    ),
    "src/k5vision/operator_analytics_person.py": (
        "282c5453a52e7862c696c2c20545b16d7a12ff98d09d426300199d2598f59d35"
    ),
    "src/k5vision/media/mediamtx_provider.py": (
        "b111351b637a8836b49b25f09ce180fa2f160cb29f42867d2efe1e6cfd1b89ef"
    ),
    "scripts/provision_operator_person_acceptance.py": (
        "ae517af633380a47b9853fc36740ae04e84b99c0d1195029988144f44b01ed05"
    ),
    "windows/desktop/donors.json": (
        "0c36bc3369c422ea3e4dc69f530b2d8d99a323732feab9c95862878bfa160899"
    ),
}
PHASES = {"forward_live", "backward_live"}
WORKERS = {"ready", "unavailable", "disk_full"}
CAMERA_STATES = {"live", "connecting", "disconnected", "unavailable"}
RECORDING_STATES = {"waiting", "recording", "paused", "stalled", "error"}
REASONS = {
    "worker_unavailable",
    "operator_paused",
    "storage_paused",
    "camera_unavailable",
    "configuration_mismatch",
    "storage_inspection",
    "disk_progress",
    "no_disk_progress",
    "awaiting_disk_progress",
}


def count(value):
    return value if type(value) is int and 0 <= value <= 2**63 - 1 else None


def finite_seconds(value):
    if type(value) not in (int, float):
        return None
    try:
        return round(min(86400, max(0, value)), 3) if math.isfinite(value) else None
    except OverflowError:
        return None


def enum(value, allowed):
    return value if type(value) is str and value in allowed else "unknown"


def admit_sources(root):
    for relative, wanted in SOURCE_HASHES.items():
        raw = (root / relative).read_bytes()
        canonical = raw.replace(b"\r\n", b"\n")
        if hashlib.sha256(canonical).hexdigest() != wanted or raw not in (
            canonical,
            canonical.replace(b"\n", b"\r\n"),
        ):
            raise ValueError("source admission")


def response_note(response):
    value = response.status
    note = {"status": value if type(value) is int and 100 <= value <= 599 else None}
    try:
        header = response.headers.get("content-type", "")
        mime = header.split(";", 1)[0].strip().lower() if type(header) is str else ""
        note["content_type"] = {
            "": "missing",
            "text/plain": "text_plain",
            "application/json": "application_json",
            "text/html": "text_html",
        }.get(mime, "other")
        body = response.body()
        if type(body) is not bytes:
            raise ValueError("body admission")
        note["body_bytes"] = min(2**32 - 1, len(body))
        note["body_class"] = (
            "media_access_denied"
            if body == b"Media access denied"
            else "empty"
            if not body
            else "other"
        )
    except BaseException:
        note["body_probe_failed"] = True
    return note


def public_json(client, route, label, result):
    status, body, _ = client.get(route, max_bytes=32768)
    result[label + "_http_status"] = (
        status if type(status) is int and 100 <= status <= 599 else None
    )
    if type(body) is not bytes or len(body) > 32768:
        raise ValueError("public response admission")
    value = json.loads(body)
    if status != 200:
        detail = value.get("detail") if type(value) is dict else None
        if type(detail) is dict:
            detail = detail.get("code")
        codes = {
            "Operator identity unavailable": "identity_unavailable",
            "Camera state unavailable": "camera_state_unavailable",
            "roles_unavailable": "roles_unavailable",
            "Media unavailable": "media_unavailable",
        }
        result[label + "_error_code"] = (
            codes.get(detail, "other") if type(detail) is str else "other"
        )
        raise ValueError("public status admission")
    return value


def after_response_snapshot(client, camera_id, cohort, *, received_at=None):
    """These are later observations, not an atomic snapshot of the failed response."""
    result = {"timing": "post_response"}
    began = time.monotonic() if received_at is None else received_at

    def elapsed_ms():
        return min(90000, max(0, round((time.monotonic() - began) * 1000)))

    transport_failure = getattr(client, "transport_failure", None)
    try:
        with cohort.lock:
            owned = tuple(itertools.islice(cohort.processes.values(), 64))
            truncated = len(cohort.processes) > len(owned)
        result["owned_identity_sample_count"] = len(owned)
        result["owned_identity_sample_truncated"] = truncated
        names = {
            "k5vision.exe": "desktop",
            "python.exe": "python",
            "pythonw.exe": "python",
            "mediamtx.exe": "mediamtx",
            "caddy.exe": "caddy",
        }
        counts = dict.fromkeys(["desktop", "python", "mediamtx", "caddy", "other"], 0)
        for process in owned:
            try:
                if process.is_running():
                    counts[names.get(process.name().casefold(), "other")] += 1
            except BaseException:
                continue
        result["owned_process_counts"] = counts
    except BaseException:
        result["cohort_probe_failed"] = True
    result["owned_process_observed_after_response_ms"] = elapsed_ms()
    try:
        try:
            status = public_json(client, "/api/v1/operator/status", "status", result)
            if type(status) is not dict:
                raise ValueError("status admission")
            storage = status.get("storage")
            clock = status.get("clock")
            result["worker_status"] = enum(status.get("worker_status"), WORKERS)
            if type(storage) is dict:
                result["storage"] = {
                    "reason": enum(
                        storage.get("reason"), {"ready", "free_space", "capacity", "inspection"}
                    ),
                    "recording_allowed": storage.get("recording_allowed") is True,
                    "recording_bytes": count(storage.get("recording_bytes")),
                }
            if type(clock) is dict:
                result["clock"] = {
                    "status": enum(clock.get("status"), {"observing", "changed", "unavailable"}),
                    "change_count": count(clock.get("change_count")),
                }
        except BaseException:
            result["status_probe_failed"] = True
        result["status_observed_after_response_ms"] = elapsed_ms()
        try:
            response = public_json(client, "/api/v1/operator/cameras", "cameras", result)
            cameras = response.get("cameras") if type(response) is dict else None
            if type(cameras) is not list or len(cameras) > 64:
                raise ValueError("camera admission")
            states = {key: 0 for key in sorted(CAMERA_STATES | {"unknown"})}
            for camera in cameras:
                if type(camera) is dict:
                    states[enum(camera.get("status"), CAMERA_STATES)] += 1
            result["camera_status_counts"] = states
            target = next(
                (
                    item
                    for item in cameras
                    if type(item) is dict and item.get("device_id") == camera_id
                ),
                None,
            )
            if target is not None:
                recording = target.get("recording_status")
                result["target"] = {
                    "status": enum(target.get("status"), CAMERA_STATES),
                    "recording": target.get("recording") is True,
                    "recording_enabled": target.get("recording_enabled") is True,
                    "recording_state": enum(target.get("recording_state"), RECORDING_STATES),
                }
                if type(recording) is dict:
                    result["target"].update(
                        reason=enum(recording.get("reason"), REASONS),
                        observed_bytes=count(recording.get("observed_bytes")),
                        last_progress_age_seconds=finite_seconds(
                            recording.get("last_progress_age_seconds")
                        ),
                    )
        except BaseException:
            result["camera_probe_failed"] = True
        result["cameras_observed_after_response_ms"] = elapsed_ms()
    finally:
        client.transport_failure = transport_failure
    return result


def analytics_failure_frames(error, source_root):
    locations = {
        (source_root / "tests/integration/test_windows_desktop_app.py").resolve(): (
            "desktop",
            {"_installed_analytics_journey", "_installed_person_fixture", "_wait"},
        ),
        (source_root / "tests/integration/test_operator_beta_browser.py").resolve(): (
            "browser",
            {
                "_open_camera_recordings",
                "_open_camera_watch",
                "_decoded_color",
                "_decoded_video",
                "_decoded_video_proof",
            },
        ),
    }
    frames = []
    current = error.__traceback__
    while current is not None:
        code = current.tb_frame.f_code
        location = locations.get(Path(code.co_filename).resolve())
        if location is not None and code.co_name in location[1]:
            frames.append(
                {
                    "file": location[0],
                    "function": code.co_name,
                    "line": min(10000, max(1, current.tb_lineno)),
                }
            )
            frames = frames[-4:]
        current = current.tb_next
    return frames


def analytics_ui_state(page):
    raw = page.wait_for_function(
        """() => {
      const visible = id => {
        const node = document.getElementById(id);
        return Boolean(node && !node.hidden && node.getClientRects().length);
      };
      const result = {
        history_visible: visible('history-panel'), live_visible: visible('live-panel'),
        wall_visible: visible('wall-panel'),
        event_button_count:
          document.querySelectorAll('#analytics-events button[data-event-id]').length,
        history_item_count: document.querySelectorAll('.history-item').length
      };
      for (const name of ['playback', 'live']) {
        const video = document.getElementById(name + '-video');
        result[name + '_present'] = Boolean(video);
        if (!video) continue;
        result[name + '_ready_state'] = video.readyState;
        result[name + '_paused'] = video.paused;
        result[name + '_time_seconds'] = Number.isFinite(video.currentTime)
          ? video.currentTime : null;
        result[name + '_duration_seconds'] = Number.isFinite(video.duration)
          ? video.duration : null;
        result[name + '_width'] = video.videoWidth;
        result[name + '_height'] = video.videoHeight;
        result[name + '_decoded_frames'] = video.getVideoPlaybackQuality().totalVideoFrames;
        result[name + '_error_code'] = video.error ? video.error.code : null;
        result[name + '_has_source'] = Boolean(video.getAttribute('src') || video.currentSrc);
      }
      const historyEnd = document.getElementById('history-end');
      const parsedEnd = historyEnd ? Date.parse(historyEnd.value + 'Z') : NaN;
      result.history_end_age_seconds = Number.isFinite(parsedEnd)
        ? (Date.now() - parsedEnd) / 1000 : null;
      const playback = document.getElementById('playback-video');
      result.playback_utc_start_present = Boolean(playback && playback.dataset.utcStart);
      return result;
    }""",
        timeout=2000,
    ).json_value()
    if type(raw) is not dict:
        return {"available": False}
    result = {"available": True}
    age = raw.get("history_end_age_seconds")
    try:
        result["history_end_age_seconds"] = (
            round(min(86400, max(-86400, age)), 3)
            if type(age) in (int, float) and math.isfinite(age)
            else None
        )
    except OverflowError:
        result["history_end_age_seconds"] = None
    bools = {"history_visible", "live_visible", "wall_visible", "playback_utc_start_present"}
    bools.update(
        name + suffix
        for name in ("playback", "live")
        for suffix in ("_present", "_paused", "_has_source")
    )
    for key in bools:
        result[key] = raw.get(key) if type(raw.get(key)) is bool else None
    for key in ("event_button_count", "history_item_count"):
        value = raw.get(key)
        result[key] = value if type(value) is int and 0 <= value <= 256 else None
    for name in ("playback", "live"):
        for suffix, maximum in (
            ("_ready_state", 4),
            ("_error_code", 4),
            ("_width", 16384),
            ("_height", 16384),
            ("_decoded_frames", 2**32 - 1),
        ):
            key, value = name + suffix, raw.get(name + suffix)
            result[key] = value if type(value) is int and 0 <= value <= maximum else None
        for suffix in ("_time_seconds", "_duration_seconds"):
            result[name + suffix] = finite_seconds(raw.get(name + suffix))
    return result


def observe_analytics(original, result, source_root):
    def observed(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        except BaseException as error:
            began = time.monotonic()
            note = {
                "timing": "after_analytics_exception",
                "kind": "timeout"
                if type(error).__name__ == "TimeoutError"
                else ("assertion" if isinstance(error, pytest.fail.Exception) else "exception"),
            }
            result["analytics_failure"] = note
            try:
                note["frames"] = analytics_failure_frames(error, source_root)
            except BaseException:
                note["frame_probe_failed"] = True
            try:
                note["ui"] = analytics_ui_state(kwargs.get("page"))
            except BaseException:
                note["ui_probe_failed"] = True
            note["ui_observed_after_exception_ms"] = min(
                90000, max(0, round((time.monotonic() - began) * 1000))
            )
            raise

    return observed


class RequestObserver:
    def __init__(self, request, diagnostic, client, camera_id, captured, result):
        self.request, self.diagnostic = request, diagnostic
        self.client, self.camera_id, self.captured, self.result = (
            client,
            camera_id,
            captured,
            result,
        )

    def get(self, *args, **kwargs):
        response = self.request.get(*args, **kwargs)
        phase = self.diagnostic.get("vm_clock_stage")
        if phase in PHASES and response.status != 200 and "failure_response" not in self.result:
            began = time.monotonic()
            try:
                note = response_note(response)
                note["phase"] = phase
                self.result["failure_response"] = note
                later = after_response_snapshot(
                    self.client, self.camera_id, self.captured.get("cohort"), received_at=began
                )
                later["elapsed_after_response_ms"] = min(
                    90000, max(0, round((time.monotonic() - began) * 1000))
                )
                self.result["post_response_observation"] = later
            except BaseException:
                self.result["failure_observation_failed"] = True
        return response

    def __getattr__(self, name):
        return getattr(self.request, name)


class ContextObserver:
    def __init__(self, context, diagnostic, client, camera_id, captured, result):
        self.context = context
        self.request = RequestObserver(
            context.request, diagnostic, client, camera_id, captured, result
        )

    def __getattr__(self, name):
        return getattr(self.context, name)


def run(output):
    started = time.monotonic()
    result = {
        "schema_version": 1,
        "diagnostic_only": True,
        "release_qualification": False,
        "source_head": HEAD,
        "installer_artifact_id": 13591480449,
        "installer_source_run_id": 42414363684,
        "platform": platform.system(),
        "status": "started",
    }
    try:
        if (
            os.name != "nt"
            or os.getenv("K5_DESKTOP_HEAD_SHA") != HEAD
            or os.getenv("K5_VM_CLOCK_ACCEPTANCE") != "1"
            or os.getenv("GITHUB_ACTIONS") != "true"
            or os.getenv("RUNNER_ENVIRONMENT") != "github-hosted"
        ):
            raise ValueError("environment admission")
        sha = os.getenv("K5_DESKTOP_SETUP_SHA256", "")
        if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            raise ValueError("installer admission")
        result["installer_sha256"] = sha
        root = Path(__file__).resolve().parents[1]
        admit_sources(root)
        result["source_hash_guard"] = True
        path = root / "tests/integration/test_windows_desktop_app.py"
        spec = importlib.util.spec_from_file_location("installed_clock_origin_58", path)
        if spec is None or spec.loader is None:
            raise ValueError("module admission")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        captured = {}
        original_cohort, original_phase = module._Cohort, module._vm_clock_phase

        class ObservedCohort(original_cohort):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                captured["cohort"] = self

        def phase(**kwargs):
            kwargs["context"] = ContextObserver(
                kwargs["context"],
                kwargs["diagnostic"],
                kwargs["client"],
                kwargs["camera_id"],
                captured,
                result,
            )
            return original_phase(**kwargs)

        module._Cohort, module._vm_clock_phase = ObservedCohort, phase
        module._installed_analytics_journey = observe_analytics(
            module._installed_analytics_journey, result, root
        )

        def property_callback(name, value):
            if name == "desktop_windows_diagnostic":
                data = json.loads(value)
                guard = data.get("vm_clock_guard", {})
                result["clock_restored"] = guard.get("clock_restored") is True
                result["services_restored"] = guard.get("services_restored") is True
                result["cleanup_failed"] = data.get("cleanup_failed") is True
                result["clock_gate_reached"] = data.get("stage") == "actual_vm_clock_changes"
                result["clock_gate_completed"] = data.get("vm_clock_stage") == "complete"

        module.test_native_windows_click_install_account_browser_tray_restart_uninstall(
            property_callback
        )
        result["status"] = "success"
    except BaseException as error:
        result["status"] = "failed"
        result["error_kind"] = (
            "assertion"
            if isinstance(error, pytest.fail.Exception)
            else ("timeout" if type(error).__name__ == "TimeoutError" else "exception")
        )
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
