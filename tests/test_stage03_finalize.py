import json
from pathlib import Path

import pytest

from k5vision import stage03_finalize
from k5vision.adapters import runtime, stage03_evidence, stage03_process

INSTALLER_SHA = "0" * 64


def sample(candidate: str, latency: float) -> runtime.RuntimeSample:
    return runtime.RuntimeSample(
        candidate=candidate,
        startup_ms=1,
        latency_ms=latency,
        cpu_percent=10,
        memory_mb=20,
        bytes_processed=0,
        recovered=True,
        completed=True,
    )


def capture() -> stage03_process.Stage03Capture:
    plan = runtime.QualificationPlan(scored_runs=5, timeout_seconds=10)
    results = []
    resources = []
    for candidate, latency in (("candidate-a", 20.0), ("candidate-b", 10.0)):
        results.append(
            runtime.QualificationResult(
                candidate=candidate,
                samples=[sample(candidate, latency) for _ in range(5)],
                recovery_sample=sample(candidate, latency),
            )
        )
        resources.append(
            stage03_evidence.ResourceProfile(
                candidate=candidate,
                samples=[
                    stage03_evidence.ResourceMeasurement(
                        candidate=candidate,
                        load_units=load,
                        cpu_percent=float(load * 10),
                        memory_mb=float(load * 20),
                        completed=True,
                    )
                    for load in (1, 2, 3)
                ],
            )
        )
    return stage03_process.Stage03Capture(plan=plan, results=results, resources=resources)


def reviews() -> list[runtime.CandidateReview]:
    return [
        runtime.CandidateReview(
            candidate=candidate,
            component_version="1.0",
            license_id="MIT",
            source_reference="immutable-source",
            commercial_use_approved=True,
            redistribution_approved=True,
            windows_supported=True,
            linux_supported=True,
        )
        for candidate in ("candidate-a", "candidate-b")
    ]


def write_inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    capture_path = tmp_path / "capture.json"
    review_path = tmp_path / "reviews.json"
    config_path = tmp_path / "candidates.json"
    capture_path.write_text(capture().model_dump_json(), encoding="utf-8")
    review_path.write_text(
        json.dumps([item.model_dump(mode="json") for item in reviews()]),
        encoding="utf-8",
    )
    config_path.write_text('[{"candidate":"candidate-a"}]', encoding="utf-8")
    return capture_path, review_path, config_path


def patch_windows_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stage03_finalize.platform, "system", lambda: "Windows")
    monkeypatch.setattr(stage03_finalize.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(stage03_finalize.platform, "node", lambda: "private-host-name")
    monkeypatch.setattr(stage03_finalize.platform, "python_version", lambda: "3.12.10")
    monkeypatch.setenv("RUNNER_NAME", "private-runner-name")


def test_build_context_retains_only_secret_bound_fingerprints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_windows_host(monkeypatch)
    config_path = tmp_path / "candidate-config.json"
    config_path.write_text("[]", encoding="utf-8")

    context = stage03_finalize.build_context(
        capture(),
        source_uri="rtsp://private-camera/live",
        secret_material="private-camera-credential",
        candidate_config_path=config_path,
        installer_sha256=INSTALLER_SHA,
    )
    retained = context.model_dump_json()

    assert context.platform == "windows"
    assert context.architecture == "AMD64"
    assert len(context.input_fingerprint) == 64
    assert len(context.host_fingerprint) == 64
    assert "private-camera" not in retained
    assert "private-host-name" not in retained
    assert "private-runner-name" not in retained
    assert "private-camera-credential" not in retained


def test_build_context_rejects_invalid_private_or_runtime_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_windows_host(monkeypatch)
    config_path = tmp_path / "candidate-config.json"
    config_path.write_text("[]", encoding="utf-8")
    common = {
        "capture": capture(),
        "candidate_config_path": config_path,
        "installer_sha256": INSTALLER_SHA,
    }

    with pytest.raises(ValueError, match="source"):
        stage03_finalize.build_context(**common, source_uri="", secret_material="secret")
    with pytest.raises(ValueError, match="key material"):
        stage03_finalize.build_context(**common, source_uri="rtsp://camera", secret_material="")
    with pytest.raises(ValueError, match="SHA-256"):
        stage03_finalize.build_context(
            capture(),
            source_uri="rtsp://camera",
            secret_material="secret",
            candidate_config_path=config_path,
            installer_sha256="bad",
        )

    monkeypatch.setattr(stage03_finalize.platform, "system", lambda: "Darwin")
    with pytest.raises(ValueError, match="Windows or Linux"):
        stage03_finalize.build_context(**common, source_uri="rtsp://camera", secret_material="secret")

    monkeypatch.setattr(stage03_finalize.platform, "system", lambda: "Windows")
    monkeypatch.setattr(stage03_finalize.platform, "machine", lambda: "")
    with pytest.raises(ValueError, match="architecture"):
        stage03_finalize.build_context(**common, source_uri="rtsp://camera", secret_material="secret")


def test_finalize_writes_complete_source_free_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_windows_host(monkeypatch)
    capture_path, review_path, config_path = write_inputs(tmp_path)
    evidence_path = tmp_path / "final-evidence.json"
    selection_path = tmp_path / "selection.json"

    selected = stage03_finalize.finalize(
        capture_path=capture_path,
        review_path=review_path,
        candidate_config_path=config_path,
        evidence_output=evidence_path,
        selection_output=selection_path,
        source_uri="rtsp://private-camera/live",
        secret_material="private-camera-credential",
        installer_sha256=INSTALLER_SHA,
    )

    evidence = stage03_evidence.Stage03Evidence.model_validate_json(evidence_path.read_text())
    selection = stage03_evidence.Stage03SelectionRecord.model_validate_json(
        selection_path.read_text()
    )
    retained = evidence_path.read_text() + selection_path.read_text()

    assert selected == "candidate-b"
    assert evidence.select().candidate == "candidate-b"
    assert selection.selected_candidate == "candidate-b"
    assert selection == evidence.selection_record()
    assert "rtsp://private-camera/live" not in retained
    assert "private-camera-credential" not in retained


def test_finalize_detects_sensitive_retention_regression(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_windows_host(monkeypatch)
    capture_path, review_path, config_path = write_inputs(tmp_path)
    evidence_path = tmp_path / "evidence.json"
    selection_path = tmp_path / "selection.json"
    original_write_text = Path.write_text

    def contaminated_write(self: Path, data: str, *args: object, **kwargs: object) -> int:
        if self == selection_path:
            data += "private-camera-credential"
        return original_write_text(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", contaminated_write)
    with pytest.raises(RuntimeError, match="sensitive configuration"):
        stage03_finalize.finalize(
            capture_path=capture_path,
            review_path=review_path,
            candidate_config_path=config_path,
            evidence_output=evidence_path,
            selection_output=selection_path,
            source_uri="rtsp://private-camera/live",
            secret_material="private-camera-credential",
            installer_sha256=INSTALLER_SHA,
        )


def test_main_uses_private_environment_without_echoing_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    patch_windows_host(monkeypatch)
    capture_path, review_path, config_path = write_inputs(tmp_path)
    evidence_path = tmp_path / "evidence.json"
    selection_path = tmp_path / "selection.json"
    monkeypatch.setenv("K5_STAGE03_SOURCE", "rtsp://private-camera/live")
    monkeypatch.setenv("K5_STAGE03_CAM_CRED", "private-camera-credential")
    monkeypatch.setenv("K5_GSTREAMER_INSTALLER_SHA256", INSTALLER_SHA)
    monkeypatch.setattr(
        "sys.argv",
        [
            "stage03_finalize",
            "--capture",
            str(capture_path),
            "--reviews",
            str(review_path),
            "--candidate-config",
            str(config_path),
            "--evidence-output",
            str(evidence_path),
            "--selection-output",
            str(selection_path),
        ],
    )

    assert stage03_finalize.main() == 0
    output = capsys.readouterr().out
    assert "candidate-b" in output
    assert "private-camera" not in output
    assert "private-camera-credential" not in output
