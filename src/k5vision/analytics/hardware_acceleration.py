"""Cross-platform accelerator discovery for optional analytics runtimes.

Portions of the accelerator-probing strategy are adapted from SharpAI/DeepCamera
`skills/lib/env_config.py` at commit
933dcc7c90226cd1f92dc6ea7cee3b3c94790ad0 (MIT).

K5 deliberately stops at detection/recommendation. It does not auto-install drivers,
packages, model weights, or arbitrary skill dependencies.
"""

from __future__ import annotations

import enum
import os
import platform
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class ComputeBackend(enum.StrEnum):
    NVIDIA = "nvidia"
    AMD = "amd"
    APPLE = "apple"
    INTEL = "intel"
    CPU = "cpu"


@dataclass(frozen=True, slots=True)
class HardwareProfile:
    """Small dependency-free compute profile safe for scheduling decisions."""

    backend: ComputeBackend
    accelerator_name: str
    memory_mb: int
    driver_version: str
    recommended_runtime: str


def _run_text(command: list[str], *, timeout_seconds: float = 3.0) -> str | None:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _nvidia_smi_path() -> str | None:
    found = shutil.which("nvidia-smi")
    if found:
        return found
    if platform.system() != "Windows":
        return None

    candidates = (
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
        / "NVIDIA Corporation"
        / "NVSMI"
        / "nvidia-smi.exe",
        Path(os.environ.get("WINDIR", r"C:\Windows"))
        / "System32"
        / "nvidia-smi.exe",
    )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def _probe_nvidia() -> HardwareProfile | None:
    executable = _nvidia_smi_path()
    if not executable:
        return None
    output = _run_text(
        [
            executable,
            "--query-gpu=name,memory.total,driver_version",
            "--format=csv,noheader,nounits",
        ]
    )
    if not output:
        return None
    first = output.splitlines()[0]
    parts = [part.strip() for part in first.split(",")]
    if len(parts) < 3:
        return None
    try:
        memory_mb = max(0, int(float(parts[1])))
    except ValueError:
        memory_mb = 0
    return HardwareProfile(
        backend=ComputeBackend.NVIDIA,
        accelerator_name=parts[0] or "NVIDIA GPU",
        memory_mb=memory_mb,
        driver_version=parts[2],
        recommended_runtime="TensorRT/CUDA",
    )


def _probe_amd() -> HardwareProfile | None:
    for executable_name in ("amd-smi", "rocm-smi"):
        executable = shutil.which(executable_name)
        if not executable:
            continue
        output = _run_text([executable, "--showproductname"])
        if output is not None:
            label = next(
                (line.strip() for line in output.splitlines() if line.strip()),
                "AMD GPU",
            )
            return HardwareProfile(
                backend=ComputeBackend.AMD,
                accelerator_name=label,
                memory_mb=0,
                driver_version="",
                recommended_runtime="ROCm",
            )
    return None


def _probe_apple() -> HardwareProfile | None:
    if platform.system() == "Darwin" and platform.machine().lower() in {"arm64", "aarch64"}:
        return HardwareProfile(
            backend=ComputeBackend.APPLE,
            accelerator_name="Apple Silicon",
            memory_mb=0,
            driver_version=platform.release(),
            recommended_runtime="CoreML",
        )
    return None


def _probe_intel() -> HardwareProfile | None:
    if platform.system() == "Windows":
        powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        if powershell:
            output = _run_text(
                [
                    powershell,
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    (
                        "Get-CimInstance Win32_VideoController | "
                        "Select-Object -ExpandProperty Name"
                    ),
                ]
            )
            if output:
                match = next(
                    (line.strip() for line in output.splitlines() if "intel" in line.lower()),
                    None,
                )
                if match:
                    return HardwareProfile(
                        backend=ComputeBackend.INTEL,
                        accelerator_name=match,
                        memory_mb=0,
                        driver_version="",
                        recommended_runtime="OpenVINO",
                    )
    return None


def probe_hardware() -> HardwareProfile:
    """Return the highest-priority locally detected analytics accelerator."""
    for probe in (_probe_nvidia, _probe_amd, _probe_apple, _probe_intel):
        profile = probe()
        if profile is not None:
            return profile
    return HardwareProfile(
        backend=ComputeBackend.CPU,
        accelerator_name=platform.processor() or "CPU",
        memory_mb=0,
        driver_version="",
        recommended_runtime="ONNX Runtime",
    )
