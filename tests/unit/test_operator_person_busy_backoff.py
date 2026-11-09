"""Prevent repeated expensive frame uploads while installed native person view warms.

The normal active-view cadence and exact-frame, no-retention contract remain.
"""

from pathlib import Path


def test_busy_person_preparation_does_not_flood_native_worker():
    app = (Path(__file__).resolve().parents[2] / "src/k5vision/operator_web/app.js").read_text(
        encoding="utf-8"
    )
    sampled = app.split("async function samplePerson(epoch)", 1)[1].split(
        "async function startLive(", 1
    )[0]

    assert "let busyRetryDelayMs = null;" in sampled
    busy = sampled.split('if (result.status === "busy") {', 1)[1].split("} else {", 1)[0]
    assert "busyRetryDelayMs = 1500;" in busy
    refused = sampled.split("if (error.status === 429) {", 1)[1].split("} else if", 1)[0]
    assert "busyRetryDelayMs = 1500;" in refused
    assert "busyRetryDelayMs ?? Math.max(0, 500 - (performance.now() - began))" in sampled
    assert "pixels?.fill(0);" in sampled
    assert "snapshot?.data.fill(0);" in sampled
    assert "state.personController?.abort();" in app
