from __future__ import annotations

import ctypes
import typing

import pytest

from k5vision.media.viewport_catalog import ViewportCatalogEntry, build_viewport_catalog
from k5vision.media.viewport_geometry import ViewportGeometry, ViewportLayout, ViewportPlacement
from k5vision.media.windows_operator_application import (
    WindowsOperatorApplicationError,
    WindowsOperatorApplicationErrorCode,
    _NativeShellError,
    _NativeShellFailure,
)
from k5vision.media.windows_operator_catalog_commands import (
    WindowsOperatorCatalogCommand,
    WindowsOperatorCatalogCommandKind,
)
from k5vision.media.windows_operator_catalog_feedback import (
    WindowsOperatorCatalogFeedback,
    _FeedbackOverlayCatalogWin32OperatorShellApi,
)
from k5vision.media.windows_operator_catalog_selector import (
    _NEXT_BUTTON_ID,
    _PREVIOUS_BUTTON_ID,
    BoundedSelectorCatalogUiWindowsOperatorControl,
    BoundedSelectorCatalogWindowsOperatorApplication,
    _SelectorFeedbackCatalogWin32OperatorShellApi,
    _validated_catalog_view_ids,
)
from k5vision.media.windows_operator_control import (
    WindowsOperatorControlError,
    WindowsOperatorControlErrorCode,
)


def _layout(x: int = 11) -> ViewportLayout:
    return ViewportLayout(
        placements=(
            ViewportPlacement(
                logical_slot=4095,
                geometry=ViewportGeometry(x=x, y=23, width=317, height=181, z_index=7),
            ),
        )
    )


class _FakeSelectorApplication:
    def __init__(
        self,
        *,
        commands: tuple[WindowsOperatorCatalogCommand, ...] = (),
        rejections: int = 0,
    ) -> None:
        self.commands = commands
        self.rejections = rejections
        self.feedback: list[WindowsOperatorCatalogFeedback] = []
        self.published: list[tuple[int, ...]] = []

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

    def set_catalog_view_ids(self, view_ids: tuple[int, ...]) -> None:
        self.published.append(view_ids)


def test_selector_view_ids_require_canonical_bounded_occupancy() -> None:
    assert _validated_catalog_view_ids((0, 7, 63)) == (0, 7, 63)
    for invalid in ((7, 0), (7, 7), (64,), (True,)):
        with pytest.raises(ValueError):
            _validated_catalog_view_ids(typing.cast(tuple[int, ...], invalid))


def test_selector_children_join_existing_overlap_safe_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = object.__new__(_SelectorFeedbackCatalogWin32OperatorShellApi)
    api._selector_view_ids = ()
    api._selector_button_handles = {}
    created: list[dict[str, object]] = []
    raised: list[bool] = []

    monkeypatch.setattr(
        _FeedbackOverlayCatalogWin32OperatorShellApi,
        "create_shell",
        lambda _self, _width, _height: 101,
    )
    api._create_child = lambda **kwargs: created.append(kwargs) or 800 + len(created)
    api._raise_catalog_controls = lambda: raised.append(True)

    assert api.create_shell(1280, 720) == 101
    assert [item["text"] for item in created] == ["Prev", "Next"]
    assert [item["control_id"] for item in created] == [_PREVIOUS_BUTTON_ID, _NEXT_BUTTON_ID]
    assert api._selector_button_handles == {
        _PREVIOUS_BUTTON_ID: 801,
        _NEXT_BUTTON_ID: 802,
    }
    assert raised == [True]


def test_selector_child_failure_cleans_ephemeral_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = object.__new__(_SelectorFeedbackCatalogWin32OperatorShellApi)
    api._selector_view_ids = (2,)
    api._selector_button_handles = {}
    destroyed: list[int] = []
    monkeypatch.setattr(
        _FeedbackOverlayCatalogWin32OperatorShellApi,
        "create_shell",
        lambda _self, _width, _height: 101,
    )
    monkeypatch.setattr(
        _FeedbackOverlayCatalogWin32OperatorShellApi,
        "destroy_shell",
        lambda _self, shell: destroyed.append(shell),
    )

    def _fail_child(**_kwargs: object) -> int:
        raise _NativeShellError(_NativeShellFailure.CREATE)

    api._create_child = _fail_child
    api._raise_catalog_controls = lambda: None

    with pytest.raises(_NativeShellError) as failed:
        api.create_shell(1280, 720)
    assert failed.value.failure == _NativeShellFailure.CREATE
    assert api._selector_view_ids == ()
    assert api._selector_button_handles == {}
    assert destroyed == [101]


def test_native_selector_rejects_noncanonical_occupancy() -> None:
    api = object.__new__(_SelectorFeedbackCatalogWin32OperatorShellApi)
    with pytest.raises(_NativeShellError) as failed:
        api.set_catalog_view_ids((7, 2))
    assert failed.value.failure == _NativeShellFailure.PUMP


def test_selector_cycles_only_published_view_ids() -> None:
    api = object.__new__(_SelectorFeedbackCatalogWin32OperatorShellApi)
    api._selector_view_ids = (2, 7, 63)
    api._selector_button_handles = {
        _PREVIOUS_BUTTON_ID: 801,
        _NEXT_BUTTON_ID: 802,
    }
    api._catalog_rejections = 0
    api._view_editor = 707
    current = [7]
    writes: list[str] = []
    api._read_view_id = lambda: current[-1]

    def _set_window_text(_handle: ctypes.c_void_p, text: str) -> int:
        writes.append(text)
        current.append(int(text))
        return 1

    api._set_window_text = _set_window_text
    assert api._consume_catalog_command_message(_NEXT_BUTTON_ID, 802)
    assert api._consume_catalog_command_message(_PREVIOUS_BUTTON_ID, 801)
    assert writes == ["63", "7"]
    assert api._catalog_rejections == 0


def test_selector_chooses_boundary_when_editor_is_not_saved() -> None:
    api = object.__new__(_SelectorFeedbackCatalogWin32OperatorShellApi)
    api._selector_view_ids = (2, 7, 63)
    api._selector_button_handles = {
        _PREVIOUS_BUTTON_ID: 801,
        _NEXT_BUTTON_ID: 802,
    }
    api._catalog_rejections = 0
    api._view_editor = 707
    api._read_view_id = lambda: 41
    writes: list[str] = []
    api._set_window_text = lambda _handle, text: writes.append(text) or 1

    assert api._consume_catalog_command_message(_NEXT_BUTTON_ID, 802)
    assert api._consume_catalog_command_message(_PREVIOUS_BUTTON_ID, 801)
    assert writes == ["2", "63"]


def test_selector_wraps_and_rejects_empty_occupancy() -> None:
    api = object.__new__(_SelectorFeedbackCatalogWin32OperatorShellApi)
    api._selector_view_ids = (2, 7, 63)
    api._selector_button_handles = {
        _PREVIOUS_BUTTON_ID: 801,
        _NEXT_BUTTON_ID: 802,
    }
    api._catalog_rejections = 0
    api._view_editor = 707
    api._read_view_id = lambda: 63
    writes: list[str] = []
    api._set_window_text = lambda _handle, text: writes.append(text) or 1

    assert api._consume_catalog_command_message(_NEXT_BUTTON_ID, 802)
    assert writes == ["2"]

    api._selector_view_ids = ()
    assert api._consume_catalog_command_message(_NEXT_BUTTON_ID, 802)
    assert api._catalog_rejections == 1
    assert writes == ["2"]


def test_selector_rejects_spoofed_button_notification() -> None:
    api = object.__new__(_SelectorFeedbackCatalogWin32OperatorShellApi)
    api._selector_view_ids = (2,)
    api._selector_button_handles = {_NEXT_BUTTON_ID: 802}
    assert not api._consume_catalog_command_message(_NEXT_BUTTON_ID, 999)


def test_selector_native_update_failure_is_sanitized() -> None:
    api = object.__new__(_SelectorFeedbackCatalogWin32OperatorShellApi)
    api._selector_view_ids = (2,)
    api._selector_button_handles = {_NEXT_BUTTON_ID: 802}
    api._catalog_rejections = 0
    api._view_editor = 707
    api._read_view_id = lambda: 2
    api._set_window_text = lambda *_args: 0

    with pytest.raises(_NativeShellError) as failed:
        api._consume_catalog_command_message(_NEXT_BUTTON_ID, 802)
    assert failed.value.failure == _NativeShellFailure.PUMP

    def _explode(*_args: object) -> int:
        raise RuntimeError("private selector detail")

    api._set_window_text = _explode
    with pytest.raises(_NativeShellError) as exploded:
        api._consume_catalog_command_message(_NEXT_BUTTON_ID, 802)
    assert exploded.value.failure == _NativeShellFailure.PUMP
    assert "private selector detail" not in str(exploded.value)


def test_selector_destroy_clears_ephemeral_occupancy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = object.__new__(_SelectorFeedbackCatalogWin32OperatorShellApi)
    api._selector_view_ids = (2, 7)
    api._selector_button_handles = {_NEXT_BUTTON_ID: 802}
    destroyed: list[int] = []
    monkeypatch.setattr(
        _FeedbackOverlayCatalogWin32OperatorShellApi,
        "destroy_shell",
        lambda _self, shell: destroyed.append(shell),
    )

    api.destroy_shell(101)
    assert api._selector_view_ids == ()
    assert api._selector_button_handles == {}
    assert destroyed == [101]


def test_selector_application_native_api_success_and_reuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native = object()
    monkeypatch.setattr(
        "k5vision.media.windows_operator_catalog_selector."
        "_SelectorFeedbackCatalogWin32OperatorShellApi",
        lambda: native,
    )
    application = BoundedSelectorCatalogWindowsOperatorApplication()

    assert application._ensure_native_api() is native
    assert application._ensure_native_api() is native


def test_selector_application_native_api_failure_is_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail() -> object:
        raise _NativeShellError(_NativeShellFailure.UNSUPPORTED_PLATFORM)

    monkeypatch.setattr(
        "k5vision.media.windows_operator_catalog_selector."
        "_SelectorFeedbackCatalogWin32OperatorShellApi",
        _fail,
    )
    application = BoundedSelectorCatalogWindowsOperatorApplication()

    with pytest.raises(WindowsOperatorApplicationError) as failed:
        application._ensure_native_api()
    assert failed.value.code == WindowsOperatorApplicationErrorCode.UNSUPPORTED_PLATFORM


def test_selector_application_validates_and_delegates_occupancy() -> None:
    application = BoundedSelectorCatalogWindowsOperatorApplication()
    received: list[tuple[int, ...]] = []

    class _Native:
        def set_catalog_view_ids(self, view_ids: tuple[int, ...]) -> None:
            received.append(view_ids)

    application._native_api = _Native()
    application.set_catalog_view_ids((0, 8, 63))
    assert received == [(0, 8, 63)]

    with pytest.raises(WindowsOperatorApplicationError) as invalid:
        application.set_catalog_view_ids((8, 0))
    assert invalid.value.code == WindowsOperatorApplicationErrorCode.INVALID_CONFIGURATION


def test_selector_application_rejects_missing_native_boundary() -> None:
    application = BoundedSelectorCatalogWindowsOperatorApplication()
    application._native_api = object()
    with pytest.raises(WindowsOperatorApplicationError) as failed:
        application.set_catalog_view_ids((3,))
    assert failed.value.code == WindowsOperatorApplicationErrorCode.PUMP_FAILURE


def test_selector_application_maps_native_failure_without_details() -> None:
    application = BoundedSelectorCatalogWindowsOperatorApplication()

    class _Native:
        def set_catalog_view_ids(self, _view_ids: tuple[int, ...]) -> None:
            raise _NativeShellError(_NativeShellFailure.PUMP)

    application._native_api = _Native()
    with pytest.raises(WindowsOperatorApplicationError) as failed:
        application.set_catalog_view_ids((3,))
    assert failed.value.code == WindowsOperatorApplicationErrorCode.PUMP_FAILURE
    assert "view 3" not in str(failed.value).casefold()


def test_control_publishes_canonical_occupancy_without_retaining_ids() -> None:
    control = BoundedSelectorCatalogUiWindowsOperatorControl()
    control._catalog = build_viewport_catalog(
        (
            ViewportCatalogEntry(view_id=63, layout=_layout()),
            ViewportCatalogEntry(view_id=2, layout=_layout(17)),
        )
    )
    application = _FakeSelectorApplication()

    control._sync_catalog_selector(application)
    control._sync_catalog_selector(application)

    assert application.published == [(2, 63)]
    payload = control.catalog_ui_snapshot.model_dump_json().casefold()
    for forbidden in ("view_id", "selector_view_ids", "rtsp://", "credential", "password", "63"):
        assert forbidden not in payload


def test_save_and_delete_refresh_selector_after_accepted_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = BoundedSelectorCatalogUiWindowsOperatorControl()
    application = _FakeSelectorApplication(
        commands=(
            WindowsOperatorCatalogCommand(
                kind=WindowsOperatorCatalogCommandKind.SAVE,
                view_id=5,
            ),
        )
    )

    def _dispatch(command: WindowsOperatorCatalogCommand) -> None:
        if command.kind == WindowsOperatorCatalogCommandKind.SAVE:
            control._catalog = build_viewport_catalog(
                (ViewportCatalogEntry(view_id=command.view_id, layout=_layout()),)
            )
        elif command.kind == WindowsOperatorCatalogCommandKind.DELETE:
            control._catalog = build_viewport_catalog(())

    monkeypatch.setattr(control, "dispatch_catalog_command", _dispatch)
    control._drain_native_catalog_commands(application)
    assert application.published == [(), (5,)]
    assert application.feedback == [WindowsOperatorCatalogFeedback.SAVED]

    application.commands = (
        WindowsOperatorCatalogCommand(
            kind=WindowsOperatorCatalogCommandKind.DELETE,
            view_id=5,
        ),
    )
    control._drain_native_catalog_commands(application)
    assert application.published[-1] == ()
    assert application.feedback[-1] == WindowsOperatorCatalogFeedback.DELETED
    assert control.catalog_ui_snapshot.native_commands == 2


def test_selector_control_maps_application_failure_without_details() -> None:
    class _FailingSelectorApplication(_FakeSelectorApplication):
        def set_catalog_view_ids(self, _view_ids: tuple[int, ...]) -> None:
            raise WindowsOperatorApplicationError(
                WindowsOperatorApplicationErrorCode.PUMP_FAILURE,
                "private selector detail",
            )

    control = BoundedSelectorCatalogUiWindowsOperatorControl()
    with pytest.raises(WindowsOperatorControlError) as failed:
        control._sync_catalog_selector(_FailingSelectorApplication())
    assert failed.value.code == WindowsOperatorControlErrorCode.APPLICATION_FAILURE
    assert "private selector detail" not in str(failed.value)


def test_selector_control_fails_closed_without_selector_boundary() -> None:
    control = BoundedSelectorCatalogUiWindowsOperatorControl()
    with pytest.raises(WindowsOperatorControlError) as failed:
        control._sync_catalog_selector(object())
    assert failed.value.code == WindowsOperatorControlErrorCode.APPLICATION_FAILURE
