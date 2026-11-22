"""Read-only, finite observations for one unchanged native time journey.

Private capabilities, response documents, paths and HTTP bodies stay in memory.
Native query handles never authorize a process mutation or extend a deadline.
"""

from __future__ import annotations

import ctypes
import json
import math
import re
import threading
from ctypes import wintypes
from pathlib import Path
from urllib.error import HTTPError

_RESULT_CODES = {
    "completed",
    "access_denied",
    "invalid_request",
    "unsupported",
    "operation_failed",
    "timeout",
    "ntp_unreachable",
}
_DETAIL_CODES = {
    "time_unavailable",
    "time_unsupported",
    "time_configuration_refused",
    "time_elevation_cancelled",
    "time_operation_failed",
    "time_operation_timeout",
    "time_ntp_unreachable",
    "time_rate_limit",
}
_ERROR_TYPES = {
    "AssertionError",
    "Failed",
    "TimeAdministrationError",
    "WindowsVmClockError",
    "OSError",
    "PermissionError",
    "FileNotFoundError",
    "RuntimeError",
    "TimeoutError",
    "TimeoutExpired",
    "HTTPError",
    "URLError",
}


def finite_error(error):
    result = {"type": type(error).__name__ if type(error).__name__ in _ERROR_TYPES else "other"}
    code = getattr(error, "code", None)
    if isinstance(code, str) and code in _DETAIL_CODES:
        result["code"] = code
    native = getattr(error, "winerror", None)
    if type(native) is int and 0 <= native <= 65535:
        result["win32_error"] = native
    return result


def _choice(value, choices):
    return value if type(value) is str and value in choices else "unrecognized"


def host_projection(value):
    if type(value) is not dict:
        return {"observed": False}
    servers = value.get("ntp_servers")
    return {
        "observed": True,
        "status": _choice(value.get("status"), {"available", "unavailable", "unsupported"}),
        "mode": _choice(value.get("mode"), {"manual", "domain", "unknown"}),
        "service_running": value.get("service_running") is True,
        "server_count": min(len(servers), 5) if type(servers) is list else None,
        "matches_requested_peer": servers == ["time.cloudflare.com"],
    }


def reference_projection(value):
    if type(value) is not dict:
        return {"observed": False}
    result = {
        "observed": True,
        "status": _choice(value.get("status"), {"measured", "unavailable"}),
        "sync_verified": value.get("sync_verified") is True,
        "authenticated": value.get("authenticated") is True,
    }
    for key, bound in (("offset_seconds", 315576000), ("uncertainty_seconds", 15)):
        number = value.get(key)
        result[key] = (
            number
            if type(number) in (int, float) and math.isfinite(number) and abs(number) <= bound
            else None
        )
    result["stratum"] = (
        value.get("stratum")
        if type(value.get("stratum")) is int and 1 <= value["stratum"] <= 15
        else None
    )
    return result


def guard_projection(value):
    if type(value) is not dict:
        return {"observed": False}
    result = {"observed": True}
    for key in (
        "clock_restored",
        "services_restored",
        "time_configuration_restored",
        "configuration_watchdog",
        "watchdog_armed",
        "privilege_verified",
    ):
        result[key] = value.get(key) is True
    result["reason"] = _choice(
        value.get("reason"),
        {
            "closed",
            "parent_closed",
            "watchdog_timeout",
            "error",
            "restore_failed",
            "configuration_restore_failed",
            "foreground_fallback",
        },
    )
    restored = value.get("last_restoration")
    if type(restored) is dict:
        result["restoration"] = {"restored": restored.get("restored") is True}
        for key in ("baseline_residual_seconds", "residual_seconds", "elapsed_seconds"):
            number = restored.get(key)
            result["restoration"][key] = (
                number
                if type(number) in (int, float)
                and math.isfinite(number)
                and abs(number) <= 315576000
                else None
            )
    return result


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError
        value[key] = item
    return value


def result_projection(value, nonce):
    if (
        type(value) is not dict
        or set(value) != {"schema_version", "nonce", "succeeded", "code"}
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["nonce"] != nonce
        or type(value["succeeded"]) is not bool
        or type(value["code"]) is not str
        or value["code"] not in _RESULT_CODES
    ):
        return None
    return {"observed": True, "succeeded": value["succeeded"], "code": value["code"]}


class _FileTime(ctypes.Structure):
    _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]


class _FileInfo(ctypes.Structure):
    _fields_ = [
        ("attributes", wintypes.DWORD),
        ("created", _FileTime),
        ("accessed", _FileTime),
        ("written", _FileTime),
        ("volume", wintypes.DWORD),
        ("size_high", wintypes.DWORD),
        ("size_low", wintypes.DWORD),
        ("links", wintypes.DWORD),
        ("index_high", wintypes.DWORD),
        ("index_low", wintypes.DWORD),
    ]


class SharedReader:
    """Read only an ordinary local leaf while permitting product deletion."""

    def __init__(self):
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        self.api.CreateFileW.restype = wintypes.HANDLE
        self.api.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(_FileInfo)]
        self.api.GetFileInformationByHandle.restype = wintypes.BOOL
        self.api.ReadFile.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p,
        ]
        self.api.ReadFile.restype = wintypes.BOOL
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.api.CloseHandle.restype = wintypes.BOOL

    def read(self, path, maximum):
        # OPEN_REPARSE_POINT; GENERIC_READ; FILE_SHARE_READ|WRITE|DELETE.
        handle = self.api.CreateFileW(str(path), 0x80000000, 7, None, 3, 0x00200000, None)
        if handle in (None, ctypes.c_void_p(-1).value):
            code = ctypes.get_last_error()
            return None, "absent" if code in {
                2,
                3,
            } else "sharing" if code == 32 else "access_denied" if code == 5 else "other"
        try:
            before = _FileInfo()
            if not self.api.GetFileInformationByHandle(handle, ctypes.byref(before)):
                return None, "identity_unavailable"
            size = (before.size_high << 32) | before.size_low
            if before.attributes & (0x10 | 0x400) or before.links != 1 or not 0 < size <= maximum:
                return None, "invalid_file"
            buffer, count = ctypes.create_string_buffer(maximum + 1), wintypes.DWORD()
            if not self.api.ReadFile(handle, buffer, maximum + 1, ctypes.byref(count), None):
                return None, "read_failed"
            after = _FileInfo()
            if not self.api.GetFileInformationByHandle(handle, ctypes.byref(after)):
                return None, "identity_unavailable"

            def identity(info):
                return (
                    info.volume,
                    info.index_high,
                    info.index_low,
                    info.size_high,
                    info.size_low,
                    info.written.high,
                    info.written.low,
                )

            if identity(before) != identity(after) or count.value != size:
                return None, "file_changed"
            return json.loads(buffer.raw[: count.value], object_pairs_hook=_unique), None
        except (ValueError, UnicodeError):
            return None, "invalid_json"
        finally:
            self.api.CloseHandle(handle)


class TimeObserver:
    def __init__(self, module):
        self.module = module
        self.operations, self.trackers = [], []
        self.guard = {"observed": False}
        self.lock = threading.Lock()
        self.current = None

    def _configuration(self):
        try:
            return host_projection(self.module.windows_configuration())
        except Exception as error:
            return {"observed": False, "error": finite_error(error)}

    def begin(self, operation, profile, service):
        state = {
            "operation": operation,
            "before_readback": self._configuration(),
            "http": {"observed": False},
            "native_result": {"observed": False},
            "request_bound_to_backend": False,
            "observer_errors": {},
            "trackers": [],
        }
        private = {
            "directory": Path(profile) / "identity" / "time-admin",
            "stop": threading.Event(),
            "state": state,
            "nonce": None,
            "response": None,
        }
        try:
            observed = service.observe()
            owner = self.module._process_issuer(observed["backend_pid"])
            private["owner"] = owner
            state["backend_before"] = owner
        except Exception as error:
            state["backend_before_error"] = finite_error(error)
        self.operations.append(state)
        self.current = private
        private["thread"] = threading.Thread(target=self._watch, args=(private,), daemon=True)
        private["thread"].start()

    def _watch(self, private):
        state = private["state"]
        try:
            reader = SharedReader()
            while not private["stop"].is_set():
                if private["nonce"] is None:
                    requests = [
                        p
                        for p in private["directory"].glob(".time-admin-*.json")
                        if re.fullmatch(r"[.]time-admin-[a-f0-9]{32}[.]json", p.name)
                    ]
                    if len(requests) == 1:
                        request, error = reader.read(requests[0], 4096)
                        if request is not None and private.get("owner") is not None:
                            owner = private["owner"]
                            nonce = request.get("nonce") if type(request) is dict else None
                            if (
                                type(nonce) is str
                                and re.fullmatch(r"[a-f0-9]{32}", nonce)
                                and request.get("operation") == state["operation"]
                                and all(request.get(k) == v for k, v in owner.items())
                                and requests[0].name == ".time-admin-" + nonce + ".json"
                            ):
                                private["nonce"] = nonce
                                private["response"] = requests[0].with_name(
                                    ".time-admin-" + nonce + ".result.json"
                                )
                                state["request_bound_to_backend"] = True
                        self._read_error(state, error)
                if private["response"] is not None and not state["native_result"]["observed"]:
                    result, error = reader.read(private["response"], 2048)
                    self._read_error(state, error)
                    projected = (
                        result_projection(result, private["nonce"]) if result is not None else None
                    )
                    if projected is not None:
                        state["native_result"] = projected
                private["stop"].wait(0.005)
        except Exception as error:
            state["observer_thread_error"] = finite_error(error)

    @staticmethod
    def _read_error(state, code):
        if code is not None:
            errors = state["observer_errors"]
            errors[code] = min(65535, errors.get(code, 0) + 1)

    def note_http(self, response):
        state = self.current["state"]
        status = getattr(response, "code", None)
        state["http"] = {
            "observed": True,
            "status": status if type(status) is int and 100 <= status <= 599 else None,
        }
        original = response.read

        def read(*args, **kwargs):
            raw = original(*args, **kwargs)
            try:
                if type(raw) is bytes and 0 < len(raw) <= 16384:
                    value = json.loads(raw, object_pairs_hook=_unique)
                    detail = value.get("detail") if type(value) is dict else None
                    state["http"]["detail"] = (
                        detail
                        if type(detail) is str and detail in _DETAIL_CODES
                        else "unrecognized_or_absent"
                    )
                    if type(value) is dict and type(value.get("host")) is dict:
                        state["http"]["host_readback"] = host_projection(value["host"])
                        state["http"]["reference"] = reference_projection(
                            value["host"].get("reference")
                        )
            except Exception as error:
                state["http"]["projection_error"] = finite_error(error)
            return raw

        try:
            response.read = read
        except Exception as error:
            state["http"]["observer_attach_error"] = finite_error(error)
        return response

    def request(self, original, client, operation):
        observer = self

        class Opener:
            def open(self, *args, **kwargs):
                try:
                    response = client.opener.open(*args, **kwargs)
                except HTTPError as error:
                    observer.note_http(error)
                    raise
                return observer.note_http(response)

        class Client:
            origin, token, opener = client.origin, client.token, Opener()

        return original(Client(), operation)

    def end(self, service, error):
        private, state = self.current, self.current["state"]
        private["stop"].set()
        private["thread"].join(timeout=1)
        state["observer_thread_joined"] = not private["thread"].is_alive()
        state["after_readback"] = self._configuration()
        state["operation_error"] = finite_error(error) if error is not None else None
        for tracker in self.trackers:
            if tracker.phase_state is state:
                tracker.observe_exits("operation_end_before_cleanup")
        try:
            state["backend_after"] = self.module._process_issuer(service.observe()["backend_pid"])
        except Exception as backend_error:
            state["backend_after_error"] = finite_error(backend_error)

    def tracker_class(self, original):
        observer = self

        class Tracker(original):
            def __init__(self, *args):
                super().__init__(*args)
                self.pins = {}
                self.record = {"processes": [], "pin_errors": 0}
                self.phase_state = observer.current["state"]
                self.phase_state["trackers"].append(self.record)
                observer.trackers.append(self)

            def sample(self):
                super().sample()
                for kind, identities in (("helper", self.helpers), ("command", self.commands)):
                    for identity in identities:
                        if identity in self.pins:
                            continue
                        handle = self.native.api.OpenProcess(0x00101000, False, identity[0])
                        try:
                            if (
                                not handle
                                or self.native.birth(identity[0], handle=handle) != identity[1]
                            ):
                                self.record["pin_errors"] += 1
                                continue
                            record = {
                                "kind": kind,
                                "pid": identity[0],
                                "created_100ns": identity[1],
                            }
                            self.record["processes"].append(record)
                            self.pins[identity] = (handle, record)
                            handle = None
                        except Exception as error:
                            self.record["pin_errors"] += 1
                            self.record["last_pin_error"] = finite_error(error)
                        finally:
                            if handle:
                                self.native.api.CloseHandle(handle)

            def observe_exits(self, phase):
                for handle, record in self.pins.values():
                    try:
                        state = self.native.api.WaitForSingleObject(handle, 0)
                        if state == 0:
                            code = wintypes.DWORD()
                            if self.native.api.GetExitCodeProcess(handle, ctypes.byref(code)):
                                record[phase] = {
                                    "observed": True,
                                    "exited": True,
                                    "exit_code": code.value,
                                }
                            else:
                                record[phase] = {"observed": False}
                        elif state == 258:
                            record[phase] = {"observed": True, "exited": False, "exit_code": None}
                        else:
                            record[phase] = {"observed": False}
                    except Exception as error:
                        record[phase] = {"observed": False, "error": finite_error(error)}

            def finish_observation(self):
                try:
                    self.observe_exits("after_fixture_cleanup")
                finally:
                    for handle, _record in self.pins.values():
                        self.native.api.CloseHandle(handle)
                self.pins.clear()

        return Tracker

    def guard_class(self, original):
        observer = self

        class Guard(original):
            def __exit__(self, *args):
                try:
                    return super().__exit__(*args)
                finally:
                    observer.guard = guard_projection(self.result)

        return Guard

    def finish(self):
        if self.current is not None:
            self.current["stop"].set()
            self.current["thread"].join(timeout=1)
        for tracker in self.trackers:
            tracker.finish_observation()
        return {
            "operations": self.operations,
            "guard_restoration": self.guard,
            "diagnostic_only": True,
            "normal_qualification": False,
        }
