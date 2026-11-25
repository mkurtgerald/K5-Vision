"""Read-only external observations during unchanged installed clock requests.

This diagnostic never qualifies a release. The original committed test owns all
requests, errors, deadlines, assertions, cleanup and Windows time restoration.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

SOURCE_HEAD = "b81a950975e50979f57b20fb13cdbfbe73fd1aac"
SAMPLER_SHA256 = "9d563a3a6efec500ebf62ce5133be949dab90610eab8e80cf909a7ea4a73a0d1"
SOURCE_HASHES = {
    ".github/actions/provision-operator-tools/action.yml": "e50c55c29830ed793c0500e52628d145580d23209f49c9369a2c218e722c0c00",
    ".github/workflows/operator-desktop-windows.yml": "c49a6024443d956b62db7cfecfb5acbd0ee4a4118726daa4a794d59390d048ec",
    "scripts/provision_operator_person_acceptance.py": "3dc414b2aa0ba92cae73ab02b81ffdca43386e5e0e1274c02dc0c52110d3c896",
    "scripts/verify_desktop_receipt.py": "1dacff23c919e09c64ff7723f8ec64d80e7660739d327dc9ebb770207f1495ca",
    "scripts/verify_windows_vm_clock_receipt.py": "b391e01b2276bd95e955263e50300507044239f75c1fd4978a5550e0c5a5be11",
    "tests/integration/test_operator_beta_browser.py": "67eea5d12df2cfe6e19603fe459405eac05c93e6db8d59c068a23f69a573f771",
    "tests/integration/test_operator_product_runtime.py": "d7c7eaa38fe36843a937e57d60b696a93bca66599b3883e51d84cd1f37345106",
    "tests/integration/test_windows_desktop_app.py": "e32632d81211987fccfb9b5412fcaa2759fe08ff0ad8db99b1dfba907efbf1ad",
    "tests/integration/test_windows_vm_clock_qualification.py": "cbd66d5cdf2b13a17c4f05e4e2c6fd10a7d50b77add65b8549a2d4d27721dcb8",
    "tests/integration/windows_vm_clock.py": "a43fb41cc3ad80ea84040c22691d20703906f884d6b586b6ccd3c86110ea10a5"
}


def _require(value):
    if not value:
        raise RuntimeError("Instrumented clock diagnostic admission refused")


def _load(path):
    spec = importlib.util.spec_from_file_location("k5_instrumented_clock_original", path)
    _require(spec is not None and spec.loader is not None)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _verify_sources(root):
    for relative, expected in SOURCE_HASHES.items():
        raw = (root / relative).read_bytes()
        normalized = raw.replace(b"\r\n", b"\n")
        _require(b"\r" not in normalized and hashlib.sha256(normalized).hexdigest() == expected)


def _is_camera_events_route(value):
    """Only a fixed public route kind escapes; no camera ID or query is emitted."""
    if type(value) is not str:
        return False
    parsed = urlsplit(value)
    parts = parsed.path.split("/")
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        return False
    if len(parts) != 7 or parts[:5] != ["", "api", "v1", "operator", "cameras"] or parts[6] != "events":
        return False
    try:
        return str(UUID(parts[5])) == parts[5]
    except ValueError:
        return False


class StackSamples:
    """Two samples overlap each original call; original calls never await them."""

    def __init__(self, helper, executable, index):
        self.helper, self.executable, self.index = helper, executable, index
        self.process, self.selection = None, "not_prepared"
        self.tickets = []
        self.sample_lock = threading.Lock()
        self.sample_count = 0

    def prepare(self, cohort):
        try:
            self.process, self.selection = self.helper.remembered_backend(cohort)
        except BaseException:
            self.process, self.selection = None, "owned_identity_inspection_failed"

    def start(self, kind="restored_history", route_note=None):
        _require(kind in {"restored_history", "forward_events", "backward_events"})
        _require(len(self.tickets) < 3 and all(ticket["kind"] != kind for ticket in self.tickets))
        ticket = {
            "kind": kind,
            "stop": threading.Event(),
            "samples": [],
            "failed": False,
            "route": route_note,
        }
        self.tickets.append(ticket)
        if self.process is None:
            return ticket
        began = time.monotonic()

        def work():
            try:
                for delay in (0.5, 1.75):
                    if ticket["stop"].wait(max(0, delay - (time.monotonic() - began))):
                        break
                    with self.sample_lock:
                        _require(self.sample_count < 6)
                        self.sample_count += 1
                    sample = self.helper.sample_once(self.executable, self.process, self.index)
                    ticket["samples"].append(
                        {
                            "scheduled_after_request_ms": round(delay * 1000),
                            "completed_after_request_ms": min(
                                90000, round((time.monotonic() - began) * 1000)
                            ),
                            "sample": sample,
                        }
                    )
            except BaseException:
                ticket["samples"].append(
                    {"sample": {"outcome": "sampler_exception", "threads": []}}
                )

        ticket["worker"] = threading.Thread(target=work, daemon=True)
        ticket["worker"].start()
        return ticket

    def stop(self, ticket, *, failed):
        ticket["failed"] = failed
        ticket["stop"].set()

    def finish(self):
        cleanup_failed = False
        for ticket in self.tickets:
            ticket["stop"].set()
            worker = ticket.get("worker")
            if worker is not None:
                try:
                    worker.join(timeout=1.5)
                    cleanup_failed = cleanup_failed or worker.is_alive()
                except BaseException:
                    cleanup_failed = True
            cleanup_failed = cleanup_failed or any(
                item.get("sample", {}).get("outcome") == "sampler_cleanup_incomplete"
                for item in ticket["samples"]
            )
        failed = [
            {
                "kind": ticket["kind"],
                "route": ticket["route"],
                "http_status": ticket.get("http_status"),
                "samples": ticket["samples"],
            }
            for ticket in self.tickets
            if ticket["failed"]
        ]
        return {
            "selection": self.selection,
            "original_requests_observed": len(self.tickets),
            "maximum_samples_per_original_request": 2,
            "maximum_original_requests": 3,
            "maximum_samples_per_vm": 6,
            "samples_invoked": self.sample_count,
            "additional_http_requests": 0,
            "original_request_waited_for_sampler": False,
            "success_samples_discarded": True,
            "cleanup_failed": cleanup_failed,
            "failed_requests": failed,
            "failed_request_samples": [
                ticket["samples"] for ticket in self.tickets if ticket["failed"]
            ],
        }


def _sampled_call(stacks, diagnostic, kind, original, args, kwargs, note=None):
    ticket = None
    try:
        ticket = stacks.start(kind, route_note=note)
    except BaseException:
        diagnostic["sampler_start_failed"] = True
    failed = False
    try:
        result = original(*args, **kwargs)
        try:
            status = result[0] if type(result) is tuple else getattr(result, "status", None)
            if type(status) is int:
                failed = status != 200
                if ticket is not None:
                    ticket["http_status"] = status if 100 <= status <= 599 else None
        except BaseException:
            diagnostic["sampler_response_status_unavailable"] = True
        return result
    except BaseException:
        failed = True
        raise
    finally:
        if ticket is not None:
            try:
                stacks.stop(ticket, failed=failed)
            except BaseException:
                diagnostic["sampler_stop_failed"] = True


def install_observation(target, stacks):
    original_loader = target._module
    original_observer = target._observe_clock_requests

    def loaded(name, path):
        module = original_loader(name, path)
        if name != "k5_clock_desktop":
            return module
        original_phase = module._vm_clock_phase

        def phase(*args, **kwargs):
            stacks.prepare(kwargs["cohort"])
            return original_phase(*args, **kwargs)

        module._vm_clock_phase = phase
        return module

    @contextmanager
    def observed(client, diagnostic):
        with original_observer(client, diagnostic):
            original_get = client.get

            def during_get(*args, **kwargs):
                route = args[0] if args else kwargs.get("route")
                stage = diagnostic.get("vm_clock_stage")
                kind = None
                if stage in {"forward_events_fetch", "backward_events_fetch"} and _is_camera_events_route(route):
                    kind = "forward_events" if stage == "forward_events_fetch" else "backward_events"
                elif stage == "restored_history_query" and target._clock_public_route(route) == "history":
                    kind = "restored_history"
                if kind is not None:
                    return _sampled_call(stacks, diagnostic, kind, original_get, args, kwargs)
                return original_get(*args, **kwargs)

            client.get = during_get
            try:
                yield
            finally:
                client.get = original_get

    target._module = loaded
    target._observe_clock_requests = observed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    _require(os.name == "nt" and os.environ.get("K5_VM_CLOCK_ACCEPTANCE") == "1")
    _require(os.environ.get("K5_DESKTOP_HEAD_SHA") == SOURCE_HEAD)
    _require(os.environ.get("GITHUB_RUN_ATTEMPT") == "1")
    _require(os.environ.get("K5_RESTORED_HISTORY_DIAGNOSTIC_PROBE") in {"1", "2", "3", "4"})
    root = Path.cwd()
    _verify_sources(root)
    sys.path.insert(0, str(root / "src"))
    sampler_path = Path(os.environ["K5_INSTRUMENTED_CLOCK_SAMPLER"])
    _require(hashlib.sha256(sampler_path.read_bytes()).hexdigest() == SAMPLER_SHA256)
    sampler = _load(sampler_path)
    executable = sampler.prepare_executable(Path(os.environ["K5_INSTRUMENTED_CLOCK_PYSPY_EXE"]))
    stacks = StackSamples(sampler, executable, sampler.public_index(root / "src/k5vision"))
    import xml.etree.ElementTree as ET

    import pytest

    target_path = root / "tests/integration/test_windows_vm_clock_qualification.py"
    node = (
        "tests/integration/test_windows_vm_clock_qualification.py::"
        "test_native_windows_vm_clock_qualification"
    )

    class ObservationPlugin:
        def pytest_collection_modifyitems(self, items):
            _require(len(items) == 1 and items[0].nodeid == node)
            target = items[0].module
            _require(Path(target.__file__).resolve(strict=True) == target_path.resolve(strict=True))
            install_observation(target, stacks)

    xml_path = args.output.with_suffix(".xml")
    _require(not xml_path.exists() and not args.output.exists())
    exit_code = 3
    try:
        exit_code = int(
            pytest.main(
                [
                    node,
                    "-o",
                    "addopts=",
                    "--no-cov",
                    "-s",
                    "--tb=short",
                    "-o",
                    "faulthandler_timeout=400",
                    "--junitxml=" + str(xml_path),
                ],
                plugins=[ObservationPlugin()],
            )
        )
    finally:
        samples = stacks.finish()
    properties = {}
    if xml_path.is_file():
        raw = xml_path.read_bytes()
        _require(0 < len(raw) <= 16 * 1024**2)
        _require(b"<!DOCTYPE" not in raw.upper() and b"<!ENTITY" not in raw.upper())
        for item in ET.fromstring(raw).iter("property"):
            name, value = item.get("name"), item.get("value")
            _require(
                name
                in {
                    "vm_clock_windows_diagnostic",
                    "vm_clock_windows_phase",
                    "vm_clock_windows_receipt",
                }
            )
            _require(name not in properties and type(value) is str and len(value) <= 1024**2)
            properties[name] = json.loads(value)
    outcome = "passed" if exit_code == 0 else "failed"
    diagnostic = properties.get("vm_clock_windows_diagnostic", {})
    error_kind = diagnostic.get("failure_exception_kind") if type(diagnostic) is dict else None
    report = {
        "schema_version": 1,
        "scope": "instrumented-clock-failure-diagnostic",
        "candidate_source_head": SOURCE_HEAD,
        "diagnostic_owner": {
            "dispatcher_head_sha": os.environ["K5_RESTORED_HISTORY_DIAGNOSTIC_DISPATCH_HEAD"],
            "run_id": int(os.environ["GITHUB_RUN_ID"]),
            "run_attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
            "matrix_probe": int(os.environ["K5_RESTORED_HISTORY_DIAGNOSTIC_PROBE"]),
        },
        "outcome": outcome,
        "original_pytest_exit_code": exit_code,
        "original_test_run_through_pytest": True,
        "error_kind": error_kind,
        "properties": properties,
        "sampler_enabled": True,
        "sampler_helper_sha256": SAMPLER_SHA256,
        "pyspy_executable_sha256": sampler.EXE_SHA256,
        "stack_sampling": samples,
        "original_failed_clock_jobs": [131638419488],
        "original_unit_guard_batch_retained": True,
        "publication_artifact_substitution_allowed": False,
        "release_qualification": False,
    }
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8"
    )
    print(
        json.dumps({"scope": report["scope"], "outcome": outcome, "release_qualification": False})
    )
    return 0 if outcome == "passed" and not samples["cleanup_failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
