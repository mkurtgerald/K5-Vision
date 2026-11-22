"""Service-owned copy-first migration staging and manifest-last publication.

The native host must bind the service token, fixed data root and live approved
source peer. A Python admission callback is an integration contract, not native
authentication. This module never opens the original profile or recording path.
"""

from __future__ import annotations

import ctypes
import functools
import hashlib
import hmac
import json
import os
import re
import sqlite3
import stat
import threading
import time
from contextlib import ExitStack, closing
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import UUID, uuid4

from k5vision import identity_state
from k5vision import legacy_identity_snapshot as snapshot
from k5vision.operator_identity_security import ProfileSecretProtector
from k5vision.operator_profile_lock import _exclusive_lock, _same_file
from k5vision.operator_recording_storage import _PATH_NAME, _segment_name

_FAILURE = "Migration target was unavailable or refused"
_STATE = "target-state.json"
_MARKER = "target-binding.json"
_LEDGER = "entries.jsonl"
_RECEIPT = "source-receipt.json"
_ATOMIC = ".atomic"
_AREAS = frozenset({"identity", "recordings", "exports"})
_IDENTITY = (snapshot._AUTHORITIES - {"identity.json"}) | {
    snapshot._STAGED_MANIFEST,
    snapshot._MARKER,
}
_MAX_ENTRIES = 100_100
_MAX_BYTES = 4100 * 1024**3
_MAX_SECONDS = 86400
_PHASES = frozenset(
    {
        "copying",
        "verifying",
        "verified",
        "preparing",
        "prepared",
        "committing",
        "publishing_recordings",
        "recordings_published",
        "publishing_identity",
        "identity_published",
        "committed",
        "rolling_back",
        "rolled_back",
    }
)
_STATE_FIELDS = frozenset(
    {
        "schema_version",
        "revision",
        "phase",
        "stage_id",
        "input_identity_id",
        "ledger_id",
        "atomic_id",
        "recordings_id",
        "exports_id",
        "original_recordings_id",
        "clone_id",
        "retired_clone_ids",
        "ledger_bytes",
        "entry_count",
        "total_bytes",
        "head",
        "receipt_sha256",
        "pending_entry",
    }
)


class TargetMigrationError(RuntimeError):
    """Finite refusal without source paths, secret values or native errors."""


def _safe(method):
    @functools.wraps(method)
    def call(*args, **kwargs):
        failed = False
        try:
            return method(*args, **kwargs)
        except Exception:
            failed = True
        if failed:
            raise TargetMigrationError(_FAILURE)

    return call


@dataclass(frozen=True)
class TargetBinding:
    site_id: str
    transfer_id: str
    source_digest: str
    source_owner_sid: str

    def __post_init__(self):
        if (
            any(type(value) is not str for value in asdict(self).values())
            or len(self.source_owner_sid) > 188
        ):
            raise ValueError
        identifier = UUID(self.site_id.removeprefix("install-"))
        if self.site_id != "install-" + identifier.hex or identifier.version != 4:
            raise ValueError
        if str(UUID(self.transfer_id)) != self.transfer_id:
            raise ValueError
        if re.fullmatch("[a-f0-9]{64}", self.source_digest) is None:
            raise ValueError
        parts = self.source_owner_sid.split("-")
        if (
            len(parts) < 4
            or len(parts) > 18
            or parts[:2] != ["S", "1"]
            or any(
                not value.isascii() or not value.isdigit() or str(int(value)) != value
                for value in parts[2:]
            )
            or int(parts[2]) > 2**48 - 1
            or any(int(value) > 2**32 - 1 for value in parts[3:])
        ):
            raise ValueError


def _directory(path, expected=None):
    identity_state._checked_path(path)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError
    if os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
        raise ValueError
    identity_state._windows_admission(path, private=True, directory=True)
    if expected is not None and _id(info) != tuple(expected):
        raise ValueError
    return info


def _id(info):
    return info.st_dev, info.st_ino


def _file(path):
    info = identity_state._regular_file(path)
    if info.st_size < 0 or (
        os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o077)
    ):
        raise ValueError
    return info


def _signature(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _read_json(path, maximum=32768):
    before = _file(path)
    if not 1 <= before.st_size <= maximum:
        raise ValueError
    with path.open("rb") as stream:
        if _id(os.fstat(stream.fileno())) != _id(before):
            raise ValueError
        raw = stream.read(maximum + 1)
    if len(raw) != before.st_size or _signature(_file(path)) != _signature(before):
        raise ValueError
    return json.loads(raw, object_pairs_hook=identity_state._unique_object)


def _sync_directory(path):
    if os.name == "posix":
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _windows_move(source, destination, *, replace=False):
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    move = kernel.MoveFileExW
    move.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    move.restype = wintypes.BOOL
    # MOVEFILE_WRITE_THROUGH; directory publication omits REPLACE_EXISTING.
    if not move(str(source), str(destination), 8 | (1 if replace else 0)):
        raise ctypes.WinError(ctypes.get_last_error())


def _atomic_json(path, value):
    payload = _canonical(value)
    if len(payload) > 32768:
        raise ValueError
    _directory(path.parent)
    previous = _signature(_file(path)) if path.exists() else None
    atomic = path.parent / _ATOMIC
    _atomic_directory(atomic)
    temporary = atomic / (".target-" + uuid4().hex + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if (_signature(_file(path)) if path.exists() else None) != previous:
            raise ValueError
        if os.name == "nt":
            _windows_move(temporary, path, replace=True)
        else:
            os.replace(temporary, path)
            _sync_directory(path.parent)
            _sync_directory(atomic)
        _file(path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_directory(path, expected=None):
    """Retain bounded crash leftovers as inert metadata, never as authority."""
    _directory(path, expected)
    for index, entry in enumerate(path.iterdir()):
        if index >= 8 or re.fullmatch(r"\.target-[a-f0-9]{32}\.tmp", entry.name) is None:
            raise ValueError
        if _file(entry).st_size > 32768:
            raise ValueError


def _binding_document(path, binding):
    value = _read_json(path)
    if _canonical(value) != _canonical({"schema_version": 1, "binding": asdict(binding)}):
        raise ValueError


def _rename_new(source, destination):
    """Publish directories without POSIX rename's empty-directory replacement."""
    _directory(source)
    _directory(destination.parent)
    if destination.exists() or destination.is_symlink():
        raise ValueError
    if os.name == "nt":
        _windows_move(source, destination)
    elif os.name == "posix":
        function = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
        if function is None:
            raise ValueError
        function.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        function.restype = ctypes.c_int
        if function(-100, os.fsencode(source), -100, os.fsencode(destination), 1) != 0:
            raise OSError(ctypes.get_errno(), "Directory publication refused")
        _sync_directory(source.parent)
        if source.parent != destination.parent:
            _sync_directory(destination.parent)
    else:
        raise ValueError


def _entry_path(area, relative):
    if (
        type(area) is not str
        or area not in _AREAS
        or type(relative) is not str
        or len(relative) > 240
    ):
        raise ValueError
    if area == "identity":
        if relative not in _IDENTITY:
            raise ValueError
    elif area == "recordings":
        parts = relative.split("/")
        if len(parts) != 2 or _PATH_NAME.fullmatch(parts[0]) is None or not _segment_name(parts[1]):
            raise ValueError
    else:
        parts = relative.rsplit(".", 1)
        if len(parts) != 2 or parts[1] not in {"json", "mp4"} or str(UUID(parts[0])) != parts[0]:
            raise ValueError
    return relative.split("/")


def _state_document(value):
    if type(value) is not dict or set(value) - {"prepared_seal"} != _STATE_FIELDS:
        raise ValueError
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["phase"] not in _PHASES
    ):
        raise ValueError
    for name, maximum in (
        ("revision", 2**63 - 1),
        ("ledger_bytes", _MAX_ENTRIES * 4096),
        ("entry_count", _MAX_ENTRIES),
        ("total_bytes", _MAX_BYTES),
    ):
        if type(value[name]) is not int or not 0 <= value[name] <= maximum:
            raise ValueError
    for name in (
        "stage_id",
        "input_identity_id",
        "ledger_id",
        "atomic_id",
        "recordings_id",
        "exports_id",
        "original_recordings_id",
        "clone_id",
    ):
        identifier = value[name]
        if name == "clone_id" and identifier is None:
            continue
        if (
            type(identifier) is not list
            or len(identifier) != 2
            or any(type(v) is not int or v < 0 for v in identifier)
        ):
            raise ValueError
    for name in ("head", "receipt_sha256"):
        if name == "receipt_sha256" and value[name] is None:
            continue
        if type(value[name]) is not str or re.fullmatch("[a-f0-9]{64}", value[name]) is None:
            raise ValueError
    retired = value["retired_clone_ids"]
    if type(retired) is not list or len(retired) > 8:
        raise ValueError
    for index, item in enumerate(retired):
        if (
            type(item) is not dict
            or set(item) != {"name", "identity"}
            or item["name"] != "retired-identity-" + str(index)
        ):
            raise ValueError
        identifier = item["identity"]
        if (
            type(identifier) is not list
            or len(identifier) != 2
            or any(type(v) is not int or v < 0 for v in identifier)
        ):
            raise ValueError
    if "prepared_seal" in value:
        seal = value["prepared_seal"]
        if (
            type(seal) is not dict
            or not {"identity.json", "users.sqlite3", snapshot._SECURITY} <= seal.keys()
            or set(seal) - snapshot._AUTHORITIES
        ):
            raise ValueError
        for item in seal.values():
            if (
                type(item) is not dict
                or set(item) != {"sha256", "signature"}
                or type(item["sha256"]) is not str
                or re.fullmatch("[a-f0-9]{64}", item["sha256"]) is None
            ):
                raise ValueError
            signature = item["signature"]
            if (
                type(signature) is not list
                or len(signature) != 5
                or any(type(v) is not int or v < 0 for v in signature)
            ):
                raise ValueError
    if value["phase"] in {
        "prepared",
        "committing",
        "publishing_recordings",
        "recordings_published",
        "publishing_identity",
        "identity_published",
        "committed",
    } and ("prepared_seal" not in value or value["clone_id"] is None):
        raise ValueError
    if value["pending_entry"] is not None:
        if value["phase"] != "copying":
            raise ValueError
        TargetMigration._validate_entry(value["pending_entry"], registered=True)
    return value


class TargetMigration:
    """Keep one instance per native migration session; ledger registration is O(1).

    Source-copy writers must close/seal each file before registering it. The
    native host must block backend startup while any target journal is pending.
    Cancel/status do not wait for an activation's long verification lock.
    """

    @_safe
    def __init__(self, root: Path, binding: TargetBinding, admission):
        if type(binding) is dict:
            binding = TargetBinding(**binding)
        if type(binding) is not TargetBinding:
            raise ValueError
        self.root = identity_state._checked_path(root)
        self.binding, self.admission = binding, admission
        self._root_id = _id(_directory(self.root))
        self.parent = self.root / ".migration"
        self.stage = self.parent / binding.transfer_id
        self._lock = threading.RLock()
        self._status_lock = threading.Lock()
        self._cancel = threading.Event()
        self._descriptor = None
        self._closed = False
        self._state = None
        self._entries = {}
        self._signatures = {}
        self._prepared = False
        self._cached = {"status": "not_started", "progress": 0}
        self._protector = ProfileSecretProtector(binding.site_id)
        self._assert_admission()

    def _assert_admission(self):
        if self._closed or self.admission.assert_authenticated(self.binding) is not None:
            raise ValueError

    def _owned(self):
        _directory(self.root, self._root_id)
        if self._descriptor is None:
            raise ValueError
        _same_file(self._descriptor, self.parent / "target.lock")
        _directory(self.stage, self._state["stage_id"])
        _directory(self.stage / "identity", self._state["input_identity_id"])
        _atomic_directory(self.stage / _ATOMIC, self._state["atomic_id"])
        if list(_id(_file(self.stage / _LEDGER))) != self._state["ledger_id"]:
            raise ValueError
        _binding_document(self.stage / _MARKER, self.binding)

    def _publish_status(self, *, progress=None):
        with self._status_lock:
            phase = "not_started" if self._state is None else self._state["phase"]
            self._cached = {
                "status": "committed"
                if phase == "committed"
                else "prepared"
                if phase == "prepared"
                else "pending",
                "phase": phase,
                "progress": self._cached.get("progress", 0) if progress is None else progress,
                "registered_files": len(self._entries),
                "registered_bytes": 0 if self._state is None else self._state["total_bytes"],
                "site_id": self.binding.site_id,
                "transfer_id": self.binding.transfer_id,
            }

    def _save(self, **changes):
        state = _state_document({**self._state, **changes, "revision": self._state["revision"] + 1})
        _atomic_json(self.stage / _STATE, state)
        self._state = state
        self._publish_status()

    def _guard(self, *, copying=False):
        self._assert_admission()
        self._owned()
        if copying and self._state["phase"] != "copying":
            raise ValueError
        if _read_json(self.stage / _STATE) != self._state:
            raise ValueError
        if _file(self.stage / _LEDGER).st_size != self._state["ledger_bytes"]:
            raise ValueError

    def _stage_inventory(self):
        allowed = {_STATE, _MARKER, _LEDGER, _ATOMIC, "identity", "recordings", "exports"}
        if self._state["receipt_sha256"] is not None:
            allowed.add(_RECEIPT)
        if self._state["clone_id"] is not None:
            allowed.add("validated-identity")
        allowed.add("original-recordings")
        allowed.update(item["name"] for item in self._state["retired_clone_ids"])
        for index, path in enumerate(self.stage.iterdir()):
            if index >= 24 or path.name not in allowed:
                raise ValueError
            identifiers = {
                "identity": self._state["input_identity_id"],
                _ATOMIC: self._state["atomic_id"],
                "recordings": self._state["recordings_id"],
                "exports": self._state["exports_id"],
                "validated-identity": self._state["clone_id"],
                "original-recordings": self._state["original_recordings_id"],
                **{item["name"]: item["identity"] for item in self._state["retired_clone_ids"]},
            }
            if path.name in identifiers:
                if identifiers[path.name] is None:
                    raise ValueError
                _directory(path, identifiers[path.name])
            else:
                _file(path)

    @_safe
    def begin(self):
        try:
            return self._begin()
        except Exception:
            descriptor, self._descriptor = self._descriptor, None
            self._closed = True
            if descriptor is not None:
                os.close(descriptor)
            raise

    def _begin(self):
        with self._lock:
            self._assert_admission()
            _directory(self.root, self._root_id)
            if self._descriptor is not None:
                self._guard()
                return self._paths()
            if not self.parent.exists():
                self.parent.mkdir(mode=0o700)
            _directory(self.parent)
            lock_path = self.parent / "target.lock"
            try:
                descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                _file(lock_path)
                descriptor = os.open(lock_path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
            try:
                os.set_inheritable(descriptor, False)
                info = _same_file(descriptor, lock_path)
                if info.st_size == 0:
                    os.write(descriptor, b"1")
                    os.fsync(descriptor)
                elif info.st_size != 1:
                    raise ValueError
                os.lseek(descriptor, 0, os.SEEK_SET)
                if os.read(descriptor, 1) != b"1":
                    raise ValueError
                _exclusive_lock(descriptor)
                _same_file(descriptor, lock_path)
                self._descriptor, descriptor = descriptor, None
            finally:
                if descriptor is not None:
                    os.close(descriptor)
            if self.stage.exists():
                _directory(self.stage)
                _binding_document(self.stage / _MARKER, self.binding)
                state = _state_document(_read_json(self.stage / _STATE))
                _directory(self.stage, state["stage_id"])
                self._state = state
                self._load_ledger()
                self._stage_inventory()
                if state["phase"] == "rolled_back":
                    if (self.root / "identity").exists():
                        raise ValueError
                    _directory(self.stage / "recordings", state["recordings_id"])
                    _directory(self.stage / "exports", state["exports_id"])
                    self._save(phase="copying")
            else:
                if (self.root / "identity").exists() or (self.root / "identity").is_symlink():
                    raise ValueError
                for index, entry in enumerate(self.parent.iterdir()):
                    if index >= 17:
                        raise ValueError
                    if entry.name == "target.lock":
                        continue
                    _directory(entry)
                    previous = _state_document(_read_json(entry / _STATE))
                    if previous.get("phase") not in {"rolled_back"}:
                        raise ValueError
                recordings = self.root / "recordings"
                original = None
                if recordings.exists():
                    original = list(_id(_directory(recordings)))
                    if next(recordings.iterdir(), None) is not None:
                        raise ValueError
                else:
                    recordings.mkdir(mode=0o700)
                    original = list(_id(_directory(recordings)))
                self.stage.mkdir(mode=0o700)
                (self.stage / _ATOMIC).mkdir(mode=0o700)
                for area in _AREAS:
                    (self.stage / area).mkdir(mode=0o700)
                _atomic_json(
                    self.stage / _MARKER, {"schema_version": 1, "binding": asdict(self.binding)}
                )
                descriptor = os.open(
                    self.stage / _LEDGER, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                )
                os.fsync(descriptor)
                os.close(descriptor)
                self._state = {
                    "schema_version": 1,
                    "revision": 0,
                    "phase": "copying",
                    "stage_id": list(_id(_directory(self.stage))),
                    "input_identity_id": list(_id(_directory(self.stage / "identity"))),
                    "ledger_id": list(_id(_file(self.stage / _LEDGER))),
                    "atomic_id": list(_id(_directory(self.stage / _ATOMIC))),
                    "recordings_id": list(_id(_directory(self.stage / "recordings"))),
                    "exports_id": list(_id(_directory(self.stage / "exports"))),
                    "original_recordings_id": original,
                    "clone_id": None,
                    "retired_clone_ids": [],
                    "ledger_bytes": 0,
                    "entry_count": 0,
                    "total_bytes": 0,
                    "head": hashlib.sha256(_canonical(asdict(self.binding))).hexdigest(),
                    "receipt_sha256": None,
                    "pending_entry": None,
                }
                _atomic_json(self.stage / _STATE, self._state)
            self._guard()
            self._publish_status()
            return self._paths()

    def _paths(self):
        if self._state["phase"] != "copying":
            return self.status()
        return {**self.status(), **{area: str(self.stage / area) for area in _AREAS}}

    def _load_ledger(self):
        head = hashlib.sha256(_canonical(asdict(self.binding))).hexdigest()
        total = count = consumed = 0
        ledger = self.stage / _LEDGER
        if list(_id(_file(ledger))) != self._state["ledger_id"]:
            raise ValueError
        with ledger.open("rb") as stream:
            if _id(os.fstat(stream.fileno())) != tuple(self._state["ledger_id"]):
                raise ValueError
            while consumed < self._state["ledger_bytes"]:
                raw = stream.readline(min(4097, self._state["ledger_bytes"] - consumed + 1))
                if len(raw) > 4096 or not raw.endswith(b"\n") or count >= _MAX_ENTRIES:
                    raise ValueError
                value = json.loads(raw, object_pairs_hook=identity_state._unique_object)
                entry = value["entry"]
                expected = hashlib.sha256(bytes.fromhex(head) + _canonical(entry)).hexdigest()
                if (
                    set(value) != {"entry", "previous", "hash"}
                    or value["previous"] != head
                    or value["hash"] != expected
                ):
                    raise ValueError
                self._validate_entry(entry, registered=True)
                key = entry["area"], entry["relative"]
                if key in self._entries:
                    raise ValueError
                self._entries[key] = entry
                head = expected
                count += 1
                total += entry["size"]
                consumed += len(raw)
                if consumed > self._state["ledger_bytes"]:
                    raise ValueError
            tail = stream.read(4097)
        if (count, total, consumed, head) != (
            self._state["entry_count"],
            self._state["total_bytes"],
            self._state["ledger_bytes"],
            self._state["head"],
        ):
            raise ValueError
        pending = self._state["pending_entry"]
        if pending is None:
            if tail:
                raise ValueError
            return
        key = pending["area"], pending["relative"]
        if key in self._entries or count >= _MAX_ENTRIES or total + pending["size"] > _MAX_BYTES:
            raise ValueError
        next_head = hashlib.sha256(bytes.fromhex(head) + _canonical(pending)).hexdigest()
        raw = _canonical({"entry": pending, "previous": head, "hash": next_head}) + b"\n"
        if not raw.startswith(tail) or len(tail) > len(raw):
            raise ValueError
        if _file(ledger).st_size != consumed + len(tail):
            raise ValueError
        # The durable pending record fixes every byte of the sole in-flight
        # append. A crash may leave none, a prefix, or all of that record.
        # Complete only its exact suffix; never truncate or guess at a tail.
        with ledger.open("ab", buffering=0) as output:
            if _id(os.fstat(output.fileno())) != tuple(self._state["ledger_id"]):
                raise ValueError
            remaining = memoryview(raw)[len(tail) :]
            while remaining:
                written = output.write(remaining)
                if not written:
                    raise ValueError
                remaining = remaining[written:]
            os.fsync(output.fileno())
        self._entries[key] = pending
        self._save(
            head=next_head,
            ledger_bytes=consumed + len(raw),
            entry_count=count + 1,
            total_bytes=total + pending["size"],
            pending_entry=None,
        )

    @staticmethod
    def _validate_entry(entry, *, registered=False):
        fields = {"area", "relative", "size", "sha256", "mtime_ns"}
        if registered:
            fields.add("file_id")
        if type(entry) is not dict or set(entry) != fields:
            raise ValueError
        _entry_path(entry["area"], entry["relative"])
        if (
            type(entry["size"]) is not int
            or not 0 <= entry["size"] <= _MAX_BYTES
            or type(entry["mtime_ns"]) is not int
            or not 0 <= entry["mtime_ns"] <= 2**63 - 1
            or type(entry["sha256"]) is not str
            or re.fullmatch("[a-f0-9]{64}", entry["sha256"]) is None
            or (
                registered
                and (
                    type(entry["file_id"]) is not list
                    or len(entry["file_id"]) != 2
                    or any(type(v) is not int or v < 0 for v in entry["file_id"])
                )
            )
            or (entry["area"] == "identity" and not 1 <= entry["size"] <= 128 * 1024**2)
        ):
            raise ValueError

    @_safe
    def record_entry(self, entry):
        self._validate_entry(entry)
        with self._lock:
            self._guard(copying=True)
            path = self.stage / entry["area"]
            for part in _entry_path(entry["area"], entry["relative"])[:-1]:
                path /= part
                _directory(path)
            path /= _entry_path(entry["area"], entry["relative"])[-1]
            info = _file(path)
            if info.st_size != entry["size"] or info.st_mtime_ns != entry["mtime_ns"]:
                raise ValueError
            registered = {**entry, "file_id": list(_id(info))}
            key = entry["area"], entry["relative"]
            if key in self._entries:
                if self._entries[key] != registered:
                    raise ValueError
                return self.status()
            if (
                len(self._entries) >= _MAX_ENTRIES
                or self._state["total_bytes"] + info.st_size > _MAX_BYTES
            ):
                raise ValueError
            head = hashlib.sha256(
                bytes.fromhex(self._state["head"]) + _canonical(registered)
            ).hexdigest()
            raw = (
                _canonical({"entry": registered, "previous": self._state["head"], "hash": head})
                + b"\n"
            )
            ledger = self.stage / _LEDGER
            if _file(ledger).st_size != self._state["ledger_bytes"]:
                raise ValueError
            self._save(pending_entry=registered)
            with ledger.open("ab", buffering=0) as output:
                remaining = memoryview(raw)
                while remaining:
                    count = output.write(remaining)
                    if not count:
                        raise ValueError
                    remaining = remaining[count:]
                os.fsync(output.fileno())
            self._entries[key] = registered
            self._save(
                head=head,
                ledger_bytes=self._state["ledger_bytes"] + len(raw),
                entry_count=len(self._entries),
                total_bytes=self._state["total_bytes"] + info.st_size,
                pending_entry=None,
            )
            return self.status()

    def _purpose(self, purpose, clear):
        if type(purpose) is not str or type(clear) not in (bytes, bytearray, memoryview):
            raise ValueError
        raw = bytes(clear)
        if purpose == "oidc-client-secret":
            if not 1 <= len(raw) <= 16384 or len(raw.decode("utf-8")) > 4096:
                raise ValueError
        elif str(UUID(purpose)) != purpose or len(raw) != 20:
            raise ValueError
        return raw

    @_safe
    def protect(self, purpose, clear):
        with self._lock:
            self._guard(copying=True)
            raw = self._purpose(purpose, clear)
            protected = self._protector.protect(raw, purpose)
            if (
                type(protected) is not bytes
                or not protected.startswith(b"dpapi-v1:")
                or len(protected) > 65545
            ):
                raise ValueError
            self._assert_admission()
            return protected

    @_safe
    def verify(self, purpose, clear, protected):
        with self._lock:
            self._guard(copying=True)
            raw = self._purpose(purpose, clear)
            if (
                type(protected) is not bytes
                or not protected.startswith(b"dpapi-v1:")
                or not 10 <= len(protected) <= 65545
                or not hmac.compare_digest(self._protector.unprotect(protected, purpose), raw)
            ):
                raise ValueError
            self._assert_admission()

    def _validate_receipt(self, receipt):
        fields = {
            "schema_version",
            "site_id",
            "transfer_id",
            "source_digest",
            "files",
            "activation_allowed",
            "staged_files",
            "omitted_source_entries",
            "phase",
            "media_transfer_required",
            "native_activation_required",
        }
        if type(receipt) is not dict or set(receipt) != fields or len(_canonical(receipt)) > 16384:
            raise ValueError
        if (
            type(receipt.get("schema_version")) is not int
            or receipt["schema_version"] != 1
            or receipt.get("site_id") != self.binding.site_id
            or receipt.get("transfer_id") != self.binding.transfer_id
            or receipt.get("source_digest") != self.binding.source_digest
            or receipt.get("activation_allowed") is not False
            or receipt.get("phase") != "identity_prepared"
            or receipt.get("media_transfer_required") is not True
            or receipt.get("native_activation_required") is not True
        ):
            raise ValueError
        omitted = receipt["omitted_source_entries"]
        if (
            type(omitted) is not list
            or any(type(name) is not str for name in omitted)
            or omitted != sorted(set(omitted))
            or set(omitted)
            - {
                "operator.lock",
                "operator-mediamtx.json",
                "operator-gateway.json",
                "time-admin",
                "exports",
                "analytics-samples",
            }
        ):
            raise ValueError
        files, staged = receipt["files"], receipt["staged_files"]
        if type(files) is not dict or type(staged) is not dict or set(files) != set(staged):
            raise ValueError
        if (
            not {"identity.json", "users.sqlite3", snapshot._SECURITY} <= files.keys()
            or set(files) - snapshot._AUTHORITIES
        ):
            raise ValueError
        if (
            hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
            != self.binding.source_digest
        ):
            raise ValueError
        for values in (files, staged):
            for value in values.values():
                if (
                    type(value) is not dict
                    or set(value) != {"bytes", "sha256"}
                    or type(value["bytes"]) is not int
                    or not 1 <= value["bytes"] <= 128 * 1024**2
                    or re.fullmatch("[a-f0-9]{64}", value["sha256"]) is None
                ):
                    raise ValueError
        if any(
            staged[name] != value for name, value in files.items() if name != snapshot._SECURITY
        ):
            raise ValueError

    @_safe
    def set_snapshot(self, receipt):
        with self._lock:
            self._guard(copying=True)
            self._validate_receipt(receipt)
            path = self.stage / _RECEIPT
            if path.exists():
                if _read_json(path) != receipt:
                    raise ValueError
            else:
                _atomic_json(path, receipt)
            self._save(receipt_sha256=hashlib.sha256(_canonical(receipt)).hexdigest())

    def _checkpoint(self, progress, stop, note):
        if self._cancel.is_set() or (stop is not None and stop() is not False):
            raise ValueError
        if time.monotonic() >= self._deadline:
            raise ValueError
        if progress is not None:
            progress(note)
        self._guard()

    def _internal_checkpoint(self):
        if self._cancel.is_set() or time.monotonic() >= self._deadline:
            raise ValueError

    def _hash(self, path, progress, stop, note):
        before = _file(path)
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            if (_id(os.fstat(stream.fileno())), os.fstat(stream.fileno()).st_size) != (
                _id(before),
                before.st_size,
            ):
                raise ValueError
            remaining = before.st_size
            while block := stream.read(min(4 * 1024**2, remaining + 1)):
                self._checkpoint(progress, stop, note)
                remaining -= len(block)
                if remaining < 0:
                    raise ValueError
                digest.update(block)
            if remaining:
                raise ValueError
        if _signature(_file(path)) != _signature(before):
            raise ValueError
        return digest.hexdigest(), _signature(before)

    def _inventory(self, area, root):
        _directory(root)
        observed = set()
        for index, path in enumerate(root.iterdir()):
            self._internal_checkpoint()
            if index >= _MAX_ENTRIES:
                raise ValueError
            if area == "recordings":
                if _PATH_NAME.fullmatch(path.name) is None:
                    raise ValueError
                _directory(path)
                for item in path.iterdir():
                    self._internal_checkpoint()
                    relative = path.name + "/" + item.name
                    _entry_path(area, relative)
                    _file(item)
                    observed.add((area, relative))
                    if len(observed) > _MAX_ENTRIES:
                        raise ValueError
            else:
                _entry_path(area, path.name)
                _file(path)
                observed.add((area, path.name))
        return observed

    def _verify_inputs(self, progress, stop):
        self._stage_inventory()
        receipt = _read_json(self.stage / _RECEIPT)
        self._validate_receipt(receipt)
        if hashlib.sha256(_canonical(receipt)).hexdigest() != self._state["receipt_sha256"]:
            raise ValueError
        expected_identity = {
            snapshot._STAGED_MANIFEST if name == "identity.json" else name
            for name in receipt["files"]
        } | {snapshot._MARKER}
        if {
            relative for area, relative in self._entries if area == "identity"
        } != expected_identity:
            raise ValueError
        marker = _read_json(self.stage / "identity" / snapshot._MARKER)
        if _canonical(marker) != _canonical(
            {
                key: receipt[key]
                for key in (
                    "schema_version",
                    "site_id",
                    "transfer_id",
                    "source_digest",
                    "files",
                    "activation_allowed",
                )
            }
        ):
            raise ValueError
        observed = set()
        for area in _AREAS:
            observed |= self._inventory(area, self.stage / area)
        if observed != self._entries.keys():
            raise ValueError
        for index, (key, entry) in enumerate(self._entries.items()):
            area, relative = key
            path = self.stage / area / relative
            if list(_id(_file(path))) != entry["file_id"]:
                raise ValueError
            digest, signature = self._hash(
                path,
                progress,
                stop,
                {
                    "phase": "verifying",
                    "verified_files": index,
                    "total_files": len(self._entries),
                },
            )
            if (
                digest != entry["sha256"]
                or signature[2] != entry["size"]
                or signature[3] != entry["mtime_ns"]
            ):
                raise ValueError
            self._signatures[key] = signature
            if area == "identity" and relative != snapshot._MARKER:
                name = "identity.json" if relative == snapshot._STAGED_MANIFEST else relative
                if receipt["staged_files"][name] != {
                    "bytes": entry["size"],
                    "sha256": entry["sha256"],
                }:
                    raise ValueError
        self._checkpoint(
            progress,
            stop,
            {
                "phase": "verified",
                "verified_files": len(self._entries),
                "total_files": len(self._entries),
            },
        )
        self._verify_input_signatures()
        return receipt

    def _verify_input_signatures(self):
        observed = set()
        for area in _AREAS:
            observed |= self._inventory(area, self.stage / area)
        if observed != self._entries.keys():
            raise ValueError
        for key, expected in self._signatures.items():
            self._internal_checkpoint()
            if _signature(_file(self.stage / key[0] / key[1])) != expected:
                raise ValueError

    def _clone_identity(self, receipt, progress, stop):
        clone = self.stage / "validated-identity"
        if clone.exists():
            if self._state["clone_id"] is None:
                raise ValueError
            _directory(clone, self._state["clone_id"])
            retired = list(self._state["retired_clone_ids"])
            if retired and retired[-1]["identity"] == self._state["clone_id"]:
                name = retired[-1]["name"]
            else:
                if len(retired) >= 8:
                    raise ValueError
                name = "retired-identity-" + str(len(retired))
                retired.append({"name": name, "identity": self._state["clone_id"]})
                # Journal both legal locations before moving the owned clone.
                self._save(retired_clone_ids=retired)
            _rename_new(clone, self.stage / name)
            self._save(clone_id=None)
        elif self._state["clone_id"] is not None:
            matching = [
                item
                for item in self._state["retired_clone_ids"]
                if item["identity"] == self._state["clone_id"]
            ]
            if len(matching) != 1:
                raise ValueError
            _directory(self.stage / matching[0]["name"], self._state["clone_id"])
        clone.mkdir(mode=0o700)
        self._save(clone_id=list(_id(_directory(clone))))
        for name in receipt["files"]:
            source_name = snapshot._STAGED_MANIFEST if name == "identity.json" else name
            original = self.stage / "identity" / source_name
            destination = clone / name
            before = _file(original)
            digest = hashlib.sha256()
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as output, original.open("rb") as incoming:
                while block := incoming.read(1024**2):
                    self._checkpoint(progress, stop, {"phase": "preparing_identity"})
                    output.write(block)
                    digest.update(block)
                output.flush()
                os.fsync(output.fileno())
            if digest.hexdigest() != receipt["staged_files"][name]["sha256"]:
                raise ValueError
            os.utime(destination, ns=(before.st_atime_ns, before.st_mtime_ns))
        return clone

    def _move_recordings(self):
        recordings, backup = self.root / "recordings", self.stage / "original-recordings"
        self._save(phase="publishing_recordings")
        if self._state["original_recordings_id"] is not None:
            _directory(recordings, self._state["original_recordings_id"])
            if next(recordings.iterdir(), None) is not None:
                raise ValueError
            _rename_new(recordings, backup)
        elif recordings.exists():
            raise ValueError
        _directory(self.stage / "recordings", self._state["recordings_id"])
        _rename_new(self.stage / "recordings", recordings)
        self._save(phase="recordings_published")

    def _prepare_authorities(self, clone):
        from k5vision.operator_storage_settings import _atomic, validate_policy

        path = clone / snapshot._SECURITY
        with closing(snapshot._connect(path, readonly=True)) as connection:
            seal = snapshot._security_digest(connection)
            snapshot._bookkeeping(
                connection,
                snapshot.SnapshotBinding(
                    self.binding.site_id,
                    self.binding.transfer_id,
                    self.binding.source_digest,
                ),
                seal,
            )
        with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True)) as connection:
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("DROP TABLE " + snapshot._TABLE)
            connection.commit()
        storage_path = clone / "operator-storage.json"
        if storage_path.exists():
            storage = _read_json(storage_path, 16384)
            if (
                set(storage)
                != {
                    "schema_version",
                    "site_id",
                    "revision",
                    "quota_bytes",
                    "minimum_free_bytes",
                    "retention_seconds",
                    "recording_directory",
                    "pending_recording_directory",
                    "migration_error",
                }
                or type(storage["schema_version"]) is not int
                or storage["schema_version"] != 1
                or storage["site_id"] != self.binding.site_id
                or type(storage["revision"]) is not int
                or not 0 <= storage["revision"] < 2**63 - 1
                or storage["pending_recording_directory"] is not None
                or storage["migration_error"] is not None
            ):
                raise ValueError
            validate_policy(
                storage["quota_bytes"], storage["minimum_free_bytes"], storage["retention_seconds"]
            )
            storage.update(
                recording_directory=str(self.root / "recordings"), revision=storage["revision"] + 1
            )
            _atomic(storage_path, storage)

    def _validate_authorities(self, clone):
        from k5vision.operator_audit import OperatorAuditStore
        from k5vision.operator_component_state import ComponentStateAnchor
        from k5vision.operator_events import OperatorEventStore
        from k5vision.operator_identity_security import AccountSecurityStore
        from k5vision.operator_integrations import OperatorIntegrationStore
        from k5vision.operator_oidc import OIDCStore
        from k5vision.operator_product_state import OperatorCameraStore
        from k5vision.operator_recording_policy import RecordingPolicyStore
        from k5vision.operator_role_policy import OperatorRolePolicy
        from k5vision.operator_storage_settings import OperatorStorageSettings
        from k5vision.services.device_registry import DeviceRegistry
        from k5vision.services.user_registry import UserRegistry

        state = identity_state.load_identity_state(clone)
        if state.site_id != self.binding.site_id:
            raise ValueError
        component_files = {
            "roles": "role-profiles.json",
            "account_security": snapshot._SECURITY,
            "audit": "operator-audit.sqlite3",
            "storage": "operator-storage.json",
            "integrations": "operator-integrations.sqlite3",
            "person_events": "operator-events.sqlite3",
        }
        with closing(snapshot._connect(state.database_path, readonly=True)) as connection:
            tables = dict(connection.execute("SELECT name,type FROM sqlite_master"))
            if any(
                tables.get(name) != "table"
                for name in (
                    "users",
                    "user_credentials",
                    "user_audit",
                    "user_camera_access",
                    "operator_component_anchors",
                )
            ):
                raise ValueError
            if not {"issued_at", "last_checked_at"} <= {
                row[1] for row in connection.execute("PRAGMA table_info(user_credentials)")
            }:
                raise ValueError
            anchors = connection.execute(
                "SELECT site_id,component,schema_version FROM operator_component_anchors LIMIT 7"
            ).fetchall()
            if (
                len(anchors) > len(component_files)
                or len({r[1] for r in anchors}) != len(anchors)
                or any(
                    site != state.site_id or name not in component_files or version != 1
                    for site, name, version in anchors
                )
                or not any(name == "account_security" for _, name, _ in anchors)
            ):
                raise ValueError
            if (
                tables.get("operator_recording_policy_anchor") is not None
                and not (clone / "operator-recording-policy.json").exists()
            ):
                raise ValueError
        for name, filename in component_files.items():
            ComponentStateAnchor(clone, state.site_id, name, present=(clone / filename).exists())
        with ExitStack() as stack:

            def opened(value):
                stack.callback(value.close)
                return value

            opened(UserRegistry(database_path=state.database_path, site_id=state.site_id))
            security = opened(AccountSecurityStore(clone, state.site_id))
            if (
                security._execute(
                    lambda connection: connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE name='oidc_provider'"
                    ).fetchone()
                )
                is not None
            ):
                OIDCStore(security).provider()
            registry = None
            if (clone / "devices.sqlite3").exists():
                registry = opened(
                    DeviceRegistry(database_path=clone / "devices.sqlite3", site_id=state.site_id)
                )
            sources = ()
            if (clone / "operator-cameras.sqlite3").exists():
                if registry is None:
                    raise ValueError
                sources = opened(
                    OperatorCameraStore(clone, state.site_id, registry=registry)
                ).list_sources()
            if (clone / "operator-recording-policy.json").exists():
                policy = opened(RecordingPolicyStore(clone, state.site_id))
                for source in sources:
                    policy.get(source)
            for filename, factory in (
                ("role-profiles.json", OperatorRolePolicy),
                ("operator-audit.sqlite3", OperatorAuditStore),
                ("operator-events.sqlite3", OperatorEventStore),
                ("operator-integrations.sqlite3", OperatorIntegrationStore),
            ):
                if (clone / filename).exists():
                    opened(factory(clone, state.site_id))
            if (clone / "operator-storage.json").exists():
                OperatorStorageSettings(clone, state.site_id, self.root / "recordings").snapshot()

    def _validate_exports(self, root):
        from k5vision.operator_exports import (
            MAX_EXPORT_JOBS,
            MAX_RETAINED_BYTES,
            _duration,
            _verify_mp4,
            export_start,
        )

        entries = list(root.iterdir())
        if len(entries) > MAX_EXPORT_JOBS * 2:
            raise ValueError
        identifiers = {path.stem for path in entries}
        total = 0
        for identifier in identifiers:
            metadata, media = root / (identifier + ".json"), root / (identifier + ".mp4")
            value = _read_json(metadata, 4096)
            if (
                set(value)
                != {
                    "schema_version",
                    "site_id",
                    "id",
                    "owner",
                    "camera",
                    "start",
                    "duration",
                    "created",
                    "bytes",
                    "sha256",
                }
                or type(value["schema_version"]) is not int
                or value["schema_version"] != 1
                or value["site_id"] != self.binding.site_id
                or value["id"] != identifier
                or str(UUID(value["owner"])) != value["owner"]
                or str(UUID(value["camera"])) != value["camera"]
                or type(value["bytes"]) is not int
                or type(value["sha256"]) is not str
                or re.fullmatch("[a-f0-9]{64}", value["sha256"]) is None
            ):
                raise ValueError
            export_start(value["start"])
            export_start(value["created"])
            duration = _duration(value["duration"])
            info = _file(media)
            if (
                info.st_size != value["bytes"]
                or self._entries[("exports", media.name)]["sha256"] != value["sha256"]
            ):
                raise ValueError
            _verify_mp4(media, duration)
            total += info.st_size
        if total > MAX_RETAINED_BYTES:
            raise ValueError

    def _rollback_owned(self):
        if self._state["phase"] == "committed":
            raise ValueError
        self._owned()
        durable = _state_document(_read_json(self.stage / _STATE))
        if durable["phase"] == "committed":
            # Atomic replacement can succeed before its durability admission
            # raises. A missing acknowledgement must never reverse a visible
            # committed journal. Admit its exact successor and active custody;
            # otherwise refuse without changing any journal or directory.
            expected = {
                **self._state,
                "phase": "committed",
                "revision": self._state["revision"] + 1,
            }
            if durable != expected:
                raise ValueError
            _directory(self.root / "identity", durable["clone_id"])
            _directory(self.root / "recordings", durable["recordings_id"])
            _directory(self.root / "identity" / "exports", durable["exports_id"])
            if (
                identity_state.load_identity_state(self.root / "identity").site_id
                != self.binding.site_id
            ):
                raise ValueError
            self._state = durable
            self._prepared = False
            self._publish_status(progress=100)
            raise ValueError
        if durable != self._state:
            # A different acknowledged state must be reopened through bounded
            # recovery instead of overwritten from stale in-memory state.
            raise ValueError
        self._save(phase="rolling_back")
        clone, active = self.stage / "validated-identity", self.root / "identity"
        clone_id = self._state["clone_id"]
        if active.exists() and clone_id is not None and _id(_directory(active)) == tuple(clone_id):
            _rename_new(active, clone)
        if clone.exists() and (clone / "exports").exists():
            _directory(clone / "exports", self._state["exports_id"])
            _rename_new(clone / "exports", self.stage / "exports")
        recordings, staged = self.root / "recordings", self.stage / "recordings"
        if recordings.exists() and _id(_directory(recordings)) == tuple(
            self._state["recordings_id"]
        ):
            _rename_new(recordings, staged)
        backup = self.stage / "original-recordings"
        if backup.exists():
            _directory(backup, self._state["original_recordings_id"])
            _rename_new(backup, recordings)
        self._save(phase="rolled_back")

    def _locations(self, clone, *, published=False):
        return {
            "identity": self.stage / "identity",
            "recordings": self.root / "recordings" if published else self.stage / "recordings",
            "exports": clone / "exports",
        }

    def _verify_registered_signatures(self, clone, *, published=False):
        observed = set()
        locations = self._locations(clone, published=published)
        for area, root in locations.items():
            observed |= self._inventory(area, root)
        if observed != self._entries.keys():
            raise ValueError
        for key, expected in self._signatures.items():
            self._internal_checkpoint()
            if _signature(_file(locations[key[0]] / key[1])) != expected:
                raise ValueError

    def _clone_seal(self, clone, receipt):
        self._internal_checkpoint()
        _directory(clone, self._state["clone_id"])
        expected = set(receipt["files"]) | {"exports"}
        if {path.name for path in clone.iterdir()} != expected:
            raise ValueError
        result = {}
        for name in receipt["files"]:
            path = clone / name
            before = _file(path)
            if before.st_size > 128 * 1024**2:
                raise ValueError
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                if _id(os.fstat(stream.fileno())) != _id(before):
                    raise ValueError
                remaining = before.st_size
                while block := stream.read(min(4 * 1024**2, remaining + 1)):
                    if self._cancel.is_set() or time.monotonic() >= self._deadline:
                        raise ValueError
                    remaining -= len(block)
                    if remaining < 0:
                        raise ValueError
                    digest.update(block)
                if remaining:
                    raise ValueError
            if _signature(_file(path)) != _signature(before):
                raise ValueError
            result[name] = {"sha256": digest.hexdigest(), "signature": list(_signature(before))}
        return result

    def _prepared_receipt(self):
        receipt = _read_json(self.stage / _RECEIPT)
        self._validate_receipt(receipt)
        if hashlib.sha256(_canonical(receipt)).hexdigest() != self._state["receipt_sha256"]:
            raise ValueError
        return receipt

    @_safe
    def prepare_activation(self, *, progress=None, stop=None):
        with self._lock:
            self._guard(copying=True)
            if (self.root / "identity").exists() or (self.root / "identity").is_symlink():
                raise ValueError
            self._deadline = time.monotonic() + _MAX_SECONDS
            try:
                self._save(phase="verifying")
                receipt = self._verify_inputs(progress, stop)
                self._save(phase="verified")
                self._save(phase="preparing")
                clone = self._clone_identity(receipt, progress, stop)
                _directory(self.stage / "exports", self._state["exports_id"])
                _rename_new(self.stage / "exports", clone / "exports")
                self._prepare_authorities(clone)
                self._validate_authorities(clone)
                self._validate_exports(clone / "exports")
                seal = self._clone_seal(clone, receipt)
                self._checkpoint(
                    progress,
                    stop,
                    {
                        "phase": "prepared",
                        "verified_files": len(self._entries),
                        "total_files": len(self._entries),
                    },
                )
                self._verify_registered_signatures(clone)
                if self._clone_seal(clone, self._prepared_receipt()) != seal:
                    raise ValueError
                self._save(phase="prepared", prepared_seal=seal)
                self._prepared = True
                return {**self.status(), "prepared": True, "activated": False}
            except Exception:
                self._prepared = False
                self._rollback_owned()
                raise

    @_safe
    def commit(self, *, stop=None):
        with self._lock:
            self._guard()
            if not self._prepared or self._state["phase"] != "prepared":
                raise ValueError
            if (self.root / "identity").exists() or (self.root / "identity").is_symlink():
                raise ValueError
            try:
                clone = self.stage / "validated-identity"
                self._checkpoint(None, stop, {"phase": "committing"})
                self._save(phase="committing")
                receipt = self._prepared_receipt()
                self._stage_inventory()
                self._verify_registered_signatures(clone)
                if self._clone_seal(clone, receipt) != self._state["prepared_seal"]:
                    raise ValueError
                # Native wiring revalidates the original administrator/source
                # immediately before this explicit call. No callback follows
                # these final target checks or the manifest-last publication.
                self._move_recordings()
                self._save(phase="publishing_identity")
                _directory(clone, self._state["clone_id"])
                _rename_new(clone, self.root / "identity")
                self._save(phase="identity_published")
                if (
                    identity_state.load_identity_state(self.root / "identity").site_id
                    != self.binding.site_id
                ):
                    raise ValueError
                self._save(phase="committed")
                self._prepared = False
                self._publish_status(progress=100)
                return {**self.status(), "activated": True, "original_source_preserved": True}
            except Exception:
                self._prepared = False
                self._rollback_owned()
                raise

    @_safe
    def activate(self, *, progress=None, stop=None):
        """Convenience for trusted in-process callers; native RPC uses both steps."""
        self.prepare_activation(progress=progress, stop=stop)
        return self.commit(stop=stop)

    @_safe
    def recover(self):
        with self._lock:
            self._guard()
            if self._state["phase"] not in {"copying", "rolled_back", "committed"}:
                self._rollback_owned()
            return self.status()

    @_safe
    def rollback(self):
        if not self._lock.acquire(blocking=False):
            raise ValueError
        try:
            self._owned()
            self._rollback_owned()
            return self.status()
        finally:
            self._lock.release()

    def cancel(self):
        self._cancel.set()

    def status(self):
        with self._status_lock:
            return dict(self._cached)

    @_safe
    def close(self):
        with self._lock:
            descriptor, self._descriptor = self._descriptor, None
            self._closed = True
            if descriptor is not None:
                os.close(descriptor)

    def __enter__(self):
        return self

    def __exit__(self, *_arguments):
        self.close()


@_safe
def inspect_target(root: Path):
    """Read-only startup guard; a half-published identity never implies ready."""
    root = identity_state._checked_path(root)
    _directory(root)
    parent = root / ".migration"
    if not parent.exists():
        return {"status": "none"}
    _directory(parent)
    observed = []
    for index, path in enumerate(parent.iterdir()):
        if index >= 17:
            raise ValueError
        if path.name == "target.lock":
            _file(path)
            continue
        if str(UUID(path.name)) != path.name:
            raise ValueError
        _directory(path)
        state = _state_document(_read_json(path / _STATE))
        _directory(path, state["stage_id"])
        marker = _read_json(path / _MARKER)
        binding = TargetBinding(**marker["binding"])
        _binding_document(path / _MARKER, binding)
        if binding.transfer_id != path.name:
            raise ValueError
        observed.append((binding, state))
    if not observed:
        return {"status": "none"}
    for binding, state in observed:
        if state["phase"] == "committed":
            _directory(root / "identity", state["clone_id"])
            if identity_state.load_identity_state(root / "identity").site_id != binding.site_id:
                raise ValueError
            if any(other["phase"] not in {"committed", "rolled_back"} for _, other in observed):
                return {"status": "pending"}
            return {
                "status": "committed",
                "site_id": binding.site_id,
                "transfer_id": binding.transfer_id,
            }
    return {"status": "pending"}


@_safe
def recover_pending(root: Path, binding: TargetBinding, admission):
    """Explicitly authorize target-only rollback using a newly verified source peer.

    MFA may legitimately change the old source fingerprint between attempts.
    Recovery binds both attempts to the same site and trusted native owner SID;
    it never broadens admission to a foreign profile or deletes staged bytes.
    """
    if type(binding) is dict:
        binding = TargetBinding(**binding)
    if type(binding) is not TargetBinding or admission.assert_authenticated(binding) is not None:
        raise ValueError
    root = identity_state._checked_path(root)
    _directory(root)
    parent = root / ".migration"
    if not parent.exists():
        if (root / "identity").exists() or (root / "identity").is_symlink():
            raise ValueError
        return {"status": "none", "recovered_transfers": 0}
    _directory(parent)
    pending = []
    for index, path in enumerate(parent.iterdir()):
        if index >= 17:
            raise ValueError
        if path.name == "target.lock":
            _file(path)
            continue
        if str(UUID(path.name)) != path.name:
            raise ValueError
        _directory(path)
        marker = _read_json(path / _MARKER)
        old = TargetBinding(**marker["binding"])
        _binding_document(path / _MARKER, old)
        if old.transfer_id != path.name:
            raise ValueError
        state = _state_document(_read_json(path / _STATE))
        _directory(path, state["stage_id"])
        if (
            old.site_id != binding.site_id
            or old.source_owner_sid != binding.source_owner_sid
            or state["phase"] == "committed"
        ):
            raise ValueError
        if old.transfer_id == binding.transfer_id:
            if old != binding:
                raise ValueError
            continue
        if state["phase"] != "rolled_back":
            pending.append((old, state))
    active = root / "identity"
    if active.exists() or active.is_symlink():
        active_id = _id(_directory(active))
        matching = [
            state
            for _, state in pending
            if state["clone_id"] is not None and active_id == tuple(state["clone_id"])
        ]
        if len(matching) != 1:
            raise ValueError

    class RecoveryAdmission:
        def __init__(self, expected):
            self.expected = expected

        def assert_authenticated(self, candidate):
            if candidate != self.expected or admission.assert_authenticated(binding) is not None:
                raise ValueError

    recovered = 0
    for old, _state in pending:
        with TargetMigration(root, old, RecoveryAdmission(old)) as session:
            session.begin()
            if admission.assert_authenticated(binding) is not None:
                raise ValueError
            session.rollback()
            recovered += 1
    if admission.assert_authenticated(binding) is not None:
        raise ValueError
    return {"status": "recovered" if recovered else "none", "recovered_transfers": recovered}
