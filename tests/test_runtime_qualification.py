import asyncio

import pytest
from pydantic import ValidationError

from k5vision.adapters.runtime import (
    QualificationErrorCode,
    QualificationPlan,
    RuntimeQualificationError,
    RuntimeSample,
    qualify_candidate,
    rank_samples,
)


def sample(
    candidate: str,
    *,
    latency: float,
    cpu: float = 10,
    recovered: bool = True,
    completed: bool = True,
) -> RuntimeSample:
    return RuntimeSample(
        candidate=candidate,
        startup_ms=20,
        latency_ms=latency,
        cpu_percent=cpu,
        memory_mb=64,
        bytes_processed=1024,
        recovered=recovered,
        completed=completed,
    )


class FakeCandidate:
    def __init__(
        self,
        candidate: str = "candidate-a",
        *,
        delay_seconds: float = 0,
        fail: bool = False,
        recovery_succeeds: bool = True,
    ) -> None:
        self.candidate = candidate
        self.delay_seconds = delay_seconds
        self.fail = fail
        self.recovery_succeeds = recovery_succeeds
        self.measure_calls = 0
        self.recovery_calls = 0

    async def measure(self, source_uri: str, *, timeout_seconds: float) -> RuntimeSample:
        del source_uri, timeout_seconds
        self.measure_calls += 1
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if self.fail:
            raise RuntimeError("dependency failure with private detail")
        return sample(self.candidate, latency=10 + self.measure_calls)

    async def measure_recovery(self, source_uri: str, *, timeout_seconds: float) -> RuntimeSample:
        del source_uri, timeout_seconds
        self.recovery_calls += 1
        return sample(
            self.candidate,
            latency=25,
            recovered=self.recovery_succeeds,
            completed=self.recovery_succeeds,
        )


def test_runtime_sample_rejects_negative_measurements() -> None:
    with pytest.raises(ValidationError):
        RuntimeSample(
            candidate="candidate-a",
            startup_ms=-1,
            latency_ms=1,
            cpu_percent=1,
            memory_mb=1,
            bytes_processed=1,
            recovered=True,
            completed=True,
        )


def test_runtime_sample_rejects_unbounded_or_extra_values() -> None:
    with pytest.raises(ValidationError):
        RuntimeSample(
            candidate="candidate-a",
            startup_ms=float("inf"),
            latency_ms=1,
            cpu_percent=1,
            memory_mb=1,
            bytes_processed=1,
            recovered=True,
            completed=True,
        )

    with pytest.raises(ValidationError):
        RuntimeSample(
            candidate="candidate-a",
            startup_ms=1,
            latency_ms=1,
            cpu_percent=1,
            memory_mb=1,
            bytes_processed=1,
            recovered=True,
            completed=True,
            source_uri="rtsp://private.example/live",
        )


def test_qualification_plan_is_bounded() -> None:
    with pytest.raises(ValidationError):
        QualificationPlan(scored_runs=4)
    with pytest.raises(ValidationError):
        QualificationPlan(scored_runs=101)
    with pytest.raises(ValidationError):
        QualificationPlan(timeout_seconds=0)


def test_qualify_candidate_enforces_warmup_scored_runs_and_recovery() -> None:
    candidate = FakeCandidate()

    result = asyncio.run(
        qualify_candidate(
            "candidate-a",
            candidate,
            "rtsp://user:secret@example/live",
            plan=QualificationPlan(scored_runs=5, timeout_seconds=1),
        )
    )

    assert candidate.measure_calls == 6
    assert candidate.recovery_calls == 1
    assert len(result.samples) == 5
    assert result.recovery_sample.recovered is True
    assert "source_uri" not in result.model_dump_json()
    assert "secret" not in result.model_dump_json()


def test_qualify_candidate_preserves_failed_recovery_as_evidence() -> None:
    candidate = FakeCandidate(recovery_succeeds=False)

    result = asyncio.run(
        qualify_candidate(
            "candidate-a",
            candidate,
            "rtsp://example/live",
            plan=QualificationPlan(timeout_seconds=1),
        )
    )

    assert result.recovery_sample.completed is False
    assert result.recovery_sample.recovered is False


def test_qualify_candidate_maps_outer_timeout_without_implicit_retry() -> None:
    candidate = FakeCandidate(delay_seconds=0.05)

    with pytest.raises(RuntimeQualificationError) as caught:
        asyncio.run(
            qualify_candidate(
                "candidate-a",
                candidate,
                "rtsp://example/live",
                plan=QualificationPlan(timeout_seconds=0.01),
            )
        )

    assert caught.value.code is QualificationErrorCode.TIMEOUT
    assert candidate.measure_calls == 1


def test_qualify_candidate_sanitizes_dependency_failure() -> None:
    candidate = FakeCandidate(fail=True)

    with pytest.raises(RuntimeQualificationError) as caught:
        asyncio.run(
            qualify_candidate(
                "candidate-a",
                candidate,
                "rtsp://example/live",
                plan=QualificationPlan(timeout_seconds=1),
            )
        )

    assert caught.value.code is QualificationErrorCode.CANDIDATE_FAILURE
    assert "private detail" not in str(caught.value)


def test_qualify_candidate_rejects_mismatched_sample_identity() -> None:
    candidate = FakeCandidate(candidate="other-candidate")

    with pytest.raises(RuntimeQualificationError) as caught:
        asyncio.run(
            qualify_candidate(
                "candidate-a",
                candidate,
                "rtsp://example/live",
                plan=QualificationPlan(timeout_seconds=1),
            )
        )

    assert caught.value.code is QualificationErrorCode.INVALID_SAMPLE


def test_rank_samples_uses_measurements_and_stable_tiebreaker() -> None:
    ranked = rank_samples(
        [
            sample("candidate-b", latency=15),
            sample("candidate-a", latency=15),
            sample("candidate-c", latency=30),
        ]
    )

    assert [item.candidate for item in ranked] == ["candidate-a", "candidate-b", "candidate-c"]


def test_rank_samples_excludes_failed_or_unrecovered_runs() -> None:
    ranked = rank_samples(
        [
            sample("good", latency=20),
            sample("failed", latency=1, completed=False),
            sample("unrecovered", latency=1, recovered=False),
        ]
    )

    assert [item.candidate for item in ranked] == ["good"]


def test_runtime_sample_does_not_retain_source_or_credentials() -> None:
    result = sample("candidate-a", latency=10).model_dump()

    assert "source_uri" not in result
    assert "username" not in result
    assert "password" not in result
