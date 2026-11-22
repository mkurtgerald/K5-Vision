"""Native Windows process topology without wall-clock birth ordering.

Toolhelp supplies parent PIDs. Raw GetProcessTimes FILETIMEs bind those PIDs
to exact process instances before and after each traversal. Creation times
are compared only for equality: a clock correction may invert a legitimate
parent/child's displayed birth order.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import re
import stat
from ctypes import wintypes
from pathlib import Path, PureWindowsPath

_IMAGE_CATEGORIES = {
    "k5visionrecorder.exe": "wrapper",
    "k5vision.exe": "host",
    "python.exe": "python",
    "pythonw.exe": "pythonw",
    "ffmpeg.exe": "ffmpeg",
    "ffprobe.exe": "ffprobe",
    "mediamtx.exe": "mediamtx",
    "caddy.exe": "caddy",
    "werfault.exe": "werfault",
    "werfaultsecure.exe": "werfaultsecure",
    "wermgr.exe": "wermgr",
    "conhost.exe": "conhost",
    "openconsole.exe": "openconsole",
    "mfpmp.exe": "mfpmp",
    "rundll32.exe": "rundll32",
    "dllhost.exe": "dllhost",
    "audiodg.exe": "audiodg",
    "svchost.exe": "svchost",
    "csrss.exe": "csrss",
    "consent.exe": "consent",
    "crashpad_handler.exe": "crashpad",
    "cmd.exe": "cmd",
    "powershell.exe": "powershell",
    "powershell_ise.exe": "powershell_ise",
    "pwsh.exe": "pwsh",
    "w32tm.exe": "w32tm",
    "sc.exe": "sc",
    "wmic.exe": "wmic",
    "reg.exe": "reg",
    "whoami.exe": "whoami",
    "hostname.exe": "hostname",
    "ipconfig.exe": "ipconfig",
    "taskkill.exe": "taskkill",
    "tasklist.exe": "tasklist",
    "where.exe": "where",
    "find.exe": "find",
    "findstr.exe": "findstr",
}
_PAYLOAD_IMAGE_FILES = {
    "k5visionrecorder.exe": "K5VisionRecorder.exe",
    "k5vision.exe": "K5Vision.exe",
    "python.exe": "runtime/python.exe",
    "pythonw.exe": "runtime/pythonw.exe",
    "ffmpeg.exe": "workers/ffmpeg.exe",
    "ffprobe.exe": "workers/ffprobe.exe",
    "mediamtx.exe": "workers/mediamtx.exe",
    "caddy.exe": "workers/caddy.exe",
}
_SYSTEM_IMAGE_FILES = {
    name: name
    for name in _IMAGE_CATEGORIES
    if name not in {*_PAYLOAD_IMAGE_FILES, "crashpad_handler.exe", "pwsh.exe"}
}
_SYSTEM_IMAGE_FILES.update(
    {
        "powershell.exe": "WindowsPowerShell/v1.0/powershell.exe",
        "powershell_ise.exe": "WindowsPowerShell/v1.0/powershell_ise.exe",
        "wmic.exe": "wbem/wmic.exe",
    }
)


class OwnershipFailure(RuntimeError):
    def __init__(self, diagnostic):
        super().__init__("Native process ownership proof failed")
        self.diagnostic = diagnostic


def admit_system_console_file(image, system_directory):
    """Read-only custody for the fixed OS helper, including normal OS hardlinks."""
    from k5vision.installed_code_security import _code_policy, _native_security
    from k5vision.windows_identity_security import check_windows_volume

    expected = PureWindowsPath(system_directory) / "conhost.exe"
    _form, actual = _local_image_path(image)
    if actual != expected:
        raise RuntimeError("Console host image is not the fixed native OS file")
    check_windows_volume(str(expected))
    native, snapshots = _native_security(), []
    for component in reversed((expected, *expected.parents)):
        info = Path(component).lstat()
        if getattr(info, "st_file_attributes", 0) & 0x400 or stat.S_ISLNK(info.st_mode):
            raise RuntimeError("Console host code has a reparse boundary")
        if component == expected:
            if not stat.S_ISREG(info.st_mode) or info.st_nlink < 1:
                raise RuntimeError("Console host code is not an ordinary OS file")
        elif not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("Console host code ancestor is not a directory")
        _code_policy(native, component, within=component.is_relative_to(expected.parent))
        snapshots.append(
            (
                component,
                (
                    info.st_dev,
                    info.st_ino,
                    info.st_mode,
                    info.st_nlink,
                    info.st_size,
                    info.st_mtime_ns,
                    getattr(info, "st_file_attributes", 0),
                ),
            )
        )
    if not _same_local_file(image, str(expected)):
        raise RuntimeError("Console host OS file identity differs")
    for component, before in snapshots:
        info = Path(component).lstat()
        if before != (
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_nlink,
            info.st_size,
            info.st_mtime_ns,
            getattr(info, "st_file_attributes", 0),
        ):
            raise RuntimeError("Console host OS custody changed during admission")
    return {"system_file_identity_verified": True, "trusted_code_custody_verified": True}


class PinnedServiceParent:
    """Retain an already-admitted parent object, including after native exit."""

    def __init__(self, native, identity, handle, kind, service_sid):
        self.native, self.identity, self.handle = native, identity, handle
        self.kind, self.service_sid, self.cached_exit = kind, service_sid, None

    def validate(self, identity, kind, service_sid):
        if (
            self.handle is None
            or self.identity != identity
            or self.kind != kind
            or self.service_sid != service_sid
            or self.native._handle_creation(self.handle) != identity[1]
        ):
            raise RuntimeError("Original admitted service parent identity differs")

    def exited(self):
        self.validate(self.identity, self.kind, self.service_sid)
        state = self.native.api.WaitForSingleObject(self.handle, 0)
        if state == 258:
            return False
        if state != 0:
            raise RuntimeError("Original service parent exit query failed")
        code = wintypes.DWORD()
        if not self.native.api.GetExitCodeProcess(self.handle, ctypes.byref(code)):
            raise RuntimeError("Original service parent exit code is unavailable")
        self.cached_exit = {"exited": True, "exit_code": code.value}
        return True

    def close(self):
        if self.handle is None:
            return
        failure = None
        try:
            if not self.exited():
                self.cached_exit = {"exited": False, "exit_code": None}
        except BaseException as error:
            failure = error
        handle = self.handle
        if self.native.api.CloseHandle(handle):
            self.handle = None
        else:
            raise RuntimeError("Original service parent query handle could not be released")
        if failure is not None:
            raise failure


class PinnedConsoleHost:
    """An original process object held with query/synchronize rights only."""

    def __init__(self, native, identity, handle, facts):
        self.native, self.identity, self.handle = native, identity, handle
        self.facts, self.cached_exit = facts, None

    def exited(self):
        if self.handle is None:
            if self.cached_exit is None:
                raise RuntimeError("Console host exit observation was unavailable")
            return self.cached_exit["exited"]
        state = self.native.api.WaitForSingleObject(self.handle, 0)
        if state == 258:
            return False
        if state != 0:
            raise RuntimeError("Console host pinned exit observation failed")
        code = wintypes.DWORD()
        if not self.native.api.GetExitCodeProcess(self.handle, ctypes.byref(code)):
            raise RuntimeError("Console host pinned exit code was unavailable")
        self.cached_exit = {"exited": True, "exit_code": code.value}
        return True

    def close(self):
        if self.handle is None:
            return
        failure = None
        try:
            if not self.exited():
                self.cached_exit = {"exited": False, "exit_code": None}
        except BaseException as error:
            failure = error
        closed = self.native.api.CloseHandle(self.handle)
        if closed:
            self.handle = None
        if failure is not None:
            raise failure
        if not closed:
            raise RuntimeError("Console host query handle could not be released")


def _local_image_path(image):
    value = str(image)
    if value.startswith("\\\\?\\") and re.match(r"^[A-Za-z]:\\", value[4:]):
        return "extended_drive", PureWindowsPath(value[4:])
    if re.match(r"^[A-Za-z]:\\", value):
        return "normal_drive", PureWindowsPath(value)
    if value.startswith("\\Device\\") or value.startswith("\\??\\"):
        return "nt_device", None
    if value.startswith("\\\\"):
        return "unc", None
    return "other", None


def _same_local_file(image, expected):
    # Reject reparse ancestors before following a local-looking path into an
    # arbitrary remote target. Both actual and expected files are read-only.
    for value in (image, expected):
        path = Path(value)
        for component in reversed((path, *path.parents)):
            if getattr(component.lstat(), "st_file_attributes", 0) & 0x400:
                raise OSError("Image identity diagnostic refuses a reparse path")
    return Path(image).samefile(expected)


def _fixed_image_target(basename):
    """Select only native fixed OS/code paths, never an arbitrary image path."""
    from k5vision.installed_code_security import _installed_root

    if basename in _SYSTEM_IMAGE_FILES:
        api = ctypes.WinDLL("kernel32", use_last_error=True).GetSystemDirectoryW
        api.argtypes, api.restype = [wintypes.LPWSTR, wintypes.UINT], wintypes.UINT
        buffer = ctypes.create_unicode_buffer(32768)
        length = api(buffer, len(buffer))
        if not 1 <= length < len(buffer):
            raise OSError("Native system directory observation unavailable")
        root = PureWindowsPath(buffer.value)
        return root / _SYSTEM_IMAGE_FILES[basename], root, "fixed_system32"
    if basename in _PAYLOAD_IMAGE_FILES:
        root = _installed_root()
        return root / _PAYLOAD_IMAGE_FILES[basename], root, "fixed_installed_payload"
    if basename == "pwsh.exe":
        root = _installed_root().parent / "PowerShell" / "7"
        return root / "pwsh.exe", root, "fixed_programfiles_powershell7"
    return None


def _code_file_snapshot(info, *, type_only=False):
    return (
        info.st_dev,
        info.st_ino,
        stat.S_IFMT(info.st_mode) if type_only else info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        getattr(info, "st_file_attributes", 0),
    )


def _fixed_image_file_diagnostic(image, *, same_file):
    """Failure-only read of a same-file, custody-checked, finite known target."""
    from k5vision.installed_code_security import _code_policy, _native_security
    from k5vision.windows_identity_security import check_windows_volume

    basename = PureWindowsPath(image).name.casefold()
    if basename not in _IMAGE_CATEGORIES or basename == "crashpad_handler.exe":
        return {"status": "unlisted_basename"}
    if os.name != "nt":
        return {"status": "native_windows_unavailable"}
    target = _fixed_image_target(basename)
    if target is None:
        return {"status": "unlisted_fixed_file"}
    expected, code_root, scope = target
    result = {"status": "different_fixed_file", "scope": scope, "same_file": False}
    # Identity comparison may inspect image metadata; content is read ONLY from
    # the selected fixed target after matching it to the rejected image.
    if not same_file(str(image), str(expected)):
        return result
    result["same_file"] = True
    check_windows_volume(str(expected))
    native, snapshots, leaf_snapshot = _native_security(), [], None
    for component in reversed((expected, *expected.parents)):
        info = Path(component).lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Fixed image observation refuses reparse code")
        if component == expected:
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 256 * 1024**2:
                raise ValueError("Fixed image observation leaf is not bounded code")
            leaf_snapshot = _code_file_snapshot(info, type_only=True)
        elif not stat.S_ISDIR(info.st_mode):
            raise ValueError("Fixed image observation ancestor is not a directory")
        _code_policy(native, component, within=component.is_relative_to(code_root))
        snapshots.append((component, _code_file_snapshot(info)))
    with Path(expected).open("rb") as stream:
        before = os.fstat(stream.fileno())
        # CPython's Windows path stat synthesizes .exe execute permission bits;
        # handle stat has no filename. Only this path/handle comparison uses
        # file type; full path/path and handle/handle snapshots remain exact.
        if _code_file_snapshot(before, type_only=True) != leaf_snapshot:
            raise ValueError("Fixed image observation open identity changed")
        sha = hashlib.file_digest(stream, "sha256").hexdigest()
        after = os.fstat(stream.fileno())
    if _code_file_snapshot(before) != _code_file_snapshot(after) or not same_file(
        str(image), str(expected)
    ):
        raise ValueError("Fixed image observation content identity changed")
    for component, snapshot in snapshots:
        if snapshot != _code_file_snapshot(Path(component).lstat()):
            raise ValueError("Fixed image observation custody changed")
    result.update(
        status="observed",
        trusted_code_custody_verified=True,
        file_identity={"volume": before.st_dev, "file_id": before.st_ino, "links": before.st_nlink},
        file_bytes=before.st_size,
        file_sha256=sha,
    )
    return result


def image_diagnostic(image, images, *, same_file=None):
    """Finite failure evidence; it never admits or repairs an unexpected image."""
    form, local = _local_image_path(image)
    result = {
        "basename_category": _IMAGE_CATEGORIES.get(PureWindowsPath(image).name.casefold(), "other"),
        "path_form": form,
        "short_component": bool(re.search(r"~[0-9]", str(image))),
        "matching_payload_roles": [],
        "samefile_unavailable_count": 0,
        "comparison_status": "skipped_nonlocal",
    }
    expected = list(images.items())[:5]
    expected_drives = {
        _local_image_path(path)[1].drive.casefold()
        for path, _ in expected
        if _local_image_path(path)[1] is not None
    }
    if local is None:
        return result
    if len(expected_drives) != 1 or local.drive.casefold() not in expected_drives:
        result["comparison_status"] = "skipped_other_drive"
        return result
    result["comparison_status"] = "compared"
    compare = _same_local_file if same_file is None else same_file
    for path, role in expected:
        try:
            if compare(str(image), path):
                result["matching_payload_roles"].append(role)
        except (OSError, ValueError, TypeError):
            result["samefile_unavailable_count"] += 1
    result["matching_payload_roles"].sort()
    try:
        result["fixed_known_file"] = _fixed_image_file_diagnostic(image, same_file=compare)
    except Exception as error:
        kind = type(error).__name__
        result["fixed_known_file"] = {
            "status": "unavailable",
            "error_kind": kind
            if kind in {"OSError", "PermissionError", "ValueError", "TypeError", "RuntimeError"}
            else "other",
        }
    return result


def service_candidate(details, service_sid, images, *, root=False, diagnose_image=None):
    """Parent PID topology nominates candidates; token/image/session admit them."""
    kind = images.get(str(PureWindowsPath(details["image"])).casefold(), "outside_payload")
    result = {
        "pid": details["pid"],
        "created_100ns": details["created_100ns"],
        "parent_pid": details["parent_pid"],
        "kind": kind,
        "sid_role": "service" if details["sid"] == service_sid else "foreign",
        "session": details["session"],
        "accepted": False,
    }
    if result["sid_role"] != "service":
        result["outcome"] = "foreign_token"
        if root:
            raise OwnershipFailure(result)
        return result
    if kind == "outside_payload" or (root and kind != "wrapper"):
        result["outcome"] = "unexpected_service_image"
        if diagnose_image is not None:
            try:
                result["image_diagnostic"] = diagnose_image(details["image"], images)
            except (OSError, ValueError, TypeError, RuntimeError):
                result["image_diagnostic"] = {"comparison_status": "unavailable"}
        raise OwnershipFailure(result)
    if details["session"] != 0:
        result["outcome"] = "unexpected_service_session"
        raise OwnershipFailure(result)
    result.update(accepted=True, outcome="admitted")
    return result


def descendant_identities(root, parent_pids, read_identity, reread_parent_pids, *, edge_sink=None):
    """Return a stable root and descendants, rejecting identity/topology races."""
    if edge_sink is not None:
        edge_sink.clear()
    if read_identity(root[0]) != root[1]:
        return ()
    children = {}
    for pid, parent in parent_pids.items():
        if pid != parent:
            children.setdefault(parent, []).append(pid)
    admitted = {root[0]: root[1]}
    edges, pending = {}, [root]
    while pending:
        parent = pending.pop()
        if read_identity(parent[0]) != parent[1]:
            continue
        for pid in children.get(parent[0], ()):
            if pid in admitted:
                continue
            birth = read_identity(pid)
            if birth is None or read_identity(parent[0]) != parent[1]:
                continue
            if read_identity(pid) != birth:
                continue
            admitted[pid] = birth
            edges[pid] = parent
            pending.append((pid, birth))
    # Recheck every link after traversal; a parent PID reused during a longer
    # enumeration must invalidate its entire branch, including grandchildren.
    after = reread_parent_pids()
    stable = {root[0]: root[1]} if read_identity(root[0]) == root[1] else {}
    pending = list(edges)
    while pending:
        remaining = []
        for pid in pending:
            parent = edges[pid]
            if parent[0] not in stable:
                remaining.append(pid)
                continue
            if (
                stable[parent[0]] == parent[1]
                and after.get(pid) == parent[0]
                and read_identity(parent[0]) == parent[1]
                and read_identity(pid) == admitted[pid]
            ):
                stable[pid] = admitted[pid]
        if len(remaining) == len(pending):
            break
        pending = remaining
    if read_identity(root[0]) != root[1]:
        return ()
    if edge_sink is not None:
        edge_sink.update(
            ((pid, birth), edges[pid])
            for pid, birth in stable.items()
            if pid in edges and stable.get(edges[pid][0]) == edges[pid][1]
        )
    return tuple(stable.items())


class _ProcessEntry(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_uint32),
        ("usage", ctypes.c_uint32),
        ("pid", ctypes.c_uint32),
        ("heap", ctypes.c_size_t),
        ("module", ctypes.c_uint32),
        ("threads", ctypes.c_uint32),
        ("parent", ctypes.c_uint32),
        ("priority", ctypes.c_int32),
        ("flags", ctypes.c_uint32),
        ("executable", ctypes.c_wchar * 260),
    ]


class _FileTime(ctypes.Structure):
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]


class NativeProcessTree:
    def __init__(self):
        if os.name != "nt":
            raise RuntimeError("Native process identity requires Windows")
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.security = ctypes.WinDLL("advapi32", use_last_error=True)
        self._service_sid = None
        self.last_parents = {}
        self.last_identity_edges = {}
        self.api.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        self.api.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        for name in ("Process32FirstW", "Process32NextW"):
            function = getattr(self.api, name)
            function.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
            function.restype = wintypes.BOOL
        self.api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.api.OpenProcess.restype = wintypes.HANDLE
        self.api.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(_FileTime)] * 4)]
        self.api.GetProcessTimes.restype = wintypes.BOOL
        self.api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        self.api.GetExitCodeProcess.restype = wintypes.BOOL
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.api.CloseHandle.restype = wintypes.BOOL
        self.api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.api.WaitForSingleObject.restype = wintypes.DWORD
        self.api.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.api.QueryFullProcessImageNameW.restype = wintypes.BOOL
        self.api.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        self.api.ProcessIdToSessionId.restype = wintypes.BOOL
        self.api.GetSystemDirectoryW.argtypes = [wintypes.LPWSTR, wintypes.UINT]
        self.api.GetSystemDirectoryW.restype = wintypes.UINT
        self.security.OpenProcessToken.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        self.security.OpenProcessToken.restype = wintypes.BOOL
        self.security.GetTokenInformation.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.security.GetTokenInformation.restype = wintypes.BOOL
        self.security.IsValidSid.argtypes, self.security.IsValidSid.restype = (
            [ctypes.c_void_p],
            wintypes.BOOL,
        )
        self.security.GetLengthSid.argtypes, self.security.GetLengthSid.restype = (
            [ctypes.c_void_p],
            wintypes.DWORD,
        )
        self.security.LookupAccountNameW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.security.LookupAccountNameW.restype = wintypes.BOOL

    def _sid_bytes(self, pointer):
        if not pointer or not self.security.IsValidSid(pointer):
            raise OSError("Native process token SID was invalid")
        length = self.security.GetLengthSid(pointer)
        if not 8 <= length <= 68:
            raise OSError("Native process token SID exceeded its bound")
        return ctypes.string_at(pointer, length)

    def service_sid(self):
        if self._service_sid is None:
            size, domain_size, use = (wintypes.DWORD() for _ in range(3))
            account = r"NT SERVICE\K5VisionRecorder"
            self.security.LookupAccountNameW(
                None,
                account,
                None,
                ctypes.byref(size),
                None,
                ctypes.byref(domain_size),
                ctypes.byref(use),
            )
            if not 8 <= size.value <= 68 or not 1 <= domain_size.value <= 256:
                raise OSError("Native recorder account SID could not be resolved")
            sid = ctypes.create_string_buffer(size.value)
            domain = ctypes.create_unicode_buffer(domain_size.value)
            if not self.security.LookupAccountNameW(
                None,
                account,
                sid,
                ctypes.byref(size),
                domain,
                ctypes.byref(domain_size),
                ctypes.byref(use),
            ):
                raise OSError("Native recorder account SID could not be read")
            self._service_sid = self._sid_bytes(sid)
        return self._service_sid

    def system_directory(self):
        result = ctypes.create_unicode_buffer(32768)
        length = self.api.GetSystemDirectoryW(result, len(result))
        if not 1 <= length < len(result):
            raise OSError("Native Windows system directory could not be read")
        return result.value

    def pin_service_parent(self, identity, kind, service_sid, expected_image):
        if kind not in {"python", "mediamtx", "caddy"}:
            raise RuntimeError("Service parent is not an admitted native executor")
        handle = self.api.OpenProcess(0x00101000, False, identity[0])
        if not handle:
            raise RuntimeError("Original service parent could not be pinned")
        transferred = False
        try:
            if self._live_parent_birth(handle) != identity[1]:
                raise ProcessLookupError("Service parent changed before pin admission")
            try:
                details = self.details(identity)
            except ProcessLookupError:
                if self._live_parent_birth(handle) == identity[1]:
                    raise RuntimeError(
                        "Live service parent authority query was unavailable"
                    ) from None
                raise
            if (
                details["sid"] != service_sid
                or details["session"] != 0
                or PureWindowsPath(details["image"]) != PureWindowsPath(expected_image)
            ):
                raise RuntimeError("Service parent native authority differs")
            if self._live_parent_birth(handle) != identity[1]:
                raise ProcessLookupError("Service parent exited during pin admission")
            pin = PinnedServiceParent(self, identity, handle, kind, service_sid)
            transferred = True
            return pin
        finally:
            if not transferred and not self.api.CloseHandle(handle):
                raise RuntimeError("Untransferred service parent handle could not be released")

    def _verify_console_parent(self, identity, parent, parent_kind, service_sid, parent_pin):
        if (
            not isinstance(parent_pin, PinnedServiceParent)
            or parent_pin.native is not self
            or parent_kind not in {"python", "mediamtx", "caddy"}
            or self.last_identity_edges.get(identity) != parent
            or self.parents().get(identity[0]) != parent[0]
        ):
            raise RuntimeError("Console host exact admitted parent edge differs")
        parent_pin.validate(parent, parent_kind, service_sid)
        current_birth = self.parent_creation(parent[0])
        if current_birth is not None:
            if current_birth != parent[1]:
                raise RuntimeError("Console host parent PID has a different occupant")
        elif not parent_pin.exited():
            raise RuntimeError("Console host original parent liveness is unavailable")

    def admit_console_host(self, identity, parent, parent_kind, service_sid, *, parent_pin):
        handle = self.api.OpenProcess(0x00101000, False, identity[0])
        if not handle:
            raise RuntimeError("Console host original process could not be pinned")
        transferred = False
        try:
            if self._handle_birth(handle) != identity[1]:
                raise ProcessLookupError("Console host changed before admission")
            self._verify_console_parent(identity, parent, parent_kind, service_sid, parent_pin)
            details = self.details(identity)
            if details["sid"] != service_sid or details["session"] != 0:
                raise RuntimeError("Console host native authority was not admitted")
            facts = admit_system_console_file(details["image"], self.system_directory())
            if self._handle_birth(handle) != identity[1]:
                raise ProcessLookupError("Console host changed during admission")
            self._verify_console_parent(identity, parent, parent_kind, service_sid, parent_pin)
            facts.update(
                admitted_parent_identity_verified=True,
                parent_pid=parent[0],
                parent_created_100ns=parent[1],
            )
            result = PinnedConsoleHost(self, identity, handle, facts)
            transferred = True
            return result
        finally:
            if not transferred and not self.api.CloseHandle(handle):
                raise RuntimeError("Untransferred console query handle could not be released")

    def details(self, identity):
        """Private native facts; callers expose only the finite projection."""
        handle = self.api.OpenProcess(0x1000, False, identity[0])
        if not handle:
            if self.birth(identity[0]) is None:
                raise ProcessLookupError("Candidate exited during ownership observation")
            raise OSError("Native candidate ownership could not be opened")
        token = wintypes.HANDLE()
        try:
            if self._handle_birth(handle) != identity[1]:
                raise ProcessLookupError("Candidate changed during ownership observation")
            image, length = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
            if not self.api.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(length)):
                raise OSError("Native candidate image could not be read")
            if not self.security.OpenProcessToken(handle, 8, ctypes.byref(token)):
                raise OSError("Native candidate token could not be read")
            size = wintypes.DWORD()
            self.security.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
            if not ctypes.sizeof(ctypes.c_void_p) <= size.value <= 4096:
                raise OSError("Native candidate token exceeded its bound")
            user = ctypes.create_string_buffer(size.value)
            if not self.security.GetTokenInformation(token, 1, user, len(user), ctypes.byref(size)):
                raise OSError("Native candidate TokenUser could not be read")
            sid = self._sid_bytes(ctypes.c_void_p.from_buffer(user).value)
            elevation, elevation_size = wintypes.DWORD(), wintypes.DWORD()
            if not self.security.GetTokenInformation(
                token,
                20,
                ctypes.byref(elevation),
                ctypes.sizeof(elevation),
                ctypes.byref(elevation_size),
            ):
                raise OSError("Native candidate token elevation could not be read")
            session = wintypes.DWORD()
            if not self.api.ProcessIdToSessionId(identity[0], ctypes.byref(session)):
                raise OSError("Native candidate session could not be read")
            if self._handle_birth(handle) != identity[1] or self.birth(identity[0]) != identity[1]:
                raise ProcessLookupError("Candidate changed during ownership observation")
            if identity[0] not in self.last_parents:
                self.parents()
            return {
                "pid": identity[0],
                "created_100ns": identity[1],
                "parent_pid": self.last_parents.get(identity[0], 0),
                "image": image.value,
                "sid": sid,
                "session": session.value,
                "elevated": elevation.value != 0,
            }
        finally:
            if token:
                self.api.CloseHandle(token)
            self.api.CloseHandle(handle)

    def _handle_creation(self, handle):
        created, exited, kernel, user = (_FileTime() for _ in range(4))
        if not self.api.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            raise OSError("Native process creation identity could not be read")
        return (created.high << 32) | created.low

    def _handle_birth(self, handle):
        code = wintypes.DWORD()
        if not self.api.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:
            return None
        return self._handle_creation(handle)

    def _live_parent_birth(self, handle):
        code = wintypes.DWORD()
        if not self.api.GetExitCodeProcess(handle, ctypes.byref(code)):
            raise OSError("Native service parent exit query failed")
        if code.value != 259:
            return None
        return self._handle_creation(handle)

    def parent_creation(self, pid):
        """Distinguish original exit from an unavailable or reused PID object."""
        handle = self.api.OpenProcess(0x1000, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            if error in {87, 1168}:
                return None
            raise OSError(0, "Native parent identity could not be opened", None, error)
        try:
            code = wintypes.DWORD()
            if not self.api.GetExitCodeProcess(handle, ctypes.byref(code)):
                raise OSError("Native parent exit query failed")
            # An exited new PID occupant must still fail exact identity matching.
            return self._handle_creation(handle)
        finally:
            if not self.api.CloseHandle(handle):
                raise RuntimeError("Native parent query handle could not be released")

    def birth(self, pid, *, handle=None):
        if handle is not None:
            return self._handle_birth(handle)
        handle = self.api.OpenProcess(0x1000, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            if error in {87, 1168}:  # Invalid PID / process no longer exists.
                return None
            raise OSError(0, "Native owned process could not be opened", None, error)
        try:
            return self._handle_birth(handle)
        finally:
            self.api.CloseHandle(handle)

    def parents(self):
        handle = self.api.CreateToolhelp32Snapshot(2, 0)
        if handle in (None, ctypes.c_void_p(-1).value):
            raise OSError("Native process topology could not be opened")
        try:
            result, entry = {}, _ProcessEntry()
            entry.size = ctypes.sizeof(entry)
            present = self.api.Process32FirstW(handle, ctypes.byref(entry))
            while present:
                result[entry.pid] = entry.parent
                if len(result) > 65536:
                    raise OSError("Native process topology exceeded its bound")
                present = self.api.Process32NextW(handle, ctypes.byref(entry))
            if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
                raise OSError("Native process topology did not complete")
            self.last_parents = result
            return result
        finally:
            self.api.CloseHandle(handle)

    def descendants(self, root):
        self.last_identity_edges = {}
        parents = self.parents()

        def identity(pid):
            try:
                return self.birth(pid)
            except OSError as error:
                raise OwnershipFailure(
                    {
                        "pid": pid,
                        "created_100ns": root[1] if pid == root[0] else 0,
                        "parent_pid": parents.get(pid, 0),
                        "kind": "unavailable",
                        "sid_role": "unavailable",
                        "session": None,
                        "accepted": False,
                        "outcome": "proof_unavailable",
                        "native_code": getattr(error, "winerror", None),
                    }
                ) from None

        edges = {}
        result = descendant_identities(root, parents, identity, self.parents, edge_sink=edges)
        self.last_identity_edges = edges
        return result
