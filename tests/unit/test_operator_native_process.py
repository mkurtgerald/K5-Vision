from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from k5vision import operator_native_process as native


def _wait_for_file(path: Path, process: native.OwnedNativeProcess) -> str:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if path.is_file():
            return path.read_text()
        if process.poll() is not None:
            pytest.fail("fixture exited before signaling readiness")
        time.sleep(0.02)
    pytest.fail("fixture did not become ready")


def _alive(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


@pytest.mark.skipif(os.name != "posix", reason="Linux process-group ownership")
def test_stop_reaps_owned_tree_and_leaves_foreign_process_alive(tmp_path: Path) -> None:
    child = tmp_path / "child.py"
    child.write_text("import time\ntime.sleep(120)\n")
    script = tmp_path / "service.py"
    script.write_text(
        "import json, os, signal, subprocess, sys, time\n"
        "from pathlib import Path\n"
        "child = subprocess.Popen([sys.executable, '-I', '-B', '-S', sys.argv[1]])\n"
        "def stop(signum, frame):\n"
        "    child.wait(timeout=2)\n"
        "    raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        "Path(sys.argv[2]).write_text(json.dumps({'parent': os.getpid(), 'child': child.pid}))\n"
        "while True: time.sleep(1)\n"
    )
    marker = tmp_path / "ready.json"
    foreign = subprocess.Popen(
        [sys.executable, "-I", "-B", "-S", str(child)], start_new_session=True
    )
    owned = native.OwnedNativeProcess(
        [sys.executable, "-I", "-B", "-S", str(script), str(child), str(marker)], cwd=tmp_path
    )
    try:
        pids = json.loads(_wait_for_file(marker, owned))
        assert pids["parent"] == owned.pid
        assert owned.poll() is None
        owned.stop()
        assert owned.poll() == 0
        assert not _alive(pids["parent"])
        assert not _alive(pids["child"])
        assert foreign.poll() is None
        owned.stop()
    finally:
        owned.stop()
        foreign.terminate()
        foreign.wait(timeout=3)


@pytest.mark.skipif(os.name != "posix", reason="Linux process-group ownership")
def test_exited_leader_keeps_pid_reserved_until_descendants_stop(tmp_path: Path) -> None:
    child = tmp_path / "stubborn.py"
    marker = tmp_path / "child.pid"
    child.write_text(
        "import os, signal, sys, time\nfrom pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "Path(sys.argv[1]).write_text(str(os.getpid()))\n"
        "while True: time.sleep(1)\n"
    )
    script = tmp_path / "exiting.py"
    script.write_text(
        "import subprocess, sys, time\nfrom pathlib import Path\n"
        "subprocess.Popen([sys.executable, '-I', '-B', '-S', sys.argv[1], sys.argv[2]])\n"
        "while not Path(sys.argv[2]).is_file(): time.sleep(0.01)\n"
    )
    owned = native.OwnedNativeProcess(
        [sys.executable, "-I", "-B", "-S", str(script), str(child), str(marker)], cwd=tmp_path
    )
    try:
        child_pid = int(_wait_for_file(marker, owned))
        deadline = time.monotonic() + 5
        while owned.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert owned.poll() == 0
        assert psutil.Process(owned.pid).status() == psutil.STATUS_ZOMBIE
        assert _alive(child_pid)
        owned.stop()
        deadline = time.monotonic() + 3
        while _alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not _alive(child_pid)
        assert not _alive(owned.pid)
    finally:
        owned.stop()


@pytest.mark.skipif(os.name != "posix", reason="Linux process-group ownership")
def test_stubborn_service_is_force_stopped_with_bounded_wait(tmp_path: Path, monkeypatch) -> None:
    marker = tmp_path / "ready"
    script = tmp_path / "ignore_term.py"
    script.write_text(
        "import signal, sys, time\nfrom pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "Path(sys.argv[1]).write_text('ready')\n"
        "while True: time.sleep(1)\n"
    )
    monkeypatch.setattr(native, "_GRACE_SECONDS", 0.1)
    owned = native.OwnedNativeProcess(
        [sys.executable, "-I", "-B", "-S", str(script), str(marker)], cwd=tmp_path
    )
    try:
        _wait_for_file(marker, owned)
        started = time.monotonic()
        owned.stop()
        assert time.monotonic() - started < 3
        assert owned.poll() == -signal.SIGKILL
    finally:
        owned.stop()


def _fake_windows(monkeypatch, *, fail_assignment=False, fail_gate=False, stubborn=False):
    events = []

    class Gate(io.BytesIO):
        def write(self, value):
            events.append(("gate", value))
            if fail_gate:
                raise BrokenPipeError("private OS diagnostic")
            return super().write(value)

        def close(self):
            events.append("gate_closed")
            super().close()

    class Process:
        pid = 123
        returncode = None

        def __init__(self, arguments, **kwargs):
            events.append(("popen", arguments, kwargs))
            self.stdin = Gate()

        def kill(self):
            events.append("kill")
            self.returncode = 1

        def poll(self):
            return self.returncode

        def wait(self, timeout):
            events.append(("wait", timeout))
            if stubborn and timeout == native._GRACE_SECONDS:
                raise subprocess.TimeoutExpired("private arguments", timeout)
            self.returncode = 0
            return 0

        def send_signal(self, value):
            events.append(("signal", value))

    class Job:
        def __init__(self):
            events.append("job")

        def assign(self, process):
            events.append("assign")
            if fail_assignment:
                raise native.NativeProcessError("private assignment diagnostic")

        def terminate(self):
            events.append("terminate_job")

        def close(self):
            events.append("close_job")

    monkeypatch.setattr(native, "_WINDOWS", True)
    monkeypatch.setattr(native, "_WindowsJob", Job)
    monkeypatch.setattr(native.subprocess, "Popen", Process)
    return events


def test_windows_assigns_isolated_relay_before_opening_native_gate(tmp_path: Path, monkeypatch):
    events = _fake_windows(monkeypatch)
    arguments = [sys.executable, str(tmp_path / "private-config")]
    owned = native.OwnedNativeProcess(arguments, cwd=tmp_path)
    assert events[0] == "job"
    _, relay, options = events[1]
    assert relay[:4] == [sys.executable, "-I", "-B", "-S"]
    assert Path(relay[4]).is_absolute()
    assert relay[5:] == ["--relay", *arguments]
    assert options["stdin"] == subprocess.PIPE
    assert options["stdout"] == options["stderr"] == subprocess.DEVNULL
    assert options["creationflags"] == native._CREATE_NEW_PROCESS_GROUP | native._CREATE_NO_WINDOW
    assert events[2:5] == ["assign", ("gate", native._START_GATE), "gate_closed"]
    assert owned.pid == 123
    owned.stop()
    assert events[-4:] == [
        ("signal", getattr(signal, "CTRL_BREAK_EVENT", 1)),
        ("wait", native._GRACE_SECONDS),
        "close_job",
        ("wait", native._REAP_SECONDS),
    ]
    count = len(events)
    owned.stop()
    assert len(events) == count


@pytest.mark.parametrize("failure", ["assignment", "gate"])
def test_windows_start_failure_closes_gate_reaps_relay_and_closes_job(
    tmp_path: Path, monkeypatch, failure: str
):
    events = _fake_windows(
        monkeypatch, fail_assignment=failure == "assignment", fail_gate=failure == "gate"
    )
    with pytest.raises(native.NativeProcessError, match="^Native process could not be started.$"):
        native.OwnedNativeProcess([sys.executable], cwd=tmp_path)
    if failure == "assignment":
        assert not any(isinstance(value, tuple) and value[0] == "gate" for value in events)
    assert events[-3:] == ["kill", ("wait", native._REAP_SECONDS), "close_job"]


def test_windows_force_stops_entire_job_when_graceful_wait_expires(tmp_path: Path, monkeypatch):
    events = _fake_windows(monkeypatch, stubborn=True)
    owned = native.OwnedNativeProcess([sys.executable], cwd=tmp_path)
    owned.stop()
    assert events[-3:] == ["terminate_job", "close_job", ("wait", native._REAP_SECONDS)]


@pytest.mark.parametrize("gate", [b"", b"wrong\n", native._START_GATE + b"unexpected"])
def test_relay_never_spawns_native_child_without_exact_gate(monkeypatch, gate: bytes) -> None:
    monkeypatch.setattr(native, "_WINDOWS", True)
    monkeypatch.setattr(native.sys, "stdin", io.TextIOWrapper(io.BytesIO(gate)))
    monkeypatch.setattr(
        native.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("native child started")
    )
    assert native._relay([sys.executable]) == 1


def test_relay_starts_admitted_native_child_without_a_console(monkeypatch) -> None:
    options = {}

    class Process:
        def __init__(self, arguments, **kwargs):
            assert arguments == [sys.executable]
            options.update(kwargs)

        def wait(self):
            return 37

    monkeypatch.setattr(native, "_WINDOWS", True)
    monkeypatch.setattr(native.sys, "stdin", io.TextIOWrapper(io.BytesIO(native._START_GATE)))
    monkeypatch.setattr(native.signal, "SIGBREAK", 21, raising=False)
    monkeypatch.setattr(native.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(native.subprocess, "Popen", Process)
    assert native._relay([sys.executable]) == 37
    assert options["creationflags"] == native._CREATE_NO_WINDOW
    assert options["stdin"] == options["stdout"] == options["stderr"] == subprocess.DEVNULL
    assert options["close_fds"] is True


def test_native_arguments_refuse_source_urls_before_any_launch(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        native.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("native child started")
    )
    with pytest.raises(native.NativeProcessError, match="configuration is invalid"):
        native.OwnedNativeProcess([sys.executable, "rtsp://private-camera/stream"], cwd=tmp_path)


@pytest.mark.skipif(os.name != "nt", reason="Real Windows Job inheritance and cleanup")
def test_windows_job_owns_native_child_and_descendant(tmp_path: Path) -> None:
    import ctypes
    from ctypes import wintypes

    marker = tmp_path / "native.json"
    child = tmp_path / "child.py"
    child.write_text(
        "import signal, time\nsignal.signal(signal.SIGBREAK, signal.SIG_IGN)\ntime.sleep(120)\n"
    )
    script = tmp_path / "service.py"
    script.write_text(
        "import ctypes, json, os, signal, subprocess, sys, time\nfrom pathlib import Path\n"
        "assert not ctypes.windll.kernel32.GetConsoleWindow()\n"
        "signal.signal(signal.SIGBREAK, signal.SIG_IGN)\n"
        "child = subprocess.Popen([sys.executable, '-I', '-B', '-S', sys.argv[1]])\n"
        "Path(sys.argv[2]).write_text(json.dumps({'native': os.getpid(), 'child': child.pid}))\n"
        "while True: time.sleep(1)\n"
    )
    owned = native.OwnedNativeProcess(
        [sys.executable, "-I", "-B", "-S", str(script), str(child), str(marker)], cwd=tmp_path
    )
    try:
        pids = json.loads(_wait_for_file(marker, owned))
        assert pids["native"] != owned.pid
        assert _alive(pids["native"]) and _alive(pids["child"])
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.IsProcessInJob.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.BOOL),
        ]
        api.IsProcessInJob.restype = wintypes.BOOL
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        api.CloseHandle.restype = wintypes.BOOL
        assert owned._job is not None
        for pid in pids.values():
            handle = api.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            assert handle
            try:
                member = wintypes.BOOL()
                assert api.IsProcessInJob(handle, owned._job._handle, ctypes.byref(member))
                assert member.value
            finally:
                assert api.CloseHandle(handle)
        owned.stop()
        deadline = time.monotonic() + 3
        while any(_alive(pid) for pid in pids.values()) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not any(_alive(pid) for pid in pids.values())
        assert not _alive(owned.pid)
    finally:
        owned.stop()
