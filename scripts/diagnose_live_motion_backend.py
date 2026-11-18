"""Replay a bounded synthetic zoned PUT in an owned installed desktop worker."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID

_WINDOWS = os.name == "nt"
_SECONDS = 45.0
_REAP_SECONDS = 2.0
_MAX_OUTPUT = 1024 * 1024
_GRANT = b"K5_PRIVATE_LIVE_MASK_PROBE_V1\n"
_CODE = r"""
import sys
if sys.stdin.buffer.readline(64) != b"K5_PRIVATE_LIVE_MASK_PROBE_V1\n":
    raise SystemExit(2)
import faulthandler,json,os,time
configuration=json.loads(sys.stdin.buffer.readline(32769))
descriptor=os.open(configuration["stack"],os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
stack=os.fdopen(descriptor,"wb",buffering=0)
original_stdout=sys.stdout
faulthandler.enable(file=stack,all_threads=True)
faulthandler.dump_traceback_later(2,file=stack,repeat=True)
try:
    from k5vision import desktop_setup
    request=desktop_setup._request(json.dumps(configuration["request"]).encode())
    result=desktop_setup._serve(request,sys.stdin.buffer,original_stdout)
    original_stdout.write(json.dumps(result)+"\n")
    original_stdout.flush()
finally:
    faulthandler.cancel_dump_traceback_later()
    faulthandler.disable()
    stack.close()
"""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _job():
    if _WINDOWS:
        from k5vision.operator_native_process import _WindowsJob

        return _WindowsJob()
    return None


def _private_file(path):
    return os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb", buffering=0)


def _write(stream, data):
    pending = memoryview(data)
    while pending:
        count = stream.write(pending)
        if type(count) is not int or not 0 < count <= len(pending):
            raise OSError("Private probe grant was not written")
        pending = pending[count:]


def _worker_environment():
    environment = dict(os.environ)
    system_root = environment["SYSTEMROOT"]
    environment["PATH"] = os.pathsep.join((str(Path(system_root) / "System32"), system_root))
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    return environment


def _request(
    opener,
    origin,
    observations,
    operation,
    route,
    *,
    token="",
    method="GET",
    payload=None,
    seconds=3,
):
    began = time.monotonic()
    event = {
        "operation": operation,
        "status": "transport_error",
        "http_status": None,
        "body_bytes": 0,
    }
    body = b""
    try:
        headers = {"Origin": origin}
        if token:
            headers["Authorization"] = "Bearer " + token
        data = None if payload is None else json.dumps(payload, allow_nan=False).encode()
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = Request(origin + route, data=data, headers=headers, method=method)
        try:
            response = opener.open(request, timeout=seconds)
        except HTTPError as error:
            response = error
        with response:
            event["http_status"] = int(response.code)
            body = response.read(_MAX_OUTPUT + 1)
        event["body_bytes"] = min(len(body), _MAX_OUTPUT + 1)
        event["status"] = "response" if len(body) <= _MAX_OUTPUT else "body_limit"
    except TimeoutError:
        event["status"] = "timeout"
    except URLError as error:
        event["status"] = "timeout" if isinstance(error.reason, TimeoutError) else "transport_error"
    except (OSError, ValueError, TypeError):
        event["status"] = "transport_error"
    event["elapsed_ms"] = min(45000, max(0, int((time.monotonic() - began) * 1000)))
    observations.append(event)
    return event, body


def _document(body):
    value = json.loads(body)
    if type(value) is not dict:
        raise ValueError
    return value


def probe_live_backend(installed, profile, port, output, *, username, password, camera_id):
    """Use the same generated profile/source while preserving the original failure.

    The native caller must stop the original app/cohort first and keep its
    generated source fixtures alive until this function returns. This is not
    graphical qualification and must not replace the original result.
    """
    installed, profile, output = Path(installed), Path(profile), Path(output)
    if (
        type(port) is not int
        or not 1 <= port <= 65535
        or str(UUID(camera_id)) != camera_id
        or type(username) is not str
        or not 1 <= len(username) <= 256
        or type(password) is not str
        or not 1 <= len(password) <= 256
        or output.exists()
        or output.is_symlink()
        or output.resolve().is_relative_to(installed.resolve())
    ):
        raise ValueError("Live diagnostic input was refused")
    runtime = installed / "runtime" / "python.exe"
    for path in (runtime, installed / "workers/mediamtx.exe", installed / "workers/caddy.exe"):
        info = path.lstat()
        if (
            path.is_symlink()
            or not stat.S_ISREG(info.st_mode)
            or not 0 < info.st_size <= 200 * 1024**2
        ):
            raise ValueError("Installed diagnostic executable was refused")
    output.mkdir(mode=0o700)
    private = output / "private"
    private.mkdir(mode=0o700)
    stdout_path, stderr_path = private / "live.stdout", private / "live.stderr"
    stack_path = private / "live-backend-stack.txt"
    origin = f"http://127.0.0.1:{port}"
    route = f"/api/v1/operator/cameras/{camera_id}"
    observations = []
    process, job, watcher = None, None, None
    stop_watch, expired = threading.Event(), threading.Event()
    began = time.monotonic()
    deadline = began + _SECONDS
    status = "launch_failed"
    cleanup = True
    token = ""
    source_binding_checked = False

    def expire():
        if stop_watch.wait(max(0, deadline - time.monotonic())):
            return
        expired.set()
        try:
            if job is not None:
                job.terminate()
        except Exception:  # noqa: BLE001 - private native errors are never emitted
            pass
        try:
            if process is not None and process.poll() is None:
                process.kill()
        except OSError:
            pass

    try:
        with _private_file(stdout_path) as stdout, _private_file(stderr_path) as stderr:
            job = _job()
            process = subprocess.Popen(
                [str(runtime), "-I", "-B", "-u", "-c", _CODE],
                stdin=subprocess.PIPE,
                stdout=stdout,
                stderr=stderr,
                bufsize=0,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if _WINDOWS else 0,
                start_new_session=not _WINDOWS,
                close_fds=True,
                cwd=installed,
                env=_worker_environment(),
            )
            status = "ownership_failed"
            if job is not None:
                job.assign(process)
            watcher = threading.Thread(target=expire, name="live-mask-probe-deadline", daemon=True)
            watcher.start()
            configuration = {
                "stack": str(stack_path),
                "request": {
                    "schema_version": 1,
                    "operation": "serve",
                    "identity_directory": str(profile / "identity"),
                    "recording_root": str(profile / "recordings"),
                    "media_executable": str(installed / "workers/mediamtx.exe"),
                    "gateway_executable": str(installed / "workers/caddy.exe"),
                    "port": port,
                    "recording_limit_gb": 20,
                },
            }
            _write(process.stdin, _GRANT + json.dumps(configuration).encode() + b"\n")
            status = "readiness_failed"
            opener = build_opener(ProxyHandler({}), _NoRedirect())
            ready_deadline = min(deadline - 25, time.monotonic() + 10)
            while time.monotonic() < ready_deadline and process.poll() is None:
                event, body = _request(
                    opener, origin, observations, "health_before", "/api/v1/health", seconds=1
                )
                if event["http_status"] == 200 and _document(body).get("status") == "ok":
                    break
                time.sleep(0.15)
            else:
                raise ValueError
            status = "login_failed"
            event, body = _request(
                opener,
                origin,
                observations,
                "login",
                "/api/v1/auth/login",
                method="POST",
                payload={"username": username, "password": password},
                seconds=3,
            )
            if event["http_status"] != 200:
                raise ValueError
            token = _document(body).get("session_token")
            if type(token) is not str or re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token) is None:
                raise ValueError
            status = "policy_failed"
            event, body = _request(
                opener,
                origin,
                observations,
                "policy_before",
                route + "/recording-policy",
                token=token,
                seconds=2,
            )
            if event["http_status"] != 200:
                raise ValueError
            policy = _document(body)
            revision = policy.get("revision")
            if type(revision) is not int or not 0 <= revision < 2**63 - 1:
                raise ValueError
            status = "preview_failed"
            event, body = _request(
                opener,
                origin,
                observations,
                "preview_post",
                route + "/motion-preview",
                token=token,
                method="POST",
                seconds=4,
            )
            if event["http_status"] != 200:
                raise ValueError
            preview = _document(body)
            identifier = preview.get("preview_id")
            expected = route + "/motion-preview/" + str(identifier)
            if (
                type(identifier) is not str
                or re.fullmatch(r"[A-Za-z0-9_-]{32,64}", identifier) is None
                or preview.get("preview_url") != expected
                or preview.get("source_revision") != policy.get("source_revision")
                or re.fullmatch(r"[0-9a-f]{64}", preview.get("source_revision", "")) is None
                or any(
                    type(preview.get(k)) is not int or not 1 <= preview[k] <= 8192
                    for k in ("width", "height")
                )
                or preview["width"] * preview["height"] > 33554432
                or type(preview.get("rotation")) is not int
                or preview["rotation"] != 0
            ):
                raise ValueError
            source_binding_checked = True
            event, body = _request(
                opener, origin, observations, "preview_jpeg", expected, token=token, seconds=2
            )
            if (
                event["http_status"] != 200
                or not body.startswith(b"\xff\xd8\xff")
                or not body.endswith(b"\xff\xd9")
            ):
                raise ValueError
            reference = {
                key: preview[key] for key in ("width", "height", "rotation", "source_revision")
            }
            payload = {
                "expected_revision": revision,
                "preview_id": identifier,
                "mode": "motion",
                "post_capture_seconds": 3,
                "sensitivity": 50,
                "minimum_area_percent": 0.1,
                "zones": {
                    "reference": reference,
                    "include": [
                        {
                            "id": "11111111-1111-4111-8111-111111111111",
                            "name": "Probe inclusion",
                            "points": [[0.02, 0.02], [0.8, 0.02], [0.8, 0.98], [0.02, 0.98]],
                        }
                    ],
                    "exclude": [
                        {
                            "id": "22222222-2222-4222-8222-222222222222",
                            "name": "Probe exclusion",
                            "points": [[0.49, 0.02], [0.8, 0.02], [0.8, 0.98], [0.49, 0.98]],
                        }
                    ],
                },
            }
            status = "insufficient_put_budget"
            if deadline - time.monotonic() < 19:
                raise ValueError
            event, body = _request(
                opener,
                origin,
                observations,
                "zoned_put",
                route + "/recording-policy",
                token=token,
                method="PUT",
                payload=payload,
                seconds=15,
            )
            status = "zoned_put_" + event["status"]
            _request(opener, origin, observations, "health_after", "/api/v1/health", seconds=1)
            _request(
                opener,
                origin,
                observations,
                "policy_after",
                route + "/recording-policy",
                token=token,
                seconds=1,
            )
            # Keep a few additional private snapshots after the observed PUT
            # without increasing its original client request deadline.
            until = min(deadline, time.monotonic() + 4)
            while time.monotonic() < until and process.poll() is None:
                time.sleep(0.1)
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        pass
    finally:
        token = ""
        stop_watch.set()
        cleanup_deadline = time.monotonic() + _REAP_SECONDS
        if watcher is not None and watcher.ident is not None:
            watcher.join(timeout=max(0, min(0.5, cleanup_deadline - time.monotonic())))
            cleanup = not watcher.is_alive()
        if process is not None:
            try:
                process.stdin.close()
                grace_deadline = min(cleanup_deadline, time.monotonic() + 0.2)
                while process.poll() is None and time.monotonic() < grace_deadline:
                    time.sleep(0.01)
            except OSError:
                cleanup = False
            try:
                if process.poll() is None:
                    process.kill()
            except OSError:
                cleanup = False
        if job is not None:
            try:
                job.close()
            except Exception:  # noqa: BLE001
                cleanup = False
        if process is not None:
            try:
                process.wait(timeout=max(0.01, cleanup_deadline - time.monotonic()))
            except (OSError, subprocess.TimeoutExpired):
                cleanup = False
    counts = {}
    for key, path in (("stdout_bytes", stdout_path), ("stderr_bytes", stderr_path)):
        counts[key] = min(_MAX_OUTPUT + 1, path.stat().st_size) if path.exists() else 0
        if path.exists():
            path.unlink()
    result = {
        "schema_version": 1,
        "scope": "installed_desktop_worker_control_replay",
        "product_qualification": False,
        "status": status,
        "worker_seconds": 45,
        "zoned_put_seconds": 15,
        "stack_repeat_seconds": 2,
        "desktop_serve_entrypoint": True,
        "restricted_system_path": True,
        "python_home_path_removed": True,
        "source_binding_checked": source_binding_checked,
        "observations": observations,
        "worker_deadline_reached": expired.is_set(),
        "cleanup_complete": cleanup,
        "elapsed_ms": min(50000, max(0, int((time.monotonic() - began) * 1000))),
        "private_stack_present": stack_path.is_file(),
        "private_stack_bytes": min(_MAX_OUTPUT + 1, stack_path.stat().st_size)
        if stack_path.is_file()
        else 0,
        **counts,
    }
    with _private_file(output / "live-backend-probe.json") as target:
        target.write((json.dumps(result, allow_nan=False, sort_keys=True) + "\n").encode())
    return result
