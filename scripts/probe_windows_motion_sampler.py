"""Diagnostic-only real RTSP/live-motion sampling; never a beta receipt."""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
from importlib.metadata import distribution
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener

_MARKER = "K5_MOTION_CHILD_DIAGNOSTIC "
_CAPTURE_OPTIONS = {
    "default": "",
    "tcp": "rtsp_transport;tcp",
    "tcp-nobuffer": "rtsp_transport;tcp|fflags;nobuffer",
}


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
    parser.add_argument("--candidate-module", type=Path)
    parser.add_argument("--candidate-module-sha256")
    parser.add_argument("--baseline-source-head")
    parser.add_argument("--candidate-source-head")
    parser.add_argument("--untraced-only", action="store_true")
    parser.add_argument("--opencv-version")
    parser.add_argument("--capture-options", choices=tuple(_CAPTURE_OPTIONS), default="default")
    args = parser.parse_args()
    if args.candidate_module and not all(
        (args.candidate_module_sha256, args.baseline_source_head, args.candidate_source_head)
    ):
        parser.error("Candidate A/B requires pinned module bytes and both source heads")
    if args.untraced_only and (
        args.candidate_module or not args.opencv_version or not args.baseline_source_head
    ):
        parser.error(
            "Untraced matrix requires a version and source head without a candidate overlay"
        )
    options = _CAPTURE_OPTIONS[args.capture_options]
    original_options = os.environ.get("OPENCV_FFMPEG_CAPTURE_OPTIONS")
    if options:
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = options
    else:
        os.environ.pop("OPENCV_FFMPEG_CAPTURE_OPTIONS", None)
    import cv2
    import numpy

    import k5vision.operator_motion_process as motion

    package = distribution("opencv-python-headless")
    if args.opencv_version and (
        package.version != args.opencv_version
        or cv2.__version__ != args.opencv_version.rsplit(".", 1)[0]
    ):
        raise RuntimeError("Native OpenCV version does not match the requested matrix arm")
    wheel_metadata = package.read_text("WHEEL") or ""
    wheel_record = package.read_text("RECORD") or ""
    native_versions = {}
    for line in cv2.getBuildInformation().splitlines():
        match = re.match(r"\s*(avcodec|avformat|avutil|swscale):\s+(?:YES\s*)?\(?([0-9.]+)", line)
        if match:
            native_versions[match[1]] = match[2]

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
        "opencv_distribution_version": package.version,
        "requested_opencv_distribution_version": args.opencv_version,
        "wheel_record_sha256": hashlib.sha256(wheel_record.encode()).hexdigest(),
        "wheel_tags": [
            line[5:] for line in wheel_metadata.splitlines() if line.startswith("Tag: ")
        ],
        "ffmpeg_library_versions": native_versions,
        "capture_options_label": args.capture_options,
        "capture_options": options,
        "capture_options_applied": os.environ.get("OPENCV_FFMPEG_CAPTURE_OPTIONS", ""),
        "capture_options_child_env_match": True,
        "baseline_source_head": args.baseline_source_head,
        "observations": [],
        "source_observations": [],
        "child_diagnostics": [],
        "native_stderr": [],
        "cleanup_complete": False,
    }
    servers, worker, publisher = [], None, None
    original_popen, original_document = subprocess.Popen, motion._document
    module_path = Path(motion.__file__).resolve()
    original_module_bytes = module_path.read_bytes()
    source_swapped, candidate_bytes = False, None
    metadata = {}
    diagnostic_streams = []
    phase, config_path = "initialization", None
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
            if args.candidate_module or args.untraced_only:
                receipt["child_entrypoint_unchanged"] = True
                receipt["motion_child_launches"] = 0

                def observe_popen(arguments, **kwargs):
                    if "k5vision.operator_motion_process" in arguments:
                        receipt["motion_child_launches"] += 1
                        receipt["child_entrypoint_unchanged"] &= arguments == [
                            sys.executable,
                            "-I",
                            "-B",
                            "-m",
                            "k5vision.operator_motion_process",
                            "--child",
                        ]
                        environment = kwargs.get("env") or os.environ
                        receipt["capture_options_child_env_match"] &= (
                            environment.get("OPENCV_FFMPEG_CAPTURE_OPTIONS", "") == options
                        )
                    return original_popen(arguments, **kwargs)

                subprocess.Popen = observe_popen
            token = base64.b64encode(f"local:{password}".encode()).decode()

            def source_state():
                request = Request(
                    f"http://127.0.0.1:{api}/v3/paths/get/{path}",
                    headers={"Authorization": "Basic " + token},
                )
                try:
                    with build_opener(ProxyHandler({})).open(request, timeout=1) as response:
                        value = json.loads(response.read(65536))
                    detail = {
                        "sample_number": len(receipt["observations"]),
                        "ready": value.get("ready") is True,
                        "inbound_bytes": value.get("inboundBytes", value.get("bytesReceived")),
                        "outbound_bytes": value.get("outboundBytes", value.get("bytesSent")),
                    }
                    sessions_request = Request(
                        f"http://127.0.0.1:{api}/v3/rtspsessions/list",
                        headers={"Authorization": "Basic " + token},
                    )
                    with build_opener(ProxyHandler({})).open(
                        sessions_request, timeout=1
                    ) as response:
                        sessions = json.loads(response.read(65536)).get("items", [])
                    readers = [item for item in sessions if item.get("state") == "read"]
                    detail["reader_transports"] = {
                        transport: sum(item.get("transport") == transport for item in readers)
                        for transport in ("TCP", "UDP")
                    }
                    detail["reader_outbound_bytes"] = sum(
                        item.get("outboundBytes", item.get("bytesSent", 0)) for item in readers
                    )
                    receipt["source_observations"].append(detail)
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
            phase = "replacement_write"
            replacement.write_text(json.dumps(config), encoding="utf-8")
            phase = "replacement_chmod"
            replacement.chmod(0o600)
            phase = "replacement_replace"
            replacement.replace(config_path)
            receipt["recording_config_replaced"] = True
            phase = "direct_sampling"
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
            if not args.untraced_only:
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

            if args.untraced_only:
                receipt["scope"] = "original_worker_dependency_transport_matrix"
            elif args.candidate_module:
                if (
                    module_path
                    != (Path.cwd() / "src/k5vision/operator_motion_process.py").resolve()
                ):
                    raise RuntimeError("A/B source is outside the owned diagnostic checkout")
                candidate_bytes = args.candidate_module.read_bytes()
                digest = hashlib.sha256(candidate_bytes).hexdigest()
                if digest != args.candidate_module_sha256:
                    raise RuntimeError("Candidate module bytes differ from the admitted blob")
                receipt.update(
                    scope="diagnostic_mixed_candidate_child_with_unchanged_baseline_parent",
                    baseline_source_head=args.baseline_source_head,
                    candidate_source_head=args.candidate_source_head,
                    candidate_module_sha256=digest,
                )
                # No worker is live while changing this development-checkout
                # child module. The parent's imported original implementation
                # and wire parser stay unchanged; new children still use -m.
                phase = "candidate_source_overlay"
                source_swapped = True
                module_path.write_bytes(candidate_bytes)
                worker = motion.LiveMotionWorker(relay_uri)
                phase = "candidate_sampling"
                _sample(
                    worker,
                    policy,
                    motion,
                    phase="candidate_quiet_live",
                    count=120,
                    observations=receipt["observations"],
                    metadata=metadata,
                    source_state=source_state,
                    publisher=generated,
                )
            else:
                subprocess.Popen = traced_popen
                worker = motion.LiveMotionWorker(relay_uri)
                phase = "traced_sampling"
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
            phase = "source_stopped"
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
            receipt["failure_phase"] = phase
            for field in ("winerror", "hresult", "errno"):
                value = getattr(error, field, None)
                if type(value) is int:
                    receipt["failure_" + field] = value
            if config_path is not None:
                try:
                    data = config_path.read_bytes()
                    attributes = getattr(config_path.stat(), "st_file_attributes", 0)
                    receipt["configuration_after_failure"] = {
                        "readable": True,
                        "bytes": len(data),
                        "empty": not data,
                        "readonly_attribute": bool(attributes & 1),
                    }
                except OSError:
                    receipt["configuration_after_failure"] = {"readable": False}
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
            if original_options is None:
                os.environ.pop("OPENCV_FFMPEG_CAPTURE_OPTIONS", None)
            else:
                os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = original_options
            if source_swapped:
                try:
                    receipt["candidate_source_unchanged_before_restore"] = (
                        module_path.read_bytes() == candidate_bytes
                    )
                    module_path.write_bytes(original_module_bytes)
                    receipt["original_module_restored"] = (
                        module_path.read_bytes() == original_module_bytes
                    )
                    cleanup = (
                        cleanup
                        and receipt["original_module_restored"]
                        and receipt["candidate_source_unchanged_before_restore"]
                    )
                except (OSError, RuntimeError):
                    cleanup = False
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
            receipt["runtime_module_sha256_after"] = hashlib.sha256(
                module_path.read_bytes()
            ).hexdigest()
            receipt["runtime_module_unchanged"] = (
                receipt["runtime_module_sha256_after"] == receipt["runtime_module_sha256"]
            )
            receipt["cleanup_complete"] &= receipt["runtime_module_unchanged"]
            receipt["phase_summaries"] = {}
            for name in dict.fromkeys(item["phase"] for item in receipt["observations"]):
                rows = [item for item in receipt["observations"] if item["phase"] == name]
                seen_ready, post_ready_refusals = False, 0
                for item in rows:
                    if item["outcome"] == "ready":
                        seen_ready = True
                    elif seen_ready:
                        post_ready_refusals += 1
                receipt["phase_summaries"][name] = {
                    "samples": len(rows),
                    "ready": sum(item["outcome"] == "ready" for item in rows),
                    "post_ready_refusals": post_ready_refusals,
                }
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8")
    return 0 if receipt["status"] == "diagnostic_complete" and receipt["cleanup_complete"] else 1


if __name__ == "__main__":
    raise SystemExit(_child() if sys.argv[1:] == ["--child"] else main())
