"""Diagnostic-only read-only attribution; original cohort rejection is unchanged."""

from __future__ import annotations

import ctypes
import hashlib
import os
import stat
import threading
import time
from ctypes import wintypes
from pathlib import Path, PureWindowsPath

OS_IMAGES = {
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
    "pythonw.exe": "pythonw",
}
OS_CANDIDATES = set(OS_IMAGES) - {"crashpad_handler.exe", "pythonw.exe"}


def finite_error(error):
    result = {
        "kind": type(error).__name__
        if type(error).__name__
        in {
            "OSError",
            "PermissionError",
            "ProcessLookupError",
            "ValueError",
            "RuntimeError",
            "AttributeError",
            "TypeError",
            "ArgumentError",
        }
        else "other"
    }
    for name in ("winerror", "errno", "hresult"):
        value = getattr(error, name, None)
        if type(value) is int:
            result[name] = value
    return result


def offline_signature(path):
    class GUID(ctypes.Structure):
        _fields_ = [
            ("a", ctypes.c_uint32),
            ("b", ctypes.c_uint16),
            ("c", ctypes.c_uint16),
            ("d", ctypes.c_ubyte * 8),
        ]

    class FileInfo(ctypes.Structure):
        _fields_ = [
            ("size", ctypes.c_uint32),
            ("path", ctypes.c_wchar_p),
            ("file", ctypes.c_void_p),
            ("subject", ctypes.c_void_p),
        ]

    class TrustData(ctypes.Structure):
        _fields_ = [
            ("size", ctypes.c_uint32),
            ("policy", ctypes.c_void_p),
            ("sip", ctypes.c_void_p),
            ("ui", ctypes.c_uint32),
            ("revoke", ctypes.c_uint32),
            ("choice", ctypes.c_uint32),
            ("file", ctypes.c_void_p),
            ("action", ctypes.c_uint32),
            ("state", ctypes.c_void_p),
            ("url", ctypes.c_wchar_p),
            ("flags", ctypes.c_uint32),
            ("context", ctypes.c_uint32),
            ("settings", ctypes.c_void_p),
        ]

    library = ctypes.WinDLL("wintrust", use_last_error=True)
    verify = library.WinVerifyTrust
    verify.argtypes = [ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(TrustData)]
    verify.restype = ctypes.c_int32
    action = GUID(
        0x00AAC56B,
        0xCD44,
        0x11D0,
        (ctypes.c_ubyte * 8)(0x8C, 0xC2, 0, 0xC0, 0x4F, 0xC2, 0x95, 0xEE),
    )
    file = FileInfo(ctypes.sizeof(FileInfo), str(path), None, None)
    trust = TrustData()
    trust.size, trust.ui, trust.choice, trust.action = ctypes.sizeof(trust), 2, 1, 1
    trust.file = ctypes.cast(ctypes.pointer(file), ctypes.c_void_p)
    trust.flags = 0x1010  # Cache-only URL retrieval; no revocation network requests.
    try:
        status = verify(ctypes.c_void_p(-1), ctypes.byref(action), ctypes.byref(trust))
        return {
            "offline_checked": True,
            "verified": status == 0,
            "status_uint32": status & 0xFFFFFFFF,
        }
    finally:
        trust.action = 2
        verify(ctypes.c_void_p(-1), ctypes.byref(action), ctypes.byref(trust))


def fixed_os_file_facts(image, system_directory):
    """Read only a recognized fixed System32 file; never return a path."""
    import windows_process_identity as helpers

    from k5vision.installed_code_security import _code_policy, _native_security
    from k5vision.windows_identity_security import check_windows_volume

    basename = PureWindowsPath(image).name.casefold()
    result = {"category": OS_IMAGES.get(basename, "other"), "fixed_system32_candidate": False}
    if basename not in OS_CANDIDATES:
        return result
    form, actual = helpers._local_image_path(image)
    expected = PureWindowsPath(system_directory) / basename
    if actual is None or form not in {"normal_drive", "extended_drive"}:
        return result
    result["fixed_system32_candidate"] = True
    check_windows_volume(str(expected))
    if not helpers._same_local_file(str(actual), str(expected)):
        result["system32_same_file_id"] = False
        return result
    native = _native_security()
    for component in reversed((expected, *expected.parents)):
        info = Path(component).lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("OS observation refuses reparse code")
        _code_policy(native, component, within=component.is_relative_to(expected.parent))
    with Path(expected).open("rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= 32 * 1024**2:
            raise ValueError("OS observation file size/type differs")
        sha = hashlib.file_digest(stream, "sha256").hexdigest()
        after = os.fstat(stream.fileno())
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ) or not helpers._same_local_file(str(actual), str(expected)):
        raise ValueError("OS observation file identity changed")
    result.update(
        system32_same_file_id=True,
        trusted_code_custody_verified=True,
        file_identity={"volume": before.st_dev, "file_id": before.st_ino, "links": before.st_nlink},
        file_bytes=before.st_size,
        file_sha256=sha,
    )
    try:
        result["signature"] = offline_signature(Path(expected))
    except Exception as error:
        result["signature_error"] = finite_error(error)
    return result


class ProcessObserver:
    def __init__(self, cohort):
        self.cohort, self.native = cohort, cohort.native
        self.lock = threading.RLock()
        self.pins, self.details, self.rejections, self.errors = {}, {}, [], []
        self.started = time.monotonic()
        self.closed = False

    def pin(self, identity, role):
        if self.closed or identity in self.pins or len(self.pins) >= 256:
            return
        handle = self.native.api.OpenProcess(0x00101000, False, identity[0])
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        transferred = False
        try:
            if self.handle_birth(handle) != identity[1]:
                raise ProcessLookupError("Original diagnostic process changed")
            self.pins[identity] = {"handle": handle, "role": role}
            transferred = True
        finally:
            if not transferred and not self.native.api.CloseHandle(handle):
                self.error(ctypes.WinError(ctypes.get_last_error()), "close_untransferred_query")

    def handle_birth(self, handle):
        # The original helper intentionally returns None after process exit.
        # Read creation FILETIME on the retained original object, including exit.
        filetime = self.native.api.GetProcessTimes.argtypes[1]._type_
        created, exited, kernel, user = (filetime() for _ in range(4))
        if not self.native.api.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return (created.high << 32) | created.low

    def exit(self, identity):
        pin = self.pins.get(identity)
        if pin is None:
            return {"pinned": False}
        if self.handle_birth(pin["handle"]) != identity[1]:
            return {"pinned": True, "birth_match": False}
        state = self.native.api.WaitForSingleObject(pin["handle"], 0)
        result = {"pinned": True, "birth_match": True, "exited": state == 0}
        if state == 258:
            result["exit_code_uint32"] = None
        elif state == 0:
            value = wintypes.DWORD()
            if not self.native.api.GetExitCodeProcess(pin["handle"], ctypes.byref(value)):
                raise ctypes.WinError(ctypes.get_last_error())
            result["exit_code_uint32"] = value.value
            result["nt_status_category"] = {
                0xC0000005: "access_violation",
                0xC000001D: "illegal_instruction",
                0xC0000409: "fail_fast",
                0xC0000374: "heap_corruption",
                0x40000015: "fatal_app_exit",
            }.get(value.value, "other")
        else:
            raise ctypes.WinError(ctypes.get_last_error())
        return result

    def error(self, error, phase):
        if len(self.errors) < 16:
            self.errors.append({"phase": phase, **finite_error(error)})

    def admitted(self, identity, note, role):
        if (
            role == "service"
            and note.get("accepted")
            and note.get("kind") in {"wrapper", "host", "python", "mediamtx", "caddy"}
        ):
            with self.lock:
                self.pin(identity, note["kind"])

    def observed_details(self, identity, details):
        with self.lock:
            if len(self.details) < 512:
                parent = details["parent_pid"]
                private = {
                    "image": details["image"],
                    "parent_pid": parent,
                    "parent_identity": None,
                }
                self.details[identity] = private
                birth = self.native.birth(parent) if parent else None
                parent_identity = (parent, birth)
                if parent_identity in self.pins:
                    private["parent_identity"] = parent_identity

    def rejected(self, identity, diagnostic):
        if diagnostic.get("outcome") != "unexpected_service_image" or len(self.rejections) >= 16:
            return
        with self.lock:
            private = self.details.get(identity)
            if private is None:
                return
            parent = private["parent_identity"]
            note = {
                "pid": identity[0],
                "created_100ns": identity[1],
                "category": OS_IMAGES.get(
                    PureWindowsPath(private["image"]).name.casefold(), "other"
                ),
                "elapsed_seconds": round(time.monotonic() - self.started, 4),
                "admission_remains_rejected": True,
                "os_file_observation": "pending",
                "child_at_rejection": {"pinned": False},
                "parent_identity_bound": parent is not None,
                "parent_pid_observed": private["parent_pid"],
                "parent_binding": "exact_live_birth_pinned"
                if parent is not None
                else "unavailable_or_unpinned",
            }
            try:
                self.pin(identity, "rejected_child")
                note["child_at_rejection"] = self.exit(identity)
            except Exception as error:
                note["child_pin_error"] = finite_error(error)
            if parent is not None:
                note.update(
                    parent_pid=parent[0],
                    parent_created_100ns=parent[1],
                    parent_payload_role=self.pins[parent]["role"],
                )
                try:
                    note["parent_at_rejection"] = self.exit(parent)
                except Exception as error:
                    note["parent_exit_error"] = finite_error(error)
            self.rejections.append({"public": note, "child": identity, "parent": parent})

        # OS trust work is observational and runs outside the admission/cleanup path.
        def file_observation():
            try:
                facts = fixed_os_file_facts(private["image"], self.native.system_directory())
                with self.lock:
                    note["os_file"] = facts
                    note["os_file_observation"] = "completed"
            except Exception as error:
                with self.lock:
                    note["os_file_error"] = finite_error(error)
                    note["os_file_observation"] = "unavailable"

        threading.Thread(target=file_observation, daemon=True).start()

    def checkpoint(self, phase):
        with self.lock:
            for row in self.rejections:
                for label in ("child", "parent"):
                    if row[label] is not None:
                        row["public"][label + "_" + phase] = self.exit(row[label])

    def safe_checkpoint(self, phase):
        try:
            self.checkpoint(phase)
        except Exception as error:
            self.error(error, phase)

    def finalize(self):
        self.safe_checkpoint("final_after_fixture_cleanup")
        self.close()
        return self.receipt()

    def close(self):
        with self.lock:
            if self.closed:
                return
            for pin in self.pins.values():
                if not self.native.api.CloseHandle(pin["handle"]):
                    self.error(ctypes.WinError(ctypes.get_last_error()), "close_query_handle")
            self.closed = True

    def receipt(self):
        with self.lock:
            return {
                "diagnostic_only": True,
                "normal_admission_changed": False,
                "rights": "QUERY_LIMITED_INFORMATION|SYNCHRONIZE",
                "blocking_waits_or_terminations": False,
                "exit_poll_timeout_ms": 0,
                "rejections": [dict(row["public"]) for row in self.rejections],
                "observation_errors": list(self.errors),
                "query_handles_closed": self.closed,
            }


def install(cohort):
    """Wrap one diagnostic cohort; every original return/exception is preserved."""
    if os.name != "nt" or os.environ.get("K5_MOTION_DIAGNOSTIC_ONLY") != "1":
        raise RuntimeError("Service image observation requires the isolated Windows diagnostic")
    observer = ProcessObserver(cohort)
    details, record, admit, finish = (
        cohort.native.details,
        cohort._record_ownership,
        cohort._admit_service,
        cohort.finish,
    )

    def observe_details(identity):
        result = details(identity)
        try:
            observer.observed_details(identity, result)
        except Exception as error:
            observer.error(error, "cache_private_identity")
        return result

    def observe_record(identity, note, *, role):
        result = record(identity, note, role=role)
        try:
            observer.admitted(identity, note, role)
        except Exception as error:
            observer.error(error, "pin_admitted_process")
        return result

    def observe_admit(identity, *, root=False):
        try:
            return admit(identity, root=root)
        except BaseException as error:
            try:
                observer.rejected(identity, getattr(error, "diagnostic", {}))
            except Exception as diagnostic_error:
                observer.error(diagnostic_error, "observe_original_rejection")
            raise

    def observe_finish():
        observer.safe_checkpoint("before_cohort_finish")
        try:
            return finish()
        finally:
            observer.safe_checkpoint("after_cohort_finish")

    cohort.native.details, cohort._record_ownership = observe_details, observe_record
    cohort._admit_service, cohort.finish = observe_admit, observe_finish
    return observer
