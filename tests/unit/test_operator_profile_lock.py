from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from k5vision import identity_state
from k5vision.operator_profile_lock import OperatorProfileLock, OperatorProfileLockError


@pytest.fixture
def identity(tmp_path: Path, monkeypatch):
    if os.name == "posix":
        original_lstat = Path.lstat

        def ordinary_system_ancestor(path, *args, **kwargs):
            info = original_lstat(path, *args, **kwargs)
            if path in (Path("/"), Path("/tmp")) and info.st_uid == 65534:
                values = list(info)
                values[4] = 0
                return os.stat_result(values)
            return info

        # Only model the sandbox's synthetic system ancestors; private fixture
        # files, modes, inode identities, links and OS locking stay real.
        monkeypatch.setattr(Path, "lstat", ordinary_system_ancestor)
        parent = tmp_path
    else:
        parent = Path(os.environ["LOCALAPPDATA"]) / f"K5 Profile Lease-{uuid4().hex}"
        identity_state.validate_new_identity_directory(parent)
        parent.mkdir(mode=0o700)
    state = identity_state.initialize_identity_state(
        parent / "identity", username="fixture-admin", password=secrets.token_urlsafe(24)
    )
    try:
        yield state
    finally:
        if os.name == "nt":
            shutil.rmtree(parent)


def test_second_owner_is_refused_and_same_file_can_be_reacquired(identity) -> None:
    first = OperatorProfileLock(identity.directory)
    second = OperatorProfileLock(identity.directory)
    path = identity.directory / "operator.lock"
    try:
        first.acquire()
        original = path.stat()
        with pytest.raises(OperatorProfileLockError) as error:
            second.acquire()
        assert error.value.__context__ is None
        assert error.value.__cause__ is None
        assert str(identity.directory) not in str(error.value)
        first.close()
        first.close()
        second.acquire()
        current = path.stat()
        assert (current.st_dev, current.st_ino) == (original.st_dev, original.st_ino)
    finally:
        first.close()
        second.close()
    assert path.read_bytes() == b"1"
    first.acquire()
    first.close()


def test_close_before_acquire_is_idempotent_and_does_not_create_a_file(identity) -> None:
    lease = OperatorProfileLock(identity.directory)
    lease.close()
    lease.close()
    assert not (identity.directory / "operator.lock").exists()


def test_existing_empty_lock_is_initialized_without_replacing_it(identity) -> None:
    path = identity.directory / "operator.lock"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    before = path.stat()
    lease = OperatorProfileLock(identity.directory)
    try:
        lease.acquire()
    finally:
        lease.close()
    after = path.stat()
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
    assert path.read_bytes() == b"1"


@pytest.mark.parametrize("contents", [b"0", b"private-marker", b"11"])
def test_malformed_existing_lock_is_refused_without_truncation(identity, contents) -> None:
    path = identity.directory / "operator.lock"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.write(descriptor, contents)
    os.close(descriptor)
    lease = OperatorProfileLock(identity.directory)
    with pytest.raises(OperatorProfileLockError) as error:
        lease.acquire()
    lease.close()
    assert error.value.__context__ is None
    assert path.read_bytes() == contents


def test_existing_hard_link_is_refused_without_touching_its_target(identity) -> None:
    target = identity.directory / "other-file"
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.write(descriptor, b"private-marker")
    os.close(descriptor)
    os.link(target, identity.directory / "operator.lock")
    lease = OperatorProfileLock(identity.directory)
    with pytest.raises(OperatorProfileLockError):
        lease.acquire()
    lease.close()
    assert target.read_bytes() == b"private-marker"


def test_existing_symlink_is_refused_without_touching_its_target(identity) -> None:
    target = identity.directory / "other-file"
    target.write_bytes(b"private-marker")
    try:
        (identity.directory / "operator.lock").symlink_to(target)
    except OSError:
        if os.name == "nt":
            pytest.skip("Windows symbolic link creation needs Developer Mode or privilege")
        raise
    lease = OperatorProfileLock(identity.directory)
    with pytest.raises(OperatorProfileLockError):
        lease.acquire()
    lease.close()
    assert target.read_bytes() == b"private-marker"


@pytest.mark.skipif(os.name != "posix", reason="POSIX file-mode admission")
def test_existing_nonprivate_lock_is_refused(identity) -> None:
    path = identity.directory / "operator.lock"
    path.write_bytes(b"1")
    path.chmod(0o644)
    lease = OperatorProfileLock(identity.directory)
    with pytest.raises(OperatorProfileLockError):
        lease.acquire()
    lease.close()
    assert path.read_bytes() == b"1"


def test_incomplete_profile_is_refused_before_creating_lock(identity) -> None:
    (identity.directory / "identity.json").unlink()
    lease = OperatorProfileLock(identity.directory)
    with pytest.raises(OperatorProfileLockError):
        lease.acquire()
    lease.close()
    assert not (identity.directory / "operator.lock").exists()


def test_lock_replacement_between_admission_and_open_is_refused(identity, monkeypatch) -> None:
    path = identity.directory / "operator.lock"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.write(descriptor, b"1")
    os.close(descriptor)
    original_open = os.open
    changed = False

    def replacing_open(value, flags, mode=0o777):
        nonlocal changed
        if Path(value) == path and not flags & os.O_CREAT and not changed:
            changed = True
            path.rename(identity.directory / "previous-lock")
            replacement = original_open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.write(replacement, b"1")
            os.close(replacement)
        return original_open(value, flags, mode)

    monkeypatch.setattr(os, "open", replacing_open)
    lease = OperatorProfileLock(identity.directory)
    with pytest.raises(OperatorProfileLockError):
        lease.acquire()
    lease.close()
    monkeypatch.setattr(os, "open", original_open)
    replacement = OperatorProfileLock(identity.directory)
    try:
        replacement.acquire()
    finally:
        replacement.close()


@pytest.mark.skipif(os.name != "nt", reason="Real Windows one-byte lock across processes")
def test_windows_independent_process_cannot_acquire_held_profile(identity) -> None:
    source = str(Path(__file__).resolve().parents[2] / "src")
    code = (
        "import sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); "
        "from k5vision.operator_profile_lock import OperatorProfileLock,OperatorProfileLockError; "
        "lease=OperatorProfileLock(Path(sys.argv[2])); "
        "\ntry: lease.acquire()\nexcept OperatorProfileLockError: sys.exit(23)\n"
        "else: lease.close(); sys.exit(0)\n"
    )
    arguments = [sys.executable, "-I", "-B", "-c", code, source, str(identity.directory)]
    lease = OperatorProfileLock(identity.directory)
    try:
        lease.acquire()
        result = subprocess.run(arguments, capture_output=True, timeout=15, check=False)
        assert result.returncode == 23
    finally:
        lease.close()
    result = subprocess.run(arguments, capture_output=True, timeout=15, check=False)
    assert result.returncode == 0
