"""Separate native CPU diagnostic. This script cannot qualify or publish a release.

Each trial owns a new process. A native illegal instruction in the default
runtime cannot prevent the same Windows VM from testing explicit FP32.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import platform
import subprocess
import sys
import tempfile
import time
from importlib.metadata import version
from pathlib import Path

import psutil

SOURCE_HEAD = "9cbeb30471d28c6a2f03eabaa9f1b8602c9c92f0"
SOURCE_FILES = (
    "src/k5vision",
    "pyproject.toml",
    "tests/integration/test_operator_beta_capacity.py",
)
VARIANTS = {"default", "fp32", "fp32_avx2"}
STAGES = {"cpu_ready", "compiled", "inference_verified", "trial_failed"}
LOG_LIMIT = 64 * 1024
TRIAL_SECONDS = 90


def require(value):
    if not value:
        raise ValueError("Person CPU diagnostic condition failed")


def safe_text(value):
    value = str(value).strip()
    require(len(value) <= 512 and all(ch.isprintable() for ch in value))
    return value


def emit(variant, stage, **fields):
    print(json.dumps({"variant": variant, "stage": stage, **fields}, allow_nan=False), flush=True)


def source_identity():
    observed = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    require(len(observed) == 40)
    changed = subprocess.check_output(
        ["git", "diff", "--name-only", SOURCE_HEAD, "--", *SOURCE_FILES], text=True
    )
    require(not changed.strip())
    return observed


def child(variant):
    import openvino as ov

    from k5vision.operator_analytics_person import (
        OperatorPersonConfiguration,
        _PersonDetector,
        validate_person_runtime,
    )

    require(variant in VARIANTS)
    config = OperatorPersonConfiguration(Path(os.environ["K5_OPERATOR_PERSON_ARTIFACT_ROOT"]))
    validate_person_runtime(config)
    helper_path = Path("tests/integration/test_operator_beta_capacity.py").resolve(strict=True)
    spec = importlib.util.spec_from_file_location("k5_person_cpu_positive_fixture", helper_path)
    require(spec is not None and spec.loader is not None)
    helpers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helpers)
    positive = helpers._positive_frame()
    require(positive.width == 544 and positive.height == 320 and positive.stride_bytes == 544 * 4)
    original_core = ov.Core
    metadata_core = original_core()
    plugin_versions = {
        safe_text(name): {
            "build_number": safe_text(item.build_number),
            "description": safe_text(item.description),
        }
        for name, item in metadata_core.get_versions("CPU").items()
    }
    emit(
        variant,
        "cpu_ready",
        device_name=safe_text(metadata_core.get_property("CPU", "FULL_DEVICE_NAME")),
        capabilities=[
            safe_text(item)
            for item in metadata_core.get_property("CPU", "OPTIMIZATION_CAPABILITIES")
        ],
        default_precision=safe_text(metadata_core.get_property("CPU", "INFERENCE_PRECISION_HINT")),
        plugin_versions=plugin_versions,
        runtime_versions={
            package: version(package) for package in ("openvino", "numpy", "openvino-telemetry")
        },
        cpu_count=os.cpu_count(),
        model_runtime_admitted=True,
        positive_fixture_verified=True,
    )

    class DiagnosticCore:
        def __init__(self):
            self.inner = original_core()

        def read_model(self, *args, **kwargs):
            return self.inner.read_model(*args, **kwargs)

        def compile_model(self, model, device, options):
            require(device == "CPU")
            options = dict(options)
            require(options == {"INFERENCE_NUM_THREADS": 2, "NUM_STREAMS": "1"})
            if variant != "default":
                options["INFERENCE_PRECISION_HINT"] = ov.Type.f32
            compiled = self.inner.compile_model(model, device, options)
            precision = compiled.get_property("INFERENCE_PRECISION_HINT")
            if variant != "default":
                require(precision == ov.Type.f32)
            emit(variant, "compiled", selected_precision=safe_text(precision))
            return compiled

    ov.Core = DiagnosticCore
    detector = _PersonDetector(config)
    positive_counts, blank_counts, elapsed = [], [], []
    for _ in range(3):
        began = time.monotonic()
        found = detector.infer(
            bytes(positive.payload), positive.width, positive.height, positive.stride_bytes, 0
        )
        elapsed.append(round(time.monotonic() - began, 6))
        require(1 <= len(found) <= 32)
        for item in found:
            require(item.category == "person" and math.isfinite(item.confidence))
            require(0.5 <= item.confidence <= 1)
            box = item.box
            require(0 <= box.x_min < box.x_max <= 1 and 0 <= box.y_min < box.y_max <= 1)
        positive_counts.append(len(found))
        blank = detector.infer(
            bytes(positive.height * positive.stride_bytes),
            positive.width,
            positive.height,
            positive.stride_bytes,
            0,
        )
        require(not blank)
        blank_counts.append(len(blank))
    emit(
        variant,
        "inference_verified",
        positive_counts=positive_counts,
        blank_counts=blank_counts,
        inference_seconds=elapsed,
        actual_inference=True,
        mock_inference=False,
    )


def owned_identity(process):
    if process.poll() is not None:
        return None
    try:
        owner = psutil.Process(process.pid)
        birth = owner.create_time()
        require(owner.ppid() == os.getpid())
    except psutil.NoSuchProcess:
        return None
    return owner, birth


def stop_owned(process, identity):
    children = []
    if identity is not None:
        owner, birth = identity
        try:
            # Never rediscover an owner by a possibly reused PID at timeout.
            if owner.is_running() and owner.create_time() == birth and owner.ppid() == os.getpid():
                for item in owner.children(recursive=True):
                    children.append((item, item.create_time()))
        except psutil.NoSuchProcess:
            pass
    for item, birth in reversed(children):
        try:
            if item.is_running() and item.create_time() == birth:
                item.kill()
        except psutil.NoSuchProcess:
            pass
    if process.poll() is None:
        process.kill()
    process.wait(timeout=10)
    _, alive = psutil.wait_procs([item for item, _birth in children], timeout=5)
    require(not alive)


def trial(variant, directory):
    require(variant in VARIANTS)
    child_environment = dict(os.environ)
    # This cap is applied only to a secondary child, after ordinary FP32 fails.
    if variant == "fp32_avx2":
        child_environment["ONEDNN_MAX_CPU_ISA"] = "AVX2"
    stdout_path, stderr_path = directory / (variant + ".stdout"), directory / (variant + ".stderr")
    began = time.monotonic()
    timed_out = False
    with stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
        process = subprocess.Popen(
            [sys.executable, "-u", str(Path(__file__).resolve()), "--child", variant],
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            env=child_environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        identity = owned_identity(process)
        try:
            process.wait(timeout=TRIAL_SECONDS)
        except subprocess.TimeoutExpired:
            timed_out = True
            stop_owned(process, identity)
        finally:
            if process.poll() is None:
                stop_owned(process, identity)
    raw = stdout_path.read_bytes() if stdout_path.stat().st_size <= LOG_LIMIT else b""
    progress = []
    try:
        for line in raw.decode("utf-8").splitlines():
            item = json.loads(line)
            require(type(item) is dict and item.get("variant") == variant)
            require(item.get("stage") in STAGES and len(progress) < 8)
            progress.append(item)
    except (ValueError, UnicodeError):
        progress = []
    verified = (
        process.returncode == 0
        and not timed_out
        and len(progress) == 3
        and [item["stage"] for item in progress] == ["cpu_ready", "compiled", "inference_verified"]
    )
    with stderr_path.open("rb") as stream:
        stderr_prefix = stream.read(LOG_LIMIT)
    return {
        "variant": variant,
        "exit_code": process.returncode,
        "windows_exit_hex": f"0x{process.returncode & 0xFFFFFFFF:08x}",
        "timed_out": timed_out,
        "duration_seconds": round(time.monotonic() - began, 3),
        "progress": progress,
        "inference_verified": verified,
        "stderr_bytes": stderr_path.stat().st_size,
        "stderr_prefix_sha256": hashlib.sha256(stderr_prefix).hexdigest(),
        "raw_stderr_published": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", choices=sorted(VARIANTS))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--allow-non-windows-regression", action="store_true")
    args = parser.parse_args()
    if args.child:
        try:
            child(args.child)
        except BaseException:
            emit(args.child, "trial_failed", error_code="person_cpu_trial_failed")
            return 2
        return 0
    require(args.output is not None)
    require(os.name == "nt" or args.allow_non_windows_regression)
    head = source_identity()
    with tempfile.TemporaryDirectory(prefix="k5-person-cpu-owned-") as temporary:
        results = [trial("default", Path(temporary)), trial("fp32", Path(temporary))]
        if not results[1]["inference_verified"]:
            results.append(trial("fp32_avx2", Path(temporary)))
    report = {
        "schema_version": 1,
        "purpose": "native_cpu_precision_diagnostic_only",
        "source_head": SOURCE_HEAD,
        "checkout_head": head,
        "platform": platform.system(),
        "native_windows_execution": os.name == "nt",
        "production_source_unchanged": True,
        "same_vm_separate_owned_children": True,
        "release_qualification": False,
        "release_publication": False,
        "results": results,
        "explicit_fp32_verified": results[1]["inference_verified"],
    }
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, allow_nan=False)
    print(json.dumps(report, allow_nan=False))
    return 0 if report["explicit_fp32_verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
