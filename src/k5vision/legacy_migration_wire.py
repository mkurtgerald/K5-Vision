"""Bounded private migration frames and the native GUI-parent admission.

Only public process identity numbers enter the helper command line. Passwords,
factors and decrypted secrets travel through an admitted local pipe. Native
Windows qualification remains separate from the portable framing tests.
"""

from __future__ import annotations

import ctypes
import json
import math
import os
import struct
import sys
from pathlib import Path

from k5vision import identity_state
from k5vision.windows_identity_security import _native_security

MAX_FRAME = 131072
_PIPE_PREFIX = r"\\.\pipe\K5VisionMigration.Source.v1."
_TARGET_PIPE_PREFIX = r"\\.\pipe\K5VisionMigration.Target.v1."


class MigrationWireError(RuntimeError):
    """Finite migration transport failure, without credentials or private paths."""


def _reject_constant(_value):
    raise ValueError


def _finite_float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError
    return result


def _read_exact(stream, count):
    result = bytearray()
    while len(result) < count:
        block = stream.read(count - len(result))
        if type(block) is not bytes or not block or len(block) > count - len(result):
            raise MigrationWireError("Migration connection is unavailable")
        result.extend(block)
    return bytes(result)


def read_frame(stream):
    try:
        count = struct.unpack("<I", _read_exact(stream, 4))[0]
        if not 1 <= count <= MAX_FRAME:
            raise ValueError
        result = json.loads(
            _read_exact(stream, count).decode("utf-8", errors="strict"),
            object_pairs_hook=identity_state._unique_object,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
        if (
            type(result) is not dict
            or type(result.get("schema_version")) is not int
            or result["schema_version"] != 1
        ):
            raise ValueError
        return result
    except Exception:
        raise MigrationWireError("Migration connection is unavailable") from None


def write_frame(stream, document):
    try:
        if (
            type(document) is not dict
            or type(document.get("schema_version")) is not int
            or document["schema_version"] != 1
        ):
            raise ValueError
        payload = json.dumps(
            document, ensure_ascii=True, allow_nan=False, separators=(",", ":")
        ).encode("ascii")
        if not 1 <= len(payload) <= MAX_FRAME:
            raise ValueError
        remaining = memoryview(struct.pack("<I", len(payload)) + payload)
        while remaining:
            count = stream.write(remaining)
            if type(count) is not int or not 0 < count <= len(remaining):
                raise ValueError
            remaining = remaining[count:]
        stream.flush()
    except Exception:
        raise MigrationWireError("Migration connection is unavailable") from None


class _ProcessBasicInformation(ctypes.Structure):
    _fields_ = [
        ("reserved1", ctypes.c_void_p),
        ("peb", ctypes.c_void_p),
        ("reserved2", ctypes.c_void_p * 2),
        ("pid", ctypes.c_void_p),
        ("parent", ctypes.c_void_p),
    ]


def _native_parent(kernel, security, expected_pid, expected_birth, *, service=False):
    """Pin the actual same-user, same-session sealed GUI process lifetime."""
    pointer, dword = ctypes.c_void_p, ctypes.c_uint32
    kernel.OpenProcess.argtypes = [dword, ctypes.c_int, dword]
    kernel.OpenProcess.restype = pointer
    kernel.WaitForSingleObject.argtypes = [pointer, dword]
    kernel.WaitForSingleObject.restype = dword
    kernel.GetProcessTimes.argtypes = [pointer] + [ctypes.POINTER(ctypes.c_int64)] * 4
    kernel.GetProcessTimes.restype = ctypes.c_int
    kernel.QueryFullProcessImageNameW.argtypes = [
        pointer,
        dword,
        ctypes.c_wchar_p,
        ctypes.POINTER(dword),
    ]
    kernel.QueryFullProcessImageNameW.restype = ctypes.c_int
    kernel.ProcessIdToSessionId.argtypes = [dword, ctypes.POINTER(dword)]
    kernel.ProcessIdToSessionId.restype = ctypes.c_int
    kernel.GetCurrentProcess.restype = pointer
    kernel.CloseHandle.argtypes = [pointer]
    kernel.CloseHandle.restype = ctypes.c_int
    process = kernel.OpenProcess(0x00101000, False, expected_pid)
    if not process:
        raise ValueError
    try:
        if kernel.WaitForSingleObject(process, 0) != 0x102:
            raise ValueError
        times = [ctypes.c_int64() for _ in range(4)]
        if not kernel.GetProcessTimes(process, *(ctypes.byref(t) for t in times)):
            raise ValueError
        if times[0].value != expected_birth:
            raise ValueError
        image, length = ctypes.create_unicode_buffer(4096), dword(4096)
        expected = Path(sys.executable).parent.parent / "K5Vision.exe"
        if (
            Path(sys.executable).name.casefold() != "python.exe"
            or Path(sys.executable).parent.name.casefold() != "runtime"
            or not kernel.QueryFullProcessImageNameW(process, 0, image, ctypes.byref(length))
            or os.path.normcase(image.value) != os.path.normcase(str(expected))
        ):
            raise ValueError
        parent_session, own_session = dword(), dword()
        if (
            not kernel.ProcessIdToSessionId(expected_pid, ctypes.byref(parent_session))
            or not kernel.ProcessIdToSessionId(os.getpid(), ctypes.byref(own_session))
            or parent_session.value != own_session.value
            or ((parent_session.value == 0) != service)
        ):
            raise ValueError
        native = _native_security()
        token = pointer()
        if not security.OpenProcessToken(process, 8, ctypes.byref(token)):
            raise ValueError
        try:
            required = dword()
            security.GetTokenInformation(token, 1, None, 0, ctypes.byref(required))
            if not ctypes.sizeof(pointer) <= required.value <= 4096:
                raise ValueError
            buffer = ctypes.create_string_buffer(required.value)
            if not security.GetTokenInformation(
                token, 1, buffer, required.value, ctypes.byref(required)
            ):
                raise ValueError
            if native.sid(ctypes.cast(buffer, ctypes.POINTER(pointer))[0]) != native.current_user():
                raise ValueError
        finally:
            kernel.CloseHandle(token)
        ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
        ntdll.NtQueryInformationProcess.argtypes = [
            pointer,
            dword,
            pointer,
            dword,
            ctypes.POINTER(dword),
        ]
        ntdll.NtQueryInformationProcess.restype = ctypes.c_int32
        basic, returned = _ProcessBasicInformation(), dword()
        if (
            ntdll.NtQueryInformationProcess(
                kernel.GetCurrentProcess(),
                0,
                ctypes.byref(basic),
                ctypes.sizeof(basic),
                ctypes.byref(returned),
            )
            != 0
            or basic.parent != expected_pid
        ):
            raise ValueError
        result, process = process, None
        return result
    finally:
        if process:
            kernel.CloseHandle(process)


class NativeParentChannel:
    """An owned helper uses a local pipe only after live native-parent checks.

    The native GUI creates the first-instance, remote-rejecting private pipe
    before starting its Job-owned helper. Its per-frame timeout kills that
    owned helper on failure; no terminal, bearer argument or plaintext file is
    involved. This first migration flow requires the legacy owner to be an
    elevated Windows administrator; cross-user UAC imports remain refused.
    """

    def __init__(self, parent_pid: int, parent_birth: int, *, service=False):
        self.stream = self.process = None
        try:
            if (
                os.name != "nt"
                or type(parent_pid) is not int
                or not 0 < parent_pid <= 2**32 - 1
                or type(parent_birth) is not int
                or not 0 < parent_birth <= 2**63 - 1
            ):
                raise ValueError
            if type(service) is not bool:
                raise ValueError
            if service:
                from k5vision.service_setup import _require_service_token

                _require_service_token()
            else:
                shell = ctypes.WinDLL("shell32", use_last_error=True)
                shell.IsUserAnAdmin.restype = ctypes.c_int
                if not shell.IsUserAnAdmin():
                    raise ValueError
            native = _native_security()
            self.kernel = native.kernel
            self.process = _native_parent(
                self.kernel, native.security, parent_pid, parent_birth, service=service
            )
            self.birth = parent_birth
            self.kernel.CreateFileW.argtypes = [
                ctypes.c_wchar_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_void_p,
            ]
            self.kernel.CreateFileW.restype = ctypes.c_void_p
            self.kernel.GetNamedPipeServerProcessId.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_uint32),
            ]
            self.kernel.GetNamedPipeServerProcessId.restype = ctypes.c_int
            handle = self.kernel.CreateFileW(
                (_TARGET_PIPE_PREFIX if service else _PIPE_PREFIX)
                + str(parent_pid)
                + "."
                + str(parent_birth),
                0xC0000000,
                0,
                None,
                3,
                0x00110080,
                None,
            )
            if handle in (None, ctypes.c_void_p(-1).value):
                raise ValueError
            try:
                actual = ctypes.c_uint32()
                if (
                    not self.kernel.GetNamedPipeServerProcessId(handle, ctypes.byref(actual))
                    or actual.value != parent_pid
                ):
                    raise ValueError
                import msvcrt

                descriptor = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
                handle = None
                try:
                    self.stream = os.fdopen(descriptor, "r+b", buffering=0)
                except Exception:
                    os.close(descriptor)
                    raise
            finally:
                if handle:
                    self.kernel.CloseHandle(handle)
            self.assert_current()
        except Exception:
            self.close()
            raise MigrationWireError("Migration connection is unavailable") from None

    def assert_current(self):
        if self.process is None or self.kernel.WaitForSingleObject(self.process, 0) != 0x102:
            raise MigrationWireError("Migration connection is unavailable")
        times = [ctypes.c_int64() for _ in range(4)]
        if (
            not self.kernel.GetProcessTimes(self.process, *(ctypes.byref(t) for t in times))
            or times[0].value != self.birth
        ):
            raise MigrationWireError("Migration connection is unavailable")

    def exchange(self, request):
        self.assert_current()
        write_frame(self.stream, request)
        response = read_frame(self.stream)
        self.assert_current()
        return response

    def receive(self):
        self.assert_current()
        result = read_frame(self.stream)
        self.assert_current()
        return result

    def send(self, response):
        self.assert_current()
        write_frame(self.stream, response)

    def close(self):
        try:
            if self.stream is not None:
                self.stream.close()
                self.stream = None
        finally:
            if self.process is not None:
                self.kernel.CloseHandle(self.process)
                self.process = None
