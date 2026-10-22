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
import secrets
import shutil
import socket
import subprocess
import threading
import time
from ctypes import wintypes
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit
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


def _install_gui(setup, installed, api):
    """Drive the actual Inno wizard without suppressing its user interface."""
    __tracebackhide__ = True
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
    try:
        while process.poll() is None:
            _require(time.monotonic() < deadline, "Actual installer wizard exceeded its deadline")
            root = psutil.Process(process.pid)
            pids = {root.pid, *(child.pid for child in root.children(recursive=True))}
            forms = [window for window in api.windows() if api.pid(window) in pids]
            for form in forms:
                controls = [
                    window
                    for window in api.windows(form)
                    if api.api.IsWindowVisible(window) and api.api.IsWindowEnabled(window)
                ]
                captions = {_normalized_caption(api.text(window)): window for window in controls}
                if "i accept the agreement" in captions:
                    api.click(captions["i accept the agreement"])
                if "launch k5 vision" in captions and not launch_unchecked:
                    checkbox = captions["launch k5 vision"]
                    if api.message(checkbox, 0x00F0) == 1:  # BM_GETCHECK / BST_CHECKED
                        api.click(checkbox)
                    launch_unchecked = True
                if "finish" in captions and not launch_unchecked:
                    launch_unchecked = _uncheck_launch(api, controls)
                    _require(launch_unchecked, "Installer launch checklist was unavailable")
                for caption in ("finish", "install", "next >", "next"):
                    if caption in captions:
                        api.click(captions[caption])
                        clicked.add(caption)
                        break
            time.sleep(0.2)
        _require(process.returncode == 0, "Actual GUI installer did not complete successfully")
        _require("install" in clicked and "finish" in clicked, "Installer wizard was not exercised")
        _require(launch_unchecked, "Installer launch option was not available to the user")
    finally:
        if process.poll() is None:
            children = psutil.Process(process.pid).children(recursive=True)
            process.kill()
            process.wait(timeout=10)
            for child in children:
                if child.is_running():
                    child.kill()


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


def _default_browser_handoff(api, form, port):
    """Click the real native action and inspect the browser's actual address bar."""
    previous_windows = set(api.windows())
    previous_processes = {process.pid for process in psutil.process_iter()}
    opened = None
    owned = []
    try:
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
        owned = [
            item
            for item in (process, *process.children(recursive=True))
            if item.pid not in previous_processes
        ]
        environment = dict(os.environ)
        environment["K5_ACCEPTANCE_BROWSER_HWND"] = str(opened)
        environment["K5_ACCEPTANCE_BROWSER_URL"] = f"http://127.0.0.1:{port}/operator/"
        script = """
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$handle = [IntPtr]::new([long]$env:K5_ACCEPTANCE_BROWSER_HWND)
$element = [System.Windows.Automation.AutomationElement]::FromHandle($handle)
$property = [System.Windows.Automation.AutomationElement]::ControlTypeProperty
$editType = [System.Windows.Automation.ControlType]::Edit
$condition = [System.Windows.Automation.PropertyCondition]::new($property, $editType)
$edits = $element.FindAll([System.Windows.Automation.TreeScope]::Descendants, $condition)
$expected = $env:K5_ACCEPTANCE_BROWSER_URL.TrimEnd('/')
$matched = $false
foreach ($edit in $edits) {
  try {
    $pattern = $edit.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
    $value = $pattern.Current.Value.TrimEnd('/')
    if ($value -eq $expected -or $value -eq $expected.Substring(7)) { $matched = $true }
  } catch { }
}
if (-not $matched) { exit 2 }
[Console]::WriteLine('K5_BROWSER_URL_MATCHED')
"""
        powershell = Path(os.environ["SYSTEMROOT"]) / (
            "System32/WindowsPowerShell/v1.0/powershell.exe"
        )
        result = subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-Command", script],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
            timeout=15,
            check=False,
        )
        _require(
            result.returncode == 0 and result.stdout.strip() == b"K5_BROWSER_URL_MATCHED",
            "Native Open cameras did not hand off the exact operator URL to the default browser",
        )
        return True
    finally:
        if opened and api.api.IsWindow(opened):
            api.close(opened)
        _, alive = psutil.wait_procs(owned, timeout=5)
        # Only processes introduced by this action and descended from its new
        # browser window are owned. Existing browser profiles/defaults stay intact.
        for process in alive:
            try:
                process.terminate()
            except psutil.NoSuchProcess:
                pass
        psutil.wait_procs(alive, timeout=5)


def _stop_app(process, form, api, cohort):
    __tracebackhide__ = True
    api.click(api.control(form, _STOP))
    _require(process.wait(timeout=40) == 0, "Desktop stop and quit did not finish successfully")
    _, alive = psutil.wait_procs(cohort.snapshot(), timeout=10)
    _require(not alive, "Owned desktop/backend/native processes survived stop and quit")


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
    users = [
        {
            "user": "any",
            "ips": ["127.0.0.0/8", "::1"],
            "permissions": [
                {"action": action, "path": "generated"} for action in ("publish", "read")
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
                "permissions": [{"action": "read", "path": "generated"}],
            },
            {
                "user": publisher_credentials[0],
                "pass": publisher_credentials[1],
                "ips": ["127.0.0.0/8", "::1"],
                "permissions": [{"action": "publish", "path": "generated"}],
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
                "paths": {"generated": {"source": "publisher"}},
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
    publisher = _publish_fixture(
        parent, ffmpeg, source_uri, helpers, color=color, credentials=publisher_credentials
    )
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
):
    """Exercise operator actions against four real installed recording paths."""
    __tracebackhide__ = True

    def sizes(indices=range(4)):
        return [_recording_bytes(profile, camera_directories[index]) for index in indices]

    def all_progress(before, indices=range(4)):
        return all(after > prior for after, prior in zip(sizes(indices), before, strict=True))

    diagnostic["nvr_stage"] = "four_recorders"
    before = sizes()
    _wait(lambda: all_progress(before), "Installed four-camera recording progress", process=app)
    evidence = {
        "four_camera_recording_progress": True,
        "camera_recording_bytes_before": before,
        "camera_recording_bytes_after": sizes(),
    }
    diagnostic["nvr_stage"] = "isolated_outage"
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
    helpers._stop_owned(publishers[1])

    def isolated_offline():
        views = {
            camera["device_id"]: camera
            for camera in client.json("/api/v1/operator/cameras")["cameras"]
        }
        _require(
            all(handle.evaluate("v => v.isConnected") for handle in handles),
            "One camera outage destroyed an unrelated installed wall player",
        )
        return views[camera_ids[1]]["status"] == "disconnected" and all(
            views[camera_ids[index]]["status"] == "live" for index in unaffected
        )

    _wait(isolated_offline, "Installed isolated camera outage", process=app, timeout=20)
    page.wait_for_function(
        """({videos,before}) => videos.every((video,index) => video.isConnected &&
          !video.paused && video.getVideoPlaybackQuality().totalVideoFrames >= before[index]+4)""",
        arg={"videos": handles, "before": frames},
        timeout=15_000,
    )
    _wait(
        lambda: all_progress(others_before, unaffected),
        "Other installed cameras record during outage",
        process=app,
    )
    evidence["outage_other_live_frames_before"] = frames
    evidence["outage_other_live_frames_after"] = [
        handle.evaluate("v => v.getVideoPlaybackQuality().totalVideoFrames") for handle in handles
    ]
    publishers[1] = _publish_fixture(parent, ffmpeg, sources[1], helpers, color="blue")
    processes.append(publishers[1])
    browser_helpers._decoded_video(
        page, f'#wall-panel [data-camera-id="{camera_ids[1]}"] video', "blue"
    )
    recovered = sizes()
    _wait(lambda: all_progress(recovered), "Installed camera recording recovery", process=app)
    evidence["isolated_camera_outage_reconnect"] = True

    diagnostic["nvr_stage"] = "pause_recording"
    browser_helpers._open_camera_watch(page, camera_ids[0])
    before_live = browser_helpers._decoded_color(page, "live-video", "red")
    live = page.locator("#live-video").element_handle()
    recording_route = f"/api/v1/operator/cameras/{camera_ids[0]}/recording"
    with page.expect_response(
        lambda response: response.request.method == "PUT" and response.url.endswith(recording_route)
    ) as paused:
        page.locator("#toggle-recording").click()
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
    with page.expect_response(
        lambda response: response.request.method == "PUT" and response.url.endswith(recording_route)
    ) as resumed:
        page.locator("#toggle-recording").click()
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

    diagnostic["nvr_stage"] = "timeline_and_export"
    browser_helpers._open_camera_recordings(page, camera_ids[0])
    evidence["timeline_playback_decoded_frames"] = browser_helpers._timeline_play(page, "red")
    export = parent / "saved installed red recording.mp4"
    evidence["export_decoded_frames"] = browser_helpers._download_recording(
        page, ffprobe, export, helpers, "red"
    )
    evidence["export_bytes"] = export.stat().st_size
    evidence["timeline_seek_playback"] = True
    evidence["actual_recording_export"] = True

    # Leave a real choice paused across the following native restart. The
    # primary source remains recording for tray and actual-VM clock checks.
    diagnostic["nvr_stage"] = "persist_paused_choice"
    browser_helpers._open_camera_watch(page, camera_ids[3])
    preference_route = f"/api/v1/operator/cameras/{camera_ids[3]}/recording"
    with page.expect_response(
        lambda response: (
            response.request.method == "PUT" and response.url.endswith(preference_route)
        )
    ) as preference:
        page.locator("#toggle-recording").click()
    _require(preference.value.status == 200, "Installed persisted recording choice failed")
    _wait(
        lambda: page.locator("#toggle-recording").get_attribute("data-enabled") == "false",
        "Installed recording choice is paused",
        process=app,
    )
    browser_helpers._open_camera_watch(page, camera_ids[0])
    browser_helpers._decoded_color(page, "live-video", "red")
    diagnostic["nvr_stage"] = "complete"
    return evidence


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
):
    """Actually change only an opted-in disposable Windows guest's UTC clock."""
    __tracebackhide__ = True
    route = "/api/v1/operator/cameras/" + camera_id
    original_token = client.token
    captured_live = []
    non_loopback = []

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
                page.locator("#recorder-time-warning").wait_for(state="visible", timeout=10_000)
                _wait(
                    lambda direction=direction: (
                        direction in page.locator("#recorder-time-warning").inner_text()
                    ),
                    "VM clock warning direction",
                    process=app,
                    timeout=10,
                )
                _wait(
                    displayed_recorder_utc_matches,
                    "VM clock visible recorder UTC",
                    process=app,
                    timeout=10,
                )
                # Observe at least two normal five-second storage-maintenance
                # cycles and allow pre-step live buffers to drain naturally.
                _wait(
                    lambda since=shifted_at: time.monotonic() - since >= 10,
                    "VM clock held observation interval",
                    process=app,
                    timeout=15,
                )
                diagnostic["vm_clock_stage"] = direction + "_live"
                live_status = context.request.get(live_url, timeout=5000).status
                diagnostic["vm_clock_media_http_status"] = live_status
                _require(live_status == 200, "VM clock step expired authenticated live media")
                _require(
                    page.locator("#workspace").is_visible(),
                    "VM clock step signed out the browser's existing human session",
                )
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

                _wait(
                    shifted_history,
                    "VM clock shifted recorder UTC history query",
                    process=app,
                    timeout=10,
                )
                _require(_hashes_preserved(closed), "VM clock step changed closed footage hashes")
                held = guard.snapshot()
                _require(
                    abs(held["baseline_residual_seconds"] - offset) <= 2,
                    "VM clock requested UTC step did not remain in effect",
                )
                diagnostic["vm_clock_stage"] = direction + "_restore"
                restoration = guard.restore()
                _require(
                    restoration["restored"] is True and abs(restoration["residual_seconds"]) <= 2,
                    "VM clock UTC restoration was not verified",
                )
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
    diagnostic["vm_clock_stage"] = "restored_playback"
    _require(_hashes_preserved(closed), "VM clock restoration changed closed footage hashes")
    restored_history = client.json(route + "/history" + history_query)["spans"]
    _require(
        all(span in restored_history for span in before_history),
        "VM clock restoration lost retained recording history",
    )
    page.locator("#history-tab").click()
    page.locator("#history-start").fill(historical_start.strftime("%Y-%m-%dT%H:%M:%S"))
    page.locator("#history-end").fill(historical_end.strftime("%Y-%m-%dT%H:%M:%S"))
    page.locator("#history-form").evaluate("form => form.requestSubmit()")
    page.locator(".history-item").first.wait_for(state="visible", timeout=15_000)
    page.locator(".history-item").first.click()
    page.locator("#play-recording").click()
    playback_frames = browser_helpers._decoded_color(page, "playback-video", "red")
    page.locator("#live-tab").click()
    browser_helpers._decoded_color(page, "live-video", "red")
    _require(not non_loopback, "VM clock proof attempted an external request while shifted")
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
    cohort = _Cohort(api)
    playwright = browser = context = onvif_camera = None
    onvif_cameras, sources, publishers = [], [], []
    primary_publisher_credentials = ("generated-fixture-publisher", secrets.token_urlsafe(24))
    form = None
    app = None
    stage, failure, cleanup_failed = "installer", None, False
    failure_detail = None
    readiness_diagnostic = {"health_http_status": 0}
    recording_diagnostic = {}
    vm_clock_diagnostic = {}
    nvr_diagnostic = {}
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
    try:
        _install_gui(setup, installed, api)
        _require((installed / "K5Vision.exe").is_file(), "Installed desktop executable is absent")
        _require((installed / "runtime/python.exe").is_file(), "Embedded Python was not installed")
        receipt["actual_gui_installer"] = True
        reservations, ports = helpers._reserve_ports(5)
        port, upstream_ports = ports[0], ports[1:]
        for listener in reservations:
            listener.close()
        stage = "first_account_screen"
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
        stage = "embedded_product_startup"
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
        stage = "native_default_browser_handoff"
        _wait(
            lambda: api.api.IsWindowEnabled(api.control(form, _OPEN)),
            "Native Open cameras button",
            process=app,
            timeout=10,
        )
        receipt["native_default_browser_handoff"] = _default_browser_handoff(api, form, port)
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
        stage = "generated_camera_fixture"
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
            generated, source = _generate_fixture(
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
            )
            processes.extend(generated)
            publishers.append(generated[-1])
            sources.append(source)
        source_uri, onvif_camera = sources[0], onvif_cameras[0]
        stage = "actual_camera_rtsp_authentication"
        browser_helpers._assert_rtsp_read_auth(
            ffprobe, source_uri, onvif_camera.username, onvif_camera.password
        )
        receipt["rtsp_read_auth_camera_count"] = 1
        receipt["authenticated_camera_read"] = True
        receipt["rtsp_anonymous_read_rejected"] = True
        receipt["rtsp_wrong_password_rejected"] = True
        stage = "actual_browser_enrollment"
        playwright = browser_api.sync_playwright().start()
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        receipt["browser_version"] = browser.version
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        page.set_default_timeout(25_000)
        page.goto(client.origin + "/operator/")
        page.locator("#username").fill(username)
        page.locator("#password").fill(password)
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
        page.locator("#workspace").wait_for(state="visible")
        browser_helpers._assert_recorder_time_health(page, client)
        receipt["recorder_time_health_displayed"] = True
        camera_ids = []
        discovery_completed = False
        for index, ((name, _, _), camera_fixture) in enumerate(
            zip(_DESKTOP_CAMERAS, onvif_cameras, strict=True)
        ):
            enrolled, discovery = browser_helpers._onvif_enroll(
                page,
                name=name,
                fixture=camera_fixture,
                reject_wrong=index == 0,
                run_discovery=index == 0,
            )
            camera_ids.append(enrolled["device_id"])
            discovery_completed = discovery_completed or discovery
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
        browser_helpers._assert_no_browser_storage(page)
        route = "/api/v1/operator/cameras/" + camera_id
        page.locator("#add-camera-dialog").wait_for(state="hidden")
        stage = "actual_four_camera_wall"
        receipt["wall_decoded_frames"] = browser_helpers._wall_frames(
            page, camera_ids, [color for _, _, color in _DESKTOP_CAMERAS]
        )
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
        )
        page.locator("#wall-layout").select_option("9")
        _wait(
            lambda: page.locator("#wall-panel video").count() == 4,
            "Installed nine-slot bounded wall layout",
            process=app,
        )
        for device, (_, _, color) in zip(camera_ids, _DESKTOP_CAMERAS, strict=True):
            browser_helpers._decoded_video(
                page, f'#wall-panel [data-camera-id="{device}"] video', color
            )
        receipt["wall_decoded_frames"] = browser_helpers._wall_frames(
            page, camera_ids, [color for _, _, color in _DESKTOP_CAMERAS]
        )
        receipt["wall_layouts"] = [1, 4, 9]
        receipt["wall_viewport_fit"] = True
        receipt["wall_viewport"] = [page.viewport_size["width"], page.viewport_size["height"]]
        receipt["camera_count"] = 4
        receipt["four_camera_wall"] = True
        browser_helpers._open_camera_watch(page, camera_id)
        stage = "actual_browser_live_decode"
        receipt["live_decoded_frames"] = browser_helpers._decoded_color(page, "live-video", "red")
        receipt["actual_edge_login_enrollment_live"] = True
        _wait(lambda: _recording_bytes(profile) > 0, "Desktop recording bytes", process=app)
        stage = "installed_nvr_workspace"
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
            )
        )
        if vm_clock_enabled:
            stage = "actual_vm_clock_changes"
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
            )
            receipt["host_clock_step_acceptance"] = True
        stage = "wall_logout_resource_cleanup"
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
        stage = "browser_closing"
        context.close()
        context = None
        browser.close()
        browser = None
        stage = "window_closing_to_tray"
        api.close(form)
        _wait(
            lambda: api.api.IsWindow(form) and not api.api.IsWindowVisible(form),
            "Title-bar close preserves the desktop window hidden in the tray",
            process=app,
        )
        receipt["close_hides_to_tray"] = True
        stage = "tray_recording_growth"
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
        stage = "double_click_reopens_existing_instance"
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
        stage = "graceful_stop_and_quit"
        _stop_app(app, reopened, api, cohort)
        receipt["graceful_owned_process_cleanup"] = True
        saved_files = sorted((profile / "recordings").rglob("*.mp4"))
        _require(bool(saved_files), "Desktop retained no recorded media")
        decoded = sum(_recorded_segment_frames(ffprobe, path) for path in saved_files)
        _require(decoded > 0, "Desktop recordings did not contain decodable media")
        receipt["recorded_decoded_frames"] = decoded
        _require(not cohort.console_seen and not cohort.failed, "Desktop opened a console window")
        stage = "durable_profile_restart"
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
        stage = "relaunch_recording_preference_ui"
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
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
        _require(returned_login.value.status == 200, "Relaunched installed workspace login failed")
        client.token = returned_login.value.json()["session_token"]
        page.locator("#wall-panel").wait_for(state="visible")
        _require(
            page.locator("#wall-panel [data-camera-id]").count() == 4,
            "Relaunched installed workspace lost its camera wall",
        )
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
        _stop_app(app, form, api, cohort)
        stage = "uninstall_preserves_profile"
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
        failure_detail = _failure_detail(error)
        failure = {
            "TimeoutExpired": "process_timeout",
            "TimeoutError": "browser_timeout",
            "Failed": "acceptance_assertion",
            "AssertionError": "fixture_assertion",
            "Error": "browser_error",
        }.get(type(error).__name__, "qualification_error")
    finally:
        try:
            desktop_status = _desktop_status(api, form) if form else "unavailable"
            desktop_probe = _desktop_probe(api, form) if form else {"available": False}
        except BaseException:
            desktop_status = "unresponsive"
            desktop_probe = {"available": False}
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
        diagnostic = {
            "stage": stage,
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
    _require(
        elapsed < (360 if vm_clock_enabled else 240),
        "Desktop qualification exceeded its bounded VM duration"
        if vm_clock_enabled
        else "Desktop qualification exceeded its bounded four-minute duration",
    )
    receipt["duration_seconds"] = round(elapsed, 2)
    receipt["owned_cleanup"] = True
    record_testsuite_property("desktop_windows_receipt", json.dumps(receipt, sort_keys=True))
