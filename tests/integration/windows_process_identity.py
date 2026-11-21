"""Native Windows process topology without wall-clock birth ordering.

Toolhelp supplies parent PIDs. Raw GetProcessTimes FILETIMEs bind those PIDs
to exact process instances before and after each traversal. Creation times
are compared only for equality: a clock correction may invert a legitimate
parent/child's displayed birth order.
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes


def descendant_identities(root, parent_pids, read_identity, reread_parent_pids):
    """Return a stable root and descendants, rejecting identity/topology races."""
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

    def _handle_birth(self, handle):
        created, exited, kernel, user = (_FileTime() for _ in range(4))
        code = wintypes.DWORD()
        if not self.api.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:
            return None
        if not self.api.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            raise OSError("Native process creation identity could not be read")
        return (created.high << 32) | created.low

    def birth(self, pid, *, handle=None):
        if handle is not None:
            return self._handle_birth(handle)
        handle = self.api.OpenProcess(0x1000, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            if error in {87, 1168}:  # Invalid PID / process no longer exists.
                return None
            raise OSError("Native owned process could not be opened")
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
            return result
        finally:
            self.api.CloseHandle(handle)

    def descendants(self, root):
        return descendant_identities(root, self.parents(), self.birth, self.parents)
