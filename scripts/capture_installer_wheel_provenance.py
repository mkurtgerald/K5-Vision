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
RUNTIME_REVISION = "e56a35de05f9ff0ddaf5d218dbeebb1c46d56835"
# Independently computed from the 136 exact tracked src/k5vision files at e56a35de.
RUNTIME_PAYLOAD_SHA256 = "592feceb21141f5618c3e5771cb16fb2eed244b725d5768ed4df67d973534fe2"
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


CAPTURE_CODES = frozenset(
    {
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
)
DIAGNOSTIC_STAGES = frozenset(
    {
        "unknown",
        "tool_loading",
        "arguments",
        "paths",
        "initial_input",
        "admitted_input",
        "expectations",
        "start_receipt_read",
        "start_receipt_validate",
        "normal_receipt_read",
        "normal_receipt_validate",
        "receipt_binding",
        "source_binding",
        "runtime_pins",
        "runtime_inventory",
        "qualified_inventory",
        "wheelhouse_binding",
        "dependency_inventory",
        "built_inventory",
        "built_identity",
        "runtime_payload",
        "host_identity",
        "host_admission",
        "ensurepip_identity",
        "marker_environment",
        "wheel_metadata",
        "archive_count",
        "installer_subset",
        "qualified_closure",
        "installer_closure",
        "installer_payload",
        "provenance_validate",
        "pre_retention_recheck",
        "storage_policy",
        "storage_root",
        "retention",
        "post_retention_recheck",
        "retained_report",
        "report_publication",
        "complete",
    }
)
DIAGNOSTIC_PACKAGES = frozenset(
    "annotated-doc annotated-types anyio attrs certifi charset-normalizer click fastapi h11 "
    "idna isodate lxml onvif-python opentelemetry-api platformdirs psutil pydantic "
    "pydantic-core pyreadline3 requests requests-file requests-toolbelt starlette "
    "typing-extensions typing-inspection urllib3 uvicorn zeep openvino "
    "opencv-python-headless numpy openvino-telemetry colorama k5-vision "
    "k5-analytics-runtime pip".split()
)
DIAGNOSTIC_ROLES = frozenset(
    {"alpha_runtime", "witness_only", "k5_runtime", "analytics_runtime", "ensurepip"}
)
RETENTION_STATES = frozenset({"not_started", "in_progress", "retained", "report_published"})
CLOSURE_CODES = frozenset(
    {
        "resource_limit",
        "invalid_name",
        "unsupported_version",
        "unsupported_specifier",
        "invalid_environment",
        "missing_environment",
        "selected_extras_unsupported",
        "unsupported_marker_comparison",
        "invalid_marker",
        "unknown_marker",
        "unsupported_extra_marker",
        "invalid_requirement",
        "direct_reference_unsupported",
        "invalid_inventory",
        "invalid_metadata",
        "duplicate_distribution",
        "requires_python_mismatch",
        "dependency_extras_unsupported",
        "missing_dependency",
        "dependency_version_mismatch",
    }
)


def diagnostic_scalar(value):
    return value if type(value) is bool or type(value) is int and 0 <= value < 2**31 else None


class CaptureDiagnostics:
    """In-memory fixed vocabulary only; never retain paths or exception text."""

    def __init__(self):
        self.retention_state = "not_started"
        self.enter("tool_loading")

    def enter(
        self, stage, *, package=None, role=None, expected=None, observed=None, wheel_index=None
    ):
        self.stage = stage if type(stage) is str and stage in DIAGNOSTIC_STAGES else "unknown"
        self.package = package if type(package) is str and package in DIAGNOSTIC_PACKAGES else None
        self.role = role if type(role) is str and role in DIAGNOSTIC_ROLES else None
        self.expected = diagnostic_scalar(expected)
        self.observed = diagnostic_scalar(observed)
        self.wheel_index = (
            wheel_index if type(wheel_index) is int and 1 <= wheel_index <= 36 else None
        )

    def retention(self, state):
        if type(state) is str and state in RETENTION_STATES:
            self.retention_state = state


def failure_diagnostic(error, diagnostic, tools=None):
    """Do not stringify errors, tracebacks, filenames or arbitrary metadata."""
    code, contract, error_class = "capture_refused", diagnostic.stage, "unexpected"
    expected, observed = diagnostic.expected, diagnostic.observed
    cleanup_pending, cleanup_known, field = False, False, None
    storage_acl = None
    if isinstance(error, CaptureError):
        error_class = "capture_contract"
        if type(error.code) is str and error.code in CAPTURE_CODES:
            code = contract = error.code
        if expected is None and observed is None:
            expected, observed = True, False
    elif tools is not None and isinstance(error, tools.transaction.OfflineAdmissionError):
        error_class, code = "offline_admission", "offline_admission"
        if (
            type(error.contract) is str
            and error.contract in tools.transaction.OFFLINE_ADMISSION_CONTRACTS
        ):
            contract = error.contract
        expected, observed = diagnostic_scalar(error.expected), diagnostic_scalar(error.observed)
    elif tools is not None and isinstance(error, tools.alpha.AlphaWitnessError):
        error_class, code = "alpha_contract", "alpha_contract"
        record = error.alpha_diagnostic
        # Revalidate the entire existing source-free contract before projecting.
        try:
            tools.alpha.validate_alpha_diagnostic(record)
        except (ValueError, TypeError, KeyError):
            pass
        else:
            contract, field = record["contract"], record["field"]
    elif tools is not None and isinstance(error, tools.alpha.common.WitnessError):
        error_class, code = "witness_contract", "witness_contract"
        if (
            len(error.args) == 1
            and type(error.args[0]) is str
            and error.args[0] in tools.alpha.common.FAILURES
        ):
            contract = error.args[0]
    elif tools is not None and isinstance(error, tools.closure.ClosureError):
        error_class = "closure_contract"
        if type(error.code) is str and error.code in CLOSURE_CODES:
            code = "closure_" + error.code
            contract = error.code
    elif tools is not None and isinstance(error, tools.storage.StorageError):
        error_class = "storage_contract"
        if type(error.code) is str and error.code in tools.storage._CODES:
            code = contract = error.code
        cleanup_pending = error.cleanup_pending is True
        cleanup_known = type(error.cleanup_pending) is bool
        if code == "storage_acl":
            storage_acl = tools.storage.bounded_acl_diagnostic(error.acl_diagnostic)
    else:
        for exception, category in (
            (FileNotFoundError, "missing_file"),
            (PermissionError, "permission_error"),
            (zipfile.BadZipFile, "invalid_zip"),
            (json.JSONDecodeError, "invalid_json"),
            (UnicodeError, "encoding_error"),
            (KeyError, "missing_key"),
            (ImportError, "import_error"),
            (AttributeError, "attribute_error"),
            (TypeError, "type_error"),
            (ValueError, "value_error"),
            (OSError, "io_error"),
            (AssertionError, "assertion_error"),
            (RuntimeError, "runtime_error"),
            (KeyboardInterrupt, "interrupted"),
            (SystemExit, "interrupted"),
        ):
            if isinstance(error, exception):
                error_class = category
                break
    return {
        "schema_version": "wheel-capture-failure-v1",
        "code": code,
        "stage": diagnostic.stage,
        "contract": contract,
        "error_class": error_class,
        "field": field,
        "package": diagnostic.package,
        "role": diagnostic.role,
        "wheel_index": diagnostic.wheel_index,
        "expected": expected,
        "observed": observed,
        "retention_state": diagnostic.retention_state,
        "cleanup_pending": cleanup_pending,
        "cleanup_known": cleanup_known,
        "storage_acl": storage_acl,
        "errno": diagnostic_scalar(error.errno) if isinstance(error, OSError) else None,
        "winerror": diagnostic_scalar(getattr(error, "winerror", None))
        if isinstance(error, OSError)
        else None,
    }


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
    raw, wheel_raw = inspector.verify_metadata(path, record)
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


def capture(
    args, *, tools=None, storage_policy=None, identity_reader=None, diagnostics=None
) -> dict:
    diagnostics = diagnostics or CaptureDiagnostics()
    diagnostics.enter("tool_loading")
    tools = tools or load_tools()
    diagnostics.enter("arguments")
    common = tools.alpha.common
    require(re.fullmatch(r"[1-9][0-9]{0,19}", args.run_id) is not None)
    require(re.fullmatch(r"[1-9][0-9]{0,9}", args.run_attempt) is not None)
    require(re.fullmatch(r"[0-9a-f]{40}", args.revision) is not None)
    require(re.fullmatch(r"[0-9a-f]{64}", args.base_python_sha256) is not None)
    root = args.launcher_root.absolute()
    workspace = args.workspace.absolute()
    runner_temp = args.runner_temp.absolute()
    runner_workspace = args.runner_workspace.absolute()
    diagnostics.enter("paths")
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
    diagnostics.enter("initial_input")
    initial, initial_hash = read_bound_json(initial_path, common)
    diagnostics.enter("admitted_input")
    expected, admitted_hash = read_bound_json(admitted_path, common)
    diagnostics.enter("expectations")
    tools.alpha.validate_expectations(initial, installed=False)
    tools.alpha.validate_expectations(expected)
    require(
        initial
        == {key: value for key, value in expected.items() if key != "runtime_identity_sha256"},
        "receipt_identity",
    )
    require(initial["revision"] == args.revision, "receipt_identity")
    diagnostics.enter("start_receipt_read")
    receipt, receipt_hash = read_bound_json(args.receipt.absolute(), common)
    diagnostics.enter("start_receipt_validate")
    tools.alpha.validate_receipt(receipt, expected)
    diagnostics.enter("normal_receipt_read")
    normal, normal_hash = read_bound_json(args.normal_receipt.absolute(), common)
    diagnostics.enter("normal_receipt_validate")
    common.validate_receipt(
        normal, revision=args.revision, identities={key: normal[key] for key in common.IDENTITIES}
    )
    diagnostics.enter("receipt_binding")
    for key in (
        "k5_payload_sha256",
        "analytics_manifest_sha256",
        "analytics_wheel_sha256",
        "model_identity_sha256",
        "seed_identity_sha256",
    ):
        require(normal[key] == expected[key], "receipt_identity")
    diagnostics.enter("source_binding")
    payload_hashes = source_identity(source, tools, initial)
    diagnostics.enter("runtime_pins")
    runtime_versions = tools.transaction.runtime_versions(
        source / "scripts/windows-alpha/runtime-requirements.txt", target_platform="win32"
    )
    diagnostics.enter("runtime_inventory", expected=28, observed=len(runtime_versions))
    require(len(runtime_versions) == 28, "runtime_inventory")
    qualified_versions = {
        **runtime_versions,
        **common.RUNTIME_VERSIONS,
        **common.WINDOWS_RUNTIME_VERSIONS,
    }
    diagnostics.enter("qualified_inventory", expected=33, observed=len(qualified_versions))
    require(len(qualified_versions) == 33, "runtime_inventory")
    diagnostics.enter("wheelhouse_binding")
    wheelhouse = root / "wheelhouse"
    require(
        common.digest(tools.alpha.tree_manifest(wheelhouse, maximum_files=256))
        == initial["wheelhouse_sha256"],
        "wheel_inventory",
    )
    diagnostics.enter("dependency_inventory", expected=33)
    dependencies = wheel_inventory(wheelhouse, qualified_versions, common)
    diagnostics.enter("built_inventory", expected=2)
    built = wheel_inventory(
        root / "wheels",
        {"k5-vision": "0.1.0", "k5-analytics-runtime": "0.0.0+g" + common.ANALYTICS_REVISION},
        common,
    )
    diagnostics.enter("built_identity")
    require(sha256(built["k5-vision"]) == initial["k5_wheel_sha256"], "runtime_identity")
    require(
        sha256(built["k5-analytics-runtime"]) == initial["analytics_wheel_sha256"],
        "runtime_identity",
    )
    diagnostics.enter("runtime_payload", expected=136, observed=len(payload_hashes))
    payload = runtime_payload(built["k5-vision"], payload_hashes)
    diagnostics.enter("host_identity")
    identity_reader = identity_reader or (lambda: native_identity(tools))
    identity = identity_reader()
    diagnostics.enter("host_admission")
    host = identity["python"]
    require(
        host["implementation"] == "cpython"
        and host["platform"] == "win_amd64"
        and re.fullmatch(r"3\.12\.[0-9]+", host["version"]) is not None,
        "host_identity",
    )
    require(host["executable_sha256"] == args.base_python_sha256, "host_identity")
    diagnostics.enter("ensurepip_identity", package="pip", role="ensurepip")
    pip = common.local_path(identity["bundled_pip"])
    require(sha256(pip) == host["ensurepip_wheel_sha256"], "host_identity")
    diagnostics.enter("marker_environment")
    marker_environment = identity["marker_environment"]
    require(
        marker_environment["sys_platform"] == "win32"
        and marker_environment["os_name"] == "nt"
        and marker_environment["python_full_version"] == host["version"],
        "host_identity",
    )
    records, retained = [], []
    for index, (name, path) in enumerate(sorted({**dependencies, **built, "pip": pip}.items()), 1):
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
        diagnostics.enter("wheel_metadata", package=name, role=role, wheel_index=index)
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
    diagnostics.enter("archive_count", expected=36, observed=len(records))
    require(len(records) == 36, "wheel_inventory")
    subset = [
        record
        for record in records
        if record["role"] in {"alpha_runtime", "k5_runtime", "ensurepip"}
    ]
    diagnostics.enter("installer_subset", expected=30, observed=len(subset))
    require(len(subset) == 30, "runtime_inventory")
    diagnostics.enter("qualified_closure", expected=36, observed=len(records))
    qualified_closure = tools.closure.verify_closure(closure_view(records), marker_environment)
    diagnostics.enter("installer_closure", expected=30, observed=len(subset))
    installer_closure = tools.closure.verify_closure(closure_view(subset), marker_environment)
    closures = {
        "qualified": qualified_closure,
        "installer": installer_closure,
        "marker_environment": marker_environment,
    }
    diagnostics.enter("installer_payload")
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
    diagnostics.enter("provenance_validate")
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
    diagnostics.enter("pre_retention_recheck")
    recheck_inputs()
    diagnostics.enter("storage_policy")
    storage_policy = storage_policy or tools.storage.NativeStoragePolicy()
    diagnostics.enter("storage_root")
    storage_root = tools.storage.derive_storage_root(
        runner_workspace, workspace, runner_temp, storage_policy
    )
    diagnostics.enter("retention", expected=36, observed=len(retained))
    diagnostics.retention("in_progress")
    retained_result = tools.storage.retain_bundle(
        storage_root,
        args.run_id,
        args.run_attempt,
        args.revision,
        retained,
        provenance,
        policy=storage_policy,
    )
    diagnostics.retention("retained")
    diagnostics.enter("post_retention_recheck")
    recheck_inputs()
    diagnostics.enter("retained_report")
    report = storage_root / retained_result["receipt_relative_path"]
    common.local_path(report)
    require(report.stat().st_size <= MAX_JSON, "output_limit")
    with report.open("rb") as stream:
        raw = stream.read(MAX_JSON + 1)
    require(len(raw) <= MAX_JSON, "output_limit")
    require(hashlib.sha256(raw).hexdigest() == retained_result["receipt_sha256"], "output_identity")
    # Only source-free JSON is placed under the approved artifact upload path.
    diagnostics.enter("report_publication")
    with args.output.absolute().open("xb") as stream:
        stream.write(raw)
    require(sha256(args.output.absolute()) == retained_result["receipt_sha256"], "output_identity")
    diagnostics.retention("report_published")
    diagnostics.enter("complete")
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
    diagnostics = CaptureDiagnostics()
    try:
        tools = load_tools()
        result = capture(args, tools=tools, diagnostics=diagnostics)
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
        print(
            "K5_WHEEL_PROVENANCE_FAILED="
            + json.dumps(failure_diagnostic(error, diagnostics, tools), sort_keys=True)
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
