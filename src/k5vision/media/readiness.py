"""Versioned, source-free readiness qualification contracts for Stage 05."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ReadinessOutcome(StrEnum):
    PASS = "pass"
    START_FAILURE = "start_failure"
    STOP_FAILURE = "stop_failure"
    RECOVERY_FAILURE = "recovery_failure"
    TIMEOUT = "timeout"


class ReadinessPlan(BaseModel):
    """Bounded reproducible workload applied to the Stage-04 session boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    cycles_per_level: int = Field(default=3, ge=2, le=20)
    concurrency_ladder: tuple[int, ...] = Field(default=(1, 2), min_length=2, max_length=6)
    operation_timeout_seconds: float = Field(default=15.0, gt=0, le=60)

    @model_validator(mode="after")
    def validate_ladder(self) -> Self:
        if self.concurrency_ladder[0] != 1:
            raise ValueError("readiness concurrency ladder must begin at one session")
        if any(value <= 0 for value in self.concurrency_ladder):
            raise ValueError("readiness concurrency levels must be positive")
        if tuple(sorted(set(self.concurrency_ladder))) != self.concurrency_ladder:
            raise ValueError("readiness concurrency ladder must be unique and increasing")
        return self

    @property
    def expected_observations(self) -> int:
        return self.cycles_per_level * len(self.concurrency_ladder)


class ReadinessObservation(BaseModel):
    """One source-free workload observation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    level: int = Field(ge=1, le=32)
    cycle: int = Field(ge=1, le=20)
    attempted_sessions: int = Field(ge=1, le=32)
    completed_sessions: int = Field(ge=0, le=32)
    outcome: ReadinessOutcome
    elapsed_ms: int = Field(ge=0, le=3_600_000)

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if self.completed_sessions > self.attempted_sessions:
            raise ValueError("completed session count cannot exceed attempted count")
        if self.outcome == ReadinessOutcome.PASS and self.completed_sessions != self.attempted_sessions:
            raise ValueError("passing observation must complete every attempted session")
        return self


class ReadinessEvidence(BaseModel):
    """Evidence bound to one revision without source or runner identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    revision: str = Field(pattern=r"^(?:[0-9a-f]{40}|local)$")
    runtime: Literal["GStreamer 1.28.7"] = "GStreamer 1.28.7"
    transport: Literal["udp"] = "udp"
    plan: ReadinessPlan
    observations: tuple[ReadinessObservation, ...]

    @model_validator(mode="after")
    def validate_observations(self) -> Self:
        if len(self.observations) != self.plan.expected_observations:
            raise ValueError("readiness evidence does not contain the complete workload")
        expected = [
            (level, cycle)
            for level in self.plan.concurrency_ladder
            for cycle in range(1, self.plan.cycles_per_level + 1)
        ]
        actual = [(item.level, item.cycle) for item in self.observations]
        if actual != expected:
            raise ValueError("readiness observations must follow the deterministic workload order")
        for item in self.observations:
            if item.attempted_sessions != item.level:
                raise ValueError("observation attempted count must match its concurrency level")
        return self

    @property
    def accepted(self) -> bool:
        return all(item.outcome == ReadinessOutcome.PASS for item in self.observations)
