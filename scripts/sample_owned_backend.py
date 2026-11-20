"""Outside diagnostic prototype. Never execute this tool inside the target process."""

from __future__ import annotations

import ast
import hashlib
import itertools
import json
import stat
import subprocess
import threading
from pathlib import Path

EXE_SHA256 = "0890578ca20bf839d7f890bbcdb508a21cb9f1b71f99bf32be371c5e492a0b48"
EXE_BYTES = 4887040
MAX_RAW = 256 * 1024


def prepare_executable(path):
    """Call before entering the original clock phase, not during its request."""
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size != EXE_BYTES:
        raise ValueError("Sampler executable admission refused")
    if hashlib.sha256(path.read_bytes()).hexdigest() != EXE_SHA256:
        raise ValueError("Sampler executable admission refused")
    return path.resolve(strict=True)


def public_index(source_root):
    """Project only names that occur in the frozen public K5 source."""
    result = {}
    for path in sorted(Path(source_root).rglob("*.py")):
        if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
            continue
        raw = path.read_bytes()
        tree = ast.parse(raw)
        names = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        names.add("<module>")
        result[path.relative_to(source_root).as_posix()] = {
            "functions": names,
            "lines": max(1, len(raw.splitlines())),
        }
    return result


def remembered_backend(cohort):
    """No fresh Process/PID lookup, descendants enumeration, or argument emission."""
    with cohort.lock:
        remembered = list(cohort.processes.values())[:64]
        truncated = len(cohort.processes) > 64
    if truncated:
        return None, "owned_sample_truncated"
    candidates = []
    for process in remembered:
        try:
            if not process.is_running() or process.name().casefold() not in {
                "python.exe",
                "pythonw.exe",
            }:
                continue
            arguments = process.cmdline()
            matches = any(
                left == "-m" and right == "k5vision.desktop_setup"
                for left, right in itertools.pairwise(arguments)
            )
            if matches and process.is_running():
                candidates.append(process)
        except Exception:  # noqa: BLE001 -- diagnostic errors must not expose private text
            return None, "owned_identity_inspection_failed"
    if len(candidates) != 1:
        return (
            None,
            "owned_backend_missing" if not candidates else "owned_backend_ambiguous",
        )
    return candidates[0], "owned_backend_unique"


def project(raw, index):
    """No thread IDs/names, paths, locals, process data or unlisted strings leave here."""
    if not 0 < len(raw) <= MAX_RAW:
        return {"outcome": "raw_size_rejected", "threads": []}
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        return {"outcome": "json_rejected", "threads": []}
    if type(data) is not list:
        return {"outcome": "json_shape_rejected", "threads": []}
    threads = []
    for thread in data[:32]:
        if type(thread) is not dict or type(thread.get("frames")) is not list:
            continue
        frames = []
        for frame in thread["frames"][:128]:
            if type(frame) is not dict:
                continue
            filename, name, line = (
                frame.get("filename"),
                frame.get("name"),
                frame.get("line"),
            )
            if (
                type(filename) is not str
                or type(name) is not str
                or type(line) is not int
            ):
                continue
            normalized = filename.replace("\\", "/")
            if "/k5vision/" not in normalized:
                continue
            public = normalized.rsplit("/k5vision/", 1)[1]
            function = name.rsplit(".", 1)[-1]
            allowed = index.get(public)
            if (
                allowed
                and function in allowed["functions"]
                and 0 <= line <= allowed["lines"]
            ):
                frames.append({"file": public, "function": function, "line": line})
                if len(frames) == 24:
                    break
        if frames:
            threads.append({"frames": frames})
        if len(threads) == 16:
            break
    return {
        "outcome": "projected",
        "threads": threads,
        "nonblocking_sample_may_be_partial": True,
    }


MAX_STDERR = 8192


def classify_stderr(raw):
    """Reduce bounded private tool text to one finite reason; never return text."""
    if type(raw) is not bytes or len(raw) > MAX_STDERR:
        return "other"
    lowered = raw.decode("utf-8", errors="replace").casefold()
    patterns = (
        ("access_denied", ("access is denied", "access denied", "permission denied", "operation not permitted", "os error 5")),
        ("process_gone", ("no such process", "process has exited", "process not found", "process does not exist")),
        ("interpreter_not_found", ("failed to find a python interpreter", "failed to find python interpreter", "failed to locate the python interpreter", "not a python process", "failed to get python version")),
        ("unsupported_version", ("unsupported python version", "unsupported version of python", "python version is not supported", "unknown python version")),
        ("invalid_arguments", ("unexpected argument", "unrecognized option", "invalid argument", "usage: py-spy", "required arguments", "invalid value")),
        ("parse_failure", ("failed to parse", "parse error", "invalid json", "failed to deserialize")),
    )
    for reason, needles in patterns:
        if any(needle in lowered for needle in needles):
            return reason
    return "other"


def stderr_note(collected):
    if not collected or type(collected[0]) is not bytes:
        return {"failure_reason": "other", "stderr_sha256": None}
    raw = collected[0]
    reason = classify_stderr(raw)
    bounded = raw[:MAX_STDERR]
    return {"failure_reason": reason, "stderr_sha256": hashlib.sha256(bounded).hexdigest()}


def sample_once(executable, owned_process, index, *, timeout=0.8):
    """Invoke from a diagnostic worker during the unchanged original request.

    This function has no retry and never accepts an arbitrary numeric PID.
    executable must already have passed prepare_executable before the phase.
    Caller retains and joins its own worker after the original request has ended.
    """
    if timeout != 0.8:
        raise ValueError("Sampler budget cannot change")
    try:
        if not owned_process.is_running():
            return {"outcome": "owned_identity_exited", "threads": []}
        process = subprocess.Popen(
            [
                str(executable),
                "dump",
                "--pid",
                str(owned_process.pid),
                "--json",
                "--nonblocking",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:  # noqa: BLE001 -- diagnostic errors must not expose private text
        return {"outcome": "sampler_start_failed", "threads": []}
    collected = []
    reader_failed = []
    reader_cleanup_failed = []

    def read():
        try:
            collected.append(process.stdout.read(MAX_RAW + 1))
        except Exception:  # noqa: BLE001 -- diagnostic errors must not expose private text
            reader_failed.append(True)
        finally:
            try:
                process.stdout.close()
            except Exception:
                reader_cleanup_failed.append(True)

    stderr_collected = []

    def read_stderr():
        try:
            stderr_collected.append(process.stderr.read(MAX_STDERR + 1))
        except Exception:
            reader_failed.append(True)
        finally:
            try:
                process.stderr.close()
            except Exception:
                reader_cleanup_failed.append(True)

    stderr_reader = threading.Thread(target=read_stderr, daemon=True)
    stderr_started = False
    reader = threading.Thread(target=read, daemon=True)
    reader_started = False
    timed_out = False
    cleanup_ok = True
    operation_failed = False
    try:
        reader.start()
        reader_started = True
        stderr_reader.start()
        stderr_started = True
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
    except Exception:  # noqa: BLE001 -- diagnostic failures keep fixed output
        operation_failed = True
    finally:
        try:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=0.2)
        except Exception:  # noqa: BLE001 -- cleanup never emits native private errors
            cleanup_ok = False
        if reader_started:
            reader.join(timeout=0.2)
        else:
            try:
                process.stdout.close()
            except Exception:
                reader_cleanup_failed.append(True)
        if stderr_started:
            stderr_reader.join(timeout=0.2)
        else:
            try:
                process.stderr.close()
            except Exception:
                reader_cleanup_failed.append(True)
    if not cleanup_ok or reader_cleanup_failed or (stderr_started and stderr_reader.is_alive()) or (reader_started and reader.is_alive()):
        return {"outcome": "sampler_cleanup_incomplete", "threads": []}
    if operation_failed:
        return {"outcome": "sampler_wait_failed", "threads": []}
    if timed_out:
        return {"outcome": "sampler_timeout", "threads": []}
    if reader.is_alive() or reader_failed or not collected:
        return {"outcome": "sampler_read_failed", "threads": []}
    if process.returncode != 0:
        return {"outcome": "sampler_failed", "threads": [], **stderr_note(stderr_collected)}
    try:
        if not owned_process.is_running():
            return {"outcome": "owned_identity_exited", "threads": []}
    except Exception:  # noqa: BLE001 -- diagnostic errors must not expose private text
        return {"outcome": "owned_identity_inspection_failed", "threads": []}
    return project(collected[0], index)
