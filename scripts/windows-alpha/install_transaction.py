"""Recoverable, per-user Alpha upgrades; no runtime process is ever terminated.

Only the fixed installer-owned paths below participate. The candidate is verified
before activation. Virtual environments are rebuilt at their final path (Python
venvs and their console entry points are not relocatable).
"""

from __future__ import annotations

import argparse
import contextlib
import ensurepip
import hashlib
import importlib.util
import itertools
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import sysconfig
import zipfile
import zlib
from collections.abc import Callable, Iterator
from email.parser import BytesParser
from pathlib import Path

FILES = (
    "Test-K5VisionAlpha.ps1",
    "Start-K5VisionAlpha.ps1",
    "Run-K5VisionAlpha.ps1",
    "gstreamer-version.txt",
    "k5-revision.txt",
)
MANAGED = (".venv", *FILES, "analytics-models", "analytics-config.json")
WORKSPACE = ".k5-alpha-upgrade"
LOCK = ".k5-alpha-install.lock"
FORMAT = "k5-alpha-upgrade-v1"
WHEELHOUSE_FORMAT = "k5-alpha-wheelhouse-v1"
# Core METADATA includes a package's long description. The pinned Pydantic
# 2.13.5 member is 110,178 bytes; WHEEL has no such description payload.
# Both declared sizes and actual reads remain independently bounded.
MAX_CORE_METADATA_BYTES = 128 * 1024
MAX_WHEEL_METADATA_BYTES = 64 * 1024
REQUIREMENTS_SHA256 = "1043752619f04dcfa6b58219936f1dd2c9be5497e52598ab9ff5fde026765efd"
PAYLOAD_FILES = (
    "scripts/windows-alpha/Install-K5VisionAlpha.ps1",
    "scripts/windows-alpha/install_transaction.py",
    "scripts/windows-alpha/Test-K5VisionAlpha.ps1",
    "scripts/windows-alpha/Start-K5VisionAlpha.ps1",
    "scripts/windows-alpha/Run-K5VisionAlpha.ps1",
    "scripts/windows-alpha/runtime-requirements.txt",
    "scripts/provision-stage03-gstreamer.ps1",
)
RUNTIME_PROBE = (
    "from k5vision.operator_runtime import LOCAL_TEST_SOURCE_ENV; "
    "raise SystemExit(0 if LOCAL_TEST_SOURCE_ENV == 'K5_LOCAL_TEST_RTSP_SOURCE' else 1)"
)


def _plain(path: Path) -> None:
    """Do not traverse symlinks/junctions, including dangling ones."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise RuntimeError(f"Installer path is a link or reparse point: {path}")


def _plain_ancestors(path: Path) -> None:
    for entry in (path, *path.parents):
        _plain(entry)


def _remove_owned(path: Path) -> None:
    _plain(path)
    if path.is_dir():
        # Do not follow links inside a partially created/externally modified tree.
        for directory, dirs, files in os.walk(path, followlinks=False):
            for name in (*dirs, *files):
                _plain(Path(directory) / name)
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


@contextlib.contextmanager
def install_lock(root: Path) -> Iterator[None]:
    _plain_ancestors(root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / LOCK
    _plain(path)
    with path.open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("Another K5 Alpha installation is in progress.") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _ps_literal(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


# Diagnostic values are deliberately independent of artifact paths and metadata.
# Consumers must use this fixed vocabulary, never str/repr of an underlying error.
OFFLINE_ADMISSION_SCALAR_MAX = 2**31 - 1
OFFLINE_ADMISSION_CONTRACTS = frozenset(
    {
        "admission",
        "path-absolute",
        "path-local",
        "path-components",
        "path-link",
        "path-access",
        "file-present",
        "file-read",
        "file-record-fields",
        "file-size-limit",
        "file-hash-format",
        "file-hash",
        "file-size",
        "requirements-target",
        "requirements-present",
        "requirements-size",
        "requirements-read",
        "requirements-hash",
        "requirements-syntax",
        "requirements-duplicate",
        "requirements-count",
        "requirements-target-count",
        "manifest-digest-format",
        "manifest-directory",
        "manifest-present",
        "manifest-size",
        "manifest-read",
        "manifest-digest",
        "manifest-json",
        "manifest-duplicate-key",
        "manifest-fields",
        "manifest-schema",
        "installer-revision",
        "runtime-revision",
        "host-base-prefix",
        "host-no-venv",
        "host-bootstrap",
        "host-identity",
        "host-implementation",
        "host-platform",
        "host-version",
        "installer-payload",
        "wheel-count",
        "wheel-record-fields",
        "wheel-name",
        "wheel-filename",
        "wheel-duplicate-filename",
        "wheel-version",
        "wheel-pip-hash",
        "wheel-inventory",
        "wheel-filename-structure",
        "wheel-filename-name",
        "wheel-filename-name-match",
        "wheel-filename-version",
        "wheel-filename-build",
        "wheel-tags-record",
        "wheel-tags-match",
        "wheel-tags-supported",
        "tag-length",
        "tag-format",
        "tag-components",
        "tag-expansion",
        "archive-open",
        "archive-read",
        "archive-member-count",
        "archive-duplicate-member",
        "archive-expanded-size",
        "archive-member-path",
        "archive-member-component",
        "archive-member-collision",
        "archive-member-symlink",
        "archive-member-encrypted",
        "archive-foreign-metadata",
        "archive-file-directory-collision",
        "metadata-present",
        "metadata-size",
        "wheel-metadata-present",
        "wheel-metadata-size",
        "metadata-name-format",
        "metadata-name",
        "metadata-version",
        "wheel-metadata-version",
        "wheel-metadata-tag-count",
        "wheel-metadata-tags",
        "runtime-member-scope",
        "runtime-payload-fields",
        "runtime-payload-members",
        "runtime-file-record",
        "runtime-file-size",
        "runtime-file-hash",
        "offline-arguments",
        "offline-path-overlap",
    }
)


class OfflineAdmissionError(RuntimeError):
    """A refusal with a fixed contract and optional bounded non-identifying scalars.

    ``contract`` belongs to OFFLINE_ADMISSION_CONTRACTS. ``expected`` and
    ``observed`` are None, exact bools, or exact ints from zero through
    OFFLINE_ADMISSION_SCALAR_MAX. Unrecognized diagnostic values are discarded;
    the refusal itself and the historical RuntimeError message are unchanged.
    """

    def __init__(
        self,
        contract: str = "admission",
        *,
        expected: bool | int | None = None,
        observed: bool | int | None = None,
    ) -> None:
        super().__init__("Offline wheelhouse admission failed; no online fallback is permitted.")
        valid = type(contract) is str and contract in OFFLINE_ADMISSION_CONTRACTS
        self.contract = contract if valid else "admission"
        self.expected = self._scalar(expected) if valid else None
        self.observed = self._scalar(observed) if valid else None

    @staticmethod
    def _scalar(value: object) -> bool | int | None:
        if type(value) is bool or (
            type(value) is int and 0 <= value <= OFFLINE_ADMISSION_SCALAR_MAX
        ):
            return value
        return None


def _admit(
    condition: bool,
    contract: str = "admission",
    *,
    expected: bool | int | None = None,
    observed: bool | int | None = None,
) -> None:
    if not condition:
        raise OfflineAdmissionError(contract, expected=expected, observed=observed)


def _offline_plain_ancestors(path: Path) -> None:
    try:
        _plain_ancestors(path)
    except RuntimeError:
        raise OfflineAdmissionError("path-link") from None
    except OSError:
        raise OfflineAdmissionError("path-access") from None


def _sha256(path: Path) -> str:
    _offline_plain_ancestors(path)
    _admit(path.is_file(), "file-present")
    try:
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError:
        raise OfflineAdmissionError("file-read") from None


def runtime_versions(path: Path, *, target_platform: str = "win32") -> dict[str, str]:
    """Return the hash-bound pins for an explicit target, never the build host.

    Only the literal reviewed pyreadline3 Windows marker is supported. This is
    not a requirements resolver or a general-purpose marker interpreter.
    """
    _admit(target_platform in ("win32", "linux", "darwin"), "requirements-target")
    _offline_plain_ancestors(path)
    _admit(path.is_file(), "requirements-present")
    try:
        size = path.stat().st_size
        _admit(size <= 16384, "requirements-size", expected=16384, observed=size)
        raw = path.read_bytes()
    except OSError:
        raise OfflineAdmissionError("requirements-read") from None
    _admit(hashlib.sha256(raw).hexdigest() == REQUIREMENTS_SHA256, "requirements-hash")
    versions = {}
    seen = set()
    for line in raw.decode("utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        windows_only = line == 'pyreadline3==3.5.6; sys_platform == "win32"'
        if windows_only:
            line = "pyreadline3==3.5.6"
        match = re.fullmatch(r"([a-z0-9-]+)==([0-9]+(?:\.[0-9]+)+)", line)
        _admit(match is not None, "requirements-syntax")
        _admit(match[1] not in seen, "requirements-duplicate")
        seen.add(match[1])
        if not windows_only or target_platform == "win32":
            versions[match[1]] = match[2]
    _admit(len(seen) == 28, "requirements-count", expected=28, observed=len(seen))
    expected = 28 if target_platform == "win32" else 27
    _admit(
        len(versions) == expected,
        "requirements-target-count",
        expected=expected,
        observed=len(versions),
    )
    return versions


def _unique_json(items: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in items:
        _admit(key not in result, "manifest-duplicate-key")
        result[key] = value
    return result


def _file_record(path: Path, record: dict) -> None:
    _admit(isinstance(record, dict) and record.keys() == {"size", "sha256"}, "file-record-fields")
    _admit(
        type(record["size"]) is int and 0 < record["size"] <= 256 * 1024 * 1024,
        "file-size-limit",
    )
    _admit(
        isinstance(record["sha256"], str) and bool(re.fullmatch("[0-9a-f]{64}", record["sha256"])),
        "file-hash-format",
    )
    _admit(_sha256(path) == record["sha256"], "file-hash")
    try:
        size = path.stat().st_size
    except OSError:
        raise OfflineAdmissionError("file-read") from None
    _admit(size == record["size"], "file-size", expected=record["size"], observed=size)


def _host_identity() -> dict:
    _admit(sys.prefix == sys.base_prefix, "host-base-prefix")
    # -S on Python 3.12 can mask a venv prefix; do not admit its executable.
    executable = Path(sys.executable)
    _admit(
        not any(
            (parent / "pyvenv.cfg").exists()
            for parent in (executable.parent, executable.parent.parent)
        ),
        "host-no-venv",
    )
    try:
        version = ensurepip.version()
        bundled = Path(ensurepip.__file__).parent / "_bundled" / f"pip-{version}-py3-none-any.whl"
    except (OSError, ValueError):
        raise OfflineAdmissionError("host-bootstrap") from None
    return {
        "implementation": sys.implementation.name,
        "version": ".".join(map(str, sys.version_info[:3])),
        "platform": sysconfig.get_platform().replace("-", "_"),
        "executable_sha256": _sha256(Path(sys.executable)),
        "ensurepip_version": version,
        "ensurepip_wheel_sha256": _sha256(bundled),
    }


def _tags(value: str) -> set[str]:
    _admit(isinstance(value, str), "tag-format")
    _admit(len(value) <= 128, "tag-length", expected=128, observed=len(value))
    parts = value.split("-")
    _admit(
        len(parts) == 3 and all(re.fullmatch(r"[a-z0-9_]+(?:\.[a-z0-9_]+)*", p) for p in parts),
        "tag-format",
    )
    tokens = [part.split(".") for part in parts]
    _admit(
        all(len(group) <= 8 and len(group) == len(set(group)) for group in tokens),
        "tag-components",
    )
    count = len(tokens[0]) * len(tokens[1]) * len(tokens[2])
    _admit(count <= 64, "tag-expansion", expected=64, observed=count)
    return {"-".join(tag) for tag in itertools.product(*tokens)}


def _windows_component(part: str) -> bool:
    return (
        bool(part)
        and part not in (".", "..")
        and not part.endswith((".", " "))
        and not any(ord(character) < 32 or character in '<>:"\\|?*' for character in part)
        and not re.fullmatch(r"(?i)(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])", part.split(".")[0])
    )


def _offline_path(path: Path) -> None:
    _admit(path.is_absolute(), "path-absolute")
    _admit(not str(path).startswith(("\\\\", "//")), "path-local")
    _admit(
        all(_windows_component(part) for part in path.parts if part != path.anchor),
        "path-components",
    )
    _offline_plain_ancestors(path)


@contextlib.contextmanager
def _offline_archive(path: Path) -> Iterator[zipfile.ZipFile]:
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, UnicodeError, zipfile.BadZipFile, zipfile.LargeZipFile):
        raise OfflineAdmissionError("archive-open") from None
    try:
        with archive:
            yield archive
    except OfflineAdmissionError:
        raise
    except (OSError, ValueError, EOFError, RuntimeError, zipfile.BadZipFile, zlib.error):
        raise OfflineAdmissionError("archive-read") from None


class OfflineWheelhouse:
    """An external reviewed digest is the trust anchor, never a self-declared hash.

    Admission executes no wheel code, installer or resolver. Runtime dependency
    closure is fixed to the reviewed requirements; real pip check and installed
    version verification run only in the disposable stage before activation.
    """

    def __init__(self, directory: Path, manifest: Path, digest: str, source: Path, revision: str):
        _admit(bool(re.fullmatch("[0-9a-f]{64}", digest)), "manifest-digest-format")
        _offline_path(directory)
        _offline_path(manifest)
        _admit(directory.is_dir(), "manifest-directory")
        _admit(manifest.is_file(), "manifest-present")
        try:
            size = manifest.stat().st_size
            _admit(size <= 1024 * 1024, "manifest-size", expected=1024 * 1024, observed=size)
            raw = manifest.read_bytes()
        except OSError:
            raise OfflineAdmissionError("manifest-read") from None
        _admit(hashlib.sha256(raw).hexdigest() == digest, "manifest-digest")
        try:
            data = json.loads(raw, object_pairs_hook=_unique_json)
        except ValueError:
            raise OfflineAdmissionError("manifest-json") from None
        _admit(
            isinstance(data, dict)
            and data.keys()
            == {
                "schema_version",
                "installer_revision",
                "runtime_revision",
                "python",
                "installer_payload",
                "runtime_payload",
                "wheels",
            },
            "manifest-fields",
        )
        _admit(data["schema_version"] == WHEELHOUSE_FORMAT, "manifest-schema")
        _admit(
            isinstance(data["installer_revision"], str)
            and bool(re.fullmatch("[0-9a-f]{40}", data["installer_revision"])),
            "installer-revision",
        )
        _admit(data["runtime_revision"] == revision, "runtime-revision")
        host = _host_identity()
        _admit(data["python"] == host, "host-identity")
        _admit(host["implementation"] == "cpython", "host-implementation")
        _admit(host["platform"] == "win_amd64", "host-platform")
        _admit(host["version"].startswith("3.12."), "host-version")
        self.directory, self.source, self.data = directory, source, data
        self.verify_source()
        versions = runtime_versions(source / "runtime-requirements.txt", target_platform="win32")
        versions.update({"k5-vision": "0.1.0", "pip": host["ensurepip_version"]})
        self.versions = versions
        wheels = data["wheels"]
        _admit(isinstance(wheels, list), "wheel-count")
        _admit(
            len(wheels) == len(versions),
            "wheel-count",
            expected=len(versions),
            observed=len(wheels),
        )
        self.wheels = {}
        names = set()
        for record in wheels:
            _admit(
                isinstance(record, dict)
                and record.keys()
                == {
                    "filename",
                    "name",
                    "version",
                    "tags",
                    "size",
                    "sha256",
                },
                "wheel-record-fields",
            )
            name, filename = record["name"], record["filename"]
            _admit(isinstance(name, str) and name in versions and name not in names, "wheel-name")
            _admit(
                isinstance(filename, str)
                and bool(re.fullmatch(r"[A-Za-z0-9_.+!-]+\.whl", filename)),
                "wheel-filename",
            )
            _admit(filename not in self.wheels, "wheel-duplicate-filename")
            _admit(record["version"] == versions[name], "wheel-version")
            self.wheels[filename] = record
            names.add(name)
            if name == "pip":
                _admit(record["sha256"] == host["ensurepip_wheel_sha256"], "wheel-pip-hash")
        self.verify_wheels(directory)

    def verify_source(self) -> None:
        payload = self.data["installer_payload"]
        _admit(
            isinstance(payload, dict) and payload.keys() == set(PAYLOAD_FILES), "installer-payload"
        )
        for name, record in payload.items():
            _file_record(self.source.parent.parent / name, record)

    def verify_scripts(self, destination: Path) -> None:
        for name in FILES[:3]:
            _file_record(
                destination / name, self.data["installer_payload"]["scripts/windows-alpha/" + name]
            )

    def verify_wheels(self, directory: Path) -> None:
        _offline_plain_ancestors(directory)
        _admit({path.name for path in directory.iterdir()} == self.wheels.keys(), "wheel-inventory")
        for filename, record in self.wheels.items():
            path = directory / filename
            _file_record(path, {key: record[key] for key in ("size", "sha256")})
            self.verify_metadata(path, record)

    def verify_metadata(self, path: Path, record: dict) -> tuple[bytes, bytes]:
        """Admit the archive and return the exact bounded METADATA/WHEEL bytes."""
        parts = path.name[:-4].split("-")
        _admit(len(parts) in (5, 6), "wheel-filename-structure")
        _admit(bool(re.fullmatch(r"[A-Za-z0-9_]+", parts[0])), "wheel-filename-name")
        _admit(
            re.sub(r"[-_.]+", "-", parts[0]).lower() == record["name"],
            "wheel-filename-name-match",
        )
        _admit(parts[1] == record["version"], "wheel-filename-version")
        if len(parts) == 6:
            _admit(bool(re.fullmatch(r"[0-9][A-Za-z0-9_]*", parts[2])), "wheel-filename-build")
        tags = _tags("-".join(parts[-3:]))
        _admit(
            isinstance(record["tags"], list)
            and all(isinstance(tag, str) for tag in record["tags"]),
            "wheel-tags-record",
        )
        _admit(
            len(record["tags"]) == len(set(record["tags"])) and set(record["tags"]) == tags,
            "wheel-tags-match",
        )
        supported = {
            f"{python}-{abi}-{platform}"
            for python, abi, platform in (
                ("py3", "none", "any"),
                ("py312", "none", "any"),
                ("cp312", "none", "any"),
                ("py3", "none", "win_amd64"),
                ("py312", "none", "win_amd64"),
                ("cp312", "none", "win_amd64"),
                ("cp312", "cp312", "win_amd64"),
            )
        }
        supported.update(f"cp3{minor}-abi3-win_amd64" for minor in range(2, 13))
        _admit(bool(tags & supported), "wheel-tags-supported")
        prefix = f"{parts[0]}-{parts[1]}.dist-info/"
        with _offline_archive(path) as archive:
            entries = archive.infolist()
            _admit(
                len(entries) <= 10000,
                "archive-member-count",
                expected=10000,
                observed=len(entries),
            )
            _admit(
                len({item.filename for item in entries}) == len(entries),
                "archive-duplicate-member",
            )
            size = sum(item.file_size for item in entries)
            _admit(
                size <= 512 * 1024 * 1024,
                "archive-expanded-size",
                expected=512 * 1024 * 1024,
                observed=size,
            )
            canonical = {}
            for item in entries:
                name = item.filename
                _admit(item.orig_filename == name, "archive-member-path")
                _admit(
                    not name.startswith("/") and "\\" not in name and ":" not in name,
                    "archive-member-path",
                )
                parts = name.rstrip("/").split("/")
                _admit(all(_windows_component(part) for part in parts), "archive-member-component")
                key = "/".join(parts).casefold()
                _admit(key not in canonical, "archive-member-collision")
                canonical[key] = item.is_dir()
                _admit(not stat.S_ISLNK(item.external_attr >> 16), "archive-member-symlink")
                _admit(not item.flag_bits & 1, "archive-member-encrypted")
                if ".dist-info/" in name.casefold():
                    _admit(name.startswith(prefix), "archive-foreign-metadata")
            for key in canonical:
                parts = key.split("/")
                for index in range(1, len(parts)):
                    _admit(
                        canonical.get("/".join(parts[:index]), True),
                        "archive-file-directory-collision",
                    )
            metadata, raw_metadata = [], []
            for name, missing, contract, limit in (
                ("METADATA", "metadata-present", "metadata-size", MAX_CORE_METADATA_BYTES),
                (
                    "WHEEL",
                    "wheel-metadata-present",
                    "wheel-metadata-size",
                    MAX_WHEEL_METADATA_BYTES,
                ),
            ):
                try:
                    info = archive.getinfo(prefix + name)
                except KeyError:
                    raise OfflineAdmissionError(missing) from None
                _admit(info.file_size <= limit, contract, expected=limit, observed=info.file_size)
                with archive.open(info) as stream:
                    raw = stream.read(limit + 1)
                _admit(len(raw) <= limit, contract, expected=limit, observed=len(raw))
                _admit(len(raw) == info.file_size, "archive-read")
                raw_metadata.append(raw)
                metadata.append(BytesParser().parsebytes(raw))
            package, wheel = metadata
            names = package.get_all("Name", [])
            _admit(
                len(names) == 1
                and isinstance(names[0], str)
                and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", names[0])),
                "metadata-name-format",
            )
            _admit(re.sub(r"[-_.]+", "-", names[0]).lower() == record["name"], "metadata-name")
            _admit(package.get_all("Version") == [record["version"]], "metadata-version")
            _admit(wheel.get_all("Wheel-Version") == ["1.0"], "wheel-metadata-version")
            declared = wheel.get_all("Tag", [])
            _admit(
                0 < len(declared) <= 64,
                "wheel-metadata-tag-count",
                expected=64,
                observed=len(declared),
            )
            _admit(set().union(*(_tags(tag) for tag in declared)) == tags, "wheel-metadata-tags")
            if record["name"] == "k5-vision":
                _admit(
                    all(item.filename.startswith(("k5vision/", prefix)) for item in entries),
                    "runtime-member-scope",
                )
                payload = self.data["runtime_payload"]
                _admit(
                    isinstance(payload, dict) and 0 < len(payload) <= 1024, "runtime-payload-fields"
                )
                actual = {
                    item.filename
                    for item in entries
                    if item.filename.startswith("k5vision/") and not item.is_dir()
                }
                _admit(
                    actual == payload.keys() and "k5vision/cli.py" in actual,
                    "runtime-payload-members",
                )
                for name, expected in payload.items():
                    _admit(
                        isinstance(expected, dict) and expected.keys() == {"size", "sha256"},
                        "runtime-file-record",
                    )
                    content = archive.read(name)
                    _admit(
                        type(expected["size"]) is int and len(content) == expected["size"],
                        "runtime-file-size",
                    )
                    _admit(
                        hashlib.sha256(content).hexdigest() == expected["sha256"],
                        "runtime-file-hash",
                    )
            return raw_metadata[0], raw_metadata[1]

    def copy_to(self, destination: Path) -> None:
        self.verify_source()
        self.verify_wheels(self.directory)
        destination.mkdir()
        for filename in self.wheels:
            shutil.copyfile(self.directory / filename, destination / filename)
        self.verify_wheels(destination)


class Installer:
    def __init__(
        self,
        root: Path,
        source: Path,
        host: Path,
        revision: str,
        gstreamer: str,
        shortcut: Path | None = None,
        *,
        run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        wheelhouse: Path | None = None,
        wheelhouse_manifest: Path | None = None,
        wheelhouse_manifest_sha256: str | None = None,
    ) -> None:
        self.root = root.absolute()
        self.source = source.absolute()
        self.host = host
        self.revision = revision.lower()
        self.gstreamer = gstreamer
        self.shortcut = shortcut.absolute() if shortcut else None
        self.run = run
        # The graphical installer requires a verified pinned engineering payload.
        # The standalone source installer remains backwards-compatible.
        candidate = self.source.parent / "analytics"
        self.analytics_bundle = candidate if candidate.exists() else None
        self.analytics_module = None
        if self.analytics_bundle is not None:
            helper = self.source / "owner_analytics_bundle.py"
            spec = importlib.util.spec_from_file_location("_k5_owner_analytics", helper)
            if spec is None or spec.loader is None:
                raise RuntimeError("Owner analytics admission module is missing.")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.validate(self.analytics_bundle)
            self.analytics_module = module
        self.work = self.root / WORKSPACE
        self.stage = self.work / "candidate"
        self.backup = self.work / "backup"
        self.wheels = self.work / "wheels"
        self.journal = self.work / "transaction.json"
        supplied = (wheelhouse, wheelhouse_manifest, wheelhouse_manifest_sha256)
        _admit(
            all(value is None for value in supplied)
            or all(value is not None for value in supplied),
            "offline-arguments",
        )
        if wheelhouse is not None:
            _offline_path(self.root)
            _offline_path(self.source)
        self.offline = (
            OfflineWheelhouse(
                wheelhouse,
                wheelhouse_manifest,
                wheelhouse_manifest_sha256,
                self.source,
                self.revision,
            )
            if wheelhouse is not None
            else None
        )
        if self.offline is not None:
            for path in (wheelhouse, wheelhouse_manifest, self.source):
                _admit(
                    not path.is_relative_to(self.root) and not self.root.is_relative_to(path),
                    "offline-path-overlap",
                )

    def command(self, args: list[str | Path], *, capture: bool = False) -> str:
        # Ambient pip/Python overrides must not redirect writes out of the
        # transaction or import a checkout instead of the installed candidate.
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.upper().startswith("PIP_")
            and key.upper() not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")
        }
        environment["PIP_CONFIG_FILE"] = os.devnull
        if self.offline is not None:
            environment.update(
                PIP_NO_INDEX="1", PIP_DISABLE_PIP_VERSION_CHECK="1", PIP_NO_CACHE_DIR="1"
            )
        result = self.run(
            [str(arg) for arg in args],
            check=True,
            timeout=900,
            text=True,
            encoding="utf-8" if capture else None,
            env=environment,
            stdout=subprocess.PIPE if capture else None,
        )
        return result.stdout or ""

    def powershell(self, command: str, *, capture: bool = False) -> str:
        if capture:
            command = (
                "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); " + command
            )
        return self.command(
            [self.host, "-NoProfile", "-NonInteractive", "-Command", command], capture=capture
        )

    def assert_idle(self) -> None:
        if not (self.root / ".venv").exists() and not (self.backup / ".venv").exists():
            return
        # An inaccessible executable path means ownership cannot be established.
        # Refuse the upgrade; never infer ownership or kill an arbitrary PID.
        output = self.powershell(
            "$ErrorActionPreference = 'Stop'; "
            "@(Get-CimInstance Win32_Process "
            "-Filter \"Name = 'python.exe' OR Name = 'pythonw.exe'\" "
            "-ErrorAction Stop | Select-Object ProcessId, ExecutablePath) | "
            "ConvertTo-Json -Compress",
            capture=True,
        )
        processes = json.loads(output) if output.strip() else []
        if isinstance(processes, dict):
            processes = [processes]
        if not isinstance(processes, list):
            raise RuntimeError("Could not verify K5 runtime process ownership.")
        executables = {
            os.path.normcase(os.path.abspath(venv / "Scripts" / exe))
            for venv in (self.root / ".venv", self.backup / ".venv")
            for exe in ("python.exe", "pythonw.exe")
        }
        for process in processes:
            executable = process.get("ExecutablePath") if isinstance(process, dict) else None
            if not isinstance(executable, str) or not executable.strip():
                raise RuntimeError(
                    "Cannot identify a Python process. Close it and retry the upgrade."
                )
            if os.path.normcase(os.path.abspath(executable)) in executables:
                raise RuntimeError("K5 Alpha is running. Close its windows and retry the upgrade.")

    def save(self, state: dict) -> None:
        _plain(self.journal)
        temporary = self.work / "transaction.tmp"
        _plain(temporary)
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(state, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.journal)

    def targets(self, state: dict) -> dict[str, Path]:
        targets = {name: self.root / name for name in MANAGED}
        shortcut = state.get("shortcut")
        if shortcut is not None:
            if not isinstance(shortcut, str) or self.shortcut is None:
                raise RuntimeError("Upgrade recovery requires the original desktop shortcut path.")
            if Path(shortcut) != self.shortcut:
                raise RuntimeError("Upgrade recovery shortcut path does not match this desktop.")
            targets["desktop-shortcut.lnk"] = self.shortcut
        return targets

    def recover(self) -> None:
        _plain(self.work)
        if not self.work.exists():
            return
        _plain(self.journal)
        if not self.journal.is_file():
            # An interruption between the final journal unlink and rmdir (or
            # before the first journal write) can leave an empty directory only.
            if self.work.is_dir():
                entries = list(self.work.iterdir())
                temporary = self.work / "transaction.tmp"
                if entries == [temporary]:
                    # Only first-journal construction can leave this exact
                    # residue: backup/staging do not exist until save returns.
                    # It may contain a partial JSON write; never adopt a link.
                    _plain(temporary)
                    if temporary.is_file():
                        temporary.unlink()
                        entries = []
                if not entries:
                    self.work.rmdir()
                    return
            raise RuntimeError(f"Unrecognized upgrade workspace; left unchanged: {self.work}")
        state = json.loads(self.journal.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or state.get("format") != FORMAT:
            raise RuntimeError("Unrecognized upgrade journal; no installed files were changed.")
        phase = state.get("phase")
        targets = self.targets(state)
        if phase not in ("preparing", "activating", "committed"):
            raise RuntimeError("Invalid upgrade phase; no installed files were changed.")
        if phase == "activating":
            originals = state.get("originals")
            if (
                not isinstance(originals, dict)
                or originals.keys() != targets.keys()
                or any(type(value) is not bool for value in originals.values())
            ):
                raise RuntimeError("Invalid upgrade recovery manifest; installed files unchanged.")
            self.assert_idle()
            for name, target in reversed(list(targets.items())):
                _plain_ancestors(target)
                backup = self.backup / name
                _plain_ancestors(backup)
                if backup.exists():
                    if name == "desktop-shortcut.lnk":
                        # Desktop may be on a different filesystem. Keep the
                        # backup until all recovery/cleanup succeeds, so a
                        # partial copy can be retried without losing old bytes.
                        shutil.copy2(backup, target)
                    else:
                        _remove_owned(target)
                        backup.rename(target)
                elif not originals[name]:
                    _remove_owned(target)
                elif not target.exists():
                    raise RuntimeError(f"Original installer path is missing: {target}")
        elif phase == "committed":
            self.assert_idle()
        # The journal must outlive every potentially locked backup/staging
        # entry. Recursive removal of work itself could delete it first.
        for child in self.work.iterdir():
            if child != self.journal:
                _remove_owned(child)
        self.journal.unlink()
        self.work.rmdir()

    def materialize_files(self, destination: Path) -> None:
        if destination == self.root:
            # Activate the exact verified candidate scripts/records, not a second
            # read of source files that could have changed during staging.
            for name in FILES:
                shutil.copyfile(self.stage / name, destination / name)
            if self.offline is not None:
                self.offline.verify_scripts(destination)
            return
        if self.offline is not None:
            self.offline.verify_source()
        for name in FILES[:3]:
            shutil.copyfile(self.source / name, destination / name)
        (destination / "gstreamer-version.txt").write_text(self.gstreamer, encoding="ascii")
        (destination / "k5-revision.txt").write_text(self.revision, encoding="ascii")
        if self.offline is not None:
            self.offline.verify_scripts(destination)

    def prepare_wheels(self) -> None:
        if self.offline is not None:
            self.offline.copy_to(self.wheels)
            return
        bootstrap = self.work / "builder"
        self.command([sys.executable, "-m", "venv", bootstrap])
        python = bootstrap / "Scripts" / "python.exe"
        self.wheels.mkdir()
        self.command(
            [
                python,
                "-m",
                "pip",
                "wheel",
                "--wheel-dir",
                self.wheels,
                "--requirement",
                self.source / "runtime-requirements.txt",
                "pip",
            ]
        )
        package_uri = f"https://github.com/mkurtgerald/K5-Vision/archive/{self.revision}.zip"
        self.command(
            [python, "-m", "pip", "wheel", "--no-deps", "--wheel-dir", self.wheels, package_uri]
        )
        if len(list(self.wheels.glob("k5_vision-*.whl"))) != 1:
            raise RuntimeError("Expected exactly one reviewed K5 wheel.")
        if self.analytics_module is not None:
            approved_wheels, _ = self.analytics_module.validate(self.analytics_bundle)
            for wheel in approved_wheels:
                target = self.wheels / wheel.name
                if target.exists():
                    raise RuntimeError("Analytics wheel collides with K5 runtime.")
                shutil.copyfile(wheel, target)
                with wheel.open("rb") as source, target.open("rb") as installed:
                    if hashlib.file_digest(source, "sha256").digest() != hashlib.file_digest(
                        installed, "sha256"
                    ).digest():
                        raise RuntimeError("Analytics wheel changed while staging.")

    def wheel_hashes(self) -> dict[str, str]:
        if self.offline is not None:
            self.offline.verify_wheels(self.wheels)
        hashes = {}
        for path in sorted(self.wheels.glob("*.whl")):
            _plain(path)
            with path.open("rb") as stream:
                hashes[path.name] = hashlib.file_digest(stream, "sha256").hexdigest()
        return hashes

    def install_runtime(self, destination: Path) -> None:
        if self.offline is not None:
            self.offline.verify_wheels(self.wheels)
        destination.mkdir(exist_ok=True)
        venv = destination / ".venv"
        isolated = ["-I", "-B"] if self.offline is not None else []
        bootstrap = ["-I", "-S", "-B"] if self.offline is not None else []
        self.command([sys.executable, *bootstrap, "-m", "venv", venv])
        python = venv / "Scripts" / "python.exe"
        self.command(
            [
                python,
                *isolated,
                "-m",
                "pip",
                "install",
                "--no-index",
                "--no-deps",
                "--force-reinstall",
                *sorted(self.wheels.glob("*.whl")),
            ]
        )
        self.command([python, *isolated, "-m", "pip", "check"])
        if self.offline is not None:
            # Check the complete installed closure; the resolver is never used.
            expected = repr(self.offline.versions)
            self.command(
                [
                    python,
                    *isolated,
                    "-c",
                    "from importlib.metadata import distributions; import re; "
                    "items=[(re.sub(r'[-_.]+','-',d.metadata['Name']).lower(),d.version) "
                    "for d in distributions()]; "
                    f"expected={expected}; "
                    "assert len(items)==len(expected) and dict(items)==expected, "
                    "'Installed wheel closure mismatch'",
                ]
            )
        self.command([python, *isolated, "-m", "k5vision.cli", "--version"])
        self.command([python, *isolated, "-c", RUNTIME_PROBE])
        if self.analytics_module is not None:
            self.analytics_module.materialize_models(self.analytics_bundle, destination)
            configuration = destination / "analytics-config.json"
            self.command(
                [
                    python,
                    *isolated,
                    "-c",
                    "from k5vision.analytics_config import load_analytics_configuration; "
                    "import sys; "
                    "assert load_analytics_configuration({'K5_ANALYTICS_CONFIG': "
                    "sys.argv[1]}) is not None",
                    configuration,
                ]
            )
        self.materialize_files(destination)

    def preflight(self, destination: Path) -> None:
        self.command(
            [
                self.host,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                destination / "Test-K5VisionAlpha.ps1",
                "-InstallRoot",
                destination,
            ]
        )

    def stage_shortcut(self) -> None:
        arguments = (
            '-NoProfile -ExecutionPolicy Bypass -NoExit -File "'
            + str(self.root / "Start-K5VisionAlpha.ps1")
            + '"'
        )
        self.powershell(
            "$ErrorActionPreference = 'Stop'; "
            "$wsh = New-Object -ComObject WScript.Shell; "
            f"$shortcut = $wsh.CreateShortcut({_ps_literal(self.stage / 'desktop-shortcut.lnk')}); "
            f"$shortcut.TargetPath = {_ps_literal(self.host)}; "
            f"$shortcut.Arguments = {_ps_literal(arguments)}; "
            f"$shortcut.WorkingDirectory = {_ps_literal(self.root)}; "
            "$shortcut.Description = 'K5 Vision Windows Alpha'; $shortcut.Save()"
        )
        if not (self.stage / "desktop-shortcut.lnk").is_file():
            raise RuntimeError("Desktop shortcut staging failed.")

    def install(self, *, skip_shortcut: bool = False) -> None:
        with install_lock(self.root):
            self.recover()
            self.assert_idle()
            self.work.mkdir()
            state = {"format": FORMAT, "phase": "preparing", "shortcut": None}
            self.save(state)
            try:
                self.backup.mkdir()
                # Never change the shared GStreamer runtime during an upgrade.
                # A missing/different requested version fails candidate preflight.
                if self.offline is None and not any(
                    (self.root / name).exists() for name in MANAGED
                ):
                    self.command(
                        [
                            self.host,
                            "-NoProfile",
                            "-NonInteractive",
                            "-ExecutionPolicy",
                            "Bypass",
                            "-File",
                            self.source.parent / "provision-stage03-gstreamer.ps1",
                            "-Version",
                            self.gstreamer,
                        ]
                    )
                self.prepare_wheels()
                hashes = self.wheel_hashes()
                self.install_runtime(self.stage)
                self.preflight(self.stage)
                if not skip_shortcut:
                    if self.shortcut is None:
                        raise RuntimeError("Desktop shortcut path is unavailable.")
                    self.stage_shortcut()
                    state["shortcut"] = str(self.shortcut)
                if not hashes or self.wheel_hashes() != hashes:
                    raise RuntimeError("Staged runtime wheels changed during verification.")
                targets = self.targets(state)
                for target in targets.values():
                    _plain_ancestors(target)
                self.assert_idle()
                state["originals"] = {name: target.exists() for name, target in targets.items()}
                state["phase"] = "activating"
                self.save(state)
                for name, target in targets.items():
                    if state["originals"][name]:
                        if name == "desktop-shortcut.lnk":
                            # Copy to an unadmitted temporary name first: a
                            # failed partial copy is never a valid old backup.
                            temporary = self.backup / "shortcut.tmp"
                            shutil.copy2(target, temporary)
                            temporary.rename(self.backup / name)
                        else:
                            target.rename(self.backup / name)
                # Reinstall exclusively from verified local wheels at the final path.
                self.install_runtime(self.root)
                self.preflight(self.root)
                if not skip_shortcut:
                    shutil.copyfile(self.stage / "desktop-shortcut.lnk", self.shortcut)
                state["phase"] = "committed"
                self.save(state)
            except BaseException as failure:
                try:
                    self.recover()
                except BaseException as recovery_failure:
                    raise RuntimeError(
                        "Upgrade failed and rollback needs recovery. "
                        "Recovery state is retained at "
                        f"{self.work}. Close K5 and rerun this installer; do not delete the "
                        f"upgrade workspace. Recovery error: {recovery_failure}"
                    ) from failure
                raise
            # A cleanup failure must not turn a committed, verified upgrade into rollback.
            try:
                self.recover()
            except Exception as exc:
                print(f"Upgrade committed; retained recovery workspace for later cleanup: {exc}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install-root", required=True, type=Path)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--powershell", required=True, type=Path)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--gstreamer", required=True)
    parser.add_argument("--shortcut", type=Path)
    parser.add_argument("--skip-shortcut", action="store_true")
    parser.add_argument("--wheelhouse", type=Path)
    parser.add_argument("--wheelhouse-manifest", type=Path)
    parser.add_argument("--wheelhouse-manifest-sha256")
    args = parser.parse_args()
    if os.name != "nt" or sys.version_info[:2] != (3, 12):
        parser.error("The Alpha installer requires Windows and Python 3.12.")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", args.revision):
        parser.error("K5 revision must be an exact SHA.")
    if not re.fullmatch(r"1\.28\.\d+", args.gstreamer):
        parser.error("Unreviewed GStreamer version.")
    Installer(
        args.install_root,
        args.source,
        args.powershell,
        args.revision,
        args.gstreamer,
        args.shortcut,
        wheelhouse=args.wheelhouse,
        wheelhouse_manifest=args.wheelhouse_manifest,
        wheelhouse_manifest_sha256=args.wheelhouse_manifest_sha256,
    ).install(skip_shortcut=args.skip_shortcut)
    print(f"K5 Vision Alpha runtime installed from reviewed commit {args.revision.lower()}.")
    print("Camera-free preflight passed; no camera media was contacted or stored.")


if __name__ == "__main__":
    main()
