"""Actual Windows acknowledgment controls for the installer test driver.

An owned WinForms fixture reuses one HWND for Next, Install and Finish. It
contains no K5 product code or real installer payload. Only fixed test outcomes
enter pytest evidence; compiler output and private temporary paths are not logged.
"""

from __future__ import annotations

import gc
import importlib.util
import os
import subprocess
import sys
import time
import weakref
from contextlib import contextmanager
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Requires actual Win32 message delivery")
_BUTTON = 5201
_STATE = 5202
_COUNT = 5203
_TITLE = "K5 installer protocol fixture"
_CS = r"""
using System;
using System.Drawing;
using System.Runtime.InteropServices;
using System.Threading;
using System.Windows.Forms;

internal sealed class ProtocolForm : Form {
  [DllImport("user32.dll", EntryPoint="SetWindowLongPtrW", SetLastError=true)]
  private static extern IntPtr SetWindowLongPtr(IntPtr window, int index, IntPtr value);
  private readonly Button action = new Button();
  private readonly Label state = new Label();
  private readonly Label count = new Label();
  private readonly Panel first = new Panel();
  private readonly Panel second = new Panel();
  private readonly int installDelay;
  private int phase;
  private int calls;

  internal ProtocolForm(bool longInstall) {
    installDelay = longInstall ? 6100 : 200;
    Text = "K5 installer protocol fixture";
    StartPosition = FormStartPosition.Manual;
    Location = new Point(40, 40);
    ClientSize = new Size(420, 180);
    action.Text = "Next >"; action.Location = new Point(290, 135);
    action.Size = new Size(100, 28);
    state.Text = "idle"; state.Location = new Point(15, 10); state.Size = new Size(200, 24);
    count.Text = "0"; count.Location = new Point(240, 10); count.Size = new Size(50, 24);
    first.Location = second.Location = new Point(15, 45);
    first.Size = second.Size = new Size(370, 65);
    first.BackColor = Color.LightGray; second.BackColor = Color.LightBlue;
    second.Visible = false;
    Controls.Add(first); Controls.Add(second); Controls.Add(state);
    Controls.Add(count); Controls.Add(action);
    action.Click += HandleAction;
    Shown += HandleShown;
  }

  private void HandleShown(object sender, EventArgs args) {
    SetWindowLongPtr(action.Handle, -12, new IntPtr(5201));
    SetWindowLongPtr(state.Handle, -12, new IntPtr(5202));
    SetWindowLongPtr(count.Handle, -12, new IntPtr(5203));
    Activate();
  }

  private void HandleAction(object sender, EventArgs args) {
    calls++; count.Text = calls.ToString();
    if (phase == 0) {
      state.Text = "entered";
      Thread.Sleep(600);
      second.Visible = true;
      state.Text = "partial";
      Thread.Sleep(600);
      first.Visible = false;
      action.Text = "Install";
      phase = 1;
      state.Text = "ready";
    } else if (phase == 1) {
      state.Text = "installing";
      Thread.Sleep(installDelay);
      phase = 2;
      action.Text = "Finish";
      state.Text = "installed";
    } else {
      state.Text = "unexpected-extra-click";
    }
  }
}

internal static class EntryPoint {
  [STAThread]
  private static void Main(string[] args) {
    Application.EnableVisualStyles();
    Application.SetCompatibleTextRenderingDefault(false);
    Application.Run(new ProtocolForm(args.Length == 1 && args[0] == "long"));
  }
}
"""


def _check(condition, code):
    if not condition:
        pytest.fail(code, pytrace=False)


def _until(predicate, deadline, code):
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.01)
    pytest.fail(code, pytrace=False)


def _desktop_helpers():
    name = "k5_actual_installer_protocol_owner"
    if name in sys.modules:
        return sys.modules[name]
    source = Path(__file__).with_name("test_windows_desktop_app.py")
    spec = importlib.util.spec_from_file_location(name, source)
    module = importlib.util.module_from_spec(spec)
    # Keep the module's callback-owner root alive across fixture/test teardown,
    # including completion messages delivered after an owned target is killed.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def compiled_protocol_fixture(tmp_path_factory):
    directory = tmp_path_factory.mktemp("owned-installer-protocol")
    source, executable = directory / "Protocol.cs", directory / "Protocol.exe"
    source.write_text(_CS, encoding="utf-8")
    windows = Path(os.environ.get("WINDIR", r"C:\Windows"))
    compiler = next(
        (
            path
            for path in (
                windows / "Microsoft.NET/Framework64/v4.0.30319/csc.exe",
                windows / "Microsoft.NET/Framework/v4.0.30319/csc.exe",
            )
            if path.is_file()
        ),
        None,
    )
    _check(compiler is not None, "native_protocol_compiler_unavailable")
    result = subprocess.run(
        [
            str(compiler),
            "/nologo",
            "/target:winexe",
            "/platform:x64",
            "/reference:System.Windows.Forms.dll",
            "/reference:System.Drawing.dll",
            "/out:" + str(executable),
            str(source),
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=30,
        check=False,
    )
    _check(result.returncode == 0 and executable.is_file(), "native_protocol_compile_failed")
    return executable


@contextmanager
def _owned_fixture(executable, *, long_install=False):
    helpers = _desktop_helpers()
    api = helpers._Windows()
    process = subprocess.Popen(
        [str(executable), "long" if long_install else "short"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 90  # Original installer deadline is unchanged.
    form = None
    try:

        def ready():
            _check(process.poll() is None, "native_protocol_fixture_exited_early")
            return next(
                (
                    window
                    for window in api.windows()
                    if api.pid(window) == process.pid
                    and api.text(window) == _TITLE
                    and api.api.IsWindowVisible(window)
                    and api.control(window, _BUTTON)
                    and api.control(window, _STATE)
                    and api.control(window, _COUNT)
                    and api.text(api.control(window, _STATE)) == "idle"
                    and api.text(api.control(window, _COUNT)) == "0"
                ),
                None,
            )

        form = _until(ready, deadline, "native_protocol_fixture_not_ready")
        yield helpers, api, process, form, deadline
    finally:
        if process.poll() is None:
            if form:
                api.api.PostMessageW(form, 0x0010, 0, 0)  # Only the owned fixture's WM_CLOSE.
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        _check(process.poll() is not None, "native_protocol_owned_cleanup_failed")


def test_old_queued_duplicate_can_execute_install_without_observing_its_caption(
    compiled_protocol_fixture,
):
    with _owned_fixture(compiled_protocol_fixture) as (_, api, _, form, deadline):
        button = api.control(form, _BUTTON)
        state, count = api.control(form, _STATE), api.control(form, _COUNT)
        _check(bool(api.api.PostMessageW(button, 0x00F5, 0, 0)), "native_protocol_old_first_post")
        _until(
            lambda: api.text(state) == "entered",
            deadline,
            "native_protocol_old_handler_not_entered",
        )
        _check(api.text(button) == "Next >", "native_protocol_old_caption_not_next")
        # Deliberately reproduce the old asynchronous driver within this one
        # owned fixture: both messages were classified while the HWND said Next.
        _check(
            bool(api.api.PostMessageW(button, 0x00F5, 0, 0)), "native_protocol_old_duplicate_post"
        )
        _until(
            lambda: api.text(state) == "installed", deadline, "native_protocol_old_repro_incomplete"
        )
        _check(api.text(count) == "2", "native_protocol_old_repro_click_count")
        _check(api.control(form, _BUTTON) == button, "native_protocol_old_hwnd_changed")
        _check(api.button_caption(button) == "finish", "native_protocol_old_repro_final_phase")


def test_completion_ticket_waits_for_handler_return_and_long_install(
    compiled_protocol_fixture,
):
    with _owned_fixture(compiled_protocol_fixture, long_install=True) as (
        helpers,
        api,
        _,
        form,
        deadline,
    ):
        button = api.control(form, _BUTTON)
        state, count = api.control(form, _STATE), api.control(form, _COUNT)
        ticket = api.post_click(button)
        _check(api in helpers._INSTALLER_COMMAND_OWNERS, "native_protocol_callback_not_rooted")
        _until(
            lambda: api.text(state) == "entered", deadline, "native_protocol_handler_not_entered"
        )
        _check(not api.click_completed(ticket), "native_protocol_callback_before_handler_return")
        _until(
            lambda: api.text(state) == "partial", deadline, "native_protocol_partial_page_absent"
        )
        _check(
            not api.click_completed(ticket), "native_protocol_callback_before_transition_finished"
        )
        _until(
            lambda: api.click_completed(ticket), deadline, "native_protocol_next_not_acknowledged"
        )
        _check(api.text(count) == "1", "native_protocol_duplicate_next_dispatched")
        _check(api.button_caption(button) == "install", "native_protocol_install_not_observed")
        _check(api.control(form, _BUTTON) == button, "native_protocol_reused_hwnd_changed")
        started = time.monotonic()
        install = api.post_click(button)
        _until(
            lambda: api.click_completed(install),
            deadline,
            "native_protocol_install_not_acknowledged",
        )
        _check(time.monotonic() - started >= 5, "native_protocol_long_handler_not_exercised")
        _check(
            api.text(state) == "installed" and api.text(count) == "2",
            "native_protocol_install_not_exercised",
        )
        _check(api.button_caption(button) == "finish", "native_protocol_finish_not_observed")
        _check(
            api not in helpers._INSTALLER_COMMAND_OWNERS, "native_protocol_completed_owner_retained"
        )


def test_pending_callback_remains_owned_when_target_exits(compiled_protocol_fixture):
    with _owned_fixture(compiled_protocol_fixture) as (helpers, api, process, form, deadline):
        ticket = api.post_click(api.control(form, _BUTTON))
        _until(
            lambda: api.text(api.control(form, _STATE)) == "entered",
            deadline,
            "native_protocol_retirement_handler_not_entered",
        )
        _check(not api.click_completed(ticket), "native_protocol_retirement_was_not_pending")
        process.kill()
        process.wait(timeout=5)
        completed = api.click_completed(ticket)
        owner = weakref.ref(api)
        if completed:
            _check(
                api not in helpers._INSTALLER_COMMAND_OWNERS, "native_protocol_retired_ack_owner"
            )
        else:
            _check(
                api in helpers._INSTALLER_COMMAND_OWNERS,
                "native_protocol_retired_callback_unrooted",
            )
    if not completed:
        del api
        gc.collect()
        _check(owner() is not None, "native_protocol_retired_callback_owner_collected")
        _check(
            owner() in helpers._INSTALLER_COMMAND_OWNERS,
            "native_protocol_retired_owner_not_globally_rooted",
        )
