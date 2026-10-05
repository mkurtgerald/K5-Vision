"""Direct Win32 qualification for the visible operator application shell."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Sequence

import pytest

from k5vision.media.mixed_presentation import MixedPresentationStream
from k5vision.media.presentation_frame import PixelFormat, PresentationVideoFrame
from k5vision.media.presentation_runtime import (
    PresentationRuntimeSnapshot,
    PresentationRuntimeState,
)
from k5vision.media.viewport_dispatch import ViewportBinding
from k5vision.media.viewport_geometry import ViewportGeometry, ViewportLayout, ViewportPlacement
from k5vision.media.windows_operator_application import (
    BoundedWindowsOperatorApplication,
    WindowsOperatorApplicationState,
)
from k5vision.media.windows_operator_host import BoundedWindowsOperatorHost
from k5vision.media.windows_operator_runtime import BoundedWindowsOperatorRuntime
from k5vision.media.windows_presentation_target import BoundedWindowsPresentationTarget
from k5vision.media.windows_viewport_layout import BoundedWindowsViewportLayout
from k5vision.media.windows_viewport_runtime import BoundedWindowsViewportRuntime

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only qualification")


def _presentation_snapshot(
    state: PresentationRuntimeState,
    delivered_frames: int = 0,
) -> PresentationRuntimeSnapshot:
    return PresentationRuntimeSnapshot(
        state=state,
        stream_count=2,
        live_streams=1,
        playback_streams=1,
        viewport_count=2,
        completed_streams=2 if state == PresentationRuntimeState.COMPLETE else 0,
        delivered_frames=delivered_frames,
        delivered_frame_bytes=delivered_frames * 16,
        max_source_span_ms=1 if delivered_frames else 0,
    )


class SyntheticPresentationRuntime:
    """Synthetic media boundary driving real child Win32 viewport targets."""

    def __init__(self, bindings: Sequence[ViewportBinding]) -> None:
        self._bindings = tuple(bindings)
        self._snapshot = _presentation_snapshot(PresentationRuntimeState.CREATED)

    @property
    def snapshot(self) -> PresentationRuntimeSnapshot:
        return self._snapshot

    async def start(
        self,
        _streams: Sequence[MixedPresentationStream],
    ) -> PresentationRuntimeSnapshot:
        frame = PresentationVideoFrame(
            payload=memoryview(bytearray([1, 2, 3, 0] * 4)),
            width=2,
            height=2,
            stride_bytes=8,
            pixel_format=PixelFormat.BGRX,
            source_elapsed_ms=1,
        )
        selected = {binding.slot: binding for binding in self._bindings}
        await selected[4095].consumer(frame)
        self._snapshot = _presentation_snapshot(
            PresentationRuntimeState.RUNNING,
            delivered_frames=1,
        )
        return self._snapshot

    async def wait(self) -> PresentationRuntimeSnapshot:
        self._snapshot = _presentation_snapshot(
            PresentationRuntimeState.COMPLETE,
            delivered_frames=1,
        )
        return self._snapshot

    async def stop(self) -> PresentationRuntimeSnapshot:
        self._snapshot = _presentation_snapshot(
            PresentationRuntimeState.STOPPED,
            delivered_frames=1,
        )
        return self._snapshot

    async def close(self) -> PresentationRuntimeSnapshot:
        self._snapshot = _presentation_snapshot(
            PresentationRuntimeState.CLOSED,
            delivered_frames=1,
        )
        return self._snapshot


def _layout(offset: int) -> ViewportLayout:
    return ViewportLayout(
        placements=(
            ViewportPlacement(
                logical_slot=7,
                geometry=ViewportGeometry(
                    x=17 + offset,
                    y=29 + offset,
                    width=83,
                    height=47,
                ),
            ),
            ViewportPlacement(
                logical_slot=4095,
                geometry=ViewportGeometry(
                    x=173 + offset,
                    y=83 + offset,
                    width=67,
                    height=53,
                ),
            ),
        )
    )


def test_real_win32_application_hosts_and_replaces_sparse_child_layouts() -> None:
    parent_handles: list[int] = []

    def host_factory(parent_handle: int) -> BoundedWindowsOperatorHost:
        parent_handles.append(parent_handle)

        def target_factory() -> BoundedWindowsPresentationTarget:
            return BoundedWindowsPresentationTarget(parent_handle=parent_handle)

        def layout_factory(layout: ViewportLayout) -> BoundedWindowsViewportLayout:
            return BoundedWindowsViewportLayout(layout, target_factory=target_factory)

        def windows_runtime_factory(layout: ViewportLayout) -> BoundedWindowsViewportRuntime:
            return BoundedWindowsViewportRuntime(layout, layout_factory=layout_factory)

        def runtime_factory(layout: ViewportLayout) -> BoundedWindowsOperatorRuntime:
            return BoundedWindowsOperatorRuntime(
                layout,
                windows_runtime_factory=windows_runtime_factory,
                presentation_runtime_factory=SyntheticPresentationRuntime,
            )

        return BoundedWindowsOperatorHost(runtime_factory=runtime_factory)

    async def scenario() -> None:
        app = BoundedWindowsOperatorApplication(host_factory=host_factory)

        opened = await app.open(640, 480)
        assert opened.state == WindowsOperatorApplicationState.OPEN
        assert opened.shell_open is True

        first = await app.start(_layout(0), ())
        assert first.state == WindowsOperatorApplicationState.RUNNING
        assert first.generation == 1
        assert first.viewport_count == 2
        assert first.open_surface_count == 2
        assert first.presentations == 1

        pumped = await app.pump(max_messages=32)
        assert pumped.shell_open is True
        assert pumped.pump_cycles == 1

        second = await app.replace(_layout(40), ())
        assert second.state == WindowsOperatorApplicationState.RUNNING
        assert second.generation == 2
        assert second.open_surface_count == 2
        assert second.presentations == 2
        assert len(parent_handles) == 1

        stopped = await app.stop()
        assert stopped.state == WindowsOperatorApplicationState.STOPPED
        assert stopped.shell_open is True
        assert stopped.open_surface_count == 0

        closed = await app.close()
        assert closed.state == WindowsOperatorApplicationState.CLOSED
        assert closed.shell_open is False
        assert closed.open_surface_count == 0
        assert closed.presentations == 2

    asyncio.run(scenario())


def test_real_shell_settled_resize_preserves_two_owned_camera_tiles(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Generated owned HWNDs only; geometry and scalar colors, never desktop capture.

    A readable, unobscured client DC is required. This test's disposable shell is
    topmost; occlusion or unavailable GetPixel fails, with no screenshot fallback.
    Programmatic settled resizing does not claim playback during native border drag.
    """
    import ctypes
    import hashlib
    import importlib
    import json
    import threading
    from pathlib import Path

    import k5vision
    from k5vision.media.windows_operator_application import _ShellRect, _Win32Point
    from k5vision.media.windows_operator_interaction import (
        BoundedInteractiveWindowsOperatorApplication,
        _InteractiveWin32OperatorShellApi,
    )
    from k5vision.media.windows_presentation_target import _Win32WindowTargetApi

    reviewed = {
        "viewport_client_projection": (
            "a9969c9c6a99c8d6a480ee9d48c18a22adb50d7e460b5ecd2dde7f8f8ef7d1ba"
        ),
        "windows_operator_application": (
            "e43eb21fe8fb065c16daf998675586b64e3c6997e0b75d3009abcca67efe581c"
        ),
        "windows_operator_interaction": (
            "86380c57e93806ea288edbeab8c5c699e249485217715bb2e1862216c18e9fba"
        ),
        "windows_operator_catalog_ui": (
            "44ee120191597e38c8b827876a678b3fe376d38bf383ff36c560c5c87e5c8bc0"
        ),
        "windows_operator_catalog_overlay": (
            "70f14b993d1d5ae29377af8d452bffa0b38b70a437838ab2947593d81ca83e25"
        ),
    }
    package = Path(k5vision.__file__).resolve().parent
    for name, expected in reviewed.items():
        module = importlib.import_module("k5vision.media." + name)
        path = Path(module.__file__).resolve()
        assert path == package / "media" / (name + ".py")
        assert hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest() == expected
    product_digest = hashlib.sha256(
        "\n".join(f"{name}:{sha}" for name, sha in sorted(reviewed.items())).encode()
    ).hexdigest()
    owner_thread = threading.get_ident()

    def same_thread() -> None:
        if threading.get_ident() != owner_thread:
            raise AssertionError("owned shell operation changed thread")

    class OwnedShell(_InteractiveWin32OperatorShellApi):
        def __init__(self) -> None:
            super().__init__()
            self.owned = 0
            self.minimum_calls = 0
            for name, args, result in (
                ("ShowWindow", [ctypes.c_void_p, ctypes.c_int], ctypes.c_int),
                ("IsWindow", [ctypes.c_void_p], ctypes.c_int),
                ("GetParent", [ctypes.c_void_p], ctypes.c_void_p),
                ("ClientToScreen", [ctypes.c_void_p, ctypes.POINTER(_Win32Point)], ctypes.c_int),
            ):
                function = getattr(self._user32, name)
                function.argtypes, function.restype = args, result

        def require_owned(self, shell: int) -> None:
            same_thread()
            if shell <= 0 or shell != self.owned:
                raise AssertionError("shell witness refused an unowned HWND")
            if not self._user32.IsWindow(ctypes.c_void_p(shell)):
                raise AssertionError("owned shell is unavailable")

        def create_shell(self, width: int, height: int) -> int:
            same_thread()
            if self.owned:
                raise AssertionError("shell witness already owns a window")
            self.owned = super().create_shell(width, height)
            return self.owned

        def client_size(self, shell: int) -> tuple[int, int]:
            self.require_owned(shell)
            return super().client_size(shell)

        def ensure_client_size(self, shell: int, width: int, height: int) -> tuple[int, int]:
            self.require_owned(shell)
            self.minimum_calls += 1
            return super().ensure_client_size(shell, width, height)

        def pump_messages(self, shell: int, max_messages: int) -> tuple[int, bool]:
            self.require_owned(shell)
            return super().pump_messages(shell, max_messages)

        def destroy_shell(self, shell: int) -> None:
            self.require_owned(shell)
            super().destroy_shell(shell)
            if self._user32.IsWindow(ctypes.c_void_p(shell)):
                raise AssertionError("owned shell cleanup is incomplete")
            self.owned = 0

        def resize_owned_client(self, width: int, height: int) -> None:
            self.require_owned(self.owned)
            assert 1 <= width <= 480 and 1 <= height <= 300
            current_width, current_height = self.client_size(self.owned)
            assert current_width and current_height
            outer = _ShellRect()
            assert self._get_window_rect(ctypes.c_void_p(self.owned), ctypes.byref(outer))
            # Independently convert desired client dimensions using measured chrome.
            assert self._set_window_pos(
                ctypes.c_void_p(self.owned),
                ctypes.c_void_p(-1),
                17,
                29,
                width + outer.right - outer.left - current_width,
                height + outer.bottom - outer.top - current_height,
                0x0010,  # NOACTIVATE; only this owned generated shell becomes topmost.
            )
            assert self.client_size(self.owned) == (width, height)

        def minimize(self, minimized: bool) -> None:
            self.require_owned(self.owned)
            self._user32.ShowWindow(ctypes.c_void_p(self.owned), 6 if minimized else 9)
            assert bool(self._is_iconic(ctypes.c_void_p(self.owned))) == minimized

    shell = OwnedShell()
    targets: list[OwnedTarget] = []

    class OwnedTarget(_Win32WindowTargetApi):
        def __init__(self, parent: int) -> None:
            shell.require_owned(parent)
            super().__init__(parent_handle=parent)
            self.owned = 0
            self.dcs: set[int] = set()
            self.acquired = self.released = 0
            gdi = ctypes.WinDLL("gdi32", use_last_error=True)
            self.get_pixel = gdi.GetPixel
            self.get_pixel.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
            self.get_pixel.restype = ctypes.c_uint32
            self.flush = gdi.GdiFlush
            self.flush.argtypes, self.flush.restype = [], ctypes.c_int

        def require_owned(self, target: int) -> None:
            shell.require_owned(shell.owned)
            if target <= 0 or target != self.owned:
                raise AssertionError("shell witness refused an unowned target")
            if int(shell._user32.GetParent(ctypes.c_void_p(target)) or 0) != shell.owned:
                raise AssertionError("owned target parent differs")

        def create_target(self, x: int, y: int, width: int, height: int) -> int:
            same_thread()
            if self.owned:
                raise AssertionError("shell witness already owns a target")
            self.owned = super().create_target(x, y, width, height)
            return self.owned

        def acquire_dc(self, target: int) -> int:
            self.require_owned(target)
            if self.dcs:
                raise AssertionError("owned target already owns a DC")
            dc = super().acquire_dc(target)
            self.dcs.add(dc)
            self.acquired += 1
            return dc

        def release_dc(self, target: int, dc: int) -> None:
            self.require_owned(target)
            if dc not in self.dcs:
                raise AssertionError("shell witness refused an unowned DC")
            super().release_dc(target, dc)
            self.dcs.remove(dc)
            self.released += 1

        def destroy_target(self, target: int) -> None:
            self.require_owned(target)
            if self.dcs:
                raise AssertionError("owned target still owns a DC")
            super().destroy_target(target)
            if shell._user32.IsWindow(ctypes.c_void_p(target)):
                raise AssertionError("owned target cleanup is incomplete")
            self.owned = 0

        def geometry(self) -> tuple[int, int, int, int]:
            self.require_owned(self.owned)
            outer, origin = _ShellRect(), _Win32Point(0, 0)
            assert shell._get_window_rect(ctypes.c_void_p(self.owned), ctypes.byref(outer))
            assert shell._user32.ClientToScreen(ctypes.c_void_p(shell.owned), ctypes.byref(origin))
            width, height = self.client_size(self.owned)
            assert (outer.right - outer.left, outer.bottom - outer.top) == (width, height)
            assert 1 <= width <= 240 and 1 <= height <= 300
            return outer.left - origin.x, outer.top - origin.y, width, height

        def scalar_colors(self) -> tuple[int, int]:
            _, _, width, height = self.geometry()
            dc = self.acquire_dc(self.owned)
            try:
                assert self.flush()
                # Center is the generated camera color; corner must be black letterbox.
                center = int(self.get_pixel(ctypes.c_void_p(dc), width // 2, height // 2))
                corner = int(self.get_pixel(ctypes.c_void_p(dc), 0, 0))
                assert center != 0xFFFFFFFF and corner != 0xFFFFFFFF
                return center, corner
            finally:
                self.release_dc(self.owned, dc)

    class SteppedPresentation(SyntheticPresentationRuntime):
        async def start(self, streams):
            assert streams == ()
            self._snapshot = _presentation_snapshot(PresentationRuntimeState.RUNNING)
            return self._snapshot

        async def emit_pair(self) -> tuple[int, int]:
            same_thread()
            sequence = self.snapshot.delivered_frames // 2 + 1
            colors = (32 + sequence, 96 + sequence)
            selected = {binding.slot: binding for binding in self._bindings}
            assert set(selected) == {7, 4095}
            for slot, gray in zip((7, 4095), colors, strict=True):
                frame = PresentationVideoFrame(
                    payload=memoryview(bytes((gray, gray, gray, 0)) * (192 * 108)),
                    width=192,
                    height=108,
                    stride_bytes=192 * 4,
                    pixel_format=PixelFormat.BGRX,
                    source_elapsed_ms=sequence,
                )
                await selected[slot].consumer(frame)
            total = self.snapshot.delivered_frames + 2
            self._snapshot = self.snapshot.model_copy(
                update={
                    "delivered_frames": total,
                    "delivered_frame_bytes": total * 192 * 108 * 4,
                    "max_source_span_ms": sequence,
                }
            )
            return colors

        async def stop(self):
            self._snapshot = self.snapshot.model_copy(
                update={"state": PresentationRuntimeState.STOPPED}
            )
            return self._snapshot

        async def close(self):
            self._snapshot = self.snapshot.model_copy(
                update={"state": PresentationRuntimeState.CLOSED}
            )
            return self._snapshot

    presentations: list[SteppedPresentation] = []
    windows: list[BoundedWindowsViewportRuntime] = []

    def host_factory(parent: int) -> BoundedWindowsOperatorHost:
        shell.require_owned(parent)

        def target_factory():
            native = OwnedTarget(parent)
            targets.append(native)
            return BoundedWindowsPresentationTarget(native_api=native, parent_handle=parent)

        def windows_factory(layout):
            result = BoundedWindowsViewportRuntime(
                layout,
                layout_factory=lambda selected: BoundedWindowsViewportLayout(
                    selected,
                    target_factory=target_factory,
                ),
            )
            windows.append(result)
            return result

        def presentation_factory(bindings):
            result = SteppedPresentation(bindings)
            presentations.append(result)
            return result

        return BoundedWindowsOperatorHost(
            runtime_factory=lambda layout: BoundedWindowsOperatorRuntime(
                layout,
                windows_runtime_factory=windows_factory,
                presentation_runtime_factory=presentation_factory,
            )
        )

    async def scenario() -> dict[str, object]:
        app = BoundedInteractiveWindowsOperatorApplication(
            native_api=shell, host_factory=host_factory
        )
        canonical = ViewportLayout(
            placements=tuple(
                ViewportPlacement(
                    logical_slot=slot,
                    geometry=ViewportGeometry(
                        x=index * 320,
                        y=0,
                        width=320,
                        height=480,
                    ),
                )
                for index, slot in enumerate((7, 4095))
            )
        )
        before = canonical.model_dump_json()
        cases = []
        try:
            await app.open(640, 480)
            shell.resize_owned_client(384, 240)
            await app.start(canonical, ())
            assert len(presentations) == len(windows) == 1
            surface_identity = dict(windows[0]._surfaces)

            async def settled(name, requested, expected):
                prior = app.snapshot
                shell.resize_owned_client(*requested)
                await app.pump(max_messages=256)
                assert shell.client_size(shell.owned) == expected
                assert app.snapshot.delivered_frames == prior.delivered_frames
                assert app.snapshot.presentations == prior.presentations
                assert app.snapshot.generation == 1
                assert dict(windows[0]._surfaces) == surface_identity
                assert len(presentations) == len(windows) == 1
                assert canonical.model_dump_json() == before
                count = len(targets)
                for _ in range(3):
                    await app.pump(max_messages=256)
                assert len(targets) == count  # no resize/relayout loop
                active = sorted(
                    (target for target in targets if target.owned), key=lambda t: t.geometry()[0]
                )
                width, height = expected
                geometry = [target.geometry() for target in active]
                # Independent equal-tile oracle, not the production projection helper.
                assert geometry == [
                    (0, 0, width // 2, height),
                    (width // 2, 0, width - width // 2, height),
                ]
                colors = await presentations[0].emit_pair()
                for target, gray in zip(active, colors, strict=True):
                    assert target.scalar_colors() == (gray * 0x010101, 0)
                cases.append(
                    {
                        "case": name,
                        "client": list(expected),
                        "children": geometry,
                        "matched_camera_colors": 2,
                        "black_corners": 2,
                    }
                )

            await settled("initial", (384, 240), (384, 240))
            await settled("shrink", (256, 192), (256, 192))
            await settled("grow", (480, 300), (480, 300))
            # Win32 enforces its own sizing-border minimum before our pump; the
            # hosted runner clamps a 100-pixel request to 120. Use a reachable
            # client below our 192-by-192 minimum, keeping exact pre/post checks.
            await settled("minimum_clamp", (160, 120), (192, 192))
            assert shell.minimum_calls == 1
            prior = app.snapshot
            target_count = len(targets)
            acquired = sum(target.acquired for target in targets)
            shell.minimize(True)
            for _ in range(3):
                await app.pump(max_messages=256)
                await presentations[0].emit_pair()
                assert app.snapshot.state == WindowsOperatorApplicationState.RUNNING
                assert app.snapshot.presentations == prior.presentations
                assert shell.client_size(shell.owned) == (0, 0)
            assert app.snapshot.delivered_frames == prior.delivered_frames + 6
            assert len(targets) == target_count
            assert sum(target.acquired for target in targets) == acquired
            shell.minimize(False)
            await settled("restore", (384, 240), (384, 240))
            assert app.snapshot.delivered_frames == 16
            assert app.snapshot.presentations == 10
            assert (
                sum(surface.snapshot.presented_frames for surface in surface_identity.values())
                == 16
            )
            assert windows[0].snapshot.presentations == 10
        finally:
            await app.close()
        if shell.owned or any(t.owned or t.dcs or t.acquired != t.released for t in targets):
            raise AssertionError("shell witness resource cleanup is incomplete")
        assert app.snapshot.open_surface_count == 0
        assert app.snapshot.state == WindowsOperatorApplicationState.CLOSED
        assert app.snapshot.delivered_frames == 16 and app.snapshot.presentations == 10
        return {
            "schema_version": "1",
            "qualification": "owned-generated-settled-shell-resize",
            "base_revision": "bde2e3d327eca1f1a4b78af6eeb491b68adc80b5",
            "product_source_sha256": product_digest,
            "cases": cases,
            "consumed_frames": 16,
            "painted_frames": 10,
            "deferred_frames": 6,
            "media_generations": 1,
            "minimum_clamps": shell.minimum_calls,
            "targets_created": len(targets),
            "targets_destroyed": len(targets),
            "dc_acquisitions": sum(t.acquired for t in targets),
            "dc_releases": sum(t.released for t in targets),
            "cleanup_complete": True,
        }

    receipt = asyncio.run(scenario())
    with capsys.disabled():
        print("K5_NATIVE_SETTLED_SHELL_RECEIPT=" + json.dumps(receipt, sort_keys=True))
