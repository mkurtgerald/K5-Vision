"""The machine probe evaluates actual policy and emits finite metadata only."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace


def _probe():
    source = Path(__file__).resolve().parents[2] / "scripts/probe_windows_machine_admission.py"
    spec = importlib.util.spec_from_file_location("machine_admission_probe_under_test", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_probe_reports_actual_ancestor_refusal_without_private_names_or_paths(monkeypatch):
    probe = _probe()
    policy = probe._policy_module()
    entries = (
        policy.AccessEntry(0, 3, 0x001F01FF, "S-1-5-18"),
        policy.AccessEntry(0, 2, 0x00120116, "S-1-5-32-545"),
        policy.AccessEntry(0, 11, 0x001F01FF, "S-1-3-0"),
        policy.AccessEntry(0, 0, 0x00120089, "PRIVATE-ACCOUNT-SID"),
    )
    native = SimpleNamespace(
        descriptor=lambda _path: ("S-1-5-18", entries),
        current_user=lambda: "PRIVATE-CURRENT-SID",
    )
    monkeypatch.setattr(policy, "_native_security", lambda: native)
    result = probe.observe(policy, Path("PRIVATE-FOLDER"))
    assert result["completed"] is True and result["admitted"] is False
    assert result["owner_kind"] == "system"
    assert result["allow_aces"][1]["foreign_forbidden_mask"] == 0x110
    assert result["allow_aces"][2]["foreign_forbidden_mask"] == 0
    assert result["allow_aces"][3]["principal_kind"] == "other"
    assert "PRIVATE" not in json.dumps(result)


def test_probe_preserves_actual_policy_acceptance_and_caps_descriptor_projection(monkeypatch):
    probe = _probe()
    policy = probe._policy_module()
    entries = tuple(policy.AccessEntry(0, 2, 0x0012008F, "S-1-5-32-545") for _ in range(70))
    native = SimpleNamespace(
        descriptor=lambda _path: ("S-1-5-18", entries), current_user=lambda: "PRIVATE-CURRENT-SID"
    )
    monkeypatch.setattr(policy, "_native_security", lambda: native)
    result = probe.observe(policy, Path("PRIVATE-FOLDER"))
    assert result["admitted"] is True
    assert result["aces_capped"] is True and len(result["allow_aces"]) == 64
    assert all(entry["foreign_forbidden_mask"] == 0 for entry in result["allow_aces"])
