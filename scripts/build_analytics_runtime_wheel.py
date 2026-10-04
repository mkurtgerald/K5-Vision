#!/usr/bin/env python3
"""Build the pinned Analytics engineering wheel from local verified sources only.

Standard library only: no build backend, install, network, native runtime or model.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import importlib.util
import io
import os
import tempfile
import zipfile
from pathlib import Path

_K5_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "_k5_analytics_package", _K5_ROOT / "src/k5vision/analytics_package.py"
)
assert _spec is not None and _spec.loader is not None
_admission = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_admission)


def build_wheel(source_root: Path, output_dir: Path) -> Path:
    """Build one deterministic wheel; reject any changed/missing package source."""
    source_root = source_root.resolve(strict=True)
    raw_manifest, manifest = _admission._load_manifest()
    expected_sources = {
        entry["source_path"]
        for entry in manifest["files"]
        if entry["source"] == "analytics" and entry["source_path"].startswith("analytics_lab/")
    }
    actual_sources = {
        path.relative_to(source_root).as_posix()
        for path in (source_root / "analytics_lab").rglob("*")
        if not path.is_dir()
    }
    if actual_sources != expected_sources:
        raise _admission.AnalyticsPackageError(
            "Analytics source inventory does not match pinned manifest"
        )
    files = _admission._generated_files(raw_manifest)
    for entry in manifest["files"]:
        root = source_root if entry["source"] == "analytics" else _admission._MANIFEST_PATH.parent
        data = _admission._verify_file(root, entry["source_path"], entry["size"], entry["sha256"])
        git_blob = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
        if git_blob != entry["git_blob_sha1"]:
            raise _admission.AnalyticsPackageError("Analytics source Git blob does not match")
        files[entry["wheel_path"]] = data
    record_path = f"{_admission.DIST_INFO}/RECORD"
    record = io.StringIO(newline="")
    writer = csv.writer(record, lineterminator="\n")
    for name, data in sorted(files.items()):
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        writer.writerow((name, f"sha256={digest}", len(data)))
    writer.writerow((record_path, "", ""))
    files[record_path] = record.getvalue().encode()
    output_dir.mkdir(parents=True, exist_ok=True)
    wheel = output_dir / f"k5_analytics_runtime-{_admission.ANALYTICS_VERSION}-py3-none-any.whl"
    descriptor, temporary = tempfile.mkstemp(dir=output_dir, suffix=".whl.tmp")
    try:
        with os.fdopen(descriptor, "wb") as output, zipfile.ZipFile(output, "w") as archive:
            for name, data in sorted(files.items()):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                info.compress_type = zipfile.ZIP_STORED
                archive.writestr(info, data)
        os.replace(temporary, wheel)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return wheel


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        wheel = build_wheel(args.source_root, args.output_dir)
    except (OSError, _admission.AnalyticsPackageError) as exc:
        parser.exit(2, f"Analytics wheel build rejected: {exc}\n")
    print(wheel)
    print(f"sha256={hashlib.sha256(wheel.read_bytes()).hexdigest()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
