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
        str(path.relative_to(root)): path.read_bytes()
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
