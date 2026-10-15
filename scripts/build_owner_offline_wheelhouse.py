"""CI-only builder for an UNQUALIFIED exact Windows owner wheelhouse.

The output SHA-256 is a proposed identity for independent provenance/licensing
review, not an external approval or permission to hand the installer to an owner.
"""
from __future__ import annotations

import argparse
import ensurepip
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sys
import zipfile
from pathlib import Path


def admit(ok: bool, reason: str) -> None:
    if not ok:
        raise RuntimeError("K5_OFFLINE_BUILD_REFUSED=" + reason)


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def transaction(root: Path):
    script = root / "scripts/windows-alpha/install_transaction.py"
    spec = importlib.util.spec_from_file_location("_owner_build_transaction", script)
    admit(spec is not None and spec.loader is not None, "verifier_unavailable")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def record(path: Path) -> dict:
    admit(path.is_file() and not path.is_symlink(), "file_missing")
    size = path.stat().st_size
    admit(0 < size <= 256 * 1024 * 1024, "file_size")
    return {"size": size, "sha256": sha256(path)}


def build(root: Path, wheels: Path, manifest: Path, revision: str, tx) -> str:
    admit(os.name == "nt" and sys.version_info[:3] == (3, 12, 10), "python_version")
    admit(sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode, "isolation")
    admit(sys.prefix == sys.base_prefix, "base_python")
    admit(re.fullmatch(r"[0-9a-f]{40}", revision) is not None, "source_revision")
    source = root / "scripts/windows-alpha"
    for path in (source, wheels, manifest):
        tx._offline_path(path)
    admit(wheels.is_dir() and not manifest.exists(), "fresh_manifest")
    admit(not wheels.is_relative_to(source), "path_overlap")
    host = tx._host_identity()
    admit(host["version"] == "3.12.10" and host["platform"] == "win_amd64", "private_python")
    pip = Path(ensurepip.__file__).parent / "_bundled" / (
        "pip-" + host["ensurepip_version"] + "-py3-none-any.whl"
    )
    admit(pip.is_file() and sha256(pip) == host["ensurepip_wheel_sha256"], "bundled_pip")
    dest = wheels / pip.name
    admit(not dest.exists(), "pip_collision")
    shutil.copyfile(pip, dest)
    admit(sha256(dest) == host["ensurepip_wheel_sha256"], "pip_copy")

    versions = tx.runtime_versions(source / "runtime-requirements.txt", target_platform="win32")
    versions.update({"k5-vision": "0.1.0", "pip": host["ensurepip_version"]})
    admit(len(versions) == 30, "pin_count")
    paths = sorted(wheels.iterdir(), key=lambda path: path.name)
    admit(len(paths) == 30, "wheel_count")
    records = []
    names = set()
    k5_wheel = None
    for path in paths:
        admit(path.is_file() and not path.is_symlink() and path.suffix == ".whl", "wheel_file")
        parts = path.name[:-4].split("-")
        admit(len(parts) in (5, 6), "wheel_filename")
        name = re.sub(r"[-_.]+", "-", parts[0]).lower()
        admit(name in versions and name not in names and parts[1] == versions[name], "wheel_identity")
        names.add(name)
        records.append({
            "filename": path.name, "name": name, "version": parts[1],
            "tags": sorted(tx._tags("-".join(parts[-3:]))), **record(path),
        })
        if name == "k5-vision":
            k5_wheel = path
    admit(names == set(versions) and k5_wheel is not None, "exact_wheels")

    runtime = {}
    with zipfile.ZipFile(k5_wheel) as archive:
        members = [e for e in archive.infolist()
                   if e.filename.startswith("k5vision/") and not e.is_dir()]
        admit(0 < len(members) <= 512 and len({e.filename for e in members}) == len(members),
              "runtime_inventory")
        admit(sum(e.file_size for e in members) <= 256 * 1024 * 1024, "runtime_size")
        for member in members:
            name = member.filename
            admit(all(part not in ("", ".", "..") for part in name.split("/")), "runtime_path")
            original = root / "src" / name
            admit(original.is_file() and not original.is_symlink(), "runtime_source")
            admit(member.file_size <= 8 * 1024 * 1024, "runtime_member_size")
            data = archive.read(member)
            admit(data == original.read_bytes(), "runtime_source_mismatch")
            runtime[name] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    admit("k5vision/cli.py" in runtime, "runtime_entrypoint")

    manifest_data = {
        "schema_version": tx.WHEELHOUSE_FORMAT, "installer_revision": revision,
        "runtime_revision": revision, "python": host,
        "installer_payload": {name: record(root / name) for name in tx.PAYLOAD_FILES},
        "runtime_payload": runtime, "wheels": records,
    }
    raw = (json.dumps(manifest_data, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
    admit(len(raw) <= 1024 * 1024, "manifest_size")
    manifest.parent.mkdir(parents=True, exist_ok=True)
    with manifest.open("xb") as stream:
        stream.write(raw)
    checksum = hashlib.sha256(raw).hexdigest()
    try:
        tx.OfflineWheelhouse(wheels, manifest, checksum, source, revision)
        admit(sha256(manifest) == checksum, "manifest_mutation")
    except BaseException:
        manifest.unlink(missing_ok=True)
        raise
    return checksum


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--emit-pins", action="store_true")
    parser.add_argument("--wheelhouse", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--revision")
    args = parser.parse_args()
    root = args.source_root.absolute()
    tx = transaction(root)
    if args.emit_pins:
        admit(args.wheelhouse is None and args.manifest is None and args.revision is None,
              "pin_arguments")
        pins = tx.runtime_versions(root / "scripts/windows-alpha/runtime-requirements.txt",
                                   target_platform="win32")
        for name, version in sorted(pins.items()):
            print(name + "==" + version)
        return
    admit(args.wheelhouse is not None and args.manifest is not None and args.revision is not None,
          "manifest_arguments")
    sha = build(root, args.wheelhouse.absolute(), args.manifest.absolute(), args.revision, tx)
    print("K5_OFFLINE_WHEELHOUSE_MANIFEST_SHA256=" + sha)
    print("K5_OFFLINE_WHEELHOUSE_EXTERNAL_APPROVAL=not_granted")


if __name__ == "__main__":
    main()
