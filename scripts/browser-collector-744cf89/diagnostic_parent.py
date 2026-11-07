"""Three controlled Windows A/B pairs; stdout contains only finite aggregate JSON.

Run from an isolated diagnostic branch based on the frozen candidate, after:
  python -m pip install -e ".[dev]" playwright==1.58.0 greenlet==3.5.6 pytest==8.4.2
  python diagnostic_parent.py --repo . --output diagnostic-summary.json

This is not a product acceptance test, and a non-reproduction proves no safety.
Raw child logs remain in a disposable private directory and are never published.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BASE = "744cf89e79c58d0946699ca7af1f03f4425634ca"
SOURCE_SHA256 = "a5ca5fbdef60960de07384138d3290d6c6a1c7e8f688362f6cfd8c7976537ae9"
VERSIONS = {"playwright": "1.58.0", "greenlet": "3.5.6", "pytest": "8.4.2"}


def frozen_source_matches(raw: bytes) -> bool:
    """Admit only the frozen LF bytes or their exact Git CRLF checkout form."""
    canonical = raw.replace(b"\r\n", b"\n")
    if b"\r" in canonical or raw not in (canonical, canonical.replace(b"\n", b"\r\n")):
        return False
    return hashlib.sha256(canonical).hexdigest() == SOURCE_SHA256


class Job:
    """A private, kill-on-close Windows Job; no unrelated processes are addressed."""

    def __init__(self) -> None:
        from ctypes import wintypes as w

        class BASIC(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", w.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", w.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", w.DWORD),
                ("SchedulingClass", w.DWORD),
            ]

        class IO(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_uint64)
                for name in (
                    "ReadOperationCount",
                    "WriteOperationCount",
                    "OtherOperationCount",
                    "ReadTransferCount",
                    "WriteTransferCount",
                    "OtherTransferCount",
                )
            ]

        class EXTENDED(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BASIC),
                ("IoInfo", IO),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        class ACCOUNTING(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_int64)
                for name in (
                    "TotalUserTime",
                    "TotalKernelTime",
                    "ThisPeriodTotalUserTime",
                    "ThisPeriodTotalKernelTime",
                )
            ]
            _fields_ += [
                (name, w.DWORD)
                for name in (
                    "TotalPageFaultCount",
                    "TotalProcesses",
                    "ActiveProcesses",
                    "TotalTerminatedProcesses",
                )
            ]

        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = w.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [
            w.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            w.DWORD,
        ]
        self.kernel.SetInformationJobObject.restype = w.BOOL
        self.kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.kernel.AssignProcessToJobObject.restype = w.BOOL
        self.kernel.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
        self.kernel.TerminateJobObject.restype = w.BOOL
        self.kernel.QueryInformationJobObject.argtypes = [
            w.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            w.DWORD,
            ctypes.c_void_p,
        ]
        self.kernel.QueryInformationJobObject.restype = w.BOOL
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        self.kernel.CloseHandle.restype = w.BOOL
        self.handle = self.kernel.CreateJobObjectW(None, None)
        self.accounting_type = ACCOUNTING
        if not self.handle:
            raise RuntimeError("job-create")
        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x2000
        if not self.kernel.SetInformationJobObject(
            self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)
        ):
            self.close()
            raise RuntimeError("job-limits")

    def assign(self, process: subprocess.Popen) -> None:
        if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise RuntimeError("job-assign")

    def terminate(self) -> None:
        if self.handle:
            self.kernel.TerminateJobObject(self.handle, 3)

    def drain(self) -> bool:
        self.terminate()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            info = self.accounting_type()
            if not self.kernel.QueryInformationJobObject(
                self.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None
            ):
                return False
            if info.ActiveProcesses == 0:
                return True
            time.sleep(0.02)
        return False

    def close(self) -> None:
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def source_contract(repo: Path) -> None:
    if not frozen_source_matches(
        (repo / "src/k5vision/windows_identity_security.py").read_bytes()
    ):
        raise RuntimeError("native-source-contract")
    for relative in ("src", "tests/integration/test_operator_beta_browser.py"):

        def revision(ref: str, relative: str = relative) -> bytes:
            return subprocess.run(
                ["git", "-C", str(repo), "rev-parse", f"{ref}:{relative}"],
                check=True,
                capture_output=True,
                timeout=10,
            ).stdout.strip()

        if revision(BASE) != revision("HEAD"):
            raise RuntimeError("frozen-source-contract")
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "diff",
            "--quiet",
            "HEAD",
            "--",
            "src",
            "tests/integration/test_operator_beta_browser.py",
        ],
        check=False,
        timeout=10,
    )
    if result.returncode:
        raise RuntimeError("dirty-source-contract")


def one(repo: Path, root: Path, pair: int, mode: str) -> dict:
    owned = root / f"pair-{pair}-{mode}"
    owned.mkdir(mode=0o700)
    job = Job()
    process = None
    timed_out = False
    drained = False
    try:
        with (
            (owned / "stderr.log").open("wb") as errors,
            (owned / "stdout.log").open("wb") as output,
        ):
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-X",
                    "faulthandler",
                    str(Path(__file__).with_name("diagnostic_child.py")),
                    "--repo",
                    str(repo),
                    "--owned",
                    str(owned),
                    "--mode",
                    mode,
                    "--seconds",
                    "8",
                ],
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=errors,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )
            job.assign(process)
            (owned / "go").write_bytes(b"owned\n")
            try:
                process.wait(timeout=32)
            except subprocess.TimeoutExpired:
                timed_out = True
                job.terminate()
                process.wait(timeout=5)
    finally:
        if process is not None and process.poll() is None:
            job.terminate()
            process.kill()
            process.wait(timeout=5)
        drained = job.drain()
        job.close()
    raw = (owned / "stderr.log").read_bytes()
    progress = (
        json.loads((owned / "progress.json").read_text(encoding="utf-8"))
        if (owned / "progress.json").exists()
        else {}
    )
    stage = progress.get("stage")
    if stage not in {
        "starting",
        "gate",
        "fixture",
        "browser",
        "workers",
        "workload",
        "cleanup",
        "complete",
        "failed",
    }:
        stage = "unavailable"
    counts = progress.get("worker_iterations", [])
    valid_counts = (
        isinstance(counts, list)
        and len(counts) == 8
        and all(type(x) is int and 0 <= x <= 1_000_000 for x in counts)
    )
    iterations = progress.get("browser_iterations", 0)
    if type(iterations) is not int or not 0 <= iterations <= 1_000_000:
        iterations = 0
    fatal = b"Windows fatal exception: access violation" in raw
    good = (
        process.returncode == 0
        and stage == "complete"
        and valid_counts
        and min(counts) > 0
        and iterations > 0
        and drained
    )
    good = good and all(
        progress.get(k) is True
        for k in ("fixture_verified", "source_verified", "edge_verified")
    )
    return {
        "pair": pair,
        "mode": mode,
        "outcome": "complete"
        if good
        else "timeout"
        if timed_out
        else "native_crash"
        if fatal
        else "invalid",
        "return_code_hex": f"0x{process.returncode & 0xFFFFFFFF:08x}",
        "last_stage": stage,
        "browser_iterations": iterations,
        "worker_iterations_min": min(counts) if valid_counts else 0,
        "worker_iterations_total": sum(counts) if valid_counts else 0,
        "fixture_verified": progress.get("fixture_verified") is True,
        "edge_verified": progress.get("edge_verified") is True,
        "job_drained": drained,
        "failure_code": progress.get("failure_code")
        if progress.get("failure_code")
        in {
            "none",
            "runtime-contract",
            "dependency-contract",
            "source-contract",
            "ownership-gate",
            "edge-contract",
            "workers-not-ready",
            "browser-roundtrip",
            "worker-failed",
            "worker-cleanup",
            "operation-failed",
        }
        else "unavailable",
        "dump_count": raw.count(b"Timeout ("),
        "access_violation": fatal,
        "interrupted_frame_line": bool(
            re.search(rb"File\s+Windows fatal exception: access violation", raw)
        ),
        "stderr_bytes": len(raw),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    summary = {
        "schema_version": 1,
        "baseline": BASE,
        "python": "3.12.10",
        "dependencies": VERSIONS,
        "pairs": [],
        "classification": "inconclusive",
        "source_unchanged": False,
        "owned_cleanup": False,
        "diagnostic_only": True,
        "failure_code": "none",
        "causality_proven": False,
        "acceptance_qualified": False,
    }
    root = None
    try:
        if os.name != "nt" or sys.version_info[:3] != (3, 12, 10):
            raise RuntimeError("runtime-contract")
        if any(
            importlib.metadata.version(package) != expected
            for package, expected in VERSIONS.items()
        ):
            raise RuntimeError("dependency-contract")
        source_contract(args.repo.resolve())
        summary["source_unchanged"] = True
        local = Path(os.environ["LOCALAPPDATA"])
        root = Path(tempfile.mkdtemp(prefix="K5 collector diagnostic-", dir=local))
        # mkdtemp uses mode0700; CPython3.12.10 supplies restricted native ACLs.
        for pair in range(1, 4):
            # Alternate order to avoid always warming the timer case first.
            for mode in ("timer", "control") if pair % 2 else ("control", "timer"):
                summary["pairs"].append(one(args.repo.resolve(), root, pair, mode))
        timer = [p for p in summary["pairs"] if p["mode"] == "timer"]
        control = [p for p in summary["pairs"] if p["mode"] == "control"]
        if all(p["job_drained"] for p in summary["pairs"]) and all(
            p["outcome"] == "complete" and p["dump_count"] == 0 for p in control
        ):
            if any(
                p["outcome"] == "native_crash" and p["dump_count"] > 0 for p in timer
            ):
                summary["classification"] = "timer-associated-native-crash"
            elif all(p["outcome"] == "complete" and p["dump_count"] > 0 for p in timer):
                summary["classification"] = "not-reproduced"
    except Exception as error:  # noqa: BLE001 - publish only fixed diagnostic failure codes.
        summary["classification"] = "diagnostic-contract-failed"
        codes = {
            "runtime-contract",
            "dependency-contract",
            "native-source-contract",
            "frozen-source-contract",
            "dirty-source-contract",
            "job-create",
            "job-limits",
            "job-assign",
        }
        summary["failure_code"] = (
            str(error) if str(error) in codes else "operation-failed"
        )
    finally:
        if root is not None:
            try:
                shutil.rmtree(root)
                summary["owned_cleanup"] = True
            except OSError:
                summary["owned_cleanup"] = False
        else:
            summary["owned_cleanup"] = True
        encoded = json.dumps(summary, sort_keys=True, indent=2) + "\n"
        args.output.write_text(encoded, encoding="utf-8")
        print(encoded, end="")
    return (
        0
        if summary["classification"]
        in {"timer-associated-native-crash", "not-reproduced"}
        and summary["owned_cleanup"]
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
