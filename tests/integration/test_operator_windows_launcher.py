"""Opt-in real PowerShell 5.1 launcher proof on an isolated Windows runner.

Supplied wheels/tools are test inputs. Passwords stay in memory and the local
login request body; no private output, source, path or credential is evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import uuid4

import psutil
import pytest

from k5vision import identity_state
from k5vision.operator_native_process import OwnedNativeProcess

_INPUTS = ("K5_OPERATOR_WHEEL", "K5_MEDIAMTX_DONOR_EXE", "K5_CADDY_DONOR_EXE")
pytestmark = pytest.mark.skipif(
    os.name != "nt" or not all(os.getenv(name) for name in _INPUTS),
    reason="Native launcher proof requires Windows and explicitly supplied wheel/tools",
)
_MODULES = ("cli.py", "operator_product.py", "operator_product_service.py")
_LAUNCHER_STAGES = frozenset(
    {
        "platform",
        "python",
        "private-state",
        "runtime-installation",
        "venv-admission",
        "venv-creation",
        "venv-file",
        "venv-version",
        "pip-installation",
        "native-workers",
        "worker-assembly",
        "tools-admission",
        "media-manifest",
        "gateway-manifest",
        "media-cache",
        "gateway-cache",
        "identity",
        "recordings",
        "operator",
    }
)
_LAUNCHER_REASONS = frozenset(
    {
        "unclassified",
        "python-operation",
        "manifest-admission",
        "artifact-count",
        "artifact-admission",
        "cached-identity",
        "archive-identity",
        "archive-contents",
        "extracted-identity",
        "license-identity",
    }
)


def _require(condition: bool, message: str) -> None:
    __tracebackhide__ = True
    if not condition:
        pytest.fail(message, pytrace=False)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _request(origin: str, route: str, *, data=None, token="") -> tuple[int, bytes]:
    __tracebackhide__ = True
    headers = {"Origin": origin}
    if token:
        headers["Authorization"] = "Bearer " + token
    payload = None
    if data is not None:
        headers["Content-Type"] = "application/json"
        payload = json.dumps(data).encode()
    request = Request(origin + route, data=payload, headers=headers)
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    try:
        try:
            response = opener.open(request, timeout=2)
        except HTTPError as error:
            response = error
        with response:
            body = response.read(65_537)
            _require(len(body) <= 65_536, "Launcher response exceeded its byte budget")
            return response.code, body
    except OSError:
        return 0, b""


def _json(body: bytes) -> dict:
    __tracebackhide__ = True
    failed = False
    result = None
    try:
        result = json.loads(body)
    except (ValueError, UnicodeError):
        failed = True
    _require(not failed and isinstance(result, dict), "Launcher API response was invalid")
    return result


def _private_directory(path: Path) -> None:
    __tracebackhide__ = True
    identity_state.validate_new_identity_directory(path)
    path.mkdir(mode=0o700)
    identity_state._checked_path(path)
    identity_state._windows_admission(path, private=True, directory=True)


def _copy_private(source: Path, target: Path) -> None:
    __tracebackhide__ = True
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output, source.open("rb") as incoming:
        shutil.copyfileobj(incoming, output, length=1024**2)
    identity_state._regular_file(target)


def _digest(path: Path) -> str:
    __tracebackhide__ = True
    result = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024**2), b""):
            result.update(chunk)
    return result.hexdigest()


def _launcher_failure(runtime: Path, name: str) -> str:
    __tracebackhide__ = True
    detail = "unavailable"
    reason = "unclassified"
    try:
        path = runtime / name
        identity_state._regular_file(path)
        with path.open("rb") as source:
            body = source.read(513)
        if len(body) <= 512:
            document = json.loads(body)
            if (
                isinstance(document, dict)
                and document.get("schema_version") == "1"
                and document.get("result") == "failed"
                and document.get("stage") in _LAUNCHER_STAGES
                and document.get("reason") in _LAUNCHER_REASONS
                and type(document.get("exit_code")) is int
                and -(2**31) <= document["exit_code"] < 2**31
            ):
                detail = document["stage"]
                reason = document["reason"]
    except Exception:
        pass
    return (
        "PowerShell launcher exited before readiness (fixed stage: "
        + detail
        + "; fixed reason: "
        + reason
        + ")"
    )


def _cache_worker(runtime: Path, wheel: Path, name: str, supplied: Path) -> None:
    __tracebackhide__ = True
    with zipfile.ZipFile(wheel) as package:
        manifest = json.loads(package.read(f"k5vision/data/{name}-donor-manifest.json"))
    artifacts = [item for item in manifest["artifacts"] if item["platform"] == "windows_amd64"]
    _require(len(artifacts) == 1, "Wheel Windows worker identity was ambiguous")
    artifact = artifacts[0]
    _require(
        _digest(supplied) == artifact["executable_sha256"]
        and supplied.stat().st_size == artifact["executable_bytes"],
        "Supplied native worker identity was refused",
    )
    license_path = supplied.parent / "LICENSE"
    _require(
        license_path.is_file() and _digest(license_path) == manifest["license_sha256"],
        "Supplied native worker license identity was refused",
    )
    component = runtime / "tools" / manifest["component"]
    _private_directory(component)
    cache = component / manifest["version"]
    _private_directory(cache)
    _copy_private(supplied, cache / artifact["executable"])
    _copy_private(license_path, cache / "LICENSE")


def _installed_wheel_import(runtime: Path, wheel: Path) -> None:
    __tracebackhide__ = True
    with zipfile.ZipFile(wheel) as package:
        hashes = {
            name: hashlib.sha256(package.read("k5vision/" + name)).hexdigest() for name in _MODULES
        }
    code = (
        "import hashlib,json,sys; from pathlib import Path; import k5vision; "
        "root=Path(k5vision.__file__).resolve().parent; expected=Path(sys.argv[1]).resolve(); "
        "hashes=json.loads(sys.argv[2]); "
        "ok=Path(sys.prefix).resolve()==expected and root.is_relative_to(expected) "
        "and all(hashlib.sha256((root/name).read_bytes()).hexdigest()==value "
        "for name,value in hashes.items()); sys.exit(0 if ok else 7)"
    )
    python = runtime / ".venv" / "Scripts" / "python.exe"
    result = subprocess.run(
        [str(python), "-I", "-B", "-c", code, str(runtime / ".venv"), json.dumps(hashes)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=15,
        check=False,
    )
    _require(result.returncode == 0, "Starter imported outside its installed wheel runtime")


def test_native_windows_powershell_launcher_installs_wheel_and_authenticates(
    record_testsuite_property,
) -> None:
    __tracebackhide__ = True
    _require(
        sys.platform == "win32"
        and sys.version_info[:2] == (3, 12)
        and sys.version_info.micro >= 4
        and struct.calcsize("P") == 8,
        "Launcher proof requires native 64-bit Python 3.12.4 or newer",
    )
    project = Path(__file__).resolve().parents[2]
    wheel = Path(os.environ["K5_OPERATOR_WHEEL"]).resolve(strict=True)
    _require(wheel.suffix == ".whl", "Explicit operator wheel required")
    powershell = (
        Path(os.environ["SYSTEMROOT"])
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    version = subprocess.run(
        [
            str(powershell),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "if ($PSVersionTable.PSVersion.Major -ne 5 -or "
            "$PSVersionTable.PSVersion.Minor -ne 1) { exit 2 }; exit 0",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=15,
        check=False,
    )
    _require(version.returncode == 0, "Actual Windows PowerShell 5.1 required")
    parent = Path(os.environ["LOCALAPPDATA"]) / f"K5 Launcher Pilot-{uuid4().hex}"
    owned = None
    descendants = []
    completed = False
    try:
        _private_directory(parent)
        runtime, state = parent / "runtime with spaces", parent / "state with spaces"
        _private_directory(runtime)
        _private_directory(state)
        _private_directory(runtime / "tools")
        username, password = "fixture-admin", secrets.token_urlsafe(32)
        identity = identity_state.initialize_identity_state(
            state / "identity", username=username, password=password
        )
        for name, environment in (
            ("mediamtx", "K5_MEDIAMTX_DONOR_EXE"),
            ("caddy", "K5_CADDY_DONOR_EXE"),
        ):
            _cache_worker(runtime, wheel, name, Path(os.environ[environment]).resolve(strict=True))
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
            reservation.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        diagnostic_name = "launcher-diagnostic-" + uuid4().hex + ".json"
        owned = OwnedNativeProcess(
            [
                str(powershell),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(project / "scripts" / "Start-K5Operator.ps1"),
                "-NoBrowser",
                "-PythonExecutable",
                sys.executable,
                "-PackagePath",
                str(wheel),
                "-RuntimeRoot",
                str(runtime),
                "-StateRoot",
                str(state),
                "-Port",
                str(port),
                "-DiagnosticName",
                diagnostic_name,
            ],
            cwd=project,
        )
        origin = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 150
        while True:
            if owned.poll() is not None:
                _require(False, _launcher_failure(runtime, diagnostic_name))
            status, body = _request(origin, "/api/v1/health")
            if status == 200 and _json(body).get("status") == "ok":
                break
            _require(time.monotonic() < deadline, "PowerShell launcher readiness deadline expired")
            time.sleep(0.2)
        status, body = _request(
            origin, "/api/v1/auth/login", data={"username": username, "password": password}
        )
        _require(status == 200, "Installed starter administrator login failed")
        login = _json(body)
        token = login.get("session_token")
        _require(isinstance(token, str) and 24 <= len(token) <= 256, "Session admission failed")
        _require(
            login.get("account", {}).get("role") == "administrator",
            "Installed starter administrator authority failed",
        )
        status, body = _request(origin, "/api/v1/operator/cameras", token=token)
        catalog = _json(body)
        _require(
            status == 200
            and catalog.get("cameras") == []
            and catalog.get("worker_status") == "ready",
            "Installed starter native workers were not ready",
        )
        _installed_wheel_import(runtime, wheel)
        _require(
            identity_state.load_identity_state(identity.directory).site_id == identity.site_id,
            "Starter changed its preprovisioned durable identity",
        )
        descendants = psutil.Process(owned.pid).children(recursive=True)
        _require(len(descendants) >= 4, "Native launcher process tree was incomplete")
        for process in descendants:
            process.create_time()  # Bind later liveness checks to this process identity.
        completed = True
    finally:
        if owned is not None:
            owned.stop()
            _require(owned.poll() is not None, "Owned launcher leader was not reaped")
            _, alive = psutil.wait_procs(descendants, timeout=10)
            _require(not alive, "Owned launcher descendants survived cleanup")
        if parent.exists():
            shutil.rmtree(parent)
    _require(completed, "Native launcher startup proof did not complete")
    receipt = {
        "schema_version": "1",
        "platform": platform.system(),
        "native_windows_execution": True,
        "powershell_version": "5.1",
        "preprovisioned_identity": True,
        "verified_cached_workers": True,
        "wheel_install": True,
        "isolated_wheel_import": True,
        "health": True,
        "administrator_login": True,
        "worker_ready": True,
        "owned_tree_cleanup": True,
        "camera_acceptance": False,
        "browser_acceptance": False,
        "analytics_acceptance": False,
    }
    record_testsuite_property(
        "operator_windows_launcher_receipt", json.dumps(receipt, sort_keys=True)
    )
