#!/usr/bin/env python3
"""Package admitted engineering worker bytes; never publish or install them."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import re
import tarfile
import urllib.request
import zipfile
from pathlib import Path

VERSION = "v1.21.2-k5-monotonic1"
OPENPGP = {
    "module": "github.com/benburkert/openpgp",
    "version": "v0.0.0-20160410205803-c2471f86866c",
    "sum": "h1:8XZeJrs4+ZYhJeJ2aZxADI2tGADS15AzIF8MQ8XAhT4=",
    "url": "https://proxy.golang.org/github.com/benburkert/openpgp/@v/v0.0.0-20160410205803-c2471f86866c.zip",
    "archive_sha256": "9607d644f1e542707e688eed0eb1f0b110c2a1a08794e4c5a2930da0a0f7a68f",
    "archive_bytes": 152756,
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def document(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def supplement_openpgp():
    with urllib.request.urlopen(OPENPGP["url"], timeout=30) as response:
        data = response.read(1_048_577)
    if len(data) != OPENPGP["archive_bytes"] or digest(data) != OPENPGP["archive_sha256"]:
        raise ValueError("Exact openpgp source notice archive identity differs")
    prefix = OPENPGP["module"] + "@" + OPENPGP["version"] + "/"
    blocks = []
    source_hashes = {}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for name in sorted(archive.namelist()):
            if not name.startswith(prefix) or not name.endswith(".go") or name.endswith("_test.go"):
                continue
            relative = name[len(prefix) :]
            if "testdata" in Path(relative).parts:
                continue
            source = archive.read(name)
            header = re.split(rb"(?m)^package\s+", source, maxsplit=1)[0]
            if b"Copyright" in header:
                blocks.append(("Source: " + relative + "\n").encode() + header.rstrip() + b"\n")
                source_hashes[relative] = digest(source)
    notices = b"\n".join(blocks)
    if (
        b"Copyright 2014 Matthew Endsley" not in notices
        or b"Redistributions in binary form must reproduce" not in notices
        or b"Copyright 2011 The Go Authors" not in notices
    ):
        raise ValueError("Required upstream copyright and redistribution notices missing")
    provenance = {
        **OPENPGP,
        "source_file_sha256": source_hashes,
        "scope": "Unchanged source headers; Go BSD terms retained as Go-LICENSE",
    }
    return notices, provenance


def static_archives(files, output, platform, binary):
    suffix = ".zip" if platform == "windows_amd64" else ".tar.gz"
    target = output / ("mediamtx_" + VERSION + "_" + platform + suffix)
    members = {**files, binary.name: binary.read_bytes()}
    if suffix == ".zip":
        with zipfile.ZipFile(
            target, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as archive:
            for name, data in sorted(members.items()):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, data, compresslevel=9)
    else:
        with (
            target.open("xb") as raw,
            gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed,
        ):
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.GNU_FORMAT) as archive:
                for name, data in sorted(members.items()):
                    info = tarfile.TarInfo(name)
                    info.size, info.mtime, info.uid, info.gid = len(data), 0, 0, 0
                    info.mode = 0o755 if name == binary.name else 0o644
                    archive.addfile(info, io.BytesIO(data))
    return {
        "platform": platform,
        "filename": target.name,
        "bytes": target.stat().st_size,
        "sha256": digest(target.read_bytes()),
        "executable": binary.name,
        "executable_bytes": binary.stat().st_size,
        "executable_sha256": digest(binary.read_bytes()),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--linux-smoke", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = args.directory.resolve()
    proof = json.loads((source / "build-provenance.json").read_text())
    smoke = json.loads(args.linux_smoke.read_text())
    if (
        proof.get("status") != "passed"
        or proof.get("reproducible_binary") is not True
        or proof.get("product_qualification") is not False
        or proof.get("derivative_version") != VERSION
        or smoke.get("status") != "passed"
        or smoke.get("native_linux") is not True
        or smoke.get("cleanup_complete") is not True
        or smoke.get("source_head") != proof["source_head"]
    ):
        raise ValueError("Required native engineering proofs differ")
    windows, linux = source / "mediamtx.exe", source / "mediamtx"
    for executable, pin in (
        (windows, {"sha256": proof["executable_sha256"], "bytes": proof["executable_bytes"]}),
        (linux, proof["linux_cross_build"]),
    ):
        if (
            executable.is_symlink()
            or executable.stat().st_size != pin["bytes"]
            or digest(executable.read_bytes()) != pin["sha256"]
        ):
            raise ValueError("Admitted worker executable identity differs")
    if smoke["executable_sha256"] != proof["linux_cross_build"]["sha256"]:
        raise ValueError("Linux smoke admits different executable bytes")
    inventory = json.loads((source / "notice-inventory.json").read_text())
    files = {}
    for entry in inventory:
        name = entry["notice"]
        if Path(name).name != name:
            raise ValueError("Notice inventory path is not a fixed leaf")
        data = (source / "licenses" / name).read_bytes()
        if digest(data) != entry["sha256"]:
            raise ValueError("Retained notice content identity differs")
        files["licenses/" + name] = data
    notices, supplemental = supplement_openpgp()
    notice_name = "github.com__benburkert__openpgp__SOURCE-NOTICES.txt"
    files["licenses/" + notice_name] = notices
    inventory.append(
        {
            "module": OPENPGP["module"],
            "version": OPENPGP["version"],
            "notice": notice_name,
            "sha256": digest(notices),
            "terms": "Go-LICENSE plus exact per-file BSD notices",
        }
    )
    dependencies = {}
    for info in ("build-info-first.txt", "build-info-linux-amd64.txt"):
        for line in (source / info).read_text().splitlines():
            fields = line.strip().split("\t")
            if len(fields) >= 3 and fields[0] == "dep":
                pin = {"version": fields[2], "sum": fields[3] if len(fields) > 3 else None}
                if fields[1] in dependencies and dependencies[fields[1]] != pin:
                    raise ValueError("Target binaries embed conflicting module identities")
                dependencies[fields[1]] = pin
    if dependencies.get(OPENPGP["module"]) != {
        "version": OPENPGP["version"],
        "sum": OPENPGP["sum"],
    }:
        raise ValueError("Supplemental source is not the exact compiled openpgp module")
    missing = set(dependencies) - {entry["module"] for entry in inventory}
    if missing:
        raise ValueError("Compiled dependency lacks a retained notice")
    files["LICENSE"] = (source / "licenses/MediaMTX-MIT.txt").read_bytes()
    files["notice-inventory.json"] = document(inventory)
    files["provenance/openpgp-source-notices.json"] = document(supplemental)
    files["provenance/linux-native-smoke.json"] = args.linux_smoke.read_bytes()
    for name in (
        "build-provenance.json",
        "build-info-first.txt",
        "build-info-linux-amd64.txt",
        "gortsplib-original-native-regression.jsonl",
        "gortsplib-native-regression.jsonl",
        "gortsplib-5.6.7-monotonic-tcp-watchdog.patch",
        "gortsplib-5.6.7-monotonic-tcp-watchdog.json",
        "mediamtx-clock-derivative.json",
        "THIRD_PARTY_NOTICES.md",
    ):
        files["provenance/" + name] = (source / name).read_bytes()
    retained = [
        {"path": name, "bytes": len(data), "sha256": digest(data)}
        for name, data in sorted(files.items())
    ]
    args.output.mkdir(parents=True, exist_ok=False)
    archives = [
        static_archives(files, args.output, "windows_amd64", windows),
        static_archives(files, args.output, "linux_amd64", linux),
    ]
    package = {
        "schema_version": 1,
        "scope": "engineering_dependency_archives_not_installer_qualification",
        "version": VERSION,
        "product_qualification": False,
        "source_head": proof["source_head"],
        "windows_reproducibility_observed": True,
        "linux_native_smoke_observed": True,
        "compiled_module_count": len(dependencies),
        "compiled_module_notices_retained": True,
        "archive_members_excluding_binary": retained,
        "artifacts": archives,
    }
    (args.output / "package-manifest.json").write_bytes(document(package))
    print(json.dumps({"version": VERSION, "artifacts": archives, "product_qualification": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
