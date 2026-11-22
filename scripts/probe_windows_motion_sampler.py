"""Diagnostic-only real RTSP/live-motion sampling; never a beta receipt."""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener

_MARKER = "K5_MOTION_CHILD_DIAGNOSTIC "


def _child():
    import k5vision.operator_motion_process as motion

    events = []
    target = motion.__file__

    def trace(frame, event, arg):
        if frame.f_code.co_filename != target:
            return None
        if event == "exception" and len(events) < 16:
            kind = arg[0].__name__
            if kind in {
                "ValueError",
                "OSError",
                "MotionProcessError",
                "MotionUnavailable",
                "error",
                "AttributeError",
                "TypeError",
                "SystemError",
                "MemoryError",
            }:
                values = frame.f_locals
                note = {
                    "function": frame.f_code.co_name,
                    "line": frame.f_lineno,
                    "kind": kind,
                }
                if frame.f_code.co_name == "capture_frames":
                    note["opened"] = values.get("capture") is not None
                    note["read_ready"] = values.get("ready") is True
                    note["sequence"] = values.get("sequence", 0)
                if frame.f_code.co_name == "_child_main":
                    note["source_failed"] = values.get("failed") is True
                    note["last_sequence"] = values.get("last_sequence", 0)
                    captured = values.get("captured")
                    if type(captured) in (float, int):
                        note["frame_age_seconds"] = round(time.monotonic() - captured, 4)
                events.append(note)
                # Production responds unavailable before its finally block;
                # its owner may reap it immediately after that response. Emit
                # this finite cause before the response can race process exit.
                sys.stderr.write(_MARKER + json.dumps({"events": [note]}) + "\n")
                sys.stderr.flush()
        return trace

    sys.settrace(trace)
    threading.settrace(trace)
    try:
        return motion._child_main()
    finally:
        sys.settrace(None)
        threading.settrace(None)
        sys.stderr.write(_MARKER + json.dumps({"events": events}) + "\n")
        sys.stderr.flush()


def _port():
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        return reserved.getsockname()[1]


def _load_fixture():
    path = Path.cwd() / "tests/integration/motion_camera_fixture.py"
    spec = importlib.util.spec_from_file_location("motion_sampler_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sample(
    worker, policy, motion, *, phase, count, observations, metadata, source_state, publisher
):
    for index in range(count):
        metadata.clear()
        began = time.monotonic()
        note = {"phase": phase, "index": index}
        try:
            result = worker.sample(policy)
            note.update(
                outcome="ready",
                sequence=result.sequence,
                age_seconds=round(result.age_seconds, 4),
                motion=result.motion,
                frame_fresh=result.age_seconds <= 2,
            )
        except motion.MotionProcessError as error:
            allowed = {
                "Motion frame is unavailable": "frame_unavailable",
                "Motion worker ownership is unavailable": "worker_ownership",
                "Motion worker cleanup is incomplete": "worker_cleanup",
            }
            note.update(outcome="refused", reason=allowed.get(str(error), "other"))
        note["duration_seconds"] = round(time.monotonic() - began, 4)
        if metadata:
            note["last_response"] = dict(metadata)
        note["source_state"] = source_state()
        note["publisher_frames_written"] = publisher.frames_written
        observations.append(note)
        time.sleep(0.5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    import cv2
    import numpy

    import k5vision.operator_motion_process as motion

    fixture = _load_fixture()
    donor = Path(os.environ["K5_MEDIAMTX_DONOR_EXE"]).resolve(strict=True)
    ffmpeg = Path(os.environ["K5_DONOR_FFMPEG"]).resolve(strict=True)
    receipt = {
        "schema_version": 1,
        "status": "infrastructure_failure",
        "diagnostic_only": True,
        "scope": "actual_worker_without_product_service_readiness",
        "runtime_module_sha256": hashlib.sha256(Path(motion.__file__).read_bytes()).hexdigest(),
        "opencv": cv2.__version__,
        "numpy": numpy.__version__,
        "observations": [],
        "child_diagnostics": [],
        "native_stderr": [],
        "cleanup_complete": False,
    }
    servers, worker, publisher = [], None, None
    original_popen, original_document = subprocess.Popen, motion._document
    metadata = {}
    diagnostic_streams = []
    with tempfile.TemporaryDirectory(prefix="k5-motion-sampler-") as temporary:
        root = Path(temporary)
        try:
            path = "k5_" + "a" * 64
            password = secrets.token_urlsafe(32)
            upstream, downstream, api = _port(), _port(), _port()
            credentials = {"user": "local", "pass": password, "ips": ["127.0.0.1"]}
            publish_uri = f"rtsp://local:{password}@127.0.0.1:{upstream}/{path}"
            relay_uri = f"rtsp://local:{password}@127.0.0.1:{downstream}/{path}"
            for index, port in enumerate((upstream, downstream)):
                config = {
                    "logLevel": "warn",
                    "authInternalUsers": [
                        {
                            **credentials,
                            "permissions": [
                                {"action": "read", "path": path},
                                {"action": "publish", "path": path},
                                {"action": "api"},
                            ],
                        }
                    ],
                    "rtspAddress": f"127.0.0.1:{port}",
                    "rtspTransports": ["tcp"],
                    "rtmp": False,
                    "hls": False,
                    "webrtc": False,
                    "srt": False,
                    "moq": False,
                    "paths": {
                        path: {"source": "publisher"}
                        if index == 0
                        else {
                            "source": publish_uri,
                            "sourceOnDemand": False,
                            "rtspTransport": "tcp",
                            "record": True,
                            "recordPath": str(root / "recordings/%path/%Y-%m-%d_%H-%M-%S-%f"),
                            "recordPartDuration": "1s",
                            "recordSegmentDuration": "60s",
                            "recordDeleteAfter": "0s",
                            "useAbsoluteTimestamp": False,
                        }
                    },
                }
                if index == 1:
                    config.update(api=True, apiAddress=f"127.0.0.1:{api}")
                config_path = root / f"relay-{index}.json"
                config_path.write_text(json.dumps(config), encoding="utf-8")
                config_path.chmod(0o600)
                process = original_popen(
                    [str(donor), str(config_path)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                job = None
                servers.append((process, job))
                if os.name == "nt":
                    from k5vision.operator_native_process import _WindowsJob

                    job = _WindowsJob()
                    servers[-1] = process, job
                    job.assign(process)
            time.sleep(0.4)
            if any(process.poll() is not None for process, _job in servers):
                raise RuntimeError("Owned relay did not start")
            publisher = fixture.MotionPublisher(ffmpeg, publish_uri)
            generated = publisher
            policy = {
                "revision": 1,
                "source_revision": "1" * 64,
                "sensitivity": 50,
                "minimum_area_percent": 0.1,
                "include": [],
                "exclude": [],
            }

            def document(raw):
                value = original_document(raw)
                if "status" in value:
                    metadata.clear()
                    metadata.update(
                        {
                            key: value[key]
                            for key in ("status", "sequence", "age_seconds")
                            if key in value
                        }
                    )
                    captured = value.get("captured_monotonic")
                    if type(captured) in (int, float):
                        metadata["received_age_seconds"] = round(time.monotonic() - captured, 4)
                return value

            motion._document = document
            token = base64.b64encode(f"local:{password}".encode()).decode()

            def source_state():
                request = Request(
                    f"http://127.0.0.1:{api}/v3/paths/get/{path}",
                    headers={"Authorization": "Basic " + token},
                )
                try:
                    with build_opener(ProxyHandler({})).open(request, timeout=1) as response:
                        value = json.loads(response.read(65536))
                    return "ready" if value.get("ready") is True else "not_ready"
                except (OSError, ValueError):
                    return "request_failed"

            worker = motion.LiveMotionWorker(relay_uri)
            _sample(
                worker,
                policy,
                motion,
                phase="recording_live",
                count=3,
                observations=receipt["observations"],
                metadata=metadata,
                source_state=source_state,
                publisher=generated,
            )
            request = Request(
                f"http://127.0.0.1:{api}/v3/config/paths/patch/{path}",
                data=b'{"record":false}',
                method="PATCH",
                headers={"Content-Type": "application/json", "Authorization": "Basic " + token},
            )
            with build_opener(ProxyHandler({})).open(request, timeout=3) as response:
                receipt["recording_toggle_http_status"] = response.status
                response.read(4096)
            config["paths"][path]["record"] = False
            replacement = root / "relay-new.json"
            replacement.write_text(json.dumps(config), encoding="utf-8")
            replacement.chmod(0o600)
            replacement.replace(config_path)
            receipt["recording_config_replaced"] = True
            _sample(
                worker,
                policy,
                motion,
                phase="quiet_live",
                count=120,
                observations=receipt["observations"],
                metadata=metadata,
                source_state=source_state,
                publisher=generated,
            )
            worker.close()
            worker = None

            def traced_popen(arguments, **kwargs):
                if arguments[1:] == [
                    "-I",
                    "-B",
                    "-m",
                    "k5vision.operator_motion_process",
                    "--child",
                ]:
                    stream = (root / f"child-{len(diagnostic_streams)}.log").open("wb")
                    diagnostic_streams.append(stream)
                    kwargs["stderr"] = stream
                    arguments = [
                        sys.executable,
                        "-I",
                        "-B",
                        str(Path(__file__).resolve()),
                        "--child",
                    ]
                return original_popen(arguments, **kwargs)

            subprocess.Popen = traced_popen
            worker = motion.LiveMotionWorker(relay_uri)
            _sample(
                worker,
                policy,
                motion,
                phase="traced_quiet_live",
                count=24,
                observations=receipt["observations"],
                metadata=metadata,
                source_state=source_state,
                publisher=generated,
            )
            publisher.close()
            publisher = None
            _sample(
                worker,
                policy,
                motion,
                phase="source_stopped",
                count=3,
                observations=receipt["observations"],
                metadata=metadata,
                source_state=source_state,
                publisher=generated,
            )
            receipt["status"] = "diagnostic_complete"
        except BaseException as error:
            receipt["failure_kind"] = type(error).__name__
        finally:
            cleanup = True
            for action in (
                lambda: worker.close() if worker is not None else None,
                lambda: publisher.close() if publisher is not None else None,
            ):
                try:
                    action()
                except BaseException:
                    cleanup = False
            subprocess.Popen = original_popen
            motion._document = original_document
            for process, job in reversed(servers):
                try:
                    if job is not None:
                        job.close()
                    elif process.poll() is None:
                        process.terminate()
                    process.wait(timeout=5)
                except BaseException:
                    cleanup = False
            for stream in diagnostic_streams:
                stream.close()
            for path in sorted(root.glob("child-*.log")):
                native_text = path.read_bytes()[:65536].decode("utf-8", errors="replace")
                lowered = native_text.lower()
                receipt["native_stderr"].append(
                    {
                        "child_index": int(path.stem.split("-")[1]),
                        "stream_timeout": "stream timeout triggered" in lowered,
                        "rtsp_describe_failed": "method describe failed" in lowered,
                        "decoder_error": "error while decoding" in lowered,
                        "demux_error": "error during demuxing" in lowered,
                        "capture_backend_refused": "can't be used to capture by name" in lowered,
                    }
                )
                for line in native_text.splitlines():
                    if line.startswith(_MARKER):
                        receipt["child_diagnostics"].append(json.loads(line[len(_MARKER) :]))
            receipt["cleanup_complete"] = cleanup
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8")
    return 0 if receipt["status"] == "diagnostic_complete" and receipt["cleanup_complete"] else 1


if __name__ == "__main__":
    raise SystemExit(_child() if sys.argv[1:] == ["--child"] else main())
