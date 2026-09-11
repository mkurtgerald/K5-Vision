import pytest
from pydantic import ValidationError

from k5vision.adapters import runtime, stage03_evidence

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


def qualification_context(*, scored_runs: int = 5) -> stage03_evidence.QualificationContext:
    return stage03_evidence.QualificationContext(
        input_fingerprint=DIGEST_A,
        host_fingerprint=DIGEST_B,
        platform="windows",
        architecture="x86_64",
        plan=runtime.QualificationPlan(scored_runs=scored_runs, timeout_seconds=10),
    )


def runtime_sample(candidate: str, *, latency: float) -> runtime.RuntimeSample:
    return runtime.RuntimeSample(
        candidate=candidate,
        startup_ms=20,
        latency_ms=latency,
        cpu_percent=10,
        memory_mb=64,
        bytes_processed=1024,
        recovered=True,
        completed=True,
    )


def result(candidate: str, *, latency: float = 10, runs: int = 5) -> runtime.QualificationResult:
    return runtime.QualificationResult(
        candidate=candidate,
        samples=[runtime_sample(candidate, latency=latency) for _ in range(runs)],
        recovery_sample=runtime_sample(candidate, latency=20),
    )


def review(candidate: str) -> runtime.CandidateReview:
    return runtime.CandidateReview(
        candidate=candidate,
        component_version="1.0.0",
        license_id="MIT",
        source_reference="immutable-source-ref",
        commercial_use_approved=True,
        redistribution_approved=True,
        windows_supported=True,
        linux_supported=True,
    )


def resource_profile(
    candidate: str,
    *,
    cpu_values: tuple[float, float, float] = (10, 20, 30),
    completed: bool = True,
    loads: tuple[int, int, int] = (1, 2, 4),
) -> stage03_evidence.ResourceProfile:
    return stage03_evidence.ResourceProfile(
        candidate=candidate,
        samples=[
            stage03_evidence.ResourceMeasurement(
                candidate=candidate,
                load_units=load,
                cpu_percent=cpu,
                memory_mb=64 + load,
                completed=completed,
            )
            for load, cpu in zip(loads, cpu_values, strict=True)
        ],
    )


def test_qualification_context_requires_safe_fixed_fingerprints() -> None:
    with pytest.raises(ValidationError):
        stage03_evidence.QualificationContext(
            input_fingerprint="rtsp://private.example/live",
            host_fingerprint=DIGEST_B,
            platform="windows",
            architecture="x86_64",
            plan=runtime.QualificationPlan(),
        )

    with pytest.raises(ValidationError):
        stage03_evidence.QualificationContext(
            input_fingerprint=DIGEST_A,
            host_fingerprint=DIGEST_B,
            platform="windows",
            architecture="x86_64",
            plan=runtime.QualificationPlan(),
            source_uri="rtsp://private.example/live",
        )


def test_resource_profile_requires_strictly_increasing_unique_loads() -> None:
    with pytest.raises(ValidationError):
        resource_profile("candidate-a", loads=(1, 4, 2))

    with pytest.raises(ValidationError):
        resource_profile("candidate-a", loads=(1, 2, 2))


def test_stage03_evidence_requires_same_candidate_set() -> None:
    with pytest.raises(ValidationError):
        stage03_evidence.Stage03Evidence(
            context=qualification_context(),
            results=[result("candidate-a")],
            reviews=[review("candidate-a")],
            resources=[resource_profile("candidate-b")],
        )


def test_stage03_evidence_requires_retained_plan_run_count() -> None:
    with pytest.raises(ValidationError):
        stage03_evidence.Stage03Evidence(
            context=qualification_context(scored_runs=6),
            results=[result("candidate-a", runs=5)],
            reviews=[review("candidate-a")],
            resources=[resource_profile("candidate-a")],
        )


def test_stage03_evidence_requires_comparable_load_ladder() -> None:
    with pytest.raises(ValidationError):
        stage03_evidence.Stage03Evidence(
            context=qualification_context(),
            results=[result("candidate-a"), result("candidate-b")],
            reviews=[review("candidate-a"), review("candidate-b")],
            resources=[
                resource_profile("candidate-a", loads=(1, 2, 4)),
                resource_profile("candidate-b", loads=(1, 3, 4)),
            ],
        )


def test_stage03_ranking_uses_high_load_resource_measurement_on_latency_tie() -> None:
    evidence = stage03_evidence.Stage03Evidence(
        context=qualification_context(),
        results=[result("candidate-a"), result("candidate-b")],
        reviews=[review("candidate-a"), review("candidate-b")],
        resources=[
            resource_profile("candidate-a", cpu_values=(10, 30, 80)),
            resource_profile("candidate-b", cpu_values=(10, 20, 40)),
        ],
    )

    ranked = evidence.rank()

    assert evidence.schema_version == "2"
    assert [item.candidate for item in ranked] == ["candidate-b", "candidate-a"]
    assert ranked[0].max_load_units == 4
    assert ranked[0].high_load_cpu_percent == 40
    assert evidence.select().candidate == "candidate-b"


def test_stage03_selection_excludes_incomplete_resource_profile() -> None:
    evidence = stage03_evidence.Stage03Evidence(
        context=qualification_context(),
        results=[result("candidate-a")],
        reviews=[review("candidate-a")],
        resources=[resource_profile("candidate-a", completed=False)],
    )

    with pytest.raises(runtime.RuntimeQualificationError):
        evidence.select()


def test_resource_evidence_rejects_unbounded_or_unexpected_values() -> None:
    with pytest.raises(ValidationError):
        stage03_evidence.ResourceMeasurement(
            candidate="candidate-a",
            load_units=1,
            cpu_percent=float("inf"),
            memory_mb=1,
            completed=True,
        )

    with pytest.raises(ValidationError):
        stage03_evidence.ResourceMeasurement(
            candidate="candidate-a",
            load_units=1,
            cpu_percent=1,
            memory_mb=1,
            completed=True,
            source_uri="rtsp://private.example/live",
        )
