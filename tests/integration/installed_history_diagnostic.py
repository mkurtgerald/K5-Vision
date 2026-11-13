"""Bounded metadata after an installed paused-history assertion has failed.

This helper never retries the original history request or reads recording
contents. Only finite counts, UTC dates, flags and opaque path hashes leave
private fixture state; the caller must preserve its original exception.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import time
from datetime import UTC, datetime, timedelta
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

_BUDGET_SECONDS = 2.5
_BODY_LIMIT = 131_072
_BYTE_LIMIT = 2**63 - 1
_POLICY_BYTE_LIMIT = 4096 * 1024**3
_FILENAME = re.compile(r"(\d{4}-\d\d-\d\d_\d\d-\d\d-\d\d-\d{6}_(?:Z|[+-]\d{4}))\.mp4")
_PATH = re.compile(r"k5_[0-9a-f]{64}")
_ERROR_DETAILS = {
    "roles_unavailable": "roles_unavailable",
    "Operator identity unavailable": "identity_unavailable",
    "Playback inventory unavailable": "playback_inventory_unavailable",
    "Unauthorized": "unauthorized",
    "Insufficient operator permission": "insufficient_permission",
}


def response_metadata(response):
    """Project an original GET tuple without retaining its body or headers."""
    result = {"http_status": None, "error_detail": "other"}
    if type(response) is not tuple or len(response) != 3:
        return result
    status, body, _ = response
    if type(status) is not int or not 100 <= status <= 599:
        return result
    result["http_status"] = status
    if status == 200:
        result["error_detail"] = None
        return result
    if type(body) is not bytes or len(body) > 16_384:
        result["error_body_unavailable"] = True
        return result
    try:
        value = json.loads(body)
        detail = value.get("detail") if type(value) is dict else None
        if type(detail) is dict:
            detail = detail.get("code")
        if type(detail) is str:
            result["error_detail"] = _ERROR_DETAILS.get(detail, "other")
    except (ValueError, UnicodeError, RecursionError):
        result["invalid_error_json"] = True
    return result


def _integer(value, maximum=_BYTE_LIMIT):
    return value if type(value) is int and 0 <= value <= maximum else None


def _number(value, minimum=0, maximum=315_576_000):
    return (
        value
        if type(value) in {int, float} and minimum <= value <= maximum and math.isfinite(value)
        else None
    )


def _boolean(value):
    return value if type(value) is bool else None


def _choice(value, allowed):
    return value if type(value) is str and value in allowed else "other"


def _iso(value):
    if not isinstance(value, datetime) or value.utcoffset() is None:
        return None
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _timestamp(value):
    if type(value) is not str or len(value) > 40:
        return None
    try:
        return _iso(datetime.fromisoformat(value))
    except (ValueError, OverflowError):
        return None


def _digest(value):
    return (
        hashlib.sha256(value.encode()).hexdigest()
        if type(value) is str and 0 < len(value) <= 4096
        else None
    )


def _endpoint(address):
    if type(address) is not str:
        raise ValueError
    parsed = urlsplit("http://" + address)
    if (
        parsed.hostname != "127.0.0.1"
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.port is None
        or not 1 <= parsed.port <= 65535
    ):
        raise ValueError
    return "http://127.0.0.1:" + str(parsed.port)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _bounded_json(opener, url, headers, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return {"budget_exhausted": True}, None
    try:
        try:
            response = opener.open(Request(url, headers=headers), timeout=min(0.35, remaining))
        except HTTPError as error:
            response = error
        with response:
            status = response.code
            note = {"status": status if type(status) is int and 100 <= status <= 599 else None}
            raw = bytearray()
            while len(raw) <= _BODY_LIMIT:
                if time.monotonic() >= deadline:
                    return {**note, "budget_exhausted": True}, None
                part = response.read1(min(65_536, _BODY_LIMIT + 1 - len(raw)))
                if not part:
                    break
                raw.extend(part)
        if len(raw) > _BODY_LIMIT:
            return {**note, "body_over_limit": True}, None
        try:
            return note, json.loads(raw)
        except (ValueError, UnicodeError):
            return {**note, "invalid_json": True}, None
    except Exception as error:  # noqa: BLE001 - exception messages remain private.
        return {
            "transport": _choice(
                type(error).__name__, {"TimeoutError", "URLError", "ConnectionRefusedError"}
            )
        }, None


def _file_inventory(directory, deadline, reference):
    result = {"files": 0, "parsed": 0, "unparsed": 0, "over_limit": False, "entries": []}
    if directory.is_symlink() or not directory.is_dir():
        return {"directory_unavailable": True}
    for index, item in enumerate(directory.iterdir()):
        if index >= 64:
            result["over_limit"] = True
            break
        if time.monotonic() >= deadline:
            result["budget_exhausted"] = True
            break
        if item.is_symlink() or not item.is_file():
            continue
        result["files"] += 1
        match = _FILENAME.fullmatch(item.name)
        if match is None:
            result["unparsed"] += 1
            continue
        try:
            stamp = datetime.strptime(match[1], "%Y-%m-%d_%H-%M-%S-%f_%z").astimezone(UTC)
            info = item.stat()
            result["entries"].append(
                {
                    "start_utc": _iso(stamp),
                    "start_minus_request_entry_seconds": _number(
                        round((stamp - reference).total_seconds(), 6),
                        -315_576_000,
                    ),
                    "bytes": _integer(info.st_size, 64 * 1024**2),
                    "modified_utc": _iso(datetime.fromtimestamp(info.st_mtime, UTC)),
                }
            )
            result["parsed"] += 1
        except (OSError, ValueError, OverflowError):
            result["unparsed"] += 1
    return result


def _status_snapshot(value):
    value = value if type(value) is dict else {}
    clock = value.get("clock")
    clock = clock if type(clock) is dict else {}
    storage = value.get("storage")
    storage = storage if type(storage) is dict else {}
    return {
        "server_utc": _timestamp(value.get("server_utc")),
        "worker_status": _choice(value.get("worker_status"), {"ready", "disk_full", "unavailable"}),
        "clock_status": _choice(clock.get("status"), {"observing", "changed", "unavailable"}),
        "clock_change_count": _integer(clock.get("change_count"), 1_000_000),
        "last_clock_step_seconds": _number(clock.get("last_step_seconds"), -315_576_000),
        "max_recording_bytes": _integer(value.get("max_recording_bytes"), _POLICY_BYTE_LIMIT),
        "storage": {
            "reason": _choice(
                storage.get("reason"), {"ready", "free_space", "capacity", "inspection"}
            ),
            "recording_allowed": _boolean(storage.get("recording_allowed")),
            "recording_bytes": _integer(storage.get("recording_bytes")),
            "free_bytes": _integer(storage.get("free_bytes")),
        },
    }


def _policy_snapshot(value):
    value = value if type(value) is dict else {}
    return {
        "revision": _integer(value.get("revision")),
        "quota_bytes": _integer(value.get("quota_bytes"), _POLICY_BYTE_LIMIT),
        "minimum_free_bytes": _integer(value.get("minimum_free_bytes"), _POLICY_BYTE_LIMIT),
        "retention_seconds": _integer(value.get("retention_seconds"), 30 * 86400),
        "recording_allowed": _boolean(value.get("recording_allowed")),
        "inspection_available": _boolean(value.get("inspection_available")),
        "recording_bytes": _integer(value.get("recording_bytes")),
        "free_bytes": _integer(value.get("free_bytes")),
    }


def observe_failure(
    *,
    client,
    profile,
    directory,
    entered_utc,
    completed_utc,
    elapsed,
    response,
    original_response=None,
    error=None,
):
    """Call only after the unchanged assertion failed; never serialize private data."""
    began = time.monotonic()
    deadline = began + _BUDGET_SECONDS
    reference = entered_utc if _iso(entered_utc) is not None else datetime.now(UTC)
    spans = response.get("spans") if type(response) is dict else None
    original_response = original_response if type(original_response) is dict else {}
    status = original_response.get("http_status")
    status = status if type(status) is int and 100 <= status <= 599 else None
    result = {
        "scope": "failed-paused-history-metadata",
        "request_retries_added": 0,
        "history_request": {
            "entered_utc": _iso(entered_utc),
            "completed_utc": _iso(completed_utc),
            "seconds": _number(elapsed, maximum=60),
            "span_count": _integer(len(spans), 1024) if type(spans) is list else None,
            "http_status": status,
            "error_detail": None
            if status == 200
            else _choice(original_response.get("error_detail"), set(_ERROR_DETAILS.values())),
            "get_completed": original_response.get("get_completed") is True,
            "get_failed": original_response.get("get_failed") is True,
            "projection_failed": original_response.get("projection_failed") is True,
        },
        "original_error_type": _choice(
            type(error).__name__,
            {
                "AssertionError",
                "RuntimeError",
                "TimeoutError",
                "JSONDecodeError",
                "KeyError",
                "Failed",
            },
        ),
        "observation_utc": _iso(datetime.now(UTC)),
        "recording_files": _file_inventory(directory, deadline, reference),
    }
    try:
        cfgpath = profile / "identity" / "operator-mediamtx.json"
        if cfgpath.is_symlink() or not cfgpath.is_file() or cfgpath.stat().st_size > 1_048_576:
            result["config_unavailable"] = True
            return result
        with cfgpath.open("rb") as stream:
            raw = stream.read(1_048_577)
        if len(raw) > 1_048_576:
            raise ValueError
        config = json.loads(raw)
        path_name = directory.name
        if type(config) is not dict or _PATH.fullmatch(path_name) is None:
            raise ValueError
        paths = config.get("paths")
        if type(paths) is not dict or len(paths) > 64:
            raise ValueError
        selected = paths.get(path_name)
        selected = selected if type(selected) is dict else {}
        result["path_identity_sha256"] = _digest(path_name)
        result["persistent_config"] = {
            "path_present": path_name in paths,
            "record": _boolean(selected.get("record")),
            "record_path_sha256": _digest(selected.get("recordPath")),
        }
        users = config.get("authInternalUsers")
        if type(users) is not list or len(users) != 1 or type(users[0]) is not dict:
            raise ValueError
        user, password = users[0].get("user"), users[0].get("pass")
        if type(user) is not str or type(password) is not str or len(user) + len(password) > 1024:
            raise ValueError
        basic = base64.b64encode((user + ":" + password).encode("ascii")).decode("ascii")
        headers = {"Authorization": "Basic " + basic, "Accept": "application/json"}
        opener = build_opener(ProxyHandler({}), _NoRedirect())
        api, playback = _endpoint(config["apiAddress"]), _endpoint(config["playbackAddress"])
        note, actual = _bounded_json(
            opener, api + "/v3/config/paths/get/" + path_name, headers, deadline
        )
        if type(actual) is dict and note.get("status") == 200:
            note.update(
                record=_boolean(actual.get("record")),
                record_path_sha256=_digest(actual.get("recordPath")),
            )
        result["native_path_config"] = note
        first, last = reference - timedelta(hours=24), reference
        query = urlencode({"path": path_name, "start": first.isoformat(), "end": last.isoformat()})
        note, available = _bounded_json(opener, playback + "/list?" + query, headers, deadline)
        note.update(
            query_start_utc=_iso(first),
            query_end_utc=_iso(last),
            query_end_basis="original_request_entry_utc",
        )
        if type(available) is list:
            note["span_count"] = _integer(len(available), 1024)
        elif type(available) is dict:
            note["error"] = (
                "no_recording_segments_found"
                if available.get("error") == "no recording segments found"
                else "other"
            )
        result["native_list"] = note
        public = urlsplit(client.origin)
        if public.scheme != "http" or public.path or public.query or public.fragment:
            raise ValueError
        origin = _endpoint(public.netloc)
        status_headers = {
            "Authorization": "Bearer " + client.token,
            "Origin": origin,
            "Accept": "application/json",
        }
        for key, route, project in (
            ("backend_status", "/api/v1/operator/status", _status_snapshot),
            ("recording_policy", "/api/v1/operator/admin/storage", _policy_snapshot),
        ):
            note, value = _bounded_json(opener, origin + route, status_headers, deadline)
            if note.get("status") == 200:
                note.update(project(value))
            result[key] = note
    except Exception:  # noqa: BLE001 - refusals never retain private exception text.
        result["native_observation_refused"] = True
    finally:
        result["observation_seconds"] = _number(round(time.monotonic() - began, 6), maximum=60)
    return result
