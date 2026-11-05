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
from pathlib import Path
from uuid import uuid4

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
    os.name != "nt" or any(not os.environ.get(key) for key in _INPUTS),
    reason="Cross-revision smoke needs both exact graphical Windows setups",
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


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def test_stopped_graphical_cross_revision_upgrade_and_reversal(record_testsuite_property):
    import subprocess

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
    try:
        d._install_gui(baseline, installed, api)
        _assert_installed(installed, old_head, old_launcher)
        client = runtime._Client(port)

        def start_and_login(*, first: bool) -> str:
            nonlocal app
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
            d._wait(
                lambda: d._healthy(client, {}),
                "Cross-revision installed backend ready",
                process=app,
                timeout=60,
            )
            runtime._login(client, username, password)
            assert client.json("/api/v1/auth/me")["role"] == "administrator"
            result = identity_state.load_identity_state(profile / "identity").site_id
            d._stop_app(app, form, api, cohort)
            app = None
            return str(result)

        original_site = start_and_login(first=True)
        verified.append("baseline_login")

        # Owner profile is outside the application directory. Use generated
        # non-media bytes, never an actual camera, credential or source URI.
        retained = profile / "upgrade-fixture-keep.bin"
        retained.write_bytes(secrets.token_bytes(4096))
        retained_hash = _digest(retained)

        def assert_preserved() -> None:
            assert retained.is_file() and _digest(retained) == retained_hash
            assert str(identity_state.load_identity_state(profile / "identity").site_id) == original_site

        # Prove the candidate's Inno guard refuses replacement of a live
        # product before it changes the currently installed executable.
        app = d._start_app(installed, profile, port, cohort)
        form = d._wait(lambda: api.form(app.pid), "Running baseline window", process=app)
        d._wait(
            lambda: d._healthy(client, {}),
            "Running baseline backend",
            process=app,
            timeout=60,
        )
        refused = subprocess.run(
            [
                str(candidate),
                "/VERYSILENT",
                "/SUPPRESSMSGBOXES",
                "/NORESTART",
                "/SP-",
                "/DIR=" + str(installed),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=45,
            check=False,
        )
        assert refused.returncode != 0, "Running upgrade unexpectedly overwrote a live installation"
        _assert_installed(installed, old_head, old_launcher)
        assert_preserved()
        d._stop_app(app, form, api, cohort)
        app = None
        verified.append("live_upgrade_refused")

        d._install_gui(candidate, installed, api)
        _assert_installed(installed, new_head, new_launcher)
        assert_preserved()
        assert start_and_login(first=False) == original_site
        verified.append("new_revision_login")

        # A stopped user-initiated reversal is *not* automatic rollback of a
        # failed activation. It demonstrates compatibility of both installers
        # with the same persistent operator account and profile bytes.
        d._install_gui(baseline, installed, api)
        _assert_installed(installed, old_head, old_launcher)
        assert_preserved()
        assert start_and_login(first=False) == original_site
        verified.append("baseline_reversal_login")
    finally:
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
                "automatic_failed_activation_rollback": False,
                "media_compatibility_qualified": False,
                "synthetic_input_only": True,
                "owned_cleanup": True,
            },
            sort_keys=True,
        ),
    )
