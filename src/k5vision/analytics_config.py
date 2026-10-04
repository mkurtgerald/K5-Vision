"""Explicit, local-only admission for the optional installed analytics runtime."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

from k5vision.analytics_package import ANALYTICS_REVISION, validate_installed_analytics
from k5vision.windows_identity_security import check_windows_volume

ANALYTICS_CONFIG_ENV = "K5_ANALYTICS_CONFIG"
PROVIDER = "analytics-lab-omz-person-v1"
RUNTIME_VERSIONS = {
    "openvino": "2026.3.1",
    "opencv-python-headless": "4.12.0.88",
    "numpy": "2.2.6",
    "openvino-telemetry": "2025.2.0",
}
_MAX_CONFIG_BYTES = 4096
_WINDOWS = os.name == "nt"


class AnalyticsConfigurationError(RuntimeError):
    """A fixed diagnostic that never includes local paths or input values."""


@dataclass(frozen=True, slots=True)
class AnalyticsConfiguration:
    artifact_root: Path


def _local_path(value: object, *, directory: bool) -> Path:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ValueError
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or value.startswith(("\\\\", "//")):
        raise ValueError
    if _WINDOWS:
        # Reuse the existing read-only fixed-volume/alias admission. This does
        # not change ACLs or inherit the separate durable-identity state policy.
        check_windows_volume(path)
    for item in (path, *path.parents):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError
    if directory != path.is_dir() or (not directory and not path.is_file()):
        raise ValueError
    return path


def _unique_object(items: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in items:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def validate_analytics_runtime(config: AnalyticsConfiguration) -> None:
    """Read-only checks before application/device state or native runtime creation."""
    try:
        validate_installed_analytics()
        for package, expected in RUNTIME_VERSIONS.items():
            if metadata.version(package) != expected:
                raise ValueError
        # The verified package's artifact module is pure Python and never downloads.
        from analytics_lab.artifacts import OPENVINO_OMZ_2023_FP16, verify_artifact_set

        root = _local_path(str(config.artifact_root), directory=True)
        for spec in OPENVINO_OMZ_2023_FP16:
            _local_path(str(root / spec.relative_path), directory=False)
        verify_artifact_set(root, OPENVINO_OMZ_2023_FP16)
    except Exception:
        raise AnalyticsConfigurationError(
            "Configured analytics runtime admission failed."
        ) from None


def load_analytics_configuration(
    environment: Mapping[str, str],
) -> AnalyticsConfiguration | None:
    """Absent config means disabled; any explicitly requested invalid config fails closed."""
    if ANALYTICS_CONFIG_ENV not in environment:
        return None
    try:
        path = _local_path(environment[ANALYTICS_CONFIG_ENV], directory=False)
        # Open only an admitted regular local file. No credentials, URLs, remote
        # model manifests or arbitrary import/provider names are accepted.
        with path.open("rb") as stream:
            payload = stream.read(_MAX_CONFIG_BYTES + 1)
        if len(payload) > _MAX_CONFIG_BYTES:
            raise ValueError
        document = json.loads(payload, object_pairs_hook=_unique_object)
        if type(document) is not dict or document.keys() != {
            "schema_version",
            "provider",
            "source_revision",
            "artifact_root",
        }:
            raise ValueError
        if (
            type(document["schema_version"]) is not int
            or document["schema_version"] != 1
            or document["provider"] != PROVIDER
            or document["source_revision"] != ANALYTICS_REVISION
        ):
            raise ValueError
        config = AnalyticsConfiguration(_local_path(document["artifact_root"], directory=True))
    except Exception:
        raise AnalyticsConfigurationError("Configured analytics input is invalid.") from None
    validate_analytics_runtime(config)
    return config
