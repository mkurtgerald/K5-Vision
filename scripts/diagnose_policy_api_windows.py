"""Bounded generated-fixture diagnostics; never Windows product qualification.

The target development Python starts behind a stdin gate. On Windows the
supervisor assigns its real kill-on-close Job Object before opening that gate.
Pytest writes directly to a file, allowing its faulthandler trace to survive a
hung asyncio teardown. Only this owned process tree is terminated at cutoff.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

TIMEOUT_SECONDS = 120.0
LOG_LIMIT = 8 * 1024 * 1024
_GATE = b"K5_POLICY_DIAGNOSTIC_START_V1\n"
_BOOTSTRAP = (
    "import sys\n"
    f"gate={_GATE!r}\n"
    "if sys.stdin.buffer.read(len(gate))!=gate or sys.stdin.buffer.read(1):\n"
    " raise SystemExit(74)\n"
    "import os,runpy,site\n"
    "site.main()\n"
    "sys.path.insert(0,os.path.join(os.getcwd(),'src'))\n"
    "arguments=sys.argv[1:]\n"
    "if arguments[:2]==['-m','pytest']:\n"
    " sys.argv=['pytest',*arguments[2:]]\n"
    " runpy.run_module('pytest',run_name='__main__',alter_sys=True)\n"
    "elif arguments[:1]==['-c'] and len(arguments)==2:\n"
    " sys.argv=['-c']\n"
    " exec(compile(arguments[1],'<generated-diagnostic-control>','exec'),{'__name__':'__main__'})\n"
    "else:\n"
    " raise SystemExit(75)\n"
)
_TRUNCATION_MARKER = b"\n[K5 diagnostic log truncated: preserved head and failure tail]\n"


def _windows_job(source):
    path = source / "src/k5vision/operator_native_process.py"
    spec = importlib.util.spec_from_file_location("_policy_diagnostic_native_owner", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("native owner unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._WindowsJob()


def _observe_exit(process):
    if os.name == "nt":
        return process.poll()
    # Keep the POSIX leader unreaped until its own process group is stopped.
    # Its reserved PID cannot then be reused for a foreign process group.
    value = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    if value is None:
        return None
    return value.si_status if value.si_code == os.CLD_EXITED else -value.si_status


def _cleanup(process, job):
    complete = True
    if job is not None:
        try:
            job.terminate()
        except Exception:
            # The independently configured kill-on-close handle is the fallback.
            pass
        try:
            job.close()
        except Exception:
            complete = False
    if process is not None:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError:
                complete = False
        elif process.poll() is None:
            # Also handles an assignment failure while the generated bootstrap
            # remains behind the closed gate and has spawned no tests.
            try:
                process.kill()
            except OSError:
                complete = False
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            complete = False
            try:
                process.kill()
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass
        except OSError:
            complete = False
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
    return complete


def supervise(
    source,
    output,
    arguments,
    *,
    timeout=TIMEOUT_SECONDS,
    log_limit=LOG_LIMIT,
    job_factory=None,
):
    """Run a fixed generated-test command; shorter bounds are for local controls."""
    source, output = Path(source).resolve(strict=True), Path(output)
    if (
        not source.is_dir()
        or output.parent.resolve(strict=True) != source
        or output.name != "policy-api-diagnostic-output"
        or output.exists()
        or output.is_symlink()
        or type(timeout) not in (int, float)
        or not 0 < timeout <= TIMEOUT_SECONDS
        or type(log_limit) is not int
        or not 1 <= log_limit <= LOG_LIMIT
        or not isinstance(arguments, list)
        or not arguments
        or any(type(value) is not str or "\x00" in value for value in arguments)
    ):
        raise ValueError("diagnostic configuration refused")
    output.mkdir(mode=0o700)
    log = output / "policy-api-diagnostic.log"
    summary_path = output / "policy-api-supervisor.json"
    started = time.monotonic()
    process = job = None
    status, error_type = "supervisor_error", None
    assigned = gate_opened = False
    observed_size = 0
    child_exit = None
    environment = dict(os.environ)
    for name in (
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "ACTIONS_RUNTIME_TOKEN",
        "SYSTEM_ACCESSTOKEN",
    ):
        environment.pop(name, None)
    environment["PYTHONUNBUFFERED"] = "1"
    environment["PYTEST_ADDOPTS"] = ""
    try:
        with log.open("xb", buffering=0) as stream:
            if os.name == "nt" or job_factory is not None:
                job = (job_factory or _windows_job)(source)
            process = subprocess.Popen(
                [sys.executable, "-I", "-S", "-u", "-c", _BOOTSTRAP, *arguments],
                cwd=source,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=stream,
                stderr=subprocess.STDOUT,
                close_fds=True,
                start_new_session=os.name == "posix",
                creationflags=0x08000200 if os.name == "nt" else 0,
            )
            if job is not None:
                job.assign(process)
                assigned = True
            if process.stdin is None:
                raise RuntimeError("start gate unavailable")
            process.stdin.write(_GATE)
            process.stdin.flush()
            process.stdin.close()
            gate_opened = True
            deadline = started + timeout
            while True:
                observed_size = max(observed_size, log.stat().st_size)
                child_exit = _observe_exit(process)
                if child_exit is not None:
                    status = "passed" if child_exit == 0 else "pytest_failed"
                    break
                if observed_size >= log_limit:
                    status = "log_limit"
                    break
                if time.monotonic() >= deadline:
                    status = "timed_out"
                    break
                time.sleep(0.025)
    except Exception as error:
        error_type = type(error).__name__
        if error_type not in {
            "OSError",
            "ValueError",
            "RuntimeError",
            "NativeProcessError",
        }:
            error_type = "other"
    finally:
        complete = _cleanup(process, job)
    if process is not None and process.returncode is not None:
        child_exit = process.returncode
    # A direct file can cross the monitor threshold between polling samples.
    # Preserve at most the declared limit after the owned writer has been reaped.
    retention = "complete"
    if log.exists():
        observed_size = max(observed_size, log.stat().st_size)
        if log.stat().st_size > log_limit:
            with log.open("r+b") as stream:
                if log_limit >= len(_TRUNCATION_MARKER) + 2:
                    head_bytes = min(64 * 1024, (log_limit - len(_TRUNCATION_MARKER)) // 4)
                    tail_bytes = log_limit - head_bytes - len(_TRUNCATION_MARKER)
                    head = stream.read(head_bytes)
                    stream.seek(-tail_bytes, os.SEEK_END)
                    preserved = head + _TRUNCATION_MARKER + stream.read(tail_bytes)
                    retention = "head_and_failure_tail"
                else:
                    stream.seek(-log_limit, os.SEEK_END)
                    preserved = stream.read(log_limit)
                    retention = "failure_tail"
                stream.seek(0)
                stream.write(preserved)
                stream.truncate(len(preserved))
    truncated = observed_size > log_limit
    if truncated and status in {"passed", "pytest_failed"}:
        status = "log_limit"
    summary = {
        "schema_version": 1,
        "scope": "generated-policy-api-fixtures-diagnostic-only",
        "production_qualification": False,
        "status": status,
        "timeout_seconds": timeout,
        "duration_seconds": round(time.monotonic() - started, 3),
        "monotonic_cutoff": True,
        "windows_job_assigned": assigned if os.name == "nt" else None,
        "start_gate_opened": gate_opened,
        "child_exit_code": child_exit,
        "log_limit_bytes": log_limit,
        "log_bytes_observed": observed_size,
        "log_bytes_preserved": log.stat().st_size if log.exists() else 0,
        "log_truncated": truncated,
        "log_retention": retention,
        "owned_cleanup_complete": complete,
        "supervisor_error_type": error_type,
        "tests_xml_written": (output / "policy-api-tests.xml").is_file(),
    }
    with summary_path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(summary, stream, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")
    print(json.dumps(summary, sort_keys=True, allow_nan=False), flush=True)
    return 0 if status == "passed" and complete else 124 if status == "timed_out" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = args.source.resolve(strict=True)
    for name in (
        "tests/unit/test_operator_recording_policy_api.py",
        "tests/unit/test_operator_motion_authority.py",
        "src/k5vision/operator_native_process.py",
    ):
        if not (source / name).is_file() or (source / name).is_symlink():
            raise ValueError("exact diagnostic source unavailable")
    arguments = [
        "-m",
        "pytest",
        "tests/unit/test_operator_recording_policy_api.py",
        "tests/unit/test_operator_motion_authority.py",
        "-vv",
        "-s",
        "-o",
        "faulthandler_timeout=30",
        "-o",
        "addopts=",
        "--no-cov",
        "--junitxml=" + str(args.output / "policy-api-tests.xml"),
    ]
    return supervise(source, args.output, arguments)


if __name__ == "__main__":
    raise SystemExit(main())
