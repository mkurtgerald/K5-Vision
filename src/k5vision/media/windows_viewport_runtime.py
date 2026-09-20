"""Bounded bridge from renderer-neutral viewport dispatch to positioned Windows targets."""

from __future__ import annotations

import asyncio
import enum
import typing
from collections.abc import Callable

from pydantic import BaseModel, ConfigDict, Field

from k5vision.media.presentation_frame import PresentationVideoFrame
from k5vision.media.viewport_dispatch import ViewportBinding
from k5vision.media.viewport_geometry import ViewportLayout
from k5vision.media.windows_presentation_surface import BoundedWindowsPresentationSurface
from k5vision.media.windows_viewport_layout import BoundedWindowsViewportLayout

_MAX_VIEWPORTS = 64
_MAX_PRESENTATIONS = 1_000_000


class WindowsViewportRuntimeState(enum.StrEnum):
    READY = "ready"
    OPEN = "open"
    FAILED = "failed"
    CLOSED = "closed"


class WindowsViewportRuntimeErrorCode(enum.StrEnum):
    INVALID_CONFIGURATION = "invalid_configuration"
    INVALID_STATE = "invalid_state"
    SURFACE_OPEN_FAILURE = "surface_open_failure"
    LAYOUT_OPEN_FAILURE = "layout_open_failure"
    UNKNOWN_SLOT = "unknown_slot"
    PRESENTATION_LIMIT = "presentation_limit"
    PRESENTATION_FAILURE = "presentation_failure"
    CLEANUP_FAILURE = "cleanup_failure"


class WindowsViewportRuntimeError(RuntimeError):
    """Sanitized viewport runtime failure."""

    def __init__(self, code: WindowsViewportRuntimeErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class WindowsViewportRuntimeSnapshot(BaseModel):
    """Aggregate source/media/path/native-identity-free runtime observability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: typing.Literal["1"] = "1"
    state: WindowsViewportRuntimeState
    viewport_count: int = Field(ge=0, le=_MAX_VIEWPORTS)
    open_surface_count: int = Field(ge=0, le=_MAX_VIEWPORTS)
    presentations: int = Field(ge=0, le=_MAX_PRESENTATIONS)


@typing.runtime_checkable
class _SurfaceBoundary(typing.Protocol):
    async def open(self) -> object: ...

    async def present(self, frame: PresentationVideoFrame) -> None: ...

    async def close(self) -> object: ...


@typing.runtime_checkable
class _LayoutBoundary(typing.Protocol):
    async def open(self) -> object: ...

    async def present(self, logical_slot: int, surface: object) -> object: ...

    async def close(self) -> object: ...


SurfaceFactory = Callable[[], _SurfaceBoundary]
LayoutFactory = Callable[[ViewportLayout], _LayoutBoundary]


class BoundedWindowsViewportRuntime:
    """Own per-slot Windows surfaces and bind them to one arbitrary positioned layout."""

    def __init__(
        self,
        layout: ViewportLayout,
        *,
        surface_factory: SurfaceFactory | None = None,
        layout_factory: LayoutFactory | None = None,
        max_presentations: int = 100_000,
    ) -> None:
        if not isinstance(layout, ViewportLayout):
            raise WindowsViewportRuntimeError(
                WindowsViewportRuntimeErrorCode.INVALID_CONFIGURATION,
                "viewport runtime layout is invalid",
            )
        if not 1 <= len(layout.placements) <= _MAX_VIEWPORTS:
            raise WindowsViewportRuntimeError(
                WindowsViewportRuntimeErrorCode.INVALID_CONFIGURATION,
                "viewport runtime layout count is invalid",
            )
        if not 1 <= max_presentations <= _MAX_PRESENTATIONS:
            raise ValueError("max_presentations must be between 1 and 1000000")

        self._layout = layout
        self._surface_factory = surface_factory or BoundedWindowsPresentationSurface
        self._layout_factory = layout_factory or BoundedWindowsViewportLayout
        self._max_presentations = max_presentations
        self._state = WindowsViewportRuntimeState.READY
        self._surfaces: dict[int, _SurfaceBoundary] = {}
        self._target_layout: _LayoutBoundary | None = None
        self._presentations = 0
        self._lock = asyncio.Lock()

    @property
    def snapshot(self) -> WindowsViewportRuntimeSnapshot:
        return WindowsViewportRuntimeSnapshot(
            state=self._state,
            viewport_count=len(self._layout.placements),
            open_surface_count=len(self._surfaces),
            presentations=self._presentations,
        )

    def _consumer_for(self, logical_slot: int) -> Callable[[PresentationVideoFrame], typing.Awaitable[None]]:
        async def consume(frame: PresentationVideoFrame) -> None:
            await self.present(logical_slot, frame)

        return consume

    @property
    def bindings(self) -> tuple[ViewportBinding, ...]:
        """Return renderer-neutral dispatcher bindings only while the runtime is open."""
        if self._state != WindowsViewportRuntimeState.OPEN:
            raise WindowsViewportRuntimeError(
                WindowsViewportRuntimeErrorCode.INVALID_STATE,
                "viewport runtime bindings are unavailable",
            )
        return tuple(
            ViewportBinding(
                slot=placement.logical_slot,
                consumer=self._consumer_for(placement.logical_slot),
            )
            for placement in self._layout.placements
        )

    async def _close_resources(self) -> bool:
        failed = False
        target_layout = self._target_layout
        self._target_layout = None
        if target_layout is not None:
            try:
                await target_layout.close()
            except asyncio.CancelledError:
                failed = True
            except Exception:
                failed = True

        surfaces = tuple(self._surfaces.values())
        self._surfaces.clear()
        for surface in reversed(surfaces):
            try:
                await surface.close()
            except asyncio.CancelledError:
                failed = True
            except Exception:
                failed = True
        return failed

    async def _fail_closed(self) -> None:
        await self._close_resources()
        self._state = WindowsViewportRuntimeState.FAILED

    async def open(self) -> WindowsViewportRuntimeSnapshot:
        """Open all per-slot surfaces and the positioned target layout."""
        async with self._lock:
            if self._state == WindowsViewportRuntimeState.OPEN:
                return self.snapshot
            if self._state != WindowsViewportRuntimeState.READY:
                raise WindowsViewportRuntimeError(
                    WindowsViewportRuntimeErrorCode.INVALID_STATE,
                    "viewport runtime cannot open from current state",
                )

            for placement in self._layout.placements:
                try:
                    surface = self._surface_factory()
                    if not isinstance(surface, _SurfaceBoundary):
                        raise TypeError("invalid surface boundary")
                    self._surfaces[placement.logical_slot] = surface
                    await surface.open()
                except asyncio.CancelledError:
                    await self._fail_closed()
                    raise
                except Exception:
                    await self._fail_closed()
                    raise WindowsViewportRuntimeError(
                        WindowsViewportRuntimeErrorCode.SURFACE_OPEN_FAILURE,
                        "viewport runtime surface open failed",
                    ) from None

            try:
                target_layout = self._layout_factory(self._layout)
                if not isinstance(target_layout, _LayoutBoundary):
                    raise TypeError("invalid layout boundary")
                self._target_layout = target_layout
                await target_layout.open()
            except asyncio.CancelledError:
                await self._fail_closed()
                raise
            except Exception:
                await self._fail_closed()
                raise WindowsViewportRuntimeError(
                    WindowsViewportRuntimeErrorCode.LAYOUT_OPEN_FAILURE,
                    "viewport runtime target layout open failed",
                ) from None

            self._state = WindowsViewportRuntimeState.OPEN
            return self.snapshot

    async def present(
        self,
        logical_slot: int,
        frame: PresentationVideoFrame,
    ) -> WindowsViewportRuntimeSnapshot:
        """Copy one transient frame into its slot surface and present it to the positioned target."""
        async with self._lock:
            if self._state != WindowsViewportRuntimeState.OPEN:
                raise WindowsViewportRuntimeError(
                    WindowsViewportRuntimeErrorCode.INVALID_STATE,
                    "viewport runtime is not open",
                )
            surface = self._surfaces.get(logical_slot)
            if surface is None:
                raise WindowsViewportRuntimeError(
                    WindowsViewportRuntimeErrorCode.UNKNOWN_SLOT,
                    "viewport runtime slot is not open",
                )
            if self._presentations >= self._max_presentations:
                await self._fail_closed()
                raise WindowsViewportRuntimeError(
                    WindowsViewportRuntimeErrorCode.PRESENTATION_LIMIT,
                    "viewport runtime presentation limit exceeded",
                )
            target_layout = self._target_layout
            if target_layout is None:
                await self._fail_closed()
                raise WindowsViewportRuntimeError(
                    WindowsViewportRuntimeErrorCode.PRESENTATION_FAILURE,
                    "viewport runtime target layout is unavailable",
                )

            try:
                await surface.present(frame)
                await target_layout.present(logical_slot, surface)
            except asyncio.CancelledError:
                await self._fail_closed()
                raise
            except Exception:
                await self._fail_closed()
                raise WindowsViewportRuntimeError(
                    WindowsViewportRuntimeErrorCode.PRESENTATION_FAILURE,
                    "viewport runtime presentation failed",
                ) from None

            self._presentations += 1
            return self.snapshot

    async def close(self) -> WindowsViewportRuntimeSnapshot:
        """Close the positioned layout and every per-slot surface, attempting all cleanup paths."""
        async with self._lock:
            if self._state == WindowsViewportRuntimeState.CLOSED:
                return self.snapshot
            failed = await self._close_resources()
            if failed:
                self._state = WindowsViewportRuntimeState.FAILED
                raise WindowsViewportRuntimeError(
                    WindowsViewportRuntimeErrorCode.CLEANUP_FAILURE,
                    "viewport runtime cleanup failed",
                )
            self._state = WindowsViewportRuntimeState.CLOSED
            return self.snapshot
