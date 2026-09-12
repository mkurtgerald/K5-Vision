"""Project-owned contracts for runtime qualification."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from enum import StrEnum
from statistics import median
from typing import Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RuntimeSample(BaseModel):
    """One reproducible qualification sample without source or credential retention."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    schema_version: Literal["1"] = "1"
    candidate: str = Field(min_length=1, max_length=128)
    startup_ms: float = Field(ge=0)
    latency_ms: float = Field(ge=0)
    cpu_percent: float = Field(ge=0)
    memory_mb: float = Field(ge=0)
    bytes_processed: int = Field(ge=0)
    recovered: bool
    completed: bool


class QualificationPlan(BaseModel):
    """Bounded, reproducible execution plan for one candidate."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    scored_runs: int = Field(default=5, ge=5, le=100)
    timeout_seconds: float = Field(default=10, gt=0, le=300)


class QualificationResult(BaseModel):
    """Scored samples plus one explicit interruption/re-entry result."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = "1"
    candidate: str = Field(min_length=1, max_length=128)
    samples: list[RuntimeSample] = Field(min_length=5, max_length=100)
    recovery_sample: RuntimeSample

    @model_validator(mode="after")
    def samples_match_candidate(self) -> Self:
        all_samples = [*self.samples, self.recovery_sample]
        if any(sample.candidate != self.candidate for sample in all_samples):
            raise ValueError("qualification samples must match the result candidate")
        return self


class CandidateReview(BaseModel):
    """Distribution and commercial review attached to one measured candidate."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = "1"
    candidate: str = Field(min_length=1, max_length=128)
    component_version: str = Field(min_length=1, max_length=128)
    license_id: str = Field(min_length=1, max_length=128)
    source_reference: str = Field(min_length=1, max_length=512)
    commercial_use_approved: bool
    redistribution_approved: bool
    windows_supported: bool
    linux_supported: bool

    def is_eligible(self) -> bool:
        """Return whether this review clears the candidate for measured ranking."""
        return all(
            (
                self.commercial_use_approved,
                self.redistribution_approved,
                self.windows_supported,
                self.linux_supported,
            )
        )


class CandidateScore(BaseModel):
    """Deterministic aggregate derived only from retained scored measurements."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    schema_version: Literal["1"] = "1"
    candidate: str = Field(min_length=1, max_length=128)
    median_startup_ms: float = Field(ge=0)
    median_latency_ms: float = Field(ge=0)
    median_cpu_percent: float = Field(ge=0)
    median_memory_mb: float = Field(ge=0)
    median_bytes_processed: float = Field(ge=0)


class QualificationErrorCode(StrEnum):
    """Stable failure classes exposed by the qualification boundary."""

    TIMEOUT = "timeout"
    CANDIDATE_FAILURE = "candidate_failure"
    INVALID_SAMPLE = "invalid_sample"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class RuntimeQualificationError(RuntimeError):
    """Sanitized qualification failure that does not retain source details."""

    def __init__(self, code: QualificationErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class RuntimeCandidate(Protocol):
    """Replaceable boundary implemented by each qualification candidate."""

    async def measure(self, source_uri: str, *, timeout_seconds: float) -> RuntimeSample:
        """Measure one bounded run and return only project-owned data."""
        ...

    async def measure_recovery(self, source_uri: str, *, timeout_seconds: float) -> RuntimeSample:
        """Measure one explicit interruption and re-entry case."""
        ...


def _validate_sample(sample: RuntimeSample, candidate: str) -> RuntimeSample:
    if sample.candidate != candidate:
        raise RuntimeQualificationError(
            QualificationErrorCode.INVALID_SAMPLE,
            "runtime candidate returned a sample for a different candidate",
        )
    return sample


async def _run_bounded(
    operation: Awaitable[RuntimeSample],
    *,
    timeout_seconds: float,
    candidate: str,
) -> RuntimeSample:
    try:
        sample = await asyncio.wait_for(operation, timeout=timeout_seconds)
    except TimeoutError:
        raise RuntimeQualificationError(
            QualificationErrorCode.TIMEOUT,
            "runtime candidate measurement timed out",
        ) from None
    except RuntimeQualificationError:
        raise
    except Exception:
        raise RuntimeQualificationError(
            QualificationErrorCode.CANDIDATE_FAILURE,
            "runtime candidate measurement failed",
        ) from None
    return _validate_sample(sample, candidate)


async def qualify_candidate(
    candidate_name: str,
    candidate: RuntimeCandidate,
    source_uri: str,
    *,
    plan: QualificationPlan | None = None,
) -> QualificationResult:
    """Execute the Stage 03 method with bounded calls and sanitized failures."""
    candidate_name = candidate_name.strip()
    if not candidate_name:
        raise ValueError("candidate_name must not be empty")
    if not source_uri.strip():
        raise ValueError("source_uri must not be empty")

    plan = plan or QualificationPlan()

    await _run_bounded(
        candidate.measure(source_uri, timeout_seconds=plan.timeout_seconds),
        timeout_seconds=plan.timeout_seconds,
        candidate=candidate_name,
    )

    samples = []
    for _ in range(plan.scored_runs):
        samples.append(
            await _run_bounded(
                candidate.measure(source_uri, timeout_seconds=plan.timeout_seconds),
                timeout_seconds=plan.timeout_seconds,
                candidate=candidate_name,
            )
        )

    recovery_sample = await _run_bounded(
        candidate.measure_recovery(source_uri, timeout_seconds=plan.timeout_seconds),
        timeout_seconds=plan.timeout_seconds,
        candidate=candidate_name,
    )

    return QualificationResult(
        candidate=candidate_name,
        samples=samples,
        recovery_sample=recovery_sample,
    )


def rank_samples(samples: list[RuntimeSample]) -> list[RuntimeSample]:
    """Rank successful samples deterministically using captured measurements only."""
    eligible = [sample for sample in samples if sample.completed and sample.recovered]
    return sorted(
        eligible,
        key=lambda sample: (
            sample.latency_ms,
            sample.cpu_percent,
            sample.memory_mb,
            sample.startup_ms,
            sample.candidate,
        ),
    )


def _aggregate_result(result: QualificationResult) -> CandidateScore:
    return CandidateScore(
        candidate=result.candidate,
        median_startup_ms=median(sample.startup_ms for sample in result.samples),
        median_latency_ms=median(sample.latency_ms for sample in result.samples),
        median_cpu_percent=median(sample.cpu_percent for sample in result.samples),
        median_memory_mb=median(sample.memory_mb for sample in result.samples),
        median_bytes_processed=median(sample.bytes_processed for sample in result.samples),
    )


def rank_qualification_results(
    results: list[QualificationResult],
    reviews: list[CandidateReview],
) -> list[CandidateScore]:
    """Rank only candidates with complete measurements, recovery, and approved review."""
    result_names = [result.candidate for result in results]
    review_names = [review.candidate for review in reviews]
    if len(result_names) != len(set(result_names)):
        raise RuntimeQualificationError(
            QualificationErrorCode.INSUFFICIENT_EVIDENCE,
            "qualification evidence contains duplicate candidate results",
        )
    if len(review_names) != len(set(review_names)):
        raise RuntimeQualificationError(
            QualificationErrorCode.INSUFFICIENT_EVIDENCE,
            "qualification evidence contains duplicate candidate reviews",
        )

    reviews_by_candidate = {review.candidate: review for review in reviews}
    missing_reviews = [name for name in result_names if name not in reviews_by_candidate]
    if missing_reviews:
        raise RuntimeQualificationError(
            QualificationErrorCode.INSUFFICIENT_EVIDENCE,
            "qualification evidence is missing a required candidate review",
        )

    scores = []
    for result in results:
        review = reviews_by_candidate[result.candidate]
        measurements_pass = all(sample.completed and sample.recovered for sample in result.samples)
        recovery_pass = result.recovery_sample.completed and result.recovery_sample.recovered
        if review.is_eligible() and measurements_pass and recovery_pass:
            scores.append(_aggregate_result(result))

    return sorted(
        scores,
        key=lambda score: (
            score.median_latency_ms,
            score.median_cpu_percent,
            score.median_memory_mb,
            score.median_startup_ms,
            score.candidate,
        ),
    )


def select_qualified_candidate(
    results: list[QualificationResult],
    reviews: list[CandidateReview],
) -> CandidateScore:
    """Select the highest-ranked candidate only from complete accepted evidence."""
    ranked = rank_qualification_results(results, reviews)
    if not ranked:
        raise RuntimeQualificationError(
            QualificationErrorCode.INSUFFICIENT_EVIDENCE,
            "no candidate has complete accepted qualification evidence",
        )
    return ranked[0]
