"""Generated decode/native presentation Pause witness, invoked by the selected gate test.

The RTP envelope and BGRX frames are generated here. No codec, live source, browser
UI, installed application, pixels-readback, or external-media acceptance is claimed.
Only source-free timestamps/counters survive in the receipt. Portable callers must
inject BOTH native APIs and their receipt is explicitly source-contract-only.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from k5vision.domain.devices import Device, DeviceCreate, DeviceKind, DeviceProtocol
from k5vision.domain.users import UserAccount, UserRole
from k5vision.media.framed_recording import FramedAtomicRecordingSink
from k5vision.media.pausable_presentation_playback import PausablePresentationPlaybackDelivery
from k5vision.media.playback_control import PlaybackControlState
from k5vision.media.presentation_frame import PixelFormat, PresentationVideoFrame
from k5vision.media.presentation_playback import PresentationPlaybackState
from k5vision.media.recording_descriptor import RecordingStreamDescriptor, VideoCodec
from k5vision.media.viewport_dispatch import BoundedViewportDispatcher, ViewportDispatchState
from k5vision.media.viewport_geometry import ViewportGeometry, ViewportLayout, ViewportPlacement
from k5vision.media.windows_presentation_surface import BoundedWindowsPresentationSurface
from k5vision.media.windows_presentation_target import BoundedWindowsPresentationTarget
from k5vision.media.windows_viewport_layout import BoundedWindowsViewportLayout
from k5vision.media.windows_viewport_runtime import (
    BoundedWindowsViewportRuntime,
    WindowsViewportRuntimeState,
)
from k5vision.operator_launch import ResolvedLiveSource
from k5vision.operator_playback import (
    BoundedOperatorPlaybackCoordinator,
    OperatorPlaybackControlAction,
    OperatorPlaybackError,
    OperatorPlaybackErrorCode,
    OperatorPlaybackMetrics,
    OperatorPlaybackRequest,
)
from k5vision.services.device_registry import DeviceRegistry

_BATCH_MS = (0, 40, 80)
_FLUSH_MS = (120, 160)
_LONG_PAUSE_SECONDS = 8.1  # Exceeds the unmodified default packet callback bound of 8s.
_PRODUCT_BASELINE_TREE = "4d69121516d6420a0d0563ca7d13d6c52d944557"
_BOUND_MODULES = (
    "media.pausable_presentation_playback",
    "media.presentation_playback",
    "media.playback_control",
    "media.playback_pump",
    "media.presentation_frame",
    "media.viewport_dispatch",
    "media.windows_presentation_surface",
    "media.windows_presentation_target",
    "media.windows_viewport_layout",
    "media.windows_viewport_runtime",
    "operator_playback",
)


def _source_identity(*, require_clean: bool) -> dict:
    """Bind imported product bytes AND witness bytes to the actual checked-out HEAD."""
    checkout = Path(__file__).resolve().parents[2]
    paths = []
    for name in _BOUND_MODULES:
        module = importlib.import_module(f"k5vision.{name}")
        actual = Path(module.__file__).resolve()
        expected = checkout / "src" / "k5vision" / f"{name.replace('.', '/')}.py"
        assert actual == expected, "witness imported product outside the bound checkout"
        paths.append(actual.relative_to(checkout).as_posix())
    paths.extend(
        (
            "tests/integration/test_current_gate_playback_pause_windows.py",
            "tests/integration/test_current_gate_runtime_windows.py",
            "tests/unit/test_playback_pause_witness_contract.py",
        )
    )

    def git(*args: str) -> bytes:
        return subprocess.run(
            ["git", "-C", str(checkout), *args],
            check=True,
            capture_output=True,
            timeout=5,
        ).stdout

    head = git("rev-parse", "HEAD").decode("ascii").strip()
    assert len(head) == 40 and all(c in "0123456789abcdef" for c in head)
    clean = not git("status", "--porcelain", "--untracked-files=all", "--", *paths).strip()
    head_paths = set(
        git("ls-tree", "-r", "--name-only", head, "--", *paths).decode("utf-8").splitlines()
    )
    hashes = {
        path: hashlib.sha256((checkout / path).read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        for path in paths
    }
    mismatches = set(paths) - head_paths
    for path in head_paths:
        blob = git("show", f"{head}:{path}").replace(b"\r\n", b"\n")
        if hashlib.sha256(blob).hexdigest() != hashes[path]:
            mismatches.add(path)
    assert git("rev-parse", "HEAD").decode("ascii").strip() == head, "checkout HEAD changed"
    if require_clean:
        assert not mismatches, "native witness HEAD source binding failed"
        assert clean, "native witness requires committed, unchanged bound files"
    return {
        "checkout_head": head,
        "product_baseline_tree": _PRODUCT_BASELINE_TREE,  # Provenance, never an acceptance pin.
        "bound_files_clean": clean,
        "head_matches_bound_files": not mismatches,
        "bound_file_lf_sha256": hashes,
    }


def _packet() -> memoryview:
    # Valid generated RTP envelope; the payload is deliberately NOT encoded media.
    return memoryview(bytes.fromhex("806000010000000000000001") + b"generated-pause-packet")


async def _recording(root: Path, recording_id: UUID, source_id: UUID) -> None:
    sink = FramedAtomicRecordingSink(root, str(recording_id), max_packets=1, max_payload_bytes=64)
    try:
        await sink.open()
        await sink.write(_packet())
        await sink.finalize()
    finally:
        if sink.snapshot.state.value != "finalized":
            await asyncio.wait_for(sink.abort(), 2)
    started = datetime(2026, 1, 1, tzinfo=UTC)
    descriptor = RecordingStreamDescriptor(
        recording_id=recording_id,
        source_id=source_id,
        codec=VideoCodec.H264,
        payload_type=96,
        clock_rate_hz=90_000,
        started_at_utc=started,
        ended_at_utc=started + timedelta(milliseconds=200),
        duration_ms=200,
        rtp_timestamp_origin=0,
        packet_count=sink.snapshot.packets,
        payload_bytes=sink.snapshot.payload_bytes,
        file_bytes=sink.snapshot.file_bytes,
    )
    (root / f"{recording_id}.k5d").write_bytes(descriptor.to_json_bytes())


class _GeneratedDecoder:
    def __init__(self, trace: Callable) -> None:
        self.trace = trace
        self.decode_calls = self.flush_calls = self.close_calls = 0

    @staticmethod
    def frame(ms: int) -> PresentationVideoFrame:
        return PresentationVideoFrame(
            payload=memoryview(bytes([ms, 255 - ms, 80, 0]) * 16),
            width=4,
            height=4,
            stride_bytes=16,
            pixel_format=PixelFormat.BGRX,
            source_elapsed_ms=ms,
        )

    async def decode(self, packet: memoryview, source_elapsed_ms: int):
        assert packet == _packet() and source_elapsed_ms == 0
        self.decode_calls += 1
        assert self.decode_calls == 1
        self.trace("generated_batch")
        return tuple(self.frame(ms) for ms in _BATCH_MS)

    async def flush(self):
        self.flush_calls += 1
        assert self.flush_calls == 1
        self.trace("generated_flush")
        return tuple(self.frame(ms) for ms in _FLUSH_MS)

    async def close(self) -> None:
        self.close_calls += 1
        self.trace("decoder_closed")


class _GeneratedResolver:
    async def resolve(self, device: Device, stream_token: str) -> ResolvedLiveSource:
        assert stream_token == "main"
        # Required coordinator metadata only: no transport is created or contacted.
        return ResolvedLiveSource(f"rtsp://{device.host}/generated", 96)


class _WitnessLauncher:
    """Test-only launcher; real delivery, dispatcher, runtime, surface and target."""

    def __init__(self, pause_at: tuple[int, ...], native_factories: tuple | None) -> None:
        self.pause_at = pause_at
        self.trace: list[dict] = []
        self.started_ns = time.monotonic_ns()
        self.paused = asyncio.Queue(maxsize=2)
        self.control = None
        self.apply_control = None
        self.delivery = None
        self.dispatcher = None
        self.surface = None
        self.target = None
        self.layout = None
        self.decoder = _GeneratedDecoder(self.mark)

        def surface_factory():
            api = None if native_factories is None else native_factories[0]()
            self.surface = BoundedWindowsPresentationSurface(native_api=api)
            return self.surface

        def target_factory():
            api = None if native_factories is None else native_factories[1]()
            self.target = BoundedWindowsPresentationTarget(native_api=api)
            return self.target

        def layout_factory(geometry):
            self.layout = BoundedWindowsViewportLayout(geometry, target_factory=target_factory)
            return self.layout

        geometry = ViewportLayout(
            placements=(
                ViewportPlacement(
                    logical_slot=7,
                    geometry=ViewportGeometry(x=17, y=29, width=64, height=64),
                ),
            )
        )
        self.runtime = BoundedWindowsViewportRuntime(
            geometry, surface_factory=surface_factory, layout_factory=layout_factory
        )

    def mark(self, event: str, **fields) -> dict:
        entry = {"event": event, "elapsed_ns": time.monotonic_ns() - self.started_ns, **fields}
        self.trace.append(entry)
        return entry

    def counts(self) -> dict:
        return {
            "frame_starts": sum(e["event"] == "frame_started" for e in self.trace),
            "delivered_frames": self.delivery.snapshot.delivered_frames,
            "dispatches": self.dispatcher.snapshot.delivered_frames,
            "surface_copies": self.surface.snapshot.presented_frames,
            "surface_blits": self.surface.snapshot.blits,
            "target_presentations": self.target.snapshot.presentations,
            "runtime_presentations": self.runtime.snapshot.presentations,
        }

    async def run(
        self,
        source,
        recording_path,
        descriptor,
        *,
        start_ms,
        end_ms,
        rate,
        width,
        height,
        pause_control=None,
    ) -> OperatorPlaybackMetrics:
        assert source.payload_type == 96 and (width, height) == (640, 240)
        assert pause_control is not None
        self.control = pause_control
        self.delivery = PausablePresentationPlaybackDelivery(
            recording_path,
            descriptor,
            start_ms,
            end_ms,
            rate,
            pause_control=pause_control,
            decoder_factory=lambda _payload: self.decoder,
            # Keep production packet/frame/decoder timeout defaults intact.
        )

        async def consume(frame: PresentationVideoFrame) -> None:
            self.mark("frame_started", source_elapsed_ms=frame.source_elapsed_ms)
            assert pause_control.snapshot.state is PlaybackControlState.RUNNING
            before = self.runtime.snapshot.presentations
            await self.dispatcher.dispatch(7, frame)
            assert self.runtime.snapshot.presentations == before + 1
            self.mark("presented", source_elapsed_ms=frame.source_elapsed_ms)
            if frame.source_elapsed_ms in self.pause_at:
                ack = await self.apply_control(OperatorPlaybackControlAction.PAUSE)
                self.paused.put_nowait((frame.source_elapsed_ms, ack))
            # Return immediately: no test barrier extends an active frame callback.

        try:
            opened = await self.runtime.open()
            assert opened.open_surface_count == 1
            self.dispatcher = BoundedViewportDispatcher(self.runtime.bindings)
            final = await self.delivery.run(consume)
            assert final.state is PresentationPlaybackState.COMPLETE
            return OperatorPlaybackMetrics(
                delivered_frames=final.delivered_frames,
                presentations=self.runtime.snapshot.presentations,
                descriptor_verified=final.descriptor_verified,
            )
        finally:
            try:
                if self.dispatcher is not None:
                    await asyncio.wait_for(self.dispatcher.close(), 2)
            finally:
                await asyncio.wait_for(self.runtime.close(), 2)
                self.mark("presentation_closed")


def _assert_trace(trace: list[dict], expected_ms: list[int]) -> None:
    assert [e["elapsed_ns"] for e in trace] == sorted(e["elapsed_ns"] for e in trace)
    paused = False
    for entry in trace:
        if entry["event"] == "pause_ack":
            paused = True
        elif entry["event"] == "resume_ack":
            paused = False
        elif entry["event"] in {"frame_started", "presented"}:
            assert not paused, "frame activity occurred during acknowledged pause"
    for event in ("frame_started", "presented"):
        assert [e["source_elapsed_ms"] for e in trace if e["event"] == event] == expected_ms


async def _next_pause(launcher: _WitnessLauncher, playback: asyncio.Task):
    waiting = asyncio.create_task(launcher.paused.get())
    try:
        done, _pending = await asyncio.wait(
            {waiting, playback}, timeout=3, return_when=asyncio.FIRST_COMPLETED
        )
        assert waiting in done, "playback ended without the expected pause acknowledgement"
        assert not playback.done(), "playback stopped while acknowledging pause"
        return waiting.result()
    finally:
        if not waiting.done():
            waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)


async def _observe_pause(launcher: _WitnessLauncher, task: asyncio.Task, seconds: float) -> dict:
    assert launcher.control.snapshot.state is PlaybackControlState.PAUSED
    before = launcher.counts()
    acknowledged_ns = next(
        e["elapsed_ns"] for e in reversed(launcher.trace) if e["event"] == "pause_ack"
    )
    started = time.monotonic_ns()
    until = time.monotonic() + seconds
    samples = 0
    while time.monotonic() < until:
        await asyncio.sleep(min(0.05, max(0, until - time.monotonic())))
        assert not task.done(), "playback stopped while an acknowledged pause was held"
        assert launcher.control.snapshot.state is PlaybackControlState.PAUSED
        assert launcher.counts() == before, "frame/presentation counter advanced while paused"
        samples += 1
    elapsed = time.monotonic_ns() - started
    assert elapsed >= seconds * 1_000_000_000
    return {
        "pause_ack_ns": acknowledged_ns,
        "observed_ns": elapsed,
        "samples": samples,
        "before": before,
        "after": launcher.counts(),
    }


async def _scenario(root: Path, name: str, factories: tuple | None, pause_seconds: float) -> dict:
    root.mkdir(parents=True)
    registry = DeviceRegistry(database_path=root / "devices.sqlite3", site_id="generated-pause")
    try:
        return await _owned_scenario(root, registry, name, factories, pause_seconds)
    finally:
        registry.close()
        # This freshly created scenario directory is exclusively witness-owned.
        # Include partially written fixtures if setup failed before a task existed.
        for packet_file in (root / "recordings").glob("*"):
            packet_file.unlink()


async def _owned_scenario(
    root: Path, registry: DeviceRegistry, name: str, factories: tuple | None, pause_seconds: float
) -> dict:
    device = registry.enroll(
        DeviceCreate(
            name="Generated pause fixture",
            host="192.0.2.40",
            kind=DeviceKind.CAMERA,
            protocols={DeviceProtocol.RTSP},
        )
    ).device
    recording_id, control_id = uuid4(), uuid4()
    recording_root = root / "recordings"
    await _recording(recording_root, recording_id, device.id)
    pause_at = (0, 120) if name == "complete" else ((0,) if name == "cancel_batch" else (120,))
    launcher = _WitnessLauncher(pause_at, factories)
    coordinator = BoundedOperatorPlaybackCoordinator(
        registry, _GeneratedResolver(), recording_root, launcher, max_active_playbacks=1
    )
    owner = UserAccount(username="generated-viewer", display_name="Generated", role=UserRole.VIEWER)

    async def control(action):
        receipt = await coordinator.control(owner, control_id, action)
        assert receipt.control_id == control_id
        assert receipt.state is (
            PlaybackControlState.PAUSED
            if action is OperatorPlaybackControlAction.PAUSE
            else PlaybackControlState.RUNNING
        )
        launcher.mark(
            f"{action.value}_ack",
            pause_count=receipt.pause_count,
            resume_count=receipt.resume_count,
            paused_total_ms=receipt.paused_total_ms,
        )
        return receipt

    launcher.apply_control = control
    task = asyncio.create_task(
        coordinator.play(
            owner,
            OperatorPlaybackRequest(
                recording_id=recording_id, control_id=control_id, width=640, height=240
            ),
        )
    )
    windows = []
    try:
        ms, ack = await _next_pause(launcher, task)
        assert ms == pause_at[0] and ack.pause_count == 1 and ack.resume_count == 0
        duplicate = await control(OperatorPlaybackControlAction.PAUSE)
        assert (duplicate.pause_count, duplicate.resume_count) == (1, 0)
        windows.append(await _observe_pause(launcher, task, pause_seconds))
        if name == "complete":
            # Supersede Resume synchronously before its awakened waiter can run.
            await control(OperatorPlaybackControlAction.RESUME)
            await control(OperatorPlaybackControlAction.PAUSE)
            windows.append(await _observe_pause(launcher, task, 0.03))
            await control(OperatorPlaybackControlAction.RESUME)
            duplicate = await control(OperatorPlaybackControlAction.RESUME)
            assert (duplicate.pause_count, duplicate.resume_count) == (2, 2)
            ms, ack = await _next_pause(launcher, task)
            assert ms == 120 and (ack.pause_count, ack.resume_count) == (3, 2)
            windows.append(await _observe_pause(launcher, task, 0.03))
            await control(OperatorPlaybackControlAction.RESUME)
            completed = await asyncio.wait_for(task, 3)
            assert completed.completed and completed.descriptor_verified
            assert completed.delivered_frames == completed.presentations == 5
            assert launcher.delivery.snapshot.pump_delivered_packets == 1
            expected_ms = list(_BATCH_MS + _FLUSH_MS)
        else:
            task.cancel()
            result = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)
            assert isinstance(result[0], asyncio.CancelledError)
            assert launcher.delivery.snapshot.state is PresentationPlaybackState.CANCELLED
            expected_ms = [ms for ms in _BATCH_MS + _FLUSH_MS if ms <= pause_at[0]]
        assert coordinator.active_playbacks == 0
        try:
            await coordinator.control(owner, control_id, OperatorPlaybackControlAction.RESUME)
        except OperatorPlaybackError as exc:
            assert exc.code is OperatorPlaybackErrorCode.CONTROL_NOT_FOUND
        else:
            raise AssertionError("completed/cancelled playback retained its control")
        assert launcher.decoder.close_calls == 1
        assert launcher.decoder.decode_calls == 1
        assert launcher.decoder.flush_calls == (0 if name == "cancel_batch" else 1)
        assert launcher.dispatcher.snapshot.state is ViewportDispatchState.CLOSED
        assert launcher.runtime.snapshot.state is WindowsViewportRuntimeState.CLOSED
        assert launcher.runtime.snapshot.open_surface_count == 0
        assert launcher.layout.snapshot.open_target_count == 0
        assert (
            not launcher.surface.snapshot.surface_open and not launcher.target.snapshot.target_open
        )
        assert (
            launcher.surface.snapshot.state.value
            == launcher.target.snapshot.state.value
            == "closed"
        )
        assert all(count == len(expected_ms) for count in launcher.counts().values())
        _assert_trace(launcher.trace, expected_ms)
        snapshot = launcher.control.snapshot
        assert (snapshot.pause_count, snapshot.resume_count) == (
            (3, 3) if name == "complete" else (1, 0)
        )
        return {
            "scenario": name,
            "state": launcher.delivery.snapshot.state.value,
            "pause_count": snapshot.pause_count,
            "resume_count": snapshot.resume_count,
            "paused_total_ms": snapshot.paused_total_ms,
            "counts": launcher.counts(),
            "pause_windows": windows,
            "trace": launcher.trace,
            "decoder_close_count": launcher.decoder.close_calls,
            "active_playbacks": coordinator.active_playbacks,
            "open_surfaces": launcher.runtime.snapshot.open_surface_count,
            "open_targets": launcher.layout.snapshot.open_target_count,
            "control_released": True,
        }
    finally:
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)


async def run_generated_pause_witness(
    root: Path,
    *,
    native_factories: tuple[Callable, Callable] | None = None,
    pause_seconds: float = _LONG_PAUSE_SECONDS,
) -> dict:
    """Default is actual Win32; injected native APIs can establish only source contracts."""
    native = native_factories is None
    if not 0 < pause_seconds <= 9:
        raise ValueError("pause observation must be bounded to at most nine seconds")
    if native and pause_seconds <= 8:
        raise ValueError("native witness must exceed the default eight-second packet deadline")
    if native and sys.platform != "win32":
        raise RuntimeError("native Pause witness requires Windows")
    if not native and (len(native_factories) != 2 or not all(map(callable, native_factories))):
        raise ValueError("source contract requires both native API factories")
    identity = _source_identity(require_clean=native)
    scenarios = []
    for name in ("complete", "cancel_batch", "cancel_flush"):
        async with asyncio.timeout(20):
            scenarios.append(
                await _scenario(
                    root / name,
                    name,
                    native_factories,
                    pause_seconds if name == "complete" else 0.03,
                )
            )
    assert _source_identity(require_clean=native) == identity, (
        "bound checkout changed during witness"
    )
    return {
        "schema_version": 1,
        "evidence_scope": "generated-decode/native-presentation"
        if native
        else "source-contract/fake-native-boundary",
        "generated_decode": True,
        "native_presentation": native,
        "codec_acceptance": False,
        "browser_ui_acceptance": False,
        "installed_application_acceptance": False,
        "pixel_readback_acceptance": False,
        "exceeds_default_packet_deadline": scenarios[0]["pause_windows"][0]["observed_ns"]
        > 8_000_000_000,
        "identity": identity,
        "scenarios": scenarios,
    }
