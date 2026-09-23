from __future__ import annotations

import pytest

from k5vision.analytics.skill_protocol import (
    SkillProtocolError,
    build_shared_memory_frame_message,
    decode_event,
    parse_detection_event,
)


def test_build_shared_memory_frame_message_contains_no_source_or_credentials() -> None:
    value = build_shared_memory_frame_message(
        frame_id=7,
        shared_memory_name="k5-frame",
        byte_length=16,
        width=2,
        height=2,
        stride_bytes=8,
        pixel_format="BGRx",
        source_elapsed_ms=33,
    )

    assert value == {
        "event": "frame",
        "schema_version": "1",
        "frame_id": 7,
        "transport": "shared_memory",
        "shared_memory_name": "k5-frame",
        "byte_length": 16,
        "width": 2,
        "height": 2,
        "stride_bytes": 8,
        "pixel_format": "BGRx",
        "source_elapsed_ms": 33,
    }
    assert "source_uri" not in value
    assert "credentials" not in value


def test_decode_and_normalize_deepcamera_style_detection_event() -> None:
    event = decode_event(
        b'{"event":"detections","frame_id":4,"objects":[{"class":"person",'
        b'"confidence":0.9,"bbox":[10,20,110,220]}]}\n',
        max_response_bytes=4096,
    )

    detections = parse_detection_event(
        event,
        expected_frame_id=4,
        width=200,
        height=400,
    )

    assert len(detections) == 1
    detection = detections[0]
    assert detection.track_id == "4:0"
    assert detection.category == "person"
    assert detection.confidence == pytest.approx(0.9)
    assert detection.box.x_min == pytest.approx(0.05)
    assert detection.box.y_min == pytest.approx(0.05)
    assert detection.box.x_max == pytest.approx(0.55)
    assert detection.box.y_max == pytest.approx(0.55)


@pytest.mark.parametrize(
    "event",
    [
        {"event": "detections", "frame_id": 99, "objects": []},
        {"event": "detections", "frame_id": 1, "objects": "bad"},
        {
            "event": "detections",
            "frame_id": 1,
            "objects": [{"class": "person", "confidence": 2, "bbox": [0, 0, 1, 1]}],
        },
        {
            "event": "detections",
            "frame_id": 1,
            "objects": [{"class": "person", "confidence": 0.5, "bbox": [-1, 0, 1, 1]}],
        },
    ],
)
def test_detection_protocol_fails_closed_on_malformed_results(event: object) -> None:
    with pytest.raises(SkillProtocolError):
        parse_detection_event(
            event,  # type: ignore[arg-type]
            expected_frame_id=1,
            width=2,
            height=2,
        )


def test_decode_event_rejects_non_json_and_oversize() -> None:
    with pytest.raises(SkillProtocolError):
        decode_event(b"not-json\n", max_response_bytes=1024)
    with pytest.raises(SkillProtocolError):
        decode_event(b"x" * 1025, max_response_bytes=1024)
