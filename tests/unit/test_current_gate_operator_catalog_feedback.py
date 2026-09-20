from __future__ import annotations

import ctypes
import typing

import pytest

from k5vision.media.windows_operator_application import (
    WindowsOperatorApplicationError,
    WindowsOperatorApplicationErrorCode,
    WindowsOperatorApplicationState,
    _NativeShellError,
    _NativeShellFailure,
)
from k5vision.media.windows_operator_catalog_commands import (
    WindowsOperatorCatalogCommand,
    WindowsOperatorCatalogCommandKind,
)
from k5vision.media.windows_operator_catalog_feedback import (
    _FEEDBACK_TEXT,
    BoundedFeedbackCatalogUiWindowsOperatorControl,
    BoundedFeedbackCatalogWindowsOperatorApplication,
    WindowsOperatorCatalogFeedback,
    _feedback_for_command,
    _FeedbackOverlayCatalogWin32OperatorShellApi,
)
from k5vision.media.windows_operator_catalog_overlay import (
    _OverlayCatalogWin32OperatorShellApi,
)
from k5vision.media.windows_operator_control import (
    WindowsOperatorControlError,
    WindowsOperatorControlErrorCode,
)


class _FakeFeedbackApplication:
    def __init__(
        self,
        *,
        commands: tuple[WindowsOperatorCatalogCommand, ...] = (),
        rejections: int = 0,
    ) -> None:
        self.commands = commands
        self.rejections = rejections
        self.feedback: list[WindowsOperatorCatalogFeedback] = []

    def drain_catalog_commands(
        self,
        *,
        max_commands: int = 16,
    ) -> tuple[WindowsOperatorCatalogCommand, ...]:
        assert max_commands == 16
        commands = self.commands
        self.commands = ()
        return commands

    def drain_catalog_rejections(self) -> int:
        rejections = self.rejections
        self.rejections = 0
        return rejections

    def set_catalog_feedback(self, feedback: WindowsOperatorCatalogFeedback) -> None:
        self.feedback.append(feedback)


def test_feedback_text_is_closed_fixed_and_source_free() -> None:
    assert _FEEDBACK_TEXT == {
        WindowsOperatorCatalogFeedback.READY: "Ready",
        WindowsOperatorCatalogFeedback.SAVED: "Saved",
        WindowsOperatorCatalogFeedback.APPLIED: "Applied",
        WindowsOperatorCatalogFeedback.DELETED: "Deleted",
        WindowsOperatorCatalogFeedback.REJECTED: "Rejected",
    }
    payload = " ".join(_FEEDBACK_TEXT.values()).casefold()
    for forbidden in (
        "view 0",
        "slot",
        "camera",
        "source",
        "rtsp",
        "credential",
        "password",
        "path",
    ):
        assert forbidden not in payload


@pytest.mark.parametrize(
    ("kind", "feedback"),
    (
        (WindowsOperatorCatalogCommandKind.SAVE, WindowsOperatorCatalogFeedback.SAVED),
        (WindowsOperatorCatalogCommandKind.APPLY, WindowsOperatorCatalogFeedback.APPLIED),
        (WindowsOperatorCatalogCommandKind.DELETE, WindowsOperatorCatalogFeedback.DELETED),
    ),
)
def test_feedback_mapping_is_deterministic(
    kind: WindowsOperatorCatalogCommandKind,
    feedback: WindowsOperatorCatalogFeedback,
) -> None:
    assert _feedback_for_command(kind) == feedback


def test_feedback_mapping_rejects_unknown_kind() -> None:
    with pytest.raises(WindowsOperatorControlError) as failed:
        _feedback_for_command(typing.cast(WindowsOperatorCatalogCommandKind, "unknown"))
    assert failed.value.code == WindowsOperatorControlErrorCode.INVALID_CONFIGURATION


def test_native_feedback_initialization_binds_only_fixed_text_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_window_text = lambda *_args: 1
    user32 = type("_User32", (), {})()
    user32.SetWindowTextW = set_window_text

    def _base_init(self: object) -> None:
        self._user32 = user32
        self._catalog_z_order_handles = []

    monkeypatch.setattr(_OverlayCatalogWin32OperatorShellApi, "__init__", _base_init)
    api = _FeedbackOverlayCatalogWin32OperatorShellApi()

    assert api._feedback_handle == 0
    assert api._set_window_text is set_window_text
    assert api._set_window_text.restype is ctypes.c_int


def test_native_feedback_writes_only_fixed_text() -> None:
    api = object.__new__(_FeedbackOverlayCatalogWin32OperatorShellApi)
    api._feedback_handle = 707
    calls: list[tuple[int, str]] = []

    def _set_window_text(handle: ctypes.c_void_p, text: str) -> int:
        calls.append((int(handle.value or 0), text))
        return 1

    api._set_window_text = _set_window_text
    api.set_catalog_feedback(WindowsOperatorCatalogFeedback.SAVED)

    assert calls == [(707, "Saved")]


def test_native_feedback_rejects_invalid_or_missing_target() -> None:
    api = object.__new__(_FeedbackOverlayCatalogWin32OperatorShellApi)
    api._feedback_handle = 0
    api._set_window_text = lambda *_args: 1

    with pytest.raises(_NativeShellError) as missing:
        api.set_catalog_feedback(WindowsOperatorCatalogFeedback.READY)
    assert missing.value.failure == _NativeShellFailure.PUMP

    api._feedback_handle = 707
    with pytest.raises(_NativeShellError) as invalid:
        api.set_catalog_feedback(typing.cast(WindowsOperatorCatalogFeedback, "saved"))
    assert invalid.value.failure == _NativeShellFailure.PUMP


def test_native_feedback_failure_is_sanitized() -> None:
    api = object.__new__(_FeedbackOverlayCatalogWin32OperatorShellApi)
    api._feedback_handle = 707
    api._set_window_text = lambda *_args: 0

    with pytest.raises(_NativeShellError) as failed:
        api.set_catalog_feedback(WindowsOperatorCatalogFeedback.REJECTED)
    assert failed.value.failure == _NativeShellFailure.PUMP

    def _explode(*_args: object) -> int:
        raise RuntimeError("native private detail")

    api._set_window_text = _explode
    with pytest.raises(_NativeShellError) as exploded:
        api.set_catalog_feedback(WindowsOperatorCatalogFeedback.APPLIED)
    assert exploded.value.failure == _NativeShellFailure.PUMP
    assert "native private detail" not in str(exploded.value)


def test_feedback_child_joins_overlap_safe_z_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = object.__new__(_FeedbackOverlayCatalogWin32OperatorShellApi)
    api._feedback_handle = 0
    created: list[dict[str, object]] = []
    raised: list[bool] = []

    monkeypatch.setattr(
        _OverlayCatalogWin32OperatorShellApi,
        "create_shell",
        lambda _self, _width, _height: 101,
    )
    api._create_child = lambda **kwargs: created.append(kwargs) or 808
    api._raise_catalog_controls = lambda: raised.append(True)

    assert api.create_shell(1280, 720) == 101
    assert api._feedback_handle == 808
    assert created == [
        {
            "shell": 101,
            "class_name": "STATIC",
            "text": "Ready",
            "style": 0x50000000,
            "x": 352,
            "width": 92,
            "control_id": None,
        }
    ]
    assert raised == [True]


def test_feedback_child_creation_failure_cleans_ephemeral_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = object.__new__(_FeedbackOverlayCatalogWin32OperatorShellApi)
    api._feedback_handle = 0
    destroyed: list[int] = []

    monkeypatch.setattr(
        _OverlayCatalogWin32OperatorShellApi,
        "create_shell",
        lambda _self, _width, _height: 101,
    )
    monkeypatch.setattr(
        _OverlayCatalogWin32OperatorShellApi,
        "destroy_shell",
        lambda _self, shell: destroyed.append(shell),
    )

    def _fail_child(**_kwargs: object) -> int:
        raise _NativeShellError(_NativeShellFailure.CREATE)

    api._create_child = _fail_child

    with pytest.raises(_NativeShellError) as failed:
        api.create_shell(1280, 720)
    assert failed.value.failure == _NativeShellFailure.CREATE
    assert api._feedback_handle == 0
    assert destroyed == [101]


def test_feedback_destroy_clears_ephemeral_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = object.__new__(_FeedbackOverlayCatalogWin32OperatorShellApi)
    api._feedback_handle = 707
    destroyed: list[int] = []
    monkeypatch.setattr(
        _OverlayCatalogWin32OperatorShellApi,
        "destroy_shell",
        lambda _self, shell: destroyed.append(shell),
    )

    api.destroy_shell(101)

    assert api._feedback_handle == 0
    assert destroyed == [101]


def test_feedback_application_native_api_success_and_reuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native = object()
    monkeypatch.setattr(
        "k5vision.media.windows_operator_catalog_feedback."
        "_FeedbackOverlayCatalogWin32OperatorShellApi",
        lambda: native,
    )
    application = BoundedFeedbackCatalogWindowsOperatorApplication()

    assert application._ensure_native_api() is native
    assert application._ensure_native_api() is native


@pytest.mark.parametrize(
    ("failure", "code"),
    (
        (
            _NativeShellFailure.UNSUPPORTED_PLATFORM,
            WindowsOperatorApplicationErrorCode.UNSUPPORTED_PLATFORM,
        ),
        (_NativeShellFailure.LOAD, WindowsOperatorApplicationErrorCode.NATIVE_LOAD_FAILURE),
    ),
)
def test_feedback_application_native_api_failure_is_sanitized(
    monkeypatch: pytest.MonkeyPatch,
    failure: _NativeShellFailure,
    code: WindowsOperatorApplicationErrorCode,
) -> None:
    def _fail() -> object:
        raise _NativeShellError(failure)

    monkeypatch.setattr(
        "k5vision.media.windows_operator_catalog_feedback."
        "_FeedbackOverlayCatalogWin32OperatorShellApi",
        _fail,
    )
    application = BoundedFeedbackCatalogWindowsOperatorApplication()

    with pytest.raises(WindowsOperatorApplicationError) as failed:
        application._ensure_native_api()
    assert failed.value.code == code
    assert application._state == WindowsOperatorApplicationState.FAILED


def test_application_feedback_validates_native_boundary() -> None:
    application = BoundedFeedbackCatalogWindowsOperatorApplication()

    with pytest.raises(WindowsOperatorApplicationError) as invalid:
        application.set_catalog_feedback(typing.cast(WindowsOperatorCatalogFeedback, "saved"))
    assert invalid.value.code == WindowsOperatorApplicationErrorCode.INVALID_CONFIGURATION

    application._native_api = object()
    with pytest.raises(WindowsOperatorApplicationError) as unavailable:
        application.set_catalog_feedback(WindowsOperatorCatalogFeedback.SAVED)
    assert unavailable.value.code == WindowsOperatorApplicationErrorCode.PUMP_FAILURE


def test_application_feedback_delegates_fixed_enum() -> None:
    application = BoundedFeedbackCatalogWindowsOperatorApplication()
    received: list[WindowsOperatorCatalogFeedback] = []

    class _Native:
        def set_catalog_feedback(self, feedback: WindowsOperatorCatalogFeedback) -> None:
            received.append(feedback)

    application._native_api = _Native()
    application.set_catalog_feedback(WindowsOperatorCatalogFeedback.DELETED)

    assert received == [WindowsOperatorCatalogFeedback.DELETED]


def test_successful_commands_show_fixed_outcomes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = BoundedFeedbackCatalogUiWindowsOperatorControl()
    commands = tuple(
        WindowsOperatorCatalogCommand(kind=kind, view_id=index)
        for index, kind in enumerate(
            (
                WindowsOperatorCatalogCommandKind.SAVE,
                WindowsOperatorCatalogCommandKind.APPLY,
                WindowsOperatorCatalogCommandKind.DELETE,
            )
        )
    )
    application = _FakeFeedbackApplication(commands=commands)
    dispatched: list[WindowsOperatorCatalogCommand] = []
    monkeypatch.setattr(control, "dispatch_catalog_command", dispatched.append)

    control._drain_native_catalog_commands(application)

    assert dispatched == list(commands)
    assert application.feedback == [
        WindowsOperatorCatalogFeedback.SAVED,
        WindowsOperatorCatalogFeedback.APPLIED,
        WindowsOperatorCatalogFeedback.DELETED,
    ]
    assert control.catalog_ui_snapshot.native_commands == 3
    assert control.catalog_ui_snapshot.native_rejections == 0


def test_rejections_show_rejected_without_retaining_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = WindowsOperatorCatalogCommand(
        kind=WindowsOperatorCatalogCommandKind.APPLY,
        view_id=63,
    )
    application = _FakeFeedbackApplication(commands=(command,), rejections=2)
    control = BoundedFeedbackCatalogUiWindowsOperatorControl()

    def _reject(_command: WindowsOperatorCatalogCommand) -> None:
        raise WindowsOperatorControlError(
            WindowsOperatorControlErrorCode.INVALID_STATE,
            "rejected",
        )

    monkeypatch.setattr(control, "dispatch_catalog_command", _reject)
    control._drain_native_catalog_commands(application)

    assert application.feedback == [
        WindowsOperatorCatalogFeedback.REJECTED,
        WindowsOperatorCatalogFeedback.REJECTED,
    ]
    snapshot = control.catalog_ui_snapshot
    assert snapshot.native_commands == 0
    assert snapshot.native_rejections == 3
    payload = snapshot.model_dump_json().casefold()
    assert '"view_id":63' not in payload
    assert "applied" not in payload
    assert "rejected" not in payload


def test_feedback_control_rejects_missing_feedback_boundary() -> None:
    control = BoundedFeedbackCatalogUiWindowsOperatorControl()

    with pytest.raises(WindowsOperatorControlError) as failed:
        control._drain_native_catalog_commands(object())
    assert failed.value.code == WindowsOperatorControlErrorCode.APPLICATION_FAILURE


def test_feedback_control_rejects_unbounded_native_command_batch() -> None:
    commands = tuple(
        WindowsOperatorCatalogCommand(
            kind=WindowsOperatorCatalogCommandKind.SAVE,
            view_id=index % 64,
        )
        for index in range(65)
    )
    control = BoundedFeedbackCatalogUiWindowsOperatorControl()
    application = _FakeFeedbackApplication(commands=commands)

    with pytest.raises(WindowsOperatorControlError) as failed:
        control._drain_native_catalog_commands(application)
    assert failed.value.code == WindowsOperatorControlErrorCode.APPLICATION_FAILURE


def test_application_feedback_maps_native_failure_without_details() -> None:
    application = BoundedFeedbackCatalogWindowsOperatorApplication()

    class _Native:
        def set_catalog_feedback(self, _feedback: WindowsOperatorCatalogFeedback) -> None:
            raise _NativeShellError(_NativeShellFailure.PUMP)

    application._native_api = _Native()
    with pytest.raises(WindowsOperatorApplicationError) as failed:
        application.set_catalog_feedback(WindowsOperatorCatalogFeedback.SAVED)
    assert failed.value.code == WindowsOperatorApplicationErrorCode.PUMP_FAILURE
    assert "native" not in str(failed.value).casefold()
