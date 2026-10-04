"""Owned, bounded local retention of already-admitted qualification wheels.

There is no package, network, installation, ACL-changing, or age-purge code here.
The caller must independently admit the qualification receipts and wheel contents.
COMPLETE proves only byte capture; a consumer must ALSO require successful final
workflow status and its qualification receipts. Source paths never enter receipts.

The injectable policy has ``principal``, ``volume(path)``,
``admit(path, *, owned=False, ancestor=False)``, ``capacity(path) -> (total, free)``,
and ``publish(source, destination, *, expected_source, expected_parent)``.
Publication MUST bind both admitted directory identities, be atomic and same-volume,
and fail if the destination exists. The production policy is Windows-only.

Failed invocations conservatively retain partial state and report cleanup_pending;
there is deliberately no pathname-based deletion. These checks support a serialized,
trusted runner principal/Admin/System context, not isolation from malicious processes
running as those principals. Consumers must revalidate the bundle before use.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import types
from contextlib import contextmanager
from pathlib import Path, PurePosixPath, PureWindowsPath
from uuid import uuid4

MAX_ARCHIVES = 36
MAX_ARCHIVE_BYTES = 8 * 1024**3
MAX_JSON_BYTES = 256 * 1024
CHUNK_BYTES = 1024 * 1024
MIN_FREE_BYTES = 2 * 1024**3
OWNER_FILE = "OWNER.json"
RECEIPT_FILE = "provenance.json"
COMPLETE_FILE = "COMPLETE.json"
_SCHEMA = "k5-local-wheel-retention-v1"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_REVISION = re.compile(r"[0-9a-f]{40}")
_ID = re.compile(r"[1-9][0-9]{0,19}")
_SID = re.compile(r"S-1-(?:[0-9]{1,10}-){1,15}[0-9]{1,10}")
_WHEEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+!-]{0,239}\.whl")
_GROUPS = {"wheelhouse", "built-wheels", "ensurepip"}
_TRUSTED_INSTALLER = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
_MUTATION = 0x010D0156
_ANCESTOR_MUTATION = 0x010D0150
_SECURITY_HASH = "a5ca5fbdef60960de07384138d3290d6c6a1c7e8f688362f6cfd8c7976537ae9"
_CODES = {
    "storage_path",
    "storage_layout",
    "storage_policy",
    "storage_volume",
    "storage_ownership",
    "storage_acl",
    "storage_helper",
    "storage_namespace",
    "storage_exists",
    "storage_identity",
    "storage_records",
    "storage_provenance",
    "storage_limit",
    "storage_capacity",
    "storage_hash",
    "storage_io",
    "storage_interrupted",
    "storage_inventory",
}


class StorageError(RuntimeError):
    """Only a fixed code and bounded, path-free cleanup state escape a failure."""

    def __init__(self, code: str, *, cleanup_pending: bool = False) -> None:
        self.code = code if code in _CODES else "storage_io"
        self.reason_code = self.code
        self.cleanup_pending = cleanup_pending
        self.report = {"reason_code": self.code, "cleanup_pending": cleanup_pending}
        super().__init__(self.code)


def _need(condition: bool, code: str) -> None:
    if not condition:
        raise StorageError(code)


def _identity(info: os.stat_result) -> tuple[int, int]:
    _need(bool(info.st_ino), "storage_identity")
    return info.st_dev, info.st_ino


def _ordinary(info: os.stat_result, *, directory: bool) -> None:
    _need(
        not stat.S_ISLNK(info.st_mode)
        and not getattr(info, "st_file_attributes", 0) & 0x400
        and (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
        and (directory or info.st_nlink == 1),
        "storage_path",
    )


def _path(value: Path) -> Path:
    _need(isinstance(value, Path), "storage_path")
    _need(value.is_absolute() and len(value.parts) <= 64, "storage_path")
    _need(not str(value).startswith(("\\\\", "//")), "storage_path")
    for part in value.parts[1:]:
        _need(
            part not in {".", ".."}
            and len(part) <= 255
            and not re.search(r'[\\:*?"<>|\x00-\x1f]', part)
            and not part.endswith((".", " "))
            and not PureWindowsPath(part).is_reserved(),
            "storage_path",
        )
    return value


def _checked(path: Path, policy, *, directory: bool = True, owned: bool = False):
    _path(path)
    policy.volume(path)
    for parent in reversed(path.parents):
        info = parent.lstat()
        _ordinary(info, directory=True)
        policy.admit(parent, ancestor=True)
    info = path.lstat()
    _ordinary(info, directory=directory)
    policy.admit(path, owned=owned)
    # resolve is only a comparison after refusing all links, never an admission
    # of their destinations. It also refuses Windows short-name/case aliases.
    _need(path.resolve(strict=True) == path, "storage_path")
    return info


def _same(path: Path, expected: tuple[int, int], policy, *, directory=True, owned=False):
    info = _checked(path, policy, directory=directory, owned=owned)
    _need(_identity(info) == expected, "storage_identity")
    return info


def _open_read(path: Path, policy):
    info = _checked(path, policy, directory=False)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        _ordinary(opened, directory=False)
        _need(_identity(opened) == _identity(info), "storage_identity")
        _same(path, _identity(info), policy, directory=False)
        return os.fdopen(descriptor, "rb"), info
    except BaseException:
        os.close(descriptor)
        raise


def _hash(path: Path, size: int, policy, expected=None) -> tuple[str, tuple[int, int]]:
    stream, info = _open_read(path, policy)
    identity = _identity(info)
    with stream:
        _need(expected is None or identity == expected, "storage_identity")
        _need(info.st_size == size, "storage_hash")
        digest = hashlib.sha256()
        remaining = size
        while remaining:
            block = stream.read(min(CHUNK_BYTES, remaining))
            _need(bool(block), "storage_hash")
            digest.update(block)
            remaining -= len(block)
        _need(not stream.read(1), "storage_hash")
        after = os.fstat(stream.fileno())
        _need(
            (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            == (info.st_size, info.st_mtime_ns, info.st_ctime_ns),
            "storage_identity",
        )
    after = _same(path, identity, policy, directory=False)
    _need(
        (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        == (info.st_size, info.st_mtime_ns, info.st_ctime_ns),
        "storage_identity",
    )
    return digest.hexdigest(), identity


def _token_owner(native) -> str:
    """Query TokenOwner, including an elevated token's actual default owner."""
    token = ctypes.c_void_p()
    _need(
        bool(
            native.security.OpenProcessToken(
                native.kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)
            )
        ),
        "storage_ownership",
    )
    try:
        length = ctypes.c_uint32()
        native.security.GetTokenInformation(token, 4, None, 0, ctypes.byref(length))
        _need(ctypes.sizeof(ctypes.c_void_p) <= length.value <= 4096, "storage_ownership")
        buffer = ctypes.create_string_buffer(length.value)
        _need(
            bool(
                native.security.GetTokenInformation(token, 4, buffer, length, ctypes.byref(length))
            ),
            "storage_ownership",
        )
        return native.sid(ctypes.c_void_p.from_buffer(buffer).value)
    finally:
        native.kernel.CloseHandle(token)


def _principal_fingerprint(policy) -> str:
    return hashlib.sha256(policy.principal.encode("ascii")).hexdigest()


class _RenameInformation(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_uint32),
        ("root", ctypes.c_void_p),
        ("name_length", ctypes.c_uint32),
        ("name", ctypes.c_uint16 * 1),
    ]


def _rename_information(parent_handle: int, name: str):
    # FILE_RENAME_INFO: flags=0 means ReplaceIfExists=FALSE. UTF-16 byte count
    # excludes the optional terminator; the leaf is relative to RootDirectory.
    # https://learn.microsoft.com/windows/win32/api/winbase/ns-winbase-file_rename_info
    _need(
        bool(re.fullmatch(r"[1-9][0-9]{0,19}-[1-9][0-9]{0,19}-[0-9a-f]{40}", name)),
        "storage_records",
    )
    encoded = name.encode("utf-16-le")
    offset = _RenameInformation.name.offset
    buffer = ctypes.create_string_buffer(
        max(ctypes.sizeof(_RenameInformation), offset + len(encoded))
    )
    header = _RenameInformation.from_buffer(buffer)
    header.flags, header.root, header.name_length = 0, parent_handle, len(encoded)
    ctypes.memmove(ctypes.addressof(buffer) + offset, encoded, len(encoded))
    return buffer


class NativeStoragePolicy:
    """Read-only native owner/ACL, fixed-volume, and capacity admission.

    Public reading is permitted. Foreign writes, deletion, ownership/ACL changes,
    reparse points, SUBST/UNC/device paths, and unsupported descriptors fail closed.
    No native queries run on import, and construction is refused outside Windows.
    """

    def __init__(self) -> None:
        _need(os.name == "nt", "storage_policy")
        helper = (
            Path(__file__).absolute().parent.parent / "src/k5vision/windows_identity_security.py"
        )
        try:
            _path(helper)
            for parent in reversed((helper, *helper.parents)):
                _ordinary(parent.lstat(), directory=parent != helper)
            before = helper.lstat()
            with helper.open("rb") as stream:
                _need(_identity(os.fstat(stream.fileno())) == _identity(before), "storage_helper")
                source = stream.read(64 * 1024 + 1)
            _need(_identity(helper.lstat()) == _identity(before), "storage_helper")
            # Git may check out text as CRLF on Windows. Only that newline
            # representation is normalized; the admitted source remains pinned.
            _need(
                hashlib.sha256(source.replace(b"\r\n", b"\n")).hexdigest() == _SECURITY_HASH,
                "storage_helper",
            )
            module = types.ModuleType("_k5_retention_security_" + uuid4().hex)
            sys.modules[module.__name__] = module
            try:
                exec(compile(source, "<k5-storage-security>", "exec"), module.__dict__)
            finally:
                sys.modules.pop(module.__name__, None)
            self._module = module
            self._native = module._NativeSecurity()
            self._bind_publication()
            self.principal = self._native.current_user()
            _need(bool(_SID.fullmatch(self.principal)), "storage_ownership")
            self.default_owner = _token_owner(self._native)
            _need(self.default_owner in {self.principal, "S-1-5-32-544"}, "storage_ownership")
        except StorageError:
            raise
        except Exception:
            raise StorageError("storage_helper") from None

    def volume(self, path: Path) -> None:
        try:
            self._module.check_windows_volume(path)
        except Exception:
            raise StorageError("storage_volume") from None

    def admit(self, path: Path, *, owned: bool = False, ancestor: bool = False) -> None:
        try:
            _need(self._native.current_user() == self.principal, "storage_ownership")
            _need(_token_owner(self._native) == self.default_owner, "storage_ownership")
            owner, entries = self._native.descriptor(os.fspath(path))
            trusted = {self.principal, "S-1-5-18", "S-1-5-32-544"}
            if ancestor and (
                owner == _TRUSTED_INSTALLER
                or any(entry.sid == _TRUSTED_INSTALLER for entry in entries)
            ):
                if self._native.trusted_installer():
                    trusted.add(_TRUSTED_INSTALLER)
            _need(
                owner in {self.principal, self.default_owner} if owned else owner in trusted,
                "storage_ownership",
            )
            for entry in entries:
                if entry.kind == 1:
                    continue  # A denial does not cancel an unsafe allow.
                _need(entry.kind == 0, "storage_acl")
                if ancestor and entry.flags & 0x08:
                    continue
                sid = entry.sid
                if sid == "S-1-3-4":
                    sid = owner
                elif sid == "S-1-3-0" and entry.flags & 0x08:
                    sid = self.principal
                mask = self._module._expanded_mask(entry.mask)
                mutation = _ANCESTOR_MUTATION if ancestor else _MUTATION
                _need(sid in trusted or not mask & mutation, "storage_acl")
        except StorageError:
            raise
        except Exception:
            raise StorageError("storage_acl") from None

    def capacity(self, path: Path) -> tuple[int, int]:
        try:
            self.volume(path)
            usage = shutil.disk_usage(path)
            return usage.total, usage.free
        except StorageError:
            raise
        except Exception:
            raise StorageError("storage_capacity") from None

    def _bind_publication(self) -> None:
        import msvcrt

        self._open_osfhandle = msvcrt.open_osfhandle
        pointer, dword = ctypes.c_void_p, ctypes.c_uint32
        bindings = (
            (
                "CreateFileW",
                [ctypes.c_wchar_p, dword, dword, pointer, dword, dword, pointer],
                pointer,
            ),
            ("SetFileInformationByHandle", [pointer, ctypes.c_int, pointer, dword], ctypes.c_int),
        )
        for name, arguments, result in bindings:
            function = getattr(self._native.kernel, name)
            function.argtypes, function.restype = arguments, result

    @contextmanager
    def _locked_directory(self, path: Path, expected, *, access: int, sharing: int):
        # OPEN_EXISTING + BACKUP_SEMANTICS + OPEN_REPARSE_POINT; no create,
        # truncate, delete-on-close, ACL change, or privilege adjustment.
        handle = self._native.kernel.CreateFileW(
            str(path), access, sharing, None, 3, 0x02200000, None
        )
        _need(handle not in (None, ctypes.c_void_p(-1).value), "storage_identity")
        descriptor = None
        try:
            descriptor = self._open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
            info = os.fstat(descriptor)
            _ordinary(info, directory=True)
            _need(_identity(info) == expected, "storage_identity")
            _same(path, expected, self, owned=True)
            yield handle
        finally:
            if descriptor is None:
                self._native.kernel.CloseHandle(handle)
            else:
                os.close(descriptor)  # open_osfhandle transferred handle ownership.

    def publish(self, source: Path, destination: Path, *, expected_source, expected_parent) -> None:
        _need(os.name == "nt", "storage_policy")
        _need(
            source.parent == destination.parent and expected_source[0] == expected_parent[0],
            "storage_volume",
        )
        _same(source.parent, expected_parent, self, owned=True)
        _same(source, expected_source, self, owned=True)
        # These handles prevent renaming the parent and opening the partial
        # DIRECTORY OBJECT for write/delete through another handle. Descendant
        # file contents are not frozen; the trusted serialized boundary and final
        # hashes/inventory checks remain necessary. RootDirectory binds the
        # destination to that exact namespace object.
        # https://learn.microsoft.com/windows/win32/api/fileapi/nf-fileapi-createfilew
        # https://learn.microsoft.com/windows/win32/api/fileapi/nf-fileapi-setfileinformationbyhandle
        with self._locked_directory(
            source.parent, expected_parent, access=0x00000084, sharing=3
        ) as parent_handle:
            with self._locked_directory(
                source, expected_source, access=0x00010080, sharing=1
            ) as source_handle:
                _need(not os.path.lexists(destination), "storage_exists")
                information = _rename_information(parent_handle, destination.name)
                if not self._native.kernel.SetFileInformationByHandle(
                    source_handle, 3, information, len(information)
                ):
                    code = (
                        "storage_exists" if ctypes.get_last_error() in {80, 183} else "storage_io"
                    )
                    raise StorageError(code)


def derive_storage_root(runner_workspace: Path, workspace: Path, runner_temp: Path, policy) -> Path:
    """Read-only derivation from the real runner layout; never guess a drive."""
    try:
        for path in (runner_workspace, workspace, runner_temp):
            _checked(path, policy)
        work_root = runner_workspace.parent
        _need(
            workspace.parent == runner_workspace
            and workspace.name == runner_workspace.name == "K5-Vision"
            and runner_temp.parent == work_root
            and runner_temp.name == "_temp",
            "storage_layout",
        )
        root_info = _checked(work_root, policy)
        _need(
            all(
                path.lstat().st_dev == root_info.st_dev
                for path in (runner_workspace, workspace, runner_temp)
            ),
            "storage_volume",
        )
        return work_root / "k5-qualification-artifacts" / "K5-Vision"
    except StorageError:
        raise
    except Exception:
        raise StorageError("storage_path") from None


def _relative(value: object) -> str:
    _need(type(value) is str and len(value) <= 270, "storage_records")
    pieces = value.split("/")
    _need(
        len(pieces) == 2
        and pieces[0] in _GROUPS
        and bool(_WHEEL.fullmatch(pieces[1]))
        and not PureWindowsPath(pieces[1]).is_reserved()
        and str(PurePosixPath(value)) == value,
        "storage_records",
    )
    return value


def _records(records: list[dict]) -> tuple[list[dict], int]:
    _need(type(records) is list and 0 < len(records) <= MAX_ARCHIVES, "storage_records")
    selected, paths, identities = [], set(), set()
    total = 0
    for record in records:
        _need(type(record) is dict, "storage_records")
        _need(record.keys() == {"source", "relative_path", "size", "sha256"}, "storage_records")
        relative = _relative(record["relative_path"])
        _need(relative.casefold() not in paths, "storage_records")
        source = _path(record["source"])
        _need(source.name == relative.split("/")[1], "storage_records")
        _need(str(source).casefold() not in identities, "storage_records")
        size, digest = record["size"], record["sha256"]
        _need(type(size) is int and 0 < size <= MAX_ARCHIVE_BYTES, "storage_records")
        _need(type(digest) is str and bool(_SHA256.fullmatch(digest)), "storage_records")
        total += size
        _need(total <= MAX_ARCHIVE_BYTES, "storage_limit")
        paths.add(relative.casefold())
        identities.add(str(source).casefold())
        selected.append(
            {"source": source, "relative_path": relative, "size": size, "sha256": digest}
        )
    return sorted(selected, key=lambda record: record["relative_path"]), total


def _json(value: dict) -> bytes:
    try:
        payload = (
            json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError):
        raise StorageError("storage_provenance") from None
    _need(len(payload) <= MAX_JSON_BYTES, "storage_limit")
    return payload


def _provenance(value: dict, selected: list[dict]) -> dict:
    """Bound a source-free collector document; semantic admission is the caller's.

    The collector owns the full schema and closure verification. This independent
    boundary refuses path/URL/credential-bearing extensions, non-JSON values,
    unbounded structures, and any claim that artifact capture accepts an installer.
    """
    allowed = {
        "schema_version",
        "scope",
        "qualification",
        "runtime",
        "installer",
        "python",
        "wheels",
        "closure",
        "installer_subset",
        "installer_accepted",
    }
    _need(type(value) is dict and value.keys() <= allowed, "storage_provenance")
    _need(
        value.get("schema_version") == "k5-native-wheel-provenance-v1"
        and value.get("scope") == "artifact-capture-only"
        and value.get("installer_accepted") is False,
        "storage_provenance",
    )
    nodes = 0

    def visit(item, depth=0):
        nonlocal nodes
        nodes += 1
        _need(depth <= 16 and nodes <= 16384, "storage_limit")
        if type(item) is dict:
            _need(len(item) <= 256, "storage_limit")
            for key, child in item.items():
                _need(type(key) is str and 0 < len(key) <= 256, "storage_provenance")
                _need(
                    key.lower()
                    not in {
                        "argv",
                        "environment",
                        "source_path",
                        "password",
                        "credentials",
                        "token",
                        "secret",
                        "access_token",
                    },
                    "storage_provenance",
                )
                visit(key, depth + 1)
                visit(child, depth + 1)
        elif type(item) is list:
            _need(len(item) <= 4096, "storage_limit")
            for child in item:
                visit(child, depth + 1)
        elif type(item) is str:
            _need(
                len(item) <= 4096 and all(32 <= ord(c) <= 126 for c in item), "storage_provenance"
            )
            _need(
                re.search(r"(?:^|[\s\"'\[({=:,])/", item) is None
                and "\\" not in item
                and re.search(r"[A-Za-z]:[/\\]|[A-Za-z][A-Za-z0-9+.-]*://", item) is None
                and not any(part == ".." for part in item.split("/")),
                "storage_provenance",
            )
        else:
            _need(
                item is None or type(item) is bool or type(item) is int and 0 <= item <= 2**63 - 1,
                "storage_provenance",
            )

    visit(value)
    if "installer_subset" in value:
        subset = value["installer_subset"]
        _need(type(subset) is list and len(subset) <= MAX_ARCHIVES, "storage_provenance")
        available = {record["relative_path"] for record in selected}
        _need(all(type(path) is str and path in available for path in subset), "storage_provenance")
        _need(len(set(subset)) == len(subset), "storage_provenance")
    # Round-trip isolates the captured document from caller mutations during I/O.
    return json.loads(_json(value))


class _Owned:
    def __init__(self, policy):
        self.policy = policy
        self.created: dict[Path, tuple[tuple[int, int], bool]] = {}
        self.sealed: dict[Path, tuple[int, int, int]] = {}
        self.creation_attempted = False

    def check(self, path: Path) -> None:
        identity, directory = self.created[path]
        info = _same(path, identity, self.policy, directory=directory, owned=True)
        if path in self.sealed:
            _need(
                (info.st_size, info.st_mtime_ns, info.st_ctime_ns) == self.sealed[path],
                "storage_identity",
            )

    def seal(self, path: Path) -> None:
        self.check(path)
        info = path.lstat()
        self.sealed[path] = (info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        self.check(path)

    def directory(self, path: Path) -> None:
        # Inherit native ACLs unchanged, then read-only admit them. Python's
        # Windows-specific mode=0o700 behavior must not rewrite that boundary.
        _checked(path.parent, self.policy)
        self.creation_attempted = True
        path.mkdir()
        self.created[path] = (_identity(path.lstat()), True)
        self.check(path)

    def file(self, path: Path, payload: bytes | None = None):
        _checked(path.parent, self.policy, owned=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        self.creation_attempted = True
        descriptor = os.open(path, flags, 0o666)
        try:
            self.created[path] = (_identity(os.fstat(descriptor)), False)
            self.check(path)
            stream = os.fdopen(descriptor, "wb")
        except BaseException:
            os.close(descriptor)
            raise
        if payload is None:
            return stream
        with stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        self.seal(path)
        return None

    def inventory(self, root: Path) -> None:
        expected = {path for path in self.created if path == root or root in path.parents}
        seen = set()
        pending = [root]
        while pending:
            path = pending.pop()
            _need(path in expected, "storage_inventory")
            self.check(path)
            seen.add(path)
            if self.created[path][1]:
                with os.scandir(path) as entries:
                    for entry in entries:
                        child = path / entry.name
                        _need(child in expected, "storage_inventory")
                        pending.append(child)
        _need(seen == expected, "storage_inventory")

    def rebase(self, source: Path, destination: Path) -> None:
        def moved(path):
            return (
                destination / path.relative_to(source)
                if path == source or source in path.parents
                else path
            )

        self.created = {moved(path): value for path, value in self.created.items()}
        self.sealed = {moved(path): value for path, value in self.sealed.items()}

    def cleanup(self) -> bool:
        # No path-based deletion can atomically establish that the object being
        # removed is still ours. Retain everything, including an uncertain
        # mkdir/open that succeeded before identity capture failed. Cleanup
        # requires a separately authorized handle-bound implementation/operator.
        return self.creation_attempted


def _namespace(path: Path, kind: str, owned: _Owned) -> None:
    expected = _json(
        {
            "schema": _SCHEMA,
            "project": "K5-Vision",
            "kind": kind,
            "principal_fingerprint": _principal_fingerprint(owned.policy),
        }
    )
    try:
        path.lstat()
    except FileNotFoundError:
        try:
            owned.directory(path)
        except FileExistsError:
            raise StorageError("storage_namespace") from None
        owned.file(path / OWNER_FILE, expected)
        return
    _checked(path, owned.policy, owned=True)
    try:
        _checked(path / OWNER_FILE, owned.policy, directory=False, owned=True)
        digest, _ = _hash(path / OWNER_FILE, len(expected), owned.policy)
        _need(digest == hashlib.sha256(expected).hexdigest(), "storage_namespace")
    except StorageError:
        raise
    except Exception:
        raise StorageError("storage_namespace") from None


def _capacity(path: Path, policy, remaining: int, expected_total: int | None = None) -> int:
    total, free = policy.capacity(path)
    _need(type(total) is int and type(free) is int and 0 <= free <= total, "storage_capacity")
    _need(expected_total is None or total == expected_total, "storage_capacity")
    reserve = max(MIN_FREE_BYTES, (total + 19) // 20)
    _need(free >= remaining + reserve, "storage_capacity")
    return total


def retain_bundle(
    storage_root: Path,
    run_id: str,
    attempt: str,
    revision: str,
    records: list[dict],
    provenance: dict,
    *,
    policy,
) -> dict:
    """Capture only admitted archives, publishing COMPLETE last without overwrites.

    ``records`` has at most 36 unique source/relative-path/size/SHA-256 records.
    The caller enforces its exact qualification inventory (currently 36). This
    layer represents installer subsets by references, never duplicate copies.
    """
    owned = _Owned(policy)
    try:
        _need(type(run_id) is str and bool(_ID.fullmatch(run_id)), "storage_records")
        _need(type(attempt) is str and bool(_ID.fullmatch(attempt)), "storage_records")
        _need(type(revision) is str and bool(_REVISION.fullmatch(revision)), "storage_records")
        _need(
            type(policy.principal) is str and bool(_SID.fullmatch(policy.principal)),
            "storage_ownership",
        )
        storage_root = _path(storage_root)
        _need(
            storage_root.name == "K5-Vision"
            and storage_root.parent.name == "k5-qualification-artifacts",
            "storage_layout",
        )
        work_root = storage_root.parent.parent
        work_identity = _identity(_checked(work_root, policy))
        selected, total_bytes = _records(records)
        provenance = _provenance(provenance, selected)
        bundle_key = f"{run_id}-{attempt}-{revision}"
        final = storage_root / bundle_key
        partial = storage_root / (".partial-" + bundle_key + "-" + uuid4().hex)
        disk_total = _capacity(work_root, policy, total_bytes)
        source_identities = {}
        for record in selected:
            source = record["source"]
            _need(storage_root != source and storage_root not in source.parents, "storage_records")
            digest, identity = _hash(source, record["size"], policy)
            _need(digest == record["sha256"], "storage_hash")
            _need(identity not in source_identities.values(), "storage_records")
            source_identities[source] = identity
        _same(work_root, work_identity, policy)
        _namespace(storage_root.parent, "artifact-namespace", owned)
        _namespace(storage_root, "project-namespace", owned)
        root_identity = _identity(_checked(storage_root, policy, owned=True))
        _need(root_identity[0] == work_identity[0], "storage_volume")
        _need(not os.path.lexists(final), "storage_exists")
        owned.directory(partial)
        owned.file(
            partial / OWNER_FILE,
            _json(
                {
                    "schema": _SCHEMA,
                    "kind": "partial-bundle",
                    "principal_fingerprint": _principal_fingerprint(policy),
                    "project": "K5-Vision",
                    "bundle_key": bundle_key,
                }
            ),
        )
        remaining = total_bytes
        for record in selected:
            _same(storage_root, root_identity, policy, owned=True)
            owned.check(partial)
            destination = partial / record["relative_path"]
            if destination.parent not in owned.created:
                owned.directory(destination.parent)
            source = record["source"]
            stream, source_info = _open_read(source, policy)
            with stream:
                _need(_identity(source_info) == source_identities[source], "storage_identity")
                _need(source_info.st_size == record["size"], "storage_hash")
                with owned.file(destination) as output:
                    left = record["size"]
                    while left:
                        _capacity(work_root, policy, remaining, disk_total)
                        owned.check(partial)
                        owned.check(destination)
                        block = stream.read(min(CHUNK_BYTES, left))
                        _need(bool(block), "storage_hash")
                        output.write(block)
                        left -= len(block)
                        remaining -= len(block)
                    _need(not stream.read(1), "storage_hash")
                    output.flush()
                    os.fsync(output.fileno())
            owned.seal(destination)
            for path, identity in (
                (source, source_identities[source]),
                (destination, owned.created[destination][0]),
            ):
                digest, _ = _hash(path, record["size"], policy, identity)
                _need(digest == record["sha256"], "storage_hash")
        _need(remaining == 0, "storage_inventory")
        public_records = [
            {key: record[key] for key in ("relative_path", "size", "sha256")} for record in selected
        ]
        receipt_bytes = _json(provenance)
        owned.file(partial / RECEIPT_FILE, receipt_bytes)
        for record in selected:
            digest, _ = _hash(
                record["source"], record["size"], policy, source_identities[record["source"]]
            )
            _need(digest == record["sha256"], "storage_hash")
            destination = partial / record["relative_path"]
            digest, _ = _hash(destination, record["size"], policy, owned.created[destination][0])
            _need(digest == record["sha256"], "storage_hash")
        owned.inventory(partial)
        _capacity(work_root, policy, 0, disk_total)
        receipt_sha256 = hashlib.sha256(receipt_bytes).hexdigest()
        complete = _json(
            {
                "schema": _SCHEMA,
                "bundle_key": bundle_key,
                "capture_only": True,
                "run_id": run_id,
                "attempt": attempt,
                "revision": revision,
                "archive_count": len(selected),
                "archive_bytes": total_bytes,
                "archives": public_records,
                "requires_successful_final_run_and_receipts": True,
                "provenance_sha256": receipt_sha256,
            }
        )
        owned.file(partial / COMPLETE_FILE, complete)
        owned.inventory(partial)
        # COMPLETE itself consumes blocks. Check actual free space after its
        # allocation and again after publication, rather than ignoring metadata.
        _capacity(work_root, policy, 0, disk_total)
        _same(work_root, work_identity, policy)
        _same(storage_root, root_identity, policy, owned=True)
        _need(not os.path.lexists(final), "storage_exists")
        policy.publish(
            partial, final, expected_source=owned.created[partial][0], expected_parent=root_identity
        )
        owned.rebase(partial, final)
        owned.inventory(final)
        for record in selected:
            destination = final / record["relative_path"]
            digest, _ = _hash(destination, record["size"], policy, owned.created[destination][0])
            _need(digest == record["sha256"], "storage_hash")
        for path, payload in (
            (final / RECEIPT_FILE, receipt_bytes),
            (final / COMPLETE_FILE, complete),
        ):
            digest, _ = _hash(path, len(payload), policy, owned.created[path][0])
            _need(digest == hashlib.sha256(payload).hexdigest(), "storage_hash")
        owned.inventory(final)
        _same(storage_root, root_identity, policy, owned=True)
        _capacity(work_root, policy, 0, disk_total)
        return {
            "bundle_key": bundle_key,
            "receipt_relative_path": f"{bundle_key}/{RECEIPT_FILE}",
            "complete_relative_path": f"{bundle_key}/{COMPLETE_FILE}",
            "receipt_sha256": receipt_sha256,
            "complete_sha256": hashlib.sha256(complete).hexdigest(),
            "archive_count": len(selected),
            "archive_bytes": total_bytes,
            "cleanup_pending": False,
        }
    except BaseException as exc:
        pending = owned.cleanup()
        code = (
            exc.code
            if isinstance(exc, StorageError)
            else (
                "storage_interrupted"
                if isinstance(exc, (KeyboardInterrupt, SystemExit))
                else "storage_io"
            )
        )
        raise StorageError(code, cleanup_pending=pending) from None
