from __future__ import annotations

import k5vision.analytics.hardware_acceleration as hardware
from k5vision.analytics.hardware_acceleration import ComputeBackend, HardwareProfile


def test_nvidia_probe_parses_accelerator_metadata(monkeypatch) -> None:
    monkeypatch.setattr(hardware, "_nvidia_smi_path", lambda: "nvidia-smi")
    monkeypatch.setattr(
        hardware,
        "_run_text",
        lambda _command: "NVIDIA RTX Test, 12288, 999.1",
    )

    profile = hardware._probe_nvidia()

    assert profile == HardwareProfile(
        backend=ComputeBackend.NVIDIA,
        accelerator_name="NVIDIA RTX Test",
        memory_mb=12288,
        driver_version="999.1",
        recommended_runtime="TensorRT/CUDA",
    )


def test_probe_hardware_uses_priority_and_cpu_fallback(monkeypatch) -> None:
    selected = HardwareProfile(
        backend=ComputeBackend.INTEL,
        accelerator_name="Intel Test GPU",
        memory_mb=0,
        driver_version="",
        recommended_runtime="OpenVINO",
    )
    monkeypatch.setattr(hardware, "_probe_nvidia", lambda: None)
    monkeypatch.setattr(hardware, "_probe_amd", lambda: None)
    monkeypatch.setattr(hardware, "_probe_apple", lambda: None)
    monkeypatch.setattr(hardware, "_probe_intel", lambda: selected)

    assert hardware.probe_hardware() is selected

    monkeypatch.setattr(hardware, "_probe_intel", lambda: None)
    monkeypatch.setattr(hardware.platform, "processor", lambda: "Test CPU")

    fallback = hardware.probe_hardware()
    assert fallback.backend is ComputeBackend.CPU
    assert fallback.accelerator_name == "Test CPU"
    assert fallback.recommended_runtime == "ONNX Runtime"
