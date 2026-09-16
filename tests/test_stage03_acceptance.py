import json
from pathlib import Path

import pytest

from k5vision.adapters.runtime import CandidateReview, QualificationPlan, QualificationResult, RuntimeSample
from k5vision.adapters.stage03_evidence import ResourceMeasurement, ResourceProfile
from k5vision.adapters.stage03_process import Stage03Capture
from k5vision import stage03_acceptance


def _sample(candidate: str, latency_ms: float) -> RuntimeSample:
    return RuntimeSample(
        candidate=candidate,
        startup_ms=1,
        latency_ms=latency_ms,
        cpu_percent=10,
        memory_mb=32,
        bytes_processed=1024,
        recovered=True,
        completed=True,
    )


def _capture() -> Stage03Capture:
    plan = QualificationPlan(scored_runs=5, timeout_seconds=10)
    results = []
    resources = []
    for candidate, latency in (("gstreamer-tcp", 20.0), ("gstreamer-udp", 10.0)):
        results.append(
            QualificationResult(
                candidate=candidate,
                samples=[_sample(candidate, latency) for _ in range(5)],
                recovery_sample=_sample(candidate, latency + 1),
            )
        )
        resources.append(
            ResourceProfile(
                candidate=candidate,
                samples=[
                    ResourceMeasurement(
                        candidate=candidate,
                        load_units=load,
                        cpu_percent=10 * load,
                        memory_mb=32 * load,
                        completed=True,
                    )
                    for load in (1, 2, 3)
                ],
            )
        )
    return Stage03Capture(plan=plan, results=results, resources=resources)


def _reviews(version: str = "1.28.7") -> list[CandidateReview]:
    return [
        CandidateReview(
            candidate=candidate,
            component_version=version,
            license_id="LGPL-2.1-or-later",
            source_reference="versioned-upstream-reference",
            commercial_use_approved=True,
            redistribution_approved=True,
            windows_supported=True,
            linux_supported=True,
        )
        for candidate in ("gstreamer-tcp", "gstreamer-udp")
    ]


def _hardening_tree(root: Path) -> None:
    for relative in stage03_acceptance._HARDENING_FILES:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"hardening:{relative}\n", encoding="utf-8")


def test_safe_input_fingerprint_ignores_host_and_credentials() -> None:
    first = stage03_acceptance.safe_input_fingerprint(
        "rtsp://user:secret@192.0.2.10:554/stream1?profile=main"
    )
    second = stage03_acceptance.safe_input_fingerprint(
        "rtsp://other:password@198.51.100.20:8554/stream1?profile=different"
    )
    different_path = stage03_acceptance.safe_input_fingerprint(
        "rtsp://other:password@198.51.100.20:8554/stream2?profile=different"
    )

    assert first == second
    assert first != different_path


def test_acceptance_bundle_is_source_free_and_selects_from_retained_evidence(tmp_path: Path) -> None:
    _hardening_tree(tmp_path)
    source = "rtsp://user:secret@192.0.2.10:554/stream1"
    bundle = stage03_acceptance.build_acceptance_bundle(
        capture=_capture(),
        reviews=_reviews(),
        source_uri=source,
        runtime_version="1.28.7",
        runtime_installer_sha256="a" * 64,
        revision_sha="b" * 40,
        workflow_run_id=123,
        repo_root=tmp_path,
    )

    retained = bundle.model_dump_json()
    assert source not in retained
    assert "192.0.2.10" not in retained
    assert "secret" not in retained
    assert bundle.selection.selected_candidate == "gstreamer-udp"
    assert bundle.selection.evidence_sha256 == bundle.evidence.digest()
    assert bundle.hardening.media_retained is False
    assert bundle.hardening.upstream_sha256_verified is True


def test_load_reviews_requires_exact_measured_version_and_eligible_boundary(tmp_path: Path) -> None:
    review_path = tmp_path / "reviews.json"
    review_path.write_text(
        json.dumps([review.model_dump(mode="json") for review in _reviews("1.28.6")]),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="version"):
        stage03_acceptance.load_reviews(review_path, runtime_version="1.28.7")

    ineligible = _reviews()
    ineligible[0] = ineligible[0].model_copy(update={"redistribution_approved": False})
    review_path.write_text(
        json.dumps([review.model_dump(mode="json") for review in ineligible]),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="not approved"):
        stage03_acceptance.load_reviews(review_path, runtime_version="1.28.7")


def test_versioned_review_config_covers_candidate_config() -> None:
    root = Path(__file__).resolve().parents[1]
    reviews = stage03_acceptance.load_reviews(
        root / "config" / "stage03-gstreamer-reviews.json",
        runtime_version="1.28.7",
    )
    candidates = json.loads(
        (root / "config" / "stage03-gstreamer-candidates.json").read_text(encoding="utf-8")
    )

    assert {review.candidate for review in reviews} == {item["candidate"] for item in candidates}
