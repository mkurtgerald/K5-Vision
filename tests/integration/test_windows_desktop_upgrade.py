"""Independent installed Windows cross-revision upgrade and reversal smoke.

Only synthetic account input and private temporary fixture bytes are used.
The actual graphical installer, native supervisor, embedded server and account
login execute for two independently built revisions. This is not a claim that
recordings, schema migrations or failed-activation rollback are qualified.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import secrets
import shutil
import socket
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import psutil
import pytest

from k5vision import identity_state

_INPUTS = (
    "K5_DESKTOP_BASELINE_SETUP",
    "K5_DESKTOP_BASELINE_HEAD_SHA",
    "K5_DESKTOP_BASELINE_SETUP_SHA256",
    "K5_DESKTOP_BASELINE_LAUNCHER_SHA256",
    "K5_DESKTOP_SETUP",
    "K5_DESKTOP_HEAD_SHA",
    "K5_DESKTOP_SETUP_SHA256",
    "K5_DESKTOP_LAUNCHER_SHA256",
)
pytestmark = pytest.mark.skipif(
    os.name != "nt", reason="Cross-revision smoke requires actual Windows execution"
)


def _desktop_helpers():
    script = Path(__file__).with_name("test_windows_desktop_app.py")
    spec = importlib.util.spec_from_file_location("k5_upgrade_desktop_helpers", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _admit_setup(path: Path, expected_hash: str) -> None:
    assert path.is_file() and path.stat().st_size > 100_000_000
    assert len(expected_hash) == 64 and _digest(path) == expected_hash


def _assert_installed(installed: Path, head: str, launcher_hash: str) -> None:
    manifest = json.loads((installed / "payload-manifest.json").read_text(encoding="utf-8"))
    assert manifest["head_sha"] == head
    assert _digest(installed / "K5Vision.exe") == launcher_hash


def _installed_payload_check(installed: Path) -> int:
    """Run the actual installed supervisor without starting backend or emitting artifacts."""
    return subprocess.run(
        [str(installed / "K5Vision.exe"), "--installation-payload-check"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=90,
    ).returncode


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _assert_running_upgrade_refused(setup: Path, installed: Path, api) -> None:
    """Exercise a real double-click-style refusal, not a hidden modal installer."""
    import subprocess
    process = subprocess.Popen(
        [str(setup), "/SP-", "/NORESTART", "/DIR=" + str(installed)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    observed_refusal = False
    deadline = time.monotonic() + 45
    try:
        while process.poll() is None and time.monotonic() < deadline:
            try:
                root = psutil.Process(process.pid)
                children = root.children(recursive=True)
                pids = {root.pid, *(child.pid for child in children)}
            except psutil.NoSuchProcess:
                pids = {process.pid}
            for form in api.windows():
                if api.pid(form) not in pids or not api.api.IsWindowVisible(form):
                    continue
                controls = api.windows(form)
                captions = {api.text(control).strip().casefold(): control for control in controls}
                if any(
                    label.startswith("k5 vision is still running. choose stop and exit")
                    for label in captions
                ):
                    ok = captions.get("ok") or captions.get("&ok")
                    if ok is not None:
                        api.click(ok)
                        observed_refusal = True
                elif "install" in captions or "next >" in captions:
                    pytest.fail("Running upgrade reached installer wizard instead of refusing")
            time.sleep(0.1)
        assert process.poll() is not None, "Running-upgrade refusal did not exit within 45s"
        assert observed_refusal, "Actual installer did not show the running-product refusal"
        assert process.returncode != 0, "Running installer refusal returned success"
    finally:
        if process.poll() is None:
            try:
                children = psutil.Process(process.pid).children(recursive=True)
            except psutil.NoSuchProcess:
                children = []
            process.kill()
            process.wait(timeout=10)
            for child in children:
                try:
                    if child.is_running():
                        child.kill()
                        child.wait(timeout=10)
                except psutil.NoSuchProcess:
                    pass


def _uninstall_through_clone_completion(uninstaller: Path) -> tuple[int, bool]:
    """Wait for the actual Inno uninstall clone, not just its original EXE.

    Inno's TEMP clone may still be completing its deletion after the original
    uninstaller returns success. Keep the same 45-second total deadline for
    both processes, and never inspect or persist their paths or arguments.
    """
    process = subprocess.Popen(
        [str(uninstaller), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 45
    children: dict[int, psutil.Process] = {}
    try:
        while process.poll() is None:
            try:
                for child in psutil.Process(process.pid).children(recursive=True)[:32]:
                    children[child.pid] = child
            except psutil.Error:
                pass
            if time.monotonic() >= deadline:
                pytest.fail("Candidate uninstaller exceeded its 45-second deadline", pytrace=False)
            time.sleep(0.05)

        # A successful return code alone does not mean Inno's clone has exited.
        # Do not retry, terminate early, or skip a still-running clone.
        if process.returncode == 0:
            for child in children.values():
                while child.is_running():
                    if time.monotonic() >= deadline:
                        pytest.fail(
                            "Candidate uninstall clone exceeded its 45-second deadline",
                            pytrace=False,
                        )
                    time.sleep(0.05)
        return process.returncode, bool(children)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        for child in children.values():
            try:
                if child.is_running():
                    child.kill()
                    child.wait(timeout=5)
            except psutil.Error:
                pass


def test_stopped_graphical_cross_revision_upgrade_and_reversal(record_testsuite_property):
    assert all(os.environ.get(name) for name in _INPUTS), "Missing exact upgrade test input"
    d = _desktop_helpers()
    runtime = d._helpers("test_operator_product_runtime")
    baseline = Path(os.environ["K5_DESKTOP_BASELINE_SETUP"]).resolve(strict=True)
    candidate = Path(os.environ["K5_DESKTOP_SETUP"]).resolve(strict=True)
    old_head, new_head = (
        os.environ["K5_DESKTOP_BASELINE_HEAD_SHA"],
        os.environ["K5_DESKTOP_HEAD_SHA"],
    )
    old_launcher, new_launcher = (
        os.environ["K5_DESKTOP_BASELINE_LAUNCHER_SHA256"],
        os.environ["K5_DESKTOP_LAUNCHER_SHA256"],
    )
    assert len(old_head) == len(new_head) == 40 and old_head != new_head
    assert all(len(value) == 64 for value in (old_launcher, new_launcher))
    assert old_launcher != new_launcher
    _admit_setup(baseline, os.environ["K5_DESKTOP_BASELINE_SETUP_SHA256"])
    _admit_setup(candidate, os.environ["K5_DESKTOP_SETUP_SHA256"])

    parent = Path(os.environ["LOCALAPPDATA"]) / ("K5 Upgrade Proof-" + uuid4().hex)
    identity_state.validate_new_identity_directory(parent)
    parent.mkdir(mode=0o700)
    installed, profile = parent / "installed app with spaces", parent / "profile with spaces"
    api = d._Windows()
    username, password = "upgrade-fixture-admin", secrets.token_urlsafe(32)
    port = _free_local_port()
    app = None
    cohort = d._Cohort(api)
    verified = []
    cleanup_failed = False
    phase = "baseline_setup"
    installer_diagnostic = {}
    uninstall_clone_observed = False
    health_statuses = set()
    readiness_attempts = 0
    last_health_transport = None
    try:
        d._install_gui(baseline, installed, api, diagnostic=installer_diagnostic)
        _assert_installed(installed, old_head, old_launcher)
        client = runtime._Client(port)

        def start_and_login(*, first: bool) -> str:
            nonlocal app, readiness_attempts, last_health_transport
            app = d._start_app(installed, profile, port, cohort)
            form = d._wait(lambda: api.form(app.pid), "Installed upgrade window", process=app)
            if first:
                d._wait(
                    lambda: api.api.IsWindowEnabled(api.control(form, d._CREATE)),
                    "First-version account creation button",
                    process=app,
                )
                api.set_text(form, d._USERNAME, username)
                api.set_text(form, d._PASSWORD, password)
                api.set_text(form, d._CONFIRM, password)
                api.click(api.control(form, d._CREATE))
            health_statuses.clear()
            readiness_attempts = 0
            last_health_transport = None

            def observed_health():
                nonlocal readiness_attempts, last_health_transport
                readiness_attempts += 1
                diagnostic = {}
                try:
                    ready = d._healthy(client, diagnostic)
                except RuntimeError:
                    # Store only the transport helper's bounded type and timing,
                    # never a route, payload, account or local profile path.
                    if client.transport_failure is not None:
                        last_health_transport = dict(client.transport_failure)
                    raise
                if "health_http_status" in diagnostic:
                    health_statuses.add(diagnostic["health_http_status"])
                return ready

            d._wait(
                observed_health,
                "Cross-revision " + phase + " installed backend ready",
                process=app,
                timeout=60,
            )
            runtime._login(client, username, password)
            assert client.json("/api/v1/auth/me")["role"] == "administrator"
            result = identity_state.load_identity_state(profile / "identity").site_id
            d._stop_app(app, form, api, cohort)
            app = None
            return str(result)

        phase = "baseline_login"
        original_site = start_and_login(first=True)
        verified.append("baseline_login")

        # Owner profile is outside the application directory. Use generated
        # non-media bytes, never an actual camera, credential or source URI.
        retained = profile / "upgrade-fixture-keep.bin"
        retained.write_bytes(secrets.token_bytes(4096))
        retained_hash = _digest(retained)

        def assert_preserved() -> None:
            assert retained.is_file() and _digest(retained) == retained_hash
            assert (
                str(identity_state.load_identity_state(profile / "identity").site_id)
                == original_site
            )

        # Prove the candidate's Inno guard refuses replacement of a live
        # product before it changes the currently installed executable.
        phase = "live_upgrade_refusal"
        app = d._start_app(installed, profile, port, cohort)
        form = d._wait(lambda: api.form(app.pid), "Running baseline window", process=app)
        d._wait(
            lambda: d._healthy(client, {}),
            "Running baseline backend",
            process=app,
            timeout=60,
        )
        _assert_running_upgrade_refused(candidate, installed, api)
        _assert_installed(installed, old_head, old_launcher)
        assert_preserved()
        d._stop_app(app, form, api, cohort)
        app = None
        verified.append("live_upgrade_refused")

        phase = "candidate_install"
        installer_diagnostic.clear()
        d._install_gui(candidate, installed, api, diagnostic=installer_diagnostic)
        _assert_installed(installed, new_head, new_launcher)
        assert _installed_payload_check(installed) == 0
        # A changed installed licensing byte must fail the same admission used
        # by Setup, with no backend started and no state or camera files read.
        licensed = installed / "licenses" / "INSTALLATION-LICENSE.txt"
        assert licensed.is_file()
        original = licensed.read_bytes()
        assert original
        try:
            licensed.write_bytes(original + b"\nunauthorized change")
            assert _installed_payload_check(installed) != 0
        finally:
            licensed.write_bytes(original)
        assert _installed_payload_check(installed) == 0
        assert_preserved()
        phase = "candidate_login"
        assert start_and_login(first=False) == original_site
        verified.append("new_revision_login")

        # An older baseline cannot trust unlisted files left by a newer
        # revision. A safe *manual* reversal must first remove the stopped
        # candidate with its real, identity-admitted Windows uninstaller,
        # then install the baseline. This is not automatic failed-activation
        # rollback or a claim of in-place downgrade compatibility.
        phase = "reversal_uninstall"
        uninstaller = installed / "unins000.exe"
        assert uninstaller.is_file(), "Candidate uninstaller is missing"
        uninstall_exit, uninstall_clone_observed = _uninstall_through_clone_completion(
            uninstaller
        )
        assert uninstall_exit == 0, "Candidate manual reversal uninstall failed"
        # Inno can terminate its original uninstaller before the TEMP clone
        # finishes. The next real installer must not race that cleanup.
        assert not (installed / "K5Vision.exe").exists(), "Candidate executable survived uninstall"
        assert_preserved()
        verified.append("candidate_uninstall_preserved_profile")

        phase = "reversal_install"
        installer_diagnostic.clear()
        d._install_gui(baseline, installed, api, diagnostic=installer_diagnostic)
        _assert_installed(installed, old_head, old_launcher)
        assert_preserved()
        phase = "reversal_login"
        assert start_and_login(first=False) == original_site
        verified.append("baseline_reversal_login")
    finally:
        # Distinguish an alive-but-unready supervisor from a UI refusal.
        # Classify known message prefixes; never retain actual window text.
        desktop_phase = "unavailable"
        try:
            if app is not None and app.poll() is None:
                last_form = api.form(app.pid)
                if last_form is not None:
                    desktop_phase = d._desktop_status(api, last_form)
        except (OSError, RuntimeError):
            pass
        # Retain only finite stage and health evidence on both pass and fail.
        record_testsuite_property(
            "desktop_cross_revision_stage_diagnostic",
            json.dumps(
                {
                    "phase": phase,
                    "verified_steps": verified,
                    "uninstall_clone_observed": uninstall_clone_observed,
                    "installer": {
                        "action": installer_diagnostic.get("installer_action"),
                        "install_posted": bool(
                            installer_diagnostic.get("installer_install_posted", False)
                        ),
                        "next_clicks": installer_diagnostic.get("installer_next_clicks", 0),
                        "modal_visible": bool(
                            installer_diagnostic.get("installer_modal_visible", False)
                        ),
                        "modal_class": installer_diagnostic.get(
                            "installer_modal_classification", "none"
                        ),
                        "seal_class": installer_diagnostic.get(
                            "installer_seal_refusal_category", "none"
                        ),
                        "steps_seen": installer_diagnostic.get("installer_steps_seen", []),
                        "exit_code": installer_diagnostic.get("installer_exit_code"),
                        "timeout_activity": installer_diagnostic.get(
                            "installer_timeout_activity", {"observation": "not_applicable"}
                        ),
                    },
                    "health_http_statuses": sorted(health_statuses),
                    "readiness_attempts": readiness_attempts,
                    "last_health_transport": last_health_transport,
                    "desktop_phase": desktop_phase,
                    "supervisor_exit_code": app.poll() if app is not None else None,
                },
                sort_keys=True,
            ),
        )
        cohort.finish()
        for process in reversed(cohort.snapshot()):
            try:
                if process.is_running():
                    process.kill()
                    process.wait(timeout=10)
            except Exception:
                cleanup_failed = True
        if parent.exists():
            try:
                shutil.rmtree(parent)
            except Exception:
                cleanup_failed = True
    assert not cleanup_failed, "Upgrade smoke did not clean its owned processes and files"
    assert not cohort.console_seen and not cohort.failed
    assert verified == [
        "baseline_login",
        "live_upgrade_refused",
        "new_revision_login",
        "candidate_uninstall_preserved_profile",
        "baseline_reversal_login",
    ]
    record_testsuite_property(
        "desktop_cross_revision_receipt",
        json.dumps(
            {
                "schema_version": 1,
                "baseline_head": old_head,
                "candidate_head": new_head,
                "actual_graphical_installations": 3,
                "live_upgrade_refused": True,
                "account_and_profile_retained": True,
                "stopped_manual_reversal": True,
                "candidate_uninstall_preserved_profile": True,
                "manual_reversal_requires_candidate_uninstall": True,
                "automatic_failed_activation_rollback": False,
                "media_compatibility_qualified": False,
                "synthetic_input_only": True,
                "owned_cleanup": True,
            },
            sort_keys=True,
        ),
    )
