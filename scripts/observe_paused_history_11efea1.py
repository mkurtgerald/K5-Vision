"""Diagnostic-only observations after an unchanged installed NVR failure.

Original test functions, requests, assertions, and deadlines remain unchanged.
Only bounded metadata is retained; credentials, source URLs, paths, response
bodies, recording content, screenshots and pixels never enter the output.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

SOURCE = "11efea1bd5d33533148c2aa3d53c445017d102f5"
TREE = "f05040b021eb429aa85d155b1635b4718f4a6cc6"
TEST = "tests/integration/test_windows_desktop_app.py"
TEST_SHA = "d08831a743c13b27a125c3272e24fde407d7fedd7f469b41f279c284634902e6"
TARGET = "test_native_windows_click_install_account_browser_tray_restart_uninstall"
OUTPUT = Path("paused-history-11efea1-observation.json")
_FILENAME = re.compile(r"(\d{4}-\d\d-\d\d_\d\d-\d\d-\d\d-\d{6}_(?:Z|[+-]\d{4}))\.mp4")


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def utc():
    return datetime.now(UTC)


def iso(value):
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def require_source(root):
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=root, text=True).strip()

    if (
        git("rev-parse", "HEAD") != SOURCE
        or git("rev-parse", "HEAD^{tree}") != TREE
        or git("status", "--porcelain", "--untracked-files=no")
        or sha((root / TEST).read_bytes()) != TEST_SHA
    ):
        raise RuntimeError("history_diagnostic_source_refused")


def finite_status(value):
    return value if type(value) is int and 100 <= value <= 599 else None


def endpoint(address):
    if type(address) is not str:
        raise ValueError
    parsed = urlsplit("http://" + address)
    if (
        parsed.hostname != "127.0.0.1"
        or parsed.username
        or parsed.password
        or parsed.path
        or not parsed.port
    ):
        raise ValueError
    return "http://127.0.0.1:" + str(parsed.port)


def bounded_json(opener, url, headers, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return {"budget_exhausted": True}, None
    request = Request(url, headers=headers)
    try:
        try:
            response = opener.open(request, timeout=min(0.35, remaining))
        except HTTPError as error:
            response = error
        with response:
            status = finite_status(response.code)
            raw = response.read(131073)
        if len(raw) > 131072:
            return {"status": status, "body_over_limit": True}, None
        try:
            return {"status": status}, json.loads(raw)
        except (ValueError, UnicodeError):
            return {"status": status, "invalid_json": True}, None
    except Exception as error:  # noqa: BLE001 - retain only finite transport names.
        name = type(error).__name__
        return {
            "transport": name
            if name in {"TimeoutError", "URLError", "ConnectionRefusedError"}
            else "other"
        }, None


def file_inventory(directory, deadline, reference):
    result = {
        "files": 0,
        "parsed": 0,
        "unparsed": 0,
        "over_limit": False,
        "entries": [],
    }
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
            value = datetime.strptime(match[1], "%Y-%m-%d_%H-%M-%S-%f_%z").astimezone(
                UTC
            )
            info = item.stat()
            result["entries"].append(
                {
                    "start_utc": iso(value),
                    "start_minus_request_end_seconds": round(
                        (value - reference).total_seconds(), 6
                    ),
                    "bytes": info.st_size
                    if 0 <= info.st_size <= 64 * 1024**2
                    else None,
                    "modified_utc": iso(datetime.fromtimestamp(info.st_mtime, UTC)),
                }
            )
            result["parsed"] += 1
        except (OSError, ValueError, OverflowError):
            result["unparsed"] += 1
    return result


def observe_failure(arguments, history_calls, error):
    began = time.monotonic()
    deadline = began + 2.5
    client, profile = arguments["client"], arguments["profile"]
    directory = arguments["camera_directories"][0]
    reference = utc()
    if history_calls:
        reference = datetime.fromisoformat(history_calls[-1]["entered_utc"])
    result = {
        "scope": "paused-history-diagnostic-only",
        "qualification": False,
        "source_head": SOURCE,
        "source_tree": TREE,
        "test_sha256": TEST_SHA,
        "original_exception_type": type(error).__name__
        if type(error).__name__
        in {"Failed", "AssertionError", "RuntimeError", "TimeoutError"}
        else "other",
        "original_test_assertions_changed": False,
        "original_request_deadlines_changed": False,
        "request_retries_added": 0,
        "history_calls": history_calls,
        "observation_utc": iso(utc()),
        "monotonic_seconds": round(began, 6),
        "recording_files": file_inventory(directory, deadline, reference),
    }
    try:
        cfgpath = profile / "identity" / "operator-mediamtx.json"
        if (
            cfgpath.is_symlink()
            or not cfgpath.is_file()
            or cfgpath.stat().st_size > 1048576
        ):
            result["config_unavailable"] = True
        else:
            config = json.loads(cfgpath.read_text(encoding="utf-8"))
            path_name = directory.name
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", path_name):
                raise ValueError("history_diagnostic_path_refused")
            selected = config.get("paths", {}).get(path_name)
            result["path_identity_sha256"] = sha(path_name.encode())
            result["persistent_config"] = {
                "path_present": type(selected) is dict,
                "record": selected.get("record")
                if type(selected) is dict and type(selected.get("record")) is bool
                else None,
                "record_path_sha256": sha(selected["recordPath"].encode())
                if type(selected) is dict and type(selected.get("recordPath")) is str
                else None,
            }
            users = config.get("authInternalUsers", [])
            if type(users) is not list or len(users) != 1:
                raise ValueError("history_diagnostic_credentials_refused")
            user = users[0]
            basic = base64.b64encode(
                (user["user"] + ":" + user["pass"]).encode("ascii")
            ).decode("ascii")
            headers = {"Authorization": "Basic " + basic, "Accept": "application/json"}
            opener = build_opener(ProxyHandler({}), NoRedirect())
            api = endpoint(config["apiAddress"])
            playback = endpoint(config["playbackAddress"])
            note, actual = bounded_json(
                opener, api + "/v3/config/paths/get/" + path_name, headers, deadline
            )
            if type(actual) is dict and note.get("status") == 200:
                note.update(
                    record=actual.get("record")
                    if type(actual.get("record")) is bool
                    else None,
                    record_path_sha256=sha(actual["recordPath"].encode())
                    if type(actual.get("recordPath")) is str
                    else None,
                )
            result["native_path_config"] = note
            first, last = reference - timedelta(hours=24), reference
            query = urlencode(
                {"path": path_name, "start": first.isoformat(), "end": last.isoformat()}
            )
            note, spans = bounded_json(
                opener, playback + "/list?" + query, headers, deadline
            )
            note.update(query_start_utc=iso(first), query_end_utc=iso(last))
            if type(spans) is list:
                note["span_count"] = len(spans) if len(spans) <= 1024 else "over_limit"
            elif type(spans) is dict:
                value = spans.get("error")
                note["error"] = (
                    "no_recording_segments_found"
                    if value == "no recording segments found"
                    else "other"
                )
            result["native_list"] = note
            status_headers = {
                "Authorization": "Bearer " + client.token,
                "Origin": client.origin,
                "Accept": "application/json",
            }
            note, status = bounded_json(
                opener,
                client.origin + "/api/v1/operator/status",
                status_headers,
                deadline,
            )
            if type(status) is dict and note.get("status") == 200:
                stamp = status.get("server_utc")
                try:
                    note["server_utc"] = iso(datetime.fromisoformat(stamp))
                except (ValueError, AttributeError, TypeError):
                    note["invalid_server_utc"] = True
                clock = status.get("clock", {})
                note["clock_status"] = (
                    clock.get("status")
                    if clock.get("status") in {"observing", "changed", "unavailable"}
                    else "other"
                )
                count = clock.get("change_count")
                note["clock_change_count"] = (
                    count if type(count) is int and 0 <= count <= 1000000 else None
                )
                step = clock.get("last_step_seconds")
                note["last_clock_step_seconds"] = (
                    step
                    if type(step) in {int, float} and -315576000 <= step <= 315576000
                    else None
                )
            result["backend_status"] = note
    except Exception:  # noqa: BLE001 - metadata refusal cannot hide prior observations.
        result["native_observation_refused"] = True
    result["observation_seconds"] = round(time.monotonic() - began, 6)
    OUTPUT.write_text(
        json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )


def decorate(original):
    def observed(**arguments):
        calls = []
        client = arguments["client"]
        original_json = client.json
        route = "/api/v1/operator/cameras/" + arguments["camera_ids"][0] + "/history"

        def json_observer(request_route, *args, **kwargs):
            if request_route != route:
                return original_json(request_route, *args, **kwargs)
            began, wall = time.monotonic(), utc()
            result = original_json(request_route, *args, **kwargs)
            try:
                if len(calls) < 8:
                    spans = result.get("spans") if type(result) is dict else None
                    calls.append(
                        {
                            "entered_utc": iso(wall),
                            "completed_utc": iso(utc()),
                            "seconds": round(time.monotonic() - began, 6),
                            "span_count": len(spans)
                            if type(spans) is list and len(spans) <= 1024
                            else None,
                        }
                    )
            except BaseException:  # noqa: BLE001, S110 - preserve original result unchanged.
                pass
            return result

        client.json = json_observer
        try:
            return original(**arguments)
        except BaseException as error:
            try:
                observe_failure(arguments, calls, error)
            except BaseException:  # noqa: BLE001, S110 - preserve original failure and cleanup.
                # Collection must not replace the original error or cleanup.
                pass
            raise
        finally:
            client.json = original_json

    return observed


class Plugin:
    def __init__(self):
        self.installed = False

    def pytest_collection_modifyitems(self, items):
        selected = [item for item in items if item.nodeid == TEST + "::" + TARGET]
        if len(selected) != 1:
            raise RuntimeError("history_diagnostic_target_refused")
        module = selected[0].module
        module._desktop_nvr_journey = decorate(module._desktop_nvr_journey)
        self.installed = True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args()
    root = args.source.resolve()
    require_source(root)
    if args.preflight_only:
        print(
            "Frozen installed GUI source admitted for failure-only history observations"
        )
        return 0
    os.chdir(root)
    import pytest

    plugin = Plugin()
    code = pytest.main(
        [
            TEST,
            "-o",
            "addopts=",
            "--no-cov",
            "-s",
            "-o",
            "faulthandler_timeout=400",
            "--junitxml=paused-history-11efea1-original.xml",
        ],
        plugins=[plugin],
    )
    if not plugin.installed:
        raise RuntimeError("history_diagnostic_observer_not_installed")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
