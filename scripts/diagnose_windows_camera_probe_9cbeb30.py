"""Generated-source FFprobe sampler diagnostic; never a release qualification."""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import platform
import socket
import subprocess
import tempfile
import time
from pathlib import Path

SOURCE_HEAD = "9cbeb30471d28c6a2f03eabaa9f1b8602c9c92f0"
LIMIT = 262144


def require(value):
    if not value:
        raise ValueError("Generated probe diagnostic condition failed")


def module(name):
    path = Path("tests/integration") / (name + ".py")
    spec = importlib.util.spec_from_file_location("probe_diagnostic_" + name, path)
    require(spec is not None and spec.loader is not None)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def summarize(raw):
    info = {"stdout_bytes": len(raw), "reason": "invalid_json"}
    if len(raw) > LIMIT:
        return {**info, "reason": "output_limit"}
    try:
        data = json.loads(raw)
        streams, frames = data["streams"], data["frames"]
        if len(streams) != 1:
            return {**info, "reason": "stream_count"}
        stream = streams[0]
        info.update(
            codec=stream["codec_name"] if stream["codec_name"] in {"h264", "mpeg4"} else "other",
            width=int(stream["width"]),
            height=int(stream["height"]),
            frame_count=len(frames),
        )
        missing = [i for i, frame in enumerate(frames) if "best_effort_timestamp_time" not in frame]
        times = [
            float(frame["best_effort_timestamp_time"])
            for frame in frames
            if "best_effort_timestamp_time" in frame
        ]
        info.update(timed_count=len(times), untimed_count=len(missing))
        if not all(math.isfinite(item) for item in times):
            return {**info, "reason": "nonfinite_pts"}
        if times:
            info.update(first_pts=times[0], last_pts=times[-1])
        if len(times) > 1:
            deltas = [b - a for a, b in zip(times[:-1], times[1:], strict=True)]
            info.update(min_delta=min(deltas), max_delta=max(deltas))
            if times[-1] != times[0]:
                info["measured_fps"] = round((len(times) - 1) / (times[-1] - times[0]), 3)
        if not 4 <= len(frames) <= 10000 or len(times) < 4:
            return {**info, "reason": "insufficient_frames"}
        if missing != list(range(len(missing))) or len(missing) > 2:
            return {**info, "reason": "untimed_frames"}
        if any(b <= a for a, b in zip(times[:-1], times[1:], strict=True)):
            return {**info, "reason": "nonmonotonic_pts"}
        if any(
            frame.get("width") != stream["width"] or frame.get("height") != stream["height"]
            for frame in frames
        ):
            return {**info, "reason": "frame_geometry"}
        return {**info, "reason": "valid_pts"}
    except (ValueError, KeyError, TypeError, ZeroDivisionError):
        return info


def sample(probe, ffprobe, source, variant, phase):
    original_run = subprocess.run
    record = {"variant": variant, "phase": phase, "reason": "not_started", "timed_out": False}

    def observe(command, **kwargs):
        require(kwargs.get("timeout") == 8)
        if variant == "bounded_analysis":
            command = [
                command[0],
                "-analyzeduration",
                "1000000",
                "-probesize",
                "1048576",
                *command[1:],
            ]
        began = time.monotonic()
        try:
            completed = original_run(command, **kwargs)
            record.update(return_code=completed.returncode, **summarize(completed.stdout))
            if completed.returncode != 0:
                record["reason"] = "nonzero_exit"
            return completed
        except subprocess.TimeoutExpired as error:
            record.update(summarize(error.stdout or b""))
            record.update(timed_out=True, reason="timeout")
            raise
        finally:
            record["elapsed_seconds"] = round(time.monotonic() - began, 3)

    subprocess.run = observe
    try:
        value = probe._probe(ffprobe, source)
        record["parser_accepted"] = value is not None
        expected = {"codec": "h264", "width": 320, "height": 240, "fps": 15.0}
        record["expected_source_verified"] = value is not None and all(
            value[k] == v for k, v in expected.items()
        )
        if value is not None:
            record["original_parser_result"] = value
            if not record["expected_source_verified"]:
                record["reason"] = "expected_value_mismatch"
        elif record["reason"] == "valid_pts":
            record["reason"] = "original_parser_rejected"
    except subprocess.TimeoutExpired:
        record.update(parser_accepted=False, expected_source_verified=False)
    finally:
        subprocess.run = original_run
    return record


def publisher_ready(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5) as connection:
            connection.settimeout(0.5)
            request = (
                f"DESCRIBE rtsp://127.0.0.1:{port}/generated RTSP/1.0\r\n"
                "CSeq: 1\r\nAccept: application/sdp\r\n\r\n"
            )
            connection.sendall(request.encode("ascii"))
            return connection.recv(4096).startswith(b"RTSP/1.0 200")
    except OSError:
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allow-non-windows-regression", action="store_true")
    args = parser.parse_args()
    require(os.name == "nt" or args.allow_non_windows_regression)
    source_files = [
        "src/k5vision",
        "pyproject.toml",
        "tests/integration/test_windows_desktop_app.py",
        "tests/integration/test_operator_product_runtime.py",
        "tests/integration/test_mediamtx_donor_runtime.py",
        "tests/integration/test_windows_camera_video_settings.py",
    ]
    require(
        not subprocess.check_output(
            ["git", "diff", "--name-only", SOURCE_HEAD, "--", *source_files], text=True
        ).strip()
    )
    checkout = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    desktop, runtime, probe = (
        module(name)
        for name in (
            "test_windows_desktop_app",
            "test_operator_product_runtime",
            "test_windows_camera_video_settings",
        )
    )
    helpers = runtime._helpers()
    media = Path(os.environ["K5_MEDIAMTX_DONOR_EXE"]).resolve(strict=True)
    ffmpeg = Path(os.environ["K5_DONOR_FFMPEG"]).resolve(strict=True)
    ffprobe = Path(os.environ["K5_DONOR_FFPROBE"]).resolve(strict=True)
    version = (
        subprocess.run(
            [str(ffprobe), "-version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=8,
            check=True,
        )
        .stdout.decode("utf-8")
        .splitlines()[0]
    )
    require(version.startswith("ffprobe version "))
    if os.name == "nt":
        require(version.startswith("ffprobe version 9.0.2"))
    owned, results = [], []
    start = helpers._start

    def tracked_start(*positional, **keywords):
        process = start(*positional, **keywords)
        owned.append(process)
        return process

    helpers._start = tracked_start
    cleanup = False
    try:
        with tempfile.TemporaryDirectory(prefix="K5 generated probe diagnostic-") as raw:
            parent = Path(raw)
            reservations, ports = helpers._reserve_ports(1)
            for listener in reservations:
                listener.close()
            try:
                _processes, source = desktop._generate_fixture(
                    parent, media, ffmpeg, ports[0], helpers
                )
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and not publisher_ready(ports[0]):
                    time.sleep(0.1)
                require(publisher_ready(ports[0]))
                for phase, pause in enumerate((0, 0.35, 0.85)):
                    time.sleep(pause)
                    for variant in ("original", "bounded_analysis"):
                        result = sample(probe, ffprobe, source, variant, phase)
                        results.append(result)
                        print(json.dumps({"probe_trial": result}, allow_nan=False), flush=True)
            finally:
                for process in reversed(owned):
                    helpers._stop_owned(process)
                cleanup = all(process.poll() is not None for process in owned)
    finally:
        helpers._start = start
        for process in reversed(owned):
            helpers._stop_owned(process)
    report = {
        "schema_version": 1,
        "source_head": SOURCE_HEAD,
        "checkout_head": checkout,
        "platform": platform.system(),
        "native_windows_execution": os.name == "nt",
        "purpose": "generated_ffprobe_diagnostic_only",
        "release_qualification": False,
        "production_source_unchanged": True,
        "original_parser_unchanged": True,
        "generated_media_only": True,
        "ffprobe_version": version,
        "timeout_seconds": 8,
        "join_phases": 3,
        "owned_cleanup": cleanup,
        "results": results,
    }
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(report, output, indent=2, allow_nan=False)
    print(json.dumps(report, allow_nan=False))
    return 0 if cleanup and len(results) == 6 else 1


if __name__ == "__main__":
    raise SystemExit(main())
