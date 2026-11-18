"""Private native motion protocol, freshness and bounded process ownership."""

from __future__ import annotations

import base64
import io
import json
import subprocess
import sys
import threading
import time
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from k5vision import operator_motion_process as motion

URI = "rtsp://worker:" + "x" * 32 + "@127.0.0.1:18554/k5_" + "a" * 64
JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAIBAQEBAQIBAQECAgICAgQDAgICAgUEBAMEBgUGBgYFBgYGBwkIBgcJ"
    "BwYGCAsICQoKCgoKBggLDAsKDAkKCgr/2wBDAQICAgICAgUDAwUKBwYHCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoK"
    "CgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgr/wAARCAABAAEDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAA"
    "AAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAk"
    "M2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKT"
    "lJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QA"
    "HwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdh"
    "cRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hp"
    "anN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk"
    "5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwD+f+iiigD/2Q=="
)


def policy():
    return {
        "revision": 1,
        "source_revision": "b" * 64,
        "sensitivity": 50,
        "minimum_area_percent": 0.5,
        "include": [],
        "exclude": [],
    }


def response(**changes):
    values = {
        "status": "ready",
        "width": 1,
        "height": 1,
        "preview_width": 1,
        "preview_height": 1,
        "sequence": 1,
        "motion": True,
        "age_seconds": 0.01,
        "captured_monotonic": time.monotonic(),
        "bytes": len(JPEG),
    }
    values.update(changes)
    return json.dumps(values).encode() + b"\n" + JPEG


class RecordingInput(io.BytesIO):
    def __init__(self, events):
        super().__init__()
        self.events = events
        self.recorded = bytearray()

    def write(self, value):
        self.events.append("write")
        self.recorded.extend(value)
        return super().write(value)


class Process:
    def __init__(self, output, events):
        self.stdin = RecordingInput(events)
        self.stdout = io.BytesIO(output)
        self.returncode = None
        self.events = events
        self.fail_wait = False

    def poll(self):
        return self.returncode

    def kill(self):
        self.events.append("kill")
        self.returncode = -9

    def wait(self, *, timeout):
        self.events.append("wait")
        assert 0 <= timeout <= 2
        if self.fail_wait:
            raise subprocess.TimeoutExpired("owned", timeout)
        return self.returncode


@pytest.fixture(params=["posix", "nt"], ids=["posix", "windows"])
def worker(monkeypatch, request):
    from k5vision import operator_native_process

    events, processes, arguments, jobs = [], [], [], []

    class Job:
        """Ownership double paired with Process, never a real Windows handle."""

        def __init__(self):
            self.assigned = None
            self.closed = False
            jobs.append(self)
            events.append("job")

        def assign(self, process):
            assert self.assigned is None and not self.closed
            assert process in processes and process.returncode is None
            assert process.stdin.recorded == b""
            self.assigned = process
            events.append("assign")

        def close(self):
            if not self.closed:
                assert self.assigned is None or self.assigned.returncode is not None
                self.closed = True
                events.append("job_close")

    def start(args, **kwargs):
        arguments.append((args, kwargs))
        events.append("spawn")
        process = Process(response(), events)
        processes.append(process)
        return process

    monkeypatch.setattr(
        motion,
        "subprocess",
        SimpleNamespace(
            Popen=start,
            PIPE=subprocess.PIPE,
            DEVNULL=subprocess.DEVNULL,
            TimeoutExpired=subprocess.TimeoutExpired,
            CREATE_NO_WINDOW=0x08000000 if request.param == "nt" else 0,
        ),
    )
    # Process has no native _handle. Pair its ownership double with it on
    # every host, and exercise both platform branches explicitly on Linux too.
    # The separate real-process tests retain the actual Windows Job boundary.
    monkeypatch.setattr(operator_native_process, "_WindowsJob", Job)
    monkeypatch.setattr(motion, "os", SimpleNamespace(name=request.param))
    instance = motion.LiveMotionWorker(URI)
    yield SimpleNamespace(
        instance=instance,
        events=events,
        processes=processes,
        args=arguments,
        jobs=jobs,
        job_type=Job,
        platform=request.param,
    )
    for process in processes:
        process.fail_wait = False
    instance.close()


@pytest.mark.parametrize(
    "uri",
    [
        URI.replace("127.0.0.1", "localhost"),
        URI.replace("127.0.0.1", "127.0.0.2"),
        URI.replace("rtsp:", "rtsps:"),
        URI + "?private=x",
        URI + "#fragment",
        URI.replace("/k5_", "/anything_"),
        URI.replace(":18554", ""),
        URI.replace("worker:", "any:"),
        42,
        None,
    ],
)
def test_refuses_noncanonical_or_untrusted_relay_without_leaking_it(uri):
    with pytest.raises(motion.MotionProcessError) as error:
        motion.LiveMotionWorker(uri)
    assert "127.0.0.2" not in str(error.value) and "fragment" not in str(error.value)


def test_private_source_stays_in_stdin_and_sample_is_immutable(worker):
    result = worker.instance.sample(policy())
    args, options = worker.args[0]
    assert args[1:4] == ["-I", "-B", "-m"]
    assert all(URI not in argument and "x" * 32 not in argument for argument in args)
    assert options["stderr"] is subprocess.DEVNULL and options["close_fds"] is True
    requests = [json.loads(line) for line in worker.processes[0].stdin.recorded.splitlines()]
    assert requests == [{"relay_uri": URI}, {"policy": policy()}]
    assert result.jpeg == JPEG and result.motion is True and result.sequence == 1
    assert 0 <= result.age_seconds <= 2
    with pytest.raises(FrozenInstanceError):
        result.sequence = 3


@pytest.mark.parametrize(
    "changes",
    [
        {"captured_monotonic": 0.0},
        {"captured_monotonic": 1e100},
        {"sequence": True},
        {"sequence": 0},
        {"motion": 1},
        {"bytes": True},
        {"bytes": 262145},
        {"age_seconds": -0.1},
        {"age_seconds": 2.1},
        {"age_seconds": True},
        {"width": 0},
        {"width": 8193},
        {"height": True},
        {"preview_width": 2},
        {"private": "PRIVATE_CAMERA"},
        {"status": "unavailable"},
    ],
)
def test_invalid_native_response_is_refused_and_owned_child_reaped(worker, changes):
    worker.instance._start()
    worker.processes[0].stdout = io.BytesIO(response(**changes))
    worker.instance._process.stdout = worker.processes[0].stdout
    with pytest.raises(motion.MotionProcessError, match="Motion frame is unavailable") as error:
        worker.instance.sample(policy())
    assert "PRIVATE_CAMERA" not in str(error.value)
    assert worker.processes[0].returncode == -9
    assert "wait" in worker.events
    assert worker.instance._process is None


def test_replayed_frame_cannot_retrigger_motion(worker):
    worker.instance._start()
    worker.processes[0].stdout = io.BytesIO(response() + response())
    worker.instance.sample(policy())
    with pytest.raises(motion.MotionProcessError):
        worker.instance.sample(policy())
    assert worker.processes[0].returncode == -9


def test_restarted_child_retains_public_sequence_progress(worker):
    first = worker.instance.sample(policy())
    worker.processes[0].returncode = 0
    second = worker.instance.sample(policy())
    assert first.sequence == 1 and second.sequence == 2
    assert len(worker.processes) == 2


def test_unreaped_owned_process_is_retained_for_cleanup_retry(worker):
    worker.instance.sample(policy())
    process = worker.processes[0]
    process.fail_wait = True
    with pytest.raises(motion.MotionProcessError, match="cleanup is incomplete"):
        worker.instance.close()
    assert worker.instance._process is process
    process.fail_wait = False
    worker.instance.close()
    assert worker.instance._process is None


def test_windows_job_is_assigned_before_private_source_is_written(monkeypatch, worker):
    monkeypatch.setattr(motion, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(motion.subprocess, "CREATE_NO_WINDOW", 0x08000000)
    worker.instance.sample(policy())
    assert worker.events[:4] == ["job", "spawn", "assign", "write"]
    assert worker.args[0][1]["creationflags"] == 0x08000000
    assert worker.args[0][1]["start_new_session"] is False
    assert worker.jobs[0].assigned is worker.processes[0]
    worker.instance.close()
    assert worker.jobs[0].closed is True


def test_fake_process_platforms_keep_their_ownership_boundary(worker):
    worker.instance.sample(policy())
    if worker.platform == "nt":
        assert len(worker.jobs) == 1
        assert worker.jobs[0].assigned is worker.processes[0]
        assert worker.events[:4] == ["job", "spawn", "assign", "write"]
        assert worker.args[0][1]["start_new_session"] is False
        assert worker.args[0][1]["creationflags"] == 0x08000000
    else:
        assert worker.jobs == []
        assert worker.events[:2] == ["spawn", "write"]
        assert worker.args[0][1]["start_new_session"] is True
        assert worker.args[0][1]["creationflags"] == 0


def test_refused_windows_assignment_never_grants_private_source(monkeypatch, worker):
    from k5vision import operator_native_process

    class RefusedJob(worker.job_type):
        def assign(self, process):
            worker.events.append("assign_refused")
            raise operator_native_process.NativeProcessError("PRIVATE_NATIVE_ERROR")

    monkeypatch.setattr(operator_native_process, "_WindowsJob", RefusedJob)
    monkeypatch.setattr(motion, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(motion.subprocess, "CREATE_NO_WINDOW", 0x08000000)
    with pytest.raises(motion.MotionProcessError, match="ownership is unavailable") as error:
        worker.instance.sample(policy())
    assert "PRIVATE_NATIVE_ERROR" not in str(error.value)
    assert worker.processes[0].stdin.recorded == b""
    assert worker.processes[0].returncode == -9
    assert worker.events == ["job", "spawn", "assign_refused", "kill", "wait", "job_close"]
    assert worker.jobs[0].closed is True
    assert worker.instance._process is None and worker.instance._job is None


def test_real_windows_job_assignment_refuses_a_process_without_native_handle():
    from k5vision import operator_native_process

    owner = object.__new__(operator_native_process._WindowsJob)
    calls = []
    owner._handle = 123
    owner._api = SimpleNamespace(AssignProcessToJobObject=lambda *args: calls.append(args))
    process = Process(response(), [])
    with pytest.raises(AttributeError, match="_handle"):
        owner.assign(process)
    assert calls == [] and process.stdin.recorded == b""


def test_real_windows_job_assignment_refuses_native_api_rejection():
    from k5vision import operator_native_process

    owner = object.__new__(operator_native_process._WindowsJob)
    calls = []
    owner._handle = 123

    def refuse(*args):
        calls.append(args)
        return False

    owner._api = SimpleNamespace(AssignProcessToJobObject=refuse)
    process = Process(response(), [])
    process._handle = 456
    with pytest.raises(operator_native_process.NativeProcessError, match="ownership"):
        owner.assign(process)
    assert calls == [(123, 456)] and process.stdin.recorded == b""


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("revision", True),
        ("source_revision", "PRIVATE_SOURCE"),
        ("sensitivity", 0),
        ("sensitivity", 101),
        ("minimum_area_percent", 0.09),
        ("minimum_area_percent", True),
        ("include", [[[0, 0], [0, 1]]]),
        ("exclude", [[[0, 0], [0, 1], [2, 1]]]),
    ],
)
def test_invalid_policies_never_start_native_capture(worker, field, value):
    item = policy()
    item[field] = value
    with pytest.raises(motion.MotionProcessError):
        worker.instance.sample(item)
    assert worker.processes == []


def test_total_polygon_limit_is_shared_between_inclusions_and_exclusions(worker):
    item = policy()
    polygon = [[0, 0], [0, 1], [1, 1]]
    item["include"], item["exclude"] = [polygon] * 5, [polygon] * 4
    with pytest.raises(motion.MotionProcessError):
        worker.instance.sample(item)
    assert worker.processes == []


def test_actual_blocked_child_is_killed_at_the_request_deadline(monkeypatch):
    real_start = subprocess.Popen
    owned = []

    def start(_args, **_kwargs):
        child = real_start(
            [sys.executable, "-I", "-B", "-c", "import sys; sys.stdin.buffer.read()"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        owned.append(child)
        return child

    monkeypatch.setattr(motion.subprocess, "Popen", start)
    monkeypatch.setattr(motion, "_REQUEST_SECONDS", 0.15)
    worker = motion.LiveMotionWorker(URI)
    started = time.monotonic()
    try:
        with pytest.raises(motion.MotionProcessError):
            worker.sample(policy())
        assert time.monotonic() - started < 2
        assert owned[0].poll() is not None
    finally:
        worker.close()
    assert not worker._deadline._thread.is_alive()


def test_begin_close_interrupts_an_active_native_read(monkeypatch):
    real_start = subprocess.Popen
    launched = threading.Event()

    def start(_args, **_kwargs):
        child = real_start(
            [sys.executable, "-I", "-B", "-c", "import sys; sys.stdin.buffer.read()"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        launched.set()
        return child

    monkeypatch.setattr(motion.subprocess, "Popen", start)
    worker = motion.LiveMotionWorker(URI)
    outcome = []

    def sample():
        try:
            worker.sample(policy())
        except motion.MotionProcessError:
            outcome.append("closed")

    thread = threading.Thread(target=sample)
    thread.start()
    assert launched.wait(1)
    worker.begin_close()
    worker.close()
    thread.join(timeout=1)
    assert not thread.is_alive() and outcome == ["closed"]


def test_child_continuously_drains_fresh_frames_without_recording_files(monkeypatch):
    class Vector(bytes):
        def tobytes(self):
            return bytes(self)

    class Capture:
        def __init__(self, uri, backend, options):
            assert uri == URI and backend == 1 and options == [2, 1500, 3, 1500, 4, 1]
            self.reads = 0
            self.released = False
            captures.append(self)

        def isOpened(self):
            return True

        def read(self):
            time.sleep(0.003)
            self.reads += 1
            return True, SimpleNamespace(shape=(1, 1, 3), ndim=3)

        def release(self):
            self.released = True

    class Detector:
        def observe(self, frame, item):
            assert item == policy() and frame.shape == (1, 1, 3)
            time.sleep(0.02)
            return True

    captures, threads = [], []
    cv2 = SimpleNamespace(
        VideoCapture=Capture,
        CAP_FFMPEG=1,
        CAP_PROP_OPEN_TIMEOUT_MSEC=2,
        CAP_PROP_READ_TIMEOUT_MSEC=3,
        CAP_PROP_N_THREADS=4,
        INTER_AREA=5,
        IMWRITE_JPEG_QUALITY=6,
        setNumThreads=threads.append,
        resize=lambda frame, dimensions, **_kwargs: frame,
        imencode=lambda *_args: (True, Vector(JPEG)),
    )
    incoming = io.BytesIO(
        json.dumps({"relay_uri": URI}).encode()
        + b"\n"
        + (json.dumps({"policy": policy()}).encode() + b"\n") * 2
    )
    outgoing = io.BytesIO()
    monkeypatch.setitem(sys.modules, "cv2", cv2)
    monkeypatch.setitem(
        sys.modules, "k5vision.operator_motion", SimpleNamespace(SceneMotionDetector=Detector)
    )
    monkeypatch.setattr(
        motion,
        "sys",
        SimpleNamespace(
            stdin=SimpleNamespace(buffer=incoming), stdout=SimpleNamespace(buffer=outgoing)
        ),
    )
    assert motion._child_main() == 0
    outgoing.seek(0)
    samples = []
    for _ in range(2):
        header = json.loads(outgoing.readline())
        assert outgoing.read(header["bytes"]) == JPEG
        samples.append(header)
    assert samples[1]["sequence"] > samples[0]["sequence"]
    assert captures[0].reads > 2 and captures[0].released
    assert threads == [1]


def test_jpeg_frame_header_geometry_is_checked():
    assert motion._jpeg_geometry(JPEG) == (1, 1)
    assert motion._jpeg_geometry(b"\xff\xd8INVALID\xff\xd9") is None
    assert motion._jpeg_geometry(JPEG[:-1]) is None
