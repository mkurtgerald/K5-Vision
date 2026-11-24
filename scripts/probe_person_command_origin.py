"""Finite caller-account experiment; never SCM or product qualification."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path, PureWindowsPath

ROOT = Path(__file__).resolve().parents[1]
BASE = "3f798445c33d34a433af3394e43064ffefb1d211"
OUTPUT = ROOT / "person-command-origin.json"
MODES = ("native_admin", "controlled_wmi_failure", "controlled_optional_telemetry_absent")
PINS = {
    key: value
    for key, value in (
        (
            "openvino/__init__.py",
            "c71ccb4af07d91e6fc36de4c68e3df965ce1da42af8de68c303b38b730a45d0b",
        ),
        (
            "openvino/tools/ovc/__init__.py",
            "f867da8794d27688a6bcec180e43bdab0987e5245f6db1834b033fd08cc2e711",
        ),
        (
            "openvino/tools/ovc/telemetry_utils.py",
            "d505c3e14db2aa016b1efa6e972f0dc51bcc7c05055ea4387eda43207c4b328f",
        ),
        (
            "openvino_telemetry/main.py",
            "b23a9f43eba5b73893fb1ff68855389907ed15acf4d36b5b26f718cf049ca945",
        ),
        (
            "openvino_telemetry/utils/opt_in_checker.py",
            "821b299db1a25578ef5d3dbc074586107756506218768a3a8a72ae14deaebdb5",
        ),
        (
            "openvino_telemetry/backend/backend_ga4.py",
            "aa923081b4a82d93e61e5ab818dbf9a0124834032f15c8d6516bd3c01918278d",
        ),
    )
}
CALLERS = {
    "openvino_telemetry.utils.opt_in_checker": "telemetry_consent",
    "openvino_telemetry.backend.backend_ga4": "telemetry_backend",
    "openvino.tools.ovc.telemetry_utils": "converter_telemetry",
    "platform": "stdlib_platform",
    "cv2": "opencv_loader",
    "uvicorn.server": "uvicorn_server",
}


def digest(path):
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def known_ver(executable, arguments):
    """Inspect in memory only; never return an executable or argument string."""
    return (
        isinstance(executable, str)
        and PureWindowsPath(executable).name.casefold() == "cmd.exe"
        and isinstance(arguments, str)
        and re.fullmatch(
            r'(?i)(?:"[^"\r\n]*cmd\.exe"|[^"\r\n]*cmd\.exe)\s+/c\s+"?ver"?\s*', arguments
        )
        is not None
    )


def admit_case(value, mode):
    """Only finite primitives from our owned child enter the artifact."""
    required = {
        "mode",
        "system_callers",
        "wmi_queries",
        "known_ver_commands",
        "other_subprocesses",
        "ver_caller_labels",
        "completed",
    }
    optional = {"generated_inference", "stock_telemetry_fallback", "error_kind"}
    labels = {*CALLERS.values(), "other"}
    if type(value) is not dict or not required <= set(value) or set(value) - required - optional:
        raise ValueError("Diagnostic fields refused")
    if value["mode"] != mode or type(value["completed"]) is not bool:
        raise ValueError("Diagnostic mode refused")
    if type(value["system_callers"]) is not dict or set(value["system_callers"]) != labels:
        raise ValueError("Diagnostic callers refused")
    counts = [value[name] for name in ("wmi_queries", "known_ver_commands", "other_subprocesses")]
    counts += list(value["system_callers"].values())
    if any(type(count) is not int or not 0 <= count <= 32 for count in counts):
        raise ValueError("Diagnostic counts refused")
    if (
        type(value["ver_caller_labels"]) is not list
        or len(value["ver_caller_labels"]) > 8
        or any(
            type(label) is not str or label not in labels for label in value["ver_caller_labels"]
        )
    ):
        raise ValueError("Diagnostic label refused")
    if value["completed"]:
        if (
            set(value) != required | {"generated_inference", "stock_telemetry_fallback"}
            or type(value["stock_telemetry_fallback"]) is not bool
            or value["generated_inference"] != [0.0, 2.0, 0.0, 4.0]
            or any(type(number) is not float for number in value["generated_inference"])
        ):
            raise ValueError("Diagnostic inference refused")
    elif set(value) != required | {"error_kind"} or value["error_kind"] not in {
        "RuntimeError",
        "ValueError",
        "OSError",
        "ImportError",
        "ModuleNotFoundError",
        "other",
    }:
        raise ValueError("Diagnostic error refused")
    return value


def child(mode):
    if sys.stdin.buffer.readline(4) != b"go\n":
        return 1
    facts = {
        "mode": mode,
        "system_callers": dict.fromkeys((*CALLERS.values(), "other"), 0),
        "wmi_queries": 0,
        "known_ver_commands": 0,
        "other_subprocesses": 0,
        "ver_caller_labels": [],
        "completed": False,
    }
    original_system, original_wmi = platform.system, platform._wmi_query

    def system():
        frame = sys._getframe(1)
        label = CALLERS.get(frame.f_globals.get("__name__"), "other")
        facts["system_callers"][label] = min(32, facts["system_callers"][label] + 1)
        return original_system()

    def wmi(*args):
        facts["wmi_queries"] = min(32, facts["wmi_queries"] + 1)
        if mode != "native_admin":
            raise OSError("Controlled WMI failure")
        return original_wmi(*args)

    def audit(event, args):
        if event != "subprocess.Popen":
            return
        if not known_ver(args[0], args[1]):
            facts["other_subprocesses"] = min(32, facts["other_subprocesses"] + 1)
            raise RuntimeError("Unapproved diagnostic subprocess")
        facts["known_ver_commands"] = min(32, facts["known_ver_commands"] + 1)
        frame, labels = sys._getframe(1), []
        for _ in range(32):
            if frame is None:
                break
            label = CALLERS.get(frame.f_globals.get("__name__"))
            if label is not None and label not in labels:
                labels.append(label)
            frame = frame.f_back
        facts["ver_caller_labels"] = labels[:8]

    try:
        platform.system, platform._wmi_query = system, wmi
        sys.addaudithook(audit)
        if mode == "controlled_optional_telemetry_absent":
            # TEST ONLY: choose the donor's existing ImportError fallback.
            sys.modules["openvino_telemetry"] = None
        from k5vision.operator_analytics_person import _cap_windows_native_cpu_isa

        _cap_windows_native_cpu_isa()
        import numpy as np
        import openvino as ov
        from openvino import opset13 as ops

        core = ov.Core()
        value = ops.parameter([4], np.float32)
        model = ov.Model([ops.relu(value)], [value], "generated_diagnostic")
        compiled = core.compile_model(model, "CPU", {"INFERENCE_NUM_THREADS": 1})
        actual = next(
            iter(compiled([np.array([-1, 2, -3, 4], dtype=np.float32)]).values())
        ).tolist()
        if actual != [0.0, 2.0, 0.0, 4.0]:
            raise ValueError("Generated inference differs")
        facts.update(
            completed=True,
            generated_inference=actual,
            stock_telemetry_fallback="openvino.tools.ovc.telemetry_stub" in sys.modules,
        )
    except BaseException as error:
        facts["error_kind"] = (
            type(error).__name__
            if type(error).__name__
            in {"RuntimeError", "ValueError", "OSError", "ImportError", "ModuleNotFoundError"}
            else "other"
        )
    sys.stdout.write(json.dumps(facts) + "\n")
    return 0 if facts["completed"] else 1


def main():
    from k5vision.operator_native_process import _WindowsJob

    result = {
        "schema_version": 1,
        "diagnostic_only": True,
        "product_qualification": False,
        "production_head": BASE,
        "harness_head": os.environ.get("GITHUB_SHA", ""),
        "scope": (
            "Version-matched development Python under caller account; not SCM, "
            "embedded distribution equality or historical worker attribution"
        ),
        "command_scope": "Known VER is argument shape only; not System32 file identity admission",
        "completed": False,
        "cleanup_complete": True,
        "cases": [],
    }
    try:
        if os.name != "nt" or sys.version_info[:3] != (3, 12, 10) or sys.maxsize <= 2**32:
            raise ValueError("Native Windows and version-matched Python required")
        if not ctypes.windll.shell32.IsUserAnAdmin():
            raise ValueError("Elevated native diagnostic caller required")
        versions = {
            name: importlib.metadata.version(name)
            for name in ["numpy", "opencv-python-headless", "openvino", "openvino-telemetry"]
        }
        if versions != {
            "numpy": "2.2.6",
            "opencv-python-headless": "5.0.0.93",
            "openvino": "2026.3.1",
            "openvino-telemetry": "2025.2.0",
        }:
            raise ValueError("Pinned dependencies differ")
        for name, expected in PINS.items():
            package = "openvino-telemetry" if name.startswith("openvino_telemetry/") else "openvino"
            if digest(Path(importlib.metadata.distribution(package).locate_file(name))) != expected:
                raise ValueError("Pinned donor Python source differs")
        if (
            digest(ROOT / "src/k5vision/operator_analytics_person.py")
            != "4921cb3713434d104b376bf83605db656d72ec00de38ae2a647522ca26552382"
        ):
            raise ValueError("Pinned product person source differs")
        result.update(
            caller_is_admin=True,
            versions=versions,
            donor_source_sha256=PINS,
            python_platform_source_sha256=digest(Path(platform.__file__)),
            script_sha256=digest(Path(__file__)),
            workflow_sha256=digest(ROOT / ".github/workflows/person-command-origin.yml"),
        )
        for mode in MODES:
            process, job = None, _WindowsJob()
            try:
                process = subprocess.Popen(
                    [sys.executable, "-I", "-B", str(Path(__file__)), "--child", mode],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    close_fds=True,
                )
                job.assign(process)
                raw, _ = process.communicate(input=b"go\n", timeout=45)
                if len(raw) > 16384 or not raw.endswith(b"\n"):
                    raise ValueError("Finite child protocol refused")
                facts = admit_case(json.loads(raw), mode)
                if (
                    facts.get("mode") != mode
                    or facts.get("completed") is not True
                    or process.returncode != 0
                ):
                    result["cases"].append(facts)
                    raise RuntimeError("Diagnostic child did not complete")
                result["cases"].append(facts)
            finally:
                clean = True
                try:
                    if process is not None and process.poll() is None:
                        process.kill()
                        process.wait(timeout=3)
                except BaseException:
                    clean = False
                try:
                    job.close()
                except BaseException:
                    clean = False
                if not clean:
                    result["cleanup_complete"] = False
                    raise RuntimeError("Diagnostic child cleanup incomplete")
        native, forced, absent = result["cases"]
        result["comparison"] = {
            "forced_wmi_fallback_exercised": forced["wmi_queries"] > 0
            and forced["known_ver_commands"] > 0,
            "stock_optional_fallback_imported": absent["stock_telemetry_fallback"],
            "stock_fallback_avoided_queries": absent["wmi_queries"] == 0
            and absent["known_ver_commands"] == 0
            and sum(absent["system_callers"].values()) == 0,
            "no_other_subprocesses": all(
                case["other_subprocesses"] == 0 for case in result["cases"]
            ),
            "same_real_inference": native["generated_inference"]
            == forced["generated_inference"]
            == absent["generated_inference"],
        }
        result["comparison_supported"] = all(result["comparison"].values())
        result["completed"] = True
    except BaseException as error:
        result["error_kind"] = (
            type(error).__name__
            if type(error).__name__ in {"RuntimeError", "ValueError", "OSError", "TimeoutExpired"}
            else "other"
        )
    OUTPUT.write_text(json.dumps(result, indent=2) + "\n")
    return 0 if result["completed"] and result["cleanup_complete"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", choices=MODES)
    options = parser.parse_args()
    raise SystemExit(child(options.child) if options.child else main())
