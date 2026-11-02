"""Real sealed native Windows time configuration, with independent restoration."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4

import pytest

from k5vision import identity_state
from k5vision.operator_product_service import _private_configuration, _private_directory
from k5vision.operator_time_admin import (
    TimeAdministrationError,
    WindowsTimeAdministrator,
    windows_configuration,
    windows_issuing_process,
)

_ENABLED = (
    os.name == "nt"
    and os.environ.get("K5_NATIVE_TIME_ACCEPTANCE") == "1"
    and os.environ.get("K5_VM_CLOCK_ACCEPTANCE") == "1"
    and bool(os.environ.get("K5_DESKTOP_PAYLOAD"))
)
pytestmark = pytest.mark.skipif(
    not _ENABLED, reason="Native source configuration requires its disposable Windows gate"
)


def _stage_private_payload(source, parent, *, supervisor_sha, manifest_sha):
    """Match installed ACL layout without changing a production admission rule."""
    source, target = Path(source), Path(parent) / "sealed native payload"
    manifest_path = source / "payload-manifest.json"

    def ordinary(path):
        info = path.lstat()
        assert stat.S_ISREG(info.st_mode) and info.st_nlink == 1
        assert not getattr(info, "st_file_attributes", 0) & 0x400
        return info

    ordinary(manifest_path)
    raw = manifest_path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == manifest_sha
    manifest = json.loads(raw, object_pairs_hook=identity_state._unique_object)
    assert type(manifest) is dict and manifest["schema_version"] == 1
    expected = {
        "payload-manifest.json": {"bytes": len(raw), "sha256": manifest_sha},
        "K5Vision.exe": {
            "bytes": ordinary(source / "K5Vision.exe").st_size,
            "sha256": supervisor_sha,
        },
    }
    directories = {Path(".")}
    folded = {value.casefold() for value in expected}
    for entry in manifest["files"]:
        relative = entry["path"]
        parts = relative.split("/")
        assert all(
            part and part not in {".", ".."} and "\\" not in part and ":" not in part
            for part in parts
        )
        assert not Path(relative).is_absolute() and relative.casefold() not in folded
        assert set(entry) == {"path", "bytes", "sha256"}
        assert type(entry["bytes"]) is int and entry["bytes"] >= 0
        folded.add(relative.casefold())
        expected[relative] = entry
        directories.update(Path(relative).parents)
    observed = set()
    pending = [source]
    while pending:
        directory = pending.pop()
        info = directory.lstat()
        assert stat.S_ISDIR(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400
        assert directory.relative_to(source) in directories
        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path)
                info = path.lstat()
                assert (
                    not stat.S_ISLNK(info.st_mode)
                    and not getattr(info, "st_file_attributes", 0) & 0x400
                )
                if stat.S_ISDIR(info.st_mode):
                    pending.append(path)
                else:
                    ordinary(path)
                    relative = path.relative_to(source).as_posix()
                    assert relative in expected
                    observed.add(relative)
    assert observed == set(expected) and not target.exists()
    target.mkdir(mode=0o700)
    try:
        for relative, entry in expected.items():
            original, destination = source / relative, target / relative
            ordinary(original)
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copyfile(original, destination, follow_symlinks=False)
            assert ordinary(destination).st_size == entry["bytes"]
            assert hashlib.sha256(destination.read_bytes()).hexdigest() == entry["sha256"]
            ordinary(original)
        identity_state._checked_path(target)
        identity_state._windows_admission(target, private=True, directory=True)
        return target
    except BaseException:
        shutil.rmtree(target)
        raise


def _helper(
    executable,
    profile,
    *,
    operation="configure",
    hardlink=False,
    existing_response=False,
    owner=None,
):
    directory = _private_directory(profile / "time-admin")
    nonce = secrets.token_hex(16)
    request = directory / (".time-admin-" + nonce + ".json")
    response = directory / (".time-admin-" + nonce + ".result.json")
    body = {
        "schema_version": 1,
        "nonce": nonce,
        "operation": operation,
        "mode": "manual",
        "ntp_servers": ["time.cloudflare.com"],
        **(windows_issuing_process() if owner is None else owner),
    }
    _private_configuration(request, json.dumps(body))
    alias = None
    try:
        if hardlink:
            alias = directory / ("hardlink-" + nonce + ".json")
            os.link(request, alias)
        if existing_response:
            _private_configuration(response, '{"sentinel":true}')
        result = subprocess.run(
            [str(executable), "--time-admin", str(request), str(response)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
            timeout=50,
        )
        if existing_response:
            assert response.read_text() == '{"sentinel":true}'
        return result.returncode
    finally:
        for path in (alias, request, response):
            if path is not None:
                path.unlink(missing_ok=True)


def _hard_killed_issuer():
    process = subprocess.Popen(
        [
            sys.executable,
            "-B",
            "-c",
            "import json,sys;"
            "from k5vision.operator_time_admin import windows_issuing_process;"
            "print(json.dumps(windows_issuing_process()),flush=True);"
            "sys.stdin.buffer.read(1)",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    timer = threading.Timer(10, process.kill)
    timer.daemon = True
    timer.start()
    try:
        raw = process.stdout.readline(513)
        assert 0 < len(raw) <= 512
        owner = json.loads(raw)
        assert owner["owner_pid"] == process.pid
        assert type(owner["owner_created_100ns"]) is int
        # Hard termination leaves a valid-looking private request possible;
        # native admission must rely on the dead kernel owner, not cleanup.
        process.kill()
        process.wait(timeout=5)
        assert process.poll() is not None
        return owner
    finally:
        timer.cancel()
        timer.join()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_native_windows_time_configuration_readback_and_resync_with_restoration(
    record_testsuite_property,
):
    from windows_time_configuration import WindowsTimeConfigurationGuard, _configuration

    executable = Path(os.environ["K5_DESKTOP_PAYLOAD"]) / "K5Vision.exe"
    assert executable.is_file()
    started = time.monotonic()
    supervisor_sha = hashlib.sha256(executable.read_bytes()).hexdigest()
    manifest_bytes = (executable.parent / "payload-manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    assert manifest["head_sha"] == os.environ.get("K5_DESKTOP_HEAD_SHA")
    parent = Path(os.environ["LOCALAPPDATA"]) / ("K5 Time Proof-" + uuid4().hex)
    identity_state.validate_new_identity_directory(parent)
    parent.mkdir(mode=0o700)
    profile = _private_directory(parent / "profile with spaces")
    guard_path = parent / "time-restoration.json"
    private_payload = _stage_private_payload(
        executable.parent,
        parent,
        supervisor_sha=supervisor_sha,
        manifest_sha=hashlib.sha256(manifest_bytes).hexdigest(),
    )
    executable = private_payload / "K5Vision.exe"
    assert hashlib.sha256(executable.read_bytes()).hexdigest() == supervisor_sha
    assert (private_payload / "payload-manifest.json").read_bytes() == manifest_bytes
    host = WindowsTimeAdministrator(profile, executable=executable)
    proof = {
        "schema_version": 1,
        "head_sha": os.environ.get("K5_DESKTOP_HEAD_SHA"),
        "supervisor_sha256": supervisor_sha,
        "payload_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "native_windows_time_administration": True,
        "real_native_supervisor": True,
        "native_payload_private_staging": True,
        "user_terminal_required": False,
        "native_configuration_readback": False,
        "native_resync_requested": False,
        "native_resync_succeeded": False,
        "upstream_unavailable": False,
        "resync_timeout": False,
        "absolute_accuracy_certified": False,
        "physical_camera_acceptance": False,
    }
    guard = WindowsTimeConfigurationGuard(profile, guard_path)
    try:
        with guard:
            original = _configuration()
            original_host = windows_configuration()
            assert _helper(executable, profile, operation="arbitrary-command") != 0
            assert _configuration() == original and windows_configuration() == original_host
            assert _helper(executable, profile, hardlink=True) != 0
            assert _configuration() == original and windows_configuration() == original_host
            assert _helper(executable, profile, existing_response=True) != 0
            assert _configuration() == original and windows_configuration() == original_host
            issuer = windows_issuing_process()
            assert (
                _helper(
                    executable,
                    profile,
                    owner={**issuer, "owner_created_100ns": issuer["owner_created_100ns"] + 1},
                )
                != 0
            )
            assert _configuration() == original and windows_configuration() == original_host
            dead = _hard_killed_issuer()
            assert _helper(executable, profile, owner=dead) != 0
            assert _configuration() == original and windows_configuration() == original_host
            assert dead["owner_created_100ns"] != issuer["owner_created_100ns"]
            assert (
                _helper(
                    executable,
                    profile,
                    owner={
                        "owner_pid": issuer["owner_pid"],
                        "owner_created_100ns": dead["owner_created_100ns"],
                    },
                )
                != 0
            )
            assert _configuration() == original and windows_configuration() == original_host
            proof.update(
                native_invalid_action_rejected=True,
                native_hardlink_rejected=True,
                native_existing_response_preserved=True,
                native_birth_mismatch_rejected=True,
                native_dead_owner_rejected=True,
                native_reused_pid_rejected=True,
            )
            configured = host.operate(
                "configure", mode="manual", ntp_servers=["time.cloudflare.com"]
            )
            assert configured["host"]["mode"] == "manual"
            assert configured["host"]["ntp_servers"] == ["time.cloudflare.com"]
            assert configured["host"]["service_running"] is True
            proof["native_configuration_readback"] = True
            proof["native_live_owner_bound"] = True
            proof["reference_measured"] = configured["host"]["reference"]["status"] == "measured"
            proof["reference_alignment_verified"] = configured["host"]["reference"]["sync_verified"]
            proof["native_resync_requested"] = True
            try:
                synchronized = host.operate("resync")
            except TimeAdministrationError as error:
                if error.code == "time_ntp_unreachable":
                    proof["upstream_unavailable"] = True
                elif error.code == "time_operation_timeout":
                    proof["resync_timeout"] = True
                else:
                    raise
            else:
                assert synchronized["operation"]["action"] == "resync"
                proof["native_resync_succeeded"] = True
                proof["reference_alignment_verified"] = synchronized["host"]["reference"][
                    "sync_verified"
                ]
                proof["reference_measured"] = (
                    synchronized["host"]["reference"]["status"] == "measured"
                )
            finally:
                host.close()
        restored = guard.result
        assert all(
            restored[key] is True
            for key in ("clock_restored", "services_restored", "time_configuration_restored")
        )
        proof.update(
            time_sources_restored=True,
            time_services_restored=True,
            utc_restored=True,
            independent_restoration_watchdog=True,
        )
        assert hashlib.sha256(executable.read_bytes()).hexdigest() == supervisor_sha
        proof["duration_seconds"] = round(time.monotonic() - started, 3)
        record_testsuite_property(
            "k5_native_time_administration_receipt", json.dumps(proof, sort_keys=True)
        )
    finally:
        host.close()
        shutil.rmtree(parent, ignore_errors=True)
