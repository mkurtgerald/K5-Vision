from __future__ import annotations

import asyncio

import pytest

from k5vision.media.mixed_presentation import MixedLiveStream
from k5vision.media.viewport_editor import (
    BoundedViewportEditor,
    ViewportEditorError,
    ViewportEditorErrorCode,
    ViewportMove,
    ViewportResize,
)
from k5vision.media.viewport_geometry import ViewportGeometry, ViewportLayout, ViewportPlacement
from k5vision.media.windows_operator_application import (
    WindowsOperatorApplicationSnapshot,
    WindowsOperatorApplicationState,
)
from k5vision.media.windows_operator_control import BoundedWindowsOperatorControl


def _layout() -> ViewportLayout:
    return ViewportLayout(
        placements=(
            ViewportPlacement(
                logical_slot=7,
                geometry=ViewportGeometry(x=17, y=29, width=613, height=347, z_index=2),
            ),
            ViewportPlacement(
                logical_slot=4095,
                geometry=ViewportGeometry(x=701, y=41, width=211, height=719, z_index=1),
            ),
        )
    )


def test_editor_moves_sparse_slot_and_preserves_untouched_slot() -> None:
    original = _layout()
    editor = BoundedViewportEditor(original)

    result = editor.apply(ViewportMove(logical_slot=4095, dx=-684, dy=-12))

    moved = result.layout.by_slot()[4095]
    untouched = result.layout.by_slot()[7]
    assert (moved.x, moved.y, moved.width, moved.height, moved.z_index) == (17, 29, 211, 719, 1)
    assert untouched == original.by_slot()[7]
    assert result.operations == 1


def test_editor_resize_preserves_position_and_z_and_allows_overlap() -> None:
    editor = BoundedViewportEditor(_layout())

    result = editor.apply(ViewportResize(logical_slot=7, dwidth=500, dheight=500))
    changed = result.layout.by_slot()[7]

    assert (changed.x, changed.y, changed.z_index) == (17, 29, 2)
    assert (changed.width, changed.height) == (1113, 847)
    assert changed.x < result.layout.by_slot()[4095].x
    assert changed.x + changed.width > result.layout.by_slot()[4095].x


def test_editor_rejects_missing_slot_and_invalid_geometry_without_mutation() -> None:
    original = _layout()
    editor = BoundedViewportEditor(original)

    with pytest.raises(ViewportEditorError) as missing:
        editor.apply(ViewportMove(logical_slot=4000, dx=1, dy=1))
    assert missing.value.code == ViewportEditorErrorCode.SLOT_NOT_FOUND
    assert editor.snapshot.layout == original
    assert editor.snapshot.operations == 0

    with pytest.raises(ViewportEditorError) as invalid:
        editor.apply(ViewportResize(logical_slot=4095, dwidth=-211, dheight=0))
    assert invalid.value.code == ViewportEditorErrorCode.INVALID_EDIT
    assert editor.snapshot.layout == original
    assert editor.snapshot.operations == 0


def test_editor_enforces_operation_limit() -> None:
    editor = BoundedViewportEditor(_layout(), max_operations=1)
    editor.apply(ViewportMove(logical_slot=7, dx=1, dy=0))

    with pytest.raises(ViewportEditorError) as exc_info:
        editor.apply(ViewportMove(logical_slot=7, dx=1, dy=0))
    assert exc_info.value.code == ViewportEditorErrorCode.OPERATION_LIMIT
    assert editor.snapshot.operations == 1


def test_editor_snapshot_is_source_and_native_identity_free() -> None:
    editor = BoundedViewportEditor(_layout())
    editor.apply(ViewportMove(logical_slot=7, dx=3, dy=5))
    serialized = editor.snapshot.model_dump_json().casefold()

    for forbidden in (
        "rtsp://",
        "credential",
        "password",
        "source_uri",
        "source_id",
        "recording_id",
        "payload",
        "handle",
        "pointer",
    ):
        assert forbidden not in serialized


class _FakeLiveDelivery:
    async def run(self, _source_uri: str, _consumer: object) -> object:
        return object()


def _streams() -> tuple[MixedLiveStream, MixedLiveStream]:
    return (
        MixedLiveStream(slot=7, source_uri="execution-a", delivery=_FakeLiveDelivery()),
        MixedLiveStream(slot=4095, source_uri="execution-b", delivery=_FakeLiveDelivery()),
    )


class _FakeApplication:
    def __init__(self) -> None:
        self.state = WindowsOperatorApplicationState.READY
        self.generation = 0
        self.relayouts: list[ViewportLayout] = []
        self.wait_entered = asyncio.Event()
        self.wait_release = asyncio.Event()

    @property
    def snapshot(self) -> WindowsOperatorApplicationSnapshot:
        return WindowsOperatorApplicationSnapshot(
            state=self.state,
            shell_open=self.state != WindowsOperatorApplicationState.CLOSED,
            pump_cycles=0,
            pumped_messages=0,
            generation=self.generation,
            viewport_count=2 if self.generation else 0,
            open_surface_count=2 if self.state == WindowsOperatorApplicationState.RUNNING else 0,
            delivered_frames=0,
            presentations=0,
        )

    async def open(self, _width: int, _height: int) -> WindowsOperatorApplicationSnapshot:
        self.state = WindowsOperatorApplicationState.OPEN
        return self.snapshot

    async def start(self, _layout: ViewportLayout, _streams: object) -> WindowsOperatorApplicationSnapshot:
        self.generation = 1
        self.state = WindowsOperatorApplicationState.RUNNING
        return self.snapshot

    async def replace(self, _layout: ViewportLayout, _streams: object) -> WindowsOperatorApplicationSnapshot:
        self.generation += 1
        return self.snapshot

    async def relayout(self, layout: ViewportLayout) -> WindowsOperatorApplicationSnapshot:
        self.relayouts.append(layout)
        return self.snapshot

    async def wait(self) -> WindowsOperatorApplicationSnapshot:
        self.wait_entered.set()
        await self.wait_release.wait()
        self.state = WindowsOperatorApplicationState.COMPLETE
        return self.snapshot

    async def stop(self) -> WindowsOperatorApplicationSnapshot:
        self.state = WindowsOperatorApplicationState.STOPPED
        self.wait_release.set()
        return self.snapshot

    async def pump(self, *, max_messages: int = 64) -> WindowsOperatorApplicationSnapshot:
        await asyncio.sleep(0)
        return self.snapshot

    async def close(self) -> WindowsOperatorApplicationSnapshot:
        self.state = WindowsOperatorApplicationState.CLOSED
        self.wait_release.set()
        return self.snapshot


def test_editor_layout_flows_through_existing_control_relayout_without_restart() -> None:
    app = _FakeApplication()
    control = BoundedWindowsOperatorControl(
        application_factory=lambda: app,
        poll_interval_seconds=0,
        max_cycles=1000,
    )
    editor = BoundedViewportEditor(_layout())
    candidate = editor.apply(ViewportMove(logical_slot=7, dx=41, dy=73)).layout

    async def scenario() -> object:
        task = asyncio.create_task(
            control.run(width=1280, height=720, layout=_layout(), streams=_streams())
        )
        await app.wait_entered.wait()
        control.request_relayout(candidate)
        for _ in range(20):
            await asyncio.sleep(0)
            if app.relayouts:
                break
        assert app.relayouts == [candidate]
        assert app.generation == 1
        assert control.control_snapshot.active_layout == candidate
        assert control.control_snapshot.relayouts == 1
        assert control.control_snapshot.replacements == 0
        control.request_stop()
        return await task

    result = asyncio.run(scenario())
    assert result.active_layout == candidate
    assert result.relayouts == 1
    serialized = result.model_dump_json().casefold()
    assert "execution-a" not in serialized
    assert "execution-b" not in serialized
