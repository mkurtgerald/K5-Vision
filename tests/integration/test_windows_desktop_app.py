"""Actual Windows installer, account screen, browser and tray qualification.

All input is generated and temporary. Evidence contains finite outcomes, never
passwords, private paths, camera URLs, screenshots or media. Real Win32 windows
are exercised; no WinForms or process behavior is mocked.
"""

from __future__ import annotations

import ctypes
import hashlib
import importlib.util
import json
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from uuid import UUID, uuid4

import psutil
import pytest

from k5vision import identity_state
from k5vision.media.mediamtx_provider import MediaMTXBinding

_INPUTS = (
    "K5_DESKTOP_SETUP",
    "K5_MEDIAMTX_DONOR_EXE",
    "K5_CADDY_DONOR_EXE",
    "K5_DONOR_FFMPEG",
    "K5_DONOR_FFPROBE",
    "K5_OPERATOR_PERSON_FIXTURE",
)
pytestmark = pytest.mark.skipif(
    os.name != "nt" or not all(os.getenv(name) for name in _INPUTS),
    reason="Desktop qualification requires Windows and explicitly supplied installer/tools",
)
_TITLE = "K5 Vision"
_PASSWORD, _CONFIRM, _CREATE, _OPEN, _STOP, _STATUS, _USERNAME = (
    1001,
    1002,
    1003,
    1004,
    1005,
    1006,
    1008,
)
_DESKTOP_CAMERAS = (
    ("Generated desktop camera", "127.0.0.1", "red"),
    ("Generated desktop blue camera", "127.0.0.2", "blue"),
    ("Generated desktop lime camera", "127.0.0.1", "lime"),
    ("Generated desktop yellow camera", "127.0.0.2", "yellow"),
)


def _require(condition, message):
    __tracebackhide__ = True
    if not condition:
        pytest.fail(message, pytrace=False)


def _helpers(name):
    spec = importlib.util.spec_from_file_location(
        "k5_desktop_" + name, Path(__file__).with_name(name + ".py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Windows:
    """Bounded Win32 access to actual windows and stable native control IDs."""

    def __init__(self):
        self.api = ctypes.WinDLL("user32", use_last_error=True)
        self.callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        signatures = {
            "EnumWindows": ([self.callback, wintypes.LPARAM], wintypes.BOOL),
            "EnumChildWindows": ([wintypes.HWND, self.callback, wintypes.LPARAM], wintypes.BOOL),
            "GetWindowTextW": ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            "GetClassNameW": ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            "GetWindowThreadProcessId": (
                [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)],
                wintypes.DWORD,
            ),
            "IsWindowVisible": ([wintypes.HWND], wintypes.BOOL),
            "IsWindow": ([wintypes.HWND], wintypes.BOOL),
            "IsWindowEnabled": ([wintypes.HWND], wintypes.BOOL),
            "GetDlgItem": ([wintypes.HWND, ctypes.c_int], wintypes.HWND),
            "GetDlgCtrlID": ([wintypes.HWND], ctypes.c_int),
            "PostMessageW": (
                [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM],
                wintypes.BOOL,
            ),
            "SendMessageTimeoutW": (
                [
                    wintypes.HWND,
                    wintypes.UINT,
                    wintypes.WPARAM,
                    wintypes.LPARAM,
                    wintypes.UINT,
                    wintypes.UINT,
                    ctypes.POINTER(ctypes.c_size_t),
                ],
                ctypes.c_size_t,
            ),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.api, name)
            function.argtypes, function.restype = arguments, result

    def text(self, window):
        value = ctypes.create_unicode_buffer(512)
        self.api.GetWindowTextW(window, value, len(value))
        return value.value

    def class_name(self, window):
        value = ctypes.create_unicode_buffer(256)
        self.api.GetClassNameW(window, value, len(value))
        return value.value

    def pid(self, window):
        value = wintypes.DWORD()
        self.api.GetWindowThreadProcessId(window, ctypes.byref(value))
        return value.value

    def windows(self, parent=None):
        values = []

        @self.callback
        def append(window, _parameter):
            values.append(window)
            return True

        if parent is None:
            self.api.EnumWindows(append, 0)
        else:
            self.api.EnumChildWindows(parent, append, 0)
        return values

    def form(self, pid):
        return next(
            (
                window
                for window in self.windows()
                if self.pid(window) == pid
                and self.text(window) == _TITLE
                and self.api.IsWindowVisible(window)
            ),
            None,
        )

    def control(self, form, identifier):
        return next(
            (
                window
                for window in self.windows(form)
                if self.api.GetDlgCtrlID(window) == identifier
            ),
            self.api.GetDlgItem(form, identifier),
        )

    def control_text(self, form, identifier):
        """WM_GETTEXT reads another process's actual edit/label control value."""
        control = self.control(form, identifier)
        if not control:
            return ""
        value = ctypes.create_unicode_buffer(1024)
        self.message(
            control, 0x000D, ctypes.cast(value, ctypes.c_void_p).value, parameter=len(value)
        )
        return value.value

    def message(self, window, message, value=0, *, parameter=0):
        __tracebackhide__ = True
        result = ctypes.c_size_t()
        _require(
            bool(self.api.SendMessageTimeoutW(window, message, parameter, value, 3, 5000, result)),
            "Native desktop control did not respond within its deadline",
        )
        return result.value

    def set_text(self, form, identifier, text):
        __tracebackhide__ = True
        control = self.control(form, identifier)
        _require(bool(control), "Expected account input control is missing")
        value = ctypes.create_unicode_buffer(text)
        self.message(control, 0x000C, ctypes.cast(value, ctypes.c_void_p).value)  # WM_SETTEXT

    def click(self, window):
        __tracebackhide__ = True
        _require(
            bool(window) and self.api.IsWindowVisible(window) and self.api.IsWindowEnabled(window),
            "Expected enabled desktop button is unavailable",
        )
        self.message(window, 0x00F5)  # BM_CLICK

    def post_click(self, window):
        """Queue a real button event whose handler may perform lengthy installation."""
        __tracebackhide__ = True
        _require(
            bool(window)
            and self.api.IsWindow(window)
            and self.api.IsWindowVisible(window)
            and self.api.IsWindowEnabled(window),
            "Expected enabled desktop button is unavailable",
        )
        _require(
            bool(self.api.PostMessageW(window, 0x00F5, 0, 0)),  # BM_CLICK
            "Native desktop button click could not be queued",
        )

    def close(self, form):
        __tracebackhide__ = True
        # A real title-bar X sends WM_SYSCOMMAND/SC_CLOSE, setting WinForms'
        # CloseReason.UserClosing. Direct WM_CLOSE represents an external
        # termination request and can be classified as TaskManagerClosing.
        _require(
            bool(self.api.PostMessageW(form, 0x0112, 0xF060, 0)),
            "Desktop title-bar close command failed",
        )


class _Cohort:
    """Observe only processes descended from this test's desktop executable."""

    def __init__(self, api):
        self.api = api
        self.roots = []
        self.processes = {}
        self.console_seen = False
        self.failed = False
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._observe, daemon=True)
        self.thread.start()

    def add(self, process):
        try:
            root = psutil.Process(process.pid)
        except psutil.NoSuchProcess:
            return
        with self.lock:
            self.roots.append(root)

    def snapshot(self):
        with self.lock:
            return self._snapshot()

    def _snapshot(self):
        for root in list(self.roots):
            try:
                cohort = [root, *root.children(recursive=True)]
            except psutil.NoSuchProcess:
                continue
            for process in cohort:
                try:
                    self.processes[(process.pid, process.create_time())] = process
                except psutil.NoSuchProcess:
                    continue
        return list(self.processes.values())

    def _observe(self):
        try:
            while not self.stop.wait(0.05):
                pids = {process.pid for process in self.snapshot() if process.is_running()}
                if any(
                    self.api.pid(window) in pids
                    and self.api.class_name(window) == "ConsoleWindowClass"
                    for window in self.api.windows()
                ):
                    self.console_seen = True
        except Exception:
            self.failed = True

    def finish(self):
        self.stop.set()
        self.thread.join(timeout=5)


def _wait(condition, label, *, timeout=30, process=None):
    __tracebackhide__ = True
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            pytest.fail(label + ": desktop application exited", pytrace=False)
        try:
            result = condition()
            if result:
                return result
        except (RuntimeError, OSError, psutil.NoSuchProcess):
            pass
        time.sleep(0.1)
    pytest.fail(label + ": readiness deadline expired", pytrace=False)


def _normalized_caption(value):
    return value.replace("&", "").replace("...", "").strip().casefold()


def _uncheck_launch(api, controls):
    """Inno's postinstall checkbox is an owner-drawn native checklist item."""
    for control in controls:
        if api.class_name(control) != "TNewCheckListBox":
            continue
        count = api.message(control, 0x018B)  # LB_GETCOUNT
        length = api.message(control, 0x018A) if count == 1 else 0  # LB_GETTEXTLEN
        if not 0 < length < 128:
            continue
        caption = ctypes.create_unicode_buffer(128)
        api.message(control, 0x0189, ctypes.cast(caption, ctypes.c_void_p).value)  # LB_GETTEXT
        if _normalized_caption(caption.value) == "launch k5 vision":
            api.message(control, 0x0186)  # LB_SETCURSEL first item
            api.message(control, 0x0100, parameter=0x20)  # WM_KEYDOWN / VK_SPACE
            api.message(control, 0x0101, parameter=0x20)  # WM_KEYUP / VK_SPACE
            return True
    return False


def _classify_installer_modal(api, forms, diagnostic):
    """Classify only finite modal evidence; never retain window text or paths."""
    for form in forms:
        if api.class_name(form) not in {"#32770", "TMessageForm", "TTaskDialog"}:
            continue
        if not api.api.IsWindowVisible(form):
            continue
        diagnostic["installer_modal_visible"] = True
        children = api.windows(form)
        diagnostic["installer_modal_controls"] = min(64, len(children))
        # Window labels may contain local installation paths. Inspect in memory
        # for fixed phrases, and emit only this bounded, non-identifying code.
        labels = [api.text(form), *(api.text(child) for child in children[:64])]
        message = " ".join(labels).casefold()
        if "could not verify the installed application" in message:
            diagnostic["installer_modal_classification"] = "payload_refusal"
        elif "still running" in message or "another setup" in message:
            diagnostic["installer_modal_classification"] = "active_instance"
        elif "file" in message and ("in use" in message or "access" in message):
            diagnostic["installer_modal_classification"] = "file_access"
        else:
            diagnostic["installer_modal_classification"] = "unclassified"
        return


def _classify_installed_seal_refusal(installed):
    """Use only bounded codes to distinguish an in-flight Inno file from bad payload.

    Never emit a filename, source URI, local directory, manifest entry or hash.
    This is diagnostic-only, invoked after the *real* installer refuses admission.
    """
    try:
        manifest = json.loads((installed / "payload-manifest.json").read_text(encoding="utf-8"))
        entries = manifest["files"]
        declared = {entry["path"].casefold() for entry in entries}
        allowed = {
            "k5vision.exe",
            "payload-manifest.json",
            "unins000.exe",
            "unins000.dat",
            "unins000.msg",
        }
        observed = set()
        for i, file in enumerate(installed.rglob("*")):
            if i >= 10000:
                return "unbounded_installed_tree"
            if file.is_file():
                observed.add(file.relative_to(installed).as_posix().casefold())
        extras = observed - declared - allowed
        if extras:
            # Inno may reserve or create additional uninstall siblings during an
            # upgrade. Report only a fixed class; never disclose their names or
            # trust these files in the product verifier.
            if all(re.fullmatch(r"unins[0-9]{3}\.(?:exe|dat|msg|tmp)", name) for name in extras):
                return "inno_uninstaller_sibling"
            if all(re.fullmatch(r"unins[0-9]{3}\.(?:tmp|new|old)", name) for name in extras):
                return "inno_uninstaller_transient"
            if any(name.startswith("runtime/") or name.startswith("workers/") for name in extras):
                return "unlisted_worker_or_runtime_file"
            return "unlisted_installed_file"
        if declared - observed:
            return "missing_sealed_file"
        for entry in entries:
            path = installed.joinpath(*entry["path"].split("/"))
            if path.stat().st_size != entry["bytes"]:
                return "changed_sealed_file_size"
            with path.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != entry["sha256"]:
                    return "changed_sealed_file_hash"
        return "native_admission_or_inflight_installer_metadata"
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return "bounded_inspection_incomplete"


def _installer_page_signature(api, controls):
    """Ephemeral native wizard-page identity, never written to diagnostics.

    The same Next control is reused across pages. A queued native click must
    not be queued again until actual visible wizard controls change.
    """
    return tuple(
        sorted(
            (api.api.GetDlgCtrlID(window), api.class_name(window), api.text(window))
            for window in controls
        )
    )


def _install_gui(setup, installed, api, *, diagnostic=None):
    """Drive the actual Inno wizard without suppressing its user interface."""
    __tracebackhide__ = True
    diagnostic = {} if diagnostic is None else diagnostic
    diagnostic.update(
        installer_action="starting",
        installer_install_posted=False,
        installer_next_clicks=0,
        installer_modal_visible=False,
    )
    process = subprocess.Popen(
        [
            str(setup),
            "/SP-",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            "/DIR=" + str(installed),
            "/TASKS=desktopicon",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline, clicked, launch_unchecked = time.monotonic() + 90, set(), False
    last_next_page = None
    try:
        while process.poll() is None:
            _require(time.monotonic() < deadline, "Actual installer wizard exceeded its deadline")
            root = psutil.Process(process.pid)
            pids = {root.pid, *(child.pid for child in root.children(recursive=True))}
            forms = [window for window in api.windows() if api.pid(window) in pids]
            if diagnostic["installer_install_posted"]:
                _classify_installer_modal(api, forms, diagnostic)
                if diagnostic.get("installer_modal_classification") == "payload_refusal":
                    diagnostic["installer_seal_refusal_category"] = (
                        _classify_installed_seal_refusal(installed)
                    )
                    pytest.fail("Installer refused its extracted sealed payload", pytrace=False)
            for form in forms:
                controls = [
                    window
                    for window in api.windows(form)
                    if api.api.IsWindowVisible(window) and api.api.IsWindowEnabled(window)
                ]
                captions = {_normalized_caption(api.text(window)): window for window in controls}
                if any(
                    caption.startswith("k5 vision could not verify the installed application")
                    for caption in captions
                ):
                    diagnostic["installer_action"] = "payload_admission_refused"
                    pytest.fail("Installer rejected the extracted sealed payload", pytrace=False)
                if "i accept the agreement" in captions:
                    diagnostic["installer_action"] = "accept_license"
                    api.click(captions["i accept the agreement"])
                if "launch k5 vision" in captions and not launch_unchecked:
                    diagnostic["installer_action"] = "uncheck_launch"
                    checkbox = captions["launch k5 vision"]
                    if api.message(checkbox, 0x00F0) == 1:  # BM_GETCHECK / BST_CHECKED
                        api.click(checkbox)
                    launch_unchecked = True
                if "finish" in captions and not launch_unchecked:
                    diagnostic["installer_action"] = "uncheck_launch"
                    launch_unchecked = _uncheck_launch(api, controls)
                    _require(launch_unchecked, "Installer launch checklist was unavailable")
                for caption in ("finish", "install", "next >", "next"):
                    if caption in captions:
                        if caption == "install":
                            if not diagnostic["installer_install_posted"]:
                                diagnostic["installer_action"] = "install_dispatch"
                                # Inno extracts the entire offline payload inside
                                # its Install handler. A synchronous BM_CLICK's
                                # five-second reply deadline is not an extraction
                                # deadline. Queue it once, then observe the real
                                # wizard and process under the existing 90s gate.
                                api.post_click(captions[caption])
                                diagnostic["installer_install_posted"] = True
                                diagnostic["installer_action"] = "install_wait"
                        else:
                            diagnostic["installer_action"] = (
                                "finish" if caption == "finish" else "next"
                            )
                            if caption.startswith("next"):
                                # Inno's Next handler can synchronously prepare
                                # an offline payload. SendMessageTimeout's 5s
                                # control deadline is not an install deadline.
                                # Queue one real BM_CLICK per observed page;
                                # retain the unchanged 90s process deadline.
                                page = _installer_page_signature(api, controls)
                                if page == last_next_page:
                                    break
                                diagnostic["installer_next_clicks"] += 1
                                api.post_click(captions[caption])
                                last_next_page = page
                            else:
                                api.click(captions[caption])
                        clicked.add(caption)
                        break
            time.sleep(0.2)
        _require(process.returncode == 0, "Actual GUI installer did not complete successfully")
        _require("install" in clicked and "finish" in clicked, "Installer wizard was not exercised")
        _require(launch_unchecked, "Installer launch option was not available to the user")
        diagnostic["installer_action"] = "completed"
    finally:
        diagnostic["installer_exit_code"] = process.poll()
        diagnostic["installer_steps_seen"] = sorted(clicked)
        diagnostic["installer_launch_unchecked"] = launch_unchecked
        if process.poll() is None:
            children = psutil.Process(process.pid).children(recursive=True)
            process.kill()
            process.wait(timeout=10)
            for child in children:
                if child.is_running():
                    child.kill()


def _verify_installed_worker_bundle(installed: Path) -> None:
    """Check actual installed native worker bytes before first startup.

    This is a local payload admission check, not a disconnected-network test.
    No private paths, source URIs, or process output enter the receipt.
    """
    workers = installed / "workers"
    manifest_path = workers / "donor-downloads.json"
    _require(
        manifest_path.is_file() and 0 < manifest_path.stat().st_size <= 65536,
        "Installed native worker manifest is absent or oversized",
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    _require(
        type(manifest) is dict and manifest.get("schema_version") == 1,
        "Installed native worker manifest identity is invalid",
    )
    artifacts = manifest.get("artifacts")
    _require(type(artifacts) is list and len(artifacts) == 2, "Installed worker count is invalid")
    expected = {
        "MediaMTX": ("mediamtx.exe", "mediamtx-LICENSE"),
        "Caddy": ("caddy.exe", "caddy-LICENSE"),
    }
    seen = set()
    for artifact in artifacts:
        _require(type(artifact) is dict, "Installed worker entry is invalid")
        component = artifact.get("component")
        _require(
            type(component) is str and component in expected and component not in seen,
            "Installed native worker identity is invalid",
        )
        seen.add(component)
        binary_name, license_name = expected[component]
        _require(
            artifact.get("executable") == binary_name
            and artifact.get("payload_executable") == "workers/" + binary_name
            and artifact.get("payload_license") == "workers/" + license_name,
            "Installed native worker path identity is invalid",
        )
        for filename, size_field, digest_field in (
            (binary_name, "executable_bytes", "executable_sha256"),
            (license_name, "payload_license_bytes", "license_sha256"),
        ):
            candidate = workers / filename
            _require(
                candidate.is_file() and not candidate.is_symlink(), "Native worker file is absent"
            )
            size = artifact.get(size_field)
            digest = artifact.get(digest_field)
            _require(
                type(size) is int
                and 0 < size <= 128 * 1024 * 1024
                and type(digest) is str
                and len(digest) == 64,
                "Installed native worker file identity is invalid",
            )
            _require(candidate.stat().st_size == size, "Installed native worker length mismatch")
            with candidate.open("rb") as stream:
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
            _require(actual == digest, "Installed native worker digest mismatch")
    _require(seen == set(expected), "Installed native worker set is incomplete")


def _start_app(installed, profile, port, cohort):
    environment = dict(os.environ)
    environment["PATH"] = os.pathsep.join(
        (str(Path(os.environ["SYSTEMROOT"]) / "System32"), os.environ["SYSTEMROOT"])
    )
    environment["PYTHONHOME"] = str(profile.parent / "no external Python")
    environment["PYTHONPATH"] = str(profile.parent / "no external modules")
    process = subprocess.Popen(
        [
            str(installed / "K5Vision.exe"),
            "--profile",
            str(profile),
            "--port",
            str(port),
            "--no-browser",
        ],
        cwd=installed,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    cohort.add(process)
    return process


class _BrowserLaunchWatch:
    """Read only new browser processes; keep no command lines or source addresses."""

    def __init__(self, previous, expected):
        self.previous = previous
        self.expected = expected
        self.port = urlsplit(expected).port
        self.argument_matched = False
        self.observed = set()
        self.origin_ancestries = []
        self.action_started = threading.Event()
        self.failed = False
        self.ready = threading.Event()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._observe, daemon=True)

    def _sample(self):
        candidates = []
        for process in psutil.process_iter(attrs=["name"]):
            try:
                if (process.info["name"] or "").casefold() not in {"msedge.exe", "chrome.exe"}:
                    continue
                candidates.append(process)
                identity = (process.pid, process.create_time())
                if identity not in self.previous:
                    self.observed.add(identity)
                    if len(self.observed) > 64:
                        self.failed = True
                        return
                    if self.expected in process.cmdline():
                        self.argument_matched = True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        # Capture short-lived URI forwarders before slower socket queries.
        for process in candidates:
            try:
                if not process.is_running():
                    continue
                # Read only matching-origin traffic, including a preexisting
                # browser's network-service process after OS URI forwarding.
                # The later visible window must match this retained ancestry.
                if any(
                    connection.status == psutil.CONN_ESTABLISHED
                    and connection.raddr
                    and connection.raddr.ip == "127.0.0.1"
                    and connection.raddr.port == self.port
                    for connection in process.net_connections(kind="tcp")
                ):
                    ancestry = frozenset(
                        (item.pid, item.create_time()) for item in [process, *process.parents()[:8]]
                    )
                    if ancestry not in self.origin_ancestries:
                        if len(self.origin_ancestries) >= 64:
                            self.failed = True
                            return
                        self.origin_ancestries.append(ancestry)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

    def origin_for(self, identity):
        return any(identity in ancestry for ancestry in self.origin_ancestries)

    def _observe(self):
        try:
            self.ready.set()
            deadline = time.monotonic() + 40
            while not self.stop.is_set() and time.monotonic() < deadline:
                if self.action_started.is_set():
                    self._sample()
                if self.failed:
                    return
                self.stop.wait(0.005)
        except BaseException:
            self.failed = True

    def finish(self):
        self.stop.set()
        self.thread.join(timeout=2)
        return not self.thread.is_alive() and not self.failed


def test_browser_origin_is_retained_when_connection_closes_before_window_visibility(monkeypatch):
    expected = "http://127.0.0.1:54321/operator/"
    phase = {"connected": False}

    def browser(pid, birth, *, parents=(), dispatch=False, network=False):
        def command_line():
            assert dispatch, "Preexisting browser command lines must never be read"
            return ["msedge.exe", expected]

        return SimpleNamespace(
            pid=pid,
            info={"name": "msedge.exe"},
            create_time=lambda: birth,
            is_running=lambda: True,
            cmdline=command_line,
            parents=lambda: list(parents),
            net_connections=lambda kind: (
                [
                    SimpleNamespace(
                        status=psutil.CONN_ESTABLISHED,
                        raddr=SimpleNamespace(ip="127.0.0.1", port=54321),
                    )
                ]
                if network and phase["connected"]
                else []
            ),
        )

    existing_root = browser(100, 1)
    network_service = browser(101, 2, parents=(existing_root,), network=True)
    dispatch = browser(102, 3, dispatch=True)
    monkeypatch.setattr(
        psutil, "process_iter", lambda attrs: [existing_root, network_service, dispatch]
    )
    watcher = _BrowserLaunchWatch({(100, 1), (101, 2)}, expected)
    watcher.action_started.set()
    # Dispatch precedes the connection. The old watcher returned here and
    # never observed the request that completed before window visibility.
    watcher._sample()
    assert watcher.argument_matched and not watcher.origin_for((100, 1))
    phase["connected"] = True
    watcher._sample()
    phase["connected"] = False
    # A scan starting only when the window appears now sees no connection.
    assert network_service.net_connections(kind="tcp") == []
    assert watcher.origin_for((100, 1))
    assert not watcher.origin_for((100, 999))  # Reused PID has a different birth.
    assert not watcher.origin_for((999, 1))  # Unrelated browser cannot qualify.
    assert len(watcher.origin_ancestries) == 1 and not watcher.failed


def _retire_temporary_browser_processes(owned):
    """Retire only browser descendants observed after native OS handoff.

    Closing the test window is the graceful shutdown signal. A long idle
    wait before terminating still-live descendants adds no observation.
    Require their actual exit instead of silently accepting leaks.
    """
    _, alive = psutil.wait_procs(list(owned.values()), timeout=0.5)
    for item in alive:
        try:
            item.terminate()
        except psutil.NoSuchProcess:
            pass
    _, remaining = psutil.wait_procs(alive, timeout=5)
    _require(not remaining, "Native browser handoff left owned processes running")


def _default_browser_handoff(api, form, port, *, diagnostic=None):
    """Observe real OS dispatch, origin traffic and the new product browser window."""
    diagnostic = {} if diagnostic is None else diagnostic
    diagnostic["browser_handoff_stage"] = "arming_launch_watch"
    previous_windows = set(api.windows())
    previous_processes = {
        (process.info["pid"], process.info["create_time"])
        for process in psutil.process_iter(attrs=["pid", "create_time"])
        if process.info["create_time"] is not None
    }
    watcher = _BrowserLaunchWatch(previous_processes, f"http://127.0.0.1:{port}/operator/")
    opened = None
    owned = {}
    watcher.thread.start()
    try:
        _require(watcher.ready.wait(timeout=2), "Native browser launch observer did not arm")
        _require(not watcher.failed, "Native browser launch observer failed to arm")
        diagnostic["browser_handoff_stage"] = "opening_browser"
        watcher.action_started.set()
        api.click(api.control(form, _OPEN))

        def browser_window():
            for window in api.windows():
                if (
                    window not in previous_windows
                    and api.api.IsWindowVisible(window)
                    and "K5 Vision" in api.text(window)
                ):
                    try:
                        process = psutil.Process(api.pid(window))
                        if process.name().casefold() in {"msedge.exe", "chrome.exe"}:
                            return window
                    except psutil.NoSuchProcess:
                        pass
            return None

        opened = _wait(browser_window, "Actual default-browser K5 window", timeout=25)
        process = psutil.Process(api.pid(opened))
        window_identity = (process.pid, process.create_time())
        diagnostic["browser_handoff_stage"] = "origin_connection"
        origin_connected = False

        def observed_handoff():
            nonlocal origin_connected
            diagnostic.update(
                native_launch_argument_matched=watcher.argument_matched,
                browser_handoff_new_browser_processes=len(watcher.observed),
                browser_handoff_cached_origin_records=len(watcher.origin_ancestries),
                page_window_visible=bool(
                    api.api.IsWindow(opened)
                    and api.api.IsWindowVisible(opened)
                    and "K5 Vision" in api.text(opened)
                    and api.pid(opened) == process.pid
                ),
            )
            _require(not watcher.failed, "Native browser launch observer failed")
            origin_connected = origin_connected or watcher.origin_for(window_identity)
            scope = [process, *process.children(recursive=True)]
            diagnostic["browser_handoff_connection_processes"] = len(scope)
            for item in scope:
                try:
                    identity = (item.pid, item.create_time())
                    if identity not in previous_processes:
                        owned[identity] = item
                    for connection in item.net_connections(kind="tcp"):
                        if (
                            connection.status == psutil.CONN_ESTABLISHED
                            and connection.raddr
                            and connection.raddr.ip == "127.0.0.1"
                            and connection.raddr.port == port
                        ):
                            origin_connected = True
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            diagnostic["browser_origin_connected"] = origin_connected
            return (
                diagnostic["native_launch_argument_matched"]
                and diagnostic["page_window_visible"]
                and origin_connected
            )

        _wait(observed_handoff, "Actual OS browser dispatch and recorder origin", timeout=15)
        diagnostic["browser_handoff_stage"] = "dispatch_origin_window_verified"
        return {
            "native_default_browser_handoff": True,
            "native_launch_argument_matched": True,
            "browser_origin_connected": True,
            "page_window_visible": True,
            "native_browser_handoff_observation": "os_dispatch_origin_and_window",
        }
    finally:
        watcher_finished = watcher.finish()
        diagnostic["browser_handoff_observer_stopped"] = watcher_finished
        if opened and api.api.IsWindow(opened):
            api.close(opened)
        # Existing browsers are not owned. Only new descendants observed
        # during this handoff may be terminated after its window closes.
        _retire_temporary_browser_processes(owned)
        _require(watcher_finished, "Native browser launch observer did not finish")


def test_native_browser_handoff_retires_only_observed_descendants(monkeypatch):
    calls = []
    child = SimpleNamespace(terminate=lambda: calls.append("terminated"))

    def wait_procs(processes, timeout):
        assert processes == [child]
        calls.append(timeout)
        return ([], [child]) if timeout == 0.5 else ([child], [])

    monkeypatch.setattr(psutil, "wait_procs", wait_procs)
    _retire_temporary_browser_processes({(123, 456): child})
    assert calls == [0.5, "terminated", 5]


def test_native_browser_handoff_rejects_unreaped_descendants(monkeypatch):
    child = SimpleNamespace(terminate=lambda: None)
    monkeypatch.setattr(psutil, "wait_procs", lambda _processes, timeout: ([], [child]))
    with pytest.raises(pytest.fail.Exception, match="left owned processes running"):
        _retire_temporary_browser_processes({(123, 456): child})


def _wait_owned_cleanup(cohort, *, timeout=10, diagnostic=None):
    """Wait for owned process identities, never a historical PID's later occupant."""
    __tracebackhide__ = True
    diagnostic = {} if diagnostic is None else diagnostic
    started = time.monotonic()
    deadline = started + timeout
    checks = 0
    names = {
        "k5vision.exe": "desktop",
        "python.exe": "python",
        "pythonw.exe": "python",
        "mediamtx.exe": "mediamtx",
        "caddy.exe": "caddy",
    }
    while True:
        tracked = cohort.snapshot()
        counts = {kind: 0 for kind in ("desktop", "python", "mediamtx", "caddy", "other")}
        for process in tracked:
            try:
                # psutil.wait_procs()/Process.wait() can reopen a recycled PID
                # without checking its original creation identity. is_running()
                # verifies (PID, creation time) and rejects that later occupant.
                if process.is_running():
                    counts[names.get(process.name().casefold(), "other")] += 1
            except psutil.NoSuchProcess:
                continue
        checks += 1
        diagnostic.update(
            stop_owned_identity_count=len(tracked),
            stop_live_owned_counts=counts,
            stop_identity_checks=checks,
            stop_wait_seconds=round(time.monotonic() - started, 3),
        )
        if not any(counts.values()):
            return
        remaining = deadline - time.monotonic()
        _require(remaining > 0, "Owned desktop/backend/native processes survived stop and quit")
        time.sleep(min(0.05, remaining))


def test_owned_cleanup_rejects_historical_pid_reuse_without_waiting_on_its_new_occupant():
    historical = psutil.Process(os.getpid())
    # Represent an earlier owner of this same PID, using the real psutil
    # identity check. No running process is killed or falsely called owned.
    historical._ident = (historical.pid, historical.create_time() - 1)
    assert historical.is_running() is False
    _, old_wait_alive = psutil.wait_procs([historical], timeout=0)
    assert len(old_wait_alive) == 1
    diagnostic = {}
    _wait_owned_cleanup(
        SimpleNamespace(snapshot=lambda: [historical]), timeout=0.1, diagnostic=diagnostic
    )
    assert not any(diagnostic["stop_live_owned_counts"].values())


def test_owned_cleanup_still_fails_for_a_live_owned_process():
    process = psutil.Process(os.getpid())
    diagnostic = {}
    started = time.monotonic()
    with pytest.raises(pytest.fail.Exception, match="processes survived stop and quit"):
        _wait_owned_cleanup(
            SimpleNamespace(snapshot=lambda: [process]), timeout=0.1, diagnostic=diagnostic
        )
    assert 0.1 <= time.monotonic() - started < 2
    assert sum(diagnostic["stop_live_owned_counts"].values()) == 1


def test_owned_cleanup_accepts_a_delayed_real_owned_process_exit():
    process = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.buffer.read(1)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}),
    )
    owned = psutil.Process(process.pid)

    def release():
        process.stdin.close()
        process.wait(timeout=3)

    timer = threading.Timer(0.1, release)
    timer.start()
    diagnostic = {}
    try:
        _wait_owned_cleanup(
            SimpleNamespace(snapshot=lambda: [owned]), timeout=3, diagnostic=diagnostic
        )
        assert not any(diagnostic["stop_live_owned_counts"].values())
        assert diagnostic["stop_identity_checks"] >= 2
    finally:
        timer.join(timeout=4)
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)


def _stop_app(process, form, api, cohort, *, diagnostic=None):
    __tracebackhide__ = True
    api.click(api.control(form, _STOP))
    _require(process.wait(timeout=40) == 0, "Desktop stop and quit did not finish successfully")
    _wait_owned_cleanup(cohort, diagnostic=diagnostic)


def _recording_bytes(profile, camera_directory=None):
    root = profile / "recordings" if camera_directory is None else camera_directory
    return sum(path.stat().st_size for path in root.rglob("*.mp4"))


def _tray_recording(client, profile, camera_id, before, diagnostic, *, camera_directory=None):
    """Require a connected recording source and new bytes while both UIs are closed."""
    root = profile / "recordings" if camera_directory is None else camera_directory
    files = list(root.rglob("*.mp4"))
    after = sum(path.stat().st_size for path in files)
    diagnostic.update(
        recording_file_count=len(files),
        recording_bytes_before=before,
        recording_bytes_after=after,
        camera_request_unavailable=True,
        camera_http_status=0,
    )
    try:
        status, body, _ = client.get("/api/v1/operator/cameras")
    except RuntimeError:
        return False
    diagnostic["camera_request_unavailable"] = False
    diagnostic["camera_http_status"] = status if status in {200, 401, 403, 404, 503} else 0
    camera_status, camera_recording = "unavailable", False
    if status == 200:
        payload = json.loads(body)
        for camera in payload.get("cameras", []):
            if camera.get("device_id") == camera_id:
                value = camera.get("status")
                camera_status = (
                    value if value in {"live", "disconnected", "connecting"} else "unavailable"
                )
                camera_recording = camera.get("recording") is True
                break
    diagnostic.update(
        camera_status=camera_status,
        camera_recording=camera_recording,
    )
    return camera_status == "live" and camera_recording and after > before


def _healthy(client, diagnostic):
    """A normal startup 503 means not ready; strict API assertions follow readiness."""
    status, body, _ = client.get("/api/v1/health")
    diagnostic["health_http_status"] = status if status in {200, 401, 403, 404, 409, 503} else 0
    if status != 200:
        return False
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeError):
        return False
    return isinstance(payload, dict) and payload.get("status") == "ok"


def _desktop_status(api, form):
    """Map our public UI messages to finite codes, never retain unknown text."""
    text = api.control_text(form, _STATUS)
    for prefix, code in (
        ("Preparing K5 Vision", "preparing"),
        ("Create your administrator account", "account_ready"),
        ("Creating your account", "account_creating"),
        ("Starting your cameras", "cameras_starting"),
        ("Recording service is running", "recording_running"),
        ("K5 Vision could not verify", "verification_failed"),
        ("K5 Vision could not use this account profile", "profile_refused"),
        ("Account setup could not complete safely", "account_refused"),
        ("Account setup could not complete.", "account_failed"),
        ("K5 Vision could not start recording", "recording_start_failed"),
        ("Recording stopped unexpectedly", "recording_stopped"),
        ("Stopping recording safely", "stopping"),
    ):
        if text.startswith(prefix):
            return code
    return "unclassified"


def _failure_detail(error):
    """Only fixed classifications are admitted from our controlled assertions."""
    text = str(error)
    for marker, code in (
        ("desktop application exited", "desktop_exited"),
        ("readiness deadline expired", "readiness_deadline"),
        ("Native desktop control did not respond", "native_control_deadline"),
        ("Expected enabled desktop button is unavailable", "native_control_unavailable"),
        ("Native desktop button click could not be queued", "native_control_post_failed"),
        ("Owned desktop/backend/native processes survived", "owned_process_survived"),
        ("Desktop opened a console window", "console_window"),
        ("Bounded camera discovery did not complete", "onvif_discovery_failed"),
        ("Wrong camera credentials were admitted", "onvif_wrong_password_admitted"),
        ("Camera did not reject the actual SOAP digest", "onvif_auth_not_exercised"),
        ("Actual ONVIF credential/profile request failed", "onvif_profiles_failed"),
        ("Profile-based camera connection failed", "onvif_connect_failed"),
        ("Actual authenticated ONVIF SOAP flow was incomplete", "onvif_protocol_incomplete"),
        ("ONVIF time response", "onvif_clock_contract"),
        ("Camera clock offset direction was absent", "onvif_clock_display"),
        ("Camera setup presented an unverified NTP report", "onvif_sync_claim"),
        ("Recorder status did not expose observed UTC", "recorder_clock_contract"),
        ("Recorder display followed client time", "recorder_clock_display"),
        ("Actual ONVIF camera clock compensation was not proven", "onvif_clock_unproven"),
        ("ONVIF profile acceptance claimed unverified video readiness", "onvif_false_video_ready"),
        ("Manual stream field remained", "manual_stream_field"),
        ("Browser sent a manual camera stream address", "manual_stream_request"),
        ("Camera stream address entered the DOM", "stream_uri_dom"),
        ("Timeline fixture has no playable", "timeline_interval_unavailable"),
        ("Timeline recorded interval metadata is invalid", "timeline_interval_invalid"),
        ("Timeline UTC window could not be loaded", "timeline_history_response"),
        ("Actual timeline track is unavailable", "timeline_track_unavailable"),
        ("Timeline playback clip crosses its recorded interval", "timeline_clip_outside_span"),
        ("Decoded video did not match the selected generated camera", "decoded_camera_mismatch"),
        ("Local operator request did not complete", "operator_transport_failure"),
    ):
        if marker in text:
            return code
    return "unclassified"


def _cohort_diagnostic(cohort):
    counts = {"desktop": 0, "python": 0, "mediamtx": 0, "caddy": 0, "other": 0}
    allowed = {
        "k5vision.exe": "desktop",
        "python.exe": "python",
        "pythonw.exe": "python",
        "mediamtx.exe": "mediamtx",
        "caddy.exe": "caddy",
    }
    for process in cohort.snapshot():
        try:
            if process.is_running():
                counts[allowed.get(process.name().casefold(), "other")] += 1
        except psutil.NoSuchProcess:
            continue
    return counts


def _desktop_probe(api, form):
    """Admit only finite, source-free fields from the app's diagnostic textbox."""
    try:
        data = json.loads(api.control_text(form, 1010))
    except (ValueError, UnicodeError):
        return {"available": False}
    if not isinstance(data, dict):
        return {"available": False}
    if data.get("schema_version") != 1:
        return {"available": False}
    result = {"available": True}
    for key in ("ready", "exiting", "backend_present", "backend_exited", "backend_invalid_output"):
        if type(data.get(key)) is bool:
            result[key] = data[key]
    value = data.get("backend_exit_code")
    if value is None or type(value) is int and -(2**31) <= value <= 2**31 - 1:
        result["backend_exit_code"] = value
    phase = data.get("phase")
    result["phase"] = (
        phase
        if phase
        in {
            "preparing",
            "account",
            "initializing",
            "stopping",
            "starting",
            "running",
            "failed",
        }
        else "unclassified"
    )
    status = data.get("backend_status")
    result["backend_status"] = (
        status
        if status in {"none", "ready", "new", "refused", "running", "stopped"}
        else "unclassified"
    )
    return result


def _generate_fixture(
    parent,
    media,
    ffmpeg,
    upstream_port,
    helpers,
    *,
    host="127.0.0.1",
    color="red",
    camera_credentials=None,
    publisher_credentials=None,
    extra_stream_paths=(),
):
    __tracebackhide__ = True
    video = parent / f"generated {color} camera.mp4"
    generated = subprocess.run(
        [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:size=320x240:rate=15",
            "-vf",
            "noise=alls=8:allf=t+u",
            "-t",
            "2",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-tune",
            "zerolatency",
            "-pix_fmt",
            "yuv420p",
            "-g",
            "15",
            "-keyint_min",
            "15",
            "-bf",
            "0",
            str(video),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=20,
        check=False,
    )
    _require(generated.returncode == 0, "Generated desktop camera fixture could not be created")
    upstream_config = parent / f"generated {color} upstream.json"
    stream_paths = ("generated", *extra_stream_paths)
    users = [
        {
            "user": "any",
            "ips": ["127.0.0.0/8", "::1"],
            "permissions": [
                {"action": action, "path": path}
                for path in stream_paths
                for action in ("publish", "read")
            ],
        }
    ]
    if camera_credentials is not None:
        _require(publisher_credentials is not None, "Authenticated fixture requires a publisher")
        users = [
            {
                "user": camera_credentials[0],
                "pass": camera_credentials[1],
                "ips": ["127.0.0.0/8", "::1"],
                "permissions": [{"action": "read", "path": path} for path in stream_paths],
            },
            {
                "user": publisher_credentials[0],
                "pass": publisher_credentials[1],
                "ips": ["127.0.0.0/8", "::1"],
                "permissions": [{"action": "publish", "path": path} for path in stream_paths],
            },
        ]
    helpers._private_write(
        upstream_config,
        json.dumps(
            {
                "logLevel": "warn",
                "rtspAddress": f"{host}:{upstream_port}",
                "rtspTransports": ["tcp"],
                "authInternalUsers": users,
                "api": False,
                "playback": False,
                "hls": False,
                "webrtc": False,
                "rtmp": False,
                "srt": False,
                "moq": False,
                "metrics": False,
                "pprof": False,
                "paths": {path: {"source": "publisher"} for path in stream_paths},
            }
        ),
    )
    upstream = helpers._start([str(media), str(upstream_config)], parent)
    try:

        def ready():
            with socket.create_connection((host, upstream_port), timeout=0.5):
                return True

        _wait(ready, "Generated upstream listener", process=upstream, timeout=10)
    except BaseException:
        helpers._stop_owned(upstream)
        raise
    source_uri = f"rtsp://{host}:{upstream_port}/generated"
    try:
        publisher = _publish_fixture(
            parent, ffmpeg, source_uri, helpers, color=color, credentials=publisher_credentials
        )
    except BaseException:
        helpers._stop_owned(upstream)
        raise
    return [upstream, publisher], source_uri


def _publish_fixture(parent, ffmpeg, source_uri, helpers, *, color="red", credentials=None):
    if credentials is not None:
        parsed = urlsplit(source_uri)
        private_address = (
            quote(credentials[0], safe="")
            + ":"
            + quote(credentials[1], safe="")
            + "@"
            + parsed.netloc
        )
        source_uri = parsed._replace(netloc=private_address).geturl()
    return helpers._start(
        [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-re",
            "-stream_loop",
            "-1",
            "-i",
            str(parent / f"generated {color} camera.mp4"),
            "-c",
            "copy",
            "-f",
            "rtsp",
            "-rtsp_transport",
            "tcp",
            source_uri,
        ],
        parent,
    )


def _closed_hashes(profile, camera_directory=None):
    """Call only after the generated source has disconnected and finalized."""
    root = profile / "recordings" if camera_directory is None else camera_directory
    files = sorted(root.rglob("*.mp4"))
    _require(0 < len(files) <= 64, "VM clock proof has no bounded closed recording inventory")
    _require(
        all(0 < path.stat().st_size <= 16 * 1024**2 for path in files)
        and sum(path.stat().st_size for path in files) <= 64 * 1024**2,
        "VM clock closed recording inventory exceeded its byte bound",
    )
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.ReadFile.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    kernel.ReadFile.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes, kernel.CloseHandle.restype = [wintypes.HANDLE], wintypes.BOOL
    hashes = {}
    for path in files:
        # Zero sharing requires the actual MediaMTX writer (and any transient
        # reader) to have closed. Source status alone is not closure evidence.
        handle = kernel.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x00200000, None)
        if handle in (None, ctypes.c_void_p(-1).value):
            raise RuntimeError("VM clock recording has not closed for exclusive reading")
        try:
            digest, total = hashlib.sha256(), 0
            buffer = ctypes.create_string_buffer(65536)
            while True:
                count = wintypes.DWORD()
                if not kernel.ReadFile(handle, buffer, len(buffer), ctypes.byref(count), None):
                    raise RuntimeError("VM clock closed recording could not be read")
                if not count.value:
                    break
                total += count.value
                _require(total <= 16 * 1024**2, "VM clock closed recording exceeded its bound")
                digest.update(buffer.raw[: count.value])
            hashes[path] = digest.digest()
        finally:
            kernel.CloseHandle(handle)
    return hashes


def _hashes_preserved(hashes):
    return all(
        path.is_file() and hashlib.sha256(path.read_bytes()).digest() == expected
        for path, expected in hashes.items()
    )


def _recorded_segment_frames(ffprobe, video):
    """VM phases can span the real recorder's full 60-second segment cadence."""
    result = subprocess.run(
        [
            str(ffprobe),
            "-v",
            "error",
            "-count_frames",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name,width,height,nb_read_frames",
            "-of",
            "json",
            str(video),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=15,
        check=False,
    )
    _require(
        result.returncode == 0 and len(result.stdout) <= 4096,
        "VM clock retained recording could not be decoded within its bound",
    )
    streams = json.loads(result.stdout)["streams"]
    _require(
        len(streams) == 1
        and (streams[0]["codec_name"], streams[0]["width"], streams[0]["height"])
        == ("h264", 320, 240),
        "VM clock retained recording changed its generated video format",
    )
    frames = int(streams[0]["nb_read_frames"])
    _require(0 < frames <= 61 * 15, "VM clock retained recording exceeded its frame bound")
    return frames


def _desktop_nvr_journey(
    *,
    page,
    client,
    profile,
    camera_ids,
    camera_directories,
    parent,
    processes,
    publishers,
    sources,
    ffmpeg,
    ffprobe,
    helpers,
    browser_helpers,
    app,
    diagnostic,
    history_not_before,
    export_proof,
):
    """Exercise operator actions against four real installed recording paths."""
    __tracebackhide__ = True

    def sizes(indices=range(4)):
        return [_recording_bytes(profile, camera_directories[index]) for index in indices]

    def all_progress(before, indices=range(4)):
        return all(after > prior for after, prior in zip(sizes(indices), before, strict=True))

    nvr_phase_clock = time.monotonic()
    previous_nvr_phase = "start"
    nvr_phase_seconds = []
    diagnostic["nvr_phase_seconds"] = nvr_phase_seconds

    def mark_nvr_phase(name):
        nonlocal nvr_phase_clock, previous_nvr_phase
        now = time.monotonic()
        nvr_phase_seconds.append((previous_nvr_phase, round(now - nvr_phase_clock, 2)))
        previous_nvr_phase = name
        nvr_phase_clock = now
        diagnostic["nvr_stage"] = name

    mark_nvr_phase("four_recorders")
    before = sizes()
    _wait(lambda: all_progress(before), "Installed four-camera recording progress", process=app)
    evidence = {
        "four_camera_recording_progress": True,
        "camera_recording_bytes_before": before,
        "camera_recording_bytes_after": sizes(),
    }
    mark_nvr_phase("outage_wall_admission")
    browser_helpers._wall_frames(page, camera_ids, [color for _, _, color in _DESKTOP_CAMERAS])
    unaffected = [0, 2, 3]
    selectors = [
        f'#wall-panel [data-camera-id="{camera_ids[index]}"] video' for index in unaffected
    ]
    handles = [page.locator(selector).element_handle() for selector in selectors]
    frames = [
        handle.evaluate("v => v.getVideoPlaybackQuality().totalVideoFrames") for handle in handles
    ]
    others_before = sizes(unaffected)
    diagnostic.update(
        nvr_outage_other_frames_before=frames,
        nvr_outage_other_recording_bytes_before=others_before,
    )
    mark_nvr_phase("outage_stop_source")
    helpers._stop_owned(publishers[1])
    diagnostic["nvr_outage_publisher_exit_code"] = publishers[1].poll()

    def sample_survivors():
        samples = [
            handle.evaluate(
                "v => ({connected:v.isConnected,paused:v.paused,"
                "frames:v.getVideoPlaybackQuality().totalVideoFrames})"
            )
            for handle in handles
        ]
        diagnostic.update(
            nvr_outage_other_connected=[sample["connected"] for sample in samples],
            nvr_outage_other_paused=[sample["paused"] for sample in samples],
            nvr_outage_other_frames=[sample["frames"] for sample in samples],
            nvr_outage_other_recording_bytes=sizes(unaffected),
        )
        return samples

    def isolated_offline():
        samples = sample_survivors()
        views = {
            camera["device_id"]: camera
            for camera in client.json("/api/v1/operator/cameras")["cameras"]
        }
        statuses = [views[device]["status"] for device in camera_ids]
        diagnostic["nvr_outage_camera_statuses"] = [
            status if status in {"live", "connecting", "disconnected", "unavailable"} else "unknown"
            for status in statuses
        ]
        _require(
            all(sample["connected"] for sample in samples),
            "One camera outage destroyed an unrelated installed wall player",
        )
        return views[camera_ids[1]]["status"] == "disconnected" and all(
            views[camera_ids[index]]["status"] == "live" for index in unaffected
        )

    mark_nvr_phase("outage_camera_status")
    _wait(isolated_offline, "Installed isolated camera outage", process=app, timeout=20)
    mark_nvr_phase("outage_other_live_progress")

    def other_live_progress():
        samples = sample_survivors()
        return all(
            sample["connected"] and not sample["paused"] and sample["frames"] >= before + 4
            for sample, before in zip(samples, frames, strict=True)
        )

    _wait(other_live_progress, "Other installed wall videos advance", process=app, timeout=15)
    mark_nvr_phase("outage_other_recording_progress")

    def other_recording_progress():
        sample_survivors()
        return all(
            after > before
            for after, before in zip(
                diagnostic["nvr_outage_other_recording_bytes"], others_before, strict=True
            )
        )

    _wait(
        other_recording_progress,
        "Other installed cameras record during outage",
        process=app,
    )
    evidence["outage_other_live_frames_before"] = frames
    evidence["outage_other_live_frames_after"] = [
        handle.evaluate("v => v.getVideoPlaybackQuality().totalVideoFrames") for handle in handles
    ]
    mark_nvr_phase("outage_camera_reconnect")
    publishers[1] = _publish_fixture(parent, ffmpeg, sources[1], helpers, color="blue")
    processes.append(publishers[1])
    browser_helpers._decoded_video(
        page, f'#wall-panel [data-camera-id="{camera_ids[1]}"] video', "blue"
    )
    recovered = sizes()
    _wait(lambda: all_progress(recovered), "Installed camera recording recovery", process=app)
    evidence["isolated_camera_outage_reconnect"] = True

    mark_nvr_phase("pause_recording")
    browser_helpers._open_camera_watch(page, camera_ids[0])
    before_live = browser_helpers._decoded_color(page, "live-video", "red")
    live = page.locator("#live-video").element_handle()
    recording_route = f"/api/v1/operator/cameras/{camera_ids[0]}/recording"
    browser_helpers._recording_control_diagnostic(None, False, diagnostic)
    with page.expect_response(
        lambda response: response.request.method == "PUT" and response.url.endswith(recording_route)
    ) as paused:
        page.locator("#toggle-recording").click()
    browser_helpers._recording_control_diagnostic(paused.value, False, diagnostic)
    _require(paused.value.status == 200, "Installed pause recording action failed")
    _wait(
        lambda: page.locator("#toggle-recording").get_attribute("data-enabled") == "false",
        "Installed paused recording control",
        process=app,
    )
    paused_hashes = _wait(
        lambda: _closed_hashes(profile, camera_directories[0]),
        "Paused installed camera has closed files",
        process=app,
        timeout=10,
    )
    paused_bytes = sizes()[0]
    running_indices = [1, 2, 3]
    others_before = sizes(running_indices)
    paused_at = time.monotonic()
    _wait(
        lambda: time.monotonic() - paused_at >= 5 and all_progress(others_before, running_indices),
        "Other installed recorders progress while one is paused",
        process=app,
    )
    _require(
        sizes()[0] == paused_bytes and _hashes_preserved(paused_hashes),
        "Paused installed camera continued writing recording files",
    )
    _require(
        live.evaluate(
            "v => v.isConnected && !v.paused && v.getVideoPlaybackQuality().totalVideoFrames"
        )
        > before_live,
        "Pausing recording interrupted installed live video",
    )
    _require(
        bool(client.json(f"/api/v1/operator/cameras/{camera_ids[0]}/history")["spans"]),
        "Pausing recording removed installed recording history",
    )
    browser_helpers._recording_control_diagnostic(None, True, diagnostic)
    with page.expect_response(
        lambda response: response.request.method == "PUT" and response.url.endswith(recording_route)
    ) as resumed:
        page.locator("#toggle-recording").click()
    browser_helpers._recording_control_diagnostic(resumed.value, True, diagnostic)
    _require(resumed.value.status == 200, "Installed resume recording action failed")
    _wait(
        lambda: sizes()[0] > paused_bytes,
        "Installed resumed recording produces new bytes",
        process=app,
    )
    evidence["pause_resume_preserves_live_history"] = True
    evidence["other_recording_while_paused"] = True
    evidence["paused_recording_bytes"] = paused_bytes
    evidence["resumed_recording_bytes"] = sizes()[0]

    mark_nvr_phase("historical_navigation")
    seeded = browser_helpers._seed_recorded_history(camera_directories[0], ffmpeg, parent)
    evidence.update(
        browser_helpers._recording_navigation_journey(
            page, device_id=camera_ids[0], seeded=seeded, diagnostic=diagnostic
        )
    )
    mark_nvr_phase("timeline_and_export")
    browser_helpers._open_camera_recordings(page, camera_ids[0])
    evidence["timeline_playback_decoded_frames"] = browser_helpers._timeline_play(
        page,
        "red",
        device_id=camera_ids[0],
        diagnostic=diagnostic,
        not_before=history_not_before,
    )
    export = parent / "saved installed red recording.mp4"
    evidence["export_decoded_frames"] = browser_helpers._download_recording(
        page,
        ffprobe,
        export,
        helpers,
        "red",
        normal_directory=parent / "Windows Downloads",
        evidence=export_proof,
        diagnostic=diagnostic,
    )
    evidence["export_bytes"] = export_proof["bytes"]
    evidence["export_normal_windows_download"] = True
    evidence["timeline_seek_playback"] = True
    evidence["actual_recording_export"] = True

    # Leave a real choice paused across the following native restart. The
    # primary source remains recording for tray and actual-VM clock checks.
    mark_nvr_phase("persist_paused_choice")
    browser_helpers._open_camera_watch(page, camera_ids[3])
    preference_route = f"/api/v1/operator/cameras/{camera_ids[3]}/recording"
    browser_helpers._recording_control_diagnostic(None, False, diagnostic)
    with page.expect_response(
        lambda response: (
            response.request.method == "PUT" and response.url.endswith(preference_route)
        )
    ) as preference:
        page.locator("#toggle-recording").click()
    browser_helpers._recording_control_diagnostic(preference.value, False, diagnostic)
    _require(preference.value.status == 200, "Installed persisted recording choice failed")
    _wait(
        lambda: page.locator("#toggle-recording").get_attribute("data-enabled") == "false",
        "Installed recording choice is paused",
        process=app,
    )
    browser_helpers._open_camera_watch(page, camera_ids[0])
    browser_helpers._decoded_color(page, "live-video", "red")
    mark_nvr_phase("complete")
    return evidence


def _installed_person_fixture(parent, ffmpeg):
    """Encode only the admitted Apache public image as a generated camera."""
    fixture = Path(os.environ["K5_OPERATOR_PERSON_FIXTURE"]).resolve(strict=True)
    _require(
        fixture.stat().st_size == 553577
        and hashlib.sha256(fixture.read_bytes()).hexdigest()
        == "2be4dfa4d874e5f7929bebd1baf401e2ee630c4bf36cead7a2f8d5421d583959",
        "Installed analytics public fixture identity was refused",
    )
    video = parent / "generated licensed person camera.mp4"
    generated = subprocess.run(
        [
            str(ffmpeg),
            "-v",
            "error",
            "-loop",
            "1",
            "-i",
            str(fixture),
            "-vf",
            "scale=320:240",
            "-r",
            "15",
            "-t",
            "2",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-tune",
            "zerolatency",
            "-pix_fmt",
            "yuv420p",
            "-g",
            "15",
            "-keyint_min",
            "15",
            "-bf",
            "0",
            str(video),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=20,
        check=False,
    )
    _require(generated.returncode == 0, "Installed analytics camera fixture generation failed")
    return video


def _installed_sampled_person_boxes(page, camera_id, client, browser_helpers, diagnostic):
    """Prove native boxes are visible on the same transient installed browser frame.

    No sampled pixels, camera URI, request body, screenshot or bearer token is
    copied to CI evidence. A painted native rectangle is required, not merely a
    successful person-detection API response.
    """
    __tracebackhide__ = True
    diagnostic["analytics_sample_stage"] = "open_live"
    browser_helpers._open_camera_watch(page, camera_id)
    try:
        diagnostic["analytics_sample_stage"] = "panel_visible"
        page.locator("#person-view-panel").wait_for(state="visible")
        diagnostic["analytics_sample_stage"] = "live_decoded"
        page.wait_for_function(
            """() => {
              const video = document.getElementById('live-video');
              return video && !video.paused && video.readyState >= 2 &&
                video.videoWidth > 0 && video.videoHeight > 0 &&
                video.getVideoPlaybackQuality().totalVideoFrames >= 4;
            }""",
            timeout=25_000,
        )
    except Exception:
        # Keep only finite player and control states; never inspect or retain
        # the source URL, session, request body, video frame or screenshot.
        try:
            diagnostic["analytics_sample_live_state"] = page.evaluate(
                """() => {
                  const video = document.getElementById('live-video');
                  const panel = document.getElementById('person-view-panel');
                  const quality = video?.getVideoPlaybackQuality?.();
                  const frames = quality?.totalVideoFrames;
                  return {
                    panel_visible: !!panel && !panel.hidden,
                    live_visible: !document.getElementById('live-panel')?.hidden,
                    paused: video?.paused ?? null,
                    ready_state: video?.readyState ?? null,
                    has_dimensions: !!video && video.videoWidth > 0 && video.videoHeight > 0,
                    decoded_frames: Number.isSafeInteger(frames) ?
                      Math.min(frames, 1000000) : null,
                    video_error: video?.error?.code ?? null,
                    retry_visible: !document.getElementById('retry-live')?.hidden,
                    play_visible: !document.getElementById('play-live')?.hidden,
                    sample_button_enabled: !document.getElementById('toggle-person-view')?.disabled
                  };
                }"""
            )
        except Exception:
            diagnostic["analytics_sample_live_state"] = {"available": False}
        raise
    button = page.locator("#toggle-person-view")
    _require(button.is_enabled(), "Installed sampled-person control was not enabled")
    route = f"/api/v1/operator/cameras/{camera_id}/person-view"
    diagnostic["analytics_sample_stage"] = "native_frame_request"
    # Observe only route identity and bounded finite counts/statuses. Never
    # retain URLs, query parameters, upload pixels or request/response bodies.
    traffic = {"requests": 0, "responses": 0, "failed": 0, "http_status": 0}
    first_sent_at = [None]

    def is_sample(request):
        return request.method == "POST" and urlsplit(request.url).path == route

    def observed_request(request):
        if is_sample(request):
            traffic["requests"] = min(5, traffic["requests"] + 1)
            if first_sent_at[0] is None:
                first_sent_at[0] = time.monotonic()

    def observed_response(response):
        if is_sample(response.request):
            traffic["responses"] = min(5, traffic["responses"] + 1)
            # HTTP status and elapsed time are finite transport evidence, not
            # response content. Preserve unexpected 5xx classes to distinguish
            # proxy/backend faults from a native inference refusal.
            traffic["http_status"] = response.status if 100 <= response.status <= 599 else 0
            if response.status == 429:
                phase = response.headers.get("x-k5-person-phase")
                traffic["busy_phase"] = phase if phase in {"capacity", "preparing"} else "other"
            if first_sent_at[0] is not None:
                traffic["first_response_ms"] = min(
                    120000, max(0, int((time.monotonic() - first_sent_at[0]) * 1000))
                )

    def observed_failure(request):
        if is_sample(request):
            traffic["failed"] = min(5, traffic["failed"] + 1)
            if first_sent_at[0] is not None:
                traffic["first_failure_ms"] = min(
                    120000, max(0, int((time.monotonic() - first_sent_at[0]) * 1000))
                )
            # Preserve only a finite transport classification, never the
            # request URL, uploaded frame, headers or browser exception text.
            failure = request.failure
            traffic["failure_class"] = (
                "aborted"
                if failure == "net::ERR_ABORTED"
                else "timed_out"
                if failure == "net::ERR_TIMED_OUT"
                else "connection_closed"
                if failure in {"net::ERR_CONNECTION_CLOSED", "net::ERR_CONNECTION_RESET"}
                else "other"
            )

    page.on("request", observed_request)
    page.on("response", observed_response)
    page.on("requestfailed", observed_failure)
    try:
        with page.expect_response(
            # The product explicitly treats transient HTTP 429 as busy and retries.
            # Only accept a real successful inference response; the positive
            # same-frame box and canvas checks below remain mandatory.
            lambda response: is_sample(response.request) and response.status == 200,
            timeout=27_000,
        ) as first:
            button.click()
        response = first.value
    except Exception:
        diagnostic["analytics_sample_request_traffic"] = dict(traffic)
        # The API client already carries the operator's bearer authorization.
        # Only the finite preparation phase enters evidence, never headers,
        # credentials, server diagnostics, exception strings, or camera media.
        try:
            status_report = client.json("/api/v1/operator/status", max_bytes=8192)
            preparation_phase = (
                status_report.get("person_preparation_phase")
                if type(status_report) is dict
                else None
            )
            diagnostic["analytics_sample_backend_preparation"] = (
                preparation_phase
                if preparation_phase in {"not_requested", "preparing", "ready", "failed", "closed"}
                else "unknown"
            )
        except Exception:
            diagnostic["analytics_sample_backend_preparation"] = "unavailable"
        try:
            diagnostic["analytics_sample_request_state"] = page.evaluate(
                """() => {
                  const message = document.getElementById('person-view-message')?.textContent || '';
                  const video = document.getElementById('live-video');
                  const button = document.getElementById('toggle-person-view');
                  const classification = message.startsWith('Processing a sampled frame')
                    ? 'processing'
                    : message.startsWith('Person detection is busy') ? 'busy'
                    : message.startsWith('Person detection is unavailable') ? 'unavailable'
                    : message.startsWith('Sampled view stopped while live video is paused')
                      ? 'paused'
                    : message.startsWith('Sampled view stopped while live video is buffering')
                      ? 'buffering'
                    : message.startsWith('Sampled view stopped while live video is unavailable')
                      ? 'video_unavailable'
                    : message.startsWith('Sampled view stopped while the camera is unavailable')
                      ? 'camera_unavailable'
                    : message.startsWith('Sampled view stopped') ? 'stopped_other'
                    : message.includes(' in this sample') ? 'sample_received'
                    : 'other';
                  return {
                    message_class: classification,
                    enabled: button?.getAttribute('aria-pressed') === 'true',
                    sample_button_enabled: !!button && !button.disabled,
                    live_playing: !!video && !video.paused && video.readyState >= 2,
                    canvas_visible: !document.getElementById('person-view-canvas')?.hidden
                  };
                }"""
            )
        except Exception:
            diagnostic["analytics_sample_request_state"] = {"available": False}
        raise
    finally:
        page.remove_listener("request", observed_request)
        page.remove_listener("response", observed_response)
        page.remove_listener("requestfailed", observed_failure)
    diagnostic["analytics_sample_request_traffic"] = dict(traffic)
    diagnostic["analytics_sample_response_no_store"] = (
        response.headers.get("cache-control") == "no-store"
    )
    _require(
        response.status == 200 and response.headers.get("cache-control") == "no-store",
        "Installed sampled-person response was refused or could be cached",
    )
    query = parse_qs(urlsplit(response.url).query, strict_parsing=True)
    _require(
        set(query) == {"width", "height", "frame_id"}
        and all(len(values) == 1 for values in query.values()),
        "Installed person frame used an unexpected bounded request contract",
    )
    width, height, frame_id = (int(query[name][0]) for name in ("width", "height", "frame_id"))
    received = response.json()
    payload = response.request.post_data_buffer
    # Restrict failure evidence to the server's finite status and booleans.
    # Neither transient frames nor response bodies enter the CI receipt.
    native_status = received.get("status") if type(received) is dict else None
    diagnostic["analytics_sample_response_contract"] = {
        "native_status": (
            native_status
            if type(native_status) is str and native_status in {"active", "busy", "unavailable"}
            else "invalid"
        ),
        "frame_correlated": (type(received) is dict and received.get("frame_id") == frame_id),
        "post_bytes_valid": isinstance(payload, bytes) and len(payload) == width * height * 4,
        "box_count": (
            min(33, len(received["boxes"]))
            if type(received) is dict and type(received.get("boxes")) is list
            else -1
        ),
    }
    _require(
        1 <= width <= 544
        and 1 <= height <= 320
        and 1 <= frame_id < 2**31
        and isinstance(payload, bytes)
        and len(payload) == width * height * 4
        and type(received) is dict
        and set(received) == {"status", "frame_id", "boxes"}
        and received["frame_id"] == frame_id
        and received["status"] in {"active", "busy"}
        and type(received["boxes"]) is list
        and len(received["boxes"]) <= 32,
        "Installed native person sampling did not bind to the captured live frame",
    )
    diagnostic["analytics_sample_stage"] = "same_frame_canvas"
    # Inspect only finite booleans, geometry and the number of the precise
    # RGB stroke pixels used by the shipped renderer. Never snapshot media.
    painted = page.wait_for_function(
        """() => {
          const canvas = document.getElementById('person-view-canvas');
          const video = document.getElementById('live-video');
          const message = document.getElementById('person-view-message')?.textContent || '';
          if (!canvas || canvas.hidden || !video || video.paused ||
              video.getVideoPlaybackQuality().totalVideoFrames < 4 ||
              canvas.width <= 0 || canvas.height <= 0 ||
              canvas.width > 544 || canvas.height > 320 ||
              !message.includes(' in this sample') || message.startsWith('0 ')) return false;
          const pixels = canvas.getContext('2d').getImageData(
            0, 0, canvas.width, canvas.height).data;
          let green = 0;
          for (let i = 0; i < pixels.length; i += 4) {
            if (pixels[i] === 104 && pixels[i + 1] === 210 &&
                pixels[i + 2] === 164 && pixels[i + 3] === 255) green++;
          }
          if (green < 12) return false;
          return {
            overlay_pixels: green,
            live_frames: video.getVideoPlaybackQuality().totalVideoFrames
          };
        }""",
        timeout=30_000,
    ).json_value()
    _require(
        type(painted) is dict
        and type(painted.get("overlay_pixels")) is int
        and 12 <= painted["overlay_pixels"] <= 544 * 320
        and type(painted.get("live_frames")) is int
        and 4 <= painted["live_frames"] <= 1_000_000,
        "Installed person rectangle was not painted over its immutable sample",
    )
    diagnostic["analytics_sample_stage"] = "clear_sample"
    button.click()
    page.wait_for_function(
        """() => {
          const canvas = document.getElementById('person-view-canvas');
          return canvas && canvas.hidden && canvas.width === 0 && canvas.height === 0;
        }""",
        timeout=5_000,
    )
    browser_helpers._open_camera_recordings(page, camera_id)
    diagnostic["analytics_sample_stage"] = "complete"
    return {
        "analytics_sampled_native_boxes": True,
        "analytics_sampled_same_frame_overlay": True,
        "analytics_sampled_canvas_cleared": True,
        "analytics_sampled_overlay_pixels": painted["overlay_pixels"],
        "analytics_sampled_live_decoded_frames": painted["live_frames"],
    }


def _poll_installed_person_event(client, route, diagnostic):
    """Tolerate only bounded gateway reconnection during the original 25s gate.

    The real positive person-event and same-frame box requirements remain
    mandatory. Retain only a finite HTTP status, never any response content.
    """
    status, body, _ = client.get(route + "/events")
    if status != 200:
        diagnostic["analytics_event_poll_status"] = (
            status if status in {401, 403, 404, 429, 500, 502, 503, 504} else "other"
        )
        if status in {502, 503, 504}:
            return None
        _require(False, "Installed person-event endpoint refused its authenticated poll")
    diagnostic["analytics_event_poll_status"] = 200
    payload = json.loads(body)
    _require(
        type(payload) is dict and type(payload.get("events")) is list,
        "Installed person-event endpoint returned an invalid event envelope",
    )
    if not payload["events"]:
        return None
    _require(
        type(payload["events"][0]) is dict,
        "Installed person-event endpoint returned an invalid event",
    )
    return payload["events"][0]


def _installed_analytics_journey(
    *,
    page,
    client,
    camera_id,
    parent,
    processes,
    publishers,
    source_uri,
    ffmpeg,
    helpers,
    browser_helpers,
    app,
    publisher_credentials,
    diagnostic,
):
    """Enable the shipped detector in the GUI and play its real recorded event."""
    __tracebackhide__ = True
    diagnostic["analytics_stage"] = "admitted_installed_model"
    _require(
        "K5_OPERATOR_PERSON_CONFIG" not in os.environ,
        "Installed analytics qualification must not inject a detector configuration",
    )
    _installed_person_fixture(parent, ffmpeg)
    route = f"/api/v1/operator/cameras/{camera_id}"
    browser_helpers._open_camera_recordings(page, camera_id)
    page.locator("#analytics-settings-form").wait_for(state="visible")
    page.wait_for_function("() => !document.getElementById('analytics-enabled').disabled")
    page.locator("#analytics-enabled").check()
    page.locator("#analytics-confidence").fill("50")
    with page.expect_response(
        lambda response: (
            response.request.method == "PATCH"
            and urlsplit(response.url).path == route + "/analytics"
        )
    ) as configured:
        page.locator("#analytics-save").click()
    _require(configured.value.status == 200, "Installed analytics could not be enabled in the GUI")
    settings = configured.value.json()
    _require(
        settings.get("enabled") is True
        and settings.get("confidence_threshold") == 0.5
        and settings.get("status") in {"starting", "active", "waiting"}
        and settings.get("source") == "recorded_camera_frames"
        and settings.get("model") == "person-detection-retail-0013",
        "Installed analytics did not admit the bundled recording detector",
    )
    diagnostic["analytics_stage"] = "positive_camera_recording"
    helpers._stop_owned(publishers[0])
    publishers[0] = _publish_fixture(
        parent,
        ffmpeg,
        source_uri,
        helpers,
        color="licensed person",
        credentials=publisher_credentials,
    )
    processes.append(publishers[0])
    positive_source_switched_at = datetime.now(UTC)
    detected = {}

    def positive_event():
        event = _poll_installed_person_event(client, route, diagnostic)
        if event is None:
            return False
        detected.update(event)
        return (
            detected.get("camera_id") == camera_id
            and type(detected.get("person_count")) is int
            and 1 <= detected["person_count"] <= 32
            and type(detected.get("confidence")) in (int, float)
            and 0.5 <= detected["confidence"] <= 1
        )

    try:
        _wait(
            positive_event,
            "Installed native detector records a real person event",
            process=app,
            timeout=25,
        )
    except BaseException:
        # Distinguish a detector startup fault from a healthy but lagging
        # recorder using only typed, credential-free backend status.
        try:
            state = client.json(route + "/analytics")
            sampled_at = state.get("last_sample_at")
            try:
                fresh_sample = (
                    datetime.fromisoformat(sampled_at.replace("Z", "+00:00"))
                    >= positive_source_switched_at
                )
            except (AttributeError, TypeError, ValueError):
                fresh_sample = False
            diagnostic["analytics_failure_state"] = {
                key: state.get(key)
                for key in (
                    "status",
                    "processed_frames",
                    "event_count",
                    "sample_attempts",
                    "sample_phase",
                    "last_completed_phase",
                )
            }
            diagnostic["analytics_failure_state"]["sample_after_source_switch"] = fresh_sample
        except Exception:
            diagnostic["analytics_failure_state"] = {"available": False}
        try:
            inventory = client.json("/api/v1/operator/cameras")
            camera = next(
                item for item in inventory["cameras"] if item.get("device_id") == camera_id
            )
            recording = camera.get("recording_status") or {}
            observed = recording.get("observed_bytes")
            progress_age = recording.get("last_progress_age_seconds")
            recording_state = camera.get("recording_state")
            diagnostic["analytics_failure_recorder"] = {
                "publisher_running": publishers[0].poll() is None,
                "camera_live": camera.get("status") == "live",
                "state": recording_state
                if recording_state in {"recording", "waiting", "paused", "stalled", "error"}
                else "other",
                "observed_bytes_positive": type(observed) is int and observed > 0,
                "recent_disk_progress": type(progress_age) in (int, float)
                and 0 <= progress_age <= 15,
            }
        except Exception:
            diagnostic["analytics_failure_recorder"] = {"available": False}
        raise
    occurred = datetime.fromisoformat(detected["occurred_at"].replace("Z", "+00:00"))
    _require(
        abs((datetime.now(UTC) - occurred).total_seconds()) < 30,
        "Person event was not mapped to the fresh recording's UTC timestamp",
    )
    sampled_evidence = _installed_sampled_person_boxes(
        page, camera_id, client, browser_helpers, diagnostic
    )
    diagnostic["analytics_stage"] = "event_recording_navigation"
    diagnostic["analytics_navigation_step"] = "history_range_click"
    page.locator("#last-24-hours").click()
    diagnostic["analytics_navigation_step"] = "events_refresh_click"
    page.locator("#analytics-refresh").click()
    expected_ms = (
        datetime.fromisoformat(detected["playback_at"].replace("Z", "+00:00")).timestamp() * 1000
    )

    def navigation_snapshot():
        # Only bounded browser state, never video source URLs or bearer values.
        return page.evaluate(
            """expected => {
              const video=document.getElementById('playback-video');
              const start=Date.parse(video?.dataset.utcStart || '');
              const from=Date.parse(document.getElementById('history-start')?.value || '');
              const to=Date.parse(document.getElementById('history-end')?.value || '');
              return {
                history_visible:!document.getElementById('history-panel')?.hidden,
                history_start_delta_seconds:Number.isFinite(from)?Math.round((from-expected)/1000):null,
                history_end_delta_seconds:Number.isFinite(to)?Math.round((to-expected)/1000):null,
                history_intervals:document.querySelectorAll('#history-list button').length,
                person_event_buttons:document.querySelectorAll('#analytics-events button').length,
                playback_start_delta_ms:Number.isFinite(start)?Math.round(start-expected):null,
                playback_ready_state:video?.readyState ?? null,
                playback_frame_count:video?.getVideoPlaybackQuality?.().totalVideoFrames ?? null,
                playback_paused:video?.paused ?? null,
                playback_error_code:video?.error?.code ?? null,
                playback_source_present:!!video?.currentSrc
              };
            }""",
            expected_ms,
        )

    diagnostic["analytics_navigation_step"] = "event_button_visible"
    event_button = page.locator(f'#analytics-events button[data-event-id="{detected["id"]}"]')
    try:
        event_button.wait_for(state="visible", timeout=15_000)
    except Exception:
        diagnostic["analytics_navigation_snapshot"] = navigation_snapshot()
        raise
    diagnostic["analytics_navigation_step"] = "event_button_click"
    event_button.click()
    diagnostic["analytics_navigation_step"] = "playback_frames"
    try:
        page.wait_for_function(
            """chosen => {
              const video=document.getElementById('playback-video');
              return Math.abs(Date.parse(video.dataset.utcStart || '')-chosen)<1000 &&
                video.readyState>=2 && !video.paused &&
                video.getVideoPlaybackQuality().totalVideoFrames>=4;
            }""",
            arg=expected_ms,
            timeout=20_000,
        )
    except Exception:
        diagnostic["analytics_navigation_snapshot"] = navigation_snapshot()
        raise
    diagnostic["analytics_navigation_step"] = "playback_decoded"
    event_frames = page.locator("#playback-video").evaluate(
        "video => video.getVideoPlaybackQuality().totalVideoFrames"
    )
    diagnostic["analytics_navigation_step"] = "analytics_status_fetch"
    status = client.json(route + "/analytics")
    diagnostic["analytics_navigation_step"] = "analytics_status_verified"
    _require(
        status.get("processed_frames", 0) >= 1 and status.get("event_count", 0) >= 1,
        "Installed detector reported an event without processing recorded frames",
    )
    diagnostic["analytics_navigation_step"] = "person_publisher_stop"
    helpers._stop_owned(publishers[0])
    diagnostic["analytics_navigation_step"] = "red_publisher_restart"
    publishers[0] = _publish_fixture(
        parent, ffmpeg, source_uri, helpers, credentials=publisher_credentials
    )
    processes.append(publishers[0])
    diagnostic["analytics_navigation_step"] = "red_live_watch_open"
    browser_helpers._open_camera_watch(page, camera_id)
    diagnostic["analytics_navigation_step"] = "red_live_decode"
    browser_helpers._decoded_color(page, "live-video", "red")
    diagnostic["analytics_stage"] = "complete"
    diagnostic["analytics_navigation_step"] = "complete"
    return {
        "product_analytics_enablement": True,
        "analytics_bundled_native_inference": True,
        "analytics_gui_configuration": True,
        "analytics_person_event_recorded": True,
        "analytics_event_utc_playback": True,
        "analytics_event_playback_decoded_frames": event_frames,
        "analytics_positive_person_count": detected["person_count"],
        "analytics_processed_frames": status["processed_frames"],
        "analytics_model_revision": "6697dead54ed1cdd664b0313189c2cb52ee6335e",
        "analytics_mock_inference": False,
        "analytics_accuracy_acceptance": False,
        "analytics_public_fixture_only": True,
        **sampled_evidence,
    }, detected


def _analytics_permission_journey(
    page, *, admin_helpers, camera_ids, private_state, analytics_event, diagnostic
):
    """Use the same bounded browser observer as the installed admin journey."""
    __tracebackhide__ = True
    denied = []
    context, observer = admin_helpers._new_user_page(page)
    origin = admin_helpers._origin(page)
    started = time.monotonic()

    def request(phase, token, method, route, data=None):
        diagnostic["analytics_permissions_phase"] = phase
        result = admin_helpers._api(observer, token, method, route, data)
        diagnostic["analytics_permissions_http_status"] = result[0]
        return result

    try:
        diagnostic["analytics_permissions_phase"] = "observer_navigation"
        observer.goto(origin + "/operator/")
        for account in private_state["users"]:
            status, issued = request(
                "password_login",
                None,
                "POST",
                "/api/v1/auth/login",
                {"username": account["username"], "password": account["password"]},
            )
            _require(status == 200, "Limited analytics observer could not authenticate")
            token = issued.get("session_token") if isinstance(issued, dict) else None
            _require(
                isinstance(token, str) and 32 <= len(token) <= 128,
                "Limited analytics observer did not receive an opaque session",
            )
            permitted = set(account["camera_access"]["camera_ids"])
            _require(
                permitted
                and permitted.issubset(set(camera_ids))
                and camera_ids[3] not in permitted,
                "Analytics role fixture lost its selected-camera boundary",
            )
            assigned_id = next(identifier for identifier in camera_ids if identifier in permitted)
            allowed = f"/api/v1/operator/cameras/{assigned_id}"
            restricted = f"/api/v1/operator/cameras/{camera_ids[3]}"
            for suffix in ("/analytics", "/events"):
                _require(
                    request("assigned_camera_read", token, "GET", allowed + suffix)[0] == 200,
                    "Assigned camera analytics were denied",
                )
                status = request("unassigned_camera_read", token, "GET", restricted + suffix)[0]
                _require(status == 404, "Unassigned camera analytics were exposed")
                denied.append(status)
            # The live person-event camera changes stream while other installed
            # user checks run. The viewer must be denied its event history;
            # the separately authorized operator must retrieve the actual
            # captured event. Both still prove live video on generated cameras.
            person_events = f"/api/v1/operator/cameras/{camera_ids[0]}/events"
            if account["role"] == "viewer":
                status, _ = request("viewer_person_events_denied", token, "GET", person_events)
                _require(status == 404, "Viewer received unassigned person analytics")
                denied.append(status)
            else:
                _require(account["role"] == "operator", "Unknown analytics test user role")
                status, events = request("operator_person_events", token, "GET", person_events)
                _require(
                    status == 200
                    and isinstance(events, dict)
                    and any(
                        isinstance(event, dict) and event.get("id") == analytics_event["id"]
                        for event in events.get("events", [])
                    ),
                    "Authorized operator could not read the recorded person event",
                )
            status = request(
                "limited_analytics_write",
                token,
                "PATCH",
                allowed + "/analytics",
                {"enabled": False, "confidence_threshold": 0.5},
            )[0]
            _require(status == 403, "A limited account changed camera analytics settings")
            denied.append(status)
            request("session_cleanup", token, "POST", "/api/v1/auth/logout")
        context.clear_cookies()
        for suffix in ("/analytics", "/events"):
            status = request(
                "anonymous_camera_read",
                None,
                "GET",
                f"/api/v1/operator/cameras/{camera_ids[0]}" + suffix,
            )[0]
            _require(status == 401, "Anonymous analytics access was admitted")
            denied.append(status)
        diagnostic["analytics_permissions_phase"] = "complete"
        return {"analytics_role_camera_permissions": True, "analytics_denied_http_statuses": denied}
    except BaseException as error:
        diagnostic["analytics_permissions_failure_type"] = (
            type(error).__name__
            if type(error).__name__
            in {"TimeoutError", "Error", "Failed", "AssertionError", "RuntimeError", "ValueError"}
            else "other"
        )
        raise
    finally:
        diagnostic["analytics_permissions_seconds"] = round(time.monotonic() - started, 3)
        context.close()


def _vm_clock_phase(
    *,
    page,
    context,
    client,
    profile,
    camera_id,
    parent,
    processes,
    source_uri,
    ffmpeg,
    ffprobe,
    helpers,
    browser_helpers,
    app,
    setup_sha256,
    diagnostic,
    publisher,
    camera_directory,
    publisher_credentials,
    analytics_event,
):
    """Actually change only an opted-in disposable Windows guest's UTC clock."""
    __tracebackhide__ = True
    route = "/api/v1/operator/cameras/" + camera_id
    original_token = client.token
    captured_live = []
    non_loopback = []
    phase_times = []
    phase_checkpoint = time.monotonic()
    diagnostic["vm_clock_phase_seconds"] = phase_times

    def mark_clock_phase(name):
        nonlocal phase_checkpoint
        now = time.monotonic()
        phase_times.append((name, round(now - phase_checkpoint, 2)))
        phase_checkpoint = now

    def observed_request(request):
        parsed = urlsplit(request.url)
        if parsed.path.startswith("/media/live/") and parsed.path.endswith(".m3u8"):
            # A past low-latency blocking query can become stale when a stream
            # resets; fetch the actual admitted playlist without that query.
            captured_live[:] = [parsed._replace(query="", fragment="").geturl()]

    def loopback_only(route):
        parsed = urlsplit(route.request.url)
        if parsed.scheme in {"http", "https"} and parsed.hostname == "127.0.0.1":
            route.continue_()
        elif parsed.scheme in {"blob", "data"}:
            route.continue_()
        else:
            non_loopback[:] = [True]
            route.abort()

    def camera_disconnected():
        cameras = client.json("/api/v1/operator/cameras")["cameras"]
        return any(
            camera.get("device_id") == camera_id and camera.get("status") == "disconnected"
            for camera in cameras
        )

    def displayed_recorder_utc_matches():
        text = page.locator("#recorder-time-status").inner_text()
        if not text.startswith("Recorder time: ") or " UTC" not in text:
            return False
        try:
            timestamp = text.removeprefix("Recorder time: ").split(" UTC", 1)[0]
            displayed = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        except ValueError:
            return False
        return abs((displayed - datetime.now(UTC)).total_seconds()) < 10

    diagnostic["vm_clock_stage"] = "finalize_closed_segments"
    # Interrupt only our generated publisher. An observed disconnected source
    # closes the real recorder; active, growing files are never hashed as fixed.
    helpers._stop_owned(publisher)
    _wait(camera_disconnected, "VM clock closed-source finalization", process=app, timeout=15)
    _wait(
        lambda: _recording_bytes(profile, camera_directory) > 0,
        "VM clock closed recordings",
        process=app,
    )
    closed = _wait(
        lambda: _closed_hashes(profile, camera_directory),
        "VM clock exclusive closed-recording admission",
        process=app,
        timeout=10,
    )
    _require(
        sum(_recorded_segment_frames(ffprobe, path) for path in closed) > 0,
        "VM clock pre-shift closed footage is not decodable",
    )
    historical_start = datetime.now(UTC) - timedelta(minutes=5)
    historical_end = datetime.now(UTC) + timedelta(seconds=10)
    history_query = "?" + urlencode(
        {"start": historical_start.isoformat(), "end": historical_end.isoformat()}
    )
    before_history = client.json(route + "/history" + history_query)["spans"]
    _require(bool(before_history), "VM clock pre-shift recording history is empty")
    mark_clock_phase("closed_footage_admission")
    page.on("request", observed_request)
    processes.append(
        _publish_fixture(parent, ffmpeg, source_uri, helpers, credentials=publisher_credentials)
    )
    baseline_bytes = _recording_bytes(profile, camera_directory)
    _wait(
        lambda: _tray_recording(
            client, profile, camera_id, baseline_bytes, {}, camera_directory=camera_directory
        ),
        "VM clock source reconnect and recording",
        process=app,
        timeout=15,
    )
    browser_helpers._decoded_color(page, "live-video", "red")
    _wait(lambda: bool(captured_live), "VM clock actual HLS request", process=app, timeout=10)
    live_url = captured_live[-1]
    _require(
        urlsplit(live_url).hostname == "127.0.0.1"
        and context.request.get(live_url, timeout=5000).status == 200,
        "VM clock baseline authenticated live media failed",
    )
    mark_clock_phase("source_reconnect_live")
    phases = []
    guard = _helpers("windows_vm_clock").WindowsVmClockGuard(
        parent / "vm-clock-guard.json", watchdog_seconds=90
    )
    page.route("**/*", loopback_only)
    try:
        with guard:
            diagnostic["vm_clock_stage"] = "guard_armed"
            for offset, direction in ((172800, "forward"), (-172800, "backward")):
                baseline = client.json("/api/v1/operator/status")
                previous_count = baseline["clock"]["change_count"]
                bytes_before = _recording_bytes(profile, camera_directory)
                diagnostic["vm_clock_stage"] = direction + "_shift"
                shifted_at = time.monotonic()
                native = guard.shift(offset)
                actual_step = native["actual_step_seconds"]
                diagnostic["vm_clock_native_step_seconds"] = actual_step
                mark_clock_phase(direction + "_native_shift")
                _require(
                    type(actual_step) in (int, float) and abs(actual_step - offset) <= 2,
                    "VM clock native UTC did not perform the requested real step",
                )

                def clock_changed(previous_count=previous_count, offset=offset):
                    status = client.json("/api/v1/operator/status")
                    clock = status["clock"]
                    step = clock.get("last_step_seconds")
                    if (
                        clock.get("status") == "changed"
                        and clock.get("change_count", 0) > previous_count
                        and type(step) in (int, float)
                        and abs(step - offset) <= 2
                        and clock.get("sync_verified") is False
                    ):
                        return status
                    return None

                diagnostic["vm_clock_stage"] = direction + "_signed_recorder_alert"
                changed = _wait(
                    clock_changed, "VM clock signed recorder alert", process=app, timeout=10
                )
                observed_utc = datetime.fromisoformat(changed["server_utc"].replace("Z", "+00:00"))
                _require(
                    abs((observed_utc - datetime.now(UTC)).total_seconds()) < 2,
                    "VM clock recorder UTC did not follow the actual guest clock",
                )
                _require(
                    client.token == original_token
                    and client.json("/api/v1/auth/me").get("role") == "administrator",
                    "VM clock step invalidated the existing human session",
                )
                diagnostic["vm_clock_stage"] = direction + "_warning_visible"
                _wait(
                    lambda: page.locator("#recorder-time-warning").is_visible(),
                    "VM clock warning visible after real step",
                    process=app,
                    timeout=10,
                )
                diagnostic["vm_clock_stage"] = direction + "_warning_direction"
                _wait(
                    lambda direction=direction: (
                        direction in page.locator("#recorder-time-warning").inner_text()
                    ),
                    "VM clock warning direction",
                    process=app,
                    timeout=10,
                )
                diagnostic["vm_clock_stage"] = direction + "_recorder_utc"
                _wait(
                    displayed_recorder_utc_matches,
                    "VM clock visible recorder UTC",
                    process=app,
                    timeout=10,
                )
                mark_clock_phase(direction + "_recorder_warning")
                # Observe at least two normal five-second storage-maintenance
                # cycles and allow pre-step live buffers to drain naturally.
                _wait(
                    lambda since=shifted_at: time.monotonic() - since >= 10,
                    "VM clock held observation interval",
                    process=app,
                    timeout=15,
                )
                mark_clock_phase(direction + "_two_maintenance_cycles")
                diagnostic["vm_clock_stage"] = direction + "_live"
                diagnostic["vm_clock_stage"] = direction + "_live_media"
                live_status = context.request.get(live_url, timeout=5000).status
                diagnostic["vm_clock_media_http_status"] = live_status
                _require(live_status == 200, "VM clock step expired authenticated live media")
                _require(
                    page.locator("#workspace").is_visible(),
                    "VM clock step signed out the browser's existing human session",
                )
                diagnostic["vm_clock_stage"] = direction + "_live_decode"
                frames_before = browser_helpers._decoded_color(page, "live-video", "red")
                page.wait_for_function(
                    """before => {
                      const video = document.getElementById('live-video');
                      return video.readyState >= 2 && !video.paused &&
                        video.getVideoPlaybackQuality().totalVideoFrames >= before + 4;
                    }""",
                    arg=frames_before,
                    timeout=10_000,
                )
                frames_after = browser_helpers._decoded_color(page, "live-video", "red")
                diagnostic["vm_clock_stage"] = direction + "_recording_progress"
                recording = {}
                _wait(
                    lambda before=bytes_before, result=recording: _tray_recording(
                        client,
                        profile,
                        camera_id,
                        before,
                        result,
                        camera_directory=camera_directory,
                    ),
                    "VM clock recording progress after real UTC step",
                    process=app,
                    timeout=15,
                )
                now = datetime.now(UTC)
                first, last = now - timedelta(minutes=2), now + timedelta(minutes=1)
                phase_query = "?" + urlencode(
                    {
                        "start": first.isoformat(),
                        "end": last.isoformat(),
                    }
                )

                def shifted_history(query=phase_query, first=first, last=last):
                    spans = client.json(route + "/history" + query)["spans"]
                    return any(
                        first <= datetime.fromisoformat(span["started_at"]) <= last
                        and span["duration_seconds"] > 0
                        for span in spans
                    )

                diagnostic["vm_clock_stage"] = direction + "_history_poll"
                _wait(
                    shifted_history,
                    "VM clock shifted recorder UTC history query",
                    process=app,
                    timeout=10,
                )
                diagnostic["vm_clock_stage"] = direction + "_events_fetch"
                retained_events = client.json(route + "/events")["events"]
                _require(
                    any(
                        event["id"] == analytics_event["id"]
                        and event["occurred_at"] == analytics_event["occurred_at"]
                        and event["playback_at"] == analytics_event["playback_at"]
                        for event in retained_events
                    ),
                    "VM clock correction changed the recorded person event's UTC identity",
                )
                event_time = datetime.fromisoformat(
                    analytics_event["occurred_at"].replace("Z", "+00:00")
                )
                event_query = "?" + urlencode(
                    {
                        "start": (event_time - timedelta(seconds=2)).isoformat(),
                        "end": (event_time + timedelta(seconds=2)).isoformat(),
                    }
                )
                event_spans = client.json(route + "/history" + event_query)["spans"]
                _require(
                    any(
                        datetime.fromisoformat(span["started_at"])
                        <= event_time
                        < datetime.fromisoformat(span["started_at"])
                        + timedelta(seconds=span["duration_seconds"])
                        for span in event_spans
                    ),
                    "VM clock correction detached a person event from its recorded UTC footage",
                )
                _require(_hashes_preserved(closed), "VM clock step changed closed footage hashes")
                held = guard.snapshot()
                _require(
                    abs(held["baseline_residual_seconds"] - offset) <= 2,
                    "VM clock requested UTC step did not remain in effect",
                )
                mark_clock_phase(direction + "_live_recording_history")
                diagnostic["vm_clock_stage"] = direction + "_restore"
                restoration = guard.restore()
                _require(
                    restoration["restored"] is True and abs(restoration["residual_seconds"]) <= 2,
                    "VM clock UTC restoration was not verified",
                )
                mark_clock_phase(direction + "_native_restore")
                # Force an observation of the correction before the next phase,
                # so its signed step is measured from restored recorder UTC.
                client.json("/api/v1/operator/status")
                phases.append(
                    {
                        "requested_offset_seconds": offset,
                        "actual_step_seconds": actual_step,
                        "held_offset_seconds": held["baseline_residual_seconds"],
                        "monotonic_elapsed_seconds": held["elapsed_seconds"]
                        - native["before"]["elapsed_seconds"],
                        "same_human_session": True,
                        "clock_alert_direction": direction,
                        "recorder_utc_verified": True,
                        "history_utc_query_verified": True,
                        "analytics_event_utc_preserved": True,
                        "analytics_event_recording_covered": True,
                        "live_http_status": live_status,
                        "live_decoded_frames_before": frames_before,
                        "live_decoded_frames_after": frames_after,
                        "recording_bytes_before": bytes_before,
                        "recording_bytes_after": recording["recording_bytes_after"],
                        "closed_segment_hashes_preserved": True,
                        "utc_restored": restoration["restored"],
                        "restore_residual_seconds": restoration["residual_seconds"],
                    }
                )
    finally:
        page.unroute("**/*", loopback_only)
        page.remove_listener("request", observed_request)
        try:
            diagnostic["vm_clock_guard"] = guard.receipt()
        except RuntimeError:
            diagnostic["vm_clock_guard"] = {"available": False}
    # Identify the exact failing post-restoration operation without exposing
    # recorded media, camera addresses or authenticated request contents.
    diagnostic["vm_clock_stage"] = "restored_closed_hashes"
    _require(_hashes_preserved(closed), "VM clock restoration changed closed footage hashes")
    diagnostic["vm_clock_stage"] = "restored_history_query"
    try:
        restored_history = client.json(route + "/history" + history_query)["spans"]
    finally:
        # The local client reports transport failures as RuntimeError. Retain
        # only its bounded, route-free exception class and elapsed time.
        if client.transport_failure is not None:
            diagnostic["vm_clock_history_transport"] = dict(client.transport_failure)
    diagnostic["vm_clock_stage"] = "restored_history_unchanged"
    _require(
        all(span in restored_history for span in before_history),
        "VM clock restoration lost retained recording history",
    )
    diagnostic["vm_clock_stage"] = "restored_browser_history_submit"
    page.locator("#history-tab").click()
    page.locator("#history-start").fill(browser_helpers._datetime_local_value(historical_start))
    page.locator("#history-end").fill(browser_helpers._datetime_local_value(historical_end))
    page.locator("#history-form").evaluate("form => form.requestSubmit()")
    diagnostic["vm_clock_stage"] = "restored_browser_history_visible"
    page.locator(".history-item").first.wait_for(state="visible", timeout=15_000)
    page.locator(".history-item").first.click()
    page.locator("#play-recording").click()
    diagnostic["vm_clock_stage"] = "restored_playback_frames"
    playback_frames = browser_helpers._decoded_color(page, "playback-video", "red")
    page.locator("#live-tab").click()
    diagnostic["vm_clock_stage"] = "restored_live_frames"
    browser_helpers._decoded_color(page, "live-video", "red")
    _require(not non_loopback, "VM clock proof attempted an external request while shifted")
    diagnostic["vm_clock_stage"] = "restored_guard_receipt"
    guard_receipt = guard.receipt()
    _require(
        guard_receipt["reason"] == "closed"
        and guard_receipt["watchdog_armed"] is True
        and guard_receipt["privilege_verified"] is True
        and len(guard_receipt["shifts"]) == 2
        and guard_receipt["clock_restored"] is True
        and guard_receipt["services_restored"] is True,
        "VM clock guard did not restore UTC and original guest time services",
    )
    mark_clock_phase("restored_playback")
    diagnostic["vm_clock_stage"] = "complete"
    return {
        "schema_version": "1",
        "head_sha": os.environ["K5_DESKTOP_HEAD_SHA"],
        "installer_sha256": setup_sha256,
        "method": "windows_SetSystemTime",
        "offset_seconds": [172800, -172800],
        "simulated_time": False,
        "browser_clock_only": False,
        "phases": phases,
        "closed_segments": len(closed),
        "closed_segment_hashes_preserved": True,
        "analytics_events_utc_preserved": True,
        "retained_playback_decoded_frames": playback_frames,
        "loopback_only": True,
        "guard": guard_receipt,
        "utc_restored": guard_receipt["clock_restored"],
        "time_services_restored": guard_receipt["services_restored"],
    }


def test_native_windows_click_install_account_browser_tray_restart_uninstall(
    record_testsuite_property,
):
    __tracebackhide__ = True
    started = time.monotonic()
    api = _Windows()
    runtime = _helpers("test_operator_product_runtime")
    browser_helpers = _helpers("test_operator_beta_browser")
    admin_helpers = _helpers("test_operator_admin_browser")
    settings_helpers = _helpers("installed_settings_acceptance")
    helpers = runtime._helpers()
    browser_api = pytest.importorskip("playwright.sync_api")
    _require(version("playwright") == "1.58.0", "Expected reviewed Playwright version is absent")
    setup = Path(os.environ["K5_DESKTOP_SETUP"]).resolve(strict=True)
    vm_clock_enabled = os.environ.get("K5_VM_CLOCK_ACCEPTANCE") == "1"
    setup_sha256 = None
    if vm_clock_enabled:
        with setup.open("rb") as stream:
            setup_sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
        _require(
            setup_sha256 == os.environ.get("K5_DESKTOP_SETUP_SHA256")
            and len(os.environ.get("K5_DESKTOP_HEAD_SHA", "")) == 40,
            "VM clock exact installer/revision identity was not admitted",
        )
    media, ffmpeg, ffprobe = (
        helpers._tool(name)
        for name in ("K5_MEDIAMTX_DONOR_EXE", "K5_DONOR_FFMPEG", "K5_DONOR_FFPROBE")
    )
    parent = Path(os.environ["LOCALAPPDATA"]) / ("K5 Desktop Proof-" + uuid4().hex)
    identity_state.validate_new_identity_directory(parent)
    parent.mkdir(mode=0o700)
    installed, profile = parent / "installed app with spaces", parent / "profile with spaces"
    username, password = "fixture-admin", secrets.token_urlsafe(32)
    processes, reservations = [], []
    admin_executor = None
    admin_future = None
    settings_future = None
    settings_diagnostic = {}
    permissions_executor = None
    permissions_future = None
    cohort = _Cohort(api)
    playwright = browser = context = page = onvif_camera = None
    onvif_cameras, sources, publishers = [], [], []
    primary_publisher_credentials = ("generated-fixture-publisher", secrets.token_urlsafe(24))
    form = None
    app = None
    stage, failure, cleanup_failed = "installer", None, False
    stage_started = started
    stage_durations = []

    def _enter_stage(name):
        nonlocal stage, stage_started
        now = time.monotonic()
        stage_durations.append((stage, round(now - stage_started, 2)))
        stage, stage_started = name, now

    failure_detail = None
    readiness_diagnostic = {"health_http_status": 0}
    recording_diagnostic = {}
    vm_clock_diagnostic = {}
    nvr_diagnostic = {}
    export_proof = {}
    receipt = {
        "schema_version": "1",
        "platform": platform.system(),
        "native_windows_execution": True,
        "generated_media_only": True,
        "physical_camera_acceptance": False,
        "capacity_acceptance": False,
        "endurance_acceptance": False,
        "windows_service": False,
        "host_clock_step_acceptance": False,
        "mocked_gui": False,
        "user_terminal_required": False,
        "browser_channel": "msedge",
        "playwright_version": "1.58.0",
    }

    def admin_progress(phase):
        if (
            phase
            in {
                "admin_create_accounts",
                "admin_activate_viewer",
                "admin_activate_operator",
                "admin_revoke_camera_access",
                "admin_disable_and_reenable",
                "admin_reset_password",
                "admin_visible_audit",
            }
            or phase.startswith("admin_operator_")
            or any(
                phase == f"admin_{role}_{step}"
                for role in ("viewer", "operator")
                for step in (
                    "open_tab",
                    "wait_panel",
                    "open_users",
                    "wait_users",
                    "open_editor",
                    "wait_editor",
                    "submit_create",
                    "one_time_credential",
                    "wait_created_row",
                )
            )
        ):
            nvr_diagnostic["admin_stage"] = phase

    def _parallel_admin_journey():
        # Own Playwright and Chromium entirely on this thread. Concurrent
        # user administration must still exercise the installed product UI.
        with browser_api.sync_playwright() as admin_playwright:
            admin_browser = admin_playwright.chromium.launch(channel="msedge", headless=True)
            try:
                admin_context = admin_browser.new_context(viewport={"width": 1440, "height": 900})
                try:
                    admin_page = admin_context.new_page()
                    admin_page.set_default_timeout(25_000)
                    admin_helpers._activate(
                        admin_page,
                        client.origin,
                        secondary_admin_username,
                        secondary_admin_credential,
                        secondary_admin_password,
                    )
                    return admin_helpers.run_admin_journey(
                        admin_page,
                        admin_username=secondary_admin_username,
                        admin_password=secondary_admin_password,
                        # Viewer and operator decode independent generated
                        # lime/blue feeds while NVR exercises red live/export.
                        # The operator still receives the red camera grant
                        # required to prove its real recorded person event;
                        # neither limited account receives the yellow camera.
                        camera_ids=[camera_ids[2], camera_ids[1]],
                        colors=("lime", "blue"),
                        operator_event_camera_id=camera_ids[0],
                        client=client,
                        progress=admin_progress,
                    )
                finally:
                    admin_context.close()
            finally:
                admin_browser.close()

    def _parallel_settings_journey():
        # Run the entire installed GUI settings/security/integrations proof
        # with an independently authenticated administrator on its own Edge
        # thread. The original administrator remains active for recording and
        # timeline acceptance. The worker pool serializes this after the
        # account/role/audit journey so those shared users are fully admitted.
        _, private_state = admin_future.result(timeout=120)
        with browser_api.sync_playwright() as settings_playwright:
            settings_browser = settings_playwright.chromium.launch(channel="msedge", headless=True)
            try:
                settings_context = settings_browser.new_context(
                    viewport={"width": 1440, "height": 900}
                )
                try:
                    settings_page = settings_context.new_page()
                    settings_page.set_default_timeout(25_000)
                    settings_client = runtime._Client(port)
                    settings_client.token = admin_helpers._login(
                        settings_page,
                        client.origin,
                        secondary_admin_username,
                        secondary_admin_password,
                    )
                    evidence = settings_helpers.run_installed_settings_journey(
                        settings_page,
                        client=settings_client,
                        admin_helpers=admin_helpers,
                        browser_helpers=browser_helpers,
                        admin_username=secondary_admin_username,
                        admin_password=secondary_admin_password,
                        camera_ids=camera_ids,
                        private_state=private_state,
                        analytics_event=analytics_event,
                        parent=parent,
                        diagnostic=settings_diagnostic,
                    )
                    return evidence, settings_diagnostic
                finally:
                    settings_context.close()
            finally:
                settings_browser.close()

    def _parallel_analytics_permissions_journey():
        # A separate, owned Edge instance proves all post-clock limited-role
        # authorization checks while the primary UI is exercised through
        # sign-out, tray operation and browser-closed recording. This preserves
        # every HTTP denial and the exact event access assertion.
        with browser_api.sync_playwright() as permission_playwright:
            permission_browser = permission_playwright.chromium.launch(
                channel="msedge", headless=True
            )
            try:
                permission_context = permission_browser.new_context(
                    viewport={"width": 1440, "height": 900}
                )
                try:
                    permission_page = permission_context.new_page()
                    permission_page.set_default_timeout(12_000)
                    permission_page.goto(client.origin + "/operator/")
                    permission_diagnostic = {}
                    evidence = _analytics_permission_journey(
                        permission_page,
                        admin_helpers=admin_helpers,
                        camera_ids=camera_ids,
                        private_state=admin_private_state,
                        analytics_event=analytics_event,
                        diagnostic=permission_diagnostic,
                    )
                    return evidence, permission_diagnostic
                finally:
                    permission_context.close()
            finally:
                permission_browser.close()

    try:
        _install_gui(setup, installed, api, diagnostic=readiness_diagnostic)
        _require((installed / "K5Vision.exe").is_file(), "Installed desktop executable is absent")
        _require((installed / "runtime/python.exe").is_file(), "Embedded Python was not installed")
        _verify_installed_worker_bundle(installed)
        receipt["installed_worker_bundle_verified"] = True
        receipt["actual_gui_installer"] = True
        reservations, ports = helpers._reserve_ports(5)
        port, upstream_ports = ports[0], ports[1:]
        for listener in reservations:
            listener.close()
        _enter_stage("first_account_screen")
        app = _start_app(installed, profile, port, cohort)
        form = _wait(lambda: api.form(app.pid), "First account window", process=app)
        _wait(
            lambda: (
                api.api.IsWindowVisible(api.control(form, _CREATE))
                and api.api.IsWindowEnabled(api.control(form, _CREATE))
            ),
            "Create account button",
            process=app,
        )
        api.set_text(form, _USERNAME, username)
        api.set_text(form, _PASSWORD, password)
        api.set_text(form, _CONFIRM, password)
        api.click(api.control(form, _CREATE))
        client = runtime._Client(port)
        _enter_stage("embedded_product_startup")
        _wait(
            lambda: _healthy(client, readiness_diagnostic),
            "Desktop embedded product startup",
            process=app,
            timeout=60,
        )
        runtime._login(client, username, password)
        _require(
            client.json("/api/v1/auth/me").get("role") == "administrator",
            "GUI account did not authorize an administrator",
        )
        _require(
            api.control_text(form, _PASSWORD) == "" and api.control_text(form, _CONFIRM) == "",
            "Desktop left the initial password in its input controls",
        )
        receipt["actual_gui_account_setup"] = True
        receipt["administrator_login"] = True
        _enter_stage("native_default_browser_handoff")
        _wait(
            lambda: api.api.IsWindowEnabled(api.control(form, _OPEN)),
            "Native Open cameras button",
            process=app,
            timeout=10,
        )
        receipt.update(_default_browser_handoff(api, form, port, diagnostic=readiness_diagnostic))
        identity = identity_state.load_identity_state(profile / "identity")
        python_processes = [
            process
            for process in cohort.snapshot()
            if process.is_running() and process.name().casefold() in {"python.exe", "pythonw.exe"}
        ]
        _require(bool(python_processes), "Desktop did not start its embedded Python process")
        _require(
            all(
                Path(process.exe()).resolve().is_relative_to(installed)
                for process in python_processes
            ),
            "Desktop used an external Python installation",
        )
        receipt["embedded_python_independent_of_path"] = True
        _enter_stage("generated_camera_fixture")
        # The four independently addressed, generated fixtures require the same
        # source bytes and listener admission, but need not be provisioned
        # serially. Bound startup concurrency to avoid overwhelming the VM.
        with ThreadPoolExecutor(max_workers=2) as fixture_pool:
            fixture_futures = []
            for index, ((_, host, color), upstream_port) in enumerate(
                zip(_DESKTOP_CAMERAS, upstream_ports, strict=True)
            ):
                camera_fixture = _helpers("onvif_camera_fixture").OnvifCameraFixture(
                    host=host,
                    stream_uri=f"rtsp://{host}:{upstream_port}/generated",
                    clock_skew_seconds=1200 if index % 2 == 0 else -1200,
                    clock_mode="NTP",
                )
                onvif_cameras.append(camera_fixture)
                fixture_futures.append(
                    fixture_pool.submit(
                        _generate_fixture,
                        parent,
                        media,
                        ffmpeg,
                        upstream_port,
                        helpers,
                        host=host,
                        color=color,
                        camera_credentials=(camera_fixture.username, camera_fixture.password)
                        if index == 0
                        else None,
                        publisher_credentials=primary_publisher_credentials if index == 0 else None,
                        extra_stream_paths=("direct-channel",) if index == 0 else (),
                    )
                )
            fixture_failure = None
            # Collect in original camera order so source identities and all
            # subsequent camera/media assertions are exactly unchanged. Even
            # after a failed worker, collect and own all successful processes
            # before the outer cleanup runs.
            for future in fixture_futures:
                try:
                    generated, source = future.result()
                except BaseException as error:
                    if fixture_failure is None:
                        fixture_failure = error
                    continue
                processes.extend(generated)
                publishers.append(generated[-1])
                sources.append(source)
        if fixture_failure is not None:
            raise fixture_failure
        source_uri, onvif_camera = sources[0], onvif_cameras[0]
        _enter_stage("actual_camera_rtsp_authentication")
        browser_helpers._assert_rtsp_read_auth(
            ffprobe, source_uri, onvif_camera.username, onvif_camera.password
        )
        receipt["rtsp_read_auth_camera_count"] = 1
        receipt["authenticated_camera_read"] = True
        receipt["rtsp_anonymous_read_rejected"] = True
        receipt["rtsp_wrong_password_rejected"] = True
        _enter_stage("actual_browser_enrollment")
        readiness_diagnostic["browser_enrollment_step"] = "launch_browser"
        playwright = browser_api.sync_playwright().start()
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        receipt["browser_version"] = browser.version
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        page.set_default_timeout(25_000)
        readiness_diagnostic["browser_enrollment_step"] = "navigate_operator"
        page.goto(client.origin + "/operator/")
        page.locator("#username").fill(username)
        page.locator("#password").fill(password)
        readiness_diagnostic["browser_enrollment_step"] = "submit_login"
        with page.expect_response(
            lambda response: (
                response.request.method == "POST" and response.url.endswith("/api/v1/auth/login")
            )
        ) as browser_login:
            page.locator("#sign-in-button").click()
        _require(browser_login.value.status == 200, "Desktop browser login failed")
        browser_token = browser_login.value.json().get("session_token")
        _require(
            isinstance(browser_token, str) and 32 <= len(browser_token) <= 128,
            "Desktop browser login did not return a bounded session",
        )
        # New human login revokes the earlier account-verification session.
        # Observe the backend using the browser's current session without a
        # second login that would revoke the active browser.
        client.token = browser_token
        readiness_diagnostic["browser_enrollment_step"] = "workspace_ready"
        page.locator("#workspace").wait_for(state="visible")
        readiness_diagnostic["browser_enrollment_step"] = "recorder_time_health"
        browser_helpers._assert_recorder_time_health(page, client)
        receipt["recorder_time_health_displayed"] = True
        camera_ids = []
        discovery_completed = False
        for index, ((name, _, _), camera_fixture) in enumerate(
            zip(_DESKTOP_CAMERAS, onvif_cameras, strict=True)
        ):
            readiness_diagnostic["browser_enrollment_step"] = "onvif_enroll"
            readiness_diagnostic["browser_onvif_camera_number"] = index + 1
            enrolled, discovery = browser_helpers._onvif_enroll(
                page,
                name=name,
                fixture=camera_fixture,
                reject_wrong=index == 0,
                run_discovery=index == 0,
                diagnostic=readiness_diagnostic,
            )
            camera_ids.append(enrolled["device_id"])
            discovery_completed = discovery_completed or discovery
        readiness_diagnostic["browser_enrollment_step"] = "onvif_enrollment_complete"
        _require(len(set(camera_ids)) == 4, "Installed NVR merged distinct camera sources")
        camera_id = camera_ids[0]
        camera_directories = [
            profile / "recordings" / MediaMTXBinding(identity.site_id, UUID(device)).path_name
            for device in camera_ids
        ]
        receipt["onvif_credential_profile_enrollment"] = True
        receipt["onvif_wrong_password_rejected"] = True
        receipt["no_manual_stream_uri"] = True
        receipt["onvif_time_health_displayed"] = True
        receipt["bounded_discovery_executed"] = discovery_completed
        receipt["generated_camera_discovered"] = False
        receipt.update(browser_helpers._onvif_clock_proof(onvif_camera))
        _enter_stage("actual_direct_rtsp_enrollment")
        direct_stream = browser_helpers._rtsp_enroll(
            page,
            name=_DESKTOP_CAMERAS[0][0],
            source_uri=source_uri,
            username=onvif_camera.username,
            password=onvif_camera.password,
            device_id=camera_id,
            reject_wrong=True,
        )
        _require(
            direct_stream.get("device_id") == camera_id
            and direct_stream.get("connection_type") == "rtsp",
            "Installed direct RTSP setup changed the camera identity or connection type",
        )
        receipt["direct_rtsp_enrollment"] = True
        receipt["direct_rtsp_wrong_password_rejected"] = True
        direct_rtsp_started_utc = int(datetime.now(UTC).timestamp() * 1000)
        _enter_stage("actual_fresh_rtsp_add")
        fresh_uri = urlsplit(source_uri)._replace(path="/direct-channel").geturl()
        fresh_publisher = _publish_fixture(
            parent,
            ffmpeg,
            fresh_uri,
            helpers,
            credentials=primary_publisher_credentials,
        )
        processes.append(fresh_publisher)
        fresh_stream = browser_helpers._rtsp_enroll(
            page,
            name="Generated direct RTSP channel",
            source_uri=fresh_uri,
            username=onvif_camera.username,
            password=onvif_camera.password,
        )
        fresh_id = fresh_stream.get("device_id")
        _require(
            fresh_id not in camera_ids
            and fresh_stream.get("host") == direct_stream.get("host")
            and fresh_stream.get("management_port") == direct_stream.get("management_port")
            and fresh_stream.get("connection_type") == "rtsp",
            "Fresh direct RTSP channel did not preserve independent same-endpoint identity",
        )
        five_cameras = client.json("/api/v1/operator/cameras")["cameras"]
        _require(
            len(five_cameras) == 5
            and {camera["device_id"] for camera in five_cameras} == {*camera_ids, fresh_id},
            "Adding a direct RTSP channel replaced another enrolled camera",
        )
        page.locator(".camera-item").filter(has_text="Generated direct RTSP channel").click()
        page.locator("#live-panel").wait_for(state="visible")
        receipt["direct_rtsp_fresh_live_decoded_frames"] = browser_helpers._decoded_color(
            page, "live-video", "red"
        )
        fresh_directory = (
            profile / "recordings" / MediaMTXBinding(identity.site_id, UUID(fresh_id)).path_name
        )
        fresh_before = _recording_bytes(profile, fresh_directory)
        _wait(
            lambda: _recording_bytes(profile, fresh_directory) > fresh_before,
            "Fresh installed RTSP channel records new bytes",
            process=app,
        )
        receipt["direct_rtsp_fresh_recording_bytes_before"] = fresh_before
        receipt["direct_rtsp_fresh_recording_bytes_after"] = _recording_bytes(
            profile, fresh_directory
        )
        receipt["direct_rtsp_fresh_enrollment"] = True
        receipt["direct_rtsp_multi_channel_same_endpoint"] = True
        page.locator("#remove-camera-button").click()
        page.locator("#remove-camera-dialog").wait_for(state="visible")
        with page.expect_response(
            lambda response: (
                response.request.method == "DELETE"
                and response.url.endswith("/api/v1/operator/cameras/" + fresh_id)
            )
        ) as fresh_removed:
            page.locator("#confirm-remove-camera").click()
        _require(fresh_removed.value.status == 204, "Fresh installed RTSP channel removal failed")
        page.locator("#remove-camera-dialog").wait_for(state="hidden")
        _require(
            {camera["device_id"] for camera in client.json("/api/v1/operator/cameras")["cameras"]}
            == set(camera_ids),
            "Fresh direct RTSP cleanup changed the four-camera inventory",
        )
        helpers._stop_owned(fresh_publisher)
        browser_helpers._assert_no_browser_storage(page)
        route = "/api/v1/operator/cameras/" + camera_id
        page.locator("#add-camera-dialog").wait_for(state="hidden")
        _enter_stage("actual_wall_initial_four")
        receipt["wall_decoded_frames"] = browser_helpers._wall_frames(
            page, camera_ids, [color for _, _, color in _DESKTOP_CAMERAS], diagnostic=nvr_diagnostic
        )
        _enter_stage("actual_wall_layout_one")
        page.locator("#wall-layout").select_option("1")
        _wait(
            lambda: page.locator("#wall-panel video").count() == 1,
            "Installed single-camera wall layout",
            process=app,
        )
        first_tile = page.locator("#wall-panel [data-camera-id]").first
        first_id = first_tile.get_attribute("data-camera-id")
        browser_helpers._decoded_video(
            page,
            f'#wall-panel [data-camera-id="{first_id}"] video',
            _DESKTOP_CAMERAS[camera_ids.index(first_id)][2],
            diagnostic=nvr_diagnostic,
        )
        _enter_stage("actual_wall_layout_nine")
        page.locator("#wall-layout").select_option("9")
        _wait(
            lambda: page.locator("#wall-panel video").count() == 4,
            "Installed nine-slot bounded wall layout",
            process=app,
        )
        for device, (_, _, color) in zip(camera_ids, _DESKTOP_CAMERAS, strict=True):
            nvr_diagnostic["wall_journey_camera_index"] = camera_ids.index(device)
            browser_helpers._decoded_video(
                page,
                f'#wall-panel [data-camera-id="{device}"] video',
                color,
                diagnostic=nvr_diagnostic,
            )
        _enter_stage("actual_wall_restore_four")
        receipt["wall_decoded_frames"] = browser_helpers._wall_frames(
            page, camera_ids, [color for _, _, color in _DESKTOP_CAMERAS], diagnostic=nvr_diagnostic
        )
        receipt["wall_layouts"] = [1, 4, 9]
        receipt["wall_viewport_fit"] = True
        receipt["wall_viewport"] = [page.viewport_size["width"], page.viewport_size["height"]]
        receipt["camera_count"] = 4
        receipt["four_camera_wall"] = True
        browser_helpers._open_camera_watch(page, camera_id)
        _enter_stage("actual_browser_live_decode")
        receipt["live_decoded_frames"] = browser_helpers._decoded_color(page, "live-video", "red")
        receipt["direct_rtsp_live_decoded_frames"] = receipt["live_decoded_frames"]
        receipt["actual_edge_login_enrollment_live"] = True
        _wait(lambda: _recording_bytes(profile) > 0, "Desktop recording bytes", process=app)
        # Sessions are intentionally single-active-token per user. Provision a
        # separate administrator through the installed UI, without revoking
        # the original administrator's session used by NVR and clock checks.
        secondary_admin_username = "concurrent-admin-" + secrets.token_hex(4)
        secondary_admin_password = secrets.token_urlsafe(24)
        admin_helpers._open_admin(page)
        page.locator("#admin-create-user").click()
        page.locator("#admin-user-dialog").wait_for(state="visible")
        page.locator("#admin-username").fill(secondary_admin_username)
        page.locator("#admin-display-name").fill("Generated concurrent administrator")
        page.locator("#admin-role").select_option("administrator")
        page.locator("#admin-enabled").set_checked(True)
        _require(
            page.locator("#admin-access-mode").input_value() == "all",
            "Concurrent administrator was not assigned all-camera authority",
        )
        with page.expect_response(
            lambda response: admin_helpers._route_response(
                response, "POST", "/api/v1/operator/admin/users"
            )
        ) as secondary_created:
            page.locator("#admin-save-user").click()
        _require(
            secondary_created.value.status == 201,
            "Installed administrator could not provision independent test administrator",
        )
        secondary_issue = secondary_created.value.json()
        secondary_account = secondary_issue.get("account", {})
        _require(
            secondary_account.get("username") == secondary_admin_username
            and secondary_account.get("role") == "administrator"
            and secondary_account.get("camera_access", {}).get("mode") == "all",
            "Independent installed administrator received incorrect durable role or scope",
        )
        secondary_admin_credential = admin_helpers._credential(page, secondary_admin_username)
        _require(
            secondary_admin_credential == secondary_issue.get("temporary_credential"),
            "Independent installed administrator received the wrong one-time credential",
        )
        admin_helpers._return_to_cameras(page)
        # Full lower-role, camera-access, revocation, and audit verification runs
        # in another real Edge instance while the original session stays live.
        admin_executor = ThreadPoolExecutor(max_workers=1)
        admin_future = admin_executor.submit(_parallel_admin_journey)
        nvr_diagnostic["admin_parallel_started"] = True
        _enter_stage("installed_continuous_person_analytics")
        analytics_evidence, analytics_event = _installed_analytics_journey(
            page=page,
            client=client,
            camera_id=camera_id,
            parent=parent,
            processes=processes,
            publishers=publishers,
            source_uri=source_uri,
            ffmpeg=ffmpeg,
            helpers=helpers,
            browser_helpers=browser_helpers,
            app=app,
            publisher_credentials=primary_publisher_credentials,
            diagnostic=nvr_diagnostic,
        )
        receipt.update(analytics_evidence)
        # The heavy installed settings/UI security journey is queued behind
        # the completed user-provisioning journey, then runs alongside NVR.
        settings_future = admin_executor.submit(_parallel_settings_journey)
        nvr_diagnostic["settings_parallel_started"] = True
        _enter_stage("installed_nvr_workspace")
        receipt.update(
            _desktop_nvr_journey(
                page=page,
                client=client,
                profile=profile,
                camera_ids=camera_ids,
                camera_directories=camera_directories,
                parent=parent,
                processes=processes,
                publishers=publishers,
                sources=sources,
                ffmpeg=ffmpeg,
                ffprobe=ffprobe,
                helpers=helpers,
                browser_helpers=browser_helpers,
                app=app,
                diagnostic=nvr_diagnostic,
                history_not_before=direct_rtsp_started_utc,
                export_proof=export_proof,
            )
        )
        receipt["direct_rtsp_recording_bytes_before"] = receipt["camera_recording_bytes_before"][0]
        receipt["direct_rtsp_recording_bytes_after"] = receipt["camera_recording_bytes_after"][0]
        receipt["direct_rtsp_history_decoded_frames"] = receipt["timeline_playback_decoded_frames"]
        _enter_stage("installed_user_administration")

        _require(admin_future is not None, "Installed administrator proof was not started")
        try:
            admin_evidence, admin_private_state = admin_future.result(timeout=120)
        except BaseException as error:
            nvr_diagnostic["admin_parallel_failure_type"] = (
                type(error).__name__
                if type(error).__name__
                in {
                    "Failed",
                    "TimeoutError",
                    "Error",
                    "RuntimeError",
                    "AssertionError",
                    "ValueError",
                }
                else "other"
            )
            # Only fixed assertion names can enter the public diagnostic.
            # Never emit an exception body, credentials, media, or source URI.
            nvr_diagnostic["admin_parallel_assertion"] = {
                "User could not replace the one-time credential": "activation",
                "Generated user could not sign in through the UI": "sign_in",
                "User sign-in did not issue a bounded opaque session": "session",
                "Login retained a password field": "password_clearance",
                "Viewer received administration UI": "viewer_admin_ui",
                "Viewer received camera setup": "viewer_camera_setup",
                "Viewer inventory exposed a camera outside its assigned set": "viewer_scope",
                "Viewer could not read assigned-camera recording history": "viewer_history",
                "Viewer capabilities did not match its permitted role": "viewer_capabilities",
                "Viewer received recording administration controls": "viewer_recording_controls",
                "A limited account received an unauthorized operation": "authorization_denial",
                "Viewer could not obtain assigned-camera media": "viewer_media",
                "Camera media left the authorized same-origin gateway": "viewer_gateway",
                "Account activation retained a credential field": "credential_clearance",
                "Operator received administration UI": "operator_admin_ui",
                "Decoded video did not match the selected generated camera": "live_color",
            }.get(str(error), "other")
            _require(False, "Installed parallel administrator proof did not qualify")

        nvr_diagnostic["admin_parallel_qualified"] = True
        for field in (
            "admin_user_management",
            "role_permissions",
            "camera_access_controls",
            "admin_user_creation",
            "admin_bootstrap_password_change",
            "admin_password_reset",
            "admin_disable_reenable",
            "admin_live_media_revocation",
            "admin_audit_visible",
            "admin_viewer_live_decoded_frames",
            "admin_operator_live_decoded_frames",
            "admin_user_count_created",
            "admin_audit_event_count",
            "admin_denied_http_statuses",
        ):
            _require(field in admin_evidence, "Installed user administration proof is incomplete")
            receipt[field] = admin_evidence[field]
        # Security and MFA proofs must finish before the actual host clock
        # shifts. Work overlaps independent live/NVR browser checks, never
        # the native UTC-correction phase.
        _enter_stage("installed_admin_settings_security_integrations")
        _require(settings_future is not None, "Installed settings proof was not started")
        try:
            settings_evidence, settings_diagnostic = settings_future.result(timeout=120)
        except BaseException as error:
            # The installed settings worker keeps finite, sanitized phase
            # evidence even when it raises rather than returning a receipt.
            nvr_diagnostic["settings_parallel_worker_done"] = settings_future.done()
            nvr_diagnostic.update(settings_diagnostic)
            nvr_diagnostic["settings_parallel_failure_type"] = (
                type(error).__name__
                if type(error).__name__
                in {
                    "Failed",
                    "TimeoutError",
                    "Error",
                    "RuntimeError",
                    "AssertionError",
                    "ValueError",
                }
                else "other"
            )
            _require(False, "Installed parallel settings proof did not qualify")
        nvr_diagnostic.update(settings_diagnostic)
        receipt.update(settings_evidence)
        nvr_diagnostic["settings_parallel_qualified"] = True
        admin_executor.shutdown(wait=True)
        admin_executor = None
        if vm_clock_enabled:
            _enter_stage("actual_vm_clock_changes")
            try:
                receipt["vm_clock"] = _vm_clock_phase(
                    page=page,
                    context=context,
                    client=client,
                    profile=profile,
                    camera_id=camera_id,
                    parent=parent,
                    processes=processes,
                    source_uri=source_uri,
                    ffmpeg=ffmpeg,
                    ffprobe=ffprobe,
                    helpers=helpers,
                    browser_helpers=browser_helpers,
                    app=app,
                    setup_sha256=setup_sha256,
                    diagnostic=vm_clock_diagnostic,
                    publisher=publishers[0],
                    camera_directory=camera_directories[0],
                    publisher_credentials=primary_publisher_credentials,
                    analytics_event=analytics_event,
                )
            except RuntimeError:
                # The local transport records only bounded type/timing classes.
                # Never retain camera routes, credential tokens or source addresses.
                if client.transport_failure is not None:
                    vm_clock_diagnostic["vm_clock_transport_failure"] = dict(
                        client.transport_failure
                    )
                raise
            receipt["host_clock_step_acceptance"] = True
        _enter_stage("installed_analytics_permissions")
        permissions_executor = ThreadPoolExecutor(max_workers=1)
        permissions_future = permissions_executor.submit(_parallel_analytics_permissions_journey)
        nvr_diagnostic["analytics_permissions_parallel_started"] = True
        _enter_stage("wall_logout_resource_cleanup")
        browser_helpers._wall_frames(page, camera_ids, [color for _, _, color in _DESKTOP_CAMERAS])
        wall_videos = page.locator("#wall-panel video").element_handles()
        _require(len(wall_videos) == 4, "Installed logout proof did not own four live players")
        page.locator("#sign-out-button").click()
        page.locator("#sign-in").wait_for(state="visible")
        _require(
            page.locator("#wall-panel video").count() == 0
            and all(
                video.evaluate(
                    "v => !v.isConnected && v.paused && !v.getAttribute('src') && !v.srcObject"
                )
                for video in wall_videos
            ),
            "Installed logout left wall players or media resources attached",
        )
        receipt["logout_destroys_wall"] = True
        # The browser has signed out. A fresh observer login now authorizes
        # subsequent tray checks without replacing an active browser session.
        runtime._login(client, username, password)
        _enter_stage("browser_closing")
        context.close()
        context = None
        browser.close()
        browser = None
        _require(
            export_proof["path"].is_file()
            and hashlib.sha256(export_proof["path"].read_bytes()).hexdigest()
            == export_proof["sha256"],
            "Closing the browser removed or changed its normal Windows download",
        )
        receipt["export_download_survives_browser_close"] = True
        _enter_stage("window_closing_to_tray")
        api.close(form)
        _wait(
            lambda: api.api.IsWindow(form) and not api.api.IsWindowVisible(form),
            "Title-bar close preserves the desktop window hidden in the tray",
            process=app,
        )
        receipt["close_hides_to_tray"] = True
        _enter_stage("tray_recording_growth")
        tray_before = [_recording_bytes(profile, root) for root in camera_directories[:3]]
        before = tray_before[0]
        _wait(
            lambda: (
                _tray_recording(
                    client,
                    profile,
                    camera_id,
                    before,
                    recording_diagnostic,
                    camera_directory=camera_directories[0],
                )
                and all(
                    _recording_bytes(profile, root) > previous
                    for root, previous in zip(camera_directories[1:3], tray_before[1:], strict=True)
                )
            ),
            "Recording continues with browser and desktop window closed",
            process=app,
            timeout=15,
        )
        receipt["recording_bytes_before_tray"] = before
        receipt["recording_bytes_after_tray"] = recording_diagnostic["recording_bytes_after"]
        receipt["tray_camera_recording_bytes_before"] = tray_before
        receipt["tray_camera_recording_bytes_after"] = [
            _recording_bytes(profile, root) for root in camera_directories[:3]
        ]
        receipt["tray_camera_status"] = recording_diagnostic["camera_status"]
        receipt["tray_camera_recording"] = recording_diagnostic["camera_recording"]
        receipt["recording_continues_after_browser_and_window_close"] = True
        _enter_stage("analytics_with_browser_closed")
        analytics_before = client.json(route + "/analytics")["event_count"]
        browser_closed_at = datetime.now(UTC)
        helpers._stop_owned(publishers[0])
        publishers[0] = _publish_fixture(
            parent,
            ffmpeg,
            source_uri,
            helpers,
            color="licensed person",
            credentials=primary_publisher_credentials,
        )
        processes.append(publishers[0])

        def background_person_event():
            status = client.json(route + "/analytics")
            events = client.json(route + "/events")["events"]
            return status["event_count"] > analytics_before and any(
                datetime.fromisoformat(event["occurred_at"].replace("Z", "+00:00"))
                >= browser_closed_at
                for event in events
            )

        _wait(
            background_person_event,
            "Person detection continues after the browser closes",
            process=app,
            timeout=25,
        )
        receipt["analytics_browser_closed_detection"] = True
        receipt["analytics_background_event_count_before"] = analytics_before
        receipt["analytics_background_event_count_after"] = client.json(route + "/analytics")[
            "event_count"
        ]
        helpers._stop_owned(publishers[0])
        publishers[0] = _publish_fixture(
            parent, ffmpeg, source_uri, helpers, credentials=primary_publisher_credentials
        )
        processes.append(publishers[0])
        _enter_stage("double_click_reopens_existing_instance")
        previous_python_pids = {
            process.pid
            for process in cohort.snapshot()
            if process.is_running() and process.name().casefold() in {"python.exe", "pythonw.exe"}
        }
        duplicate = _start_app(installed, profile, port, cohort)
        _require(duplicate.wait(timeout=10) == 0, "Second click did not activate the existing app")
        reopened = _wait(lambda: api.form(app.pid), "Second click restores desktop", process=app)
        _require(reopened == form, "Second click opened a second desktop instance")
        current_python_pids = {
            process.pid
            for process in cohort.snapshot()
            if process.is_running() and process.name().casefold() in {"python.exe", "pythonw.exe"}
        }
        _require(current_python_pids == previous_python_pids, "Second click duplicated backend")
        receipt["second_click_reopens_single_instance"] = True
        _enter_stage("installed_analytics_permissions_completion")
        _require(permissions_future is not None, "Analytics permissions proof was not started")
        try:
            permission_evidence, permission_diagnostic = permissions_future.result(timeout=30)
        except BaseException as error:
            nvr_diagnostic["analytics_permissions_parallel_failure_type"] = (
                type(error).__name__
                if type(error).__name__
                in {"TimeoutError", "Error", "Failed", "RuntimeError", "AssertionError"}
                else "other"
            )
            _require(False, "Installed parallel analytics permissions proof did not qualify")
        nvr_diagnostic.update(permission_diagnostic)
        receipt.update(permission_evidence)
        nvr_diagnostic["analytics_permissions_parallel_qualified"] = True
        permissions_executor.shutdown(wait=True)
        permissions_executor = None
        _enter_stage("graceful_stop_and_quit")
        _stop_app(app, reopened, api, cohort, diagnostic=nvr_diagnostic)
        receipt["graceful_owned_process_cleanup"] = True
        saved_files = sorted((profile / "recordings").rglob("*.mp4"))
        _require(bool(saved_files), "Desktop retained no recorded media")
        decoded = sum(_recorded_segment_frames(ffprobe, path) for path in saved_files)
        _require(decoded > 0, "Desktop recordings did not contain decodable media")
        receipt["recorded_decoded_frames"] = decoded
        _require(not cohort.console_seen and not cohort.failed, "Desktop opened a console window")
        _enter_stage("stopped_same_setup_reinstall")
        # Reinstall the exact already-qualified Setup while the product is fully
        # stopped, then reuse the existing full restart/account/media witness.
        # This is NOT a different-version upgrade or rollback acceptance.
        retained_samples = [
            (path, hashlib.sha256(path.read_bytes()).digest()) for path in saved_files[:3]
        ]
        # Re-admit the exact installed sealed tree after real recording and
        # shutdown. This distinguishes a runtime-created foreign file from a
        # verifier rejection that only occurs during Inno's second extraction.
        try:
            payload_check = subprocess.run(
                [str(installed / "K5Vision.exe"), "--installation-payload-check"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=90,
            ).returncode
        except subprocess.TimeoutExpired:
            payload_check = -1
        readiness_diagnostic["stopped_payload_precheck"] = (
            "admitted" if payload_check == 0 else "refused"
        )
        _require(
            payload_check == 0,
            "Stopped installed payload could not be re-admitted before reinstall",
        )
        _install_gui(setup, installed, api, diagnostic=readiness_diagnostic)
        _require(
            identity_state.load_identity_state(profile / "identity").site_id == identity.site_id,
            "Stopped reinstall replaced the existing account identity",
        )
        _require(
            all(
                path.is_file() and hashlib.sha256(path.read_bytes()).digest() == digest
                for path, digest in retained_samples
            ),
            "Stopped reinstall changed or removed retained recording bytes",
        )
        _enter_stage("durable_profile_restart")
        app = _start_app(installed, profile, port, cohort)
        form = _wait(lambda: api.form(app.pid), "Returning desktop window", process=app)
        _wait(
            lambda: _healthy(client, readiness_diagnostic),
            "Returning embedded product",
            process=app,
            timeout=60,
        )
        _require(
            not api.api.IsWindowVisible(api.control(form, _CREATE)),
            "Returning user was asked to create another account",
        )
        client = runtime._Client(port)
        runtime._login(client, username, password)
        cameras = client.json("/api/v1/operator/cameras")["cameras"]
        _require(
            len(cameras) == 4 and {camera["device_id"] for camera in cameras} == set(camera_ids),
            "Desktop restart did not preserve camera enrollment",
        )
        _require(bool(client.json(route + "/history")["spans"]), "Restart lost recording history")
        _require(
            next(camera for camera in cameras if camera["device_id"] == camera_id).get(
                "connection_type"
            )
            == "rtsp",
            "Native restart lost the direct RTSP source selection",
        )
        receipt["direct_rtsp_persisted"] = True
        _require(
            next(camera for camera in cameras if camera["device_id"] == camera_ids[3]).get(
                "recording_enabled"
            )
            is False,
            "Native restart lost the operator's paused recording choice",
        )
        _require(
            identity_state.load_identity_state(profile / "identity").site_id == identity.site_id,
            "Desktop restart changed the durable account site",
        )
        _require(all(path.is_file() for path in saved_files), "Restart removed recorded files")
        receipt["restart_preserves_account_camera_history"] = True
        restored_analytics = client.json(route + "/analytics")
        restored_events = client.json(route + "/events")["events"]
        _require(
            restored_analytics["enabled"] is True
            and restored_analytics["confidence_threshold"] == 0.5
            and any(
                event["id"] == analytics_event["id"]
                and event["occurred_at"] == analytics_event["occurred_at"]
                for event in restored_events
            ),
            "Native restart lost saved analytics settings or person events",
        )
        receipt["analytics_settings_events_persisted"] = True
        _require(
            export_proof["path"].is_file()
            and hashlib.sha256(export_proof["path"].read_bytes()).hexdigest()
            == export_proof["sha256"],
            "Restarting K5 removed or changed the user's downloaded MP4",
        )
        receipt["export_download_restart_decoded_frames"] = helpers._decoded_frames(
            ffprobe, export_proof["path"]
        )
        receipt["export_download_survives_product_restart"] = True
        # The existing restart journey above has now proved the reinstalled
        # account, camera IDs, paused preference, history and exported bytes.
        receipt["stopped_same_setup_reinstall_preserves_profile"] = True
        _enter_stage("relaunch_recording_preference_ui")
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        _enter_stage("open_saved_windows_download_after_restart")
        downloaded_page = context.new_page()
        downloaded_page.goto(export_proof["path"].as_uri(), wait_until="domcontentloaded")
        downloaded_video = downloaded_page.locator("video")
        downloaded_video.wait_for(state="visible", timeout=10_000)
        downloaded_video.evaluate("video => { video.muted=true; return video.play(); }")
        downloaded_page.wait_for_function(
            """() => {
              const video=document.querySelector('video');
              return video?.readyState>=2 && video.videoWidth===320 && video.videoHeight===240 &&
                video.getVideoPlaybackQuality().totalVideoFrames>=4;
            }""",
            timeout=10_000,
        )
        downloaded_page.close()
        receipt["export_download_opened_in_edge"] = True
        _enter_stage("relaunch_recording_preference_ui")
        page = context.new_page()
        page.set_default_timeout(15_000)
        page.goto(client.origin + "/operator/")
        page.locator("#username").fill(username)
        page.locator("#password").fill(password)
        with page.expect_response(
            lambda response: (
                response.request.method == "POST" and response.url.endswith("/api/v1/auth/login")
            )
        ) as returned_login:
            page.locator("#sign-in-button").click()
        nvr_diagnostic["restart_login_http_status"] = returned_login.value.status
        _require(returned_login.value.status == 200, "Relaunched installed workspace login failed")
        client.token = returned_login.value.json()["session_token"]
        page.locator("#wall-panel").wait_for(state="visible")

        def restarted_wall_ready():
            identifiers = page.locator("#wall-panel [data-camera-id]").evaluate_all(
                "tiles => tiles.map(tile => tile.getAttribute('data-camera-id'))"
            )
            nvr_diagnostic["restart_wall_observed_tile_count"] = len(identifiers)
            return len(identifiers) == 4 and set(identifiers) == set(camera_ids)

        _wait(
            restarted_wall_ready,
            "Relaunched installed camera inventory",
            process=app,
            timeout=10,
        )
        _require(
            page.locator("#wall-panel [data-camera-id]").count() == 4,
            "Relaunched installed workspace lost its camera wall",
        )
        _enter_stage("durable_user_administration_restart")
        admin_restarted = admin_helpers.assert_admin_restart(
            page,
            private_state=admin_private_state,
            camera_ids=[camera_ids[2], camera_ids[0]],
            client=client,
            viewer_color="lime",
        )
        for field in ("admin_state_persisted", "admin_restart_viewer_live_decoded_frames"):
            _require(field in admin_restarted, "Restarted user administration proof is incomplete")
            receipt[field] = admin_restarted[field]
        _enter_stage("relaunch_recording_preference_ui")
        browser_helpers._open_camera_watch(page, camera_ids[3])
        _wait(
            lambda: page.locator("#toggle-recording").get_attribute("data-enabled") == "false",
            "Relaunched installed recording preference",
            process=app,
        )
        receipt["recording_choice_persisted"] = True
        browser_helpers._open_camera_watch(page, camera_ids[0])
        receipt["authenticated_camera_restart_frames"] = browser_helpers._decoded_color(
            page, "live-video", "red"
        )
        receipt["authenticated_camera_restart_live"] = True
        context.close()
        context = None
        browser.close()
        browser = None
        _stop_app(app, form, api, cohort, diagnostic=nvr_diagnostic)
        _enter_stage("uninstall_preserves_profile")
        uninstaller = installed / "unins000.exe"
        _require(uninstaller.is_file(), "Native GUI uninstaller was not installed")
        result = subprocess.run(
            [str(uninstaller), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=45,
            check=False,
        )
        _require(result.returncode == 0, "Native desktop uninstall failed")
        _require(not (installed / "K5Vision.exe").exists(), "Uninstall left the installed app")
        _require(
            identity_state.load_identity_state(profile / "identity").site_id == identity.site_id,
            "Uninstall removed or changed durable account state",
        )
        _require(all(path.is_file() for path in saved_files), "Uninstall removed recording files")
        receipt["uninstall_preserves_profile_and_media"] = True
        receipt["no_console_windows"] = not cohort.console_seen and not cohort.failed
    except BaseException as error:
        nvr_diagnostic["failure_type"] = (
            type(error).__name__
            if type(error).__name__
            in {
                "TimeoutExpired",
                "TimeoutError",
                "Failed",
                "AssertionError",
                "Error",
                "RuntimeError",
                "ValueError",
                "OSError",
                "HTTPError",
            }
            else "other"
        )
        failure_detail = _failure_detail(error)
        failure = {
            "TimeoutExpired": "process_timeout",
            "TimeoutError": "browser_timeout",
            "Failed": "acceptance_assertion",
            "AssertionError": "fixture_assertion",
            "Error": "browser_error",
        }.get(type(error).__name__, "qualification_error")
    finally:
        stage_durations.append((stage, round(time.monotonic() - stage_started, 2)))
        cleanup_started = time.monotonic()
        if failure is not None and page is not None:
            try:
                nvr_diagnostic["wall_state_at_failure"] = browser_helpers._wall_state(page)
            except BaseException:
                nvr_diagnostic["wall_state_probe_failed"] = True
        if failure is not None and stage.startswith("actual_wall"):
            try:
                status, body, _ = client.get("/api/v1/operator/cameras")
                nvr_diagnostic["wall_camera_api_http_status"] = status
                if status == 200 and len(body) <= 32_768:
                    views = json.loads(body).get("cameras")
                    if type(views) is list and len(views) <= 9:
                        for field, allowed in (
                            ("status", {"live", "connecting", "disconnected", "unavailable"}),
                            (
                                "recording_state",
                                {"waiting", "recording", "paused", "stalled", "error"},
                            ),
                        ):
                            counts = {}
                            for camera in views:
                                value = camera.get(field)
                                key = value if value in allowed else "unknown"
                                counts[key] = counts.get(key, 0) + 1
                            nvr_diagnostic["wall_camera_" + field + "_counts"] = counts
            except BaseException:
                nvr_diagnostic["wall_camera_api_probe_failed"] = True
        try:
            desktop_status = _desktop_status(api, form) if form else "unavailable"
            desktop_probe = _desktop_probe(api, form) if form else {"available": False}
        except BaseException:
            desktop_status = "unresponsive"
            desktop_probe = {"available": False}
        if admin_executor is not None:
            try:
                admin_executor.shutdown(wait=True, cancel_futures=True)
            except Exception:
                cleanup_failed = True
        if permissions_executor is not None:
            try:
                permissions_executor.shutdown(wait=True, cancel_futures=True)
            except Exception:
                cleanup_failed = True
        process_counts = _cohort_diagnostic(cohort)
        app_exit_code = app.poll() if app else None
        form_alive = bool(form and api.api.IsWindow(form))
        form_visible = bool(form and api.api.IsWindowVisible(form))
        cohort.finish()
        for resource in (context, browser, playwright):
            if resource is not None:
                try:
                    resource.stop() if resource is playwright else resource.close()
                except Exception:
                    cleanup_failed = True
        for process in reversed(cohort.snapshot()):
            try:
                if process.is_running():
                    process.kill()
                    process.wait(timeout=10)
            except psutil.NoSuchProcess:
                pass
            except Exception:
                cleanup_failed = True
        for process in reversed(processes):
            try:
                helpers._stop_owned(process)
            except Exception:
                cleanup_failed = True
        for camera_fixture in onvif_cameras:
            try:
                camera_fixture.close()
            except Exception:
                cleanup_failed = True
        for listener in reservations:
            listener.close()
        if parent.exists():
            try:
                shutil.rmtree(parent)
            except Exception:
                cleanup_failed = True
        stage_durations.append(("cleanup", round(time.monotonic() - cleanup_started, 2)))
        diagnostic = {
            "stage": stage,
            "stage_durations": stage_durations,
            "failure_code": failure,
            "failure_detail": failure_detail,
            "cleanup_failed": cleanup_failed,
            "desktop_status": desktop_status,
            "desktop_probe": desktop_probe,
            "owned_process_counts": process_counts,
            "app_exit_code": app_exit_code,
            "form_alive": form_alive,
            "form_visible": form_visible,
            **readiness_diagnostic,
            **recording_diagnostic,
            **vm_clock_diagnostic,
            **nvr_diagnostic,
        }
        record_testsuite_property(
            "desktop_windows_diagnostic", json.dumps(diagnostic, sort_keys=True)
        )
        if failure is not None or cleanup_failed:
            print("Desktop qualification diagnostic: " + json.dumps(diagnostic, sort_keys=True))
    _require(failure is None, "Desktop qualification failed at " + stage + ": " + str(failure))
    _require(not cleanup_failed, "Owned desktop qualification cleanup failed")
    _require(not cohort.console_seen and not cohort.failed, "Desktop opened a console window")
    elapsed = time.monotonic() - started
    print("Desktop qualification stage seconds: " + json.dumps(stage_durations))
    if vm_clock_enabled:
        print(
            "Desktop VM clock phase seconds: "
            + json.dumps(vm_clock_diagnostic.get("vm_clock_phase_seconds", []))
        )
        print(
            "Desktop NVR phase seconds: " + json.dumps(nvr_diagnostic.get("nvr_phase_seconds", []))
        )
    _require(
        elapsed < 360,
        "Desktop qualification exceeded its bounded VM duration"
        if vm_clock_enabled
        else "Desktop qualification exceeded its bounded six-minute duration",
    )
    receipt["duration_seconds"] = round(elapsed, 2)
    receipt["owned_cleanup"] = True
    record_testsuite_property("desktop_windows_receipt", json.dumps(receipt, sort_keys=True))
