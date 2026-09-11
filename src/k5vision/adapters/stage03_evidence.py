"""Resource and reproducibility evidence required for Stage 03 selection."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from k5vision.adapters.runtime import (
    CandidateReview,
    CandidateScore,
    QualificationErrorCode,
    QualificationPlan,
    QualificationResult,
    RuntimeQualificationError,
    rank_qualification_results,
)


class QualificationContext(BaseModel):
    """Safe retained context proving candidates were measured comparably."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = "1"
    input_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    host_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    platform: Literal["windows", "linux"]
    architecture: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    plan: QualificationPlan


class ResourceMeasurement(BaseModel):
    """One bounded resource observation at an explicit load level."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    schema_version: Literal["1"] = "1"
    candidate: str = Field(min_length=1, max_length=128)
    load_units: int = Field(ge=1, le=100_000)
    cpu_percent: float = Field(ge=0)
    memory_mb: float = Field(ge=0)
    completed: bool


class ResourceProfile(BaseModel):
    """Comparable increasing-load resource evidence for one candidate."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = "1"
    candidate: str = Field(min_length=1, max_length=128)
    samples: list[ResourceMeasurement] = Field(min_length=2, max_length=32)

    @model_validator(mode="after")
    def validate_profile(self) -> Self:
        if any(sample.candidate != self.candidate for sample in self.samples):
            raise ValueError("resource samples must match the profile candidate")

        loads = [sample.load_units for sample in self.samples]
        if loads != sorted(loads) or len(loads) != len(set(loads)):
            raise ValueError("resource load levels must be unique and strictly increasing")
        return self

    @property
    def load_ladder(self) -> tuple[int, ...]:
        """Return the retained load sequence used for cross-candidate comparison."""
        return tuple(sample.load_units for sample in self.samples)


class Stage03CandidateScore(BaseModel):
    """Qualification score combined with comparable highest-load evidence."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    schema_version: Literal["1"] = "1"
    candidate: str = Field(min_length=1, max_length=128)
    qualification: CandidateScore
    max_load_units: int = Field(ge=1)
    high_load_cpu_percent: float = Field(ge=0)
    high_load_memory_mb: float = Field(ge=0)


class Stage03Evidence(BaseModel):
    """Complete comparable evidence required before Stage 03 may select a candidate."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["2"] = "2"
    context: QualificationContext
    results: list[QualificationResult] = Field(min_length=1, max_length=32)
    reviews: list[CandidateReview] = Field(min_length=1, max_length=32)
    resources: list[ResourceProfile] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def validate_evidence_identity(self) -> Self:
        result_names = [item.candidate for item in self.results]
        review_names = [item.candidate for item in self.reviews]
        resource_names = [item.candidate for item in self.resources]

        for names in (result_names, review_names, resource_names):
            if len(names) != len(set(names)):
                raise ValueError("stage 03 evidence contains duplicate candidate identity")

        if not (set(result_names) == set(review_names) == set(resource_names)):
            raise ValueError("stage 03 evidence must cover the same candidate set")

        if any(len(result.samples) != self.context.plan.scored_runs for result in self.results):
            raise ValueError("qualification results must match the retained scored-run plan")

        ladders = {profile.load_ladder for profile in self.resources}
        if len(ladders) != 1:
            raise ValueError("resource evidence must use one comparable load ladder")
        return self

    def rank(self) -> list[Stage03CandidateScore]:
        """Rank only candidates with complete review, recovery, and resource evidence."""
        base_scores = rank_qualification_results(self.results, self.reviews)
        resources_by_candidate = {item.candidate: item for item in self.resources}
        scores: list[Stage03CandidateScore] = []

        for base in base_scores:
            profile = resources_by_candidate[base.candidate]
            if not all(sample.completed for sample in profile.samples):
                continue
            high_load = profile.samples[-1]
            scores.append(
                Stage03CandidateScore(
                    candidate=base.candidate,
                    qualification=base,
                    max_load_units=high_load.load_units,
                    high_load_cpu_percent=high_load.cpu_percent,
                    high_load_memory_mb=high_load.memory_mb,
                )
            )

        return sorted(
            scores,
            key=lambda score: (
                score.qualification.median_latency_ms,
                score.high_load_cpu_percent,
                score.high_load_memory_mb,
                score.qualification.median_cpu_percent,
                score.qualification.median_memory_mb,
                score.qualification.median_startup_ms,
                score.candidate,
            ),
        )

    def select(self) -> Stage03CandidateScore:
        """Select only when every required Stage 03 evidence class is satisfied."""
        ranked = self.rank()
        if not ranked:
            raise RuntimeQualificationError(
                QualificationErrorCode.INSUFFICIENT_EVIDENCE,
                "no candidate has complete accepted stage 03 evidence",
            )
        return ranked[0]
