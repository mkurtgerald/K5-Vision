import pytest
from pydantic import ValidationError

from k5vision.adapters.runtime import RuntimeSample, rank_samples


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
