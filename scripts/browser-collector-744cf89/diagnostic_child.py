"""Bounded Windows-only collector experiment; never imports or alters a live install."""

from __future__ import annotations

import argparse
import faulthandler
import hashlib
import importlib.metadata
import json
import os
import sys
import threading
import time
from pathlib import Path

SOURCE_SHA256 = "a5ca5fbdef60960de07384138d3290d6c6a1c7e8f688362f6cfd8c7976537ae9"
VERSIONS = {"playwright": "1.58.0", "greenlet": "3.5.6", "pytest": "8.4.2"}
STAGES = {
    "starting",
    "gate",
    "fixture",
    "browser",
    "workers",
    "workload",
    "cleanup",
    "complete",
    "failed",
}


def frozen_source_matches(raw: bytes) -> bool:
    """Admit only the frozen LF bytes or their exact Git CRLF checkout form."""
    canonical = raw.replace(b"\r\n", b"\n")
    if b"\r" in canonical or raw not in (canonical, canonical.replace(b"\n", b"\r\n")):
        return False
    return hashlib.sha256(canonical).hexdigest() == SOURCE_SHA256


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--owned", type=Path, required=True)
    parser.add_argument("--mode", choices=("timer", "control"), required=True)
    parser.add_argument("--seconds", type=int, choices=range(5, 11), default=8)
    args = parser.parse_args()
    report = args.owned / "progress.json"
    state = {
        "stage": "starting",
        "browser_iterations": 0,
        "worker_iterations": [0] * 8,
        "worker_errors": [False] * 8,
        "workers_alive": 0,
        "fixture_verified": False,
        "source_verified": False,
        "edge_verified": False,
        "elapsed_seconds": 0.0,
        "failure_code": "none",
        "failure_stage": "starting",
    }

    def checkpoint(stage: str) -> None:
        assert stage in STAGES
        state["stage"] = stage
        temporary = report.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
        temporary.replace(report)

    stop = threading.Event()
    workers: list[threading.Thread] = []
    browser = None
    driver = None
    timer_armed = False
    try:
        if os.name != "nt" or sys.version_info[:3] != (3, 12, 10):
            raise RuntimeError("runtime-contract")
        for package, expected in VERSIONS.items():
            if importlib.metadata.version(package) != expected:
                raise RuntimeError("dependency-contract")
        source = args.repo / "src/k5vision/windows_identity_security.py"
        if not frozen_source_matches(source.read_bytes()):
            raise RuntimeError("source-contract")
        state["source_verified"] = True
        checkpoint("gate")
        gate_deadline = time.monotonic() + 5
        while not (args.owned / "go").exists():
            if time.monotonic() >= gate_deadline:
                raise RuntimeError("ownership-gate")
            time.sleep(0.01)
        sys.path.insert(0, str(args.repo / "src"))
        sys.dont_write_bytecode = True
        from k5vision import identity_state
        from k5vision import windows_identity_security as security
        from playwright.sync_api import sync_playwright

        checkpoint("fixture")
        fixture = args.owned / "private native security fixture"
        identity_state.validate_new_identity_directory(fixture)
        fixture.mkdir(mode=0o700)
        security.check_windows_security(
            fixture, private=True, require_file_inheritance=True
        )
        sample = fixture / "owned fixed fixture.bin"
        sample.write_bytes(b"K5 owned read-only diagnostic fixture\n")
        security.check_windows_security(sample, private=True)
        baseline = security._NativeSecurity().current_user()
        fixture_identity = fixture.stat()
        state["fixture_verified"] = True
        checkpoint("browser")
        runtime_temp = args.owned / "runtime temporary files"
        runtime_temp.mkdir(mode=0o700)
        # Scope Node/Edge temporary profiles to this owned disposable child.
        os.environ["TEMP"] = os.environ["TMP"] = str(runtime_temp)
        driver = sync_playwright().start()
        browser = driver.chromium.launch(
            channel="msedge", headless=True, timeout=10_000
        )
        context = browser.new_context()
        page = context.new_page()
        page.set_default_timeout(3_000)
        page.set_content(
            "<title>K5 owned collector diagnostic</title><p id='proof'>0</p>"
        )
        edge = page.evaluate("navigator.userAgent.includes('Edg/')")
        if edge is not True:
            raise RuntimeError("edge-contract")
        state["edge_verified"] = True
        checkpoint("workers")

        def worker(index: int) -> None:
            try:
                native = security._NativeSecurity()
                while not stop.is_set():
                    current = fixture.stat()
                    if (current.st_dev, current.st_ino) != (
                        fixture_identity.st_dev,
                        fixture_identity.st_ino,
                    ):
                        raise RuntimeError("fixture-identity")
                    if native.current_user() != baseline:
                        raise RuntimeError("current-user")
                    security.check_windows_security(
                        fixture, private=True, require_file_inheritance=True
                    )
                    security.check_windows_security(sample, private=True)
                    sample.stat()
                    state["worker_iterations"][index] += 1
            except Exception:  # noqa: BLE001 - publish a finite diagnostic flag, never exception text.
                state["worker_errors"][index] = True
                stop.set()

        for index in range(8):
            thread = threading.Thread(
                target=worker, args=(index,), name=f"owned-query-{index}", daemon=True
            )
            workers.append(thread)
            thread.start()
        ready_deadline = time.monotonic() + 3
        while not all(state["worker_iterations"]):
            if stop.is_set() or time.monotonic() >= ready_deadline:
                raise RuntimeError("workers-not-ready")
            page.evaluate("1 + 1")
        checkpoint("workload")
        # Same C watchdog API as pytest's faulthandler_timeout; only this varies.
        if args.mode == "timer":
            faulthandler.dump_traceback_later(0.02, repeat=True, file=sys.stderr)
            timer_armed = True
        start = last_checkpoint = time.monotonic()
        while time.monotonic() - start < args.seconds:
            count = state["browser_iterations"] + 1
            if (
                page.evaluate(
                    "n => { document.querySelector('#proof').textContent = n; return Number(document.querySelector('#proof').textContent); }",
                    count,
                )
                != count
            ):
                raise RuntimeError("browser-roundtrip")
            state["browser_iterations"] = count
            state["elapsed_seconds"] = round(time.monotonic() - start, 3)
            if stop.is_set():
                raise RuntimeError("worker-failed")
            if time.monotonic() - last_checkpoint >= 0.1:
                checkpoint("workload")
                last_checkpoint = time.monotonic()
        if timer_armed:
            faulthandler.cancel_dump_traceback_later()
            timer_armed = False
        checkpoint("cleanup")
        stop.set()
        end_join = time.monotonic() + 3
        for thread in workers:
            thread.join(max(0, end_join - time.monotonic()))
        state["workers_alive"] = sum(thread.is_alive() for thread in workers)
        if state["workers_alive"] or any(state["worker_errors"]):
            raise RuntimeError("worker-cleanup")
        browser.close()
        browser = None
        driver.stop()
        driver = None
        checkpoint("complete")
        return 0
    except Exception as error:  # noqa: BLE001 - sanitize all diagnostic failures before publication.
        codes = {
            "runtime-contract",
            "dependency-contract",
            "source-contract",
            "ownership-gate",
            "edge-contract",
            "workers-not-ready",
            "browser-roundtrip",
            "worker-failed",
            "worker-cleanup",
        }
        state["failure_code"] = (
            str(error) if str(error) in codes else "operation-failed"
        )
        state["failure_stage"] = state["stage"]
        checkpoint("failed")
        return 2
    finally:
        if timer_armed:
            faulthandler.cancel_dump_traceback_later()
        stop.set()
        for thread in workers:
            thread.join(0.1)
        # The parent's Windows Job owns this child and every Edge/driver process.
        # A native crash or stuck close is bounded by that parent, never by taskkill.
        if browser is not None:
            browser.close()
        if driver is not None:
            driver.stop()


if __name__ == "__main__":
    raise SystemExit(main())
