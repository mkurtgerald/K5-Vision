"""Generated dependency metadata only; no package or native runtime is executed."""

from __future__ import annotations

import hashlib
import importlib.util
from email.parser import BytesParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = ROOT / "scripts/windows-alpha/runtime-requirements.txt"
WINDOWS_PIN = 'pyreadline3==3.5.6; sys_platform == "win32"'
LEGACY_SHA256 = "ba1ae7620ce4f660fc5e6a7fd2ace352e9c1859972cb09e8b7f76449272fcb52"


def load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


transaction = load("platform_transaction", "scripts/windows-alpha/install_transaction.py")
common = load("platform_witness", "scripts/installed_analytics_witness.py")
closure = load("platform_closure", "scripts/installer_wheel_requirements.py")


def environment(platform: str) -> dict[str, str]:
    return {
        "python_version": "3.12",
        "python_full_version": "3.12.10",
        "sys_platform": platform,
    }


def generated_metadata(versions: dict[str, str]) -> list[dict]:
    """Model only the observed onvif edge, not unaudited actual wheel metadata."""
    wheels = []
    for name, version in versions.items():
        raw = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
        raw += "Requires-Python: >=3.12\n"
        if name == "onvif-python":
            raw += 'Requires-Dist: pyreadline3>=3.5.4; sys_platform == "win32"\n'
        parsed = BytesParser().parsebytes(raw.encode("ascii"))
        wheels.append(
            {
                "name": parsed["Name"],
                "version": parsed["Version"],
                "requires_python": parsed["Requires-Python"],
                "requires_dist": parsed.get_all("Requires-Dist", []),
            }
        )
    return wheels


def test_runtime_requirements_pin_observed_windows_package():
    assert WINDOWS_PIN in REQUIREMENTS.read_text(encoding="utf-8").splitlines()


def test_legacy_27_pins_remain_byte_identical():
    # The only requirements change is the explicitly reviewed platform pin.
    raw = REQUIREMENTS.read_bytes().replace(b"\r\n", b"\n")
    assert raw.count((WINDOWS_PIN + "\n").encode()) == 1
    legacy = raw.replace((WINDOWS_PIN + "\n").encode(), b"")
    assert hashlib.sha256(legacy).hexdigest() == LEGACY_SHA256


def test_baseline_29_inventory_fails_only_when_generated_windows_edge_active():
    legacy = {
        line.split("==")[0]: line.split("==")[1]
        for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#") and ";" not in line
    }
    assert len(legacy) == 27
    legacy.update({"k5-vision": "0.1.0", "pip": "25.0.1"})
    wheels = generated_metadata(legacy)
    with pytest.raises(closure.ClosureError, match="^missing_dependency$"):
        closure.verify_closure(wheels, environment("win32"))
    linux = closure.verify_closure(wheels, environment("linux"))
    assert linux["result"] == "pass" and linux["wheel_count"] == 29
    assert linux["dependency_edges"][0]["active"] is False


@pytest.mark.parametrize("platform,count", [("win32", 28), ("linux", 27), ("darwin", 27)])
def test_repaired_target_inventory_and_generated_closure(tmp_path, platform, count):
    # Fixture canonicalization does not relax production's exact byte admission.
    path = tmp_path / "requirements.txt"
    path.write_bytes(REQUIREMENTS.read_bytes().replace(b"\r\n", b"\n"))
    versions = transaction.runtime_versions(path, target_platform=platform)
    assert versions == common.requirements(REQUIREMENTS, target_platform=platform)
    assert len(versions) == count
    assert (versions.get("pyreadline3") == "3.5.6") is (platform == "win32")
    versions.update({"k5-vision": "0.1.0", "pip": "25.0.1"})
    result = closure.verify_closure(generated_metadata(versions), environment(platform))
    assert result["result"] == "pass" and result["wheel_count"] == count + 2
    assert result["dependency_edges"][0]["active"] is (platform == "win32")


def test_common_default_uses_execution_platform_and_explicit_target_overrides(monkeypatch):
    monkeypatch.setattr(common.sys, "platform", "linux")
    assert "pyreadline3" not in common.requirements(REQUIREMENTS)
    assert common.requirements(REQUIREMENTS, target_platform="win32")["pyreadline3"] == "3.5.6"
    monkeypatch.setattr(common.sys, "platform", "win32")
    assert common.requirements(REQUIREMENTS)["pyreadline3"] == "3.5.6"


def test_native_witness_inventory_is_unchanged():
    legacy = common.requirements(REQUIREMENTS, target_platform="linux")
    windows = common.requirements(REQUIREMENTS, target_platform="win32")
    before = {**legacy, **common.RUNTIME_VERSIONS, **common.WINDOWS_RUNTIME_VERSIONS}
    after = {**windows, **common.RUNTIME_VERSIONS, **common.WINDOWS_RUNTIME_VERSIONS}
    assert before == after and len(after) == 33
    assert after["pyreadline3"] == "3.5.6" and after["colorama"] == "0.4.6"


@pytest.mark.parametrize(
    "line",
    [
        'pyreadline3==3.5.7; sys_platform == "win32"',
        'pyreadline3>=3.5.4; sys_platform == "win32"',
        'pyreadline3==3.5.6; sys_platform != "linux"',
        'pyreadline3==3.5.6; sys_platform == "linux"',
        'pyreadline3==3.5.6; os_name == "nt"',
        'colorama==0.4.6; sys_platform == "win32"',
        'pyreadline3==3.5.6; sys_platform == "win32" or extra == "test"',
        'pyreadline3==3.5.6; sys_platform == "win32" # comment',
        WINDOWS_PIN + "\n" + WINDOWS_PIN,
        WINDOWS_PIN + "\npyreadline3==3.5.6",
        "pyreadline3==3.5.6\n" + WINDOWS_PIN,
        "unbounded>=1.0",
    ],
)
@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_witness_marker_reader_rejects_other_syntax_even_on_inactive_target(
    tmp_path, line, platform
):
    path = tmp_path / "requirements.txt"
    path.write_text("base==1.0\n" + line + "\n", encoding="utf-8")
    with pytest.raises(common.WitnessError, match="^admission_failed$"):
        common.requirements(path, target_platform=platform)


@pytest.mark.parametrize("platform", ["", "win_amd64", "Windows", None, 0])
def test_hash_bound_inventory_refuses_invalid_explicit_target(platform):
    with pytest.raises(RuntimeError, match="no online fallback"):
        transaction.runtime_versions(REQUIREMENTS, target_platform=platform)


@pytest.mark.parametrize("change", ["version", "remove", "extra", "comment", "crlf"])
def test_runtime_inventory_keeps_source_byte_trust(tmp_path, change):
    raw = REQUIREMENTS.read_bytes().replace(b"\r\n", b"\n")
    if change == "version":
        raw = raw.replace(b"pyreadline3==3.5.6", b"pyreadline3==3.5.7")
    elif change == "remove":
        raw = raw.replace((WINDOWS_PIN + "\n").encode(), b"")
    elif change == "extra":
        raw += b"unexpected==1.0\n"
    elif change == "comment":
        raw += b"# Unreviewed byte change\n"
    else:
        raw = raw.replace(b"\n", b"\r\n")
    path = tmp_path / "requirements.txt"
    path.write_bytes(raw)
    with pytest.raises(RuntimeError, match="no online fallback"):
        transaction.runtime_versions(path, target_platform="win32")
