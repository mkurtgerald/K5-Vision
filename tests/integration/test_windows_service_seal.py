"""Exercise the actual privileged C# sealer on a fresh disposable Windows VM.

Opt in with K5_WINDOWS_SERVICE_SEAL_TEST=1 before installing K5. The production
guard deliberately has no path override, so this test refuses any existing
Program Files installation and removes only the fixture it creates itself.
No service, media worker, installer or application window is started.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import pytest

pytestmark = pytest.mark.skipif(
    os.name != "nt" or os.getenv("K5_WINDOWS_SERVICE_SEAL_TEST") != "1",
    reason="Native sealer proof requires explicit opt-in on a fresh disposable Windows VM",
)

_ROOT = Path(__file__).resolve().parents[2]
_MANDATORY = (
    "runtime/python.exe",
    "runtime/python312._pth",
    "K5VisionRecorder.exe",
    "K5VisionRecorder.xml",
    "licenses/WinSW-MIT.txt",
    "workers/donor-downloads.json",
    "workers/mediamtx.exe",
    "workers/caddy.exe",
    "workers/mediamtx-LICENSE",
    "workers/caddy-LICENSE",
)
_BOOTSTRAP = ("unins000.exe", "unins000.dat", "unins000.msg")


def _program_files() -> Path:
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    ole = ctypes.WinDLL("ole32", use_last_error=True)
    shell.SHGetKnownFolderPath.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    shell.SHGetKnownFolderPath.restype = ctypes.c_int32
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole.CoTaskMemFree.restype = None
    identifier = ctypes.create_string_buffer(UUID("905e63b6-c1bf-494e-b29c-65b732d3d21a").bytes_le)
    result = ctypes.c_void_p()
    try:
        assert shell.SHGetKnownFolderPath(identifier, 0, None, ctypes.byref(result)) == 0
        assert result.value
        return Path(ctypes.wstring_at(result))
    finally:
        if result.value:
            ole.CoTaskMemFree(result)


def _security(path: Path) -> tuple[str, bytes]:
    """Capture the exact owner SID and native DACL bytes, without changing either."""
    from k5vision.windows_identity_security import _native_security

    native = _native_security()
    owner, dacl, descriptor = (ctypes.c_void_p() for _ in range(3))
    assert (
        native.security.GetNamedSecurityInfoW(
            str(path),
            1,
            0x00000005,
            ctypes.byref(owner),
            None,
            ctypes.byref(dacl),
            None,
            ctypes.byref(descriptor),
        )
        == 0
    )
    try:
        assert owner.value and dacl.value and descriptor.value
        size = struct.unpack_from("<H", ctypes.string_at(dacl, 8), 2)[0]
        assert 8 <= size <= 65535
        return native.sid(owner.value), ctypes.string_at(dacl, size)
    finally:
        native.kernel.LocalFree(descriptor)


def _tree_security(root: Path) -> dict[str, tuple[str, bytes]]:
    return {
        str(path.relative_to(root)): _security(path) for path in [root, *sorted(root.rglob("*"))]
    }


@pytest.fixture(scope="module")
def compiled_sealer(tmp_path_factory):
    assert ctypes.sizeof(ctypes.c_void_p) == 8, "The native fixture requires x64 Python"
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    shell.IsUserAnAdmin.argtypes = []
    shell.IsUserAnAdmin.restype = ctypes.c_int
    assert shell.IsUserAnAdmin(), "Explicit native qualification requires an elevated disposable VM"
    build = tmp_path_factory.mktemp("actual-csharp-sealer")
    payload = build / "payload"
    payload.mkdir()
    inventory = []
    for relative in _MANDATORY:
        path = payload / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = ("inert native sealer fixture: " + relative + "\n").encode()
        path.write_bytes(raw)
        inventory.append(
            {"path": relative, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        )
    head = "f" * 40
    manifest = json.dumps({"schema_version": 1, "head_sha": head, "files": inventory}).encode()
    (payload / "payload-manifest.json").write_bytes(manifest)
    (build / "PayloadIdentity.generated.cs").write_text(
        "namespace K5VisionDesktop { internal static class PayloadIdentity {\n"
        f' internal const string ManifestSha256 = "{hashlib.sha256(manifest).hexdigest()}";\n'
        f' internal const string HeadSha = "{head}";\n'
        "} }\n",
        encoding="utf-8",
    )
    # The production compiler helper compiles all four actual source files and
    # the real ClientProgram entry point; no sealer code is copied into a test.
    result = subprocess.run(
        [sys.executable, str(_ROOT / "scripts/compile_desktop_launcher.py"), "--build", str(build)],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        timeout=150,
        check=False,
    )
    assert result.returncode == 0, (result.stdout + result.stderr)[-8000:]
    guard = payload / "K5Vision.exe"
    assert guard.is_file()
    for name in _BOOTSTRAP:
        (payload / name).write_bytes(("inert Inno bootstrap fixture: " + name).encode())
    return payload, guard


@dataclass(frozen=True)
class _Fixture:
    installed: Path
    guard: Path
    outside: Path

    def seal(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(self.guard), "--installation-service-seal"],
            cwd=self.guard.parent,
            capture_output=True,
            timeout=45,
            check=False,
        )


@pytest.fixture
def site_fixture(compiled_sealer):
    payload, guard = compiled_sealer
    installed = _program_files() / "K5 Vision"
    # Never overwrite, adopt, seal or remove a pre-existing application tree.
    try:
        installed.lstat()
    except FileNotFoundError:
        pass
    else:
        pytest.fail("Refusing to touch a pre-existing K5 installation; use a fresh disposable VM")
    # Keep the outside witness on the same volume so an actual hardlink is
    # possible even if the runner's ordinary TEMP directory is on another drive.
    outside = Path(tempfile.mkdtemp(prefix="K5-seal-witness-", dir=installed.parent))
    created = False
    try:
        installed.mkdir()
        created = True
        shutil.copytree(payload, installed, dirs_exist_ok=True)
        yield _Fixture(installed, guard, outside)
    finally:
        if created:
            shutil.rmtree(installed)
        shutil.rmtree(outside)


@pytest.mark.parametrize("listed", [False, True], ids=["unlisted-link", "listed-exact-bytes-link"])
def test_actual_sealer_refuses_hardlinks_before_changing_any_owner_or_dacl(site_fixture, listed):
    fixture = site_fixture
    original = fixture.installed / "workers/mediamtx.exe"
    outside = fixture.outside / "unchanged-outside-file.bin"
    outside.write_bytes(original.read_bytes())
    attack = original if listed else fixture.installed / "workers/unlisted-link.bin"
    if listed:
        original.unlink()
    os.link(outside, attack)
    assert outside.stat().st_nlink == 2
    before_outside = _security(outside)
    before_tree = _tree_security(fixture.installed)
    before_bytes = outside.read_bytes()
    assert fixture.seal().returncode != 0
    assert _security(outside) == before_outside
    assert outside.read_bytes() == before_bytes
    assert _tree_security(fixture.installed) == before_tree


def test_actual_sealer_refuses_a_live_writer_before_changing_any_acl(site_fixture):
    fixture = site_fixture
    outside = fixture.outside / "unchanged-outside-file.bin"
    outside.write_bytes(b"outside witness remains untouched")
    before_outside = _security(outside)
    before_tree = _tree_security(fixture.installed)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    kernel.CreateFileW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int
    # The writer itself shares everything. The sealer must still refuse it
    # because the sealer's READ-only sharing excludes existing write access.
    writer = kernel.CreateFileW(
        str(fixture.installed / "workers/mediamtx.exe"), 2, 7, None, 3, 0, None
    )
    assert writer and writer != ctypes.c_void_p(-1).value
    try:
        assert fixture.seal().returncode != 0
        assert _tree_security(fixture.installed) == before_tree
        assert _security(outside) == before_outside
    finally:
        assert kernel.CloseHandle(writer)


def test_actual_sealer_accepts_only_the_complete_ordinary_fixture(site_fixture):
    from k5vision.windows_identity_security import _native_security

    fixture = site_fixture
    before = {
        str(path.relative_to(fixture.installed)): hashlib.sha256(path.read_bytes()).digest()
        for path in fixture.installed.rglob("*")
        if path.is_file()
    }
    assert fixture.seal().returncode == 0
    assert before == {
        str(path.relative_to(fixture.installed)): hashlib.sha256(path.read_bytes()).digest()
        for path in fixture.installed.rglob("*")
        if path.is_file()
    }
    native = _native_security()
    for path in [fixture.installed, *fixture.installed.rglob("*")]:
        owner, entries = native.descriptor(str(path))
        assert owner == "S-1-5-32-544"
        assert {entry.sid for entry in entries} == {"S-1-5-18", "S-1-5-32-544", "S-1-5-32-545"}
        assert all(entry.kind == 0 and not entry.flags & 3 for entry in entries)
        users = [entry for entry in entries if entry.sid == "S-1-5-32-545"]
        assert len(users) == 1 and users[0].mask == 0x001200A9  # ReadAndExecute only.
