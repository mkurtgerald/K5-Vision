"""Passive transport evidence around the original installed NVR journey.

The product checkout, original assertions, requests and deadlines are unchanged.
Only failure emits observations. No URL, credential, body, pixel or raw decoder
message is written. Finished playback response bytes are hashed in memory only.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import math
import os
import re
import subprocess
from collections import OrderedDict, deque
from functools import wraps
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

SOURCE_HEAD = "b81a950975e50979f57b20fb13cdbfbe73fd1aac"
SOURCE_TREE = "41a09ec229b848cc7594453fb61ba5f8e9bd16d9"
TEST_FILE = "tests/integration/test_windows_desktop_app.py"
TEST_SHA256 = "e32632d81211987fccfb9b5412fcaa2759fe08ff0ad8db99b1dfba907efbf1ad"
TEST_NAME = "test_native_windows_click_install_account_browser_tray_restart_uninstall"
OUTPUT = "combined-transport-b81a950-observation.json"
BODY_LIMIT = 16 * 1024 * 1024
MAX_REQUESTS = 512
MAX_EVENTS = 1024
BROWSER_SCRIPT = '(cameraIds) => {\n  "use strict";\n  if (window.__K5PassiveTransportWitness) return;\n  const indices = new Map(cameraIds.slice(0, 4).map((id, index) => [id, index]));\n  const tracked = new WeakMap(), listeners = new Map(), events = [];\n  const currentVideos = () => Array.from(document.querySelectorAll("#wall-panel video,#live-video,#playback-video")).slice(0, 64);\n  const kinds = ["emptied", "waiting", "playing", "error", "stalled", "pause", "ended", "loadedmetadata"];\n  const messageClasses = ["DEMUXER_ERROR_COULD_NOT_OPEN", "DEMUXER_ERROR_NO_SUPPORTED_STREAMS",\n    "PIPELINE_ERROR_DECODE", "DECODER_ERROR_NOT_SUPPORTED", "PIPELINE_ERROR_NETWORK"];\n  let serial = 0, dropped = 0, faults = 0, stopped = false;\n  const finite = (value, maximum = 1e12) => typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= maximum ? value : null;\n  const guard = (action) => (...args) => {\n    try { return action(...args); }\n    catch (_) { faults = Math.min(1000, faults + 1); return null; }\n  };\n  const category = (video) => video.id === "playback-video" ? "playback"\n    : video.id === "live-video" ? "focused_live"\n      : video.closest("#wall-panel") ? "wall" : null;\n  const state = (video) => {\n    const scope = category(video);\n    if (!scope) return null;\n    const quality = video.getVideoPlaybackQuality();\n    const frames = finite(quality.totalVideoFrames);\n    const before = tracked.get(video);\n    const owner = before || { element: ++serial, frames: null };\n    const id = video.closest("[data-camera-id]")?.dataset.cameraId;\n    const row = {\n      element: owner.element, scope, camera: indices.has(id) ? indices.get(id) : null,\n      milliseconds: Math.round(performance.now() * 1000) / 1000,\n      frames, previous_frames: owner.frames,\n      counter_reset: frames !== null && owner.frames !== null && frames < owner.frames,\n      connected: video.isConnected, paused: video.paused,\n      ready_state: video.readyState, network_state: video.networkState,\n      error_code: Number.isInteger(video.error?.code) && video.error.code >= 1 && video.error.code <= 4 ? video.error.code : null,\n      error_message_class: video.error ? messageClasses.find(value =>\n        typeof video.error.message === "string" && video.error.message.slice(0, 4096).includes(value)) || "other" : null,\n      current_time: finite(video.currentTime), width: finite(video.videoWidth, 100000),\n      height: finite(video.videoHeight, 100000),\n    };\n    owner.frames = frames;\n    tracked.set(video, owner);\n    return row;\n  };\n  const record = (event, row) => {\n    if (!row) return;\n    if (events.length === 128) { events.shift(); dropped += 1; }\n    events.push({ event, ...row });\n  };\n  const sample = guard(() => {\n    if (stopped) return;\n    for (const video of currentVideos()) {\n      const row = state(video);\n      if (row?.counter_reset) record("counter_reset", row);\n    }\n  });\n  const attach = guard((video) => {\n    if (listeners.has(video) || listeners.size >= 64 || !category(video)) return;\n    const handlers = kinds.map((kind) => {\n      const handler = guard(() => { if (!stopped) record(kind, state(video)); });\n      video.addEventListener(kind, handler);\n      return [kind, handler];\n    });\n    listeners.set(video, handlers);\n    record("observed", state(video));\n  });\n  const refresh = guard(() => {\n    if (stopped) return;\n    for (const [video, handlers] of listeners) {\n      if (!video.isConnected) {\n        for (const [kind, handler] of handlers) video.removeEventListener(kind, handler);\n        listeners.delete(video);\n      }\n    }\n    for (const video of currentVideos()) attach(video);\n  });\n  const mutation = new MutationObserver(refresh);\n  mutation.observe(document.documentElement, { childList: true, subtree: true });\n  let resource = null;\n  if (typeof PerformanceObserver === "function") {\n    resource = new PerformanceObserver(sample);\n    try { resource.observe({ entryTypes: ["resource"] }); }\n    catch (_) { faults = Math.min(1000, faults + 1); }\n  }\n  refresh();\n  Object.defineProperty(window, "__K5PassiveTransportWitness", {\n    value: Object.freeze({\n      sample,\n      snapshot: async () => {\n        refresh(); sample();\n        const videos = [];\n        for (const video of currentVideos()) {\n          const row = guard(state)(video);\n          if (!row) continue;\n          let error_message_sha256 = null;\n          if (video.error && typeof video.error.message === "string" && video.error.message.length <= 4096) {\n            try {\n              const raw = new TextEncoder().encode(video.error.message);\n              const hashed = await crypto.subtle.digest("SHA-256", raw);\n              error_message_sha256 = Array.from(new Uint8Array(hashed), value => value.toString(16).padStart(2, "0")).join("");\n            } catch (_) { faults = Math.min(1000, faults + 1); }\n          }\n          videos.push({ ...row, error_message_sha256 });\n        }\n        return { available: true, scope: "passive-browser-transport-failure-only", qualification: false,\n          faults, dropped, observed_elements: serial, events: events.slice(), videos };\n      },\n      stop: guard(() => {\n        stopped = true; mutation.disconnect(); resource?.disconnect();\n        for (const [video, handlers] of listeners) {\n          for (const [kind, handler] of handlers) video.removeEventListener(kind, handler);\n        }\n        listeners.clear();\n      }),\n    }),\n  });\n}\n'  # noqa: E501 - frozen standalone browser witness
MIMES = frozenset(
    {
        "video/mp4",
        "video/mp2t",
        "application/mp4",
        "application/vnd.apple.mpegurl",
        "application/x-mpegurl",
        "audio/mpegurl",
        "text/plain",
        "application/json",
        "application/octet-stream",
        "application/javascript",
    }
)
NET_ERRORS = frozenset(
    {
        "net::ERR_ABORTED",
        "net::ERR_TIMED_OUT",
        "net::ERR_CONNECTION_RESET",
        "net::ERR_CONNECTION_CLOSED",
        "net::ERR_CONNECTION_REFUSED",
        "net::ERR_EMPTY_RESPONSE",
        "net::ERR_FAILED",
        "net::ERR_INSUFFICIENT_RESOURCES",
        "net::ERR_CONTENT_LENGTH_MISMATCH",
        "net::ERR_HTTP_RESPONSE_CODE_FAILURE",
    }
)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def finite(value, maximum=1e12):
    return (
        value
        if type(value) in {int, float} and math.isfinite(value) and 0 <= value <= maximum
        else None
    )


def mime(value):
    candidate = value.split(";", 1)[0].strip().lower() if isinstance(value, str) else ""
    return candidate if candidate in MIMES else "other"


def content_length(value):
    return (
        int(value)
        if isinstance(value, str)
        and re.fullmatch(r"[0-9]{1,10}", value)
        and int(value) <= 256 * 1024**2
        else None
    )


def mp4_projection(raw):
    """Describe structure only; no atom payload, text or pixel bytes survive."""
    result = {
        "bytes": len(raw),
        "sha256": digest(raw),
        "ftyp": False,
        "moov": False,
        "moof": False,
        "mdat": False,
        "truncated_atom": False,
    }
    offset, atoms = 0, 0
    while offset < len(raw) and atoms < 256:
        if len(raw) - offset < 8:
            result["truncated_atom"] = True
            break
        size = int.from_bytes(raw[offset : offset + 4], "big")
        kind = bytes(raw[offset + 4 : offset + 8])
        header = 8
        if size == 1:
            if len(raw) - offset < 16:
                result["truncated_atom"] = True
                break
            size = int.from_bytes(raw[offset + 8 : offset + 16], "big")
            header = 16
        elif size == 0:
            size = len(raw) - offset
        if size < header or size > len(raw) - offset:
            result["truncated_atom"] = True
            break
        if kind in {b"ftyp", b"moov", b"moof", b"mdat"}:
            result[kind.decode("ascii")] = True
        offset += size
        atoms += 1
    result["atom_limit"] = offset < len(raw) and atoms == 256
    return result


def observation_only(function):
    @wraps(function)
    def guarded(self, *args, **kwargs):
        try:
            return function(self, *args, **kwargs)
        except BaseException:  # noqa: BLE001 - observer faults cannot alter original outcomes.
            self.faults += 1
            return None

    return guarded


def bounded_send(session, method, params, seconds=1):
    # Playwright 1.58.0 is pinned by the original acceptance lane. Its sync
    # bridge lets cache-only failure observations have a finite transport wait.
    return session._sync(asyncio.wait_for(session._impl_obj.send(method, params), seconds))


def bounded_session(page, seconds=1):
    from playwright.sync_api._generated import mapping

    context = page.context
    implementation = context._sync(
        asyncio.wait_for(context._impl_obj.new_cdp_session(page=page), seconds)
    )
    return mapping.from_impl(implementation)


def bounded_detach(session, seconds=1):
    return session._sync(asyncio.wait_for(session._impl_obj.detach(), seconds))


class TransportWitness:
    def __init__(self, page, camera_ids, camera_directories):
        self.page = page
        self.faults = 0
        self.requests = OrderedDict()
        self.events = deque(maxlen=MAX_EVENTS)
        self.finished_playback = deque(maxlen=2)
        self.connections = {}
        self.serial = 0
        self.started = None
        self.session = None
        self.handlers = []
        self.camera_ids = {str(value): index for index, value in enumerate(camera_ids[:4])}
        self.media_paths = {
            Path(value).name: index for index, value in enumerate(camera_directories[:4])
        }
        self.attach()

    def classify(self, url):
        parsed = urlsplit(url)
        path, query = parsed.path, parse_qs(parsed.query)
        pieces = path.split("/")
        if path.startswith("/media/live/") and len(pieces) == 5:
            name = pieces[-1]
            kind = (
                "hls_playlist"
                if name.endswith(".m3u8")
                else "hls_part"
                if "part" in name
                else "hls_segment"
            )
            return {
                "category": kind,
                "camera": self.media_paths.get(pieces[-2]),
                "blocking_reload": "_HLS_msn" in query or "_HLS_part" in query,
            }
        if path == "/media/playback":
            values = query.get("path", [])
            return {
                "category": "playback",
                "camera": self.media_paths.get(values[0]) if len(values) == 1 else None,
            }
        if path.startswith("/api/v1/operator/cameras/") and path.endswith("/media-session"):
            return {
                "category": "media_session",
                "camera": self.camera_ids.get(pieces[-2]),
            }
        categories = {
            "/api/v1/operator/cameras": "inventory",
            "/api/v1/operator/capabilities": "capabilities",
            "/api/v1/operator/status": "status",
            "/media/hls.js": "hls_script",
            "/api/v1/auth/me": "account",
        }
        return {"category": categories[path], "camera": None} if path in categories else None

    def relative(self, timestamp):
        value = finite(timestamp)
        if value is None:
            return None
        if self.started is None:
            self.started = value
        return round((value - self.started) * 1000, 3)

    def record(self, event, row, timestamp):
        self.events.append({"event": event, "milliseconds": self.relative(timestamp), **row})

    @observation_only
    def attach(self):
        self.session = bounded_session(self.page)
        for name, function in (
            ("Network.requestWillBeSent", self.requested),
            ("Network.responseReceived", self.responded),
            ("Network.loadingFinished", self.finished),
            ("Network.loadingFailed", self.failed),
        ):
            self.session.on(name, function)
            self.handlers.append((name, function))
        bounded_send(self.session, "Network.enable", {})
        bounded_send(
            self.session,
            "Runtime.evaluate",
            {
                "expression": "(" + BROWSER_SCRIPT + ")(" + json.dumps(list(self.camera_ids)) + ")",
                "returnByValue": True,
                "timeout": 1000,
            },
        )

    @observation_only
    def requested(self, event):
        request = event.get("request", {})
        classified = self.classify(request.get("url", ""))
        if classified is None:
            return
        self.serial += 1
        row = {
            "sequence": self.serial,
            **classified,
            "start": finite(event.get("timestamp")),
            "resource": event.get("type")
            if event.get("type") in {"Media", "XHR", "Fetch", "Script", "Other"}
            else "other",
        }
        identifier = event.get("requestId")
        self.requests[identifier] = row
        if len(self.requests) > MAX_REQUESTS:
            self.requests.popitem(last=False)
        self.record(
            "request",
            {k: v for k, v in row.items() if k != "start"},
            event.get("timestamp"),
        )

    @observation_only
    def responded(self, event):
        row = self.requests.get(event.get("requestId"))
        if row is None:
            return
        response = event.get("response", {})
        headers = {str(k).lower(): v for k, v in response.get("headers", {}).items()}
        status = response.get("status")
        timing = response.get("timing", {})
        connection = response.get("connectionId")
        if connection not in self.connections and len(self.connections) < 64:
            self.connections[connection] = len(self.connections)
        row.update(
            status=status if type(status) in {int, float} and 100 <= status <= 599 else None,
            mime=mime(headers.get("content-type", response.get("mimeType"))),
            content_length=content_length(headers.get("content-length")),
            protocol=response.get("protocol")
            if response.get("protocol") in {"http/1.0", "http/1.1", "h2", "h3"}
            else "other",
            connection=self.connections.get(connection),
            connection_reused=response.get("connectionReused") is True,
        )
        safe_timing = {
            key: finite(timing.get(key), 120000)
            for key in (
                "proxyStart",
                "proxyEnd",
                "dnsStart",
                "dnsEnd",
                "connectStart",
                "connectEnd",
                "sslStart",
                "sslEnd",
                "sendStart",
                "sendEnd",
                "receiveHeadersStart",
                "receiveHeadersEnd",
            )
        }
        if finite(timing.get("requestTime")) is not None and row["start"] is not None:
            safe_timing["request_queue_ms"] = finite(
                (timing["requestTime"] - row["start"]) * 1000, 120000
            )
        row["timing"] = safe_timing
        self.record(
            "response",
            {k: v for k, v in row.items() if k != "start"},
            event.get("timestamp"),
        )

    @observation_only
    def finished(self, event):
        identifier = event.get("requestId")
        row = self.requests.get(identifier)
        if row is None:
            return
        row["transfer_bytes"] = finite(event.get("encodedDataLength"), 256 * 1024**2)
        self.record(
            "finished",
            {k: v for k, v in row.items() if k != "start"},
            event.get("timestamp"),
        )
        if row["category"] == "playback":
            self.finished_playback.append((identifier, dict(row)))

    @observation_only
    def failed(self, event):
        row = self.requests.get(event.get("requestId"))
        if row is None:
            return
        error = event.get("errorText", "")
        self.record(
            "failed",
            {
                "sequence": row["sequence"],
                "category": row["category"],
                "camera": row["camera"],
                "canceled": event.get("canceled") is True,
                "network_error": error if error in NET_ERRORS else "other",
            },
            event.get("timestamp"),
        )

    @observation_only
    def capture(self, error):
        snapshot = {"available": False}
        bodies = []
        if self.session is not None:
            try:
                result = bounded_send(
                    self.session,
                    "Runtime.evaluate",
                    {
                        "expression": (
                            "window.__K5PassiveTransportWitness ? "
                            "window.__K5PassiveTransportWitness.snapshot() : ({available:false})"
                        ),
                        "returnByValue": True,
                        "awaitPromise": True,
                        "timeout": 1000,
                    },
                    1.5,
                )
                snapshot = result.get("result", {}).get("value", {"available": False})
            except BaseException:  # noqa: BLE001 - preserve the original failure.
                self.faults += 1
            for identifier, row in list(self.finished_playback):
                length, transferred = (
                    row.get("content_length"),
                    row.get("transfer_bytes"),
                )
                observation = {
                    "sequence": row["sequence"],
                    "status": row.get("status"),
                    "mime": row.get("mime"),
                    "cache_body_available": False,
                }
                if (
                    (length is not None and length > BODY_LIMIT)
                    or transferred is None
                    or transferred > BODY_LIMIT
                ):
                    observation["over_capture_limit"] = True
                else:
                    raw = None
                    try:
                        cached = bounded_send(
                            self.session,
                            "Network.getResponseBody",
                            {"requestId": identifier},
                            0.75,
                        )
                        body = cached.get("body", "")
                        if not isinstance(body, str) or len(body) > BODY_LIMIT * 2:
                            raise ValueError("cache_body_over_limit")
                        raw = bytearray(
                            base64.b64decode(body, validate=True)
                            if cached.get("base64Encoded")
                            else body.encode("utf-8")
                        )
                        if len(raw) > BODY_LIMIT:
                            raise ValueError("cache_body_over_limit")
                        observation.update(cache_body_available=True, **mp4_projection(raw))
                    except BaseException:  # noqa: BLE001 - a missing cache entry is not a new request.
                        self.faults += 1
                    finally:
                        if raw is not None:
                            raw[:] = b"\0" * len(raw)
                bodies.append(observation)
        result = {
            "scope": "combined-transport-diagnostic-only",
            "qualification": False,
            "source_head": SOURCE_HEAD,
            "source_tree": SOURCE_TREE,
            "test_sha256": TEST_SHA256,
            "original_exception_type": type(error).__name__
            if type(error).__name__ in {"Failed", "AssertionError", "TimeoutError", "Error"}
            else "other",
            "original_assertions_changed": False,
            "original_request_deadlines_changed": False,
            "requests_retried": 0,
            "additional_http_requests": 0,
            "observer_faults": self.faults,
            "observed_request_count": self.serial,
            "network_events": list(self.events),
            "browser": snapshot,
            "completed_playback_cache": bodies,
        }
        Path(OUTPUT).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")

    @observation_only
    def stop(self):
        if self.session is not None:
            try:
                bounded_send(
                    self.session,
                    "Runtime.evaluate",
                    {"expression": "window.__K5PassiveTransportWitness?.stop()", "timeout": 1000},
                )
            except BaseException:  # noqa: BLE001 - cleanup cannot alter the original result.
                self.faults += 1
            for name, function in self.handlers:
                self.session.remove_listener(name, function)
            # Closing only this debugger session removes its listeners. It does
            # not alter the page, original player or outstanding media requests.
            bounded_detach(self.session)


class WitnessPlugin:
    installed = False

    def pytest_collection_modifyitems(self, items):
        targets = [item for item in items if item.nodeid == TEST_FILE + "::" + TEST_NAME]
        if len(targets) != 1:
            raise RuntimeError("transport_witness_target_refused")
        module = targets[0].module
        original = module._desktop_nvr_journey

        @wraps(original)
        def observed(*args, **kwargs):
            witness = None
            try:
                try:
                    witness = TransportWitness(
                        kwargs["page"],
                        kwargs["camera_ids"],
                        kwargs["camera_directories"],
                    )
                except BaseException:  # noqa: BLE001 - the original must still run unchanged.
                    witness = None
                return original(*args, **kwargs)
            except BaseException as error:
                if witness is not None:
                    try:
                        witness.capture(error)
                    except BaseException:  # noqa: BLE001 - re-raise the original exception.
                        pass  # noqa: S110 - observer errors are deliberately non-authoritative.
                raise
            finally:
                if witness is not None:
                    try:
                        witness.stop()
                    except BaseException:  # noqa: BLE001 - preserve original cleanup and outcome.
                        pass  # noqa: S110 - diagnostic cleanup cannot replace test failures.

        module._desktop_nvr_journey = observed
        self.installed = True


def verify_source(root):
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=root, text=True).strip()

    if (
        not __debug__
        or git("rev-parse", "HEAD") != SOURCE_HEAD
        or git("rev-parse", "HEAD^{tree}") != SOURCE_TREE
        or git("status", "--porcelain", "--untracked-files=no")
        or digest((root / TEST_FILE).read_bytes()) != TEST_SHA256
    ):
        raise RuntimeError("transport_witness_source_refused")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args()
    root = args.source.resolve()
    verify_source(root)
    if args.preflight_only:
        print("Frozen original source admitted for transport diagnostic only.")
        return 0
    os.chdir(root)
    import pytest

    plugin = WitnessPlugin()
    result = pytest.main(
        [
            TEST_FILE + "::" + TEST_NAME,
            "-o",
            "addopts=",
            "--no-cov",
            "-s",
            "-o",
            "faulthandler_timeout=400",
            "--junitxml=combined-transport-b81a950-original.xml",
        ],
        plugins=[plugin],
    )
    if not plugin.installed:
        raise RuntimeError("transport_witness_not_installed")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
