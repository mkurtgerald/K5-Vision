"""Recoverable, per-user Alpha upgrades; no runtime process is ever terminated.

Only the fixed installer-owned paths below participate. The candidate is verified
before activation. Virtual environments are rebuilt at their final path (Python
venvs and their console entry points are not relocatable).
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

FILES = (
    "Test-K5VisionAlpha.ps1",
    "Start-K5VisionAlpha.ps1",
    "Run-K5VisionAlpha.ps1",
    "gstreamer-version.txt",
    "k5-revision.txt",
)
MANAGED = (".venv", *FILES)
WORKSPACE = ".k5-alpha-upgrade"
LOCK = ".k5-alpha-install.lock"
FORMAT = "k5-alpha-upgrade-v1"
RUNTIME_PROBE = (
    "from k5vision.operator_runtime import LOCAL_TEST_SOURCE_ENV; "
    "raise SystemExit(0 if LOCAL_TEST_SOURCE_ENV == 'K5_LOCAL_TEST_RTSP_SOURCE' else 1)"
)


def _plain(path: Path) -> None:
    """Do not traverse symlinks/junctions, including dangling ones."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise RuntimeError(f"Installer path is a link or reparse point: {path}")


def _plain_ancestors(path: Path) -> None:
    for entry in (path, *path.parents):
        _plain(entry)


def _remove_owned(path: Path) -> None:
    _plain(path)
    if path.is_dir():
        # Do not follow links inside a partially created/externally modified tree.
        for directory, dirs, files in os.walk(path, followlinks=False):
            for name in (*dirs, *files):
                _plain(Path(directory) / name)
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


@contextlib.contextmanager
def install_lock(root: Path) -> Iterator[None]:
    _plain_ancestors(root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / LOCK
    _plain(path)
    with path.open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("Another K5 Alpha installation is in progress.") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _ps_literal(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


class Installer:
    def __init__(
        self,
        root: Path,
        source: Path,
        host: Path,
        revision: str,
        gstreamer: str,
        shortcut: Path | None = None,
        *,
        run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ) -> None:
        self.root = root.absolute()
        self.source = source.absolute()
        self.host = host
        self.revision = revision.lower()
        self.gstreamer = gstreamer
        self.shortcut = shortcut.absolute() if shortcut else None
        self.run = run
        self.work = self.root / WORKSPACE
        self.stage = self.work / "candidate"
        self.backup = self.work / "backup"
        self.wheels = self.work / "wheels"
        self.journal = self.work / "transaction.json"

    def command(self, args: list[str | Path], *, capture: bool = False) -> str:
        # Ambient pip/Python overrides must not redirect writes out of the
        # transaction or import a checkout instead of the installed candidate.
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.upper().startswith("PIP_")
            and key.upper() not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")
        }
        environment["PIP_CONFIG_FILE"] = os.devnull
        result = self.run(
            [str(arg) for arg in args],
            check=True,
            timeout=900,
            text=True,
            encoding="utf-8" if capture else None,
            env=environment,
            stdout=subprocess.PIPE if capture else None,
        )
        return result.stdout or ""

    def powershell(self, command: str, *, capture: bool = False) -> str:
        if capture:
            command = (
                "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); " + command
            )
        return self.command(
            [self.host, "-NoProfile", "-NonInteractive", "-Command", command], capture=capture
        )

    def assert_idle(self) -> None:
        if not (self.root / ".venv").exists() and not (self.backup / ".venv").exists():
            return
        # An inaccessible executable path means ownership cannot be established.
        # Refuse the upgrade; never infer ownership or kill an arbitrary PID.
        output = self.powershell(
            "$ErrorActionPreference = 'Stop'; "
            "@(Get-CimInstance Win32_Process "
            "-Filter \"Name = 'python.exe' OR Name = 'pythonw.exe'\" "
            "-ErrorAction Stop | Select-Object ProcessId, ExecutablePath) | "
            "ConvertTo-Json -Compress",
            capture=True,
        )
        processes = json.loads(output) if output.strip() else []
        if isinstance(processes, dict):
            processes = [processes]
        if not isinstance(processes, list):
            raise RuntimeError("Could not verify K5 runtime process ownership.")
        executables = {
            os.path.normcase(os.path.abspath(venv / "Scripts" / exe))
            for venv in (self.root / ".venv", self.backup / ".venv")
            for exe in ("python.exe", "pythonw.exe")
        }
        for process in processes:
            executable = process.get("ExecutablePath") if isinstance(process, dict) else None
            if not isinstance(executable, str) or not executable.strip():
                raise RuntimeError(
                    "Cannot identify a Python process. Close it and retry the upgrade."
                )
            if os.path.normcase(os.path.abspath(executable)) in executables:
                raise RuntimeError("K5 Alpha is running. Close its windows and retry the upgrade.")

    def save(self, state: dict) -> None:
        _plain(self.journal)
        temporary = self.work / "transaction.tmp"
        _plain(temporary)
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(state, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.journal)

    def targets(self, state: dict) -> dict[str, Path]:
        targets = {name: self.root / name for name in MANAGED}
        shortcut = state.get("shortcut")
        if shortcut is not None:
            if not isinstance(shortcut, str) or self.shortcut is None:
                raise RuntimeError("Upgrade recovery requires the original desktop shortcut path.")
            if Path(shortcut) != self.shortcut:
                raise RuntimeError("Upgrade recovery shortcut path does not match this desktop.")
            targets["desktop-shortcut.lnk"] = self.shortcut
        return targets

    def recover(self) -> None:
        _plain(self.work)
        if not self.work.exists():
            return
        _plain(self.journal)
        if not self.journal.is_file():
            # An interruption between the final journal unlink and rmdir (or
            # before the first journal write) can leave an empty directory only.
            if self.work.is_dir():
                entries = list(self.work.iterdir())
                temporary = self.work / "transaction.tmp"
                if entries == [temporary]:
                    # Only first-journal construction can leave this exact
                    # residue: backup/staging do not exist until save returns.
                    # It may contain a partial JSON write; never adopt a link.
                    _plain(temporary)
                    if temporary.is_file():
                        temporary.unlink()
                        entries = []
                if not entries:
                    self.work.rmdir()
                    return
            raise RuntimeError(f"Unrecognized upgrade workspace; left unchanged: {self.work}")
        state = json.loads(self.journal.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or state.get("format") != FORMAT:
            raise RuntimeError("Unrecognized upgrade journal; no installed files were changed.")
        phase = state.get("phase")
        targets = self.targets(state)
        if phase not in ("preparing", "activating", "committed"):
            raise RuntimeError("Invalid upgrade phase; no installed files were changed.")
        if phase == "activating":
            originals = state.get("originals")
            if (
                not isinstance(originals, dict)
                or originals.keys() != targets.keys()
                or any(type(value) is not bool for value in originals.values())
            ):
                raise RuntimeError("Invalid upgrade recovery manifest; installed files unchanged.")
            self.assert_idle()
            for name, target in reversed(list(targets.items())):
                _plain_ancestors(target)
                backup = self.backup / name
                _plain_ancestors(backup)
                if backup.exists():
                    if name == "desktop-shortcut.lnk":
                        # Desktop may be on a different filesystem. Keep the
                        # backup until all recovery/cleanup succeeds, so a
                        # partial copy can be retried without losing old bytes.
                        shutil.copy2(backup, target)
                    else:
                        _remove_owned(target)
                        backup.rename(target)
                elif not originals[name]:
                    _remove_owned(target)
                elif not target.exists():
                    raise RuntimeError(f"Original installer path is missing: {target}")
        elif phase == "committed":
            self.assert_idle()
        # The journal must outlive every potentially locked backup/staging
        # entry. Recursive removal of work itself could delete it first.
        for child in self.work.iterdir():
            if child != self.journal:
                _remove_owned(child)
        self.journal.unlink()
        self.work.rmdir()

    def materialize_files(self, destination: Path) -> None:
        if destination == self.root:
            # Activate the exact verified candidate scripts/records, not a second
            # read of source files that could have changed during staging.
            for name in FILES:
                shutil.copyfile(self.stage / name, destination / name)
            return
        for name in FILES[:3]:
            shutil.copyfile(self.source / name, destination / name)
        (destination / "gstreamer-version.txt").write_text(self.gstreamer, encoding="ascii")
        (destination / "k5-revision.txt").write_text(self.revision, encoding="ascii")

    def prepare_wheels(self) -> None:
        bootstrap = self.work / "builder"
        self.command([sys.executable, "-m", "venv", bootstrap])
        python = bootstrap / "Scripts" / "python.exe"
        self.wheels.mkdir()
        self.command(
            [
                python,
                "-m",
                "pip",
                "wheel",
                "--wheel-dir",
                self.wheels,
                "--requirement",
                self.source / "runtime-requirements.txt",
                "pip",
            ]
        )
        package_uri = f"https://github.com/mkurtgerald/K5-Vision/archive/{self.revision}.zip"
        self.command(
            [python, "-m", "pip", "wheel", "--no-deps", "--wheel-dir", self.wheels, package_uri]
        )
        if len(list(self.wheels.glob("k5_vision-*.whl"))) != 1:
            raise RuntimeError("Expected exactly one reviewed K5 wheel.")

    def wheel_hashes(self) -> dict[str, str]:
        hashes = {}
        for path in sorted(self.wheels.glob("*.whl")):
            _plain(path)
            with path.open("rb") as stream:
                hashes[path.name] = hashlib.file_digest(stream, "sha256").hexdigest()
        return hashes

    def install_runtime(self, destination: Path) -> None:
        destination.mkdir(exist_ok=True)
        venv = destination / ".venv"
        self.command([sys.executable, "-m", "venv", venv])
        python = venv / "Scripts" / "python.exe"
        self.command(
            [
                python,
                "-m",
                "pip",
                "install",
                "--no-index",
                "--no-deps",
                "--force-reinstall",
                *sorted(self.wheels.glob("*.whl")),
            ]
        )
        self.command([python, "-m", "pip", "check"])
        self.command([python, "-m", "k5vision.cli", "--version"])
        self.command([python, "-c", RUNTIME_PROBE])
        self.materialize_files(destination)

    def preflight(self, destination: Path) -> None:
        self.command(
            [
                self.host,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                destination / "Test-K5VisionAlpha.ps1",
                "-InstallRoot",
                destination,
            ]
        )

    def stage_shortcut(self) -> None:
        arguments = (
            '-NoProfile -ExecutionPolicy Bypass -NoExit -File "'
            + str(self.root / "Start-K5VisionAlpha.ps1")
            + '"'
        )
        self.powershell(
            "$ErrorActionPreference = 'Stop'; "
            "$wsh = New-Object -ComObject WScript.Shell; "
            f"$shortcut = $wsh.CreateShortcut({_ps_literal(self.stage / 'desktop-shortcut.lnk')}); "
            f"$shortcut.TargetPath = {_ps_literal(self.host)}; "
            f"$shortcut.Arguments = {_ps_literal(arguments)}; "
            f"$shortcut.WorkingDirectory = {_ps_literal(self.root)}; "
            "$shortcut.Description = 'K5 Vision Windows Alpha'; $shortcut.Save()"
        )
        if not (self.stage / "desktop-shortcut.lnk").is_file():
            raise RuntimeError("Desktop shortcut staging failed.")

    def install(self, *, skip_shortcut: bool = False) -> None:
        with install_lock(self.root):
            self.recover()
            self.assert_idle()
            self.work.mkdir()
            state = {"format": FORMAT, "phase": "preparing", "shortcut": None}
            self.save(state)
            try:
                self.backup.mkdir()
                # Never change the shared GStreamer runtime during an upgrade.
                # A missing/different requested version fails candidate preflight.
                if not any((self.root / name).exists() for name in MANAGED):
                    self.command(
                        [
                            self.host,
                            "-NoProfile",
                            "-NonInteractive",
                            "-ExecutionPolicy",
                            "Bypass",
                            "-File",
                            self.source.parent / "provision-stage03-gstreamer.ps1",
                            "-Version",
                            self.gstreamer,
                        ]
                    )
                self.prepare_wheels()
                hashes = self.wheel_hashes()
                self.install_runtime(self.stage)
                self.preflight(self.stage)
                if not skip_shortcut:
                    if self.shortcut is None:
                        raise RuntimeError("Desktop shortcut path is unavailable.")
                    self.stage_shortcut()
                    state["shortcut"] = str(self.shortcut)
                if not hashes or self.wheel_hashes() != hashes:
                    raise RuntimeError("Staged runtime wheels changed during verification.")
                targets = self.targets(state)
                for target in targets.values():
                    _plain_ancestors(target)
                self.assert_idle()
                state["originals"] = {name: target.exists() for name, target in targets.items()}
                state["phase"] = "activating"
                self.save(state)
                for name, target in targets.items():
                    if state["originals"][name]:
                        if name == "desktop-shortcut.lnk":
                            # Copy to an unadmitted temporary name first: a
                            # failed partial copy is never a valid old backup.
                            temporary = self.backup / "shortcut.tmp"
                            shutil.copy2(target, temporary)
                            temporary.rename(self.backup / name)
                        else:
                            target.rename(self.backup / name)
                # Reinstall exclusively from verified local wheels at the final path.
                self.install_runtime(self.root)
                self.preflight(self.root)
                if not skip_shortcut:
                    shutil.copyfile(self.stage / "desktop-shortcut.lnk", self.shortcut)
                state["phase"] = "committed"
                self.save(state)
            except BaseException as failure:
                try:
                    self.recover()
                except BaseException as recovery_failure:
                    raise RuntimeError(
                        "Upgrade failed and rollback needs recovery. "
                        "Recovery state is retained at "
                        f"{self.work}. Close K5 and rerun this installer; do not delete the "
                        f"upgrade workspace. Recovery error: {recovery_failure}"
                    ) from failure
                raise
            # A cleanup failure must not turn a committed, verified upgrade into rollback.
            try:
                self.recover()
            except Exception as exc:
                print(f"Upgrade committed; retained recovery workspace for later cleanup: {exc}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install-root", required=True, type=Path)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--powershell", required=True, type=Path)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--gstreamer", required=True)
    parser.add_argument("--shortcut", type=Path)
    parser.add_argument("--skip-shortcut", action="store_true")
    args = parser.parse_args()
    if os.name != "nt" or sys.version_info[:2] != (3, 12):
        parser.error("The Alpha installer requires Windows and Python 3.12.")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", args.revision):
        parser.error("K5 revision must be an exact SHA.")
    if not re.fullmatch(r"1\.28\.\d+", args.gstreamer):
        parser.error("Unreviewed GStreamer version.")
    Installer(
        args.install_root,
        args.source,
        args.powershell,
        args.revision,
        args.gstreamer,
        args.shortcut,
    ).install(skip_shortcut=args.skip_shortcut)
    print(f"K5 Vision Alpha runtime installed from reviewed commit {args.revision.lower()}.")
    print("Camera-free preflight passed; no camera media was contacted or stored.")


if __name__ == "__main__":
    main()
