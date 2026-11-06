"""Actual Inno completion protocol, with an isolated deterministic guard fixture.

Run on an admitted disposable Windows desktop with K5_DESKTOP_ISCC pointing to
an archive-hash-admitted Inno 6.4.3 compiler. This is installer-control evidence,
not acceptance of a full installed payload or operator workflow.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(os.name != "nt", reason="Actual Windows installer required")


def _build(tmp_path: Path, reject: bool) -> Path:
    compiler = os.environ.get("K5_DESKTOP_ISCC")
    if not compiler:
        pytest.skip("An admitted Inno 6.4.3 compiler is required")
    csc = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    assert csc.is_file()
    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "licenses").mkdir()
    (payload / "licenses/INSTALLATION-LICENSE.txt").write_text("Test fixture only")
    (payload / "payload-manifest.json").write_text("{}")
    # Independent mutex namespace, no backend, accounts, recordings or sockets.
    # Rejection happens ONLY after extraction, so the old ssPostInstall script
    # demonstrably reaches its defective success path.
    fixture = tmp_path / "Guard.cs"
    fixture.write_text(
        """using System;
using System.IO;
using System.Security.Principal;
class Guard {
  static int Main(string[] args) {
    if (args.Length == 2 && args[0] == "--installation-identity") {
      File.WriteAllText(args[1], "Global\\\\K5VisionDesktop-" +
        WindowsIdentity.GetCurrent().User.Value + "-MUTEX_SUFFIX");
      return 0;
    }
    string root = AppDomain.CurrentDomain.BaseDirectory;
    if (args.Length == 1 && args[0] == "--installation-extraction-check") {
      File.WriteAllText(Path.Combine(root, "extraction-observed"), "yes");
      return 0;
    }
    if (args.Length == 1 && args[0] == "--installation-payload-check") {
      File.WriteAllText(Path.Combine(root, "final-observed"), "yes");
      return FINAL_RESULT;
    }
    File.WriteAllText(Path.Combine(root, "launch-observed"), "yes");
    return 0;
  }
}
""".replace("MUTEX_SUFFIX", str(uuid4().int)).replace("FINAL_RESULT", "1" if reject else "0")
    )
    launcher = payload / "K5Vision.exe"
    subprocess.run(
        [str(csc), "/nologo", "/target:winexe", "/out:" + str(launcher), str(fixture)],
        check=True,
        timeout=60,
        capture_output=True,
    )
    script = (ROOT / "windows/desktop/K5Vision.iss").read_text()
    # Only isolate installation identity and OS integrations; Code and Run
    # sections remain the exact production source under test.
    script = script.replace(
        "AppId={{B495C557-C6B7-4DF6-B0B8-4D2A16E940F8}", "AppId={{" + str(uuid4()) + "}"
    )
    script = script.replace("[Setup]\n", "[Setup]\nUninstallable=no\n")
    start, end = script.index("[Icons]"), script.index("[Run]")
    script = script[:start] + script[end:]
    configuration = tmp_path / "completion.iss"
    configuration.write_text(script)
    output = tmp_path / "output"
    result = subprocess.run(
        [
            compiler,
            "/DPayloadRoot=" + str(payload),
            "/DOutputRoot=" + str(output),
            "/DLauncherSha=" + hashlib.sha256(launcher.read_bytes()).hexdigest(),
            str(configuration),
        ],
        check=True,
        timeout=90,
        capture_output=True,
        text=True,
    )
    assert "6.4.3" in result.stdout
    return output / "K5VisionSetup.exe"


def _gui_completion(process, reject: bool):
    import psutil

    spec = importlib.util.spec_from_file_location(
        "completion_desktop_helpers", Path(__file__).with_name("test_windows_desktop_app.py")
    )
    assert spec and spec.loader
    helpers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helpers)
    api = helpers._Windows()
    deadline = time.monotonic() + 60
    completion_seen = False
    while process.poll() is None and time.monotonic() < deadline:
        try:
            root = psutil.Process(process.pid)
            pids = {root.pid, *(child.pid for child in root.children(recursive=True))}
        except psutil.NoSuchProcess:
            break
        for form in api.windows():
            if api.pid(form) not in pids or not api.api.IsWindowVisible(form):
                continue
            controls = [w for w in api.windows(form) if api.api.IsWindowVisible(w)]
            captions = {api.text(w).replace("&", "").strip().lower(): w for w in controls}
            if "finish" in captions:
                completion_seen = True
                text = "\n".join(captions)
                if reject:
                    assert "k5 vision installation failed" in text
                    assert "do not use this installation" in text
                    assert "has been installed" not in text
                    assert not any(api.class_name(w) == "TNewCheckListBox" for w in controls)
                else:
                    assert "has been installed" in text
                api.click(captions["finish"])
                continue
            if "i accept the agreement" in captions:
                api.click(captions["i accept the agreement"])
            for name in ("install", "next >", "next", "ok"):
                button = captions.get(name)
                if button and api.api.IsWindowEnabled(button):
                    # Install is synchronous inside Inno; post rather than block.
                    assert api.api.PostMessageW(button, 0x00F5, 0, 0)
                    break
        time.sleep(0.1)
    assert completion_seen, "Installer never presented its completion outcome"


@pytest.mark.parametrize("reject", [False, True])
@pytest.mark.parametrize("silent", [False, True])
def test_actual_installer_completion_after_final_guard_result(tmp_path, reject, silent):
    setup = _build(tmp_path, reject)
    installed = tmp_path / "installed"
    args = [str(setup), "/SP-", "/SUPPRESSMSGBOXES", "/NORESTART", "/DIR=" + str(installed)]
    if silent:
        args.append("/VERYSILENT")
    process = subprocess.Popen(args, stdin=subprocess.DEVNULL)
    try:
        if not silent:
            _gui_completion(process, reject)
        result = process.wait(timeout=90)
        assert result == (1 if reject else 0)
        assert (installed / "extraction-observed").is_file()
        assert (installed / "final-observed").is_file()
        if reject:
            assert not (installed / "launch-observed").exists()
    finally:
        # Kill only this fixture's descendants if a failed assertion left them.
        if process.poll() is None:
            import psutil

            root = psutil.Process(process.pid)
            for child in root.children(recursive=True):
                child.kill()
            process.kill()
            process.wait(timeout=10)
