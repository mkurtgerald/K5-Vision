"""Internal composition of existing durable state outside replaceable application files.

This is a path-only profile, not a camera or credential configuration format. Its
caller supplies the installation root and already-provisioned private state. Missing
state is refused, never initialized, reset, migrated or cleaned up here. Runtime
source custody and continuous-recording acceptance remain separate prerequisites.
"""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Iterator, MutableMapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from k5vision.identity_state import (
    IdentityState,
    IdentityStateError,
    _checked_path,
    _regular_file,
    _windows_admission,
    identity_environment,
    load_identity_state,
)

_DEVICE_ENV = "K5_DEVICE_DB_PATH"
_RECORDING_ENV = "K5_STAGE_ONE_RECORDING_ROOT"
_TRIAL_SOURCE_ENV = (
    "K5_STAGE03_SOURCE",
    "K5_STAGE03_CAM_CRED",
    "K5_PUBLIC_TEST_RTSP_SOURCE",
    "K5_PUBLIC_TEST_SOURCE_IP",
    "K5_LOCAL_TEST_RTSP_SOURCE",
)


class InstalledProfileError(RuntimeError):
    """Sanitized refusal of incomplete or conflicting installed composition."""


@dataclass(frozen=True, slots=True)
class InstalledProfile:
    identity: IdentityState
    device_database: Path
    recording_root: Path


def _path(value: str | Path) -> Path:
    text = str(value)
    if (
        not isinstance(value, (str, Path))
        or not text
        or text != text.strip()
        or len(text) > 4096
        or text.startswith(("//", "\\\\"))
    ):
        raise ValueError
    path = Path(text)
    if any(":" in part for part in path.parts[1:]):
        raise ValueError
    return _checked_path(path)


def _directory(path: Path, *, private: bool) -> None:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError
    if private:
        _windows_admission(path, private=True, directory=True)
        if os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
            raise ValueError


def _overlap(first: Path, second: Path) -> bool:
    return first.is_relative_to(second) or second.is_relative_to(first)


def load_installed_profile(
    *,
    application_root: str | Path,
    identity_directory: str | Path,
    device_database: str | Path,
    recording_root: str | Path,
) -> InstalledProfile:
    """Read-only admission of explicit existing paths; never fall back to defaults.

    The installation root must cover the caller's replaceable app files. The
    current Python environment and package directory are excluded independently.
    Identity is read using its existing durable-site and user-registry validation.
    Device/media contents are recovered by the existing application after admission.
    """
    try:
        application = _path(application_root)
        identity = _path(identity_directory)
        database = _path(device_database)
        recording = _path(recording_root)
        _directory(application, private=False)
        # These roots describe the running installation, rather than untrusted
        # state selections. Resolve their aliases so a real, non-linked state
        # path cannot evade exclusion via a symlinked interpreter/package root.
        # Persistent paths above remain subject to strict no-link admission.
        try:
            roots = (
                application,
                Path(sys.prefix).resolve(strict=True),
                Path(__file__).parent.resolve(strict=True),
            )
        except RuntimeError:
            # Python 3.12 reports trusted-root symlink loops as RuntimeError.
            raise ValueError from None
        if any(
            _overlap(persistent, replaceable)
            for persistent in (identity, database.parent, recording)
            for replaceable in roots
        ):
            raise ValueError
        if (
            _overlap(identity, recording)
            or database.is_relative_to(identity)
            or database.is_relative_to(recording)
        ):
            raise ValueError
        for directory in (identity, database.parent, recording):
            _directory(directory, private=True)
        # Require the enrolled-device file to exist. An absent file on restart
        # must not silently become a fresh empty registry through SQLite create.
        _regular_file(database)
        for suffix in ("-journal", "-wal", "-shm"):
            sidecar = Path(str(database) + suffix)
            try:
                sidecar.lstat()
            except FileNotFoundError:
                continue
            _regular_file(sidecar)
        state = load_identity_state(identity)
        return InstalledProfile(state, database, recording)
    except (OSError, ValueError, TypeError, IdentityStateError):
        raise InstalledProfileError("Installed persistent state admission failed.") from None


def refuse_trial_sources(environment: MutableMapping[str, str]) -> None:
    """Do not turn transient physical/Alpha trial credentials into installed config."""
    if any(environment.get(name) for name in _TRIAL_SOURCE_ENV):
        raise InstalledProfileError("Installed profile conflicts with trial source configuration.")


@contextmanager
def installed_profile_environment(
    profile: InstalledProfile, environment: MutableMapping[str, str]
) -> Iterator[None]:
    """Validate/bind one caller-owned mapping; never supply process-wide environ.

    The installed factory uses a disposable copy only and forwards admitted paths
    explicitly. Restoration of a mapping is not concurrent-factory isolation.
    """
    refuse_trial_sources(environment)
    expected = {
        _DEVICE_ENV: str(profile.device_database),
        _RECORDING_ENV: str(profile.recording_root),
    }
    if any(environment.get(name) not in (None, "", value) for name, value in expected.items()):
        raise InstalledProfileError("Installed profile conflicts with configured persistent state.")
    prior = {name: environment.get(name) for name in expected}
    try:
        with identity_environment(profile.identity, environment):
            environment.update(expected)
            try:
                yield
            finally:
                for name, value in prior.items():
                    if value is None:
                        environment.pop(name, None)
                    else:
                        environment[name] = value
    except IdentityStateError:
        raise InstalledProfileError("Installed identity configuration was refused.") from None
