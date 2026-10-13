"""Actual Windows installer, account screen, browser and tray qualification.

All input is generated and temporary. Evidence contains finite outcomes, never
passwords, private paths, camera URLs, screenshots or media. Real Win32 windows
are exercised; no WinForms or process behavior is mocked.
"""

from __future__ import annotations

import ctypes
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
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

import psutil
import pytest

from k5vision import identity_state

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
_PASSWORD, _CONFIRM, _CREATE, _STOP, _STATUS, _USERNAME = 1001, 1002, 1003, 1005, 1006, 1008


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
        _require(bool(self.api.PostMessageW(form, 0x0010, 0, 0)), "Desktop close command failed")


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


def _stop_app(process, form, api, cohort):
    __tracebackhide__ = True
    api.click(api.control(form, _STOP))
    _require(process.wait(timeout=40) == 0, "Desktop stop and quit did not finish successfully")
    _, alive = psutil.wait_procs(cohort.snapshot(), timeout=10)
    _require(not alive, "Owned desktop/backend/native processes survived stop and quit")


def _recording_bytes(profile):
    return sum(path.stat().st_size for path in (profile / "recordings").rglob("*.mp4"))


def _tray_recording(client, profile, camera_id, before, diagnostic):
    """Require a connected recording source and new bytes while both UIs are closed."""
    files = list((profile / "recordings").rglob("*.mp4"))
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


def _generate_fixture(parent, media, ffmpeg, upstream_port, helpers):
    __tracebackhide__ = True
    video = parent / "generated red camera.mp4"
    generated = subprocess.run(
        [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:size=320x240:rate=15",
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
    upstream_config = parent / "generated upstream.json"
    helpers._private_write(
        upstream_config,
        json.dumps(
            {
                "logLevel": "warn",
                "rtspAddress": f"127.0.0.1:{upstream_port}",
                "rtspTransports": ["tcp"],
                "authInternalUsers": [
                    {
                        "user": "any",
                        "ips": ["127.0.0.1", "::1"],
                        "permissions": [
                            {"action": "publish", "path": "generated"},
                            {"action": "read", "path": "generated"},
                        ],
                    }
                ],
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
            with socket.create_connection(("127.0.0.1", upstream_port), timeout=0.5):
                return True

        _wait(ready, "Generated upstream listener", process=upstream, timeout=10)
    except BaseException:
        helpers._stop_owned(upstream)
        raise
    source_uri = f"rtsp://127.0.0.1:{upstream_port}/generated"
    publisher = helpers._start(
        [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-re",
            "-stream_loop",
            "-1",
            "-i",
            str(video),
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
    return [upstream, publisher], source_uri


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
    playwright = browser = context = None
    form = None
    app = None
    stage, failure, cleanup_failed = "installer", None, False
    failure_detail = None
    readiness_diagnostic = {"health_http_status": 0}
    recording_diagnostic = {}
    receipt = {
        "schema_version": "1",
        "platform": platform.system(),
        "native_windows_execution": True,
        "generated_media_only": True,
        "physical_camera_acceptance": False,
        "capacity_acceptance": False,
        "endurance_acceptance": False,
        "windows_service": False,
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
        reservations, ports = helpers._reserve_ports(2)
        port, upstream_port = ports
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
        processes, source_uri = _generate_fixture(parent, media, ffmpeg, upstream_port, helpers)
        stage = "actual_browser_enrollment"
        playwright = browser_api.sync_playwright().start()
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        receipt["browser_version"] = browser.version
        context = browser.new_context()
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
        page.locator("#add-camera-button").click()
        page.locator("#new-camera-name").fill("Generated desktop camera")
        page.locator("#new-camera-host").fill("127.0.0.1")
        page.locator("#new-camera-source").fill(source_uri)
        with page.expect_response(
            lambda response: (
                response.request.method == "POST"
                and response.url.endswith("/api/v1/operator/cameras")
            )
        ) as enrolled:
            page.locator("#save-camera").click()
        _require(enrolled.value.status == 201, "Desktop browser enrollment failed")
        camera_id = enrolled.value.json()["device_id"]
        route = "/api/v1/operator/cameras/" + camera_id
        page.locator("#add-camera-dialog").wait_for(state="hidden")
        stage = "actual_browser_live_decode"
        receipt["live_decoded_frames"] = browser_helpers._decoded_color(page, "live-video", "red")
        receipt["actual_edge_login_enrollment_live"] = True
        _wait(lambda: _recording_bytes(profile) > 0, "Desktop recording bytes", process=app)
        stage = "browser_closing"
        context.close()
        context = None
        browser.close()
        browser = None
        stage = "window_closing_to_tray"
        api.close(form)
        _wait(lambda: not api.api.IsWindowVisible(form), "Close hides desktop to tray", process=app)
        receipt["close_hides_to_tray"] = True
        stage = "tray_recording_growth"
        before = _recording_bytes(profile)
        _wait(
            lambda: _tray_recording(client, profile, camera_id, before, recording_diagnostic),
            "Recording continues with browser and desktop window closed",
            process=app,
            timeout=15,
        )
        receipt["recording_bytes_before_tray"] = before
        receipt["recording_bytes_after_tray"] = recording_diagnostic["recording_bytes_after"]
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
        decoded = sum(helpers._decoded_frames(ffprobe, path) for path in saved_files)
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
            len(cameras) == 1 and cameras[0]["device_id"] == camera_id,
            "Desktop restart did not preserve camera enrollment",
        )
        _require(bool(client.json(route + "/history")["spans"]), "Restart lost recording history")
        _require(
            identity_state.load_identity_state(profile / "identity").site_id == identity.site_id,
            "Desktop restart changed the durable account site",
        )
        _require(all(path.is_file() for path in saved_files), "Restart removed recorded files")
        receipt["restart_preserves_account_camera_history"] = True
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
    _require(elapsed < 240, "Desktop qualification exceeded its bounded four-minute duration")
    receipt["duration_seconds"] = round(elapsed, 2)
    receipt["owned_cleanup"] = True
    record_testsuite_property("desktop_windows_receipt", json.dumps(receipt, sort_keys=True))
