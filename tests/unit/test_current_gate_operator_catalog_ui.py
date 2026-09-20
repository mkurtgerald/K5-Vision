from __future__ import annotations

from collections import deque

import pytest

from k5vision.media.viewport_geometry import (
    ViewportGeometry,
    ViewportLayout,
    ViewportPlacement,
)
from k5vision.media.windows_operator_application import (
    WindowsOperatorApplicationSnapshot,
    WindowsOperatorApplicationState,
    _NativeShellError,
)
from k5vision.media.windows_operator_catalog_commands import (
    WindowsOperatorCatalogCommand,
    WindowsOperatorCatalogCommandKind,
)
from k5vision.media.windows_operator_catalog_ui import (
    BoundedCatalogUiWindowsOperatorControl,
    BoundedCatalogWindowsOperatorApplication,
    _APPLY_BUTTON_ID,
    _CatalogWin32OperatorShellApi,
    _DELETE_BUTTON_ID,
    _MAX_NATIVE_CATALOG_COMMANDS,
    _SAVE_BUTTON_ID,
    _catalog_kind_for_control,
    _parse_view_id_text,
)
from k5vision.media.windows_operator_session import WindowsOperatorSessionState


def _layout(offset: int = 0) -> ViewportLayout:
    return ViewportLayout(
        placements=(
            ViewportPlacement(
                logical_slot=7,
                geometry=ViewportGeometry(
                    x=17 + offset,
                    y=29 + offset,
                    width=613 - offset,
                    height=347 + offset,
                    z_index=2,
                ),
            ),
            ViewportPlacement(
                logical_slot=4095,
                geometry=ViewportGeometry(
                    x=701 - offset,
                    y=41 + offset,
                    width=211 + offset,
                    height=719 - offset,
                    z_index=1,
                ),
            ),
        )
    )


class _FakeApplication:
    def __init__(
        self,
        *,
        commands: tuple[WindowsOperatorCatalogCommand, ...] = (),
        rejected: int = 0,
    ) -> None:
        self._commands = deque(commands)
        self._rejected = rejected

    @property
    def snapshot(self) -> WindowsOperatorApplicationSnapshot:
        return WindowsOperatorApplicationSnapshot(
            state=WindowsOperatorApplicationState.RUNNING,
            shell_open=True,
            pump_cycles=0,
            pumped_messages=0,
            generation=3,
            viewport_count=2,
            open_surface_count=2,
            delivered_frames=0,
            presentations=0,
        )

    def drain_catalog_commands(
        self,
        *,
        max_commands: int = 16,
    ) -> tuple[WindowsOperatorCatalogCommand, ...]:
        drained: list[WindowsOperatorCatalogCommand] = []
        while self._commands and len(drained) < max_commands:
            drained.append(self._commands.popleft())
        return tuple(drained)

    def drain_catalog_rejections(self) -> int:
        rejected = self._rejected
        self._rejected = 0
        return rejected


class _FakeNative:
    def __init__(
        self,
        commands: tuple[WindowsOperatorCatalogCommand, ...],
        rejected: int,
    ) -> None:
        self.commands = commands
        self.rejected = rejected

    def drain_catalog_commands(self, max_commands: int) -> tuple[WindowsOperatorCatalogCommand, ...]:
        return self.commands[:max_commands]

    def drain_catalog_rejections(self) -> int:
        return self.rejected


def _command(
    kind: WindowsOperatorCatalogCommandKind,
    view_id: int,
) -> WindowsOperatorCatalogCommand:
    return WindowsOperatorCatalogCommand(kind=kind, view_id=view_id)


def _control(application: _FakeApplication) -> BoundedCatalogUiWindowsOperatorControl:
    control = BoundedCatalogUiWindowsOperatorControl()
    control._state = WindowsOperatorSessionState.RUNNING
    control._application = application
    control._active_layout = _layout()
    return control


@pytest.mark.parametrize(
    ("text", "expected"),
    (
        ("0", 0),
        ("9", 9),
        ("63", 63),
        ("", None),
        ("64", None),
        ("99", None),
        ("-1", None),
        (" 1", None),
        ("1 ", None),
        ("01", 1),
        ("abc", None),
    ),
)
def test_view_id_text_is_strict_and_bounded(text: str, expected: int | None) -> None:
    assert _parse_view_id_text(text) == expected


def test_native_control_ids_map_only_to_accepted_catalog_actions() -> None:
    assert _catalog_kind_for_control(_SAVE_BUTTON_ID) == WindowsOperatorCatalogCommandKind.SAVE
    assert _catalog_kind_for_control(_APPLY_BUTTON_ID) == WindowsOperatorCatalogCommandKind.APPLY
    assert _catalog_kind_for_control(_DELETE_BUTTON_ID) == WindowsOperatorCatalogCommandKind.DELETE
    assert _catalog_kind_for_control(0) is None


def test_native_button_message_queues_only_exact_button_handle_and_valid_view() -> None:
    api = object.__new__(_CatalogWin32OperatorShellApi)
    api._catalog_commands = deque()
    api._catalog_rejections = 0
    api._button_handles = {_SAVE_BUTTON_ID: 101}
    api._read_view_id = lambda: 63

    assert api._consume_catalog_command_message(_SAVE_BUTTON_ID, 999) is False
    assert api._consume_catalog_command_message(_SAVE_BUTTON_ID, 101) is True

    assert tuple(api._catalog_commands) == (_command(WindowsOperatorCatalogCommandKind.SAVE, 63),)
    assert api._catalog_rejections == 0


def test_native_invalid_view_is_rejected_without_command_or_payload_retention() -> None:
    api = object.__new__(_CatalogWin32OperatorShellApi)
    api._catalog_commands = deque()
    api._catalog_rejections = 0
    api._button_handles = {_APPLY_BUTTON_ID: 202}
    api._read_view_id = lambda: None

    assert api._consume_catalog_command_message(_APPLY_BUTTON_ID, 202) is True
    assert not api._catalog_commands
    assert api._catalog_rejections == 1


def test_native_command_queue_is_bounded_and_fails_closed() -> None:
    api = object.__new__(_CatalogWin32OperatorShellApi)
    api._catalog_commands = deque(
        _command(WindowsOperatorCatalogCommandKind.SAVE, 1)
        for _ in range(_MAX_NATIVE_CATALOG_COMMANDS)
    )
    api._catalog_rejections = 0
    api._button_handles = {_DELETE_BUTTON_ID: 303}
    api._read_view_id = lambda: 1

    with pytest.raises(_NativeShellError):
        api._consume_catalog_command_message(_DELETE_BUTTON_ID, 303)

    assert len(api._catalog_commands) == _MAX_NATIVE_CATALOG_COMMANDS


def test_application_exposes_validated_source_free_native_batches() -> None:
    commands = (
        _command(WindowsOperatorCatalogCommandKind.SAVE, 2),
        _command(WindowsOperatorCatalogCommandKind.APPLY, 2),
    )
    application = BoundedCatalogWindowsOperatorApplication(
        native_api=_FakeNative(commands, rejected=3)
    )

    assert application.drain_catalog_commands(max_commands=16) == commands
    assert application.drain_catalog_rejections() == 3


def test_control_drains_success_and_rejection_without_session_failure() -> None:
    application = _FakeApplication(
        commands=(
            _command(WindowsOperatorCatalogCommandKind.SAVE, 5),
            _command(WindowsOperatorCatalogCommandKind.APPLY, 6),
        ),
        rejected=1,
    )
    control = _control(application)
    generation = application.snapshot.generation

    control._drain_native_catalog_commands(application)

    snapshot = control.catalog_ui_snapshot
    assert control._catalog.by_id()[5] == _layout()
    assert snapshot.native_commands == 1
    assert snapshot.native_rejections == 2
    assert snapshot.command.catalog_commands == 1
    assert snapshot.command.catalog_save_commands == 1
    assert application.snapshot.generation == generation
    assert control._controls.empty()


def test_native_apply_preserves_sparse_geometry_and_queues_existing_relayout_path() -> None:
    application = _FakeApplication()
    control = _control(application)
    control.dispatch_catalog_command(_command(WindowsOperatorCatalogCommandKind.SAVE, 8))
    accepted = control._catalog.by_id()[8]
    control._active_layout = _layout(11)
    application._commands.append(_command(WindowsOperatorCatalogCommandKind.APPLY, 8))

    control._drain_native_catalog_commands(application)
    request = control._controls.get_nowait()

    assert request.layout == accepted
    assert request.layout.by_slot()[4095].x == 701
    assert request.layout.by_slot()[7].z_index == 2
    assert control.catalog_ui_snapshot.native_commands == 1
    assert application.snapshot.generation == 3


def test_catalog_ui_snapshot_retains_no_native_or_command_arguments() -> None:
    application = _FakeApplication(
        commands=(_command(WindowsOperatorCatalogCommandKind.SAVE, 5),),
        rejected=1,
    )
    control = _control(application)
    control._set_selection(4095)
    control._drain_native_catalog_commands(application)
    payload = control.catalog_ui_snapshot.model_dump_json().casefold()

    assert '"selected_logical_slot":4095' in payload
    assert '"native_commands":1' in payload
    assert '"native_rejections":1' in payload
    for forbidden in (
        "rtsp://",
        "credential",
        "password",
        "source_id",
        "recording_id",
        "private_path",
        "native_handle",
        "pointer_value",
        "runner_identity",
        "payload",
        "camera_id",
        "stream_id",
        '"view_id"',
        '"kind"',
        "window_text",
    ):
        assert forbidden not in payload
