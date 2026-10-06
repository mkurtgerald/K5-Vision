"""Unqualified native acceptance for real BUTTON notifications and sent close.

This lazy helper uses only disposable owned windows and does not load media.
The already-selected application Windows wrapper owns its single execution.
Authenticated media pause/pacing, manual title-bar/Alt+F4, and two-run playback
remain separate acceptance requirements; source fake tests do not qualify them.
"""

from __future__ import annotations

import ctypes
import sys


def run_owned_playback_shell_controls() -> dict[str, object]:
    """Exercise actual native BUTTON notifications and sent owner-close handling."""
    if sys.platform != "win32":
        raise RuntimeError("native playback shell qualification requires Windows")

    import hashlib
    from pathlib import Path

    from k5vision.media import (
        windows_operator_application,
        windows_operator_interaction,
        windows_operator_message_routing,
        windows_operator_playback_ui,
        windows_operator_window_procedure,
    )
    from k5vision.media.playback_control import PlaybackControlState
    from k5vision.media.windows_operator_playback_ui import (
        _PAUSE_BUTTON_ID,
        _RESUME_BUTTON_ID,
        _STOP_BUTTON_ID,
        WindowsPlaybackCommand,
        _PlaybackWin32OperatorShellApi,
    )
    from k5vision.media.windows_operator_window_procedure import _PINNED_PROCEDURES

    api_a, api_b = _PlaybackWin32OperatorShellApi(), _PlaybackWin32OperatorShellApi()
    owned = []
    initial_pins = len(_PINNED_PROCEDURES)
    user32 = api_a._user32
    send = user32.SendMessageW
    send.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_size_t, ctypes.c_ssize_t]
    send.restype = ctypes.c_ssize_t
    is_window = user32.IsWindow
    is_window.argtypes = [ctypes.c_void_p]
    is_window.restype = ctypes.c_int
    try:
        a = api_a.create_shell(1280, 720)
        owned.append((api_a, a))
        b = api_b.create_shell(1280, 720)
        owned.append((api_b, b))
        # BM_CLICK runs the real BUTTON procedure, which sends BN_CLICKED to its
        # parent synchronously. No posted WM_COMMAND substitutes this step.
        send(ctypes.c_void_p(api_a._button_handles[_PAUSE_BUTTON_ID]), 0x00F5, 0, 0)
        assert api_a.drain_playback_commands(4) == (WindowsPlaybackCommand.PAUSE,)
        assert api_b.drain_playback_commands(4) == ()
        api_a.set_playback_state(a, PlaybackControlState.PAUSED)
        send(ctypes.c_void_p(api_a._button_handles[_RESUME_BUTTON_ID]), 0x00F5, 0, 0)
        assert api_a.drain_playback_commands(4) == (WindowsPlaybackCommand.RESUME,)
        send(ctypes.c_void_p(api_b._button_handles[_STOP_BUTTON_ID]), 0x00F5, 0, 0)
        assert api_b.drain_playback_commands(4) == (WindowsPlaybackCommand.STOP,)
        # A real sent WM_COMMAND with B's button identity cannot control A.
        send(ctypes.c_void_p(a), 0x0111, _STOP_BUTTON_ID, api_b._button_handles[_STOP_BUTTON_ID])
        assert api_a.drain_playback_commands(4) == ()
        # Default WM_SYSCOMMAND(SC_CLOSE) synchronously sends WM_CLOSE. The owned
        # subclass consumes it, preserving HWND until asynchronous owner cleanup.
        send(ctypes.c_void_p(a), 0x0112, 0xF060, 0)
        assert is_window(ctypes.c_void_p(a))
        assert api_a.pump_messages(a, 64)[1]
        assert not api_b.pump_messages(b, 64)[1]
        send(ctypes.c_void_p(a), 0x0010, 0, 0)
        assert is_window(ctypes.c_void_p(a))
        api_a.destroy_shell(a)
        owned.remove((api_a, a))
        assert not is_window(ctypes.c_void_p(a))
        assert is_window(ctypes.c_void_p(b))
        assert api_b.drain_playback_commands(4) == ()
    finally:
        for api, hwnd in reversed(owned):
            api.destroy_shell(hwnd)
    assert len(_PINNED_PROCEDURES) == initial_pins
    source_root = Path(__file__).resolve().parents[2]
    digests = {}
    for module in (
        windows_operator_application,
        windows_operator_interaction,
        windows_operator_message_routing,
        windows_operator_playback_ui,
        windows_operator_window_procedure,
    ):
        path = Path(module.__file__).resolve()
        relative = path.relative_to(source_root)
        assert str(relative).replace("\\", "/").startswith("src/k5vision/media/")
        digests[relative.as_posix()] = hashlib.sha256(
            path.read_bytes().replace(b"\r\n", b"\n")
        ).hexdigest()
    for name in (
        "test_operator_playback_shell_windows.py",
        "test_current_gate_application_windows.py",
    ):
        path = Path(__file__).with_name(name)
        digests[path.relative_to(source_root).as_posix()] = hashlib.sha256(
            path.read_bytes().replace(b"\r\n", b"\n")
        ).hexdigest()
    return {
        "schema_version": "1",
        "qualification": "owned-generated-native-playback-controls",
        "source_sha256": digests,
        "hash_basis": "sha256-lf-normalized",
        "actual_button_clicks": 3,
        "accepted_native_commands": 3,
        "forged_sender_rejected": True,
        "sent_close_deferred": True,
        "sibling_shell_survived": True,
        "owned_shells_destroyed": 2,
        "callback_pins_remaining": len(_PINNED_PROCEDURES) - initial_pins,
        "media_execution": False,
        "authenticated_media_acceptance": False,
    }
