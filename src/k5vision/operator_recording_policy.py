"""Private durable motion-recording intent and normalized trigger zones.

The source revision is a site-private stable HMAC. It deliberately excludes the
master recording switch and display labels, while including the selected private
stream/profile. Filesystem and SQLite work belongs on the service's owned worker.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import stat
import threading
from contextlib import closing
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from k5vision import identity_state

_FILE = "operator-recording-policy.json"
_TABLE = "operator_recording_policy_anchor"
_MAX_BYTES = 1024 * 1024
_MAX_CAMERAS = 64
_HEX = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
_COORD = Annotated[float, Field(ge=0, le=1, strict=True, allow_inf_nan=False)]


class RecordingPolicyError(RuntimeError):
    """A finite motion-policy failure without private state or source values."""


class RecordingPolicyConflict(RecordingPolicyError):
    pass


class RecordingSourceChanged(RecordingPolicyError):
    pass


class RecordingPolicyAuthorizationChanged(RecordingPolicyError):
    pass


class _WriteGuardRejected(Exception):
    def __init__(self, error):
        self.error = error


def _cross(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _on_segment(a, b, p):
    return (
        abs(_cross(a, b, p)) <= 1e-12
        and min(a[0], b[0]) - 1e-12 <= p[0] <= max(a[0], b[0]) + 1e-12
        and min(a[1], b[1]) - 1e-12 <= p[1] <= max(a[1], b[1]) + 1e-12
    )


def _intersects(a, b, c, d):
    values = (_cross(a, b, c), _cross(a, b, d), _cross(c, d, a), _cross(c, d, b))
    return (values[0] * values[1] < -1e-24 and values[2] * values[3] < -1e-24) or any(
        _on_segment(x, y, p) for x, y, p in ((a, b, c), (a, b, d), (c, d, a), (c, d, b))
    )


class MotionPolygon(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    name: str = Field(min_length=1, max_length=64)
    points: list[tuple[_COORD, _COORD]] = Field(min_length=3, max_length=16)

    @model_validator(mode="after")
    def geometry(self):
        self.name = self.name.strip()
        if not self.name or any(ord(c) < 32 or ord(c) == 127 for c in self.name):
            raise ValueError("Motion zone is invalid")
        try:
            self.name.encode("utf-8")
        except UnicodeError:
            raise ValueError("Motion zone is invalid") from None
        if len(set(self.points)) != len(self.points):
            raise ValueError("Motion zone is invalid")
        pairs = list(zip(self.points, self.points[1:] + self.points[:1], strict=True))
        area = abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in pairs)) / 2
        if area <= 1e-6:
            raise ValueError("Motion zone is invalid")
        for i, (a, b) in enumerate(pairs):
            for j in range(i + 1, len(pairs)):
                if j == i + 1 or (i == 0 and j == len(pairs) - 1):
                    continue
                if _intersects(a, b, *pairs[j]):
                    raise ValueError("Motion zone is invalid")
        return self


class MotionReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    width: StrictInt = Field(ge=1, le=8192)
    height: StrictInt = Field(ge=1, le=8192)
    rotation: Literal[0] = 0
    source_revision: _HEX

    @field_validator("rotation", mode="before")
    @classmethod
    def exact_rotation(cls, value):
        if type(value) is not int or value != 0:
            raise ValueError("Motion reference is invalid")
        return value

    @model_validator(mode="after")
    def bounded(self):
        if self.width * self.height > 32 * 1024**2:
            raise ValueError("Motion reference is invalid")
        return self


class MotionZones(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reference: MotionReference | None = None
    include: list[MotionPolygon] = Field(default_factory=list, max_length=8)
    exclude: list[MotionPolygon] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def bounded(self):
        polygons = [*self.include, *self.exclude]
        if len(polygons) > 8 or len({p.id for p in polygons}) != len(polygons):
            raise ValueError("Motion zones are invalid")
        if polygons and self.reference is None:
            raise ValueError("Motion zones require their observed frame")
        return self


class MotionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["continuous", "motion"] = "continuous"
    post_capture_seconds: StrictInt = Field(default=10, ge=1, le=120)
    sensitivity: StrictInt = Field(default=50, ge=1, le=100)
    minimum_area_percent: float = Field(
        default=0.5, ge=0.1, le=100, strict=True, allow_inf_nan=False
    )
    zones: MotionZones = Field(default_factory=MotionZones)


def validate_motion_policy(value: dict) -> dict:
    """Return a detached, finite canonical DTO or a finite validation failure."""
    try:
        if type(value) is not dict:
            raise ValueError
        return MotionPolicy.model_validate(value).model_dump(mode="json")
    except (ValueError, TypeError, UnicodeError):
        raise RecordingPolicyError("motion_invalid_request") from None


def _signature(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


class RecordingPolicyStore:
    """Atomic bounded policy state, with a dedicated backwards-compatible anchor.

    A separate authority table is ignored by older releases. Adding an unknown
    component to the existing component allowlist would break manual reversal.
    Missing anchored policy state is refused instead of restoring broad defaults.
    """

    def __init__(self, root: Path, site_id: str):
        self._lock = threading.RLock()
        self._closed = False
        self._identity = None
        self._key = None
        try:
            state = identity_state.load_identity_state(root)
            if state.site_id != site_id:
                raise ValueError
            self.root, self.site_id = state.directory, site_id
            self._root_identity = self.root.lstat()
            self._authority = state.database_path
            info = self._private(self._authority)
            self._authority_identity = info.st_dev, info.st_ino
            self.path = self.root / _FILE
            with self._lock:
                anchor = self._anchor()
                present = self.path.exists() or self.path.is_symlink()
                if anchor is not None and not present:
                    raise ValueError
                if present:
                    document = self._read()
                else:
                    document = {
                        "schema_version": 1,
                        "site_id": self.site_id,
                        "revision": 1,
                        "key": secrets.token_hex(32),
                        "policies": {},
                    }
                    self._write(document)
                key = bytes.fromhex(document["key"])
                digest = hashlib.sha256(key).hexdigest()
                if anchor is not None and anchor != digest:
                    raise ValueError
                self._key = key
                self._anchor(digest)
        except Exception:
            self._closed = True
            raise RecordingPolicyError("motion_policy_unavailable") from None

    def _private(self, path):
        identity_state._same_directory(self.root, self._root_identity)
        info = identity_state._regular_file(path)
        if os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
            raise RecordingPolicyError("motion_policy_unavailable")
        identity_state._windows_admission(path, private=True)
        return info

    def _anchor(self, seal=None):
        info = self._private(self._authority)
        if (info.st_dev, info.st_ino) != self._authority_identity:
            raise ValueError
        for suffix in ("-journal", "-wal", "-shm"):
            sidecar = Path(str(self._authority) + suffix)
            if sidecar.exists() or sidecar.is_symlink():
                self._private(sidecar)
        with closing(
            sqlite3.connect(self._authority.as_uri() + "?mode=rw", uri=True, timeout=3)
        ) as c:
            c.execute("PRAGMA synchronous=FULL")
            if seal is not None:
                c.execute("BEGIN IMMEDIATE")
                c.execute(
                    f"CREATE TABLE IF NOT EXISTS {_TABLE} (site_id TEXT PRIMARY KEY, "
                    "schema_version INTEGER NOT NULL, key_digest TEXT NOT NULL)"
                )
            tables = c.execute("SELECT type FROM sqlite_master WHERE name=?", (_TABLE,)).fetchall()
            if not tables:
                return None
            if tables != [("table",)]:
                raise ValueError
            columns = c.execute(f"PRAGMA table_info({_TABLE})").fetchall()
            if [(r[1], r[2], r[3], r[5]) for r in columns] != [
                ("site_id", "TEXT", 0, 1),
                ("schema_version", "INTEGER", 1, 0),
                ("key_digest", "TEXT", 1, 0),
            ]:
                raise ValueError
            rows = c.execute(
                f"SELECT site_id,schema_version,key_digest FROM {_TABLE} LIMIT 2"
            ).fetchall()
            if len(rows) > 1:
                raise ValueError
            if rows and (
                rows[0][:2] != (self.site_id, 1)
                or type(rows[0][2]) is not str
                or len(rows[0][2]) != 64
                or any(x not in "0123456789abcdef" for x in rows[0][2])
            ):
                raise ValueError
            existing = rows[0][2] if rows else None
            if seal is not None:
                if existing is None:
                    c.execute(f"INSERT INTO {_TABLE} VALUES(?,?,?)", (self.site_id, 1, seal))
                elif existing != seal:
                    raise ValueError
                self._private(self._authority)
                c.commit()
            return existing

    def _read(self):
        if self._closed:
            raise ValueError
        before = self._private(self.path)
        if self._identity is not None and (before.st_dev, before.st_ino) != self._identity:
            raise ValueError
        if not 0 < before.st_size <= _MAX_BYTES:
            raise ValueError
        fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (
                (opened.st_dev, opened.st_ino, opened.st_size)
                != (before.st_dev, before.st_ino, before.st_size)
                or not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
            ):
                raise ValueError
            raw = stream.read(_MAX_BYTES + 1)
            after_opened = os.fstat(stream.fileno())
        if _signature(self._private(self.path)) != _signature(before):
            raise ValueError
        if _signature(opened) != _signature(after_opened) or len(raw) != before.st_size:
            raise ValueError
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=identity_state._unique_object)
        if type(value) is not dict or set(value) != {
            "schema_version",
            "site_id",
            "revision",
            "key",
            "policies",
        }:
            raise ValueError
        if (
            type(value["schema_version"]) is not int
            or value["schema_version"] != 1
            or value["site_id"] != self.site_id
            or type(value["revision"]) is not int
            or not 1 <= value["revision"] < 2**63
            or type(value["key"]) is not str
            or len(value["key"]) != 64
            or any(c not in "0123456789abcdef" for c in value["key"])
            or (self._key is not None and bytes.fromhex(value["key"]) != self._key)
            or type(value["policies"]) is not dict
            or len(value["policies"]) > _MAX_CAMERAS
        ):
            raise ValueError
        for camera_id, policy in value["policies"].items():
            if (
                str(UUID(camera_id)) != camera_id
                or type(policy) is not dict
                or set(policy) != {"revision", "source_revision", "policy"}
            ):
                raise ValueError
            if (
                type(policy["revision"]) is not int
                or not 1 <= policy["revision"] < 2**63
                or type(policy["source_revision"]) is not str
                or len(policy["source_revision"]) != 64
                or any(c not in "0123456789abcdef" for c in policy["source_revision"])
                or validate_motion_policy(policy["policy"]) != policy["policy"]
            ):
                raise ValueError
        self._identity = before.st_dev, before.st_ino
        return value

    def _write(self, document, *, before_write=None):
        payload = (
            json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
        ).encode()
        if len(payload) > _MAX_BYTES:
            raise ValueError
        previous = (
            self._private(self.path) if self.path.exists() or self.path.is_symlink() else None
        )
        if (
            previous is not None
            and self._identity is not None
            and (previous.st_dev, previous.st_ino) != self._identity
        ):
            raise ValueError
        temporary = self.root / (".k5-motion-" + secrets.token_hex(16) + ".tmp")
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        committed = False
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            info = self._private(temporary)
            identity_state._same_directory(self.root, self._root_identity)
            current = (
                self._private(self.path) if self.path.exists() or self.path.is_symlink() else None
            )
            if (None if current is None else _signature(current)) != (
                None if previous is None else _signature(previous)
            ):
                raise ValueError
            if before_write is not None:
                try:
                    before_write()
                except Exception as error:
                    raise _WriteGuardRejected(error) from None
            os.replace(temporary, self.path)
            self._identity = info.st_dev, info.st_ino
            committed = True
        finally:
            if not committed and temporary.exists():
                temporary.unlink()

    def source_revision(self, source):
        if self._closed or self._key is None:
            raise RecordingPolicyError("motion_policy_unavailable")
        try:
            profile = getattr(source, "profile_key", "")
            if type(profile) is not str or (
                profile
                and (len(profile) != 64 or any(c not in "0123456789abcdef" for c in profile))
            ):
                raise ValueError
            value = {
                "version": 1,
                "site_id": source.binding.site_id,
                "camera_id": str(source.binding.device_id),
                "stream_token": source.binding.stream_token,
                "profile_key": profile,
                "host": str(source.device.host),
                "management_port": source.device.management_port,
                "protocols": sorted(source.device.protocols),
                "source_uri": source.source_uri,
            }
            if value["site_id"] != self.site_id:
                raise ValueError
            raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
            return hmac.new(self._key, raw, hashlib.sha256).hexdigest()
        except Exception:
            raise RecordingPolicyError("motion_policy_unavailable") from None

    def get(self, source):
        try:
            with self._lock:
                document = self._read()
                current = self.source_revision(source)
                stored = document["policies"].get(str(source.binding.device_id))
                policy = (
                    MotionPolicy().model_dump(mode="json") if stored is None else stored["policy"]
                )
                return {
                    "camera_id": str(source.binding.device_id),
                    "revision": 0 if stored is None else stored["revision"],
                    "source_revision": current,
                    "policy_source_revision": current
                    if stored is None
                    else stored["source_revision"],
                    "pre_capture_seconds": 0,
                    **json.loads(json.dumps(policy)),
                }
        except Exception:
            raise RecordingPolicyError("motion_policy_unavailable") from None

    def save(self, source, expected_revision, policy, *, before_write=None):
        policy = validate_motion_policy(policy)
        if type(expected_revision) is not int or not 0 <= expected_revision < 2**63 - 1:
            raise RecordingPolicyError("motion_invalid_request")
        if before_write is not None and not callable(before_write):
            raise RecordingPolicyError("motion_invalid_request")
        try:
            with self._lock:
                document = self._read()
                current = self.source_revision(source)
                reference = policy["zones"]["reference"]
                if (
                    policy["mode"] == "motion"
                    and reference is not None
                    and reference["source_revision"] != current
                ):
                    raise RecordingSourceChanged("motion_source_changed")
                identifier = str(source.binding.device_id)
                stored = document["policies"].get(identifier)
                revision = 0 if stored is None else stored["revision"]
                if expected_revision != revision:
                    raise RecordingPolicyConflict("motion_policy_conflict")
                if stored is None and len(document["policies"]) >= _MAX_CAMERAS:
                    raise RecordingPolicyError("motion_policy_unavailable")
                document["policies"][identifier] = {
                    "revision": revision + 1,
                    "source_revision": current,
                    "policy": policy,
                }
                document["revision"] += 1
                if document["revision"] >= 2**63:
                    raise ValueError
                self._write(document, before_write=before_write)
                # No fallible reread after the atomic commit.
                return {
                    "camera_id": identifier,
                    "revision": revision + 1,
                    "source_revision": current,
                    "policy_source_revision": current,
                    "pre_capture_seconds": 0,
                    **policy,
                }
        except _WriteGuardRejected as error:
            raise error.error from None
        except (
            RecordingPolicyConflict,
            RecordingSourceChanged,
            RecordingPolicyAuthorizationChanged,
        ):
            raise
        except Exception:
            raise RecordingPolicyError("motion_policy_unavailable") from None

    snapshot = get
    configure = save

    @staticmethod
    def _camera_id(value):
        try:
            if isinstance(value, UUID):
                return str(value)
            if type(value) is str and str(UUID(value)) == value:
                return value
        except ValueError:
            pass
        raise RecordingPolicyError("motion_invalid_request")

    def remove(self, camera_id):
        """Forget a policy only after canonical camera deletion has committed."""
        identifier = self._camera_id(camera_id)
        try:
            with self._lock:
                document = self._read()
                if identifier not in document["policies"]:
                    return False
                del document["policies"][identifier]
                document["revision"] += 1
                if document["revision"] >= 2**63:
                    raise ValueError
                self._write(document)
                return True
        except Exception:
            raise RecordingPolicyError("motion_policy_unavailable") from None

    def prune_existing(self, camera_ids):
        """Startup reconciliation with an admitted canonical camera snapshot.

        This must not run against a proposed source list before its camera
        transaction commits: a compensated camera removal must retain intent.
        """
        if type(camera_ids) not in (tuple, list, set, frozenset) or len(camera_ids) > _MAX_CAMERAS:
            raise RecordingPolicyError("motion_invalid_request")
        identifiers = {self._camera_id(value) for value in camera_ids}
        if len(identifiers) != len(camera_ids):
            raise RecordingPolicyError("motion_invalid_request")
        try:
            with self._lock:
                document = self._read()
                removed = set(document["policies"]) - identifiers
                if not removed:
                    return 0
                for identifier in removed:
                    del document["policies"][identifier]
                document["revision"] += 1
                if document["revision"] >= 2**63:
                    raise ValueError
                self._write(document)
                return len(removed)
        except Exception:
            raise RecordingPolicyError("motion_policy_unavailable") from None

    def close(self):
        with self._lock:
            self._closed = True
            self._key = None
