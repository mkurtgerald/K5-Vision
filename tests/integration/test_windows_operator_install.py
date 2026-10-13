"""Opt-in actual Windows PowerShell 5.1 installed-operator lifecycle proof.

No mocks, credential argv, private stdout, fixture media or profile in evidence.
The only receipt is a finite set of outcomes; supplied workers are hash-admitted.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener
from uuid import uuid4

import psutil
import pytest

from k5vision import identity_state
from k5vision.operator_native_process import OwnedNativeProcess

_INPUTS = ("K5_OPERATOR_WHEEL", "K5_MEDIAMTX_DONOR_EXE", "K5_CADDY_DONOR_EXE")
pytestmark = pytest.mark.skipif(
    os.name != "nt" or not all(os.getenv(name) for name in _INPUTS),
    reason="Installed operator proof requires Windows and explicitly supplied wheel/tools",
)
_FILES = (
    "scripts/Start-K5Operator.ps1",
    "scripts/Install-K5Operator.ps1",
    "scripts/Uninstall-K5Operator.ps1",
    "src/k5vision/windows_identity_security.py",
    "docs/WINDOWS_OPERATOR_INSTALL.md",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "third_party/licenses/MediaMTX-MIT.txt",
    "third_party/licenses/Caddy-Apache-2.0.txt",
)


def _require(condition: bool, message: str) -> None:
    __tracebackhide__ = True
    if not condition:
        pytest.fail(message, pytrace=False)


def _private_directory(path: Path) -> None:
    __tracebackhide__ = True
    identity_state.validate_new_identity_directory(path)
    path.mkdir(mode=0o700)
    identity_state._checked_path(path)
    identity_state._windows_admission(path, private=True, directory=True)


def _copy(source: Path, destination: Path) -> None:
    __tracebackhide__ = True
    with destination.open("xb") as output, source.open("rb") as incoming:
        shutil.copyfileobj(incoming, output, length=1024**2)
    identity_state._regular_file(destination)


def _digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _stage_package(project: Path, package: Path, wheel: Path) -> None:
    __tracebackhide__ = True
    _private_directory(package)
    for name in _FILES:
        target = package / name
        for parent in reversed(target.parent.parents):
            if parent.is_relative_to(package) and not parent.exists():
                _private_directory(parent)
        if not target.parent.exists():
            _private_directory(target.parent)
        _copy(project / name, target)
    _copy(project / "docs/WINDOWS_OPERATOR_PILOT.md", package / "README.md")
    _private_directory(package / "dist")
    _copy(wheel, package / "dist" / wheel.name)


def _cache_workers(runtime: Path, wheel: Path) -> None:
    __tracebackhide__ = True
    _private_directory(runtime / "tools")
    with zipfile.ZipFile(wheel) as archive:
        for name, variable in (
            ("mediamtx", "K5_MEDIAMTX_DONOR_EXE"),
            ("caddy", "K5_CADDY_DONOR_EXE"),
        ):
            manifest = json.loads(archive.read(f"k5vision/data/{name}-donor-manifest.json"))
            artifacts = [a for a in manifest["artifacts"] if a["platform"] == "windows_amd64"]
            _require(len(artifacts) == 1, "Expected one Windows worker artifact")
            artifact = artifacts[0]
            source = Path(os.environ[variable]).resolve(strict=True)
            license_path = source.parent / "LICENSE"
            _require(
                _digest(source) == artifact["executable_sha256"]
                and source.stat().st_size == artifact["executable_bytes"]
                and _digest(license_path) == manifest["license_sha256"],
                "Supplied native worker/license identity refused",
            )
            component = runtime / "tools" / manifest["component"]
            _private_directory(component)
            cache = component / manifest["version"]
            _private_directory(cache)
            _copy(source, cache / artifact["executable"])
            _copy(license_path, cache / "LICENSE")


def _upgrade_same_version_wheel(wheel: Path) -> str:
    """Change only a harmless source comment and its valid wheel RECORD hash."""
    module = "k5vision/operator_product.py"
    with zipfile.ZipFile(wheel) as original:
        entries = original.infolist()
        contents = {entry.filename: original.read(entry.filename) for entry in entries}
    contents[module] += b"\n# Native installed same-version upgrade acceptance fixture.\n"
    module_hash = hashlib.sha256(contents[module]).digest()
    records = [name for name in contents if name.endswith(".dist-info/RECORD")]
    _require(len(records) == 1, "Operator wheel RECORD identity was ambiguous")
    rows = list(csv.reader(io.StringIO(contents[records[0]].decode())))
    for row in rows:
        if row[0] == module:
            row[1] = "sha256=" + base64.urlsafe_b64encode(module_hash).decode().rstrip("=")
            row[2] = str(len(contents[module]))
    record = io.StringIO(newline="")
    csv.writer(record).writerows(rows)
    contents[records[0]] = record.getvalue().encode()
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as rewritten:
        for entry in entries:
            rewritten.writestr(entry, contents[entry.filename])
    wheel.write_bytes(archive.getvalue())
    return module_hash.hex()


def _verify_installed_module(runtime: Path, digest: str) -> None:
    __tracebackhide__ = True
    code = (
        "import hashlib,sys; from pathlib import Path; import k5vision; "
        "root=Path(k5vision.__file__).resolve().parent; expected=Path(sys.argv[1]).resolve(); "
        "actual=hashlib.sha256((root/'operator_product.py').read_bytes()).hexdigest(); "
        "sys.exit(0 if Path(sys.prefix).resolve()==expected and root.is_relative_to(expected) "
        "and actual==sys.argv[2] else 7)"
    )
    result = subprocess.run(
        [
            str(runtime / ".venv/Scripts/python.exe"),
            "-I",
            "-B",
            "-c",
            code,
            str(runtime / ".venv"),
            digest,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=15,
        check=False,
    )
    _require(result.returncode == 0, "Private installed runtime did not import the upgraded wheel")


def _run_script(powershell: Path, script: Path, arguments: list[str]) -> tuple[int, str]:
    __tracebackhide__ = True
    result = subprocess.run(
        [
            str(powershell),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            *arguments,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=45,
        check=False,
    )
    # Only a fixed stage from our script is admitted into a failure assertion.
    text = result.stdout[:1024].decode("ascii", errors="ignore")
    match = re.search(r"installation failed at ([a-z-]+)\.", text)
    stage = match.group(1) if match else "unclassified"
    if stage not in {"platform", "python", "package-and-private-paths", "shortcuts"}:
        stage = "unclassified"
    detail = re.search(
        r"K5 install diagnostic: ([a-z0-9-]+) public-line ([0-9]{1,4}) reason ([a-z-]+)", text
    )
    if (
        detail
        and detail.group(1)
        in {
            "bootstrap",
            "package-admission",
            "root-separation",
            "profile-admission",
            "shortcut-admission",
            "shortcut-slot-0",
            "shortcut-slot-1",
            "shortcut-slot-2",
            "identity-admission",
            "package-content",
            "wheel-admission",
            "install-admission",
            "release-lock",
            "runtime-lock",
            "release-stage",
            "release-commit",
        }
        and detail.group(3)
        in {
            "private-boundary",
            "foreign-access",
            "file-inheritance",
            "ownership",
            "volume-alias",
            "volume-type",
            "acl-query",
            "python-validation",
            "file-operation",
            "other",
        }
    ):
        stage += (
            "/" + detail.group(1) + " public-line " + detail.group(2) + " reason " + detail.group(3)
        )
    return result.returncode, stage


def _shortcut_paths(powershell: Path) -> list[Path]:
    __tracebackhide__ = True
    code = (
        "$paths=@('Programs','DesktopDirectory','Startup') | ForEach-Object { "
        "[IO.Path]::Combine([Environment]::GetFolderPath($_,'DoNotVerify'),"
        "'K5 Vision Operator.lnk') }; "
        "$json=$paths | ConvertTo-Json -Compress; "
        "[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json))"
    )
    result = subprocess.run(
        [str(powershell), "-NoProfile", "-NonInteractive", "-Command", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=15,
        check=False,
    )
    _require(result.returncode == 0, "Native special-folder discovery failed")
    # Real special folder paths must survive non-ASCII Windows user names.
    return [Path(value) for value in json.loads(base64.b64decode(result.stdout))]


def _verify_shortcuts(powershell: Path, install: Path, paths: list[Path]) -> None:
    __tracebackhide__ = True
    code = (
        "$shell=New-Object -ComObject WScript.Shell; "
        "$entry=$shell.CreateShortcut($args[0]); "
        "$expected='-NoProfile -ExecutionPolicy Bypass -File ' + [char]34 + "
        "[IO.Path]::Combine($args[2],'Start-K5Operator.ps1') + [char]34; "
        "if ($args[3] -eq '2') { $expected+=' -NoBrowser' }; "
        "if ($entry.TargetPath -ine $args[1] -or $entry.Arguments -cne $expected) { exit 7 }"
    )
    for index, path in enumerate(paths):
        _require(path.is_file(), "Installed shortcut is missing")
        # Fixed script constructs quotes; PS5.1 never transports them in argv.
        verifier = install.parent / ("shortcut-check-" + uuid4().hex + ".ps1")
        verifier.write_text(code, encoding="ascii")
        try:
            result, _ = _run_script(
                powershell, verifier, [str(path), str(powershell), str(install), str(index)]
            )
            _require(result == 0, "Installed shortcut dispatch differs from its owned starter")
        finally:
            verifier.unlink()


def _make_foreign_shortcut(powershell: Path, parent: Path, path: Path) -> None:
    __tracebackhide__ = True
    script = parent / "foreign-shortcut-fixture.ps1"
    script.write_text(
        "$null=[IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($args[0])); "
        "$shell=New-Object -ComObject WScript.Shell; "
        "$entry=$shell.CreateShortcut($args[0]); $entry.TargetPath=$args[1]; "
        "$entry.Arguments='-NoProfile'; $entry.Save()",
        encoding="ascii",
    )
    try:
        result, _ = _run_script(powershell, script, [str(path), str(powershell)])
        _require(result == 0 and path.exists(), "Foreign shortcut fixture failed")
    finally:
        script.unlink()


def _foreign_access(path: Path, *, grant: bool, permission: str = "F") -> None:
    __tracebackhide__ = True
    executable = Path(os.environ["SYSTEMROOT"]) / "System32/icacls.exe"
    access = "*S-1-1-0:(OI)(CI)F" if permission == "F" else "*S-1-1-0:" + permission
    arguments = ["/grant", access] if grant else ["/remove:g", "*S-1-1-0"]
    result = subprocess.run(
        [str(executable), str(path), *arguments],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=15,
        check=False,
    )
    _require(result.returncode == 0, "Native foreign-access fixture operation failed")


def _request(origin: str, route: str, data: dict | None = None) -> tuple[int, dict]:
    __tracebackhide__ = True
    payload = None if data is None else json.dumps(data).encode()
    request = Request(
        origin + route,
        data=payload,
        headers={"Origin": origin, "Content-Type": "application/json"},
    )
    opener = build_opener(ProxyHandler({}))
    try:
        try:
            response = opener.open(request, timeout=2)
        except HTTPError as error:
            response = error
        with response:
            body = response.read(65_537)
            _require(len(body) <= 65_536, "Installed operator response exceeded its budget")
            return response.code, json.loads(body)
    except OSError:
        return 0, {}


def _installed_failure(install: Path, runtime: Path, name: str) -> str:
    __tracebackhide__ = True
    candidates = (
        (
            install / name,
            {
                "bootstrap",
                "private-bootstrap",
                "active-settings",
                "release-manifest",
                "release-files",
                "wheel-admission",
                "verification",
                "settings-parsing",
                "starter-dispatch",
            },
        ),
        (
            runtime / ("launcher-diagnostic-" + name[30:]),
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
            },
        ),
    )
    reasons = {
        "private-boundary",
        "python-validation",
        "file-operation",
        "other",
        "powershell-operation",
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
    for path, stages in candidates:
        try:
            identity_state._regular_file(path)
            with path.open("rb") as source:
                body = source.read(1025)
            if len(body) > 1024:
                continue
            detail = json.loads(body)
            if (
                detail.get("schema_version") == "1"
                and detail.get("stage") in stages
                and detail.get("reason") in reasons
                and type(detail.get("line")) is int
                and 0 <= detail["line"] <= 5000
            ):
                return (
                    "fixed stage "
                    + detail["stage"]
                    + " public-line "
                    + str(detail["line"])
                    + " reason "
                    + detail["reason"]
                )
        except Exception:
            pass
    return "fixed diagnostic unavailable"


def test_native_windows_installed_operator_lifecycle(
    record_testsuite_property, monkeypatch
) -> None:
    __tracebackhide__ = True
    project = Path(__file__).resolve().parents[2]
    wheel = Path(os.environ["K5_OPERATOR_WHEEL"]).resolve(strict=True)
    powershell = Path(os.environ["SYSTEMROOT"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    version = subprocess.run(
        [
            str(powershell),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$PSVersionTable.PSVersion.ToString()",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=15,
    )
    _require(
        version.returncode == 0 and version.stdout.startswith(b"5.1."), "Actual PS5.1 required"
    )
    shortcuts = _shortcut_paths(powershell)
    if any(path.exists() for path in shortcuts):
        pytest.skip("Native lifecycle proof requires disposable unoccupied operator shortcuts")
    parent = Path(os.environ["LOCALAPPDATA"]) / ("K5 Install Proof-" + uuid4().hex)
    owned = None
    descendants = []
    install = parent / "installed operator with spaces"
    try:
        _private_directory(parent)
        runtime, state = parent / "runtime", parent / "state"
        _private_directory(runtime)
        _private_directory(state)
        username, password = "fixture-admin", secrets.token_urlsafe(32)
        identity = identity_state.initialize_identity_state(
            state / "identity", username=username, password=password
        )
        _private_directory(state / "recordings")
        retained = state / "recordings" / "retained-fixture-marker.txt"
        retained.write_bytes(b"preserve-on-uninstall")
        _cache_workers(runtime, wheel)
        package = parent / "extracted package"
        _stage_package(project, package, wheel)
        installer = package / "scripts/Install-K5Operator.ps1"
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
            reservation.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        arguments = [
            "-PackageRoot",
            str(package),
            "-InstallRoot",
            str(install),
            "-PythonExecutable",
            sys.executable,
            "-RuntimeRoot",
            str(runtime),
            "-StateRoot",
            str(state),
            "-Port",
            str(port),
            "-RecordingLimitGb",
            "1",
            "-DesktopShortcut",
            "-StartAtLogon",
        ]
        canary = parent / "unreviewed-bootstrap-executed.txt"
        helper = package / "src/k5vision/windows_identity_security.py"
        helper_original = helper.read_bytes()
        malicious = (
            helper_original
            + (
                "\nfrom pathlib import Path as _CanaryPath\n"
                f"_CanaryPath({str(canary)!r}).write_bytes(b'unreviewed-code-ran')\n"
            ).encode()
        )
        helper.write_bytes(malicious)
        try:
            result, _ = _run_script(powershell, installer, arguments)
            _require(
                result != 0 and not canary.exists() and not install.exists(),
                "Installer executed an unreviewed bootstrap",
            )
        finally:
            helper.write_bytes(helper_original)
        _foreign_access(package, grant=True)
        try:
            result, _ = _run_script(powershell, installer, arguments)
            _require(
                result != 0 and not install.exists(),
                "Installer accepted a foreign-writable package",
            )
        finally:
            _foreign_access(package, grant=False)
        _make_foreign_shortcut(powershell, parent, shortcuts[0])
        result, _ = _run_script(powershell, installer, arguments)
        _require(result != 0 and not install.exists(), "Installer replaced a foreign shortcut")
        shortcuts[0].unlink()
        result, stage = _run_script(powershell, installer, arguments)
        _require(result == 0, "Native installer failed at " + stage)
        active = (install / "active.json").read_bytes()
        _require(
            _json(install / "active.json")["recording_limit_gb"] == 1,
            "Installed recording limit was not persisted",
        )
        release = _json(install / "active.json")["release"]
        _verify_shortcuts(powershell, install, shortcuts)
        # Nonsecret shortcuts may be readable by another local user, while even
        # a foreign WRITE_DATA-only grant must fail before release/deletion.
        _foreign_access(shortcuts[1], grant=True, permission="(R)")
        try:
            result, stage = _run_script(powershell, installer, arguments)
            _require(result == 0, "Readonly foreign shortcut admission failed at " + stage)
        finally:
            _foreign_access(shortcuts[1], grant=False)
        _foreign_access(shortcuts[1], grant=True, permission="(WD)")
        try:
            result, _ = _run_script(powershell, installer, arguments)
            _require(
                result != 0 and (install / "active.json").read_bytes() == active,
                "Installer accepted foreign-write shortcut bytes",
            )
            result, _ = _run_script(powershell, install / "Uninstall-K5Operator.ps1", [])
            _require(
                result != 0 and shortcuts[1].exists(),
                "Uninstaller accepted foreign-write shortcut bytes",
            )
        finally:
            _foreign_access(shortcuts[1], grant=False)
        result, stage = _run_script(powershell, installer, arguments)
        _require(result == 0, "Repeat native installer failed at " + stage)
        _require(
            (install / "active.json").read_bytes() == active,
            "Repeat install changed active identity",
        )
        _require(
            len(list((install / "releases").iterdir())) == 1, "Repeat install duplicated release"
        )

        diagnostic_name = "installed-launcher-diagnostic-" + uuid4().hex + ".json"
        owned = OwnedNativeProcess(
            [
                str(powershell),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(install / "Start-K5Operator.ps1"),
                "-NoBrowser",
                "-DiagnosticName",
                diagnostic_name,
            ],
            cwd=parent,
        )
        origin = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 150
        while True:
            if owned.poll() is not None:
                _require(
                    False,
                    "Installed wrapper exited: "
                    + _installed_failure(install, runtime, diagnostic_name),
                )
            status, body = _request(origin, "/api/v1/health")
            if status == 200 and body.get("status") == "ok":
                break
            _require(time.monotonic() < deadline, "Installed wrapper readiness deadline expired")
            time.sleep(0.2)
        status, login = _request(
            origin, "/api/v1/auth/login", {"username": username, "password": password}
        )
        _require(
            status == 200 and login.get("account", {}).get("role") == "administrator",
            "Installed wrapper administrator login failed",
        )
        descendants = psutil.Process(owned.pid).children(recursive=True)
        _require(len(descendants) >= 4, "Installed native process tree was incomplete")
        for process in descendants:
            process.create_time()
        result, _ = _run_script(powershell, installer, arguments)
        _require(result != 0, "Installer replaced a running operator")
        result, _ = _run_script(powershell, install / "Uninstall-K5Operator.ps1", [])
        _require(result != 0, "Uninstaller removed a running operator")
        _require(
            (install / "active.json").read_bytes() == active, "Live upgrade changed active release"
        )
        owned.stop()
        _, alive = psutil.wait_procs(descendants, timeout=10)
        _require(not alive, "Installed native descendants survived owned cleanup")
        owned = None

        # A changed same-version wheel must install new source bytes rather than
        # merely switch the release pointer. The fixture adds only a comment.
        upgraded_digest = _upgrade_same_version_wheel(package / "dist" / wheel.name)
        result, stage = _run_script(powershell, installer, arguments)
        _require(result == 0, "Stopped native upgrade failed at " + stage)
        _require(
            _json(install / "active.json")["release"] != release, "Upgrade did not switch release"
        )
        _require(
            len(list((install / "releases").iterdir())) == 2, "Upgrade did not retain prior release"
        )
        _require(
            identity_state.load_identity_state(identity.directory).site_id == identity.site_id,
            "Upgrade changed durable identity",
        )
        # This actual child process inherits no-index; environment restoration
        # does not alter its snapshot. Cached dependencies must suffice while
        # the local same-version K5 wheel still updates its installed code.
        diagnostic_name = "installed-launcher-diagnostic-" + uuid4().hex + ".json"
        with monkeypatch.context() as startup_environment:
            startup_environment.setenv("PIP_NO_INDEX", "1")
            owned = OwnedNativeProcess(
                [
                    str(powershell),
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(install / "Start-K5Operator.ps1"),
                    "-NoBrowser",
                    "-DiagnosticName",
                    diagnostic_name,
                ],
                cwd=parent,
            )
        deadline = time.monotonic() + 150
        while True:
            if owned.poll() is not None:
                _require(
                    False,
                    "Upgraded wrapper exited: "
                    + _installed_failure(install, runtime, diagnostic_name),
                )
            status, body = _request(origin, "/api/v1/health")
            if status == 200 and body.get("status") == "ok":
                break
            _require(time.monotonic() < deadline, "Upgraded wrapper readiness deadline expired")
            time.sleep(0.2)
        _verify_installed_module(runtime, upgraded_digest)
        status, login = _request(
            origin, "/api/v1/auth/login", {"username": username, "password": password}
        )
        _require(
            status == 200 and login.get("account", {}).get("role") == "administrator",
            "Upgraded installed wrapper lost administrator login",
        )
        descendants = psutil.Process(owned.pid).children(recursive=True)
        for process in descendants:
            process.create_time()
        owned.stop()
        _, alive = psutil.wait_procs(descendants, timeout=10)
        _require(not alive, "Upgraded native descendants survived owned cleanup")
        owned = None

        current = install / "releases" / _json(install / "active.json")["release"]
        # Forging helper + its mutable certifying metadata must never execute
        # helper code. Only the reviewed LF/CRLF pins authorize bootstrap code.
        installed_helper = install / "windows_identity_security.py"
        original_helper = installed_helper.read_bytes()
        original_active = (install / "active.json").read_bytes()
        original_manifest = (current / "release.json").read_bytes()
        forged_manifest = _json(current / "release.json")
        forged_manifest["files"]["src/k5vision/windows_identity_security.py"] = hashlib.sha256(
            malicious
        ).hexdigest()
        forged_bytes = json.dumps(forged_manifest, sort_keys=True, separators=(",", ":")).encode()
        forged_release = hashlib.sha256(forged_bytes).hexdigest()
        forged_path = current.parent / forged_release
        current.rename(forged_path)
        try:
            installed_helper.write_bytes(malicious)
            (forged_path / "release.json").write_bytes(forged_bytes)
            forged_active = json.loads(original_active)
            forged_active["release"] = forged_release
            (install / "active.json").write_bytes(json.dumps(forged_active).encode())
            result, _ = _run_script(powershell, install / "Uninstall-K5Operator.ps1", [])
            _require(
                result != 0 and not canary.exists() and install.exists(),
                "Uninstaller executed a metadata-certified unreviewed bootstrap",
            )
        finally:
            installed_helper.write_bytes(original_helper)
            (install / "active.json").write_bytes(original_active)
            (forged_path / "release.json").write_bytes(original_manifest)
            forged_path.rename(current)
        _foreign_access(install, grant=True)
        try:
            result, _ = _run_script(powershell, install / "Uninstall-K5Operator.ps1", [])
            _require(
                result != 0 and (install / "active.json").read_bytes() == original_active,
                "Uninstaller accepted foreign-writable application metadata",
            )
        finally:
            _foreign_access(install, grant=False)
        changed = current / "README.md"
        original = changed.read_bytes()
        changed.write_bytes(original + b"tamper")
        result, _ = _run_script(powershell, install / "Start-K5Operator.ps1", ["-NoBrowser"])
        _require(result != 0, "Installed wrapper accepted a changed release file")
        result, _ = _run_script(powershell, installer, arguments)
        _require(result != 0, "Repeat installation repaired a changed release silently")
        changed.write_bytes(original)
        unknown = install / "unowned.txt"
        unknown.write_bytes(b"do-not-delete")
        result, _ = _run_script(powershell, install / "Uninstall-K5Operator.ps1", [])
        _require(result != 0 and unknown.exists(), "Uninstall deleted unknown application content")
        unknown.unlink()
        _foreign_access(shortcuts[1], grant=True, permission="(R)")
        result, _ = _run_script(powershell, install / "Uninstall-K5Operator.ps1", [])
        _require(result == 0 and not install.exists(), "Native application uninstall failed")
        _require(not any(path.exists() for path in shortcuts), "Owned shortcuts survived uninstall")
        _require(
            retained.read_bytes() == b"preserve-on-uninstall", "Uninstall removed recording data"
        )
        _require(
            (runtime / ".venv/Scripts/python.exe").exists(), "Uninstall removed reusable runtime"
        )
        _require(
            identity_state.load_identity_state(identity.directory).site_id == identity.site_id,
            "Uninstall removed or changed durable identity",
        )
    finally:
        if owned is not None:
            owned.stop()
            psutil.wait_procs(descendants, timeout=10)
        # Only disposable runner paths made by this test are removed.
        for path in shortcuts:
            if path.exists():
                path.unlink()
        if parent.exists():
            shutil.rmtree(parent)
    record_testsuite_property(
        "operator_windows_install_receipt",
        json.dumps(
            {
                "schema_version": "1",
                "platform": platform.system(),
                "powershell_version": "5.1",
                "native_windows_execution": True,
                "per_user_install": True,
                "start_desktop_logon_shortcuts": True,
                "repeat_install": True,
                "foreign_shortcut_refused": True,
                "readonly_foreign_shortcut_accepted": True,
                "foreign_write_shortcut_refused": True,
                "bootstrap_identity_refused": True,
                "foreign_writable_tree_refused": True,
                "recording_limit_persisted": True,
                "same_version_wheel_upgrade": True,
                "cached_runtime_no_index": True,
                "installed_wrapper_health": True,
                "administrator_login": True,
                "live_upgrade_uninstall_refused": True,
                "owned_tree_cleanup": True,
                "stopped_upgrade": True,
                "prior_release_preserved": True,
                "changed_release_refused": True,
                "unknown_content_refused": True,
                "uninstall_preserves_profile": True,
                "windows_service": False,
                "browser_acceptance": False,
                "physical_camera_acceptance": False,
                "analytics_acceptance": False,
            },
            sort_keys=True,
        ),
    )
