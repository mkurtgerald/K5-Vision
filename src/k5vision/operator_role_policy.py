"""Durable custom roles narrow the existing account and camera authority."""

from __future__ import annotations

import json
import os
import secrets
import threading
from pathlib import Path
from uuid import UUID, uuid4

from k5vision import identity_state
from k5vision.domain.users import UserRole
from k5vision.operator_component_state import ComponentStateAnchor

PERMISSIONS = frozenset(
    {
        "cameras.view_live",
        "cameras.playback",
        "cameras.export",
        "cameras.manage",
        "recording.manage",
        "analytics.manage",
        "users.manage",
        "roles.manage",
        "storage.manage",
        "time.manage",
        "integrations.manage",
        "audit.read",
    }
)
BASE_PERMISSIONS = {
    UserRole.VIEWER: frozenset({"cameras.view_live", "cameras.playback"}),
    UserRole.OPERATOR: frozenset({"cameras.view_live", "cameras.playback", "cameras.export"}),
    UserRole.ADMINISTRATOR: PERMISSIONS,
}
_MAX_BYTES = 2 * 1024 * 1024
_MAX_PROFILES = 128
_MAX_BINDINGS = 100_000


class RolePolicyError(RuntimeError):
    """A finite role administration failure, without private state values."""


def _uuid(value):
    if type(value) is not str or str(UUID(value)) != value:
        raise ValueError
    return value


def _profile(value):
    if type(value) is not dict or set(value) != {
        "id",
        "name",
        "description",
        "base_role",
        "permissions",
    }:
        raise ValueError
    _uuid(value["id"])
    if (
        type(value["name"]) is not str
        or not 1 <= len(value["name"].strip()) <= 64
        or type(value["description"]) is not str
        or len(value["description"]) > 256
        or any(ord(c) < 32 for c in value["name"] + value["description"])
    ):
        raise ValueError
    base = UserRole(value["base_role"])
    permissions = value["permissions"]
    if (
        type(permissions) is not list
        or any(type(p) is not str for p in permissions)
        or len(permissions) != len(set(permissions))
        or not set(permissions) <= BASE_PERMISSIONS[base]
    ):
        raise ValueError
    return {**value, "name": value["name"].strip(), "permissions": sorted(permissions)}


class OperatorRolePolicy:
    """Bounded atomic state, with admission on every permission resolution.

    Bindings are applied before a base-account change: interrupted assignment
    can narrow access, but cannot temporarily expand the target's privileges.
    A role mismatch intersects both authorities until an administrator repairs it.
    """

    def __init__(self, root: Path, site_id: str):
        self._lock = threading.RLock()
        self._closed = False
        state = identity_state.load_identity_state(root)
        if state.site_id != site_id:
            raise RolePolicyError("roles_unavailable")
        self._root, self.site_id = state.directory, site_id
        self._root_identity = self._root.lstat()
        self._path = self._root / "role-profiles.json"
        self._identity = None
        try:
            anchor = ComponentStateAnchor(
                self._root,
                site_id,
                "roles",
                present=self._path.exists() or self._path.is_symlink(),
            )
            if self._path.exists() or self._path.is_symlink():
                self._read()
            else:
                self._write(
                    {
                        "schema_version": 1,
                        "site_id": site_id,
                        "revision": 1,
                        "profiles": {},
                        "bindings": {},
                    }
                )
            anchor.seal()
        except Exception:
            raise RolePolicyError("roles_unavailable") from None

    def _admit(self):
        if self._closed:
            raise RolePolicyError("roles_unavailable")
        identity_state._same_directory(self._root, self._root_identity)
        info = identity_state._regular_file(self._path)
        if os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
            raise RolePolicyError("roles_unavailable")
        if info.st_size > _MAX_BYTES:
            raise RolePolicyError("roles_unavailable")
        if self._identity is not None and (info.st_dev, info.st_ino) != self._identity:
            raise RolePolicyError("roles_unavailable")
        return info

    def _read(self):
        before = self._admit()
        with self._path.open("rb") as stream:
            raw = stream.read(_MAX_BYTES + 1)
            opened = os.fstat(stream.fileno())
        after = self._admit()
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise RolePolicyError("roles_unavailable")
        try:

            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError
                    result[key] = value
                return result

            value = json.loads(raw, object_pairs_hook=unique)
            if (
                set(value) != {"schema_version", "site_id", "revision", "profiles", "bindings"}
                or type(value["schema_version"]) is not int
                or value["schema_version"] != 1
                or value["site_id"] != self.site_id
                or type(value["revision"]) is not int
                or not 1 <= value["revision"] < 2**63
                or type(value["profiles"]) is not dict
                or len(value["profiles"]) > _MAX_PROFILES
                or type(value["bindings"]) is not dict
                or len(value["bindings"]) > _MAX_BINDINGS
            ):
                raise ValueError
            for key, profile in value["profiles"].items():
                if _uuid(key) != _profile(profile)["id"]:
                    raise ValueError
            if len({p["name"].casefold() for p in value["profiles"].values()}) != len(
                value["profiles"]
            ):
                raise ValueError
            for user, role in value["bindings"].items():
                _uuid(user)
                if _uuid(role) not in value["profiles"]:
                    raise ValueError
        except Exception:
            raise RolePolicyError("roles_unavailable") from None
        self._identity = before.st_dev, before.st_ino
        return value

    def _write(self, value):
        identity_state._same_directory(self._root, self._root_identity)
        if self._identity is not None:
            self._admit()
        raw = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
        if len(raw) > _MAX_BYTES:
            raise RolePolicyError("roles_capacity")
        temporary = self._root / (".roles-" + secrets.token_hex(16) + ".tmp")
        try:
            fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            identity_state._regular_file(temporary)
            if self._identity is not None:
                self._admit()
            os.replace(temporary, self._path)
            info = identity_state._regular_file(self._path)
            self._identity = info.st_dev, info.st_ino
            self._admit()
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _permissions(account, state):
        base = BASE_PERMISSIONS[account.role]
        profile = state["profiles"].get(state["bindings"].get(str(account.id)))
        return base if profile is None else base.intersection(profile["permissions"])

    def permissions(self, account):
        with self._lock:
            return self._permissions(account, self._read())

    def account_fields(self, account):
        with self._lock:
            state = self._read()
            return {
                "role_profile_id": state["bindings"].get(str(account.id)),
                "effective_permissions": sorted(self._permissions(account, state)),
            }

    def snapshot(self):
        with self._lock:
            state = self._read()
            return {
                "revision": state["revision"],
                "roles": list(state["profiles"].values()),
                "available_permissions": sorted(PERMISSIONS),
                "base_permissions": {r.value: sorted(p) for r, p in BASE_PERMISSIONS.items()},
            }

    def bound_user_ids(self, role_id):
        with self._lock:
            state = self._read()
            return tuple(
                UUID(user) for user, role in state["bindings"].items() if role == str(role_id)
            )

    @staticmethod
    def _full_administrator(account, state):
        return (
            account.enabled
            and account.role is UserRole.ADMINISTRATOR
            and {"users.manage", "roles.manage"} <= OperatorRolePolicy._permissions(account, state)
        )

    def _guard(self, users, state, *, replacement=None, excluded=None):
        for offset in range(0, 100_000, 100):
            rows = users.list(offset=offset, limit=100)
            for account in rows:
                if account.id == excluded:
                    continue
                if replacement is not None and replacement.id == account.id:
                    account = replacement
                if (
                    self._full_administrator(account, state)
                    and users.credential_status(account.id) == "active"
                ):
                    return
            if len(rows) < 100:
                break
        raise RolePolicyError("roles_last_administrator")

    def guard_account_change(self, users, account, fields):
        with self._lock:
            state = self._read()
            changed = account.model_copy(update=fields)
            if str(account.id) in state["bindings"] and "role" in fields:
                raise RolePolicyError("roles_profile_bound")
            self._guard(users, state, replacement=changed)

    def guard_credential_reset(self, users, account):
        with self._lock:
            self._guard(users, self._read(), excluded=account.id)

    def upsert(self, users, payload, *, role_id=None, expected_revision=None):
        with self._lock:
            state = self._read()
            if expected_revision is not None and expected_revision != state["revision"]:
                raise RolePolicyError("roles_revision_conflict")
            role_id = str(uuid4()) if role_id is None else str(role_id)
            if role_id not in state["profiles"] and len(state["profiles"]) >= _MAX_PROFILES:
                raise RolePolicyError("roles_capacity")
            profile = _profile({"id": role_id, **payload})
            previous = state["profiles"].get(role_id)
            if previous is not None and previous["base_role"] != profile["base_role"]:
                raise RolePolicyError("roles_base_immutable")
            if any(
                p["id"] != role_id and p["name"].casefold() == profile["name"].casefold()
                for p in state["profiles"].values()
            ):
                raise RolePolicyError("roles_name_exists")
            state["profiles"][role_id] = profile
            self._guard(users, state)
            state["revision"] += 1
            self._write(state)
            return profile

    def assign(self, users, user_id, role_id, *, expected_revision=None):
        with self._lock:
            state = self._read()
            if expected_revision is not None and expected_revision != state["revision"]:
                raise RolePolicyError("roles_revision_conflict")
            account = users.get(user_id)
            if account is None:
                raise RolePolicyError("roles_user_not_found")
            if role_id is None:
                state["bindings"].pop(str(user_id), None)
                replacement = account
            else:
                profile = state["profiles"].get(str(role_id))
                if profile is None:
                    raise RolePolicyError("roles_not_found")
                if (
                    len(state["bindings"]) >= _MAX_BINDINGS
                    and str(user_id) not in state["bindings"]
                ):
                    raise RolePolicyError("roles_capacity")
                state["bindings"][str(user_id)] = str(role_id)
                replacement = account.model_copy(update={"role": UserRole(profile["base_role"])})
            self._guard(users, state, replacement=replacement)
            state["revision"] += 1
            self._write(state)
            return replacement

    def delete(self, users, role_id, *, expected_revision):
        with self._lock:
            state = self._read()
            if expected_revision != state["revision"]:
                raise RolePolicyError("roles_revision_conflict")
            if str(role_id) in state["bindings"].values():
                raise RolePolicyError("roles_in_use")
            if state["profiles"].pop(str(role_id), None) is None:
                raise RolePolicyError("roles_not_found")
            self._guard(users, state)
            state["revision"] += 1
            self._write(state)

    def close(self):
        with self._lock:
            self._closed = True
