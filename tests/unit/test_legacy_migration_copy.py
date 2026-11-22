from __future__ import annotations

import hashlib
import io
import os
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import test_operator_time_admin as time_helpers

from k5vision import identity_state
from k5vision import legacy_migration_copy as copy
from k5vision.windows_identity_security import (
    _OWNER_RIGHTS,
    AccessEntry,
    WindowsIdentitySecurityError,
)

private_profile = time_helpers.private_profile
_SERVICE = "S-1-5-80-1-2-3-4-5"
_FOREIGN = "S-1-5-21-42"
_SEGMENT = "2026-11-22_02-00-00-000000_+0530.mp4"


def _acl(*, flags=3):
    return tuple(
        AccessEntry(0, flags, copy._FILE_ALL, sid) for sid in (_SERVICE, copy._BA, copy._SY)
    )


def _policy(entries=None, **changes):
    arguments = {
        "protected": True,
        "require_protected": True,
        "directory": True,
    }
    arguments.update(changes)
    copy._target_policy(copy._BA, _acl() if entries is None else entries, _SERVICE, **arguments)


class _PortableStage:
    """Filesystem algorithm seam; this adapter proves no native Windows ACL."""

    def __init__(self, root):
        self.root = root
        self.pinned = ()
        self.checks = 0
        self.created = 0

    @contextmanager
    def pin(self, transfer, area, parts):
        parent = self.root / ".migration" / transfer / area
        for part in parts[:-1]:
            parent /= part
            parent.mkdir(mode=0o700, exist_ok=True)
        self.pinned = tuple((p, p.stat().st_ino) for p in (self.root, parent))
        try:
            self.recheck()
            yield parent / parts[-1]
            self.recheck()
        finally:
            self.pinned = ()

    def recheck(self):
        self.checks += 1
        for path, inode in self.pinned:
            info = copy._ordinary(path, directory=True)
            if info.st_ino != inode or info.st_mode & 0o077:
                raise ValueError

    def admit_file(self, path, *, created=False):
        info = copy._ordinary(path, directory=False)
        if info.st_mode & 0o077:
            raise ValueError
        return info

    @contextmanager
    def open_source(self, path):
        with path.open("rb", buffering=0) as stream:
            yield stream

    @contextmanager
    def open_existing(self, path):
        with path.open("rb", buffering=0) as stream:
            yield stream

    @contextmanager
    def open_new(self, path):
        self.created += 1
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        with os.fdopen(descriptor, "w+b", buffering=0) as stream:
            yield stream

    def set_mtime(self, stream, value):
        os.utime(stream.fileno(), ns=(value, value))


@pytest.fixture
def files(private_profile):
    # Configured storage is deliberately outside the fixed legacy profile.
    source_root = private_profile / "configured-footage-volume"
    source_root.mkdir(mode=0o700)
    source = source_root / _SEGMENT
    source.write_bytes(b"retained video bytes\x00" * 100003)
    source.chmod(0o600)
    os.utime(source, ns=(1760000000123400000, 1760000000123400000))
    entry = {
        "area": "recordings",
        "relative": "k5_" + "a" * 64 + "/" + source.name,
        "size": source.stat().st_size,
        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "mtime_ns": source.stat().st_mtime_ns,
    }
    root = private_profile / "ProgramData" / "K5Vision"
    root.mkdir(mode=0o700, parents=True)
    transfer = str(uuid4())
    for area in ("identity", "recordings", "exports"):
        (root / ".migration" / transfer / area).mkdir(mode=0o700, parents=True)
    destination = root / ".migration" / transfer / entry["area"] / entry["relative"]
    return SimpleNamespace(
        source=source,
        entry=entry,
        transfer=transfer,
        destination=destination,
        backend=_PortableStage(root),
    )


def _run(files, **changes):
    arguments = {
        "source": files.source,
        "entry": files.entry,
        "transfer_id": files.transfer,
        "backend": files.backend,
    }
    arguments.update(changes)
    return copy._copy_with_backend(**arguments)


def test_copy_preserves_original_and_every_target_byte_filename_offset_and_mtime(files):
    before = files.source.stat()
    original = files.source.read_bytes()
    _run(files)
    assert files.destination.read_bytes() == original
    assert files.destination.stat().st_mtime_ns == before.st_mtime_ns
    assert copy._signature(files.source.stat()) == copy._signature(before)
    assert files.source.read_bytes() == original
    assert files.destination.name.endswith("_+0530.mp4")
    assert files.backend.created == 1 and files.backend.checks > 4


def test_complete_identical_existing_target_resumes_without_opening_for_write(files):
    _run(files)
    before = copy._signature(files.destination.stat())
    _run(files)
    assert copy._signature(files.destination.stat()) == before
    assert files.backend.created == 1


@pytest.mark.parametrize("change", ["partial", "corrupt", "mtime", "symlink", "hardlink"])
def test_existing_changed_or_partial_target_is_preserved_and_never_overwritten(files, change):
    files.destination.parent.mkdir(mode=0o700)
    data = files.source.read_bytes()
    if change == "symlink":
        files.destination.symlink_to(files.source)
    elif change == "hardlink":
        os.link(files.source, files.destination)
    else:
        files.destination.write_bytes(data[:30] if change == "partial" else data)
        files.destination.chmod(0o600)
        if change == "corrupt":
            with files.destination.open("r+b") as stream:
                stream.write(b"changed")
        os.utime(files.destination, ns=(files.entry["mtime_ns"], files.entry["mtime_ns"]))
        if change == "mtime":
            os.utime(files.destination, ns=(1, 1))
    before = files.destination.lstat()
    preserved = files.destination.read_bytes()
    with pytest.raises((ValueError, identity_state.IdentityStateError)):
        _run(files)
    assert files.destination.read_bytes() == preserved
    assert copy._signature(files.destination.lstat()) == copy._signature(before)
    assert files.backend.created == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("size", -1),
        ("size", True),
        ("size", 2**43),
        ("mtime_ns", -1),
        ("mtime_ns", True),
        ("sha256", "A" * 64),
        ("area", "other"),
        ("relative", "../outside.mp4"),
        ("relative", "k5_" + "a" * 64 + "\\" + _SEGMENT),
        ("relative", "k5_" + "a" * 64 + "/invalid.mp4"),
    ],
)
def test_invalid_metadata_never_creates_target(files, field, value):
    entry = {**files.entry, field: value}
    with pytest.raises(ValueError):
        _run(files, entry=entry)
    assert not files.destination.exists() and files.backend.created == 0


@pytest.mark.parametrize("transfer", ["../outside", "{" + str(uuid4()) + "}", str(uuid4()).upper()])
def test_transfer_must_be_canonical_uuid(files, transfer):
    with pytest.raises(ValueError):
        _run(files, transfer_id=transfer)
    assert not files.destination.exists()


@pytest.mark.parametrize("seconds", [0, -1, True, float("nan"), float("inf"), 86401])
def test_monotonic_budget_is_finite_and_bounded(files, seconds):
    with pytest.raises(ValueError):
        _run(files, maximum_seconds=seconds)
    assert not files.destination.exists()


def test_full_retained_import_accepts_24_hour_bound(files):
    _run(files, maximum_seconds=86400)
    assert files.destination.read_bytes() == files.source.read_bytes()


@pytest.mark.parametrize(
    "area,name",
    [
        ("identity", "identity.json.snapshot"),
        ("exports", str(uuid4()) + ".mp4"),
        ("exports", str(uuid4()) + ".json"),
    ],
)
def test_identity_authorities_and_completed_exports_use_fixed_stage_sections(files, area, name):
    renamed = files.source.with_name(name)
    files.source.rename(renamed)
    entry = {**files.entry, "area": area, "relative": name}
    _run(files, source=renamed, entry=entry)
    target = files.backend.root / ".migration" / files.transfer / area / name
    assert target.read_bytes() == renamed.read_bytes()
    assert not (target.parent / "identity.json").exists()


def test_activatable_identity_manifest_and_extra_metadata_are_refused(files):
    for entry in (
        {**files.entry, "area": "identity", "relative": "identity.json"},
        {**files.entry, "source_path": "/untrusted"},
    ):
        with pytest.raises(ValueError):
            _run(files, entry=entry)
    assert files.backend.created == 0


def test_expired_monotonic_budget_refuses_before_first_write(files, monkeypatch):
    times = iter([100.0, 102.0])
    monkeypatch.setattr(copy.time, "monotonic", lambda: next(times))
    with pytest.raises(TimeoutError):
        _run(files, maximum_seconds=1)
    assert not files.destination.exists()


def test_source_hash_mismatch_preserves_original_and_new_unregistered_target(files):
    original = files.source.read_bytes()
    with pytest.raises(ValueError):
        _run(files, entry={**files.entry, "sha256": "0" * 64})
    assert files.source.read_bytes() == original
    assert files.destination.read_bytes() == original


def test_revocation_between_chunks_stops_and_preserves_partial_target(files):
    calls = 0

    def revoke():
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("authorization revoked")

    with pytest.raises(RuntimeError, match="authorization revoked"):
        _run(files, checkpoint=revoke)
    assert 0 < files.destination.stat().st_size < files.entry["size"]
    assert files.source.stat().st_size == files.entry["size"]


def test_last_external_checkpoint_cannot_tamper_source_or_target_undetected(files):
    size = files.entry["size"]

    def tamper():
        if files.destination.exists() and files.destination.stat().st_size == size:
            with files.destination.open("r+b") as stream:
                stream.write(b"TAMPER")

    with pytest.raises(ValueError):
        _run(files, checkpoint=tamper)


def test_checkpoint_source_modification_is_detected_after_copy(files):
    def tamper():
        if files.destination.exists() and files.destination.stat().st_size == files.entry["size"]:
            os.utime(files.source, ns=(1, 1))

    with pytest.raises(ValueError):
        _run(files, checkpoint=tamper)


def test_mutable_ancestor_aborts_copy_and_keeps_original(files):
    def mutate():
        if files.destination.exists():
            files.backend.root.chmod(0o777)

    with pytest.raises(ValueError):
        _run(files, checkpoint=mutate)
    assert files.source.stat().st_size == files.entry["size"]


def test_bounded_stream_detects_short_extra_and_partial_write():
    class ShortWriter:
        def write(self, block):
            return len(block) - 1

    for payload, expected, output in [
        (b"a", 2, None),
        (b"abc", 2, None),
        (b"abc", 3, ShortWriter()),
    ]:
        with pytest.raises(ValueError):
            copy._hash_stream(io.BytesIO(payload), expected, lambda: None, output=output)


def test_bounded_stream_never_requests_or_buffers_entire_large_file():
    class Bounded(io.BytesIO):
        def read(self, size=-1):
            assert 0 < size <= copy._CHUNK
            return super().read(size)

    payload = b"x" * (copy._CHUNK * 3 + 2)
    assert (
        copy._hash_stream(Bounded(payload), len(payload), lambda: None)
        == hashlib.sha256(payload).hexdigest()
    )
    assert copy._hash_stream(Bounded(b""), 0, lambda: None) == hashlib.sha256(b"").hexdigest()


def test_target_policy_accepts_only_private_service_chain_and_inherited_descendants():
    _policy()
    _policy(_acl(flags=0x13), protected=False, require_protected=False)
    _policy(_acl(flags=0), directory=False)


def _python_private_acl(*, owner_mask=copy._FILE_ALL):
    return (
        AccessEntry(0, 3, copy._FILE_ALL, copy._SY),
        AccessEntry(0, 3, copy._FILE_ALL, copy._BA),
        AccessEntry(0, 3, owner_mask, _OWNER_RIGHTS),
    )


def test_service_owned_python_0700_owner_rights_are_effective_service_full_control():
    # CPython3.12.10 os.mkdir(0700) emits SY/BA/OW, not a literal service ACE.
    copy._target_policy(
        _SERVICE,
        _python_private_acl(),
        _SERVICE,
        protected=True,
        require_protected=True,
        directory=True,
    )


@pytest.mark.parametrize("owner", [_FOREIGN, copy._BA, copy._SY])
def test_owner_rights_never_admit_foreign_owner_or_promote_another_owner_to_service(owner):
    with pytest.raises(ValueError):
        copy._target_policy(
            owner,
            _python_private_acl(),
            _SERVICE,
            protected=True,
            require_protected=True,
            directory=True,
        )


def test_owner_rights_do_not_admit_foreign_write_widening():
    with pytest.raises(ValueError):
        copy._target_policy(
            _SERVICE,
            _python_private_acl() + (AccessEntry(0, 3, 0x40000000, _FOREIGN),),
            _SERVICE,
            protected=True,
            require_protected=True,
            directory=True,
        )


def test_owner_rights_still_require_effective_service_full_control():
    with pytest.raises(ValueError):
        copy._target_policy(
            _SERVICE,
            _python_private_acl(owner_mask=0x120089),
            _SERVICE,
            protected=True,
            require_protected=True,
            directory=True,
        )


@pytest.mark.parametrize(
    "entries,changes",
    [
        (_acl() + (AccessEntry(0, 0, 1, _FOREIGN),), {}),
        (_acl() + (AccessEntry(1, 0, 1, _SERVICE),), {}),
        (_acl(flags=0), {}),
        (_acl(flags=0x0B), {}),
        ((AccessEntry(0, 3, 1, _SERVICE),), {}),
        (_acl(), {"protected": False}),
        (_acl(), {"protected": 1}),
    ],
)
def test_target_policy_rejects_foreign_aces_missing_full_access_or_mutable_root(entries, changes):
    with pytest.raises(ValueError):
        _policy(entries, **changes)


def test_native_adapter_ancestor_policy_rejects_foreign_namespace_mutation(private_profile):
    stage = copy._WindowsStage.__new__(copy._WindowsStage)
    stage.root = private_profile / "ProgramData" / "K5Vision"
    stage.program_data = stage.root.parent
    stage.service = _SERVICE
    stage.native = SimpleNamespace(trusted_installer=lambda: False)
    stage._descriptor = lambda _path: (
        copy._BA,
        _acl() + (AccessEntry(0, 0, 0x10000, _FOREIGN),),
        True,
    )
    with pytest.raises(WindowsIdentitySecurityError):
        stage._admit_directory(private_profile)


def test_native_adapter_rejects_reparse_directory_without_admitting_destination(private_profile):
    stage = copy._WindowsStage.__new__(copy._WindowsStage)
    link = private_profile / "junction"
    link.symlink_to(private_profile, target_is_directory=True)
    with pytest.raises(ValueError):
        stage._admit_directory(link)


def test_native_pin_closes_handle_even_when_identity_query_fails(files, monkeypatch):
    stage = copy._WindowsStage.__new__(copy._WindowsStage)
    stage.root = files.backend.root
    stage._pins = []
    closed = []
    stage.kernel = SimpleNamespace(CloseHandle=lambda handle: closed.append(handle) or 1)
    stage._admit_directory = lambda _path: None
    stage._open_handle = lambda *_args, **_kwargs: 42

    def failed_identity(*_args, **_kwargs):
        raise ValueError

    stage._handle_key = failed_identity
    monkeypatch.setattr(copy, "check_windows_volume", lambda _path: None)
    with pytest.raises(ValueError), stage.pin(files.transfer, "identity", ["users.sqlite3"]):
        pytest.fail("Invalid handle identity must refuse before destination exposure")
    assert closed == [42] and stage._pins == []


def test_public_entry_requires_native_elevated_owner_and_hides_private_failure(monkeypatch):
    def rejected():
        raise RuntimeError("PRIVATE source credentials and paths")

    monkeypatch.setattr(copy, "_WindowsStage", rejected)
    with pytest.raises(copy.MigrationCopyError) as error:
        copy.copy_to_service_stage(Path("PRIVATE"), {}, str(uuid4()))
    assert str(error.value) == "Legacy migration copy is unavailable"
    assert error.value.__context__ is None and error.value.__cause__ is None
