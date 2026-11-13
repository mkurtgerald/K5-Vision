"""Supporting Win32 protocol and exact-EA-installer evidence; never qualification."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from uuid import uuid4

import psutil
import pytest

SOURCE = "2d743342cdf393cb1a546340ada9e5b862f8a975"
TREE = "e8e1fa8aa93c5de19bd174c5ac0f6c93f096dc36"
CANDIDATE_SOURCE = "ea14188f7ca021fe2ff770532c41bcdb317181f4"
HELPER_SHA = "24d2019f7a7ddd348fcfc22d0fbdb53b5193caa0ed0d5fc67382aa3f2ae63b2b"
EXE_SHA = "2e719ba3deeef94d1c0703e71560dfce4edbff7f7bc772b92aa335b7bcc101b3"
PROTOCOL_SHA = "eee02ad304ac8c28510d2e41557113a61e8bf0e602608cc832e52007ef145021"
OUTPUT = "installer-navigation-result.json"


def require(condition):
    if not condition:
        raise RuntimeError("installer_diagnostic_refused")


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def uninstall(installed):
    """Use the normal silent uninstaller and retain only its owned descendants."""
    executable = installed / "unins000.exe"
    require(executable.is_file())
    process = subprocess.Popen(
        [str(executable), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    remembered = {}
    deadline = time.monotonic() + 90
    try:
        root = psutil.Process(process.pid)
        remembered[(root.pid, root.create_time())] = root
        while process.poll() is None:
            require(time.monotonic() < deadline)
            try:
                for child in root.children(recursive=True)[:16]:
                    remembered[(child.pid, child.create_time())] = child
            except psutil.NoSuchProcess:
                pass
            time.sleep(0.1)
        require(process.returncode == 0)
        require(not (installed / "K5Vision.exe").exists())
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        for key, child in remembered.items():
            try:
                if child.is_running() and child.create_time() == key[1]:
                    child.kill()
                    child.wait(timeout=5)
            except psutil.NoSuchProcess:
                pass


def safe_installer(diagnostic):
    result = {}
    for key in (
        "installer_install_posted",
        "installer_launch_unchecked",
        "installer_modal_visible",
    ):
        result[key] = diagnostic.get(key) if type(diagnostic.get(key)) is bool else None
    for key in ("installer_next_clicks", "installer_exit_code"):
        value = diagnostic.get(key)
        result[key] = value if type(value) is int and -100 <= value <= 100 else None
    steps = diagnostic.get("installer_steps_seen")
    result["installer_steps_seen"] = (
        steps
        if type(steps) is list
        and len(steps) <= 8
        and all(
            type(value) is str and value in {"next", "next >", "install", "finish"}
            for value in steps
        )
        else None
    )
    return result


def admit_controls(path):
    raw = path.read_bytes()
    require(0 < len(raw) <= 4 * 1024**2 and b"<!DOCTYPE" not in raw.upper())
    cases = list(ET.fromstring(raw).iter("testcase"))
    require(len(cases) == 32)
    require(
        all(
            not any(case.find(tag) is not None for tag in ("failure", "error", "skipped"))
            for case in cases
        )
    )
    expected = {
        "test_old_queued_duplicate_can_execute_install_without_observing_its_caption",
        "test_completion_ticket_waits_for_handler_return_and_long_install",
        "test_pending_callback_remains_owned_when_target_exits",
    }
    native = [case for case in cases if case.get("name") in expected]
    require(len(native) == 3 and {case.get("name") for case in native} == expected)
    # Pytest emits an empty classname for the explicitly selected temporary
    # native module on another Windows drive. Its source bytes and selection
    # are already pinned; admit that presentation only for these exact names.
    require(
        all(
            case.get("classname") == ""
            or case.get("classname", "").endswith("test_windows_installer_navigation_protocol")
            for case in native
        )
    )
    require(
        all(
            case.get("name") in expected
            or case.get("classname")
            in {
                "tests.unit.test_windows_installer_navigation",
                "tests.unit.test_windows_desktop_cleanup",
            }
            for case in cases
        )
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--protocol", type=Path, required=True)
    args = parser.parse_args()
    source, protocol = args.source.resolve(strict=True), args.protocol.resolve(strict=True)
    require(__debug__ and platform.system() == "Windows")
    require(os.environ["K5_VM_CLOCK_ACCEPTANCE"] == "0")
    require(os.environ["RUNNER_ENVIRONMENT"] == "github-hosted")

    def git(*items):
        return subprocess.check_output(["git", *items], cwd=source, text=True).strip()

    require(git("rev-parse", "HEAD") == SOURCE and git("rev-parse", "HEAD^{tree}") == TREE)
    require(not git("status", "--porcelain", "--untracked-files=no"))
    helper = source / "tests/integration/test_windows_desktop_app.py"
    require(digest(helper) == HELPER_SHA and digest(protocol) == PROTOCOL_SHA)
    setup = Path(os.environ["K5_DESKTOP_SETUP"]).resolve(strict=True)
    require(setup.stat().st_size == 300005122 and digest(setup) == EXE_SHA)
    owned = Path(os.environ["RUNNER_TEMP"]) / ("K5 installer navigation " + uuid4().hex)
    owned.mkdir()
    result = {
        "scope": "installer-navigation-supporting-diagnostic-only",
        "qualification": False,
        "controller_source": SOURCE,
        "controller_tree": TREE,
        "candidate_source": CANDIDATE_SOURCE,
        "candidate_sha256": EXE_SHA,
        "clock_test_started": False,
        "app_startup_requested": False,
        "protocol_controls_exit_code": None,
        "stage": "protocol_controls",
        "completed": False,
        "cycles": [{"cycle": number, "outcome": "pending"} for number in range(1, 4)],
        "cleanup_complete": False,
    }
    exit_code = 1
    module = None
    try:
        copied = owned / "tests/integration"
        copied.mkdir(parents=True)
        shutil.copyfile(helper, copied / helper.name)
        shutil.copyfile(protocol, copied / protocol.name)
        require(digest(copied / helper.name) == HELPER_SHA)
        require(digest(copied / protocol.name) == PROTOCOL_SHA)
        result["protocol_controls_exit_code"] = int(
            pytest.main(
                [
                    str(source / "tests/unit/test_windows_installer_navigation.py"),
                    str(source / "tests/unit/test_windows_desktop_cleanup.py"),
                    str(copied / protocol.name),
                    "-o",
                    "addopts=",
                    "--no-cov",
                    "--tb=no",
                    "-q",
                    "--basetemp=" + str(owned / "controls"),
                    "--junitxml=" + str(source / "installer-navigation-controls.xml"),
                ]
            )
        )
        require(result["protocol_controls_exit_code"] == 0)
        admit_controls(source / "installer-navigation-controls.xml")
        result["controls_passed"] = {
            "unit_and_cleanup": 29,
            "actual_native_protocol": 3,
            "skipped": 0,
        }
        spec = importlib.util.spec_from_file_location(
            "k5_installer_diagnostic_owned_helper", helper
        )
        require(spec is not None and spec.loader is not None)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        api = module._Windows()
        for row in result["cycles"]:
            installed = owned / ("installed-" + str(row["cycle"]))
            diagnostic = {}
            started = time.monotonic()
            result["stage"] = "install"
            try:
                module._install_gui(setup, installed, api, diagnostic=diagnostic)
                row["installer_seconds"] = round(time.monotonic() - started, 3)
                row.update(safe_installer(diagnostic))
                result["stage"] = "payload_guard"
                module._verify_installed_worker_bundle(installed)
                result["stage"] = "uninstall"
                uninstall(installed)
                row["outcome"] = "success"
            except BaseException:
                row.update(safe_installer(diagnostic))
                row["outcome"] = "failure"
                raise
            finally:
                if (installed / "unins000.exe").is_file():
                    uninstall(installed)
        require(not git("status", "--porcelain", "--untracked-files=no"))
        result.update(stage="complete", completed=True)
        exit_code = 0
    except BaseException as error:
        result["error_type"] = (
            type(error).__name__
            if type(error).__name__
            in {
                "Failed",
                "RuntimeError",
                "OSError",
                "TimeoutExpired",
                "TimeoutError",
                "AssertionError",
            }
            else "other"
        )
    finally:
        try:
            if module is not None:
                module._remove_owned_temporary_tree(owned)
            else:
                shutil.rmtree(owned)
            require(not owned.exists())
            result["cleanup_complete"] = True
        except Exception:
            result["cleanup_complete"] = False
            exit_code = 1
        Path(OUTPUT).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
