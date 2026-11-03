"""Admit actual Windows Time controls separately from clock-step resilience."""

from __future__ import annotations

import argparse
import json
import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path

_TRUE = {
    "native_windows_time_administration",
    "real_native_supervisor",
    "native_payload_private_staging",
    "native_configuration_readback",
    "native_resync_requested",
    "native_invalid_action_rejected",
    "native_hardlink_rejected",
    "native_existing_response_preserved",
    "native_birth_mismatch_rejected",
    "native_dead_owner_rejected",
    "native_reused_pid_rejected",
    "native_live_owner_bound",
    "time_sources_restored",
    "time_services_restored",
    "utc_restored",
    "independent_restoration_watchdog",
}
_FALSE = {"user_terminal_required", "absolute_accuracy_certified", "physical_camera_acceptance"}
_BOOL = {
    "native_resync_succeeded",
    "upstream_unavailable",
    "resync_timeout",
    "reference_measured",
    "reference_alignment_verified",
}
_HASH = re.compile(r"[a-f0-9]{64}\Z")


def _require(condition):
    if not condition:
        raise ValueError("Native Windows time administration evidence refused")


def _unique(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def verify(
    report: Path, *, expected_head: str, supervisor_sha256: str, payload_manifest_sha256: str
):
    _require(re.fullmatch(r"[a-f0-9]{40}", expected_head) is not None)
    _require(_HASH.fullmatch(supervisor_sha256) is not None)
    _require(_HASH.fullmatch(payload_manifest_sha256) is not None)
    _require(0 < report.stat().st_size <= 1024**2)
    root = ET.fromstring(report.read_bytes())
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    _require(len(suites) == 1)
    _require(
        all(
            suites[0].get(key) == value
            for key, value in {"tests": "1", "failures": "0", "errors": "0", "skipped": "0"}.items()
        )
    )
    cases = root.findall(".//testcase")
    _require(
        len(cases) == 1
        and not root.findall(".//failure")
        and not root.findall(".//error")
        and not root.findall(".//skipped")
    )
    _require(
        cases[0].get("name")
        == "test_native_windows_time_configuration_readback_and_resync_with_restoration"
    )
    values = [
        entry.get("value")
        for entry in root.findall(".//property")
        if entry.get("name") == "k5_native_time_administration_receipt"
    ]
    _require(len(values) == 1)
    receipt = json.loads(values[0], object_pairs_hook=_unique, parse_constant=lambda _: None)
    _require(type(receipt) is dict)
    _require(type(receipt.get("schema_version")) is int and receipt["schema_version"] == 1)
    _require(receipt.get("head_sha") == expected_head)
    _require(receipt.get("supervisor_sha256") == supervisor_sha256)
    _require(receipt.get("payload_manifest_sha256") == payload_manifest_sha256)
    _require(all(receipt.get(key) is True for key in _TRUE))
    _require(all(receipt.get(key) is False for key in _FALSE))
    _require(all(type(receipt.get(key)) is bool for key in _BOOL))
    _require(
        sum(
            receipt[key]
            for key in ("native_resync_succeeded", "upstream_unavailable", "resync_timeout")
        )
        == 1
    )
    _require(not receipt["reference_alignment_verified"] or receipt["reference_measured"])
    duration = receipt.get("duration_seconds")
    _require(type(duration) in (int, float) and math.isfinite(duration) and 0 < duration <= 240)
    _require(
        set(receipt)
        == _TRUE
        | _FALSE
        | _BOOL
        | {
            "schema_version",
            "head_sha",
            "supervisor_sha256",
            "payload_manifest_sha256",
            "duration_seconds",
        }
    )
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--expected-head", required=True)
    parser.add_argument("--supervisor-sha256", required=True)
    parser.add_argument("--payload-manifest-sha256", required=True)
    args = parser.parse_args()
    receipt = verify(
        args.report,
        expected_head=args.expected_head,
        supervisor_sha256=args.supervisor_sha256,
        payload_manifest_sha256=args.payload_manifest_sha256,
    )
    args.output.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "head_sha": args.expected_head,
                "lane": "windows_time_administration",
                "receipt": receipt,
            },
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print("Native Windows time administration and restoration evidence admitted")


if __name__ == "__main__":
    main()
