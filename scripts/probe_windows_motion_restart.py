"""Bounded Windows-only decoder experiment; never a product qualification receipt."""

from __future__ import annotations

import argparse
import ast
import ctypes
import hashlib
import importlib
import importlib.util
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from importlib.metadata import version
from pathlib import Path
from uuid import UUID

BASE = "6993360f0682e41589005b0b97aa391398baa163"
ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "motion-restart-diagnostic.json"
SOURCE_PINS = {
    key: value
    for key, value in (
        (
            "src/k5vision/operator_motion.py",
            "369c036d858359d1480df62d1aa43801feea6a7fd82abb62890238c5c7158297",
        ),
        (
            "src/k5vision/operator_motion_process.py",
            "f7fded7405d00e9ec48052dbe3cf5f7b0e242861433b72ceb67a519f1f239655",
        ),
        (
            "src/k5vision/operator_native_process.py",
            "ddbba79e1606e78e877c92d540ce4a2d4cb7848c2821d8e19a8a5bbc374b5cb8",
        ),
        (
            "src/k5vision/media/mediamtx_config.py",
            "623c8f66582a125fb904d5a4a49a180da10925e6683be776dff20c1e7061c430",
        ),
        (
            "src/k5vision/media/mediamtx_provider.py",
            "46e5ee348b7c2d42cd5baa981ec48f7c92b788449474005439e903b1c9ef13c4",
        ),
        (
            "src/k5vision/domain/devices.py",
            "f3bbc0b0e6608f16b6cc5928f0ea3b9a9a36d562d4266b7db0a39403933f3800",
        ),
        (
            "src/k5vision/domain/streams.py",
            "87cb1fc91a0bab54572cc3b531e59696a9d152f48b61ed53a5bade84619f570f",
        ),
        (
            "tests/integration/motion_camera_fixture.py",
            "4ab1751e2d6fe20d6c37fac17f51d32a6c2fb2c46518aaf8b566792e8c0cc9d7",
        ),
        (
            "windows/desktop/runtime-requirements.txt",
            "9d9e94ccadbe016d051d1e84374a59e467695116e00434c087dd4580fb837837",
        ),
        (
            "windows/desktop/donors.json",
            "d85fc5ce9ec10650cb7037c282e9199cd1fe011c1c90178f4d78fa95e2dac45a",
        ),
        (
            "src/k5vision/data/mediamtx-donor-manifest.json",
            "4ff2aea3acb6a46518f308d3e0f10c11501a5f19bf6f5f6f9847db569e4fedea",
        ),
        (
            "src/k5vision/data/mediamtx-donor-test-tools.json",
            "c959a1e68716fba4bc3ff6d3ff02e9bed4ba008623f5bf47ca981dcca55ababb",
        ),
        (
            "src/k5vision/data/caddy-donor-manifest.json",
            "b37206fddeedc8e04c865525cc4b5787796c4c9bf599903640f5ed61ee2ca90e",
        ),
        (
            ".github/actions/provision-operator-tools/action.yml",
            "a75d5868c1d904d13cf588848c8e06a2b29ced4240d0795b9fa4f630ed91851c",
        ),
    )
}
ORIGINAL_POPEN = subprocess.Popen
CYCLES = 12
STDERR_LIMIT = 65536
STDERR_CATEGORIES = {
    "timeout": (b"stream timeout triggered after", b"timed out", b"operation timeout"),
    "eof": (b"end of file", b"unexpected eof", b"averror_eof"),
    "connection": (b"connection reset", b"connection refused", b"broken pipe"),
    "decode": (b"error while decoding", b"failed to decode", b"decode_slice_header error"),
    "nal": (b"invalid nal", b"missing picture in access unit", b"no start code is found"),
    "pps": (b"non-existing pps", b"pps id out of range", b"sps_id out of range"),
    "read_attempts": (b"packet read max attempts exceeded",),
    "decode_attempts": (b"frame decode max attempts exceeded",),
}


def stderr_summary(raw):
    """Discard private text; return only capped counts for fixed byte tokens."""
    if type(raw) is not bytes:
        raise ValueError("Diagnostic stderr bytes required")
    counts = dict.fromkeys((*STDERR_CATEGORIES, "other"), 0)
    for line in raw[:STDERR_LIMIT].lower().splitlines():
        if not line.strip():
            continue
        matched = False
        for category, tokens in STDERR_CATEGORIES.items():
            if any(token in line for token in tokens):
                counts[category] = min(999, counts[category] + 1)
                matched = True
        if not matched:
            counts["other"] = min(999, counts["other"] + 1)
    return {
        "bytes_scanned": min(len(raw), STDERR_LIMIT),
        "truncated": len(raw) > STDERR_LIMIT,
        "category_counts": counts,
    }


def stderr_controls():
    canary = b"rtsp://diagnostic-secret@example.invalid/private-camera-CANARY"
    for category, tokens in STDERR_CATEGORIES.items():
        for token in tokens:
            result = stderr_summary(b"prefix " + token.upper() + b" " + canary + b"\n")
            if result["category_counts"][category] != 1 or "CANARY" in json.dumps(result):
                raise ValueError("Fixed stderr category control failed")
    unknown = stderr_summary(canary + b"\n\xff\xfe\n")
    if unknown["category_counts"]["other"] != 2 or "CANARY" in json.dumps(unknown):
        raise ValueError("Private stderr negative control failed")
    trailing = stderr_summary(b"stream timeout triggered after")["category_counts"]
    if (
        trailing["timeout"] != 1
        or trailing != stderr_summary(b"stream timeout triggered after\n")["category_counts"]
    ):
        raise ValueError("Trailing stderr line control failed")
    if (
        stderr_summary(b"timed out stream timeout triggered after")["category_counts"]["timeout"]
        != 1
    ):
        raise ValueError("One-line stderr category control failed")
    if any(stderr_summary(b"\n \n\t\n")["category_counts"].values()):
        raise ValueError("Blank stderr control failed")
    capped = stderr_summary(b"x\n" * 40000)
    if (
        not capped["truncated"]
        or capped["bytes_scanned"] != STDERR_LIMIT
        or capped["category_counts"]["other"] != 999
    ):
        raise ValueError("Bounded stderr counter control failed")
    if stderr_summary(b"timed out\n" * 1500)["category_counts"]["timeout"] != 999:
        raise ValueError("Bounded known-category control failed")
    giant = stderr_summary(b"x" * STDERR_LIMIT + b"stream timeout triggered after")
    if not giant["truncated"] or giant["category_counts"]["timeout"] != 0:
        raise ValueError("Bounded stderr scan control failed")
    for malformed in (None, "private", bytearray(b"private")):
        try:
            stderr_summary(malformed)
        except ValueError:
            pass
        else:
            raise ValueError("Malformed stderr control failed")
    return 8


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def stamp(raw, index, width=320, height=240):
    """A producer ordinal independent of the decoder's local capture clock."""
    if not 0 <= index <= 65535:
        raise ValueError("Generated ordinal bound exceeded")
    value = index | (((index & 255) ^ (index >> 8) ^ 165) << 16)
    result = bytearray(raw)
    for bit in range(24):
        row = bytes((255 if value & (1 << bit) else 0,)) * 24
        for y in range(height - 12, height - 4):
            offset = (y * width + 8 + bit * 10) * 3
            result[offset : offset + 24] = row
    return bytes(result)


def ordinal(jpeg):
    import cv2
    import numpy as np

    frame = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
    if frame is None or frame.shape != (240, 320, 3):
        raise ValueError("Generated ordinal geometry differs")
    value = 0
    for bit in range(24):
        cell = frame[230:234, 10 + bit * 10 : 14 + bit * 10]
        average = float(np.median(cell))
        if not (average <= 64 or average >= 192):
            raise ValueError("Generated ordinal contrast differs")
        value |= int(average >= 192) << bit
    index, check = value & 65535, value >> 16
    if check != ((index & 255) ^ (index >> 8) ^ 165):
        raise ValueError("Generated ordinal checksum differs")
    return index


def controls():
    import cv2
    import numpy as np

    for index in (0, 1, 255, 256, 32767, 65535):
        image = np.frombuffer(stamp(bytes((220, 8, 8)) * 76800, index), np.uint8)
        ok, jpeg = cv2.imencode(".jpg", image.reshape(240, 320, 3))
        if not ok or ordinal(jpeg.tobytes()) != index:
            raise ValueError("Generated ordinal control failed")
    for bad in (b"", b"not-a-jpeg"):
        try:
            ordinal(bad)
        except (ValueError, cv2.error):
            pass
        else:
            raise ValueError("Malformed ordinal control failed")
    return 8


def free_ports(count):
    reservations = []
    try:
        for _ in range(count):
            handle = socket.socket()
            handle.bind(("127.0.0.1", 0))
            reservations.append(handle)
        return [handle.getsockname()[1] for handle in reservations]
    finally:
        for handle in reservations:
            handle.close()


def stop(process):
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def main():
    import cv2

    from k5vision.domain.devices import Device, DeviceKind, DeviceProtocol
    from k5vision.media.mediamtx_config import MediaMTXSource, generate_mediamtx_configuration
    from k5vision.media.mediamtx_provider import MediaMTXBinding, MediaMTXCredentials
    from k5vision.operator_motion_process import LiveMotionWorker, MotionProcessError

    result = {
        "schema_version": 1,
        "diagnostic_only": True,
        "product_qualification": False,
        "production_head": BASE,
        "harness_head": os.environ.get("GITHUB_SHA", ""),
        "scope": (
            "elevated caller-account source decoder experiment; "
            "not SCM identity or full installed acceptance"
        ),
        "python_distribution": "setup-python version match; embedded binary equality unproved",
        "completed": False,
        "cleanup_complete": False,
        "phase": "admission",
        "cases": [],
        "child_stderr": [],
        "telemetry_scope": (
            "private regular-file stderr capture; fixed categories only; "
            "before-call and successful-return snapshots precede cleanup; "
            "failed-return and final child summaries may include forced cleanup; "
            "producer generation/pipe-write snapshots are correlations, not RTP arrival proof"
        ),
    }
    owned, logs, publisher, worker = [], [], None, None
    temporary = None
    phase = "admission"
    try:
        if os.name != "nt" or platform.machine().lower() not in {"amd64", "x86_64"}:
            raise ValueError("Native Windows x64 required")
        if sys.version_info[:3] != (3, 12, 10):
            raise ValueError("Pinned Python differs")
        if not ctypes.windll.shell32.IsUserAnAdmin():
            raise ValueError("Elevated diagnostic caller required")
        for package, expected in (("numpy", "2.2.6"), ("opencv-python-headless", "5.0.0.93")):
            if version(package) != expected:
                raise ValueError("Pinned runtime package differs")
        for name, expected in SOURCE_PINS.items():
            if (
                hashlib.sha256((ROOT / name).read_bytes().replace(b"\r\n", b"\n")).hexdigest()
                != expected
            ):
                raise ValueError("Production source differs")
        for name in (
            "k5vision.operator_motion",
            "k5vision.operator_motion_process",
            "k5vision.operator_native_process",
            "k5vision.media.mediamtx_config",
            "k5vision.media.mediamtx_provider",
            "k5vision.domain.devices",
            "k5vision.domain.streams",
        ):
            module = importlib.import_module(name)
            expected_path = ROOT / "src" / (name.replace(".", "/") + ".py")
            if Path(module.__file__).resolve() != expected_path.resolve():
                raise ValueError("Imported production module differs")
        import k5vision.operator_motion_process as motion

        if (motion._REQUEST_SECONDS, motion._CLOSE_SECONDS, motion._MAX_AGE) != (4.0, 2.0, 2.0):
            raise ValueError("Production timeout envelope differs")
        syntax = ast.parse((ROOT / "src/k5vision/operator_motion_process.py").read_text())
        captures = [
            node
            for node in ast.walk(syntax)
            if isinstance(node, ast.Call) and ast.unparse(node.func) == "cv2.VideoCapture"
        ]
        if len(captures) != 1 or len(captures[0].args) != 3:
            raise ValueError("Production decoder call differs")
        options = captures[0].args[2].elts
        envelope = {
            ast.unparse(options[i]): ast.literal_eval(options[i + 1])
            for i in range(0, len(options), 2)
        }
        if envelope != {
            "cv2.CAP_PROP_OPEN_TIMEOUT_MSEC": 1500,
            "cv2.CAP_PROP_READ_TIMEOUT_MSEC": 1500,
            "cv2.CAP_PROP_N_THREADS": 1,
        }:
            raise ValueError("Production decoder options differ")
        build = cv2.getBuildInformation()
        if "61.19.100" not in build or "61.7.100" not in build:
            raise ValueError("Pinned Windows FFmpeg decoder differs")
        donors = json.loads((ROOT / "windows/desktop/donors.json").read_text())
        decoder = donors["decoder"]["binary"]
        decoder_file = Path(cv2.__file__).parent / "opencv_videoio_ffmpeg500_64.dll"
        if (
            decoder_file.stat().st_size != decoder["bytes"]
            or digest(decoder_file) != decoder["sha256"]
        ):
            raise ValueError("Pinned native decoder DLL differs")
        media, ffmpeg = (
            Path(os.environ["K5_MEDIAMTX_DONOR_EXE"]),
            Path(os.environ["K5_DONOR_FFMPEG"]),
        )
        manifest = json.loads((ROOT / "src/k5vision/data/mediamtx-donor-manifest.json").read_text())
        expected = next(x for x in manifest["artifacts"] if x["platform"] == "windows_amd64")
        if (
            digest(media) != expected["executable_sha256"]
            or media.stat().st_size != expected["executable_bytes"]
        ):
            raise ValueError("Pinned MediaMTX executable differs")
        result.update(
            native_windows=True,
            python="3.12.10",
            opencv="5.0.0.93",
            numpy="2.2.6",
            decoder_avcodec="61.19.100",
            decoder_avformat="61.7.100",
            media_executable_sha256=digest(media),
            ordinal_controls=controls(),
            stderr_category_controls=stderr_controls(),
            production_source_sha256=SOURCE_PINS,
            imported_production_modules_verified=True,
            timeout_envelope={
                "request_seconds": 4.0,
                "cleanup_seconds": 2.0,
                "freshness_seconds": 2.0,
                "decoder_options": envelope,
            },
            decoder_dll_sha256=decoder["sha256"],
            caller_is_admin=True,
            harness_script_sha256=hashlib.sha256(
                Path(__file__).read_bytes().replace(b"\r\n", b"\n")
            ).hexdigest(),
            harness_workflow_sha256=hashlib.sha256(
                (ROOT / ".github/workflows/windows-motion-restart-probe.yml")
                .read_bytes()
                .replace(b"\r\n", b"\n")
            ).hexdigest(),
        )
        temporary = Path(tempfile.mkdtemp(prefix="K5-motion-restart-"))
        identity = temporary.stat().st_dev, temporary.stat().st_ino
        source_port, api_port, playback_port, rtsp_port, hls_port = free_ports(5)
        spec = importlib.util.spec_from_file_location(
            "motion_probe_fixture", ROOT / "tests/integration/motion_camera_fixture.py"
        )
        fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture)
        times, timestamp_lock, original_frame = {}, threading.Lock(), fixture.generated_frame
        generation_gaps, generation_state = [], {"count": 0, "latest": None}

        def generated(mode, index, **kwargs):
            raw = stamp(original_frame(mode, index, **kwargs), index, **kwargs)
            with timestamp_lock:
                generated_at = time.monotonic()
                previous = generation_state["latest"]
                if previous is not None:
                    generation_gaps.append((generated_at, generated_at - previous))
                    if len(generation_gaps) > 1024:
                        del generation_gaps[0]
                generation_state.update(count=generation_state["count"] + 1, latest=generated_at)
                times[index] = generated_at
                if len(times) > 1024:
                    del times[min(times)]
            return raw

        fixture.generated_frame = generated
        source_config = {
            "logLevel": "error",
            "rtspAddress": f"127.0.0.1:{source_port}",
            "rtspTransports": ["tcp"],
            "rtmp": False,
            "hls": False,
            "webrtc": False,
            "srt": False,
            "moq": False,
            "playback": False,
            "authMethod": "internal",
            "authInternalUsers": [
                {
                    "user": "publisher",
                    "pass": "p" * 32,
                    "permissions": [{"action": "publish", "path": "generated"}],
                },
                {
                    "user": "reader",
                    "pass": "r" * 32,
                    "permissions": [{"action": "read", "path": "generated"}],
                },
            ],
            "paths": {"generated": {}},
        }
        (temporary / "source.json").write_text(json.dumps(source_config))
        device = Device(
            id=UUID(int=1),
            name="Generated probe",
            host="127.0.0.1",
            kind=DeviceKind.CAMERA,
            protocols={DeviceProtocol.RTSP},
        )
        binding = MediaMTXBinding("probe-site", device.id)
        selected = MediaMTXSource(
            binding, device, f"rtsp://reader:{'r' * 32}@127.0.0.1:{source_port}/generated", False
        )
        config = generate_mediamtx_configuration(
            site_id=binding.site_id,
            sources=(selected,),
            credentials=MediaMTXCredentials("worker", "w" * 32),
            recording_directory=str(temporary / "recordings"),
            api_port=api_port,
            playback_port=playback_port,
            rtsp_port=rtsp_port,
            hls_port=hls_port,
        )
        (temporary / "relay.json").write_text(config)

        def launch(name, port):
            log = (temporary / (name + ".log")).open("ab")
            logs.append(log)
            process = ORIGINAL_POPEN(
                [str(media), str(temporary / name)],
                stdout=log,
                stderr=log,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            owned.append(process)
            until = time.monotonic() + 5
            while time.monotonic() < until:
                if process.poll() is not None:
                    raise RuntimeError("Owned fixture server exited")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        return process
                except OSError:
                    time.sleep(0.02)
            raise RuntimeError("Owned fixture server not ready")

        phase = "source_start"
        launch("source.json", source_port)
        publisher = fixture.MotionPublisher(
            ffmpeg, f"rtsp://publisher:{'p' * 32}@127.0.0.1:{source_port}/generated"
        )
        until = time.monotonic() + 10
        while publisher.frames_written < 30:
            publisher.check()
            if time.monotonic() >= until:
                raise RuntimeError("Owned fixture publisher not ready")
            time.sleep(0.05)

        def source_snapshot():
            with timestamp_lock:
                observed = time.monotonic()
                latest = generation_state["latest"]
                age = observed - latest if latest is not None else None
                recent = [gap for ended, gap in generation_gaps if ended >= observed - 5]
                return {
                    "generated_frames": generation_state["count"],
                    "pipe_frames_written": publisher.frames_written,
                    "latest_generation_age_seconds": round(age, 6) if age is not None else None,
                    "largest_generation_gap_last_5_seconds": round(
                        max(recent + ([age] if age is not None else []), default=0), 6
                    ),
                }

        relay = launch("relay.json", rtsp_port)
        policy = {
            "revision": 1,
            "source_revision": "b" * 64,
            "sensitivity": 50,
            "minimum_area_percent": 0.5,
            "include": [],
            "exclude": [],
        }
        child_captures = []
        cycle = 0

        def launch_child(args, **kwargs):
            if args == [
                sys.executable,
                "-I",
                "-B",
                "-m",
                "k5vision.operator_motion_process",
                "--child",
            ]:
                if (
                    kwargs["env"].get("OPENCV_FFMPEG_CAPTURE_OPTIONS")
                    != "rtsp_transport;tcp|fflags;nobuffer"
                ):
                    raise ValueError("Original worker options differ")
                path = temporary / f"child-{cycle:02d}.stderr"
                handle = path.open("wb")
                logs.append(handle)
                child_captures.append((cycle, path, handle))
                kwargs["stderr"] = handle
            return ORIGINAL_POPEN(args, **kwargs)

        def current_stderr():
            matching = [path for owner_cycle, path, _ in child_captures if owner_cycle == cycle]
            if not matching:
                return None
            with matching[-1].open("rb") as captured:
                return stderr_summary(captured.read(STDERR_LIMIT + 1))

        motion.subprocess.Popen = launch_child
        modes = ["baseline"]
        for mode in modes:
            phase = mode
            for cycle in range(CYCLES):
                if worker is not None:
                    worker.close()
                    worker = None
                stop(relay)
                relay = launch("relay.json", rtsp_port)
                time.sleep((cycle % 6) * 0.1)
                publisher.check()
                worker = LiveMotionWorker(
                    f"rtsp://worker:{'w' * 32}@127.0.0.1:{rtsp_port}/{binding.path_name}"
                )
                cycle_started, first_ready_at = time.monotonic(), None
                for sample_number in range(3):
                    started = time.monotonic()
                    facts = {
                        "mode": mode,
                        "cycle": cycle,
                        "sample": sample_number,
                        "source_before": source_snapshot(),
                        "stderr_before_sample": current_stderr(),
                        "sample_started_since_cycle_seconds": round(started - cycle_started, 6),
                    }
                    try:
                        sample = worker.sample(policy)
                        captured_index = ordinal(sample.jpeg)
                        with timestamp_lock:
                            generated_at = times.get(captured_index)
                        if generated_at is None:
                            raise ValueError("Generated ordinal no longer retained")
                        producer_age = time.monotonic() - generated_at
                        facts.update(
                            outcome="ready",
                            producer_frame_age_seconds=round(producer_age, 6),
                            source_ordinal=captured_index,
                            source_fresh=0 <= producer_age <= 2,
                            decoder_age_seconds=sample.age_seconds,
                        )
                    except MotionProcessError as error:
                        facts.update(
                            outcome="failed",
                            phase=error.phase,
                            decoder_phase=error.decoder_phase,
                            reason=error.reason,
                        )
                    finished = time.monotonic()
                    if facts["outcome"] == "ready" and first_ready_at is None:
                        first_ready_at = finished
                    facts.update(
                        elapsed_seconds=round(finished - started, 6),
                        sample_completed_since_cycle_seconds=round(finished - cycle_started, 6),
                        seconds_since_first_ready=round(finished - first_ready_at, 6)
                        if first_ready_at is not None
                        else None,
                        source_after=source_snapshot(),
                        stderr_after_sample=current_stderr(),
                    )
                    result["cases"].append(facts)
                    if facts["outcome"] == "failed":
                        break
                    time.sleep(0.5)
                worker.close()
                worker = None
                for owner_cycle, path, handle in child_captures:
                    if owner_cycle == cycle:
                        handle.close()
                        with path.open("rb") as captured:
                            diagnostic = stderr_summary(captured.read(STDERR_LIMIT + 1))
                        result["child_stderr"].append({"cycle": cycle, **diagnostic})
                        path.unlink()
        result["completed"] = True
    except BaseException as error:
        result.update(
            error_kind=type(error).__name__
            if type(error).__name__
            in {
                "AssertionError",
                "OSError",
                "RuntimeError",
                "ValueError",
                "KeyError",
                "TimeoutError",
            }
            else "Other",
            failure_phase=phase,
        )
    finally:
        subprocess.Popen = ORIGINAL_POPEN
        clean = True
        for callback in ([worker.close] if worker else []) + (
            [publisher.close] if publisher else []
        ):
            try:
                callback()
            except BaseException:
                clean = False
        for process in reversed(owned):
            try:
                stop(process)
            except BaseException:
                clean = False
        for log in logs:
            try:
                log.close()
            except BaseException:
                clean = False
        if temporary is not None:
            try:
                if (
                    temporary.stat().st_dev,
                    temporary.stat().st_ino,
                ) != identity or temporary.is_symlink():
                    raise ValueError("Owned diagnostic root changed")
                shutil.rmtree(temporary)
            except BaseException:
                clean = False
        result["cleanup_complete"] = clean and all(p.poll() is not None for p in owned)
        result["phase"] = phase
        OUTPUT.write_text(json.dumps(result, indent=2) + "\n")
    return 0 if result["completed"] and result["cleanup_complete"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--controls", action="store_true")
    arguments = parser.parse_args()
    if arguments.controls:
        print(
            json.dumps(
                {
                    "diagnostic_controls": controls(),
                    "stderr_category_controls": stderr_controls(),
                    "product_qualification": False,
                }
            )
        )
    else:
        raise SystemExit(main())
