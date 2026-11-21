"""Real sealed native Windows time configuration, with independent restoration."""

from __future__ import annotations

import ctypes
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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PureWindowsPath
from urllib.error import HTTPError
from urllib.request import Request
from uuid import uuid4

import psutil
import pytest

from k5vision import identity_state
from k5vision.operator_product_service import _private_configuration, _private_directory
from k5vision.operator_time_admin import (
    TimeAdministrationError,
    windows_configuration,
    windows_issuing_process,
)

_ENABLED = (
    os.name == "nt"
    and os.environ.get("K5_NATIVE_TIME_ACCEPTANCE") == "1"
    and os.environ.get("K5_VM_CLOCK_ACCEPTANCE") == "1"
    and all(
        os.environ.get(name)
        for name in ("K5_DESKTOP_SETUP", "K5_DESKTOP_SETUP_SHA256", "K5_DESKTOP_HEAD_SHA")
    )
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


def _candidate(setup, expected_head, expected_setup):
    """Bind the original installer and machine payload before installation."""
    setup = Path(setup).resolve(strict=True)
    assert len(expected_head) == 40 and len(expected_setup) == 64
    assert hashlib.sha256(setup.read_bytes()).hexdigest() == expected_setup
    metadata = json.loads(
        (setup.parent / "desktop-package.json").read_bytes(),
        object_pairs_hook=identity_state._unique_object,
    )
    raw = (setup.parent / "payload-manifest.json").read_bytes()
    manifest = json.loads(raw, object_pairs_hook=identity_state._unique_object)
    assert metadata["qualification_status"] == "unqualified"
    assert metadata["head_sha"] == manifest["head_sha"] == expected_head
    assert metadata["setup_sha256"] == expected_setup
    assert metadata["setup_bytes"] == setup.stat().st_size
    assert metadata["payload_manifest_sha256"] == hashlib.sha256(raw).hexdigest()
    assert all(
        type(metadata[key]) is str
        and len(metadata[key]) == 64
        and set(metadata[key]) <= set("0123456789abcdef")
        for key in ("launcher_sha256", "payload_manifest_sha256")
    )
    return setup, metadata, raw


def _process_issuer(pid):
    """Read the live service backend's exact native FILETIME, never a float."""
    from k5vision.operator_time_admin import _FileTime

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(_FileTime)] * 4
    kernel.GetProcessTimes.restype = ctypes.c_int
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.WaitForSingleObject.restype = ctypes.c_uint32
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x00100000 | 0x00001000, False, pid)
    assert handle
    try:
        assert kernel.WaitForSingleObject(handle, 0) == 258
        created, exited, system, user = (_FileTime() for _ in range(4))
        assert kernel.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(system),
            ctypes.byref(user),
        )
        return {
            "owner_pid": pid,
            "owner_created_100ns": (int(created.high) << 32) | int(created.low),
        }
    finally:
        kernel.CloseHandle(handle)


def _time_request(client, operation):
    """Keep the existing API request open while a human-visible native approval runs."""
    configure = operation == "configure"
    route = "/api/v1/operator/admin/time" + ("" if configure else "/synchronize")
    payload = {"mode": "manual", "ntp_servers": ["time.cloudflare.com"]} if configure else {}
    request = Request(
        client.origin + route,
        data=json.dumps(payload).encode(),
        method="PATCH" if configure else "POST",
        headers={
            "Origin": client.origin,
            "Authorization": "Bearer " + client.token,
            "Content-Type": "application/json",
        },
    )
    try:
        response = client.opener.open(request, timeout=90)
    except HTTPError as error:
        response = error
    with response:
        raw = response.read(16385)
        assert 0 < len(raw) <= 16384, "Time API response exceeded its finite byte budget"
        result = json.loads(raw)
        if response.code != 200:
            code = result.get("detail") if type(result) is dict else None
            raise TimeAdministrationError(code if type(code) is str else "time_operation_failed")
        assert type(result) is dict
        return result


def _time_processes():
    for entry in psutil.process_iter(("name",)):
        try:
            if (entry.info.get("name") or "").casefold() in {"k5vision.exe", "w32tm.exe"}:
                yield psutil.Process(entry.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue


class _TimeHelperTracker:
    """Observe explicit approving processes independently of AppInfo ancestry."""

    def __init__(self, cohort, installed, native):
        self.cohort, self.native = cohort, native
        self.images = {
            str(PureWindowsPath(installed) / "K5Vision.exe").casefold(): "helper",
            str(PureWindowsPath(native.system_directory()) / "w32tm.exe").casefold(): "command",
        }
        runner = (os.getpid(), native.birth(os.getpid()))
        expected = native.details(runner)
        assert expected["elevated"] is True, "Time qualification runner is not elevated"
        self.sid, self.session = expected["sid"], expected["session"]
        self.helpers, self.commands = set(), set()
        self.baseline = set()
        for process in _time_processes():
            try:
                birth = native.birth(process.pid)
                if birth is not None:
                    self.baseline.add((process.pid, birth))
            except (ProcessLookupError, psutil.NoSuchProcess):
                continue
            except OSError:
                raise RuntimeError("Native time helper baseline identity was unavailable") from None

    def sample(self):
        self.native.parents()
        candidates = []
        for process in _time_processes():
            try:
                identity = (process.pid, self.native.birth(process.pid))
                if identity[1] is None or identity in self.baseline:
                    continue
                if identity in self.helpers or identity in self.commands:
                    continue
                details = self.native.details(identity)
                kind = self.images.get(str(PureWindowsPath(details["image"])).casefold())
                if (
                    kind is None
                    or details["sid"] != self.sid
                    or details["session"] != self.session
                    or details["elevated"] is not True
                ):
                    continue
                arguments = process.cmdline()
                if (
                    not arguments
                    or str(PureWindowsPath(arguments[0])).casefold()
                    != str(PureWindowsPath(details["image"])).casefold()
                    or self.native.birth(process.pid) != identity[1]
                ):
                    continue
                candidates.append((kind, identity, process, details, arguments[1:]))
            except (OSError, ProcessLookupError, psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        # Process IDs are not a birth ordering. A reused command PID may be
        # smaller than its helper's PID, so admit helper candidates first.
        for kind, identity, process, _details, arguments in candidates:
            if kind == "helper" and arguments == ["--approve-time"]:
                if self.cohort.add(process, role="time_helper", expected_identity=identity):
                    self.helpers.add(identity)
        for kind, identity, process, details, arguments in candidates:
            if kind != "command" or arguments not in (
                ["/config", "/update"],
                ["/resync", "/rediscover"],
            ):
                continue
            parents = [item for item in self.helpers if item[0] == details["parent_pid"]]
            if len(parents) != 1 or self.native.birth(parents[0][0]) != parents[0][1]:
                continue
            # New since the pre-click baseline, with a still-live, admitted
            # parent instance. A historical/reused PPID alone grants nothing.
            try:
                current = self.native.details(identity)
                if current["parent_pid"] != parents[0][0]:
                    continue
                if self.native.birth(parents[0][0]) != parents[0][1]:
                    continue
                if self.cohort.add(process, role="time_helper", expected_identity=identity):
                    self.commands.add(identity)
            except (OSError, ProcessLookupError, psutil.NoSuchProcess):
                continue  # A short native command may already have exited.

    def evidence(self, operation):
        return {
            "operation": operation,
            "helper_count": len(self.helpers),
            "helper_observed": len(self.helpers) == 1,
            "native_command_count": len(self.commands),
            "native_command_observed": bool(self.commands),
        }


def _approve_time(
    client, operation, *, desktop, api, app, form, profile, service, cohort, ownership
):
    """Exercise authenticated HTTP -> service capability -> native GUI approval."""
    before = service.observe()
    assert before["backend_session"] == before["service_session"] == 0
    assert before["backend_account_verified"] and before["service_account_verified"]
    issuer = _process_issuer(before["backend_pid"])
    directory = profile / "identity" / "time-admin"
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_time_request, client, operation)
        try:

            def pending_button():
                if future.done():
                    future.result()  # Preserve finite API failure codes before a GUI timeout.
                    pytest.fail("Time mutation completed without its required native approval")
                # The previous operation's button can remain visible until
                # the next native status poll. Require the new capability as
                # well, so that stale UI cannot race the next API request.
                requests = [
                    path
                    for path in directory.glob(".time-admin-*.json")
                    if not path.name.endswith(".result.json")
                ]
                if len(requests) != 1:
                    return None
                button = api.control(form, 1011)
                return (
                    button
                    if button
                    and api.api.IsWindowVisible(button)
                    and api.api.IsWindowEnabled(button)
                    else None
                )

            button = desktop._wait(
                pending_button, "Native Approve time change button", process=app, timeout=20
            )
            requests = [
                path
                for path in directory.glob(".time-admin-*.json")
                if not path.name.endswith(".result.json")
            ]
            assert len(requests) == 1
            request = requests[0]
            info = request.lstat()
            assert stat.S_ISREG(info.st_mode) and info.st_nlink == 1
            assert not getattr(info, "st_file_attributes", 0) & 0x400
            raw = request.read_bytes()
            assert 0 < len(raw) <= 4096
            payload = json.loads(raw)
            assert {key: payload[key] for key in issuer} == issuer
            assert payload["operation"] == operation
            assert type(payload["nonce"]) is str and len(payload["nonce"]) == 32
            assert request.name == ".time-admin-" + payload["nonce"] + ".json"
            tracker = _TimeHelperTracker(
                cohort,
                service.installed,
                desktop._helpers("windows_process_identity").NativeProcessTree(),
            )
            try:
                api.click(button)
                deadline = time.monotonic() + 80
                while not future.done():
                    tracker.sample()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("Native time approval exceeded its existing deadline")
                    time.sleep(min(0.02, remaining))
                tracker.sample()
                result = future.result(timeout=0)
            finally:
                ownership.append(tracker.evidence(operation))
            assert not request.exists(), "Completed time operation retained its admission token"
        except BaseException:
            # Revoke this generated fixture's sole pending capability before
            # draining its bounded HTTP worker or restoring Windows settings.
            directory = profile / "identity" / "time-admin"
            if directory.is_dir():
                for request in directory.glob(".time-admin-*.json"):
                    if not request.name.endswith(".result.json"):
                        request.unlink(missing_ok=True)
            raise
    after = service.observe()
    assert (after["backend_pid"], after["backend_created"]) == (
        before["backend_pid"],
        before["backend_created"],
    )
    assert result["capabilities"]["requires_interactive_approval"] is True
    return result


def _cleanup_fixture(actions, record, *, primary_error=None):
    """Keep an original failure while reporting every bounded cleanup attempt."""
    allowed = {
        "AssertionError",
        "Failed",
        "FileNotFoundError",
        "OSError",
        "PermissionError",
        "RuntimeError",
        "TimeoutError",
        "TimeoutExpired",
        "TimeAdministrationError",
        "WindowsVmClockError",
    }

    def finite_type(error):
        name = type(error).__name__
        return name if name in allowed else "other"

    diagnostic = {
        "primary_failed": primary_error is not None,
        "primary_error_type": "none" if primary_error is None else finite_type(primary_error),
        "cleanup_failed": False,
        "cleanup_timeout": False,
        "cleanup_error_type": "none",
        "cleanup_error_stage": "none",
    }
    for stage, action in actions:
        try:
            action()
        except BaseException as error:
            diagnostic["cleanup_timeout"] |= isinstance(
                error, (subprocess.TimeoutExpired, TimeoutError)
            )
            if not diagnostic["cleanup_failed"]:
                diagnostic["cleanup_error_type"] = finite_type(error)
                diagnostic["cleanup_error_stage"] = stage
            diagnostic["cleanup_failed"] = True
    record("k5_native_time_cleanup_diagnostic", json.dumps(diagnostic, sort_keys=True))
    if diagnostic["cleanup_failed"] and primary_error is None:
        pytest.fail("Native time fixture cleanup failed: " + json.dumps(diagnostic), pytrace=False)


def test_native_windows_time_configuration_readback_and_resync_with_restoration(
    record_testsuite_property,
):
    import test_windows_desktop_app as desktop
    from windows_time_configuration import WindowsTimeConfigurationGuard, _configuration

    started = time.monotonic()
    # This gate runs on its own fresh disposable VM. The normal graphical
    # journey intentionally uninstalls its payload and preserves its site.
    setup, candidate, manifest_bytes = _candidate(
        os.environ["K5_DESKTOP_SETUP"],
        os.environ["K5_DESKTOP_HEAD_SHA"],
        os.environ["K5_DESKTOP_SETUP_SHA256"],
    )
    installed, profile = desktop._machine_paths()
    assert not installed.exists() and not profile.exists(), "Time gate requires a fresh VM site"
    executable = installed / "K5Vision.exe"
    supervisor_sha = candidate["launcher_sha256"]
    parent = Path(os.environ["LOCALAPPDATA"]) / ("K5 Time Proof-" + uuid4().hex)
    identity_state.validate_new_identity_directory(parent)
    parent.mkdir(mode=0o700)
    negative_profile = _private_directory(parent / "negative capability profile with spaces")
    guard_path = parent / "time-restoration.json"
    api, runtime = desktop._Windows(), desktop._helpers("test_operator_product_runtime")
    cohort = desktop._Cohort(api)
    app = form = primary_error = None
    time_ownership = []
    proof = {
        "schema_version": 2,
        "head_sha": os.environ.get("K5_DESKTOP_HEAD_SHA"),
        "installer_sha256": os.environ["K5_DESKTOP_SETUP_SHA256"],
        "supervisor_sha256": supervisor_sha,
        "payload_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "native_windows_time_administration": True,
        "real_native_supervisor": True,
        "user_terminal_required": False,
        "interactive_uac_consent_observed": False,
        "native_configuration_readback": False,
        "native_resync_requested": False,
        "native_resync_succeeded": False,
        "upstream_unavailable": False,
        "resync_timeout": False,
        "absolute_accuracy_certified": False,
        "physical_camera_acceptance": False,
    }
    try:
        desktop._install_gui(setup, installed, api)
        assert hashlib.sha256(executable.read_bytes()).hexdigest() == supervisor_sha
        assert (installed / "payload-manifest.json").read_bytes() == manifest_bytes
        admitted = subprocess.run(
            [str(executable), "--installation-payload-check"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
            timeout=30,
        )
        assert admitted.returncode == 0, "Installed payload did not pass native code admission"
        desktop._verify_installed_worker_bundle(installed)
        proof.update(actual_gui_installer=True, native_programfiles_installed_payload=True)
        app = desktop._start_app(installed, profile, 8000, cohort)
        form = desktop._wait(lambda: api.form(app.pid), "Native time account window", process=app)
        desktop._wait(
            lambda: (
                api.api.IsWindowVisible(api.control(form, desktop._CREATE))
                and api.api.IsWindowEnabled(api.control(form, desktop._CREATE))
            ),
            "Native time account setup controls",
            process=app,
        )
        username, password = "time-fixture-admin", secrets.token_urlsafe(32)
        api.set_text(form, desktop._USERNAME, username)
        api.set_text(form, desktop._PASSWORD, password)
        api.set_text(form, desktop._CONFIRM, password)
        api.click(api.control(form, desktop._CREATE))
        client = runtime._Client(8000)
        desktop._wait(
            lambda: desktop._healthy(client, {}),
            "Native time service backend startup",
            process=app,
            timeout=60,
        )
        runtime._login(client, username, password)
        assert client.json("/api/v1/auth/me")["role"] == "administrator"
        assert api.control_text(form, desktop._PASSWORD) == ""
        assert api.control_text(form, desktop._CONFIRM) == ""
        service = cohort.service
        observation = service.observe()
        assert observation["backend_session"] == observation["service_session"] == 0
        assert observation["backend_account_verified"] and observation["service_account_verified"]
        status = client.json("/api/v1/operator/admin/time")
        assert status["capabilities"] == {
            "configure_ntp": True,
            "resynchronize": True,
            "requires_interactive_approval": True,
        }
        proof.update(actual_gui_account_setup=True, installed_service_session_zero=True)
        guard = WindowsTimeConfigurationGuard(profile / "identity", guard_path)
        with guard:
            original = _configuration()
            original_host = windows_configuration()
            assert _helper(executable, negative_profile, operation="arbitrary-command") != 0
            assert _configuration() == original and windows_configuration() == original_host
            assert _helper(executable, negative_profile, hardlink=True) != 0
            assert _configuration() == original and windows_configuration() == original_host
            assert _helper(executable, negative_profile, existing_response=True) != 0
            assert _configuration() == original and windows_configuration() == original_host
            issuer = windows_issuing_process()
            assert (
                _helper(
                    executable,
                    negative_profile,
                    owner={**issuer, "owner_created_100ns": issuer["owner_created_100ns"] + 1},
                )
                != 0
            )
            assert _configuration() == original and windows_configuration() == original_host
            dead = _hard_killed_issuer()
            assert _helper(executable, negative_profile, owner=dead) != 0
            assert _configuration() == original and windows_configuration() == original_host
            assert dead["owner_created_100ns"] != issuer["owner_created_100ns"]
            assert (
                _helper(
                    executable,
                    negative_profile,
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
            configured = _approve_time(
                client,
                "configure",
                desktop=desktop,
                api=api,
                app=app,
                form=form,
                profile=profile,
                service=service,
                cohort=cohort,
                ownership=time_ownership,
            )
            assert configured["host"]["mode"] == "manual"
            assert configured["host"]["ntp_servers"] == ["time.cloudflare.com"]
            assert configured["host"]["service_running"] is True
            assert windows_configuration()["ntp_servers"] == ["time.cloudflare.com"]
            assert windows_configuration()["mode"] == "manual"
            proof["native_configuration_readback"] = True
            proof["native_live_owner_bound"] = True
            proof["installed_service_api"] = True
            proof["native_gui_time_approval"] = True
            proof["reference_measured"] = configured["host"]["reference"]["status"] == "measured"
            proof["reference_alignment_verified"] = configured["host"]["reference"]["sync_verified"]
            proof["native_resync_requested"] = True
            try:
                synchronized = _approve_time(
                    client,
                    "resync",
                    desktop=desktop,
                    api=api,
                    app=app,
                    form=form,
                    profile=profile,
                    service=service,
                    cohort=cohort,
                    ownership=time_ownership,
                )
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
        restored = guard.result
        record_testsuite_property(
            "k5_native_time_helper_ownership", json.dumps(time_ownership, sort_keys=True)
        )
        assert len(time_ownership) == 2 and all(
            item["helper_observed"] is True for item in time_ownership
        ), "Native approving helper ownership was not observed for both time operations"
        proof["native_approval_helper_observed"] = True
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
        proof["no_console_windows"] = not cohort.console_seen and not cohort.failed
        proof["duration_seconds"] = round(time.monotonic() - started, 3)
        record_testsuite_property(
            "k5_native_time_administration_receipt", json.dumps(proof, sort_keys=True)
        )
    except BaseException as error:
        primary_error = error
        raise
    finally:

        def stop_client():
            if app is not None and app.poll() is None and form is not None:
                desktop._stop_app(app, form, api, cohort)

        def finish_cohort():
            cohort.finish()
            assert not cohort.failed, "Native time fixture process cleanup failed"

        def uninstall():
            if (installed / "unins000.exe").is_file():
                result = subprocess.run(
                    [
                        str(installed / "unins000.exe"),
                        "/VERYSILENT",
                        "/SUPPRESSMSGBOXES",
                        "/NORESTART",
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=45,
                    check=False,
                )
                assert result.returncode == 0, "Native time fixture uninstall failed"

        _cleanup_fixture(
            (
                ("client", stop_client),
                ("cohort", finish_cohort),
                ("uninstall", uninstall),
                ("temporary_state", lambda: desktop._remove_owned_temporary_tree(parent)),
            ),
            record_testsuite_property,
            primary_error=primary_error,
        )
