"""Explicit local-inference usage policy for isolated person workers only."""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import stat
import sys
from pathlib import Path

_STUB_SHA256 = "c42d35599321c6cbec9357dbd5d178cb0c21b22aacc9f77b31dd96589a228bb4"
_selected = None


def configure_local_person_telemetry() -> None:
    """Select the donor's unchanged no-usage class, retaining its real package.

    Call only in a fresh isolated worker after its validated model grant. This
    is K5's local-inference policy, not a vendor environment setting. The stock
    OpenVINO class performs no usage reporting or platform-version discovery.
    """
    global _selected
    try:
        if _selected is not None:
            module, selected, spec, package_path = _selected
            if (
                sys.modules.get("openvino_telemetry") is not module
                or module.Telemetry is not selected
                or module.__spec__ is not spec
                or tuple(module.__path__) != package_path
            ):
                raise RuntimeError
            return
        if "openvino" in sys.modules:
            raise RuntimeError
        runtime = importlib.metadata.distribution("openvino")
        telemetry_distribution = importlib.metadata.distribution("openvino-telemetry")
        if runtime.version != "2026.3.1" or telemetry_distribution.version != "2025.2.0":
            raise RuntimeError
        runtime_spec = importlib.util.find_spec("openvino")
        expected_runtime = Path(runtime.locate_file("openvino/__init__.py"))
        if (
            runtime_spec is None
            or runtime_spec.origin is None
            or Path(runtime_spec.origin).resolve() != expected_runtime.resolve()
        ):
            raise RuntimeError
        spec = importlib.util.find_spec("openvino_telemetry")
        expected_init = Path(telemetry_distribution.locate_file("openvino_telemetry/__init__.py"))
        if (
            spec is None
            or spec.origin is None
            or Path(spec.origin).resolve() != expected_init.resolve()
        ):
            raise RuntimeError
        import openvino_telemetry

        actual_spec = openvino_telemetry.__spec__
        if (
            actual_spec is None
            or actual_spec.name != "openvino_telemetry"
            or actual_spec.origin is None
            or Path(actual_spec.origin).resolve() != expected_init.resolve()
            or Path(openvino_telemetry.__file__).resolve() != expected_init.resolve()
            or not openvino_telemetry.__path__
        ):
            raise RuntimeError
        path = Path(runtime.locate_file("openvino/tools/ovc/telemetry_stub.py"))
        info = path.stat()
        if (
            not stat.S_ISREG(info.st_mode)
            or path.is_symlink()
            or getattr(info, "st_file_attributes", 0) & 0x400
            or info.st_size != 619
        ):
            raise RuntimeError
        source = path.read_bytes()
        if hashlib.sha256(source).hexdigest() != _STUB_SHA256:
            raise RuntimeError
        namespace = {"__name__": "_k5_local_person_usage_policy"}
        exec(compile(source, "<k5-local-person-usage-policy>", "exec"), namespace)
        selected = namespace["Telemetry"]
        # Rebind only this export. Real module identity, package search path,
        # dependency metadata and backend modules remain available unchanged.
        openvino_telemetry.Telemetry = selected
        _selected = (openvino_telemetry, selected, actual_spec, tuple(openvino_telemetry.__path__))
    except (OSError, ImportError, KeyError, AttributeError, TypeError, ValueError, RuntimeError):
        raise RuntimeError("Local person usage policy is unavailable") from None
