"""Project-owned contracts for runtime qualification."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from enum import StrEnum
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


class QualificationErrorCode(StrEnum):
    """Stable failure classes exposed by the qualification boundary."""

    TIMEOUT = "timeout"
    CANDIDATE_FAILURE = "candidate_failure"
    INVALID_SAMPLE = "invalid_sample"


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

    async def measure_recovery(
        self, source_uri: str, *, timeout_seconds: float
    ) -> RuntimeSample:
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
