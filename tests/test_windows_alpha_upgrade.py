"""Filesystem fault injection for the installer, without installing software.

Portable tests exercise the real transaction engine. Only the subprocess/media
boundary is simulated; native Windows process/COM execution is a separate gate.
"""

import importlib.util
import json
import os
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts" / "windows-alpha"
SPEC = importlib.util.spec_from_file_location(
    "alpha_install_transaction", SOURCE / "install_transaction.py"
)
assert SPEC and SPEC.loader
transaction = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(transaction)


class FakeInstaller(transaction.Installer):
    """Replace only native commands with deterministic on-disk artifacts."""

    def __init__(self, root: Path, shortcut: Path | None = None):
        super().__init__(root, SOURCE, Path("powershell.exe"), "a" * 40, "1.28.7", shortcut)
        self.events = []
        self.fail = None
        self.inventory = []
        self.originals = snapshot(root)
        self.run = Mock(return_value=subprocess.CompletedProcess([], 0, ""))

    def event(self, event):
        self.events.append(event)
        if event == self.fail:
            raise RuntimeError(f"injected {event}")

    def powershell(self, command, *, capture=False):
        assert "Stop-Process" not in command
        return json.dumps(self.inventory)

    def prepare_wheels(self):
        self.event("download")
        self.wheels.mkdir()
        (self.wheels / "k5_vision-0.1.0-py3-none-any.whl").write_bytes(b"verified wheel")

    def install_runtime(self, destination):
        phase = "stage" if destination == self.stage else "activate"
        destination.mkdir(exist_ok=True)
        (destination / ".venv").mkdir()
        (destination / ".venv" / "runtime").write_bytes(b"new runtime")
        self.materialize_files(destination)
        self.event(phase)

    def preflight(self, destination):
        if destination == self.stage:
            assert snapshot(self.root) == self.originals
            self.event("stage-preflight")
        else:
            self.event("active-preflight")

    def stage_shortcut(self):
        (self.stage / "desktop-shortcut.lnk").write_bytes(b"new shortcut")
        self.event("shortcut")


def snapshot(root):
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
        and transaction.WORKSPACE not in path.parts
        and path.name != transaction.LOCK
    }


@pytest.fixture
def previous(tmp_path):
    root = tmp_path / "installed K5"
    root.mkdir()
    (root / ".venv").mkdir()
    (root / ".venv" / "runtime").write_bytes(b"prior runtime\0\xff")
    for name in transaction.FILES:
        (root / name).write_bytes(("prior " + name).encode())
    (root / "config").mkdir()
    (root / "config" / "private.json").write_bytes(b"user config")
    (root / "recording.bin").write_bytes(b"user data\0\xff")
    return root


@pytest.mark.parametrize(
    "failure", ["download", "stage", "stage-preflight", "activate", "active-preflight"]
)
def test_failed_upgrade_restores_exact_prior_files(previous, failure):
    before = snapshot(previous)
    installer = FakeInstaller(previous)
    installer.fail = failure
    with pytest.raises(RuntimeError, match=f"injected {failure}"):
        installer.install(skip_shortcut=True)
    assert snapshot(previous) == before
    assert not installer.work.exists()


def test_success_validates_before_activation_and_preserves_config_data(previous):
    installer = FakeInstaller(previous)
    installer.install(skip_shortcut=True)
    assert installer.events == [
        "download",
        "stage",
        "stage-preflight",
        "activate",
        "active-preflight",
    ]
    assert (previous / ".venv" / "runtime").read_bytes() == b"new runtime"
    assert (previous / "config" / "private.json").read_bytes() == b"user config"
    assert (previous / "recording.bin").read_bytes() == b"user data\0\xff"
    assert (previous / "k5-revision.txt").read_text() == "a" * 40
    assert not installer.work.exists()
    # Existing installs must never reprovision their shared GStreamer runtime.
    installer.run.assert_not_called()


def test_failed_first_install_removes_only_new_managed_paths(tmp_path):
    root = tmp_path / "new install"
    root.mkdir()
    (root / "user.txt").write_bytes(b"keep")
    installer = FakeInstaller(root)
    installer.fail = "active-preflight"
    with pytest.raises(RuntimeError, match="injected active-preflight"):
        installer.install(skip_shortcut=True)
    assert snapshot(root) == {"user.txt": b"keep"}
    assert "provision-stage03-gstreamer.ps1" in str(installer.run.call_args)


def test_shortcut_failure_and_activation_failure_preserve_previous_shortcut(previous, tmp_path):
    shortcut = tmp_path / "K5 Vision Alpha.lnk"
    shortcut.write_bytes(b"original shortcut")
    for failure in ("shortcut", "active-preflight"):
        installer = FakeInstaller(previous, shortcut)
        installer.fail = failure
        with pytest.raises(RuntimeError, match=f"injected {failure}"):
            installer.install()
        assert shortcut.read_bytes() == b"original shortcut"
    FakeInstaller(previous, shortcut).install()
    assert shortcut.read_bytes() == b"new shortcut"


def test_shortcut_copy_failure_rolls_back_all_paths(previous, tmp_path, monkeypatch):
    shortcut = tmp_path / "K5 Vision Alpha.lnk"
    shortcut.write_bytes(b"original shortcut")
    before = snapshot(previous)
    installer = FakeInstaller(previous, shortcut)
    copy = transaction.shutil.copyfile

    def fail_copy(source, target, **kwargs):
        if source == installer.stage / "desktop-shortcut.lnk" and target == shortcut:
            shortcut.write_bytes(b"partial")
            raise OSError("shortcut write failure")
        return copy(source, target, **kwargs)

    monkeypatch.setattr(transaction.shutil, "copyfile", fail_copy)
    with pytest.raises(OSError, match="shortcut write failure"):
        installer.install()
    assert snapshot(previous) == before
    assert shortcut.read_bytes() == b"original shortcut"


@pytest.mark.parametrize("inventory", [[{}], [{"ExecutablePath": ""}], [{"ExecutablePath": None}]])
def test_unknown_process_ownership_refuses_without_mutation(previous, inventory):
    before = snapshot(previous)
    installer = FakeInstaller(previous)
    installer.inventory = inventory
    with pytest.raises(RuntimeError, match="Cannot identify a Python process"):
        installer.install(skip_shortcut=True)
    assert snapshot(previous) == before
    assert not installer.work.exists()


@pytest.mark.parametrize("executable", ["python.exe", "pythonw.exe"])
def test_live_installed_python_is_not_killed(previous, executable):
    installer = FakeInstaller(previous)
    installer.inventory = [{"ExecutablePath": str(previous / ".venv" / "Scripts" / executable)}]
    with pytest.raises(RuntimeError, match="K5 Alpha is running"):
        installer.install(skip_shortcut=True)
    assert snapshot(previous) == installer.originals
    assert not installer.events


def test_unrelated_identified_python_is_preserved(previous):
    installer = FakeInstaller(previous)
    installer.inventory = [{"ExecutablePath": str(previous.parent / "other" / "python.exe")}]
    installer.install(skip_shortcut=True)


def test_process_inventory_failure_is_fail_closed(previous):
    installer = FakeInstaller(previous)
    installer.powershell = Mock(side_effect=subprocess.CalledProcessError(1, "inventory"))
    with pytest.raises(subprocess.CalledProcessError):
        installer.install(skip_shortcut=True)
    assert snapshot(previous) == installer.originals


def test_process_starting_during_staging_prevents_activation(previous):
    installer = FakeInstaller(previous)
    preflight = installer.preflight

    def start_runtime(destination):
        preflight(destination)
        installer.inventory = [
            {"ExecutablePath": str(previous / ".venv" / "Scripts" / "python.exe")}
        ]

    installer.preflight = start_runtime
    with pytest.raises(RuntimeError, match="K5 Alpha is running"):
        installer.install(skip_shortcut=True)
    assert snapshot(previous) == installer.originals
    assert "activate" not in installer.events


def test_wheel_change_after_candidate_verification_prevents_activation(previous):
    installer = FakeInstaller(previous)
    preflight = installer.preflight

    def mutate_wheel(destination):
        preflight(destination)
        next(installer.wheels.glob("*.whl")).write_bytes(b"changed")

    installer.preflight = mutate_wheel
    with pytest.raises(RuntimeError, match="wheels changed"):
        installer.install(skip_shortcut=True)
    assert snapshot(previous) == installer.originals


def interrupted(installer, *, count=None):
    installer.work.mkdir()
    installer.backup.mkdir()
    targets = {name: installer.root / name for name in transaction.MANAGED}
    state = {
        "format": transaction.FORMAT,
        "phase": "activating",
        "shortcut": None,
        "originals": {name: target.exists() for name, target in targets.items()},
    }
    installer.save(state)
    for name in list(targets)[:count]:
        targets[name].rename(installer.backup / name)
    return state


@pytest.mark.parametrize("count", [0, 1, 3, None])
def test_next_invocation_recovers_interrupted_backup(previous, count):
    installer = FakeInstaller(previous)
    before = snapshot(previous)
    interrupted(installer, count=count)
    installer.recover()
    assert snapshot(previous) == before
    assert not installer.work.exists()


def test_recovery_restores_interrupted_partial_activation(previous):
    installer = FakeInstaller(previous)
    before = snapshot(previous)
    interrupted(installer)
    installer.stage.mkdir()
    installer.materialize_files(installer.stage)
    installer.install_runtime(previous)
    installer.recover()
    assert snapshot(previous) == before


def test_recovery_failure_retains_backup_and_retry_restores(previous, monkeypatch):
    installer = FakeInstaller(previous)
    before = snapshot(previous)
    installer.fail = "active-preflight"
    rename = Path.rename

    def fail_restore(path, target):
        if path == installer.backup / ".venv":
            raise OSError("locked old runtime")
        return rename(path, target)

    monkeypatch.setattr(Path, "rename", fail_restore)
    with pytest.raises(RuntimeError, match="rollback needs recovery"):
        installer.install(skip_shortcut=True)
    assert (installer.backup / ".venv" / "runtime").read_bytes() == b"prior runtime\0\xff"
    assert installer.journal.is_file()
    monkeypatch.setattr(Path, "rename", rename)
    installer.recover()
    assert snapshot(previous) == before


def test_failed_backup_rename_rolls_back_prior_moves(previous, monkeypatch):
    installer = FakeInstaller(previous)
    before = snapshot(previous)
    rename = Path.rename

    def fail_backup(path, target):
        if path == previous / "Run-K5VisionAlpha.ps1":
            raise OSError("locked launcher")
        return rename(path, target)

    monkeypatch.setattr(Path, "rename", fail_backup)
    with pytest.raises(OSError, match="locked launcher"):
        installer.install(skip_shortcut=True)
    assert snapshot(previous) == before


def test_lock_prevents_concurrent_installer(previous):
    with transaction.install_lock(previous), pytest.raises(RuntimeError, match="in progress"):
        FakeInstaller(previous).install(skip_shortcut=True)


def test_unknown_workspace_is_not_deleted(previous):
    work = previous / transaction.WORKSPACE
    work.mkdir()
    (work / "user.txt").write_bytes(b"leave alone")
    with pytest.raises(RuntimeError, match="Unrecognized upgrade workspace"):
        FakeInstaller(previous).install(skip_shortcut=True)
    assert (work / "user.txt").read_bytes() == b"leave alone"


@pytest.mark.parametrize(
    "field,value", [("phase", "bad"), ("originals", {"../user": False}), ("format", "foreign")]
)
def test_invalid_journal_does_not_mutate_install(previous, field, value):
    installer = FakeInstaller(previous)
    state = interrupted(installer, count=0)
    state[field] = value
    installer.save(state)
    with pytest.raises(RuntimeError, match="[Ii]nvalid|Unrecognized"):
        installer.recover()
    assert snapshot(previous) == installer.originals


@pytest.mark.skipif(
    os.name == "nt", reason="Portable symlink test; native reparse qualification separate"
)
def test_linked_managed_path_is_not_followed(previous, tmp_path):
    target = tmp_path / "unrelated"
    target.write_bytes(b"leave alone")
    launcher = previous / "Run-K5VisionAlpha.ps1"
    launcher.unlink()
    launcher.symlink_to(target)
    installer = FakeInstaller(previous)
    with pytest.raises(RuntimeError, match="link or reparse"):
        installer.install(skip_shortcut=True)
    assert target.read_bytes() == b"leave alone"
    assert launcher.is_symlink()


def test_activation_commands_are_offline_and_venv_is_built_at_destination(tmp_path):
    installer = transaction.Installer(tmp_path, SOURCE, Path("powershell.exe"), "a" * 40, "1.28.7")
    installer.wheels.mkdir(parents=True)
    wheel = installer.wheels / "k5_vision-0.1.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    installer.run = Mock(return_value=subprocess.CompletedProcess([], 0, ""))
    installer.stage.mkdir()
    installer.materialize_files(installer.stage)
    installer.install_runtime(tmp_path)
    calls = [call.args[0] for call in installer.run.call_args_list]
    assert calls[0][-3:] == ["-m", "venv", str(tmp_path / ".venv")]
    pip_install = calls[1]
    assert "--no-index" in pip_install and "--no-deps" in pip_install
    assert pip_install[-1] == str(wheel)
    assert not any("https://" in arg for call in calls for arg in call)
    assert all(
        call.kwargs["check"] and call.kwargs["timeout"] == 900
        for call in installer.run.call_args_list
    )


def test_entrypoint_keeps_pin_and_never_stops_processes():
    entry = (SOURCE / "Install-K5VisionAlpha.ps1").read_text()
    helper = (SOURCE / "install_transaction.py").read_text()
    assert "d531d50d479f46af6ceed324a7cc379745becb61" in entry
    assert "Stop-Process" not in entry + helper
    assert "install_transaction.py" in entry
    assert "kill(" not in helper
    assert "RUNNER_TEMP" in entry
    assert "--skip-shortcut" in entry


def test_repeated_upgrade_keeps_configuration_and_stable_runtime_path(previous):
    for _ in range(2):
        installer = FakeInstaller(previous)
        installer.install(skip_shortcut=True)
        assert (previous / ".venv" / "runtime").read_bytes() == b"new runtime"
        assert (previous / "config" / "private.json").read_bytes() == b"user config"
        assert not installer.work.exists()


@pytest.mark.skipif(os.name != "nt", reason="Requires Windows file sharing semantics")
def test_windows_locked_launcher_failure_restores_prior_install(previous):
    import ctypes
    from ctypes import wintypes

    create_file = ctypes.windll.kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    close = ctypes.windll.kernel32.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    installer = FakeInstaller(previous)
    before = snapshot(previous)
    # Do not read a file held with exclusive sharing during candidate checks.
    installer.preflight = lambda destination: installer.event(
        "stage-preflight" if destination == installer.stage else "active-preflight"
    )
    handle = create_file(str(previous / "Run-K5VisionAlpha.ps1"), 0x80000000, 0, None, 3, 0, None)
    assert handle != wintypes.HANDLE(-1).value
    try:
        with pytest.raises(OSError):
            installer.install(skip_shortcut=True)
    finally:
        close(handle)
    assert snapshot(previous) == before
    assert installer.events == ["download", "stage", "stage-preflight"]


@pytest.mark.skipif(os.name != "nt", reason="Requires native Windows PowerShell")
def test_windows_entrypoint_parses_without_executing_installation(tmp_path):
    import base64

    # Parse the dynamic inventory/shortcut commands too, without executing
    # process enumeration, COM creation, provisioning or installation.
    root = tmp_path / "operator's café"
    (root / ".venv").mkdir(parents=True)
    installer = transaction.Installer(
        root,
        SOURCE,
        Path("powershell.exe"),
        "a" * 40,
        "1.28.7",
        run=Mock(return_value=subprocess.CompletedProcess([], 0, "[]")),
    )
    installer.assert_idle()
    with pytest.raises(RuntimeError, match="shortcut staging failed"):
        installer.stage_shortcut()
    encoded_commands = [
        base64.b64encode(call.args[0][-1].encode("utf-8")).decode("ascii")
        for call in installer.run.call_args_list
    ]
    path = str(SOURCE / "Install-K5VisionAlpha.ps1").replace("'", "''")
    script = (
        "$tokens = $null; $errors = $null; "
        f"[System.Management.Automation.Language.Parser]::ParseFile('{path}', "
        "[ref]$tokens, [ref]$errors) | Out-Null; "
        "if ($errors.Count -ne 0) { $errors | Out-String | Write-Error; exit 1 }"
    )
    for encoded in encoded_commands:
        script += (
            "; $inputText = [Text.Encoding]::UTF8.GetString("
            f"[Convert]::FromBase64String('{encoded}')); "
            "[System.Management.Automation.Language.Parser]::ParseInput("
            "$inputText, [ref]$tokens, [ref]$errors) | Out-Null; "
            "if ($errors.Count -ne 0) { $errors | Out-String | Write-Error; exit 1 }"
        )
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        check=True,
        timeout=30,
    )


def test_child_commands_ignore_ambient_pip_and_python_redirects(tmp_path, monkeypatch):
    monkeypatch.setenv("PIP_TARGET", str(tmp_path / "foreign"))
    monkeypatch.setenv("PIP_PREFIX", str(tmp_path / "foreign-prefix"))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "checkout"))
    monkeypatch.setenv("PYTHONHOME", str(tmp_path / "python"))
    run = Mock(return_value=subprocess.CompletedProcess([], 0, ""))
    installer = transaction.Installer(
        tmp_path, SOURCE, Path("powershell.exe"), "a" * 40, "1.28.7", run=run
    )
    installer.command(["python", "-m", "pip", "check"])
    environment = run.call_args.kwargs["env"]
    assert not {"PIP_TARGET", "PIP_PREFIX", "PYTHONPATH", "PYTHONHOME"} & environment.keys()
    assert environment["PIP_CONFIG_FILE"] == os.devnull


def test_activation_uses_verified_staged_files_even_if_source_changes(previous, tmp_path):
    installer = FakeInstaller(previous)
    source = tmp_path / "source"
    source.mkdir()
    for name in transaction.FILES[:3]:
        (source / name).write_bytes(("verified " + name).encode())
    installer.source = source
    preflight = installer.preflight

    def alter_source(destination):
        preflight(destination)
        (source / "Run-K5VisionAlpha.ps1").write_bytes(b"unverified change")

    installer.preflight = alter_source
    installer.install(skip_shortcut=True)
    assert (previous / "Run-K5VisionAlpha.ps1").read_bytes() == b"verified Run-K5VisionAlpha.ps1"


def test_committed_journal_cleanup_preserves_verified_install(previous):
    installer = FakeInstaller(previous)
    state = interrupted(installer)
    installer.stage.mkdir()
    installer.materialize_files(installer.stage)
    installer.install_runtime(previous)
    before = snapshot(previous)
    state["phase"] = "committed"
    installer.save(state)
    installer.recover()
    assert snapshot(previous) == before
    assert not installer.work.exists()


def test_recovery_does_not_remove_a_running_partial_activation(previous):
    installer = FakeInstaller(previous)
    state = interrupted(installer)
    installer.stage.mkdir()
    installer.materialize_files(installer.stage)
    installer.install_runtime(previous)
    installer.inventory = [{"ExecutablePath": str(previous / ".venv" / "Scripts" / "python.exe")}]
    with pytest.raises(RuntimeError, match="K5 Alpha is running"):
        installer.recover()
    assert (installer.backup / ".venv" / "runtime").read_bytes() == b"prior runtime\0\xff"
    assert json.loads(installer.journal.read_text()) == state
    installer.inventory = []
    installer.recover()
    assert snapshot(previous) == installer.originals


@pytest.mark.parametrize("phase", ["preparing", "committed"])
def test_cleanup_failure_keeps_journal_and_retry_succeeds(previous, phase, monkeypatch):
    installer = FakeInstaller(previous)
    installer.work.mkdir()
    installer.backup.mkdir()
    locked = installer.backup / "locked"
    locked.write_bytes(b"owned leftover")
    installer.save({"format": transaction.FORMAT, "phase": phase, "shortcut": None})
    unlink = transaction.os.unlink

    def fail_locked(path, *args, **kwargs):
        if Path(path).name == "locked":
            raise PermissionError(13, "locked during cleanup", str(path))
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(transaction.os, "unlink", fail_locked)
    with pytest.raises(PermissionError, match="locked during cleanup"):
        installer.recover()
    assert installer.journal.is_file()
    assert locked.read_bytes() == b"owned leftover"
    monkeypatch.setattr(transaction.os, "unlink", unlink)
    installer.recover()
    assert not installer.work.exists()
    assert snapshot(previous) == installer.originals


def test_empty_workspace_after_interrupted_cleanup_is_recoverable(previous):
    installer = FakeInstaller(previous)
    installer.work.mkdir()
    installer.recover()
    assert not installer.work.exists()
    assert snapshot(previous) == installer.originals


@pytest.mark.parametrize("failure", [None, "active-preflight"])
def test_cross_volume_shortcut_never_requires_rename(previous, tmp_path, monkeypatch, failure):
    import errno

    shortcut = tmp_path / "different-volume.lnk"
    shortcut.write_bytes(b"old shortcut")
    installer = FakeInstaller(previous, shortcut)
    installer.fail = failure
    rename = Path.rename

    def volume_boundary(path, target):
        if path == shortcut or target == shortcut:
            raise OSError(errno.EXDEV, "different volumes")
        return rename(path, target)

    monkeypatch.setattr(Path, "rename", volume_boundary)
    if failure:
        with pytest.raises(RuntimeError, match=f"injected {failure}"):
            installer.install()
        assert snapshot(previous) == installer.originals
        assert shortcut.read_bytes() == b"old shortcut"
    else:
        installer.install()
        assert shortcut.read_bytes() == b"new shortcut"


def test_partial_shortcut_backup_is_never_restored_over_original(previous, tmp_path, monkeypatch):
    shortcut = tmp_path / "K5 Vision Alpha.lnk"
    shortcut.write_bytes(b"original shortcut")
    installer = FakeInstaller(previous, shortcut)
    copy = transaction.shutil.copy2

    def fail_backup(source, target):
        if source == shortcut:
            Path(target).write_bytes(b"partial old shortcut")
            raise OSError("backup interrupted")
        return copy(source, target)

    monkeypatch.setattr(transaction.shutil, "copy2", fail_backup)
    with pytest.raises(OSError, match="backup interrupted"):
        installer.install()
    assert shortcut.read_bytes() == b"original shortcut"
    assert snapshot(previous) == installer.originals


def test_installer_requires_same_payload_sibling_helper():
    entry = (SOURCE / "Install-K5VisionAlpha.ps1").read_text()
    assert '$transactionInstaller = Join-Path $PSScriptRoot "install_transaction.py"' in entry
    assert "$runtimeRequirements, $provisioner, $transactionInstaller)" in entry
    assert 'throw "Required alpha bootstrap file is missing: $path"' in entry
    assert "$transactionInstaller," in entry and '"--source", $PSScriptRoot' in entry
    assert "Invoke-WebRequest" not in entry


def test_one_file_bootstrap_is_still_on_pretransaction_payload():
    # This reachability regression deliberately guards the remaining delivery
    # blocker. A later reviewed payload-pin change must update this assertion.
    bootstrap = (ROOT / "Install-K5VisionAlpha.ps1").read_text()
    readme = (SOURCE / "README.md").read_text()
    assert '$K5Revision = "d531d50d479f46af6ceed324a7cc379745becb61"' in bootstrap
    assert "archive/$K5Revision.zip" in bootstrap
    assert '$sourceRoot = Join-Path $extractRoot ("K5-Vision-" + $K5Revision)' in bootstrap
    assert (
        '$installer = Join-Path $sourceRoot "scripts\\windows-alpha\\Install-K5VisionAlpha.ps1"'
        in bootstrap
    )
    assert "install_transaction.py" not in bootstrap
    assert "does **not** deliver this repair" in readme


@pytest.mark.parametrize("failure", ["write", "replace"])
def test_interrupted_first_journal_write_is_retry_safe(previous, monkeypatch, failure):
    installer = FakeInstaller(previous)
    before = snapshot(previous)
    dump = transaction.json.dump
    replace = transaction.os.replace

    def fail_write(state, stream):
        stream.write('{"format":')
        raise OSError("initial journal write interrupted")

    def fail_replace(source, target):
        raise OSError("initial journal replace interrupted")

    if failure == "write":
        monkeypatch.setattr(transaction.json, "dump", fail_write)
    else:
        monkeypatch.setattr(transaction.os, "replace", fail_replace)
    with pytest.raises(OSError, match=f"initial journal {failure} interrupted"):
        installer.install(skip_shortcut=True)
    assert snapshot(previous) == before
    assert sorted(path.name for path in installer.work.iterdir()) == ["transaction.tmp"]
    monkeypatch.setattr(transaction.json, "dump", dump)
    monkeypatch.setattr(transaction.os, "replace", replace)
    installer.install(skip_shortcut=True)
    assert (previous / ".venv" / "runtime").read_bytes() == b"new runtime"
    assert not installer.work.exists()


@pytest.fixture
def offline_bundle(tmp_path, monkeypatch):
    """Generated metadata/artifacts only; no package installation or wheel code runs."""
    import hashlib
    import zipfile
    from types import SimpleNamespace

    source_root = tmp_path / "payload"
    source = source_root / "scripts" / "windows-alpha"
    for name in transaction.PAYLOAD_FILES:
        target = source_root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        # Generate canonical LF fixtures even when the test checkout uses CRLF.
        # Production admission still requires the original exact payload bytes.
        target.write_bytes((ROOT / name).read_bytes().replace(b"\r\n", b"\n"))
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    versions = transaction.runtime_versions(
        source / "runtime-requirements.txt", target_platform="win32"
    )
    versions.update({"k5-vision": "0.1.0", "pip": "25.0.1"})

    def record(path):
        content = path.read_bytes()
        return {"size": len(content), "sha256": hashlib.sha256(content).hexdigest()}

    records = []
    runtime = b"# generated runtime identity fixture, never executed\n"
    for name, version in versions.items():
        escaped = name.replace("-", "_")
        filename = f"{escaped}-{version}-py3-none-any.whl"
        with zipfile.ZipFile(wheels / filename, "w") as archive:
            prefix = f"{escaped}-{version}.dist-info/"
            archive.writestr(
                prefix + "METADATA", f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
            )
            archive.writestr(prefix + "WHEEL", "Wheel-Version: 1.0\nTag: py3-none-any\n")
            archive.writestr(prefix + "RECORD", "")
            archive.writestr(
                "k5vision/cli.py" if name == "k5-vision" else f"{escaped}/__init__.py", runtime
            )
        records.append(
            {
                "filename": filename,
                "name": name,
                "version": version,
                "tags": ["py3-none-any"],
                **record(wheels / filename),
            }
        )
    pip = next(r for r in records if r["name"] == "pip")
    host = {
        "implementation": "cpython",
        "version": "3.12.10",
        "platform": "win_amd64",
        "executable_sha256": "a" * 64,
        "ensurepip_version": "25.0.1",
        "ensurepip_wheel_sha256": pip["sha256"],
    }
    monkeypatch.setattr(transaction, "_host_identity", lambda: dict(host))
    data = {
        "schema_version": transaction.WHEELHOUSE_FORMAT,
        "installer_revision": "b" * 40,
        "runtime_revision": "c" * 40,
        "python": dict(host),
        "installer_payload": {
            name: record(source_root / name) for name in transaction.PAYLOAD_FILES
        },
        "runtime_payload": {
            "k5vision/cli.py": {"size": len(runtime), "sha256": hashlib.sha256(runtime).hexdigest()}
        },
        "wheels": records,
    }
    manifest = tmp_path / "manifest.json"

    def write():
        manifest.write_text(json.dumps(data), encoding="utf-8")
        return {
            "wheelhouse": wheels,
            "wheelhouse_manifest": manifest,
            "wheelhouse_manifest_sha256": record(manifest)["sha256"],
        }

    def installer(root=None, **kwargs):
        return transaction.Installer(
            root or tmp_path / "installed",
            source,
            Path("powershell.exe"),
            "c" * 40,
            "1.28.7",
            **write(),
            **kwargs,
        )

    def edit_wheel(name, edit):
        entry = next(r for r in records if r["name"] == name)
        path = wheels / entry["filename"]
        with zipfile.ZipFile(path) as archive:
            files = {i.filename: archive.read(i) for i in archive.infolist()}
        edit(files)
        with zipfile.ZipFile(path, "w") as archive:
            for filename, content in files.items():
                archive.writestr(filename, content)
        entry.update(record(path))

    return SimpleNamespace(
        source=source,
        root=tmp_path / "installed",
        wheels=wheels,
        manifest=manifest,
        data=data,
        write=write,
        installer=installer,
        edit_wheel=edit_wheel,
        record=record,
    )


def test_offline_admission_copies_exact_closed_wheel_set_without_commands(offline_bundle):
    run = Mock()
    installer = offline_bundle.installer(run=run)
    installer.work.mkdir(parents=True)
    installer.prepare_wheels()
    assert installer.wheel_hashes() == {
        r["filename"]: r["sha256"] for r in offline_bundle.data["wheels"]
    }
    assert installer.offline.versions["pip"] == "25.0.1"
    assert installer.offline.versions["pyreadline3"] == "3.5.6"
    assert len(installer.offline.versions) == 30
    run.assert_not_called()


def test_offline_admission_refuses_previous_inventory_without_windows_pin(offline_bundle):
    records = offline_bundle.data["wheels"]
    missing = next(record for record in records if record["name"] == "pyreadline3")
    records.remove(missing)
    (offline_bundle.wheels / missing["filename"]).unlink()
    run = Mock()
    with pytest.raises(RuntimeError, match="no online fallback"):
        offline_bundle.installer(run=run)
    assert not offline_bundle.root.exists()
    run.assert_not_called()


def test_offline_admission_keeps_platform_pin_version_exact(offline_bundle):
    record = next(r for r in offline_bundle.data["wheels"] if r["name"] == "pyreadline3")
    record["version"] = "3.5.7"
    run = Mock()
    with pytest.raises(RuntimeError, match="no online fallback"):
        offline_bundle.installer(run=run)
    assert not offline_bundle.root.exists()
    run.assert_not_called()


@pytest.mark.parametrize("present", [(0,), (1,), (2,), (0, 1), (0, 2), (1, 2)])
def test_offline_partial_arguments_never_fall_back(offline_bundle, present):
    all_args = offline_bundle.write()
    kwargs = {key: value for i, (key, value) in enumerate(all_args.items()) if i in present}
    with pytest.raises(RuntimeError, match="no online fallback"):
        transaction.Installer(
            offline_bundle.root,
            offline_bundle.source,
            Path("powershell.exe"),
            "c" * 40,
            "1.28.7",
            **kwargs,
        )
    assert not offline_bundle.root.exists()


@pytest.mark.parametrize(
    "change",
    [
        "digest",
        "runtime",
        "host",
        "schema",
        "payload",
        "closure_missing",
        "closure_extra",
        "duplicate_name",
        "version",
        "tags",
        "size_bool",
        "pip_hash",
        "runtime_payload",
        "source_revision",
    ],
)
def test_offline_manifest_rejects_identity_and_closure_mismatch(offline_bundle, change):
    data = offline_bundle.data
    if change == "runtime":
        data["runtime_revision"] = "d" * 40
    elif change == "host":
        data["python"]["version"] = "3.12.99"
    elif change == "schema":
        data["schema_version"] = "unrecognized"
    elif change == "payload":
        data["installer_payload"][transaction.PAYLOAD_FILES[0]]["sha256"] = "0" * 64
    elif change == "closure_missing":
        data["wheels"].pop()
    elif change == "closure_extra":
        data["wheels"].append(dict(data["wheels"][0]))
    elif change == "duplicate_name":
        data["wheels"][1]["name"] = data["wheels"][0]["name"]
    elif change == "version":
        data["wheels"][0]["version"] = "99.0"
    elif change == "tags":
        data["wheels"][0]["tags"] = ["cp311-cp311-linux_x86_64"]
    elif change == "size_bool":
        data["wheels"][0]["size"] = True
    elif change == "pip_hash":
        next(r for r in data["wheels"] if r["name"] == "pip")["sha256"] = "0" * 64
    elif change == "runtime_payload":
        data["runtime_payload"]["k5vision/cli.py"]["sha256"] = "0" * 64
    elif change == "source_revision":
        data["installer_revision"] = "main"
    args = offline_bundle.write()
    if change == "digest":
        args["wheelhouse_manifest_sha256"] = "0" * 64
    with pytest.raises(RuntimeError, match="admission failed"):
        transaction.Installer(
            offline_bundle.root,
            offline_bundle.source,
            Path("powershell.exe"),
            "c" * 40,
            "1.28.7",
            **args,
        )
    assert not offline_bundle.root.exists()


@pytest.mark.parametrize("extra", ["surprise.whl", "unexpected.txt", "directory"])
def test_offline_directory_requires_exact_inventory(offline_bundle, extra):
    path = offline_bundle.wheels / extra
    path.mkdir() if extra == "directory" else path.write_bytes(b"unexpected")
    with pytest.raises(RuntimeError, match="admission failed"):
        offline_bundle.installer()
    assert not offline_bundle.root.exists()


@pytest.mark.parametrize(
    "kind", ["name", "version", "duplicate_name", "wheel_tag", "traversal", "extra_runtime"]
)
def test_offline_wheel_metadata_must_match_admitted_artifact(offline_bundle, kind):
    def edit(files):
        meta = "k5_vision-0.1.0.dist-info/METADATA"
        if kind == "name":
            files[meta] = files[meta].replace(b"Name: k5-vision", b"Name: other")
        elif kind == "version":
            files[meta] = files[meta].replace(b"Version: 0.1.0", b"Version: 9.9.9")
        elif kind == "duplicate_name":
            files[meta] += b"Name: k5-vision\n"
        elif kind == "wheel_tag":
            files["k5_vision-0.1.0.dist-info/WHEEL"] = (
                b"Wheel-Version: 1.0\nTag: cp311-cp311-win_amd64\n"
            )
        elif kind == "traversal":
            files["../escape.py"] = b"no"
        elif kind == "extra_runtime":
            files["unadmitted.py"] = b"no"

    offline_bundle.edit_wheel("k5-vision", edit)
    with pytest.raises(RuntimeError, match="admission failed"):
        offline_bundle.installer()


def test_offline_duplicate_json_keys_are_refused_even_with_matching_digest(offline_bundle):
    import hashlib

    args = offline_bundle.write()
    raw = offline_bundle.manifest.read_bytes().replace(
        b'{"schema_version":', b'{"schema_version":"duplicate", "schema_version":', 1
    )
    offline_bundle.manifest.write_bytes(raw)
    args["wheelhouse_manifest_sha256"] = hashlib.sha256(raw).hexdigest()
    with pytest.raises(RuntimeError, match="admission failed"):
        transaction.Installer(
            offline_bundle.root,
            offline_bundle.source,
            Path("powershell.exe"),
            "c" * 40,
            "1.28.7",
            **args,
        )


def test_offline_runtime_versions_cannot_be_redefined_by_supplied_manifest(offline_bundle):
    requirements = offline_bundle.source / "runtime-requirements.txt"
    requirements.write_text(requirements.read_text().replace("anyio==4.15.1", "anyio==4.15.2"))
    offline_bundle.data["installer_payload"]["scripts/windows-alpha/runtime-requirements.txt"] = (
        offline_bundle.record(requirements)
    )
    with pytest.raises(RuntimeError, match="admission failed"):
        offline_bundle.installer()


@pytest.mark.parametrize("change", ["wheel", "source", "copied_wheel"])
def test_offline_inputs_are_rechecked_before_use(offline_bundle, monkeypatch, change):
    installer = offline_bundle.installer(run=Mock())
    installer.work.mkdir(parents=True)
    if change == "wheel":
        next(offline_bundle.wheels.glob("*.whl")).write_bytes(b"tamper")
    elif change == "source":
        (offline_bundle.source / "Run-K5VisionAlpha.ps1").write_bytes(b"tamper")
    else:
        copy = transaction.shutil.copyfile

        def tamper(source, destination):
            copy(source, destination)
            Path(destination).write_bytes(b"tamper")

        monkeypatch.setattr(transaction.shutil, "copyfile", tamper)
    with pytest.raises(RuntimeError, match="admission failed"):
        installer.prepare_wheels()
    installer.run.assert_not_called()


def offline_command_fixture(bundle, root, fail=None):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        if "venv" in args:
            (Path(args[-1]) / "Scripts").mkdir(parents=True)
            (Path(args[-1]) / "Scripts" / "python.exe").write_bytes(b"not executable fixture")
        if fail and fail(args):
            raise subprocess.CalledProcessError(1, args)
        return subprocess.CompletedProcess(args, 0, "[]")

    return bundle.installer(root=root, run=run), calls


@pytest.mark.parametrize("failure", [None, "pip_check", "installed_closure", "active_preflight"])
def test_offline_real_transaction_uses_mocked_closed_commands_and_rolls_back(
    offline_bundle, previous, failure
):
    before = snapshot(previous)

    def fail(args):
        if failure == "pip_check":
            return args[-3:] == ["-m", "pip", "check"]
        if failure == "installed_closure":
            return "Installed wheel closure mismatch" in args[-1]
        return failure == "active_preflight" and args[-2:] == ["-InstallRoot", str(previous)]

    installer, calls = offline_command_fixture(offline_bundle, previous, fail if failure else None)
    if failure:
        with pytest.raises(subprocess.CalledProcessError):
            installer.install(skip_shortcut=True)
        assert snapshot(previous) == before
    else:
        installer.install(skip_shortcut=True)
        assert (previous / "config/private.json").read_bytes() == before["config/private.json"]
        assert (previous / "k5-revision.txt").read_text() == "c" * 40
    assert not installer.work.exists()
    assert all(
        "wheel" not in args and not any("https://" in arg for arg in args) for args, _ in calls
    )
    installs = [args for args, _ in calls if "install" in args]
    assert installs and all("--no-index" in args and "--no-deps" in args for args in installs)
    assert all(kwargs["env"]["PIP_NO_INDEX"] == "1" for _, kwargs in calls)
    assert all("provision-stage03-gstreamer.ps1" not in str(args) for args, _ in calls)


def test_offline_fresh_install_never_provisions_shared_runtime(offline_bundle):
    installer, calls = offline_command_fixture(offline_bundle, offline_bundle.root)
    installer.install(skip_shortcut=True)
    assert all("provision-stage03-gstreamer.ps1" not in str(args) for args, _ in calls)
    assert any("Installed wheel closure mismatch" in args[-1] for args, _ in calls)


def test_offline_wrapper_forwards_all_inputs_without_default_pin_change():
    text = (SOURCE / "Install-K5VisionAlpha.ps1").read_text()
    for parameter in ("Wheelhouse", "WheelhouseManifest", "WheelhouseManifestSha256"):
        assert f'[string]${parameter} = ""' in text
    assert '"--wheelhouse", $Wheelhouse' in text
    assert '"--wheelhouse-manifest", $WheelhouseManifest' in text
    assert '"--wheelhouse-manifest-sha256", $WheelhouseManifestSha256' in text
    assert "$PSBoundParameters.ContainsKey($name)" in text
    assert "if ($offlineCount -ne 0 -and $offlineCount -ne 3)" in text
    assert '$K5Revision = "d531d50d479f46af6ceed324a7cc379745becb61"' in text


def test_offline_bootstrap_isolation_precedes_every_python_execution():
    text = (SOURCE / "Install-K5VisionAlpha.ps1").read_text()
    assert 'if ($offlineCount -eq 3) { $pythonIsolation = @("-I", "-S", "-B") }' in text
    assert "& $py.Source -3.12 @pythonIsolation -c" in text
    assert "& $candidate.Source @pythonIsolation -c" in text
    assert "& $pythonCommand @pythonPrefixArgs @pythonIsolation @arguments" in text
    assert text.index("$pythonIsolation = @()") < text.index("$py = Get-Command")


def test_offline_venv_creation_excludes_ambient_site_code(offline_bundle):
    installer, calls = offline_command_fixture(offline_bundle, offline_bundle.root)
    installer.install(skip_shortcut=True)
    creates = [args for args, _ in calls if "venv" in args]
    assert creates and all(args[1:4] == ["-I", "-S", "-B"] for args in creates)
    # The installed venv needs site initialization on 3.12, but no user site.
    pip_calls = [args for args, _ in calls if "pip" in args]
    assert pip_calls and all(args[1:3] == ["-I", "-B"] and "-S" not in args for args in pip_calls)


@pytest.mark.parametrize("component", ["..", "bad.", "bad ", "NUL", "COM1.txt", "aux"])
def test_offline_install_root_refuses_windows_path_aliases_before_mutation(
    offline_bundle, component
):
    root = offline_bundle.root / component / "child"
    with pytest.raises(RuntimeError, match="admission failed"):
        offline_bundle.installer(root=root)
    assert not offline_bundle.root.exists()


@pytest.mark.parametrize(
    "name",
    [
        "k5vision/CLI.py",
        "k5vision/cli.py.",
        "k5vision/NUL.py",
        "k5vision/COM1",
        "k5vision/cli.py ",
        "k5vision//other.py",
    ],
)
def test_offline_wheel_rejects_windows_aliases_even_in_trusted_payload(offline_bundle, name):
    import hashlib

    content = b"different generated fixture"
    offline_bundle.edit_wheel("k5-vision", lambda files: files.update({name: content}))
    offline_bundle.data["runtime_payload"][name] = {
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }
    with pytest.raises(RuntimeError, match="admission failed"):
        offline_bundle.installer()


def test_offline_wheel_rejects_file_directory_collision(offline_bundle):
    offline_bundle.edit_wheel(
        "anyio", lambda files: files.update({"anyio": b"file collides with directory"})
    )
    with pytest.raises(RuntimeError, match="admission failed"):
        offline_bundle.installer()


@pytest.mark.parametrize(
    "tag",
    [
        "py3.py3-none-any",
        "a" * 129 + "-none-any",
        ".".join(f"py{i}" for i in range(9)) + "-none-any",
        "a.b.c.d.e-f.g.h.i.j-k.l.m",
    ],
)
def test_offline_tag_expansion_is_bounded_before_cartesian_product(monkeypatch, tag):
    product = Mock(side_effect=AssertionError("unbounded expansion"))
    monkeypatch.setattr(transaction.itertools, "product", product)
    with pytest.raises(RuntimeError, match="admission failed"):
        transaction._tags(tag)
    product.assert_not_called()


def test_offline_wheel_refuses_excessive_tag_headers(offline_bundle):
    offline_bundle.edit_wheel(
        "anyio",
        lambda files: files.update(
            {
                "anyio-4.15.1.dist-info/WHEEL": b"Wheel-Version: 1.0\n"
                + b"Tag: py3-none-any\n" * 65,
            }
        ),
    )
    with pytest.raises(RuntimeError, match="admission failed"):
        offline_bundle.installer()


@pytest.mark.parametrize("value", [r"\\server\share\wheels", r"\\?\C:\wheels"])
def test_offline_network_and_device_paths_are_refused_before_filesystem_access(monkeypatch, value):
    from pathlib import PureWindowsPath

    inspect = Mock()
    monkeypatch.setattr(transaction, "_plain_ancestors", inspect)
    with pytest.raises(RuntimeError, match="admission failed"):
        transaction._offline_path(PureWindowsPath(value))
    inspect.assert_not_called()


@pytest.mark.parametrize("path_class", ["PureWindowsPath", "PurePosixPath"])
def test_snapshot_nested_keys_are_platform_independent(path_class):
    import pathlib

    relative = getattr(pathlib, path_class)("config") / "private.json"
    entry = Mock()
    entry.is_file.return_value = True
    entry.parts = relative.parts
    entry.name = relative.name
    entry.relative_to.return_value = relative
    entry.read_bytes.return_value = b"generated configuration"
    root = Mock()
    root.exists.return_value = True
    root.rglob.return_value = [entry]
    assert snapshot(root) == {"config/private.json": b"generated configuration"}


OFFLINE_REFUSAL = "Offline wheelhouse admission failed; no online fallback is permitted."


def assert_offline_contract(action, contract, *, expected=None, observed=None):
    with pytest.raises(transaction.OfflineAdmissionError) as refusal:
        action()
    error = refusal.value
    assert isinstance(error, RuntimeError)
    assert str(error) == OFFLINE_REFUSAL
    assert error.args == (OFFLINE_REFUSAL,)
    assert vars(error) == {"contract": contract, "expected": expected, "observed": observed}
    assert contract in transaction.OFFLINE_ADMISSION_CONTRACTS
    return error


@pytest.mark.parametrize("scalar", [None, False, True, 0, 1, 2**31 - 1])
def test_offline_diagnostic_contract_keeps_legacy_message_and_bounded_scalars(scalar, capsys):
    assert_offline_contract(
        lambda: transaction._admit(False, "wheel-count", expected=scalar, observed=scalar),
        "wheel-count",
        expected=scalar,
        observed=scalar,
    )
    assert transaction._admit(True, "wheel-count", expected=scalar, observed=scalar) is None
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("scalar", ["private path or metadata", -1, 2**31, 1.0, [], {}])
def test_offline_diagnostic_contract_discards_unbounded_or_identifying_scalars(scalar):
    assert_offline_contract(
        lambda: transaction._admit(False, "metadata-size", expected=scalar, observed=scalar),
        "metadata-size",
    )


@pytest.mark.parametrize("contract", ["private wheel name", "", None, [], 1])
def test_offline_diagnostic_contract_discards_unknown_labels_and_their_scalars(contract):
    assert_offline_contract(
        lambda: transaction._admit(False, contract, expected=1, observed=2), "admission"
    )


def test_offline_diagnostic_contract_does_not_coerce_external_objects():
    class External:
        def __repr__(self):
            raise AssertionError("external diagnostic data must not be formatted")

        __str__ = __repr__

    class IntSubclass(int):
        pass

    class StrSubclass(str):
        pass

    assert_offline_contract(lambda: transaction._admit(False, External()), "admission")
    assert_offline_contract(
        lambda: transaction._admit(False, StrSubclass("wheel-count")), "admission"
    )
    assert_offline_contract(
        lambda: transaction._admit(
            False, "wheel-count", expected=External(), observed=IntSubclass(1)
        ),
        "wheel-count",
    )


@pytest.mark.parametrize(
    ("filename", "contract"),
    [
        ("anyio.whl", "wheel-filename-structure"),
        ("any.io-4.15.1-py3-none-any.whl", "wheel-filename-name"),
        ("private_name-4.15.1-py3-none-any.whl", "wheel-filename-name-match"),
        ("anyio-9.9.9-py3-none-any.whl", "wheel-filename-version"),
        ("anyio-4.15.1-privatebuild-py3-none-any.whl", "wheel-filename-build"),
        ("anyio-4.15.1-PY3-none-any.whl", "tag-format"),
    ],
)
def test_offline_filename_diagnostics_are_fixed_labels_without_opening_files(filename, contract):
    wheelhouse = object.__new__(transaction.OfflineWheelhouse)
    record = {"name": "anyio", "version": "4.15.1", "tags": ["py3-none-any"]}
    assert_offline_contract(lambda: wheelhouse.verify_metadata(Path(filename), record), contract)


@pytest.mark.parametrize(
    ("tags", "contract"),
    [
        (None, "wheel-tags-record"),
        ([1], "wheel-tags-record"),
        (["private_tag"], "wheel-tags-match"),
    ],
)
def test_offline_record_tag_diagnostics_do_not_echo_tags(tags, contract):
    wheelhouse = object.__new__(transaction.OfflineWheelhouse)
    record = {"name": "anyio", "version": "4.15.1", "tags": tags}
    assert_offline_contract(
        lambda: wheelhouse.verify_metadata(Path("anyio-4.15.1-py3-none-any.whl"), record), contract
    )


def test_offline_unsupported_filename_tags_keep_admission_closed():
    wheelhouse = object.__new__(transaction.OfflineWheelhouse)
    record = {"name": "anyio", "version": "4.15.1", "tags": ["cp311-cp311-win_amd64"]}
    assert_offline_contract(
        lambda: wheelhouse.verify_metadata(Path("anyio-4.15.1-cp311-cp311-win_amd64.whl"), record),
        "wheel-tags-supported",
    )


@pytest.mark.parametrize(
    ("member", "content", "contract", "expected", "observed"),
    [
        ("METADATA", None, "metadata-present", None, None),
        ("WHEEL", None, "wheel-metadata-present", None, None),
        ("METADATA", b"x" * 65537, "metadata-size", 65536, 65537),
        ("WHEEL", b"x" * 65537, "wheel-metadata-size", 65536, 65537),
        ("METADATA", b"malformed private metadata\n", "metadata-name-format", None, None),
        ("METADATA", b"Name: private-name\nVersion: 4.15.1\n", "metadata-name", None, None),
        ("METADATA", b"Name: \xff\nVersion: 4.15.1\n", "metadata-name-format", None, None),
        ("METADATA", b"Name: anyio\nName: anyio\n", "metadata-name-format", None, None),
        ("METADATA", b"Name: anyio\n", "metadata-version", None, None),
        ("METADATA", b"Name: anyio\nVersion: private-version\n", "metadata-version", None, None),
        ("WHEEL", b"Tag: py3-none-any\n", "wheel-metadata-version", None, None),
        ("WHEEL", b"Wheel-Version: 1.0\n", "wheel-metadata-tag-count", 64, 0),
        ("WHEEL", b"Wheel-Version: 1.0\nTag: \xff\n", "tag-format", None, None),
        (
            "WHEEL",
            b"Wheel-Version: 1.0\nTag: py312-none-any\n",
            "wheel-metadata-tags",
            None,
            None,
        ),
        (
            "WHEEL",
            b"Wheel-Version: 1.0\n" + b"Tag: py3-none-any\n" * 65,
            "wheel-metadata-tag-count",
            64,
            65,
        ),
    ],
)
def test_offline_metadata_diagnostics_refuse_missing_malformed_and_oversize_headers(
    offline_bundle, member, content, contract, expected, observed, capsys
):
    def edit(files):
        name = "anyio-4.15.1.dist-info/" + member
        if content is None:
            del files[name]
        else:
            files[name] = content

    offline_bundle.edit_wheel("anyio", edit)
    run = Mock()
    error = assert_offline_contract(
        lambda: offline_bundle.installer(run=run), contract, expected=expected, observed=observed
    )
    assert "private" not in repr(error) + repr(vars(error))
    assert not offline_bundle.root.exists()
    run.assert_not_called()
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize(
    "member",
    [
        "foreign-1.0.dist-info/METADATA",
        "anyio/vendor/foreign-1.0.dist-info/METADATA",
        "anyio/vendor/anyio-4.15.1.dist-info/WHEEL",
        "FOREIGN-4.15.1.DIST-INFO/METADATA",
    ],
)
def test_offline_nested_or_foreign_metadata_stays_rejected_with_fixed_diagnostic(
    offline_bundle, member
):
    offline_bundle.edit_wheel("anyio", lambda files: files.update({member: b"private metadata"}))
    assert_offline_contract(offline_bundle.installer, "archive-foreign-metadata")
    assert not offline_bundle.root.exists()


def test_offline_own_nested_metadata_files_remain_admitted(offline_bundle):
    offline_bundle.edit_wheel(
        "anyio",
        lambda files: files.update(
            {"anyio-4.15.1.dist-info/licenses/LICENSE": b"generated license"}
        ),
    )
    run = Mock()
    installer = offline_bundle.installer(run=run)
    assert installer.offline.versions["anyio"] == "4.15.1"
    assert not offline_bundle.root.exists()
    run.assert_not_called()


@pytest.mark.parametrize(
    ("member", "contract"),
    [
        ("/private-path", "archive-member-path"),
        ("private:path", "archive-member-path"),
        (r"anyio\private-path", "archive-member-path"),
        ("anyio/../private-path", "archive-member-component"),
        ("anyio/NUL.txt", "archive-member-component"),
        ("ANYIO/__init__.py", "archive-member-collision"),
        ("anyio", "archive-file-directory-collision"),
    ],
)
def test_offline_archive_member_diagnostics_are_nonidentifying(offline_bundle, member, contract):
    offline_bundle.edit_wheel("anyio", lambda files: files.update({member: b"generated fixture"}))
    assert_offline_contract(offline_bundle.installer, contract)


@pytest.mark.parametrize(
    ("mutation", "contract", "expected", "observed"),
    [
        ("symlink", "archive-member-symlink", None, None),
        ("encrypted", "archive-member-encrypted", None, None),
        ("duplicate", "archive-duplicate-member", None, None),
        ("count", "archive-member-count", 10000, 10001),
        ("expanded", "archive-expanded-size", 512 * 1024 * 1024, 512 * 1024 * 1024 + 1),
    ],
)
def test_offline_archive_resource_and_entry_guards_keep_fixed_diagnostics(
    offline_bundle, monkeypatch, mutation, contract, expected, observed
):
    import stat

    # Mutate only generated ZipInfo fixtures; no oversized archive is allocated.
    original = transaction.zipfile.ZipFile.infolist

    def infolist(archive):
        entries = original(archive)
        if mutation == "symlink":
            entries[0].external_attr = (stat.S_IFLNK | 0o777) << 16
        elif mutation == "encrypted":
            entries[0].flag_bits |= 1
        elif mutation == "duplicate":
            entries.append(entries[0])
        elif mutation == "count":
            entries = [entries[0]] * 10001
        elif mutation == "expanded":
            for item in entries:
                item.file_size = 0
            entries[0].file_size = 512 * 1024 * 1024 + 1
        return entries

    monkeypatch.setattr(transaction.zipfile.ZipFile, "infolist", infolist)
    assert_offline_contract(
        offline_bundle.installer, contract, expected=expected, observed=observed
    )


def test_offline_malformed_archive_has_a_fixed_refusal_without_raw_exception(offline_bundle):
    record = next(item for item in offline_bundle.data["wheels"] if item["name"] == "anyio")
    path = offline_bundle.wheels / record["filename"]
    path.write_bytes(b"private malformed archive")
    record.update(offline_bundle.record(path))
    assert_offline_contract(offline_bundle.installer, "archive-open")


@pytest.mark.parametrize("error_class", [transaction.zipfile.BadZipFile, transaction.zlib.error])
def test_offline_archive_read_error_suppresses_external_exception_text(
    offline_bundle, monkeypatch, error_class
):
    read = Mock(side_effect=error_class("private path and archive metadata"))
    monkeypatch.setattr(transaction.zipfile.ZipFile, "read", read)
    error = assert_offline_contract(offline_bundle.installer, "archive-read")
    assert error.__suppress_context__


@pytest.mark.parametrize(
    ("field", "value", "contract"),
    [
        ("implementation", "private-implementation", "host-implementation"),
        ("platform", "private-platform", "host-platform"),
        ("version", "3.11.0", "host-version"),
    ],
)
def test_offline_host_diagnostics_never_default_failed_identity(
    offline_bundle, monkeypatch, field, value, contract
):
    host = dict(offline_bundle.data["python"])
    host[field] = value
    offline_bundle.data["python"] = dict(host)
    monkeypatch.setattr(transaction, "_host_identity", lambda: host)
    assert_offline_contract(offline_bundle.installer, contract)


def test_offline_host_mismatch_is_labeled_without_identity_contents(offline_bundle):
    offline_bundle.data["python"]["version"] = "private-version"
    assert_offline_contract(offline_bundle.installer, "host-identity")


def test_offline_requirements_hash_and_wheel_count_have_specific_diagnostics(offline_bundle):
    offline_bundle.data["wheels"].pop()
    assert_offline_contract(offline_bundle.installer, "wheel-count", expected=30, observed=29)
    requirements = offline_bundle.source / "runtime-requirements.txt"
    requirements.write_bytes(b"private requirements")
    assert_offline_contract(lambda: transaction.runtime_versions(requirements), "requirements-hash")


def test_offline_requirements_count_is_labeled_without_pin_data(tmp_path, monkeypatch):
    import hashlib

    path = tmp_path / "generated-requirements.txt"
    raw = b"anyio==4.15.1\n"
    path.write_bytes(raw)
    # Reach the existing count predicate with a generated hash-bound fixture.
    monkeypatch.setattr(transaction, "REQUIREMENTS_SHA256", hashlib.sha256(raw).hexdigest())
    assert_offline_contract(
        lambda: transaction.runtime_versions(path), "requirements-count", expected=28, observed=1
    )


def test_offline_path_refusal_suppresses_link_and_access_details(tmp_path, monkeypatch):
    inspect = Mock(side_effect=RuntimeError("private link path"))
    monkeypatch.setattr(transaction, "_plain_ancestors", inspect)
    assert_offline_contract(lambda: transaction._offline_path(tmp_path), "path-link")
    inspect.side_effect = OSError("private inaccessible path")
    assert_offline_contract(lambda: transaction._offline_path(tmp_path), "path-access")


@pytest.mark.parametrize(
    ("change", "contract"),
    [
        ("member", "runtime-member-scope"),
        ("fields", "runtime-payload-fields"),
        ("members", "runtime-payload-members"),
        ("record", "runtime-file-record"),
        ("size", "runtime-file-size"),
        ("hash", "runtime-file-hash"),
    ],
)
def test_offline_runtime_payload_refusals_are_labeled_without_content(
    offline_bundle, change, contract
):
    payload = offline_bundle.data["runtime_payload"]
    if change == "member":
        offline_bundle.edit_wheel(
            "k5-vision", lambda files: files.update({"private.py": b"private"})
        )
    elif change == "fields":
        offline_bundle.data["runtime_payload"] = []
    elif change == "members":
        payload["k5vision/private.py"] = dict(payload["k5vision/cli.py"])
    elif change == "record":
        payload["k5vision/cli.py"] = {}
    elif change == "size":
        payload["k5vision/cli.py"]["size"] = True
    else:
        payload["k5vision/cli.py"]["sha256"] = "private-hash"
    assert_offline_contract(offline_bundle.installer, contract)
    assert not offline_bundle.root.exists()


@pytest.mark.parametrize("content", [b"{private malformed JSON", b"\xff"])
def test_offline_manifest_parse_refusal_is_labeled_without_raw_data(offline_bundle, content):
    args = offline_bundle.write()
    offline_bundle.manifest.write_bytes(content)
    args["wheelhouse_manifest_sha256"] = offline_bundle.record(offline_bundle.manifest)["sha256"]
    error = assert_offline_contract(
        lambda: transaction.Installer(
            offline_bundle.root,
            offline_bundle.source,
            Path("powershell.exe"),
            "c" * 40,
            "1.28.7",
            **args,
        ),
        "manifest-json",
    )
    assert error.__suppress_context__
    assert not offline_bundle.root.exists()


def test_offline_host_venv_refusals_precede_bootstrap_identity(tmp_path, monkeypatch):
    version = Mock(side_effect=AssertionError("must not query ensurepip in refused venv"))
    monkeypatch.setattr(transaction.ensurepip, "version", version)
    monkeypatch.setattr(transaction.sys, "prefix", "generated-prefix")
    monkeypatch.setattr(transaction.sys, "base_prefix", "generated-base-prefix")
    assert_offline_contract(transaction._host_identity, "host-base-prefix")
    monkeypatch.setattr(transaction.sys, "prefix", "generated-base-prefix")
    executable = tmp_path / "generated-venv" / "Scripts" / "python.exe"
    executable.parent.mkdir(parents=True)
    (executable.parent.parent / "pyvenv.cfg").write_text("generated fixture")
    monkeypatch.setattr(transaction.sys, "executable", str(executable))
    assert_offline_contract(transaction._host_identity, "host-no-venv")
    version.assert_not_called()
