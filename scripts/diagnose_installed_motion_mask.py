"""Failure-only cold-runtime mask probes. Never a product qualification result."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import time
from pathlib import Path

_WINDOWS = os.name == "nt"
_SECONDS = 12.0
_REAP_SECONDS = 2.0
_MAX_OUTPUT = 1024 * 1024
_GRANT = b"K5_PRIVATE_MASK_PROBE_V1\n"
_PHASES = frozenset(
    {
        "bootstrap",
        "public_import_begin",
        "public_import_ready",
        "executor_begin",
        "native_import_begin",
        "opencv_import_ready",
        "native_import_ready",
        "mask_begin",
        "mask_ready",
        "complete",
        "failed",
    }
)
_CODE = r"""
import sys
if sys.stdin.buffer.readline(64) != b"K5_PRIVATE_MASK_PROBE_V1\n":
    raise SystemExit(2)
import faulthandler, json, os, time
began = time.monotonic()
phase = "bootstrap"
stack = None
def emit(value, **fields):
    global phase
    phase = value
    item = {"phase": value, "elapsed_ms": min(12000, max(0, int((time.monotonic()-began)*1000)))}
    item.update(fields)
    print(json.dumps(item, allow_nan=False), flush=True)
try:
    descriptor = os.open(sys.argv[2], os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    stack = os.fdopen(descriptor, "wb", buffering=0)
    faulthandler.enable(file=stack, all_threads=True)
    faulthandler.dump_traceback_later(8, file=stack, repeat=False)
    emit("bootstrap")
    emit("public_import_begin")
    import k5vision.operator_product_api
    import k5vision.operator_motion as motion
    emit("public_import_ready", cv2_preloaded="cv2" in sys.modules,
         numpy_preloaded="numpy" in sys.modules)
    def work():
        emit("native_import_begin")
        import cv2
        emit("opencv_import_ready")
        import numpy as np
        emit("native_import_ready")
        include = [[0.05,0.05],[0.95,0.05],[0.95,0.95],[0.05,0.95]]
        exclude = [[0.6,0.6],[0.9,0.6],[0.9,0.9],[0.6,0.9]]
        policy = {
            "revision": 1, "source_revision": "0"*64,
            "sensitivity": 50, "minimum_area_percent": 0.5,
            "zones": {
                "reference": {"width":320,"height":240,"rotation":0,"source_revision":"0"*64},
                "include":[{"points":include}], "exclude":[{"points":exclude}],
            },
        }
        emit("mask_begin")
        motion.validate_effective_mask(policy)
        selected = motion.allowed_mask(320,240,[include],[exclude])
        count = int(np.count_nonzero(selected))
        if selected.shape != (240,320) or not 0 < count < 76800:
            raise ValueError
        emit("mask_ready", nonzero_pixels=count)
    if sys.argv[1] == "main":
        work()
    elif sys.argv[1] == "executor":
        from concurrent.futures import ThreadPoolExecutor
        emit("executor_begin")
        with ThreadPoolExecutor(max_workers=1,
                                thread_name_prefix="k5-live-motion-probe") as executor:
            executor.submit(work).result()
    else:
        raise ValueError
    emit("complete")
except BaseException:
    emit("failed")
    raise SystemExit(1) from None
finally:
    faulthandler.cancel_dump_traceback_later()
    faulthandler.disable()
    if stack is not None:
        stack.close()
"""


def _job():
    if _WINDOWS:
        from k5vision.operator_native_process import _WindowsJob

        return _WindowsJob()
    return None


def _private_file(path):
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    return os.fdopen(descriptor, "wb", buffering=0)


def _events(raw):
    result = []
    valid = True
    for line in raw.splitlines():
        try:
            value = json.loads(line)
            keys = {"phase", "elapsed_ms"}
            if value.get("phase") == "public_import_ready":
                keys |= {"cv2_preloaded", "numpy_preloaded"}
                if any(
                    type(value.get(k)) is not bool for k in ("cv2_preloaded", "numpy_preloaded")
                ):
                    raise ValueError
            if value.get("phase") == "mask_ready":
                keys.add("nonzero_pixels")
                if (
                    type(value.get("nonzero_pixels")) is not int
                    or not 0 < value["nonzero_pixels"] < 76800
                ):
                    raise ValueError
            if (
                type(value) is not dict
                or set(value) != keys
                or value.get("phase") not in _PHASES
                or type(value.get("elapsed_ms")) is not int
                or not 0 <= value["elapsed_ms"] <= 12000
            ):
                raise ValueError
            result.append(value)
        except (ValueError, TypeError, AttributeError):
            valid = False
    return result, valid


def _probe(runtime, private, mode):
    stdout_path, stderr_path = private / (mode + ".stdout"), private / (mode + ".stderr")
    stack_path = private / ("opencv-" + mode + "-stack.txt")
    process, job = None, None
    began = time.monotonic()
    status = "launch_failed"
    cleanup = True
    code = None
    try:
        with _private_file(stdout_path) as stdout, _private_file(stderr_path) as stderr:
            job = _job()
            process = subprocess.Popen(
                [str(runtime), "-I", "-B", "-u", "-c", _CODE, mode, str(stack_path)],
                stdin=subprocess.PIPE,
                stdout=stdout,
                stderr=stderr,
                bufsize=0,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if _WINDOWS else 0,
                start_new_session=not _WINDOWS,
                close_fds=True,
                cwd=private,
            )
            status = "ownership_failed"
            if job is not None:
                job.assign(process)
            status = "grant_failed"
            process.stdin.write(_GRANT)
            process.stdin.close()
            status = "exited"
            while process.poll() is None:
                if time.monotonic() - began >= _SECONDS:
                    status = "timeout"
                    break
                if any(p.stat().st_size > _MAX_OUTPUT for p in (stdout_path, stderr_path)):
                    status = "output_limit"
                    break
                time.sleep(0.05)
    except Exception:  # noqa: BLE001 - only a finite stage is retained
        status = "launch_failed" if process is None else status
    finally:
        deadline = time.monotonic() + _REAP_SECONDS
        if process is not None:
            try:
                if process.poll() is None:
                    process.kill()
            except OSError:
                cleanup = False
        if job is not None:
            try:
                job.close()
            except Exception:  # noqa: BLE001 - no native error text enters evidence
                cleanup = False
        if process is not None:
            try:
                code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
            except (OSError, subprocess.TimeoutExpired):
                cleanup = False
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except OSError:
                    cleanup = False
    sizes = {}
    raw = b""
    for name, path in (("stdout", stdout_path), ("stderr", stderr_path)):
        if path.exists():
            sizes[name] = min(_MAX_OUTPUT + 1, path.stat().st_size)
            if name == "stdout":
                with path.open("rb") as stream:
                    raw = stream.read(_MAX_OUTPUT)
            path.unlink()
        else:
            sizes[name] = 0
    events, valid = _events(raw)
    if status == "exited":
        status = (
            "complete"
            if code == 0 and valid and events and events[-1]["phase"] == "complete"
            else "failed"
        )
    if not valid or sizes["stdout"] > _MAX_OUTPUT:
        status = "invalid_output"
    if not cleanup:
        status = "cleanup_failed"
    return {
        "mode": mode,
        "status": status,
        "elapsed_ms": min(16000, max(0, int((time.monotonic() - began) * 1000))),
        "return_code": code if type(code) is int and -(2**31) <= code < 2**32 else None,
        "cleanup_complete": cleanup,
        "stdout_bytes": sizes["stdout"],
        "stderr_bytes": sizes["stderr"],
        "stdout_valid": valid,
        "events": events,
        "private_stack_present": stack_path.is_file(),
        "private_stack_bytes": min(_MAX_OUTPUT + 1, stack_path.stat().st_size)
        if stack_path.is_file()
        else 0,
    }


def probe_installed_mask(installed: Path, output: Path) -> dict:
    """Run two isolated cold probes after failure and before uninstall.

    Only opencv-probe.json and the two private no-locals stack files are retained.
    The original product result remains independent and must stay failed.
    """
    installed, output = Path(installed), Path(output)
    runtime = installed / "runtime" / "python.exe"
    if (
        output.exists()
        or output.is_symlink()
        or output.resolve().is_relative_to(installed.resolve())
    ):
        raise ValueError("Diagnostic output was refused")
    info = runtime.lstat()
    if (
        runtime.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or not 0 < info.st_size <= 32 * 1024**2
    ):
        raise ValueError("Installed runtime was refused")
    output.mkdir(mode=0o700)
    private = output / "private"
    private.mkdir(mode=0o700)
    result = {
        "schema_version": 1,
        "scope": "cold_installed_runtime_after_failed_journey",
        "product_qualification": False,
        "probe_seconds": 12,
        "reap_seconds": 2,
        "stack_after_seconds": 8,
        "public_product_api_preload": True,
        "probes": [_probe(runtime, private, mode) for mode in ("main", "executor")],
    }
    with _private_file(output / "opencv-probe.json") as target:
        target.write((json.dumps(result, allow_nan=False, sort_keys=True) + "\n").encode())
    return result
