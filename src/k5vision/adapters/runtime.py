"""Project-owned contracts for runtime qualification."""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, Field


class RuntimeSample(BaseModel):
    """One reproducible qualification sample without source or credential retention."""

    schema_version: Literal["1"] = "1"
    candidate: str = Field(min_length=1)
    startup_ms: float = Field(ge=0)
    latency_ms: float = Field(ge=0)
    cpu_percent: float = Field(ge=0)
    memory_mb: float = Field(ge=0)
    bytes_processed: int = Field(ge=0)
    recovered: bool
    completed: bool


class RuntimeCandidate(Protocol):
    """Replaceable boundary implemented by each qualification candidate."""

    async def measure(self, source_uri: str, *, timeout_seconds: float) -> RuntimeSample:
        """Measure one bounded run and return only project-owned data."""
        ...


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
