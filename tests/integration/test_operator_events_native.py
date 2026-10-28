"""Real decoded recording/person inference, never a substitute for installer proof."""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from k5vision import identity_state
from k5vision.operator_analytics_person import OperatorPersonConfiguration
from k5vision.operator_events import OperatorEventService, OperatorEventStore, RecordedFrameSampler

pytestmark = pytest.mark.skipif(
    os.getenv("K5_OPERATOR_EVENTS_NATIVE_ACCEPTANCE") != "1",
    reason="Native recorded-event proof requires explicit opt-in and licensed supplied fixtures",
)


@pytest.fixture
def native_identity(tmp_path, monkeypatch):
    if os.name == "posix":
        original = Path.lstat

        def system_ancestors(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            if path in (Path("/"), Path("/tmp")) and info.st_uid == 65534:
                fields = list(info)
                fields[4] = 0
                return os.stat_result(fields)
            return info

        monkeypatch.setattr(Path, "lstat", system_ancestors)
    state = identity_state.initialize_identity_state(
        tmp_path / "identity", username="native-events-admin", password="native-events-password"
    )
    return state


def _inputs():
    fixture = Path(os.environ["K5_OPERATOR_PERSON_FIXTURE"])
    assert hashlib.sha256(fixture.read_bytes()).hexdigest() == (
        "2be4dfa4d874e5f7929bebd1baf401e2ee630c4bf36cead7a2f8d5421d583959"
    )
    model = Path(os.environ["K5_OPERATOR_PERSON_ARTIFACT_ROOT"])
    executable = os.getenv("K5_DONOR_FFMPEG") or shutil.which("ffmpeg")
    assert executable is not None
    return fixture, model, executable


def _writer(executable, fixture, target, *, seconds=3, realtime=False):
    command = [str(executable), "-nostdin", "-hide_banner", "-loglevel", "error"]
    if realtime:
        command.append("-re")
    command.extend(
        [
            "-loop",
            "1",
            "-i",
            str(fixture),
            "-t",
            str(seconds),
            "-r",
            "5",
            "-c:v",
            "libx264",
            "-threads",
            "1",
            "-pix_fmt",
            "yuv420p",
            "-g",
            "5",
            "-movflags",
            "+frag_keyframe+empty_moov+default_base_moof",
            "-frag_duration",
            "1000000",
            str(target),
        ]
    )
    return subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def test_native_decoder_uses_recorded_presentation_time_and_cleans_transient_copy(native_identity):
    fixture, _model, executable = _inputs()
    root = native_identity.directory / "recordings"
    root.mkdir(mode=0o700)
    path = native_identity.directory / "generated-fixture.mp4"
    process = _writer(executable, fixture, path)
    assert process.wait(timeout=15) == 0
    sampler = RecordedFrameSampler(root)
    started = datetime(2026, 10, 26, 12, tzinfo=UTC)
    sample = sampler.decode(path.read_bytes(), started, "generated-fixture")
    assert "-B" in sampler._worker._process.args
    sampler.close()
    assert sample is not None
    assert 2 <= (sample.occurred_at - started).total_seconds() <= 3
    assert sample.frame.width == 544 and sample.frame.height == 320
    assert sample.frame.payload.nbytes == 544 * 320 * 4
    assert not list((native_identity.directory / "analytics-samples").iterdir())


def test_real_recording_growth_generates_person_event_without_browser_and_survives_restart(
    native_identity,
):
    fixture, model, executable = _inputs()
    root = native_identity.directory / "recordings"
    root.mkdir(mode=0o700)
    identifier = uuid4()
    source = SimpleNamespace(
        binding=SimpleNamespace(device_id=identifier, path_name="k5_" + "a" * 64),
        recording_enabled=True,
    )
    directory = root / source.binding.path_name
    directory.mkdir(mode=0o700)
    started = datetime(2026, 10, 26, 12, tzinfo=UTC)
    target = directory / started.strftime("%Y-%m-%d_%H-%M-%S-%f_%z.mp4")
    store = OperatorEventStore(native_identity.directory, native_identity.site_id)
    events = OperatorEventService(
        store=store,
        cameras=SimpleNamespace(list_sources=lambda: (source,)),
        recording_root=root,
        configuration=OperatorPersonConfiguration(model),
    )
    events.configure(identifier, enabled=True, confidence_threshold=0.5)
    process = _writer(executable, fixture, target, seconds=12, realtime=True)

    async def run():
        try:
            await events.start()
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if store.summary(identifier)["event_count"]:
                    break
                await asyncio.sleep(0.25)
            assert store.summary(identifier)["event_count"] >= 1
            row = store.events(identifier)["events"][0]
            # Host inference UTC differs by two days. The event still points to
            # the decoded camera recording, not current or browser wall time.
            stamp = datetime.fromisoformat(row["occurred_at"])
            assert started <= stamp <= started + timedelta(seconds=12)
            assert row["person_count"] > 0 and row["confidence"] >= 0.5
            assert row["playback_at"] == row["occurred_at"]
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
            await events.aclose()

    asyncio.run(run())
    reopened = OperatorEventStore(native_identity.directory, native_identity.site_id)
    try:
        assert reopened.settings(identifier)["enabled"]
        assert reopened.events(identifier)["events"]
        assert not list((native_identity.directory / "analytics-samples").iterdir())
    finally:
        reopened.close()
