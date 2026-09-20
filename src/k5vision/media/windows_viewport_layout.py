"""Bounded composition of arbitrary viewport layouts onto Windows presentation targets."""

from __future__ import annotations

import asyncio
import enum
import typing
from collections.abc import Callable

from pydantic import BaseModel, ConfigDict, Field

from k5vision.media.viewport_geometry import ViewportLayout
from k5vision.media.windows_presentation_target import BoundedWindowsPresentationTarget

_MAX_TARGETS = 64
_MAX_PRESENTATIONS = 1_000_000


class WindowsViewportLayoutState(enum.StrEnum):
    READY = "ready"
    OPEN = "open"
    FAILED = "failed"
    CLOSED = "closed"


class WindowsViewportLayoutErrorCode(enum.StrEnum):
    INVALID_CONFIGURATION = "invalid_configuration"
    INVALID_STATE = "invalid_state"
    TARGET_OPEN_FAILURE = "target_open_failure"
    UNKNOWN_SLOT = "unknown_slot"
    PRESENTATION_FAILURE = "presentation_failure"
    CLEANUP_FAILURE = "cleanup_failure"


class WindowsViewportLayoutError(RuntimeError):
    """Sanitized layout-to-target failure."""

    def __init__(self, code: WindowsViewportLayoutErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class WindowsViewportLayoutSnapshot(BaseModel):
    """Source/media/native-identity-free aggregate layout observability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: typing.Literal["1"] = "1"
    state: WindowsViewportLayoutState
    target_count: int = Field(ge=0, le=_MAX_TARGETS)
    open_target_count: int = Field(ge=0, le=_MAX_TARGETS)
    presentations: int = Field(ge=0, le=_MAX_PRESENTATIONS)


@typing.runtime_checkable
class _TargetBoundary(typing.Protocol):
    async def open(
        self,
        width: int,
        height: int,
        *,
        x: int = 0,
        y: int = 0,
    ) -> object: ...

    async def present(self, surface: object) -> object: ...

    async def close(self) -> object: ...


TargetFactory = Callable[[], _TargetBoundary]


class BoundedWindowsViewportLayout:
    """Open, route and close positioned targets for one immutable viewport layout."""

    def __init__(
        self,
        layout: ViewportLayout,
        *,
        target_factory: TargetFactory | None = None,
        max_presentations: int = 100_000,
    ) -> None:
        if not isinstance(layout, ViewportLayout):
            raise WindowsViewportLayoutError(
                WindowsViewportLayoutErrorCode.INVALID_CONFIGURATION,
                "viewport layout is invalid",
            )
        if not 1 <= len(layout.placements) <= _MAX_TARGETS:
            raise WindowsViewportLayoutError(
                WindowsViewportLayoutErrorCode.INVALID_CONFIGURATION,
                "viewport layout target count is invalid",
            )
        if not 1 <= max_presentations <= _MAX_PRESENTATIONS:
            raise ValueError("max_presentations must be between 1 and 1000000")
        self._layout = layout
        self._target_factory = target_factory or BoundedWindowsPresentationTarget
        self._max_presentations = max_presentations
        self._state = WindowsViewportLayoutState.READY
        self._targets: dict[int, _TargetBoundary] = {}
        self._presentations = 0
        self._lock = asyncio.Lock()

    @property
    def snapshot(self) -> WindowsViewportLayoutSnapshot:
        return WindowsViewportLayoutSnapshot(
            state=self._state,
            target_count=len(self._layout.placements),
            open_target_count=len(self._targets),
            presentations=self._presentations,
        )

    async def _close_targets(self) -> bool:
        failed = False
        targets = tuple(self._targets.values())
        self._targets.clear()
        for target in reversed(targets):
            try:
                await target.close()
            except asyncio.CancelledError:
                failed = True
            except Exception:
                failed = True
        return failed

    async def open(self) -> WindowsViewportLayoutSnapshot:
        """Open one positioned target per logical placement, failing closed on partial open."""
        async with self._lock:
            if self._state == WindowsViewportLayoutState.OPEN:
                return self.snapshot
            if self._state != WindowsViewportLayoutState.READY:
                raise WindowsViewportLayoutError(
                    WindowsViewportLayoutErrorCode.INVALID_STATE,
                    "viewport target layout cannot open from current state",
                )

            for placement in self._layout.placements:
                try:
                    target = self._target_factory()
                    if not isinstance(target, _TargetBoundary):
                        raise TypeError("invalid target boundary")
                    self._targets[placement.logical_slot] = target
                    geometry = placement.geometry
                    await target.open(
                        geometry.width,
                        geometry.height,
                        x=geometry.x,
                        y=geometry.y,
                    )
                except asyncio.CancelledError:
                    await self._close_targets()
                    self._state = WindowsViewportLayoutState.FAILED
                    raise
                except Exception:
                    await self._close_targets()
                    self._state = WindowsViewportLayoutState.FAILED
                    raise WindowsViewportLayoutError(
                        WindowsViewportLayoutErrorCode.TARGET_OPEN_FAILURE,
                        "viewport target layout open failed",
                    ) from None

            self._state = WindowsViewportLayoutState.OPEN
            return self.snapshot

    async def present(self, logical_slot: int, surface: object) -> WindowsViewportLayoutSnapshot:
        """Route one accepted surface to its logical positioned target."""
        async with self._lock:
            if self._state != WindowsViewportLayoutState.OPEN:
                raise WindowsViewportLayoutError(
                    WindowsViewportLayoutErrorCode.INVALID_STATE,
                    "viewport target layout is not open",
                )
            target = self._targets.get(logical_slot)
            if target is None:
                raise WindowsViewportLayoutError(
                    WindowsViewportLayoutErrorCode.UNKNOWN_SLOT,
                    "viewport logical slot is not open",
                )
            if self._presentations >= self._max_presentations:
                await self._close_targets()
                self._state = WindowsViewportLayoutState.FAILED
                raise WindowsViewportLayoutError(
                    WindowsViewportLayoutErrorCode.PRESENTATION_FAILURE,
                    "viewport target layout presentation limit exceeded",
                )
            try:
                await target.present(surface)
            except asyncio.CancelledError:
                await self._close_targets()
                self._state = WindowsViewportLayoutState.FAILED
                raise
            except Exception:
                await self._close_targets()
                self._state = WindowsViewportLayoutState.FAILED
                raise WindowsViewportLayoutError(
                    WindowsViewportLayoutErrorCode.PRESENTATION_FAILURE,
                    "viewport target layout presentation failed",
                ) from None
            self._presentations += 1
            return self.snapshot

    async def close(self) -> WindowsViewportLayoutSnapshot:
        """Close every target deterministically, attempting all cleanup paths."""
        async with self._lock:
            if self._state == WindowsViewportLayoutState.CLOSED:
                return self.snapshot
            failed = await self._close_targets()
            if failed:
                self._state = WindowsViewportLayoutState.FAILED
                raise WindowsViewportLayoutError(
                    WindowsViewportLayoutErrorCode.CLEANUP_FAILURE,
                    "viewport target layout cleanup failed",
                )
            self._state = WindowsViewportLayoutState.CLOSED
            return self.snapshot
