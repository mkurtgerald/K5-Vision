"""Explicit durable identity state, separate from disposable Alpha test sessions."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from collections.abc import Iterator, MutableMapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

from k5vision.domain.users import UserAccount, UserAuditEvent, UserCreate, UserRole
from k5vision.services.user_registry import (
    _CREDENTIAL_SCHEME,
    _SALT_BYTES,
    _SCRYPT_DKLEN,
    MAX_USER_CAPACITY,
    UserRegistry,
)
from k5vision.windows_identity_security import (
    WindowsIdentitySecurityError,
    check_windows_security,
    check_windows_volume,
)

_MANIFEST = "identity.json"
_DATABASE = "users.sqlite3"
_SITE_ENV = "K5_CONTROL_PLANE_SITE_ID"
_DATABASE_ENV = "K5_USER_DB_PATH"
_TEST_ENV = ("K5_LOCAL_TEST_RTSP_SOURCE", "K5_PUBLIC_TEST_RTSP_SOURCE")
_MAX_MANIFEST_BYTES = 1024
_WINDOWS = os.name == "nt"


class IdentityStateError(RuntimeError):
    """Durable state is absent, inconsistent or unsafe to use."""


def _windows_admission(path: Path, *, private: bool, directory: bool = False) -> None:
    if _WINDOWS:
        try:
            check_windows_security(path, private=private, require_file_inheritance=directory)
        except (
            WindowsIdentitySecurityError,
            OSError,
            ValueError,
            TypeError,
            AttributeError,
        ) as exc:
            raise IdentityStateError(
                "Windows identity ownership or access is unsafe or unsupported"
            ) from exc


@dataclass(frozen=True)
class IdentityState:
    directory: Path
    site_id: str

    @property
    def database_path(self) -> Path:
        return self.directory / _DATABASE


def _checked_path(value: str | Path) -> Path:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or path == Path(path.anchor):
        raise IdentityStateError("identity directory must be an absolute local directory")
    if path.drive.startswith("\\\\"):
        raise IdentityStateError("network identity directories are not supported")
    if _WINDOWS:
        try:
            check_windows_volume(path)
        except (
            WindowsIdentitySecurityError,
            OSError,
            ValueError,
            TypeError,
            AttributeError,
        ) as exc:
            raise IdentityStateError(
                "Windows identity requires a supported fixed local volume"
            ) from exc
    # Never resolve a link and then bless its foreign destination. Windows
    # junctions/mount points are reparse points even when is_symlink is false.
    for component in reversed((path, *path.parents)):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise IdentityStateError("linked identity paths are not supported")
        if component != path and not stat.S_ISDIR(info.st_mode):
            raise IdentityStateError("identity parent is not a directory")
        # Check the immediate parent before touching the leaf: an empty shared
        # parent can otherwise be turned into a junction between path probes.
        immediate_parent = component == path.parent
        _windows_admission(component, private=immediate_parent, directory=immediate_parent)
        if os.name == "posix" and stat.S_ISDIR(info.st_mode):
            if info.st_uid not in (0, os.geteuid()):
                raise IdentityStateError("identity ancestors must have trusted ownership")
            if info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX:
                raise IdentityStateError("identity ancestors must resist replacement by others")
    return path


def validate_new_identity_directory(value: str | Path) -> Path:
    """Read-only admission; existing and partial installations are never adopted."""
    path = _checked_path(value)
    if path.exists() or not path.parent.is_dir():
        raise IdentityStateError("use a new identity directory beneath an existing private parent")
    parent = path.parent.stat()
    if os.name == "posix" and (parent.st_uid != os.geteuid() or parent.st_mode & 0o022):
        raise IdentityStateError("identity parent must be owned and protected from other writers")
    _windows_admission(path.parent, private=True, directory=True)
    return path


def _regular_file(path: Path) -> os.stat_result:
    _checked_path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise IdentityStateError("identity files must be ordinary unlinked files")
    _windows_admission(path, private=True)
    return info


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise IdentityStateError("identity manifest contains duplicate fields")
        result[key] = value
    return result


def _same_directory(path: Path, expected: os.stat_result) -> None:
    _checked_path(path)
    current = path.lstat()
    if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (
        expected.st_dev,
        expected.st_ino,
    ):
        raise IdentityStateError("identity directory changed during setup")
    _windows_admission(path.parent, private=True, directory=True)
    _windows_admission(path, private=True, directory=True)


def _validate_registry(connection: sqlite3.Connection, site_id: str) -> None:
    connection.row_factory = sqlite3.Row
    users = {}
    for row in connection.execute("SELECT * FROM users LIMIT ?", (MAX_USER_CAPACITY + 1,)):
        if (
            row["site_id"] != site_id
            or type(row["enabled"]) is not int
            or row["enabled"] not in (0, 1)
        ):
            raise IdentityStateError("identity account state is invalid")
        user = UserAccount(
            id=row["id"],
            username=row["username"],
            display_name=row["display_name"],
            role=row["role"],
            enabled=bool(row["enabled"]),
        )
        if (
            str(user.id) != row["id"]
            or row["id"] in users
            or user.username != row["username"]
            or user.display_name != row["display_name"]
        ):
            raise IdentityStateError("identity account identifier is invalid")
        users[row["id"]] = user
    credentials = {}
    for row in connection.execute(
        "SELECT * FROM user_credentials LIMIT ?", (MAX_USER_CAPACITY + 1,)
    ):
        if (
            row["site_id"] != site_id
            or row["user_id"] not in users
            or row["user_id"] in credentials
            or row["scheme"] != _CREDENTIAL_SCHEME
            or type(row["salt"]) is not bytes
            or len(row["salt"]) != _SALT_BYTES
            or type(row["verifier"]) is not bytes
            or len(row["verifier"]) != _SCRYPT_DKLEN
        ):
            raise IdentityStateError("identity credential state is invalid")
        if row["credential_kind"] == "password":
            if row["expires_at"] is not None:
                raise IdentityStateError("identity password state is invalid")
        elif row["credential_kind"] == "bootstrap":
            expiry = datetime.fromisoformat(row["expires_at"])
            if expiry.tzinfo is None or expiry.utcoffset() is None:
                raise IdentityStateError("identity bootstrap state is invalid")
        else:
            raise IdentityStateError("identity credential kind is invalid")
        credentials[row["user_id"]] = row["credential_kind"]
    setup_complete = False
    if connection.execute(
        "SELECT 1 FROM user_audit WHERE site_id IS NOT ? LIMIT 1", (site_id,)
    ).fetchone():
        raise IdentityStateError("identity audit belongs to a different site")
    setup_events = list(
        connection.execute("SELECT * FROM user_audit WHERE action = 'password-initialized' LIMIT 2")
    )
    if len(setup_events) != 1:
        raise IdentityStateError("identity setup evidence is invalid")
    for row in setup_events:
        if row["site_id"] != site_id:
            raise IdentityStateError("identity audit belongs to a different site")
        event = UserAuditEvent(
            event_id=row["event_id"],
            user_id=row["user_id"],
            action=row["action"],
            actor=row["actor"],
            changed_fields=json.loads(row["changed_fields_json"]),
            occurred_at=row["occurred_at"],
        )
        if (
            event.action == "password-initialized"
            and event.actor == "local-admin-setup"
            and event.changed_fields == ("credential_state",)
            and credentials.get(str(event.user_id)) == "password"
        ):
            setup_complete = True
    if (
        not users
        or len(users) > MAX_USER_CAPACITY
        or users.keys() != credentials.keys()
        or not setup_complete
    ):
        raise IdentityStateError("identity setup is incomplete")


def initialize_identity_state(
    directory: str | Path, *, username: str, password: str
) -> IdentityState:
    """Create only a brand-new installation using a locally supplied password.

    An interrupted or failed setup is preserved for diagnosis, never silently
    reset. Creation of the directory is exclusive across setup processes.
    """
    path = validate_new_identity_directory(directory)
    request = UserCreate(
        username=username,
        display_name="Recovery administrator",
        role=UserRole.ADMINISTRATOR,
        enabled=True,
    )
    if type(password) is not str or not 12 <= len(password) <= 256:
        raise ValueError("password must be between 12 and 256 characters")
    password.encode("utf-8", errors="strict")
    state = IdentityState(path, f"install-{uuid4().hex}")
    path.mkdir(mode=0o700)
    directory_identity = path.lstat()
    # Python 3.12.4+ applies a restricted Windows ACL for mode 0700. Admission
    # verifies the result and private inheritance on every supported patch;
    # no existing directory's access settings are changed.
    _same_directory(path, directory_identity)
    descriptor = os.open(state.database_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    _same_directory(path, directory_identity)
    _regular_file(state.database_path)
    registry = UserRegistry(database_path=state.database_path, site_id=state.site_id)
    try:
        registry.bootstrap_administrator(request, password=password)
    finally:
        registry.close()
    _same_directory(path, directory_identity)
    manifest = json.dumps({"schema_version": 1, "site_id": state.site_id}, sort_keys=True)
    descriptor = os.open(path / _MANIFEST, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(manifest + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return load_identity_state(path)


def load_identity_state(directory: str | Path) -> IdentityState:
    """Read a complete, site-bound installation without creating or repairing it."""
    try:
        path = _checked_path(directory)
        info = path.stat()
        if not stat.S_ISDIR(info.st_mode):
            raise IdentityStateError("identity directory is unavailable")
        if os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
            raise IdentityStateError("identity directory must be private to its owner")
        _windows_admission(path.parent, private=True, directory=True)
        _windows_admission(path, private=True, directory=True)
        manifest_path = path / _MANIFEST
        before = _regular_file(manifest_path)
        if before.st_size > _MAX_MANIFEST_BYTES:
            raise IdentityStateError("identity manifest is invalid")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(manifest_path, flags)
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            current = os.fstat(stream.fileno())
            if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
                raise IdentityStateError("identity manifest changed during admission")
            manifest = json.loads(
                stream.read(_MAX_MANIFEST_BYTES + 1), object_pairs_hook=_unique_object
            )
        if (
            type(manifest) is not dict
            or set(manifest) != {"schema_version", "site_id"}
            or type(manifest["schema_version"]) is not int
            or manifest["schema_version"] != 1
            or type(manifest["site_id"]) is not str
        ):
            raise IdentityStateError("identity manifest is invalid")
        site_id = manifest["site_id"]
        identifier = UUID(hex=site_id.removeprefix("install-"))
        if identifier.version != 4 or site_id != f"install-{identifier.hex}":
            raise IdentityStateError("identity site identifier is invalid")
        state = IdentityState(path, site_id)
        _regular_file(state.database_path)
        for suffix in ("-journal", "-wal", "-shm"):
            sibling = Path(str(state.database_path) + suffix)
            try:
                sibling.lstat()
            except FileNotFoundError:
                continue
            _regular_file(sibling)
        connection = sqlite3.connect(state.database_path.as_uri() + "?mode=ro", uri=True)
        try:
            _validate_registry(connection, site_id)
        finally:
            connection.close()
        return state
    except (OSError, ValueError, TypeError, IndexError, KeyError, sqlite3.Error) as exc:
        raise IdentityStateError("identity state is missing or invalid") from exc


@contextmanager
def identity_environment(
    state: IdentityState, environment: MutableMapping[str, str]
) -> Iterator[None]:
    """Bind only user identity; device/media configuration stays explicitly separate."""
    if any(environment.get(name) for name in _TEST_ENV):
        raise IdentityStateError("durable identity cannot be used by an Alpha test source")
    expected = {_SITE_ENV: state.site_id, _DATABASE_ENV: str(state.database_path)}
    for name, value in expected.items():
        if environment.get(name) not in (None, "", value):
            raise IdentityStateError("durable identity conflicts with configured service state")
    prior = {name: environment.get(name) for name in expected}
    environment.update(expected)
    try:
        yield
    finally:
        for name, value in prior.items():
            if value is None:
                environment.pop(name, None)
            else:
                environment[name] = value
