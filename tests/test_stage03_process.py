import asyncio
import sys

import psutil
import pytest
from pydantic import ValidationError

from k5vision.adapters.runtime import QualificationPlan
from k5vision.adapters.stage03_process import (
    ProcessCandidateSpec,
    ProcessRuntimeCandidate,
    capture_candidate_set,
    measure_resource_profile,
)


def _spec(name: str, *, sleep_seconds: float = 0.08) -> ProcessCandidateSpec:
    return ProcessCandidateSpec(
        candidate=name,
        argv=[
            sys.executable,
            "-c",
            f"import time; time.sleep({sleep_seconds})",
            "{source}",
        ],
        interruption_seconds=0.02,
        poll_interval_seconds=0.01,
    )


def test_process_spec_requires_exactly_one_source_token() -> None:
    with pytest.raises(ValidationError):
        ProcessCandidateSpec(candidate="a", argv=[sys.executable, "-c", "pass"])

    with pytest.raises(ValidationError):
        ProcessCandidateSpec(
            candidate="a",
            argv=[sys.executable, "{source}", "{source}"],
        )


def test_process_candidate_measurement_is_bounded_and_project_owned() -> None:
    candidate = ProcessRuntimeCandidate(_spec("candidate-a"))

    sample = asyncio.run(candidate.measure("local-test-input", timeout_seconds=1))

    assert sample.candidate == "candidate-a"
    assert sample.completed is True
    assert sample.recovered is True
    assert sample.latency_ms > 0
    assert sample.startup_ms >= 0
    assert sample.cpu_percent >= 0
    assert sample.memory_mb >= 0
    assert "local-test-input" not in sample.model_dump_json()


def test_process_candidate_nonzero_exit_is_retained_as_failure() -> None:
    spec = ProcessCandidateSpec(
        candidate="candidate-a",
        argv=[sys.executable, "-c", "raise SystemExit(3)", "{source}"],
        interruption_seconds=0.02,
        poll_interval_seconds=0.01,
    )
    sample = asyncio.run(ProcessRuntimeCandidate(spec).measure("input", timeout_seconds=1))

    assert sample.completed is False
    assert sample.recovered is False


def test_process_candidate_recovery_interrupts_then_reenters() -> None:
    candidate = ProcessRuntimeCandidate(_spec("candidate-a", sleep_seconds=0.12))

    sample = asyncio.run(candidate.measure_recovery("input", timeout_seconds=1))

    assert sample.completed is True
    assert sample.recovered is True


def test_process_candidate_includes_descendant_resource_usage() -> None:
    child_code = "import time; payload=bytearray(32 * 1024 * 1024); payload[0]=1; time.sleep(0.15)"
    parent_code = (
        "import subprocess, sys; "
        f"child=subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        "child.wait()"
    )
    spec = ProcessCandidateSpec(
        candidate="candidate-a",
        argv=[sys.executable, "-c", parent_code, "{source}"],
        interruption_seconds=0.02,
        poll_interval_seconds=0.01,
    )

    sample = asyncio.run(ProcessRuntimeCandidate(spec).measure("input", timeout_seconds=1))

    assert sample.completed is True
    assert sample.memory_mb >= 24


def test_process_candidate_cleans_orphan_descendants(tmp_path) -> None:
    pid_path = tmp_path / "child.pid"
    child_code = "import time; time.sleep(5)"
    parent_code = (
        "import subprocess, sys, time; "
        f"child=subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        f"open({str(pid_path)!r}, 'w', encoding='utf-8').write(str(child.pid)); "
        "time.sleep(0.08)"
    )
    spec = ProcessCandidateSpec(
        candidate="candidate-a",
        argv=[sys.executable, "-c", parent_code, "{source}"],
        interruption_seconds=0.02,
        poll_interval_seconds=0.01,
    )

    sample = asyncio.run(ProcessRuntimeCandidate(spec).measure("input", timeout_seconds=1))

    child_pid = int(pid_path.read_text(encoding="utf-8"))
    assert sample.completed is False
    assert not psutil.pid_exists(child_pid)


def test_resource_profile_uses_bounded_comparable_load_ladder() -> None:
    candidate = ProcessRuntimeCandidate(_spec("candidate-a"))

    profile = asyncio.run(
        measure_resource_profile(
            "candidate-a",
            candidate,
            "input",
            load_ladder=(1, 2),
            timeout_seconds=1,
        )
    )

    assert profile.load_ladder == (1, 2)
    assert all(sample.completed for sample in profile.samples)
    assert profile.samples[1].memory_mb >= 0


@pytest.mark.parametrize("ladder", [(1,), (2, 1), (1, 1), (1, 33)])
def test_resource_profile_rejects_unsafe_load_ladders(ladder: tuple[int, ...]) -> None:
    candidate = ProcessRuntimeCandidate(_spec("candidate-a"))

    with pytest.raises(ValueError):
        asyncio.run(
            measure_resource_profile(
                "candidate-a",
                candidate,
                "input",
                load_ladder=ladder,
                timeout_seconds=1,
            )
        )


def test_capture_candidate_set_retains_measurements_without_source() -> None:
    capture = asyncio.run(
        capture_candidate_set(
            [_spec("candidate-a"), _spec("candidate-b", sleep_seconds=0.1)],
            "private-source-value",
            plan=QualificationPlan(scored_runs=5, timeout_seconds=1),
            load_ladder=(1, 2),
        )
    )

    payload = capture.model_dump_json()
    assert len(capture.results) == 2
    assert len(capture.resources) == 2
    assert "private-source-value" not in payload
    assert {item.candidate for item in capture.results} == {"candidate-a", "candidate-b"}


def test_capture_candidate_set_requires_unique_comparative_candidates() -> None:
    with pytest.raises(ValueError):
        asyncio.run(capture_candidate_set([_spec("candidate-a")], "input"))

    with pytest.raises(ValueError):
        asyncio.run(
            capture_candidate_set(
                [_spec("candidate-a"), _spec("candidate-a")],
                "input",
            )
        )
