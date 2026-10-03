"""Native, disposable Windows qualification; never uses an owner's credentials.

The real CLI tests require K5_RECOVERY_TEST_PYTHON to name a clean interpreter
with this checkout installed. Passwords live only in memory/private child pipes;
no fixture changes ACLs, creates OS accounts, or connects to network shares.
"""

from __future__ import annotations

import base64
import ctypes
import json
import multiprocessing
import os
import platform
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from contextlib import closing, contextmanager
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from k5vision.domain.users import UserCreate, UserRole
from k5vision.identity_state import (
    IdentityStateError,
    initialize_identity_state,
    load_identity_state,
    validate_new_identity_directory,
)
from k5vision.services.user_registry import UserRegistry, UserRegistryConflictError

pytestmark = pytest.mark.skipif(os.name != "nt", reason="native Windows qualification")

_TRUSTED_MACHINE_SIDS = {"S-1-5-18", "S-1-5-32-544"}  # SYSTEM, Administrators
# Fixed service SID documented by Microsoft, MSDN Magazine, November 2008:
# "Access Control: Understanding Windows File And Registry Permissions".
_TRUSTED_INSTALLER = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
_REPLACEMENT_RIGHTS = (
    0x00010000
    | 0x00040000
    | 0x00080000
    | 0x00000040
    | 0x00000010
    | 0x00000100
    | 0x40000000
    | 0x10000000
    | 0x01000000
)
_PROCESS_TIMEOUT = 30


@pytest.fixture
def private_root():
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        pytest.fail("native qualification needs a private LOCALAPPDATA parent")
    root = Path(local_app_data) / f"K5 Recovery-Δ-{uuid4().hex}"
    # Admission is read-only. Unsafe runner profiles fail instead of being fixed.
    validate_new_identity_directory(root)
    root.mkdir(mode=0o700)
    try:
        validate_new_identity_directory(root / "identity")
        yield root
    finally:
        shutil.rmtree(root)


def _clean_process_environment():
    names = {
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATH",
        "PATHEXT",
        "TEMP",
        "TMP",
        "LOCALAPPDATA",
        "APPDATA",
        "USERPROFILE",
        "SYSTEMDRIVE",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "PROCESSOR_ARCHITECTURE",
        "NUMBER_OF_PROCESSORS",
        "OS",
    }
    return {key: value for key, value in os.environ.items() if key.upper() in names}


@pytest.fixture
def installed_python():
    value = os.environ.get("K5_RECOVERY_TEST_PYTHON")
    if not value or not Path(value).is_file():
        pytest.fail("set K5_RECOVERY_TEST_PYTHON to the clean installed probe interpreter")
    return str(Path(value).absolute())


def _assert_secret_free(secrets_to_check, *payloads):
    # Plain raises deliberately avoid pytest's value-rich assertion introspection.
    for payload in payloads:
        raw = payload if isinstance(payload, bytes) else payload.encode("utf-8")
        if any(value.encode("utf-8") in raw for value in secrets_to_check):
            raise AssertionError("disposable credential or session appeared in retained output")


_MAX_SNAPSHOT_BYTES = 1_048_576
_MAX_SNAPSHOT_PATHS = 64
_MAX_SNAPSHOT_RULES = 256


def _decode_security_snapshot(output, paths):
    """Decode only the bounded scalar records emitted by the native reference."""
    if not 1 <= len(paths) <= _MAX_SNAPSHOT_PATHS:
        raise ValueError("invalid snapshot path count")
    if not isinstance(output, str) or len(output) > _MAX_SNAPSHOT_BYTES or not output.isascii():
        raise ValueError("invalid snapshot size or encoding")
    if not output.endswith("\n"):
        raise ValueError("incomplete snapshot output")
    lines = output.split("\n")[:-1]
    records = iter(line.removesuffix("\r").split("|") for line in lines)

    def take(tag, width):
        record = next(records, None)
        if record is None or len(record) != width or record[0] != tag:
            raise ValueError("invalid snapshot record")
        return record[1:]

    def integer(value, minimum, maximum):
        if re.fullmatch(r"-?(?:0|[1-9][0-9]{0,9})", value) is None:
            raise ValueError("invalid snapshot integer")
        number = int(value)
        if str(number) != value or not minimum <= number <= maximum:
            raise ValueError("snapshot integer out of range")
        return number

    def flag(value):
        return bool(integer(value, 0, 1))

    def sid(value):
        if (
            re.fullmatch(r"S-1-(?:0|[1-9][0-9]{0,14})(?:-(?:0|[1-9][0-9]{0,9})){1,15}", value)
            is None
        ):
            raise ValueError("invalid snapshot SID")
        numbers = value.split("-")[2:]
        if int(numbers[0]) >= 2**48 or any(int(part) >= 2**32 for part in numbers[1:]):
            raise ValueError("snapshot SID out of range")
        return value

    version, count, current, installer = take("K5_SECURITY_SNAPSHOT", 5)
    if version != "1" or integer(count, 1, _MAX_SNAPSHOT_PATHS) != len(paths):
        raise ValueError("snapshot identity mismatch")
    if installer != _TRUSTED_INSTALLER:
        raise ValueError("snapshot installer identity mismatch")
    snapshot = {"current_user": sid(current), "trusted_installer": installer, "items": []}
    for index, path in enumerate(paths):
        item_index, directory, owner, present, nonnull, rule_count = take("I", 7)
        if integer(item_index, 0, _MAX_SNAPSHOT_PATHS - 1) != index:
            raise ValueError("snapshot item order mismatch")
        item = {
            "path": str(path),
            "directory": flag(directory),
            "owner": sid(owner),
            "dacl_present": flag(present),
            "dacl_nonnull": flag(nonnull),
            "rules": [],
        }
        for _ in range(integer(rule_count, 0, _MAX_SNAPSHOT_RULES)):
            identity, kind, mask, inherited, inheritance, propagation = take("R", 7)
            if kind not in {"Allow", "Deny"}:
                raise ValueError("invalid snapshot access kind")
            item["rules"].append(
                {
                    "sid": sid(identity),
                    "kind": kind,
                    "mask": integer(mask, -(2**31), 2**31 - 1),
                    "inherited": flag(inherited),
                    "inheritance": integer(inheritance, 0, 3),
                    "propagation": integer(propagation, 0, 3),
                }
            )
        snapshot["items"].append(item)
    (completed,) = take("E", 2)
    if integer(completed, 1, _MAX_SNAPSHOT_PATHS) != len(paths) or next(records, None) is not None:
        raise ValueError("snapshot completion mismatch")
    return snapshot


def _security_snapshot_diagnostics(stdout, stderr, path_count):
    # Retain only scalar progress from partial output, never command/error text,
    # paths, credentials, audit records, or the partially serialized descriptor.
    def raw(value):
        if value is None:
            return b""
        return value if isinstance(value, bytes) else value.encode("utf-8")

    stdout, stderr = raw(stdout), raw(stderr)
    diagnostic = {
        "last_stage": "no-script-marker",
        "item_index": None,
        "completed_items": 0,
        "stdout_bytes": len(stdout),
        "stderr_bytes": len(stderr),
        "stderr_format": "plain",
        "unrecognized_stderr_lines": 0,
    }
    completed = set()
    global_stages = {"script-start", "current-user", "json-serialize", "complete"}
    item_stages = {
        "acl-read",
        "descriptor-read",
        "sid-owner",
        "sid-rules",
        "path-kind",
        "item-complete",
        "snapshot-write",
    }
    for line in stderr.decode("utf-8", errors="replace").splitlines():
        match = re.fullmatch(r"K5_SECURITY_STAGE:([a-z-]+):([0-9]{1,4})", line)
        if match is None:
            if line == "#< CLIXML":
                diagnostic["stderr_format"] = "clixml"
            elif line:
                diagnostic["unrecognized_stderr_lines"] += 1
            continue
        stage, index = match[1], int(match[2])
        if stage in global_stages and index == 0:
            diagnostic.update(last_stage=stage, item_index=None)
        elif stage in item_stages and index < path_count:
            diagnostic.update(last_stage=stage, item_index=index)
            if stage == "item-complete":
                completed.add(index)
    diagnostic["completed_items"] = len(completed)
    return json.dumps(diagnostic, sort_keys=True)


def _security_snapshot(paths):
    # Read-only native evidence independent of the application's ctypes reader.
    paths = list(paths)
    if not 1 <= len(paths) <= _MAX_SNAPSHOT_PATHS:
        pytest.fail("invalid native security evidence path count", pytrace=False)
    literals = ",".join("'" + str(path).replace("'", "''") + "'" for path in paths)
    script = r"""
[Console]::Error.WriteLine('K5_SECURITY_STAGE:script-start:0')
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::Error.WriteLine('K5_SECURITY_STAGE:current-user:0')
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
try { $current = $identity.User.Value } finally { $identity.Dispose() }
$paths = @(__PATH_LITERALS__)
$sections = [System.Security.AccessControl.AccessControlSections]::Owner -bor
    [System.Security.AccessControl.AccessControlSections]::Access
# Emit only explicit scalar records through .NET. In particular, do not invoke
# A generic serializer's command discovery/serialization/output boundary
# previously timed out after the ACL reads had completed on a hosted image.
$culture = [System.Globalization.CultureInfo]::InvariantCulture
[Console]::Out.WriteLine("K5_SECURITY_SNAPSHOT|1|" + $paths.Count.ToString($culture) +
    "|" + $current + "|__TRUSTED_INSTALLER_SID__")
for ($index = 0; $index -lt $paths.Count; $index++) {
    $path = $paths[$index]
    [Console]::Error.WriteLine("K5_SECURITY_STAGE:path-kind:$index")
    $attributes = [System.IO.File]::GetAttributes($path)
    $directory = (($attributes -band [System.IO.FileAttributes]::Directory) -ne 0)
    [Console]::Error.WriteLine("K5_SECURITY_STAGE:acl-read:$index")
    $acl = if ($directory) {
        [System.IO.Directory]::GetAccessControl($path, $sections)
    } else {
        [System.IO.File]::GetAccessControl($path, $sections)
    }
    [Console]::Error.WriteLine("K5_SECURITY_STAGE:descriptor-read:$index")
    $raw = [System.Security.AccessControl.RawSecurityDescriptor]::new(
        $acl.GetSecurityDescriptorBinaryForm(), 0)
    [Console]::Error.WriteLine("K5_SECURITY_STAGE:sid-owner:$index")
    $owner = $acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value
    [Console]::Error.WriteLine("K5_SECURITY_STAGE:sid-rules:$index")
    $rules = @($acl.GetAccessRules($true, $true,
        [System.Security.Principal.SecurityIdentifier]))
    if ($rules.Count -gt __MAX_RULES__) { throw 'native rule count exceeds bound' }
    [Console]::Error.WriteLine("K5_SECURITY_STAGE:snapshot-write:$index")
    [Console]::Out.WriteLine([string]::Join('|', [string[]]@(
        'I', $index.ToString($culture), [int]$directory, $owner,
        [int](($raw.ControlFlags -band 4) -ne 0),
        [int]($null -ne $raw.DiscretionaryAcl), $rules.Count.ToString($culture))))
    foreach ($rule in $rules) {
        [Console]::Out.WriteLine([string]::Join('|', [string[]]@(
            'R', $rule.IdentityReference.Value, $rule.AccessControlType.ToString(),
            ([long]$rule.FileSystemRights).ToString($culture), [int]$rule.IsInherited,
            ([int]$rule.InheritanceFlags).ToString($culture),
            ([int]$rule.PropagationFlags).ToString($culture))))
    }
    [Console]::Error.WriteLine("K5_SECURITY_STAGE:item-complete:$index")
}
[Console]::Out.WriteLine('E|' + $paths.Count.ToString($culture))
[Console]::Error.WriteLine('K5_SECURITY_STAGE:complete:0')
""".replace("__PATH_LITERALS__", literals)
    script = script.replace("__TRUSTED_INSTALLER_SID__", _TRUSTED_INSTALLER).replace(
        "__MAX_RULES__", str(_MAX_SNAPSHOT_RULES)
    )
    # UTF-16LE is PowerShell's documented EncodedCommand format. No stdin and no
    # command-line quoting roundtrip for the disposable Unicode fixture paths.
    command = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    failure = None
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-EncodedCommand",
                command,
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_PROCESS_TIMEOUT,
            env=_clean_process_environment(),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        failure = "read-only native security evidence timed out after 30 seconds; "
        failure += _security_snapshot_diagnostics(exc.stdout, exc.stderr, len(paths))
    else:
        if result.returncode:
            failure = "read-only native security evidence process failed; "
            failure += _security_snapshot_diagnostics(result.stdout, result.stderr, len(paths))
    if failure is not None:
        # Fail outside the exception handler so pytest never renders the raw
        # TimeoutExpired command/output or its implicit exception chain.
        pytest.fail(failure, pytrace=False)
    snapshot = None
    try:
        snapshot = _decode_security_snapshot(result.stdout, paths)
    except (ValueError, TypeError, UnicodeError):
        failure = "read-only native security evidence output was invalid; "
        failure += _security_snapshot_diagnostics(result.stdout, result.stderr, len(paths))
    if failure is not None:
        pytest.fail(failure, pytrace=False)
    return snapshot


def _assert_private_security(snapshot, private_paths):
    current = snapshot["current_user"]
    assert current.startswith("S-1-")
    trusted = {current, *_TRUSTED_MACHINE_SIDS}
    for item in snapshot["items"]:
        assert item["owner"].startswith("S-1-")
        assert item["dacl_present"] and item["dacl_nonnull"]
        private = item["path"] in private_paths
        accepted = set(trusted)
        if not private and snapshot["trusted_installer"] == _TRUSTED_INSTALLER:
            accepted.add(_TRUSTED_INSTALLER)
        assert item["owner"] in accepted
        file_inheritance = False
        for rule in item["rules"]:
            assert rule["sid"].startswith("S-1-")
            inherit_only = bool(rule["propagation"] & 2)
            # Higher ancestor checks concern access to that object. Private
            # directories must also avoid granting future files foreign access.
            if rule["kind"] != "Allow" or (inherit_only and not private):
                continue
            sid = rule["sid"]
            if sid == "S-1-3-4":  # OWNER RIGHTS
                sid = item["owner"]
            elif sid == "S-1-3-0" and inherit_only:  # CREATOR OWNER
                sid = current
            if sid not in accepted:
                if private:
                    assert rule["mask"] == 0
                else:
                    assert not rule["mask"] & _REPLACEMENT_RIGHTS
            elif rule["inheritance"] & 2 and rule["mask"] & 3 == 3:
                file_inheritance = True
        if private and item["directory"]:
            assert file_inheritance


def test_windows_identity_fixture_has_private_native_security(private_root, record_property):
    password = secrets.token_urlsafe(24)
    state = initialize_identity_state(
        private_root / "identity", username="recovery-admin", password=password
    )
    paths = [*reversed(state.directory.parents), state.directory]
    paths.extend([state.directory / "identity.json", state.database_path])
    for path in paths:
        assert not path.lstat().st_file_attributes & 0x400
    get_drive_type = ctypes.WinDLL("kernel32", use_last_error=True).GetDriveTypeW
    get_drive_type.argtypes = [ctypes.c_wchar_p]
    get_drive_type.restype = ctypes.c_uint
    assert get_drive_type(state.directory.anchor) == 3  # DRIVE_FIXED
    snapshot = _security_snapshot(paths)
    _assert_private_security(
        snapshot,
        {
            str(private_root.parent),
            str(private_root),
            str(state.directory),
            *(str(path) for path in paths[-2:]),
        },
    )
    assert load_identity_state(state.directory) == state
    record_property("python", sys.version)
    record_property("windows", platform.platform())
    record_property("runner_image", os.environ.get("ImageOS", "unrecorded"))
    record_property("runner_image_version", os.environ.get("ImageVersion", "unrecorded"))
    record_property("native_security", json.dumps(snapshot, sort_keys=True))
    summary = {
        "python": sys.version,
        "windows": platform.platform(),
        "runner_image": os.environ.get("ImageOS", "unrecorded"),
        "runner_image_version": os.environ.get("ImageVersion", "unrecorded"),
        "security_object_count": len(snapshot["items"]),
        "checked_scope": "private local parent, fixture, identity files and existing ancestors",
        "volume": "DRIVE_FIXED",
        "reparse_points": "absent",
    }
    print("Native recovery qualification metadata: " + json.dumps(summary, sort_keys=True))


def test_windows_ambiguous_and_unc_identity_paths_are_refused(monkeypatch):
    def no_filesystem(*args, **kwargs):
        pytest.fail("lexically unsupported path reached the filesystem")

    monkeypatch.setattr(Path, "lstat", no_filesystem)
    monkeypatch.setattr(Path, "stat", no_filesystem)
    for path in (
        "relative-identity",
        "C:relative-identity",
        r"\root-relative-identity",
        "C:\\",
        r"C:\parent\..\identity",
        r"\\server\share\identity",
        r"\\?\C:\identity",
        r"\\?\UNC\server\share\identity",
        r"\\.\C:\identity",
    ):
        with pytest.raises(IdentityStateError):
            validate_new_identity_directory(path)
        with pytest.raises(IdentityStateError):
            load_identity_state(path)


def _directory_bytes(path):
    return {
        str(item.relative_to(path)): item.read_bytes() for item in path.rglob("*") if item.is_file()
    }


def _junction(link, destination):
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(destination)],
        capture_output=True,
        timeout=_PROCESS_TIMEOUT,
        check=False,
    )
    assert result.returncode == 0
    assert link.lstat().st_file_attributes & 0x400


def test_windows_junction_identity_paths_are_refused(private_root):
    destination = private_root / "destination"
    destination.mkdir(mode=0o700)
    state = initialize_identity_state(
        destination / "identity", username="recovery-admin", password=secrets.token_urlsafe(24)
    )
    before = _directory_bytes(destination)
    links = []
    try:
        ancestor_link = private_root / "ancestor-junction"
        _junction(ancestor_link, destination)
        links.append(ancestor_link)
        identity_link = private_root / "identity-junction"
        _junction(identity_link, state.directory)
        links.append(identity_link)
        for path in (ancestor_link / "identity", identity_link):
            with pytest.raises(IdentityStateError):
                load_identity_state(path)
            with pytest.raises(IdentityStateError):
                initialize_identity_state(
                    path, username="recovery-admin", password=secrets.token_urlsafe(24)
                )
        with pytest.raises(IdentityStateError):
            initialize_identity_state(
                ancestor_link / "new-identity",
                username="recovery-admin",
                password=secrets.token_urlsafe(24),
            )
        sidecar_destination = private_root / "sidecar-target"
        sidecar_destination.mkdir(mode=0o700)
        dangling_sidecar = Path(str(state.database_path) + "-wal")
        _junction(dangling_sidecar, sidecar_destination)
        links.append(dangling_sidecar)
        os.rmdir(sidecar_destination)
        assert dangling_sidecar.lstat().st_file_attributes & 0x400
        assert not dangling_sidecar.exists() and not dangling_sidecar.is_symlink()
        database_before = state.database_path.read_bytes()
        with pytest.raises(IdentityStateError):
            load_identity_state(state.directory)
        assert state.database_path.read_bytes() == database_before
        assert _directory_bytes(destination) == before
    finally:
        for link in reversed(links):
            os.rmdir(link)  # Remove only our junction, never its destination.


@pytest.mark.parametrize(
    "name",
    [
        "identity.json",
        "users.sqlite3",
        "users.sqlite3-journal",
        "users.sqlite3-wal",
        "users.sqlite3-shm",
    ],
)
def test_windows_hardlinked_identity_files_are_refused(private_root, name):
    state = initialize_identity_state(
        private_root / "identity", username="recovery-admin", password=secrets.token_urlsafe(24)
    )
    original = state.directory / name
    if not original.exists():
        original.write_bytes(b"disposable linked sidecar")
    alias = private_root / "hardlink"
    os.link(original, alias)
    assert original.stat().st_nlink > 1 and alias.stat().st_nlink > 1
    before = _directory_bytes(private_root)
    with pytest.raises(IdentityStateError):
        load_identity_state(state.directory)
    assert _directory_bytes(private_root) == before


def _unused_loopback_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


@contextmanager
def _installed_server(python, directory, logs):
    port = _unused_loopback_port()
    process = subprocess.Popen(
        [
            python,
            "-I",
            "-m",
            "k5vision.cli",
            "serve",
            "--identity-dir",
            str(directory),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=directory.parent,
        env=_clean_process_environment(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}", timeout=2, trust_env=False
        ) as client:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    pytest.fail("installed CLI exited before accepting loopback requests")
                try:
                    if client.get("/api/v1/auth/me").status_code == 401:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(0.05)
            else:
                pytest.fail("installed CLI did not become ready within 20 seconds")
            yield client
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            output, _ = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            output, _ = process.communicate(timeout=10)
        logs.append(output)
        assert process.poll() is not None


def test_windows_cli_restart_preserves_login_and_invalidates_sessions(
    private_root, installed_python
):
    password, wrong_password = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    state = initialize_identity_state(
        private_root / "identity", username="recovery-admin", password=password
    )
    secrets_to_check = [password, wrong_password]
    logs, audits = [], []
    previous_token = None
    account_id = None
    try:
        for _ in range(2):
            assert load_identity_state(state.directory) == state
            with _installed_server(installed_python, state.directory, logs) as client:
                assert client.get("/api/v1/users").status_code == 401
                wrong = client.post(
                    "/api/v1/auth/login",
                    json={"username": "recovery-admin", "password": wrong_password},
                )
                assert wrong.status_code == 401
                if previous_token:
                    assert (
                        client.get(
                            "/api/v1/auth/me", headers={"Authorization": f"Bearer {previous_token}"}
                        ).status_code
                        == 401
                    )
                login = client.post(
                    "/api/v1/auth/login", json={"username": "recovery-admin", "password": password}
                )
                assert login.status_code == 200
                body = login.json()
                token = body["session_token"]
                secrets_to_check.append(token)
                assert body["account"]["username"] == "recovery-admin"
                assert body["account"]["role"] == "administrator"
                if account_id is not None:
                    assert body["account"]["id"] == account_id
                account_id = body["account"]["id"]
                headers = {"Authorization": f"Bearer {token}"}
                users = client.get("/api/v1/users", headers=headers)
                assert users.status_code == 200
                assert len(users.json()) == 1
                audit = client.get("/api/v1/users/audit", headers=headers)
                assert audit.status_code == 200
                assert [event["action"] for event in audit.json()] == [
                    "created",
                    "password-initialized",
                ]
                assert all(event["actor"] == "local-admin-setup" for event in audit.json())
                audits.append(audit.text)
                previous_token = token
        assert audits[0] == audits[1]
        assert load_identity_state(state.directory) == state
    finally:
        _assert_secret_free(
            secrets_to_check,
            *logs,
            *audits,
            (state.directory / "identity.json").read_bytes(),
            state.database_path.read_bytes(),
        )


def _bootstrap_worker(directory, index, ready, start, pipe, registry_only):
    registry = None
    password = secrets.token_urlsafe(24)
    try:
        if registry_only:
            registry = UserRegistry(database_path=directory, site_id="native-race")
        else:
            validate_new_identity_directory(directory)
        ready.set()
        if not start.wait(_PROCESS_TIMEOUT):
            pipe.send({"status": "barrier-timeout"})
            return
        try:
            if registry is None:
                state = initialize_identity_state(
                    directory, username=f"admin-{index}", password=password
                )
                site_id = state.site_id
            else:
                registry.bootstrap_administrator(
                    UserCreate(
                        username=f"admin-{index}",
                        display_name="Disposable qualification administrator",
                        role=UserRole.ADMINISTRATOR,
                    ),
                    password=password,
                )
                site_id = "native-race"
            status = "created"
        except (IdentityStateError, FileExistsError, UserRegistryConflictError) as exc:
            status, site_id = type(exc).__name__, None
        pipe.send({"status": status, "site_id": site_id, "index": index, "password": password})
    except Exception as exc:
        pipe.send({"status": f"unexpected-{type(exc).__name__}"})
    finally:
        if registry is not None:
            registry.close()
        pipe.close()


def _race_bootstraps(path, *, registry_only):
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    children, parents, ready_events = [], [], []
    try:
        for index in range(2):
            parent, child = context.Pipe(duplex=False)
            ready = context.Event()
            process = context.Process(
                target=_bootstrap_worker,
                args=(str(path), index, ready, start, child, registry_only),
            )
            process.start()
            child.close()
            children.append(process)
            parents.append(parent)
            ready_events.append(ready)
        for ready in ready_events:
            assert ready.wait(_PROCESS_TIMEOUT), "child did not reach the setup barrier"
        start.set()
        outcomes = []
        for pipe in parents:
            assert pipe.poll(_PROCESS_TIMEOUT), "setup child did not return a bounded result"
            outcomes.append(pipe.recv())
        for child in children:
            child.join(_PROCESS_TIMEOUT)
            assert child.exitcode == 0
        return outcomes
    finally:
        start.set()
        for child in children:
            if child.is_alive():
                child.terminate()
                child.join(5)
            if child.is_alive():
                child.kill()
                child.join(5)
            assert not child.is_alive(), "owned setup child did not stop"
            child.close()
        for pipe in parents:
            pipe.close()


def _assert_one_bootstrap(database, site_id, outcomes):
    winners = [item for item in outcomes if item["status"] == "created"]
    winner_count = len(winners)
    assert winner_count == 1
    winner = winners[0]
    expected_username = f"admin-{winner['index']}"
    registry = UserRegistry(database_path=database, site_id=site_id)
    try:
        users = registry.list()
        assert len(users) == 1 and users[0].username == expected_username
        for item in outcomes:
            account = registry.verify_password(
                username=users[0].username, password=item["password"]
            )
            if (account is not None) != (item["status"] == "created"):
                raise AssertionError("only the winning disposable credential must authenticate")
        events = registry.audit_events()
        assert [event.action for event in events] == ["created", "password-initialized"]
        assert all(event.actor == "local-admin-setup" for event in events)
        audit = json.dumps([event.model_dump(mode="json") for event in events])
    finally:
        registry.close()
    # sqlite3.Connection.__exit__ ends transactions; it does not close the handle.
    with closing(sqlite3.connect(database)) as connection:
        for table, expected in (("users", 1), ("user_credentials", 1), ("user_audit", 2)):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == expected
            assert connection.execute(f"SELECT DISTINCT site_id FROM {table}").fetchall() == [
                (site_id,)
            ]
    _assert_secret_free([item["password"] for item in outcomes], database.read_bytes(), audit)


def test_windows_simultaneous_setup_processes_create_one_identity(private_root):
    path = private_root / "identity"
    outcomes = _race_bootstraps(path, registry_only=False)
    statuses = [item["status"] for item in outcomes]
    assert statuses.count("created") == 1
    assert sum(status in {"IdentityStateError", "FileExistsError"} for status in statuses) == 1
    state = load_identity_state(path)
    _assert_one_bootstrap(state.database_path, state.site_id, outcomes)
    _assert_secret_free(
        [item["password"] for item in outcomes], (path / "identity.json").read_bytes()
    )


def test_windows_simultaneous_registry_processes_commit_one_bootstrap(private_root):
    database = private_root / "users.sqlite3"
    registry = UserRegistry(database_path=database, site_id="native-race")
    registry.close()
    outcomes = _race_bootstraps(database, registry_only=True)
    statuses = sorted(item["status"] for item in outcomes)
    assert statuses == ["UserRegistryConflictError", "created"]
    _assert_one_bootstrap(database, "native-race", outcomes)


def test_windows_setup_cli_rejects_redirected_input(private_root, installed_python):
    path = private_root / "identity"
    result = subprocess.run(
        [installed_python, "-I", "-m", "k5vision.cli", "setup-admin", "--identity-dir", str(path)],
        input="",
        capture_output=True,
        text=True,
        cwd=private_root,
        env=_clean_process_environment(),
        timeout=_PROCESS_TIMEOUT,
        check=False,
    )
    assert result.returncode == 1
    assert "Administrator setup refused or incomplete" in result.stdout
    assert "New recovery password:" not in result.stdout + result.stderr
    assert not path.exists()
