import asyncio

import pytest
from pydantic import ValidationError

from k5vision.adapters.runtime import (
    CandidateReview,
    QualificationErrorCode,
    QualificationPlan,
    QualificationResult,
    RuntimeQualificationError,
    RuntimeSample,
    qualify_candidate,
    rank_qualification_results,
    rank_samples,
    select_qualified_candidate,
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


def qualification_result(
    candidate: str,
    latencies: list[float],
    *,
    recovery_succeeds: bool = True,
) -> QualificationResult:
    return QualificationResult(
        candidate=candidate,
        samples=[sample(candidate, latency=value) for value in latencies],
        recovery_sample=sample(
            candidate,
            latency=25,
            recovered=recovery_succeeds,
            completed=recovery_succeeds,
        ),
    )


def review(
    candidate: str,
    *,
    commercial: bool = True,
    redistribution: bool = True,
    windows: bool = True,
    linux: bool = True,
) -> CandidateReview:
    return CandidateReview(
        candidate=candidate,
        component_version="1.0.0",
        license_id="MIT",
        source_reference="immutable-source-ref",
        commercial_use_approved=commercial,
        redistribution_approved=redistribution,
        windows_supported=windows,
        linux_supported=linux,
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


def test_candidate_review_rejects_unexpected_fields() -> None:
    data = review("candidate-a").model_dump()
    data["private_note"] = "do not retain"

    with pytest.raises(ValidationError):
        CandidateReview.model_validate(data)


def test_rank_qualification_results_uses_median_measurements() -> None:
    results = [
        qualification_result("candidate-a", [30, 30, 30, 30, 30]),
        qualification_result("candidate-b", [9, 9, 9, 9, 9]),
    ]
    reviews = [review("candidate-b"), review("candidate-a")]

    ranked = rank_qualification_results(results, reviews)

    assert [item.candidate for item in ranked] == ["candidate-b", "candidate-a"]
    assert ranked[0].median_latency_ms == 9
    assert select_qualified_candidate(results, reviews).candidate == "candidate-b"


def test_rank_qualification_results_excludes_failed_recovery_or_review() -> None:
    results = [
        qualification_result("candidate-fast", [1, 1, 1, 1, 1], recovery_succeeds=False),
        qualification_result("candidate-reviewed-out", [2, 2, 2, 2, 2]),
        qualification_result("candidate-good", [20, 20, 20, 20, 20]),
    ]
    reviews = [
        review("candidate-fast"),
        review("candidate-reviewed-out", commercial=False),
        review("candidate-good"),
    ]

    ranked = rank_qualification_results(results, reviews)

    assert [item.candidate for item in ranked] == ["candidate-good"]


def test_rank_qualification_results_requires_review_for_every_result() -> None:
    results = [qualification_result("candidate-a", [10, 10, 10, 10, 10])]

    with pytest.raises(RuntimeQualificationError) as caught:
        rank_qualification_results(results, [])

    assert caught.value.code is QualificationErrorCode.INSUFFICIENT_EVIDENCE


def test_rank_qualification_results_rejects_duplicate_result_identity() -> None:
    result = qualification_result("candidate-a", [10, 10, 10, 10, 10])

    with pytest.raises(RuntimeQualificationError) as caught:
        rank_qualification_results([result, result], [review("candidate-a")])

    assert caught.value.code is QualificationErrorCode.INSUFFICIENT_EVIDENCE


def test_selection_fails_when_no_candidate_has_complete_evidence() -> None:
    result = qualification_result("candidate-a", [10, 10, 10, 10, 10])

    with pytest.raises(RuntimeQualificationError) as caught:
        select_qualified_candidate([result], [review("candidate-a", linux=False)])

    assert caught.value.code is QualificationErrorCode.INSUFFICIENT_EVIDENCE
