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
_PASSWORD, _CONFIRM, _CREATE, _STOP, _USERNAME = 1001, 1002, 1003, 1005, 1008


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
    stage, failure, cleanup_failed = "installer", None, False
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
            lambda: client.json("/api/v1/health").get("status") == "ok",
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
            api.text(api.control(form, _PASSWORD)) == ""
            and api.text(api.control(form, _CONFIRM)) == "",
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
        page.locator("#sign-in-button").click()
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
        stage = "browser_close_and_tray_recording"
        context.close()
        context = None
        browser.close()
        browser = None
        api.close(form)
        _wait(lambda: not api.api.IsWindowVisible(form), "Close hides desktop to tray", process=app)
        before = _recording_bytes(profile)
        _wait(
            lambda: _recording_bytes(profile) > before,
            "Recording continues with browser and desktop window closed",
            process=app,
            timeout=15,
        )
        receipt["close_hides_to_tray"] = True
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
            lambda: client.json("/api/v1/health").get("status") == "ok",
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
        failure = {
            "TimeoutExpired": "process_timeout",
            "TimeoutError": "browser_timeout",
            "Failed": "acceptance_assertion",
            "AssertionError": "fixture_assertion",
            "Error": "browser_error",
        }.get(type(error).__name__, "qualification_error")
    finally:
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
        diagnostic = {"stage": stage, "failure_code": failure, "cleanup_failed": cleanup_failed}
        record_testsuite_property(
            "desktop_windows_diagnostic", json.dumps(diagnostic, sort_keys=True)
        )
    _require(failure is None, "Desktop qualification failed at " + stage + ": " + str(failure))
    _require(not cleanup_failed, "Owned desktop qualification cleanup failed")
    _require(not cohort.console_seen and not cohort.failed, "Desktop opened a console window")
    elapsed = time.monotonic() - started
    _require(elapsed < 240, "Desktop qualification exceeded its bounded four-minute duration")
    receipt["duration_seconds"] = round(elapsed, 2)
    receipt["owned_cleanup"] = True
    record_testsuite_property("desktop_windows_receipt", json.dumps(receipt, sort_keys=True))
