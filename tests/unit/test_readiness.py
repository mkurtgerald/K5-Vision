from __future__ import annotations

import pytest
from pydantic import ValidationError

from k5vision.media.readiness import (
    ReadinessEvidence,
    ReadinessObservation,
    ReadinessOutcome,
    ReadinessPlan,
)


def passing_observations(plan: ReadinessPlan) -> tuple[ReadinessObservation, ...]:
    return tuple(
        ReadinessObservation(
            level=level,
            cycle=cycle,
            attempted_sessions=level,
            completed_sessions=level,
            outcome=ReadinessOutcome.PASS,
            elapsed_ms=100,
        )
        for level in plan.concurrency_ladder
        for cycle in range(1, plan.cycles_per_level + 1)
    )


def test_default_plan_is_bounded_versioned_and_deterministic() -> None:
    plan = ReadinessPlan()
    assert plan.schema_version == "1"
    assert plan.concurrency_ladder == (1, 2)
    assert plan.cycles_per_level == 3
    assert plan.expected_observations == 6


@pytest.mark.parametrize(
    "ladder",
    [
        (2, 3),
        (1, 1),
        (1, 3, 2),
        (1, 0),
    ],
)
def test_plan_rejects_invalid_concurrency_ladders(ladder: tuple[int, ...]) -> None:
    with pytest.raises(ValidationError):
        ReadinessPlan(concurrency_ladder=ladder)


def test_passing_observation_requires_full_completion() -> None:
    with pytest.raises(ValidationError):
        ReadinessObservation(
            level=2,
            cycle=1,
            attempted_sessions=2,
            completed_sessions=1,
            outcome=ReadinessOutcome.PASS,
            elapsed_ms=50,
        )


def test_evidence_accepts_complete_ordered_source_free_workload() -> None:
    plan = ReadinessPlan(cycles_per_level=2, concurrency_ladder=(1, 2, 4))
    evidence = ReadinessEvidence(
        revision="a" * 40,
        plan=plan,
        observations=passing_observations(plan),
    )
    assert evidence.accepted
    payload = evidence.model_dump_json()
    assert "rtsp://" not in payload
    assert "credential" not in payload.casefold()
    assert "runner" not in payload.casefold()


def test_evidence_rejects_incomplete_workload() -> None:
    plan = ReadinessPlan(cycles_per_level=2)
    with pytest.raises(ValidationError):
        ReadinessEvidence(
            revision="b" * 40,
            plan=plan,
            observations=passing_observations(plan)[:-1],
        )


def test_evidence_rejects_out_of_order_workload() -> None:
    plan = ReadinessPlan(cycles_per_level=2)
    observations = list(passing_observations(plan))
    observations[0], observations[1] = observations[1], observations[0]
    with pytest.raises(ValidationError):
        ReadinessEvidence(
            revision="c" * 40,
            plan=plan,
            observations=tuple(observations),
        )


def test_evidence_rejects_level_attempt_mismatch() -> None:
    plan = ReadinessPlan(cycles_per_level=2)
    observations = list(passing_observations(plan))
    observations[0] = ReadinessObservation(
        level=1,
        cycle=1,
        attempted_sessions=2,
        completed_sessions=2,
        outcome=ReadinessOutcome.PASS,
        elapsed_ms=50,
    )
    with pytest.raises(ValidationError):
        ReadinessEvidence(
            revision="d" * 40,
            plan=plan,
            observations=tuple(observations),
        )


def test_nonpassing_observation_keeps_evidence_complete_but_not_accepted() -> None:
    plan = ReadinessPlan(cycles_per_level=2)
    observations = list(passing_observations(plan))
    observations[-1] = ReadinessObservation(
        level=2,
        cycle=2,
        attempted_sessions=2,
        completed_sessions=1,
        outcome=ReadinessOutcome.START_FAILURE,
        elapsed_ms=90,
    )
    evidence = ReadinessEvidence(
        revision="e" * 40,
        plan=plan,
        observations=tuple(observations),
    )
    assert not evidence.accepted
