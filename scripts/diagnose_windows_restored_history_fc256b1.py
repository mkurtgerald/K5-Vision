"""Observe public recovery only after the original restored-history failure.

This is a separate diagnostic owner, never a release witness. It executes the
original dedicated test and preserves its requests, error, assertions, cleanup
and clock restoration. At most two public read-only requests follow an already
failed restored-history call; the failed history request is never retried.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path

SOURCE_HEAD = "fc256b14e6e3063e641c60a3fd2262850720afa9"
SOURCE_HASHES = {
    "tests/integration/test_windows_vm_clock_qualification.py": (
        "3f94b77621f5c5d1bd597e68106d60b02c910dda845407c28ca7ed7920df624c"
    ),
    "tests/integration/test_windows_desktop_app.py": (
        "4b889b2d63345359d7617518aee0ff928511ce5036d0dc16626a641ddc832e7f"
    ),
    "tests/integration/windows_vm_clock.py": (
        "a43fb41cc3ad80ea84040c22691d20703906f884d6b586b6ccd3c86110ea10a5"
    ),
    "tests/integration/test_operator_product_runtime.py": (
        "dd8d5590be971ad0db99dbfcaf9bd0964251c84c8b891d0bfe7dfc953daa1a36"
    ),
    "scripts/verify_desktop_receipt.py": (
        "7455fc5df085aacfd54605adccf6bc51500b4ab23fcc51549e95b28fb08f1141"
    ),
    "scripts/verify_windows_vm_clock_receipt.py": (
        "971af433ea62357f00ad129d21ce52980ff38ebb2042a711028d532346e73dc1"
    ),
}


def _require(value):
    if not value:
        raise RuntimeError("Restored-history diagnostic admission refused")


def _load(path):
    spec = importlib.util.spec_from_file_location("k5_restored_history_original", path)
    _require(spec is not None and spec.loader is not None)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _verify_sources(root):
    for relative, expected in SOURCE_HASHES.items():
        raw = (root / relative).read_bytes()
        normalized = raw.replace(b"\r\n", b"\n")
        _require(b"\r" not in normalized and hashlib.sha256(normalized).hexdigest() == expected)


def _post_failure(target, desktop, client, private_context, began):
    """Use the already reviewed remembered-identity and public-state projection."""
    requests = []
    original_get = client.get
    had_override = "get" in vars(client)
    prior_override = vars(client).get("get")
    had_transport = "transport_failure" in vars(client)
    original_transport = vars(client).get("transport_failure")

    def observed(*args, **kwargs):
        route = target._clock_public_route(args[0] if args else kwargs.get("route"))
        # These two calls belong only to the original fixed public snapshot.
        _require(route in {"status", "cameras"} and len(requests) < 2)
        note = {"route": route, "response_received": False, "status": None}
        started = time.monotonic()
        try:
            result = original_get(*args, **kwargs)
        except BaseException as error:
            note["exception_kind"] = target._clock_exception_kind(error)
            try:
                note["transport_failure"] = target._clock_transport_note(client)
            except BaseException:
                note["transport_probe_failed"] = True
            raise
        else:
            try:
                note.update(target._clock_response_note(result, route))
            except BaseException:
                note.update(response_received=True, observation_failed=True)
            return result
        finally:
            note["elapsed_ms"] = min(90000, max(0, round((time.monotonic() - started) * 1000)))
            requests.append(note)

    client.get = observed
    try:
        state = desktop._native_clock_post_snapshot(
            client,
            private_context["camera_id"],
            private_context["cohort"],
            received_at=began,
        )
    finally:
        if had_override:
            client.get = prior_override
        else:
            del client.get
        if had_transport:
            client.transport_failure = original_transport
        elif "transport_failure" in vars(client):
            del client.transport_failure
    renamed = {
        key.replace("_after_response_ms", "_after_failure_ms"): value
        for key, value in state.items()
        if key != "timing"
    }
    return {
        "timing": "after_original_history_failure",
        "state_is_later_not_atomic": True,
        "original_history_request_retried": False,
        "state": renamed,
        "followup_requests": requests,
    }


def install_observation(target):
    """Keep the original observer's last failed call intact before probing later state."""
    original_loader = target._module
    original_observer = target._observe_clock_requests
    private_context = {}

    def loaded(name, path):
        module = original_loader(name, path)
        if name == "k5_clock_desktop":
            original_phase = module._vm_clock_phase
            private_context["desktop"] = module

            def phase(*args, **kwargs):
                private_context["camera_id"] = kwargs["camera_id"]
                private_context["cohort"] = kwargs["cohort"]
                return original_phase(*args, **kwargs)

            module._vm_clock_phase = phase
        return module

    @contextmanager
    def observed(client, diagnostic):
        try:
            with original_observer(client, diagnostic):
                yield
        except BaseException:
            began = time.monotonic()
            last = diagnostic.get("clock_last_request")
            guard = diagnostic.get("vm_clock_guard")
            if (
                diagnostic.get("vm_clock_stage") == "restored_playback"
                and type(last) is dict
                and last.get("route") == "history"
                and type(guard) is dict
                and guard.get("clock_restored") is True
                and guard.get("services_restored") is True
                and guard.get("reason") == "closed"
            ):
                try:
                    diagnostic["restored_history_post_failure"] = _post_failure(
                        target, private_context["desktop"], client, private_context, began
                    )
                except BaseException:
                    diagnostic["restored_history_post_failure_probe_failed"] = True
            raise

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
    target = _load(root / "tests/integration/test_windows_vm_clock_qualification.py")
    install_observation(target)
    properties = {}

    def record(name, value):
        _require(
            name
            in {"vm_clock_windows_diagnostic", "vm_clock_windows_phase", "vm_clock_windows_receipt"}
        )
        _require(name not in properties and type(value) is str and len(value) <= 1024**2)
        properties[name] = json.loads(value)

    outcome = "passed"
    error_kind = None
    try:
        target.test_native_windows_vm_clock_qualification(record)
    except BaseException as error:
        outcome = "failed"
        error_kind = target._clock_exception_kind(error)
    report = {
        "schema_version": 1,
        "scope": "restored-history-failure-diagnostic",
        "qualified_source_head": SOURCE_HEAD,
        "diagnostic_owner": {
            "dispatcher_head_sha": os.environ["K5_RESTORED_HISTORY_DIAGNOSTIC_DISPATCH_HEAD"],
            "run_id": int(os.environ["GITHUB_RUN_ID"]),
            "run_attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
            "matrix_probe": int(os.environ["K5_RESTORED_HISTORY_DIAGNOSTIC_PROBE"]),
        },
        "outcome": outcome,
        "error_kind": error_kind,
        "properties": properties,
        "publication_artifact_substitution_allowed": False,
        "release_qualification": False,
    }
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8"
    )
    print(
        json.dumps({"scope": report["scope"], "outcome": outcome, "release_qualification": False})
    )
    return 0 if outcome == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
