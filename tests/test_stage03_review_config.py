import json
from pathlib import Path

from pydantic import TypeAdapter

from k5vision.adapters.runtime import CandidateReview
from k5vision.adapters.stage03_process import ProcessCandidateSpec

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_versioned_stage03_reviews_match_runtime_candidates() -> None:
    candidates = TypeAdapter(list[ProcessCandidateSpec]).validate_json(
        (PROJECT_ROOT / "config/stage03-gstreamer-candidates.json").read_text(encoding="utf-8")
    )
    reviews = TypeAdapter(list[CandidateReview]).validate_json(
        (PROJECT_ROOT / "config/stage03-gstreamer-reviews.json").read_text(encoding="utf-8")
    )

    assert {item.candidate for item in candidates} == {item.candidate for item in reviews}
    assert len(reviews) >= 2
    assert all(item.is_eligible() for item in reviews)
    assert all(item.license_id == "LGPL-2.1-or-later" for item in reviews)
    assert all("1.28.7" in item.component_version for item in reviews)
    assert all("032fc6062b8539838fc8da22589cb9b24c5d820baa7f8cc160af9ea08395badf" in item.source_reference for item in reviews)


def test_versioned_review_file_contains_no_private_source_fields() -> None:
    payload = json.loads(
        (PROJECT_ROOT / "config/stage03-gstreamer-reviews.json").read_text(encoding="utf-8")
    )
    retained = json.dumps(payload, sort_keys=True).lower()

    assert "rtsp://" not in retained
    assert "username" not in retained
    assert "password" not in retained
    assert "credential" not in retained
