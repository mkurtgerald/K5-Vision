"""Admit the pinned, separately installed Analytics Lab engineering package.

This is an installation-integrity check, not a sandbox against a compromised
interpreter or concurrent writers. It never imports Analytics or native runtimes.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import importlib.machinery
import importlib.metadata
import importlib.util
import io
import json
import marshal
import sys
from pathlib import Path, PurePosixPath

ANALYTICS_REVISION = "c8b347ae538991a0c0ce38eabc2dc17b566531d3"
ANALYTICS_DISTRIBUTION = "k5-analytics-runtime"
ANALYTICS_VERSION = f"0.0.0+g{ANALYTICS_REVISION}"
DIST_INFO = f"k5_analytics_runtime-{ANALYTICS_VERSION}.dist-info"
MANIFEST_SHA256 = "62cd59b95380f429e7b77cfaf4855845ad96242ed6f2d2bafa2a1600cde1f3c2"
_MANIFEST_PATH = Path(__file__).with_name("data") / "analytics-runtime-manifest.json"
_admitted_root: Path | None = None


class AnalyticsPackageError(RuntimeError):
    """The installed Analytics Lab package does not match the reviewed payload."""


def _load_manifest() -> tuple[bytes, dict]:
    raw = _MANIFEST_PATH.read_bytes()
    if hashlib.sha256(raw).hexdigest() != MANIFEST_SHA256:
        raise AnalyticsPackageError("Analytics package admission manifest is invalid")
    return raw, json.loads(raw)


def _generated_files(manifest_bytes: bytes) -> dict[str, bytes]:
    metadata = (
        "Metadata-Version: 2.4\n"
        f"Name: {ANALYTICS_DISTRIBUTION}\n"
        f"Version: {ANALYTICS_VERSION}\n"
        "Summary: Unmodified pinned Analytics Lab engineering runtime\n"
        "Requires-Python: >=3.12\n"
        "License: K5 Analytics Lab Source-Available License v1.0; donor notices apply\n"
        "License-File: licenses/Analytics-Lab-LICENSE\n"
        "License-File: licenses/THIRD_PARTY.md\n"
        "License-File: licenses/ByteTrack-MIT.txt\n"
        "License-File: licenses/Apache-2.0.txt\n"
        f"X-Analytics-Revision: {ANALYTICS_REVISION}\n"
        f"X-Analytics-Manifest-SHA256: {MANIFEST_SHA256}\n"
        "\nEngineering qualification only. No production or redistribution clearance.\n"
    ).encode()
    return {
        f"{DIST_INFO}/METADATA": metadata,
        f"{DIST_INFO}/WHEEL": (
            b"Wheel-Version: 1.0\nGenerator: k5-analytics-runtime-offline-1\n"
            b"Root-Is-Purelib: true\nTag: py3-none-any\n"
        ),
        f"{DIST_INFO}/top_level.txt": b"analytics_lab\n",
        f"{DIST_INFO}/analytics-runtime-manifest.json": manifest_bytes,
    }


def _safe_file(root: Path, relative: str) -> Path:
    parts = PurePosixPath(relative).parts
    if not parts or relative != "/".join(parts) or any(p in {".", ".."} for p in parts):
        raise AnalyticsPackageError("Analytics package contains an unsafe file path")
    path = root
    for part in parts:
        path = path / part
        if path.is_symlink():
            raise AnalyticsPackageError("Analytics package contains a symbolic link")
    if not path.is_file() or not path.resolve().is_relative_to(root):
        raise AnalyticsPackageError("Analytics package payload is missing or outside installation")
    return path


def _verify_file(root: Path, relative: str, size: int, digest: str) -> bytes:
    path = _safe_file(root, relative)
    if path.stat().st_size != size:
        raise AnalyticsPackageError("Analytics package payload size does not match")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != digest:
        raise AnalyticsPackageError("Analytics package payload hash does not match")
    return data


def _verify_bytecode(path: Path, sources: dict[Path, bytes]) -> None:
    """Permit only current-interpreter caches compiled from the verified sources."""
    for source, data in sources.items():
        for optimization in ("", "1", "2"):
            if path != Path(
                importlib.util.cache_from_source(str(source), optimization=optimization)
            ):
                continue
            code = compile(
                data, str(source), "exec", dont_inherit=True, optimize=int(optimization or 0)
            )
            expected = marshal.dumps(code)
            if path.stat().st_size != len(expected) + 16:
                break
            actual = path.read_bytes()
            if (
                actual[:4] == importlib.util.MAGIC_NUMBER
                and int.from_bytes(actual[4:8], "little") in (0, 1, 3)
                and actual[16:] == expected
            ):
                return
            break
    raise AnalyticsPackageError("Analytics package contains unverified bytecode")


def _verify_package_tree(root: Path, sources: dict[Path, bytes]) -> None:
    package = root / "analytics_lab"
    for path in package.rglob("*"):
        if path.is_symlink():
            raise AnalyticsPackageError("Analytics package contains a symbolic link")
        if path.is_dir():
            if path != package / "__pycache__":
                raise AnalyticsPackageError("Analytics package contains unexpected directories")
        elif path not in sources:
            if path.parent.name != "__pycache__" or path.suffix != ".pyc":
                raise AnalyticsPackageError("Analytics package contains unexpected files")
            _verify_bytecode(path, sources)


def _verify_record(root: Path, expected: dict[str, bytes]) -> None:
    record_name = f"{DIST_INFO}/RECORD"
    record = _safe_file(root, record_name)
    if record.stat().st_size > 128_000:
        raise AnalyticsPackageError("Analytics package record exceeds the admitted bound")
    rows = list(csv.reader(io.StringIO(record.read_text(encoding="utf-8"))))
    entries = {}
    for row in rows:
        if len(row) != 3 or row[0] in entries:
            raise AnalyticsPackageError("Analytics package record is malformed")
        entries[row[0]] = row[1:]
    for name, data in expected.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        if entries.get(name) != [f"sha256={digest}", str(len(data))]:
            raise AnalyticsPackageError("Analytics package record does not bind the payload")
    if entries.get(record_name) != ["", ""]:
        raise AnalyticsPackageError("Analytics package record has no valid self-entry")
    installer_files = {
        f"{DIST_INFO}/{name}" for name in ("INSTALLER", "REQUESTED", "direct_url.json")
    }
    for name in entries.keys() - expected.keys() - {record_name} - installer_files:
        path = _safe_file(root, name)
        if not path.is_relative_to(root / "analytics_lab" / "__pycache__"):
            raise AnalyticsPackageError("Analytics package record contains unexpected entries")
    direct_url = root / DIST_INFO / "direct_url.json"
    if direct_url.exists():
        direct_url = _safe_file(root, f"{DIST_INFO}/direct_url.json")
        if direct_url.stat().st_size > 16_384:
            raise AnalyticsPackageError("Analytics package installer metadata exceeds the bound")
        data = json.loads(direct_url.read_text(encoding="utf-8"))
        if data.get("dir_info", {}).get("editable"):
            raise AnalyticsPackageError("Editable Analytics package installations are not admitted")


def _verify_import_origin(package: Path, sources: dict[Path, bytes]) -> None:
    loaded = {
        name: module
        for name, module in tuple(sys.modules.items())
        if name == "analytics_lab" or name.startswith("analytics_lab.")
    }
    if loaded and _admitted_root != package:
        raise AnalyticsPackageError("Analytics was imported before installed-package admission")
    for name, module in loaded.items():
        filename = "__init__.py" if name == "analytics_lab" else name.split(".", 1)[1] + ".py"
        expected = package / filename
        spec = getattr(module, "__spec__", None)
        if (
            expected not in sources
            or spec is None
            or type(spec.loader) is not importlib.machinery.SourceFileLoader
            or Path(spec.origin or "").resolve() != expected
            or Path(spec.loader.path).resolve() != expected
            or Path(getattr(module, "__file__", "")).resolve() != expected
        ):
            raise AnalyticsPackageError("Loaded Analytics module has an unverified origin")
    spec = importlib.util.find_spec("analytics_lab")
    if (
        spec is None
        or type(spec.loader) is not importlib.machinery.SourceFileLoader
        or Path(spec.origin or "").resolve() != package / "__init__.py"
        or Path(spec.loader.path).resolve() != package / "__init__.py"
        or list(spec.submodule_search_locations or []) != [str(package)]
    ):
        raise AnalyticsPackageError("Analytics import is shadowed or outside the installed package")
    if loaded and list(getattr(loaded["analytics_lab"], "__path__", [])) != [str(package)]:
        raise AnalyticsPackageError("Loaded Analytics package search path has changed")


def validate_installed_analytics() -> Path:
    """Verify metadata, every payload file and import origin before Analytics use.

    Returns the verified ``analytics_lab`` directory. Raises AnalyticsPackageError
    for missing/wrong/edited installs, editable packages or checkout shadowing.
    No model, media, network, Analytics import or native runtime is used here.
    """
    global _admitted_root
    try:
        if sys.pycache_prefix is not None:
            raise AnalyticsPackageError("External Analytics bytecode caches are not admitted")
        manifest_bytes, manifest = _load_manifest()
        distributions = list(importlib.metadata.distributions(name=ANALYTICS_DISTRIBUTION))
        if len(distributions) != 1:
            raise AnalyticsPackageError(
                "Exactly one pinned Analytics runtime installation is required"
            )
        distribution = distributions[0]
        if (
            distribution.metadata.get("Name") != ANALYTICS_DISTRIBUTION
            or distribution.version != ANALYTICS_VERSION
        ):
            raise AnalyticsPackageError("Installed Analytics runtime identity does not match")
        root = Path(distribution.locate_file("")).resolve()
        expected = _generated_files(manifest_bytes)
        for name, data in expected.items():
            _verify_file(root, name, len(data), hashlib.sha256(data).hexdigest())
        for entry in manifest["files"]:
            expected[entry["wheel_path"]] = _verify_file(
                root, entry["wheel_path"], entry["size"], entry["sha256"]
            )
        sources = {
            root / name: data
            for name, data in expected.items()
            if name.startswith("analytics_lab/")
        }
        _verify_package_tree(root, sources)
        _verify_record(root, expected)
        package = root / "analytics_lab"
        _verify_import_origin(package, sources)
    except AnalyticsPackageError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, ImportError) as exc:
        raise AnalyticsPackageError("Analytics package installation cannot be verified") from exc
    _admitted_root = package
    return package
