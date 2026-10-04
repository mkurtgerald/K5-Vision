#!/usr/bin/env python3
"""Read already-qualified native archives; retain only exact generated wheel bytes.

No resolver, installer, source build, network client or wheel code is invoked.
Native execution is a separately admitted workflow operation. This module is
import-safe for generated portable fixtures; success is artifact capture only.
"""

from __future__ import annotations

import argparse
import ensurepip
import hashlib
import importlib.util
import json
import os
import platform
import re
import sys
import zipfile
from email.parser import BytesParser
from pathlib import Path
from types import SimpleNamespace

SCHEMA = "k5-native-wheel-provenance-v1"
OUTPUT_NAME = "installed-alpha-wheel-provenance.json"
RUNTIME_REVISION = "eccd0cb88c31e75c98d328ebb5fb7f0407ea5cd1"
# Independently computed from the 132 exact tracked src/k5vision files at eccd.
RUNTIME_PAYLOAD_SHA256 = "c755ac54055c3d36ca12da20089f349b76477c7f1757c88b2cda9751160de92e"
MAX_JSON = 1024 * 1024
MAX_WHEEL = 256 * 1024 * 1024
MODULES = (
    "capture_installer_wheel_provenance.py",
    "installer_wheel_requirements.py",
    "installer_wheel_storage.py",
    "installed_alpha_launcher_witness.py",
    "installed_analytics_witness.py",
    "windows_owned_preflight.py",
)


class CaptureError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def require(condition: bool, code: str = "input_identity") -> None:
    if not condition:
        raise CaptureError(code)


def canonical(value: object) -> bytes:
    raw = (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()
    require(len(raw) <= MAX_JSON, "output_limit")
    return raw


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    require(spec is not None and spec.loader is not None)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_tools():
    scripts = Path(__file__).absolute().parent
    return SimpleNamespace(
        alpha=load_module("_capture_alpha", scripts / "installed_alpha_launcher_witness.py"),
        transaction=load_module(
            "_capture_transaction", scripts / "windows-alpha/install_transaction.py"
        ),
        closure=load_module("_capture_closure", scripts / "installer_wheel_requirements.py"),
        storage=load_module("_capture_storage", scripts / "installer_wheel_storage.py"),
    )


def native_identity(tools) -> dict:
    require(sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode, "host_identity")
    host = tools.transaction._host_identity()
    require(os.name == "nt" and host["platform"] == "win_amd64", "host_identity")
    bundled = (
        Path(ensurepip.__file__).parent
        / "_bundled"
        / f"pip-{host['ensurepip_version']}-py3-none-any.whl"
    )
    markers = {
        "python_version": ".".join(map(str, sys.version_info[:2])),
        "python_full_version": host["version"],
        "implementation_name": sys.implementation.name,
        "implementation_version": platform.python_version(),
        "os_name": os.name,
        "sys_platform": sys.platform,
        "platform_machine": platform.machine(),
        "platform_system": platform.system(),
        "platform_release": platform.release(),
        "platform_version": platform.version(),
        "platform_python_implementation": platform.python_implementation(),
    }
    return {"python": host, "bundled_pip": bundled, "marker_environment": markers}


def read_bound_json(path: Path, common, *, limit=16384) -> tuple[dict, str]:
    common.local_path(path)
    before = sha256(path)
    value = common.read_json(path, limit=limit)
    require(sha256(path) == before, "input_changed")
    return value, before


def source_identity(source: Path, tools, expected: dict) -> dict[str, str]:
    common = tools.alpha.common
    require(
        common.digest(tools.alpha.tree_manifest(source)) == expected["source_tree_sha256"],
        "source_identity",
    )
    for name in MODULES:
        require(
            (source / "scripts" / name).read_bytes() == Path(__file__).with_name(name).read_bytes(),
            "source_identity",
        )
    transaction = source / "scripts/windows-alpha/install_transaction.py"
    require(
        transaction.read_bytes() == Path(tools.transaction.__file__).read_bytes(), "source_identity"
    )
    payload = tools.alpha.source_payload(source)
    require(
        common.digest(payload) == expected["k5_payload_sha256"] == RUNTIME_PAYLOAD_SHA256,
        "runtime_identity",
    )
    require(
        sha256(source / "src/k5vision/data/analytics-runtime-manifest.json")
        == expected["analytics_manifest_sha256"],
        "runtime_identity",
    )
    return payload


def runtime_payload(wheel: Path, expected: dict[str, str]) -> dict[str, dict]:
    with zipfile.ZipFile(wheel) as archive:
        actual = {
            entry.filename
            for entry in archive.infolist()
            if entry.filename.startswith("k5vision/") and not entry.is_dir()
        }
        require(actual == expected.keys(), "runtime_identity")
        result = {}
        for name, digest in expected.items():
            info = archive.getinfo(name)
            require(info.file_size <= 8 * 1024 * 1024, "wheel_limit")
            raw = archive.read(info)
            require(hashlib.sha256(raw).hexdigest() == digest, "runtime_identity")
            result[name] = {"size": len(raw), "sha256": digest}
        return result


def inspect_wheel(path: Path, name: str, version: str, role: str, tools, payload: dict) -> dict:
    tools.alpha.common.local_path(path)
    require(path.stat().st_size <= MAX_WHEEL, "wheel_limit")
    before = sha256(path)
    parts = path.name[:-4].split("-")
    require(path.suffix == ".whl" and len(parts) in (5, 6), "wheel_metadata")
    tags = sorted(tools.transaction._tags("-".join(parts[-3:])))
    record = {
        "filename": path.name,
        "name": name,
        "version": version,
        "tags": tags,
        "size": path.stat().st_size,
        "sha256": before,
    }
    inspector = object.__new__(tools.transaction.OfflineWheelhouse)
    inspector.data = {"runtime_payload": payload}
    inspector.verify_metadata(path, record)
    prefix = f"{parts[0]}-{parts[1]}.dist-info/"
    with zipfile.ZipFile(path) as archive:
        raw = archive.read(prefix + "METADATA")
        wheel_raw = archive.read(prefix + "WHEEL")
    metadata = BytesParser().parsebytes(raw)
    requires_python = metadata.get_all("Requires-Python", [])
    require(len(requires_python) <= 1, "wheel_metadata")
    requires_dist = metadata.get_all("Requires-Dist", [])
    require(
        len(requires_dist) <= 256 and all(len(value) <= 4096 for value in requires_dist),
        "wheel_limit",
    )
    for requirement in requires_dist:
        require(
            not re.search(
                r"(?i)(?:[a-z][a-z0-9+.-]*://|\\\\|(?:^|[\s'\"(=])/|[a-z]:[\\/])", requirement
            ),
            "metadata_privacy",
        )
    require(sha256(path) == before, "input_changed")
    return {
        **record,
        "role": role,
        "requires_python": requires_python[0] if requires_python else "",
        "requires_dist": requires_dist,
        "metadata_sha256": hashlib.sha256(raw).hexdigest(),
        "wheel_metadata_sha256": hashlib.sha256(wheel_raw).hexdigest(),
    }


def wheel_inventory(directory: Path, versions: dict[str, str], common) -> dict[str, Path]:
    common.local_path(directory, directory=True)
    paths = list(directory.iterdir())
    require(len(paths) == len(versions), "wheel_inventory")
    result = {}
    for path in paths:
        common.local_path(path)
        require(path.suffix == ".whl" and len(path.name) <= 255, "wheel_inventory")
        name = re.sub(r"[-_.]+", "-", path.name.split("-", 1)[0]).lower()
        require(name in versions and name not in result, "wheel_inventory")
        result[name] = path
    require(result.keys() == versions.keys(), "wheel_inventory")
    return result


def closure_view(records: list[dict]) -> list[dict]:
    return [
        {key: record[key] for key in ("name", "version", "requires_python", "requires_dist")}
        for record in records
    ]


def validate_provenance(value: dict) -> None:
    require(
        value.keys()
        == {
            "schema_version",
            "scope",
            "qualification",
            "runtime",
            "installer",
            "python",
            "wheels",
            "closure",
            "installer_subset",
            "installer_accepted",
        },
        "output_schema",
    )
    require(
        value["schema_version"] == SCHEMA
        and value["scope"] == "artifact-capture-only"
        and value["installer_accepted"] is False,
        "output_schema",
    )
    require(len(value["wheels"]) == 36 and len(value["installer_subset"]) == 30, "output_schema")
    require(
        value["closure"]["qualified"]["result"]
        == value["closure"]["installer"]["result"]
        == "pass",
        "output_schema",
    )
    require(
        value["closure"]["qualified"]["wheel_count"] == 36
        and value["closure"]["installer"]["wheel_count"] == 30,
        "output_schema",
    )
    require(
        value["runtime"]["revision"] == RUNTIME_REVISION
        and value["runtime"]["payload_sha256"] == RUNTIME_PAYLOAD_SHA256,
        "output_schema",
    )
    canonical(value)


def capture(args, *, tools=None, storage_policy=None, identity_reader=None) -> dict:
    tools = tools or load_tools()
    common = tools.alpha.common
    require(re.fullmatch(r"[1-9][0-9]{0,19}", args.run_id) is not None)
    require(re.fullmatch(r"[1-9][0-9]{0,9}", args.run_attempt) is not None)
    require(re.fullmatch(r"[0-9a-f]{40}", args.revision) is not None)
    require(re.fullmatch(r"[0-9a-f]{64}", args.base_python_sha256) is not None)
    root = args.launcher_root.absolute()
    workspace = args.workspace.absolute()
    runner_temp = args.runner_temp.absolute()
    runner_workspace = args.runner_workspace.absolute()
    for path in (root, workspace, runner_temp, runner_workspace):
        tools.transaction._offline_path(path)
    require(root == runner_temp / f"k5-alpha-launcher-{args.run_id}-{args.run_attempt}")
    require(args.receipt.absolute() == workspace / "artifacts" / tools.alpha.RECEIPT_NAME)
    require(
        args.normal_receipt.absolute()
        == workspace / "artifacts" / "installed-analytics-candidate.json"
    )
    require(args.output.absolute() == workspace / "artifacts" / OUTPUT_NAME)
    require(not os.path.lexists(args.output), "output_exists")
    common.local_path(root, directory=True)
    common.local_path(workspace, directory=True)
    common.local_path(args.output.absolute().parent, directory=True)
    source = root / "source"
    initial_path = root / "inputs.json"
    admitted_path = root / tools.alpha.EXPECTATIONS_NAME
    initial, initial_hash = read_bound_json(initial_path, common)
    expected, admitted_hash = read_bound_json(admitted_path, common)
    tools.alpha.validate_expectations(initial, installed=False)
    tools.alpha.validate_expectations(expected)
    require(
        initial
        == {key: value for key, value in expected.items() if key != "runtime_identity_sha256"},
        "receipt_identity",
    )
    require(initial["revision"] == args.revision, "receipt_identity")
    receipt, receipt_hash = read_bound_json(args.receipt.absolute(), common)
    tools.alpha.validate_receipt(receipt, expected)
    normal, normal_hash = read_bound_json(args.normal_receipt.absolute(), common)
    common.validate_receipt(
        normal, revision=args.revision, identities={key: normal[key] for key in common.IDENTITIES}
    )
    for key in (
        "k5_payload_sha256",
        "analytics_manifest_sha256",
        "analytics_wheel_sha256",
        "model_identity_sha256",
        "seed_identity_sha256",
    ):
        require(normal[key] == expected[key], "receipt_identity")
    payload_hashes = source_identity(source, tools, initial)
    runtime_versions = tools.transaction.runtime_versions(
        source / "scripts/windows-alpha/runtime-requirements.txt", target_platform="win32"
    )
    require(len(runtime_versions) == 28, "runtime_inventory")
    qualified_versions = {
        **runtime_versions,
        **common.RUNTIME_VERSIONS,
        **common.WINDOWS_RUNTIME_VERSIONS,
    }
    require(len(qualified_versions) == 33, "runtime_inventory")
    wheelhouse = root / "wheelhouse"
    require(
        common.digest(tools.alpha.tree_manifest(wheelhouse, maximum_files=256))
        == initial["wheelhouse_sha256"],
        "wheel_inventory",
    )
    dependencies = wheel_inventory(wheelhouse, qualified_versions, common)
    built = wheel_inventory(
        root / "wheels",
        {"k5-vision": "0.1.0", "k5-analytics-runtime": "0.0.0+g" + common.ANALYTICS_REVISION},
        common,
    )
    require(sha256(built["k5-vision"]) == initial["k5_wheel_sha256"], "runtime_identity")
    require(
        sha256(built["k5-analytics-runtime"]) == initial["analytics_wheel_sha256"],
        "runtime_identity",
    )
    payload = runtime_payload(built["k5-vision"], payload_hashes)
    identity_reader = identity_reader or (lambda: native_identity(tools))
    identity = identity_reader()
    host = identity["python"]
    require(
        host["implementation"] == "cpython"
        and host["platform"] == "win_amd64"
        and re.fullmatch(r"3\.12\.[0-9]+", host["version"]) is not None,
        "host_identity",
    )
    require(host["executable_sha256"] == args.base_python_sha256, "host_identity")
    pip = common.local_path(identity["bundled_pip"])
    require(sha256(pip) == host["ensurepip_wheel_sha256"], "host_identity")
    marker_environment = identity["marker_environment"]
    require(
        marker_environment["sys_platform"] == "win32"
        and marker_environment["os_name"] == "nt"
        and marker_environment["python_full_version"] == host["version"],
        "host_identity",
    )
    records, retained = [], []
    for name, path in sorted({**dependencies, **built, "pip": pip}.items()):
        version = qualified_versions.get(name)
        if name == "pip":
            version, role, group = host["ensurepip_version"], "ensurepip", "ensurepip"
        elif name == "k5-vision":
            version, role, group = "0.1.0", "k5_runtime", "built-wheels"
        elif name == "k5-analytics-runtime":
            version, role, group = (
                "0.0.0+g" + common.ANALYTICS_REVISION,
                "analytics_runtime",
                "built-wheels",
            )
        else:
            role, group = (
                ("alpha_runtime" if name in runtime_versions else "witness_only"),
                "wheelhouse",
            )
        record = inspect_wheel(path, name, version, role, tools, payload)
        record["relative_path"] = group + "/" + path.name
        records.append(record)
        retained.append(
            {
                "source": path,
                "relative_path": record["relative_path"],
                "size": record["size"],
                "sha256": record["sha256"],
            }
        )
    require(len(records) == 36, "wheel_inventory")
    subset = [
        record
        for record in records
        if record["role"] in {"alpha_runtime", "k5_runtime", "ensurepip"}
    ]
    require(len(subset) == 30, "runtime_inventory")
    closures = {
        "qualified": tools.closure.verify_closure(closure_view(records), marker_environment),
        "installer": tools.closure.verify_closure(closure_view(subset), marker_environment),
        "marker_environment": marker_environment,
    }
    installer_payload = {
        name: {"size": (source / name).stat().st_size, "sha256": sha256(source / name)}
        for name in tools.transaction.PAYLOAD_FILES
    }
    provenance = {
        "schema_version": SCHEMA,
        "scope": "artifact-capture-only",
        "installer_accepted": False,
        "qualification": {
            "repository": "mkurtgerald/K5-Vision",
            "run_id": args.run_id,
            "run_attempt": args.run_attempt,
            "candidate_revision": args.revision,
            "expectations_sha256": initial_hash,
            "admitted_expectations_sha256": admitted_hash,
            "receipt_sha256": receipt_hash,
            "normal_receipt_sha256": normal_hash,
            "source_tree_sha256": initial["source_tree_sha256"],
            "original_wheelhouse_sha256": initial["wheelhouse_sha256"],
        },
        "runtime": {
            "revision": RUNTIME_REVISION,
            "payload": payload,
            "payload_sha256": RUNTIME_PAYLOAD_SHA256,
            "requirements_sha256": tools.transaction.REQUIREMENTS_SHA256,
        },
        "installer": {"revision": args.revision, "payload": installer_payload},
        "python": host,
        # Metadata hashes and normalized dependency edges are sufficient here.
        # Raw marker strings can contain private paths even on inactive edges.
        "wheels": [
            {
                key: value
                for key, value in record.items()
                if key not in {"requires_dist", "requires_python"}
            }
            for record in records
        ],
        "closure": closures,
        "installer_subset": [record["relative_path"] for record in subset],
    }
    validate_provenance(provenance)

    def recheck_inputs():
        require(source_identity(source, tools, initial) == payload_hashes, "input_changed")
        require(
            common.digest(tools.alpha.tree_manifest(wheelhouse, maximum_files=256))
            == initial["wheelhouse_sha256"],
            "input_changed",
        )
        for path, expected_hash in (
            (initial_path, initial_hash),
            (admitted_path, admitted_hash),
            (args.receipt.absolute(), receipt_hash),
            (args.normal_receipt.absolute(), normal_hash),
        ):
            require(sha256(path) == expected_hash, "input_changed")
        require(identity_reader() == identity, "host_identity")

    # Admission is checked both before mutation and after copying, before any
    # source-free receipt is published. Retained bytes alone are not acceptance.
    recheck_inputs()
    storage_policy = storage_policy or tools.storage.NativeStoragePolicy()
    storage_root = tools.storage.derive_storage_root(
        runner_workspace, workspace, runner_temp, storage_policy
    )
    retained_result = tools.storage.retain_bundle(
        storage_root,
        args.run_id,
        args.run_attempt,
        args.revision,
        retained,
        provenance,
        policy=storage_policy,
    )
    recheck_inputs()
    report = storage_root / retained_result["receipt_relative_path"]
    common.local_path(report)
    require(report.stat().st_size <= MAX_JSON, "output_limit")
    with report.open("rb") as stream:
        raw = stream.read(MAX_JSON + 1)
    require(len(raw) <= MAX_JSON, "output_limit")
    require(hashlib.sha256(raw).hexdigest() == retained_result["receipt_sha256"], "output_identity")
    # Only source-free JSON is placed under the approved artifact upload path.
    with args.output.absolute().open("xb") as stream:
        stream.write(raw)
    require(sha256(args.output.absolute()) == retained_result["receipt_sha256"], "output_identity")
    return retained_result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "launcher-root",
        "receipt",
        "normal-receipt",
        "workspace",
        "runner-workspace",
        "runner-temp",
        "output",
    ):
        parser.add_argument("--" + name, required=True, type=Path)
    for name in ("revision", "run-id", "run-attempt", "base-python-sha256"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    tools = None
    try:
        tools = load_tools()
        result = capture(args, tools=tools)
        print(
            "K5_WHEEL_PROVENANCE="
            + json.dumps(
                {
                    key: result[key]
                    for key in ("bundle_key", "receipt_sha256", "archive_count", "archive_bytes")
                },
                sort_keys=True,
            )
        )
        return 0
    except BaseException as error:
        allowed = {
            "input_identity",
            "input_changed",
            "source_identity",
            "runtime_identity",
            "receipt_identity",
            "wheel_inventory",
            "wheel_metadata",
            "wheel_limit",
            "runtime_inventory",
            "host_identity",
            "output_schema",
            "output_limit",
            "output_exists",
            "output_identity",
            "metadata_privacy",
        }
        code = (
            error.code
            if isinstance(error, CaptureError) and error.code in allowed
            else "capture_refused"
        )
        cleanup_pending = False
        if tools is not None and isinstance(error, tools.closure.ClosureError):
            code = "closure_" + error.code
        if tools is not None and isinstance(error, tools.storage.StorageError):
            code = error.code
            cleanup_pending = error.cleanup_pending
        if not re.fullmatch(r"[a-z_]{1,80}", code):
            code = "capture_refused"
        print(
            "K5_WHEEL_PROVENANCE_FAILED="
            + json.dumps({"code": code, "cleanup_pending": cleanup_pending}, sort_keys=True)
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
