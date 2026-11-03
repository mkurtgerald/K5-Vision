"""Opt-in native person-view broker proof using an immutable public OMZ image.

The image and model are independently licensed, supplied local development
fixtures. Neither their bytes nor native diagnostics belong in evidence artifacts.
This proves a small engineering path, not live-camera AI, accuracy or endurance.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import subprocess
import threading
import time
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

import pytest

from k5vision.media.presentation_frame import PixelFormat, PresentationVideoFrame
from k5vision.operator_analytics import OperatorAnalyticsBroker
from k5vision.operator_analytics_person import MODEL_REVISION, OperatorPersonConfiguration

_INPUTS = ("K5_OPERATOR_PERSON_ARTIFACT_ROOT", "K5_OPERATOR_PERSON_FIXTURE", "K5_DONOR_FFMPEG")
_WIDTH, _HEIGHT = 544, 320
_FIXTURE_BYTES = 553_577
_FIXTURE_SHA256 = "2be4dfa4d874e5f7929bebd1baf401e2ee630c4bf36cead7a2f8d5421d583959"
_FIXTURE_SOURCE = (
    "https://raw.githubusercontent.com/openvinotoolkit/open_model_zoo/"
    + MODEL_REVISION
    + "/models/intel/person-detection-retail-0013/assets/person-detection-retail-0013.png"
)
pytestmark = pytest.mark.skipif(
    os.getenv("K5_OPERATOR_PERSON_ACCEPTANCE") != "1"
    or not all(os.getenv(name) for name in _INPUTS),
    reason="Native person broker qualification needs explicit opt-in and verified local fixtures",
)


def _require(condition, message):
    __tracebackhide__ = True
    if not condition:
        pytest.fail(message, pytrace=False)


def _positive_frame():
    __tracebackhide__ = True
    fixture = Path(os.environ["K5_OPERATOR_PERSON_FIXTURE"]).resolve(strict=True)
    _require(
        fixture.is_file() and fixture.stat().st_size == _FIXTURE_BYTES,
        "Official positive fixture size was refused",
    )
    with fixture.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    _require(digest == _FIXTURE_SHA256, "Official positive fixture identity was refused")
    ffmpeg = Path(os.environ["K5_DONOR_FFMPEG"]).resolve(strict=True)
    _require(ffmpeg.is_file(), "Explicit native fixture decoder was unavailable")
    decoded = subprocess.run(
        [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(fixture),
            "-vf",
            f"scale={_WIDTH}:{_HEIGHT}",
            "-frames:v",
            "1",
            "-pix_fmt",
            "bgr0",
            "-f",
            "rawvideo",
            "-",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=20,
        check=False,
    )
    _require(
        decoded.returncode == 0 and len(decoded.stdout) == _WIDTH * _HEIGHT * 4,
        "Positive fixture did not decode to the bounded BGRX interface",
    )
    return PresentationVideoFrame(
        memoryview(decoded.stdout), _WIDTH, _HEIGHT, _WIDTH * 4, PixelFormat.BGRX, 0
    )


def _box_contract(result):
    __tracebackhide__ = True
    document = result.as_dict()
    _require(
        set(document) == {"status", "frame_id", "boxes"},
        "Person broker exposed fields outside the transient view contract",
    )
    for box in document["boxes"]:
        _require(
            set(box) == {"category", "confidence", "x_min", "y_min", "x_max", "y_max"},
            "Person view exposed an identity or execution field",
        )
        _require(
            box["category"] == "person"
            and 0.5 <= box["confidence"] <= 1.0
            and 0 <= box["x_min"] < box["x_max"] <= 1
            and 0 <= box["y_min"] < box["y_max"] <= 1,
            "Person view returned an invalid normalized box",
        )


def test_native_person_broker_positive_blank_capacity_and_cleanup(record_testsuite_property):
    __tracebackhide__ = True
    positive = _positive_frame()
    blank = PresentationVideoFrame(
        memoryview(bytes(_WIDTH * _HEIGHT * 4)), _WIDTH, _HEIGHT, _WIDTH * 4, PixelFormat.BGRX, 0
    )
    config = OperatorPersonConfiguration(Path(os.environ["K5_OPERATOR_PERSON_ARTIFACT_ROOT"]))
    started = time.monotonic()
    baseline_threads = {thread.ident for thread in threading.enumerate()}
    receipt = {
        "schema_version": "1",
        "platform": platform.system(),
        "native_windows_execution": os.name == "nt",
        "model": "person-detection-retail-0013",
        "model_revision": MODEL_REVISION,
        "fixture_sha256": _FIXTURE_SHA256,
        "fixture_source": _FIXTURE_SOURCE,
        "fixture_license": "Apache-2.0",
        "runtime_versions": {
            package: version(package) for package in ("openvino", "numpy", "openvino-telemetry")
        },
        "width": _WIDTH,
        "height": _HEIGHT,
        "max_views": 2,
        "mock_inference": False,
        "private_camera_fixture": False,
        "live_camera_acceptance": False,
        "product_analytics_enablement": False,
        "accuracy_acceptance": False,
        "long_duration_acceptance": False,
    }

    async def qualify():
        broker = OperatorAnalyticsBroker(
            config, max_views=2, idle_seconds=1.0, inference_timeout_seconds=10.0
        )
        first_owner, second_owner, third_owner = uuid4(), uuid4(), uuid4()
        camera = uuid4()
        try:
            positive_task = asyncio.create_task(
                broker.infer(owner_id=first_owner, camera_id=camera, frame_id=1, frame=positive)
            )
            await asyncio.sleep(0)
            overlap = await broker.infer(
                owner_id=first_owner, camera_id=camera, frame_id=2, frame=positive
            )
            _require(
                overlap.status == "busy" and not overlap.observations,
                "An in-flight native person view admitted another frame",
            )
            detected = await positive_task
            _require(
                detected.status == "active"
                and detected.frame_id == 1
                and 1 <= len(detected.observations) <= 32,
                "Actual native person inference did not detect the official positive fixture",
            )
            _box_contract(detected)
            receipt["positive_person_boxes"] = len(detected.observations)
            receipt["single_flight_busy"] = True
            empty = await broker.infer(
                owner_id=second_owner, camera_id=camera, frame_id=3, frame=blank
            )
            _require(
                empty.status == "active" and empty.frame_id == 3 and not empty.observations,
                "Actual native person inference did not return an empty blank frame",
            )
            _box_contract(empty)
            receipt["blank_person_boxes"] = 0
            receipt["independent_view_workers"] = True
            blocked = await broker.infer(
                owner_id=third_owner, camera_id=camera, frame_id=4, frame=positive
            )
            _require(
                blocked.status == "busy" and not blocked.observations,
                "Native person broker exceeded its admitted two-view capacity",
            )
            receipt["third_view_capacity_denied"] = True
            await broker.close_owner(first_owner)
            restored = await broker.infer(
                owner_id=third_owner, camera_id=camera, frame_id=5, frame=positive
            )
            _require(
                restored.status == "active" and len(restored.observations) >= 1,
                "Closing a person view did not release actual native capacity",
            )
            _box_contract(restored)
            receipt["close_owner_released_capacity"] = True
            await asyncio.sleep(1.1)
            await broker.expire_idle()
            resumed = await broker.infer(
                owner_id=first_owner, camera_id=camera, frame_id=6, frame=blank
            )
            _require(
                resumed.status == "active" and not resumed.observations,
                "Idle person views did not release actual native capacity",
            )
            receipt["idle_views_expired"] = True
        finally:
            await broker.aclose()
        closed = await broker.infer(
            owner_id=first_owner, camera_id=camera, frame_id=7, frame=positive
        )
        _require(
            closed.status == "unavailable" and not closed.observations,
            "Closed native person broker accepted another frame",
        )
        receipt["closed_broker_refused"] = True
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not any(
                thread.name.startswith("k5-analytics") and thread.ident not in baseline_threads
                for thread in threading.enumerate()
            ):
                receipt["owned_native_threads_stopped"] = True
                return
            await asyncio.sleep(0.02)
        _require(False, "Owned person native threads did not finish cleanup")

    asyncio.run(qualify())
    elapsed = time.monotonic() - started
    _require(elapsed < 120, "Person native qualification exceeded its two-minute duration")
    receipt["duration_seconds"] = round(elapsed, 2)
    record_testsuite_property("operator_beta_person_receipt", json.dumps(receipt, sort_keys=True))
