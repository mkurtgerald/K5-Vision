import asyncio
import json
import os
from pathlib import Path

import pytest

from k5vision.adapters.runtime import QualificationPlan
from k5vision.adapters.stage03_process import ProcessCandidateSpec, capture_candidate_set


def test_stage03_physical_capture() -> None:
    source_uri = os.getenv("K5_STAGE03_SOURCE")
    if not source_uri:
        pytest.skip("K5_STAGE03_SOURCE is not configured")

    raw_specs = os.getenv("K5_STAGE03_CANDIDATES_JSON")
    if not raw_specs:
        pytest.fail("K5_STAGE03_CANDIDATES_JSON is required for physical capture")

    output_path = os.getenv("K5_STAGE03_OUTPUT")
    if not output_path:
        pytest.fail("K5_STAGE03_OUTPUT is required so measured evidence is retained")

    scored_runs = int(os.getenv("K5_STAGE03_SCORED_RUNS", "5"))
    timeout_seconds = float(os.getenv("K5_STAGE03_TIMEOUT", "10"))
    load_ladder = tuple(
        int(value.strip())
        for value in os.getenv("K5_STAGE03_LOAD_LADDER", "1,2").split(",")
        if value.strip()
    )

    specs = [ProcessCandidateSpec.model_validate(item) for item in json.loads(raw_specs)]
    capture = asyncio.run(
        capture_candidate_set(
            specs,
            source_uri,
            plan=QualificationPlan(
                scored_runs=scored_runs,
                timeout_seconds=timeout_seconds,
            ),
            load_ladder=load_ladder,
        )
    )

    assert all(
        sample.completed and sample.recovered
        for result in capture.results
        for sample in [*result.samples, result.recovery_sample]
    )
    assert all(sample.completed for profile in capture.resources for sample in profile.samples)

    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(capture.model_dump_json(indent=2), encoding="utf-8")
    retained = destination.read_text(encoding="utf-8")
    assert source_uri not in retained
