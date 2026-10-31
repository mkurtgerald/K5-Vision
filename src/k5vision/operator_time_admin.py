"""Explicit native Windows and ONVIF time administration, with read-back proof."""

from __future__ import annotations

import asyncio
import ctypes
import ipaddress
import json
import math
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit
from xml.sax.saxutils import escape

from k5vision.domain.devices import DeviceProtocol
from k5vision.identity_state import _checked_path, _regular_file, _windows_admission
from k5vision.operator_onvif import (
    _DEVICE,
    OnvifConnector,
    OnvifError,
    _Client,
    _endpoint,
    _find,
    _text,
)
from k5vision.operator_product_service import _private_configuration, _private_directory

_SERVER = re.compile(
    r"(?=.{1,253}\Z)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\.?\Z"
)
_CODES = frozenset(
    {
        "time_unavailable",
        "time_unsupported",
        "time_busy",
        "time_ntp_unreachable",
        "time_elevation_cancelled",
        "time_operation_failed",
        "time_operation_timeout",
        "camera_time_unsupported",
        "camera_time_invalid_response",
        "camera_time_unreachable",
        "camera_time_auth_failed",
    }
)


class TimeAdministrationError(RuntimeError):
    def __init__(self, code: str):
        self.code = code if code in _CODES else "time_operation_failed"
        super().__init__(self.code)


class _FileTime(ctypes.Structure):
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]


def _current_windows_issuer(kernel) -> dict:
    created, exited, system, user = (_FileTime() for _ in range(4))
    if not kernel.GetProcessTimes(
        kernel.GetCurrentProcess(),
        ctypes.byref(created),
        ctypes.byref(exited),
        ctypes.byref(system),
        ctypes.byref(user),
    ):
        raise TimeAdministrationError("time_unavailable")
    # FILETIME is kept as an integer throughout JSON/native admission. A
    # floating-point process creation time loses identity bits at this epoch.
    return {
        "owner_pid": os.getpid(),
        "owner_created_100ns": (int(created.high) << 32) | int(created.low),
    }


def windows_issuing_process() -> dict:
    if os.name != "nt":
        raise TimeAdministrationError("time_unsupported")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    kernel.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(_FileTime)] * 4
    kernel.GetProcessTimes.restype = ctypes.c_int
    return _current_windows_issuer(kernel)


def normalize_servers(values: object, *, allow_empty: bool = False) -> list[str]:
    if type(values) is not list or len(values) > 4 or (not values and not allow_empty):
        raise ValueError("NTP server list refused")
    result = []
    for value in values:
        if (
            type(value) is not str
            or not value
            or "%" in value
            or value != value.strip()
            or len(value) > 253
        ):
            raise ValueError("NTP server refused")
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            if _SERVER.fullmatch(value) is None or ":" in value:
                raise ValueError("NTP server refused") from None
            normalized = value.lower().rstrip(".")
        else:
            if address.is_multicast or address.is_unspecified:
                raise ValueError("NTP address refused")
            normalized = str(address)
        if normalized in result:
            raise ValueError("Duplicate NTP server")
        result.append(normalized)
    return result


def unavailable_reference() -> dict:
    return {
        "status": "unavailable",
        "offset_seconds": None,
        "uncertainty_seconds": None,
        "measured_at": None,
        "stratum": None,
        "sync_verified": False,
        "authenticated": False,
    }


def windows_configuration() -> dict:
    if os.name != "nt":
        return {
            "platform": sys.platform,
            "status": "unsupported",
            "service_running": False,
            "mode": "unknown",
            "ntp_servers": [],
        }
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Services\W32Time\Parameters"
        ) as key:
            mode = winreg.QueryValueEx(key, "Type")[0]
            raw = winreg.QueryValueEx(key, "NtpServer")[0]
        servers = normalize_servers(
            [entry.split(",", 1)[0] for entry in raw.split()], allow_empty=True
        )
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        advapi.OpenSCManagerW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
        advapi.OpenSCManagerW.restype = ctypes.c_void_p
        advapi.OpenServiceW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32]
        advapi.OpenServiceW.restype = ctypes.c_void_p
        advapi.QueryServiceStatus.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        advapi.QueryServiceStatus.restype = ctypes.c_int
        advapi.CloseServiceHandle.argtypes = [ctypes.c_void_p]
        manager = advapi.OpenSCManagerW(None, None, 1)
        if not manager:
            raise OSError
        service = None
        try:
            service = advapi.OpenServiceW(manager, "W32Time", 4)
            status = (ctypes.c_uint32 * 7)()
            if not service or not advapi.QueryServiceStatus(service, ctypes.byref(status)):
                raise OSError
            running = status[1] == 4
        finally:
            if service:
                advapi.CloseServiceHandle(service)
            advapi.CloseServiceHandle(manager)
        return {
            "platform": "windows",
            "status": "available",
            "service_running": running,
            "mode": {"NTP": "manual", "NT5DS": "domain"}.get(mode, "unknown"),
            # NtpServer is a dormant manual-peer value in domain mode; it is
            # not the selected domain-controller source and must not be used
            # to certify that source's offset.
            "ntp_servers": servers if mode == "NTP" else [],
        }
    except Exception:
        return {
            "platform": "windows",
            "status": "unavailable",
            "service_running": False,
            "mode": "unknown",
            "ntp_servers": [],
        }


def measure_reference(server: str) -> dict:
    """Own and bound DNS/native/socket work; never leave a timed-out DNS thread."""
    from k5vision.operator_native_process import _WindowsJob

    process = job = None
    try:
        job = _WindowsJob() if os.name == "nt" else None
        server = normalize_servers([server])[0]
        process = subprocess.Popen(
            [sys.executable, "-B", "-m", "k5vision.operator_time_probe"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            start_new_session=os.name != "nt",
            close_fds=True,
        )
        if job is not None:
            job.assign(process)
        output, _ = process.communicate((json.dumps({"server": server}) + "\n").encode(), timeout=5)
        if len(output) > 2048 or process.returncode != 0:
            raise ValueError
        result = json.loads(output)
        if (
            type(result) is not dict
            or set(result) != set(unavailable_reference())
            or result["status"] != "measured"
            or result["authenticated"] is not False
        ):
            raise ValueError
        offset, uncertainty = result["offset_seconds"], result["uncertainty_seconds"]
        observed = datetime.fromisoformat(result["measured_at"])
        if (
            type(offset) not in (int, float)
            or not math.isfinite(offset)
            or abs(offset) > 315_576_000
            or type(uncertainty) not in (int, float)
            or not math.isfinite(uncertainty)
            or not 0 <= uncertainty <= 15
            or observed.utcoffset() is None
            or type(result["stratum"]) is not int
            or not 1 <= result["stratum"] <= 15
            or type(result["sync_verified"]) is not bool
            or result["sync_verified"] is not (abs(offset) + uncertainty <= 2)
        ):
            raise ValueError
        return result
    except Exception:
        return unavailable_reference()
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)
        if job is not None:
            job.close()


def _elevated_helper(executable: Path, request: Path, response: Path) -> None:
    """Request only the supervisor's fixed time operation through native UAC."""
    if os.name != "nt":
        raise TimeAdministrationError("time_unsupported")
    from ctypes import wintypes

    class ShellExecuteInfo(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", ctypes.c_ulong),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", ctypes.c_void_p),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIcon", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    shell, kernel = (
        ctypes.WinDLL("shell32", use_last_error=True),
        ctypes.WinDLL("kernel32", use_last_error=True),
    )
    shell.ShellExecuteExW.argtypes = [ctypes.POINTER(ShellExecuteInfo)]
    shell.ShellExecuteExW.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    info = ShellExecuteInfo()
    info.cbSize, info.fMask, info.lpVerb, info.lpFile = (
        ctypes.sizeof(info),
        0x40 | 0x400,
        "runas",
        str(executable),
    )
    info.lpParameters, info.nShow = (
        subprocess.list2cmdline(["--time-admin", str(request), str(response)]),
        0,
    )
    if not shell.ShellExecuteExW(ctypes.byref(info)):
        raise TimeAdministrationError(
            "time_elevation_cancelled"
            if ctypes.get_last_error() == 1223
            else "time_operation_failed"
        )
    try:
        if not info.hProcess or kernel.WaitForSingleObject(info.hProcess, 45_000) != 0:
            raise TimeAdministrationError("time_operation_timeout")
    finally:
        if info.hProcess:
            kernel.CloseHandle(info.hProcess)


class WindowsTimeAdministrator:
    def __init__(
        self,
        profile: Path,
        *,
        read_configuration=windows_configuration,
        measure=measure_reference,
        invoke=None,
        executable: Path | None = None,
        owner_identity=windows_issuing_process,
    ):
        self.profile = Path(profile)
        self._read, self._measure = read_configuration, measure
        self._invoke = invoke or self._owned_elevation
        self._native_supported = invoke is not None or os.name == "nt"
        self._owner_identity = owner_identity
        self.executable = executable or Path(sys.executable).parent.parent / "K5Vision.exe"
        self._closed = threading.Event()
        self._ownership = threading.Lock()
        self._broker = None
        self._pending: set[Path] = set()

    def _owned_elevation(self, executable: Path, request: Path, response: Path):
        from k5vision.operator_native_process import _WindowsJob

        if os.name != "nt":
            raise TimeAdministrationError("time_unsupported")
        job, process = None, None
        try:
            job = _WindowsJob()
            with self._ownership:
                if self._closed.is_set():
                    raise TimeAdministrationError("time_unavailable")
                process = subprocess.Popen(
                    [sys.executable, "-B", "-m", "k5vision.operator_time_elevation"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    close_fds=True,
                )
                job.assign(process)
                self._broker = process
            raw = json.dumps(
                {"executable": str(executable), "request": str(request), "response": str(response)}
            )
            output, _ = process.communicate((raw + "\n").encode(), timeout=65)
            if len(output) > 256:
                raise ValueError
            result = json.loads(output)
            if type(result) is not dict or set(result) != {"code"}:
                raise ValueError
            if result["code"] != "completed" or process.returncode != 0:
                raise TimeAdministrationError(result["code"])
        except subprocess.TimeoutExpired:
            raise TimeAdministrationError("time_operation_timeout") from None
        finally:
            with self._ownership:
                if self._broker is process:
                    self._broker = None
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=3)
            if job is not None:
                job.close()

    def cancel_current(self):
        with self._ownership:
            # Remove admission tokens before ending a still-pending UAC broker.
            # A later accepted UAC prompt cannot authorize a stale request.
            for file in tuple(self._pending):
                try:
                    file.unlink(missing_ok=True)
                except OSError:
                    pass
            if self._broker is not None and self._broker.poll() is None:
                self._broker.kill()

    def close(self):
        self._closed.set()
        self.cancel_current()

    def status(self, *, reference: bool = True) -> dict:
        if self._closed.is_set():
            raise TimeAdministrationError("time_unavailable")
        host = dict(self._read())
        host["reference"] = unavailable_reference()
        if reference and host["mode"] == "manual" and host["ntp_servers"]:
            host["reference"] = self._measure(host["ntp_servers"][0])
        capable = os.name == "nt" and self.executable.is_file()
        return {
            "host": host,
            "capabilities": {"configure_ntp": capable, "resynchronize": capable},
            "absolute_accuracy_certified": False,
        }

    def operate(
        self, operation: str, *, mode: str = "manual", ntp_servers: list[str] | None = None
    ) -> dict:
        if self._closed.is_set():
            raise TimeAdministrationError("time_unavailable")
        if not self._native_supported:
            raise TimeAdministrationError("time_unsupported")
        if operation not in {"configure", "resync"} or mode not in {"manual", "domain"}:
            raise ValueError("Time operation refused")
        servers = normalize_servers(
            ntp_servers or [], allow_empty=mode == "domain" or operation == "resync"
        )
        try:
            root = _private_directory(self.profile / "time-admin")
            nonce = secrets.token_hex(16)
            request, response = (
                root / (".time-admin-" + nonce + ".json"),
                root / (".time-admin-" + nonce + ".result.json"),
            )
            owner = self._owner_identity()
            if (
                type(owner) is not dict
                or set(owner) != {"owner_pid", "owner_created_100ns"}
                or type(owner["owner_pid"]) is not int
                or not 1 <= owner["owner_pid"] <= 2**32 - 1
                or type(owner["owner_created_100ns"]) is not int
                or not 1 <= owner["owner_created_100ns"] <= 2**63 - 1
            ):
                raise ValueError
            payload = {
                "schema_version": 1,
                "operation": operation,
                "ntp_servers": servers,
                "mode": mode,
                "nonce": nonce,
                **owner,
            }
            _regular_file(self.executable)
            _windows_admission(self.executable, private=False)
            _private_configuration(request, json.dumps(payload, allow_nan=False))
        except Exception:
            raise TimeAdministrationError("time_unavailable") from None
        with self._ownership:
            self._pending.add(request)
        try:
            if self._closed.is_set():
                raise TimeAdministrationError("time_unavailable")
            if response.exists() or response.is_symlink():
                raise ValueError
            self._invoke(self.executable, request, response)
            info = _regular_file(response)
            if info.st_size > 2048:
                raise ValueError
            result = json.loads(response.read_bytes())
            _regular_file(response)
            if (
                type(result) is not dict
                or set(result) != {"schema_version", "nonce", "succeeded", "code"}
                or type(result["schema_version"]) is not int
                or result["schema_version"] != 1
                or result["nonce"] != nonce
                or type(result["succeeded"]) is not bool
                or result["code"]
                not in {
                    "completed",
                    "access_denied",
                    "invalid_request",
                    "unsupported",
                    "operation_failed",
                    "timeout",
                    "ntp_unreachable",
                }
            ):
                raise ValueError
            if result["succeeded"] is not True or result["code"] != "completed":
                raise TimeAdministrationError(
                    "time_operation_timeout"
                    if result["code"] == "timeout"
                    else "time_ntp_unreachable"
                    if result["code"] == "ntp_unreachable"
                    else "time_operation_failed"
                )
            status = self.status()
            configured = status["host"]
            if operation == "configure" and (
                configured["mode"] != mode
                or configured["service_running"] is not True
                or (mode == "manual" and configured["ntp_servers"] != servers)
            ):
                raise TimeAdministrationError("time_operation_failed")
            verified = configured["reference"]["sync_verified"] is True
            return {
                **status,
                "operation": {
                    "action": operation,
                    "status": "verified"
                    if verified
                    else "configured"
                    if operation == "configure"
                    else "completed",
                    "verified": verified,
                },
            }
        except TimeAdministrationError:
            raise
        except Exception:
            raise TimeAdministrationError("time_operation_failed") from None
        finally:
            with self._ownership:
                self._pending.discard(request)
            for file in (request, response):
                try:
                    _checked_path(file)
                    file.unlink(missing_ok=True)
                except Exception:
                    pass


def _camera_client(source, timeout: float, *, username=None, password=None):
    if DeviceProtocol.ONVIF not in source.device.protocols:
        raise OnvifError("unsupported")
    parsed = urlsplit(source.source_uri)
    if username is None and password is None:
        username, password = unquote(parsed.username or ""), unquote(parsed.password or "")
    if (
        type(username) is not str
        or type(password) is not str
        or len(username) > 256
        or len(password) > 256
        or any(ord(character) < 32 for character in username + password)
    ):
        raise OnvifError("invalid_response")
    host, port = str(source.device.host), source.device.management_port
    secure = "k5-onvif-https" in source.device.tags
    authority = f"[{host}]" if ":" in host else host
    endpoint = _endpoint(
        f"{'https' if secure else 'http'}://{authority}:{port}/onvif/device_service", host
    )
    client = _Client(host, username, password, time.monotonic() + timeout, require_secure=secure)
    client.synchronize_clock(endpoint)
    return client, endpoint


def _camera_status(client, endpoint: str, identifier: str) -> dict:
    # Authenticated read also supports cameras that restrict the anonymous
    # PRE_AUTH clock sample used for WS-Security compensation.
    started = time.monotonic()
    raw = client.request(endpoint, "<tds:GetSystemDateAndTime/>", _DEVICE, "GetSystemDateAndTime")
    finished = time.monotonic()
    utc = _find(raw, "UTCDateTime")
    if utc is None:
        raise OnvifError("invalid_response")
    date, clock = _find(utc, "Date"), _find(utc, "Time")
    if date is None or clock is None:
        raise OnvifError("invalid_response")
    observed = datetime.now(UTC)
    camera = datetime(
        int(_text(date, "Year")),
        int(_text(date, "Month")),
        int(_text(date, "Day")),
        int(_text(clock, "Hour")),
        int(_text(clock, "Minute")),
        int(_text(clock, "Second")),
        tzinfo=UTC,
    )
    mode = {"NTP": "ntp", "Manual": "manual"}.get(_text(raw, "DateTimeType"), "unknown")
    offset = (camera - observed).total_seconds() + max(0, finished - started) / 2
    uncertainty = 1.0 + max(0, finished - started) / 2
    ntp = {"mode": mode, "from_dhcp": False, "servers": [], "available": False}
    try:
        response = client.request(endpoint, "<tds:GetNTP/>", _DEVICE, "GetNTP")
        dhcp = _text(response, "FromDHCP")
        if dhcp not in {"true", "false", "1", "0"}:
            raise ValueError
        values = []
        for item in response.iter():
            if item.tag.rsplit("}", 1)[-1] in {"NTPManual", "NTPFromDHCP"}:
                value = (
                    _text(item, "IPv4Address")
                    or _text(item, "IPv6Address")
                    or _text(item, "DNSname")
                )
                values.append(value)
        ntp = {
            "mode": mode,
            "from_dhcp": dhcp in {"true", "1"},
            "servers": normalize_servers(values[:4], allow_empty=True),
            "available": True,
        }
    except (OnvifError, ValueError):
        pass
    return {
        "camera_id": identifier,
        "clock": {
            "status": "measured",
            "offset_seconds": round(offset, 3),
            "uncertainty_seconds": round(uncertainty, 3),
            "measured_at": observed.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "reported_mode": mode,
            "sync_verified": False,
            "relative_alignment_verified": abs(offset) + uncertainty <= 2,
        },
        "ntp": ntp,
        "capabilities": {"configure_ntp": ntp["available"], "synchronize_to_host": True},
        "absolute_sync_verified": False,
    }


def _camera_operation(source, action: str, payload: dict, timeout: float):
    client, endpoint = _camera_client(
        source, timeout, username=payload.get("username"), password=payload.get("password")
    )
    identifier = str(source.binding.device_id)
    if action == "read":
        return _camera_status(client, endpoint, identifier)
    current = client.request(
        endpoint, "<tds:GetSystemDateAndTime/>", _DEVICE, "GetSystemDateAndTime"
    )
    daylight = _text(current, "DaylightSavings")
    if daylight not in {"true", "false", "0", "1"}:
        raise OnvifError("invalid_response")
    daylight = "true" if daylight in {"true", "1"} else "false"
    zone = _find(current, "TimeZone")
    timezone = "" if zone is None else _text(zone, "TZ")
    if len(timezone) > 128 or any(ord(value) < 32 for value in timezone):
        raise OnvifError("invalid_response")
    timezone_xml = (
        f"<tds:TimeZone><tt:TZ>{escape(timezone)}</tt:TZ></tds:TimeZone>" if timezone else ""
    )
    if action == "configure":
        servers = normalize_servers(payload["ntp_servers"], allow_empty=payload["from_dhcp"])
        manual = ""
        for server in servers:
            try:
                address = ipaddress.ip_address(server)
                tag, kind = (
                    ("IPv4Address", "IPv4") if address.version == 4 else ("IPv6Address", "IPv6")
                )
            except ValueError:
                tag, kind = "DNSname", "DNS"
            manual += (
                f"<tds:NTPManual><tt:Type>{kind}</tt:Type>"
                f"<tt:{tag}>{escape(server)}</tt:{tag}></tds:NTPManual>"
            )
        operation = (
            "<tds:SetNTP>"
            f"<tds:FromDHCP>{str(payload['from_dhcp']).lower()}</tds:FromDHCP>"
            f"{manual}</tds:SetNTP>"
        )
        client.request(endpoint, operation, _DEVICE, "SetNTP")
        client.request(
            endpoint,
            "<tds:SetSystemDateAndTime><tds:DateTimeType>NTP</tds:DateTimeType>"
            f"<tds:DaylightSavings>{daylight}</tds:DaylightSavings>"
            f"{timezone_xml}</tds:SetSystemDateAndTime>",
            _DEVICE,
            "SetSystemDateAndTime",
        )
        result = _camera_status(client, endpoint, identifier)
        ntp = result["ntp"]
        if (
            ntp["mode"] != "ntp"
            or ntp["from_dhcp"] is not payload["from_dhcp"]
            or (not payload["from_dhcp"] and ntp["servers"] != servers)
        ):
            raise OnvifError("invalid_response")
        result["operation"] = {"action": "configure", "status": "configured", "verified": False}
        return result
    if action != "synchronize":
        raise OnvifError("unsupported")
    now = datetime.now(UTC)
    operation = (
        "<tds:SetSystemDateAndTime><tds:DateTimeType>Manual</tds:DateTimeType>"
        f"<tds:DaylightSavings>{daylight}</tds:DaylightSavings>{timezone_xml}"
        f"<tds:UTCDateTime><tt:Time><tt:Hour>{now.hour}</tt:Hour>"
        f"<tt:Minute>{now.minute}</tt:Minute><tt:Second>{now.second}</tt:Second>"
        f"</tt:Time><tt:Date><tt:Year>{now.year}</tt:Year><tt:Month>{now.month}</tt:Month>"
        f"<tt:Day>{now.day}</tt:Day></tt:Date></tds:UTCDateTime></tds:SetSystemDateAndTime>"
    )
    client.request(endpoint, operation, _DEVICE, "SetSystemDateAndTime")
    # A successful write does not prove the camera accepted the requested time.
    client._camera_clock = None
    client.synchronize_clock(endpoint)
    result = _camera_status(client, endpoint, identifier)
    verified = result["clock"]["relative_alignment_verified"] is True
    result["operation"] = {
        "action": "synchronize",
        "status": "verified" if verified else "completed",
        "verified": verified,
    }
    return result


class TimeAdministrationService:
    def __init__(self, profile: Path, *, host=None, connector=None):
        self.host = host or WindowsTimeAdministrator(profile)
        self.connector = connector or OnvifConnector(timeout_seconds=15)
        self._host_lock = threading.Lock()
        self._closed = False
        self._tasks: set[asyncio.Task] = set()

    async def host_status(self):
        return await self._host_call(self.host.status)

    async def host_operation(self, operation: str, **options):
        return await self._host_call(lambda: self.host.operate(operation, **options))

    async def _host_call(self, function):
        if self._closed:
            raise TimeAdministrationError("time_unavailable")
        if not self._host_lock.acquire(blocking=False):
            raise TimeAdministrationError("time_busy")

        def owned():
            try:
                if self._closed:
                    raise TimeAdministrationError("time_unavailable")
                return function()
            finally:
                self._host_lock.release()

        try:
            task = asyncio.create_task(asyncio.to_thread(owned))
        except BaseException:
            self._host_lock.release()
            raise
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return await self._owned_result(task, cancel=getattr(self.host, "cancel_current", None))

    @staticmethod
    async def _owned_result(task, *, cancel=None):
        """A cancelled HTTP request cannot release its mutation lease mid-write."""
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if cancel is not None:
                cancel()
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not task.cancelled():
                task.exception()
            raise

    async def camera(self, source, action: str = "read", **payload):
        if self._closed:
            raise TimeAdministrationError("time_unavailable")
        try:
            task = asyncio.create_task(
                self.connector._run(_camera_operation, source, action, payload, 15.0)
            )
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return await self._owned_result(task)
        except OnvifError as error:
            raise TimeAdministrationError(
                {
                    "auth_failed": "camera_time_auth_failed",
                    "unsupported": "camera_time_unsupported",
                    "unreachable": "camera_time_unreachable",
                    "busy": "time_busy",
                }.get(error.code, "camera_time_invalid_response")
            ) from None

    async def aclose(self):
        self._closed = True
        close = getattr(self.host, "close", None)
        if close is not None:
            close()
        active = tuple(self._tasks) + tuple(getattr(self.connector, "_tasks", ()))
        if active:
            await asyncio.gather(*(asyncio.shield(task) for task in active), return_exceptions=True)
