"""Build the source-free final Stage 03 acceptance bundle from measured capture."""

from __future__ import annotations

import argparse
import json
import platform
import sys
from hashlib import sha256
from pathlib import Path
from typing import Literal, Self
from urllib.parse import parse_qsl, urlsplit

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from k5vision.adapters.runtime import CandidateReview
from k5vision.adapters.stage03_evidence import (
    QualificationContext,
    Stage03CandidateScore,
    Stage03Evidence,
    Stage03SelectionRecord,
)
from k5vision.adapters.stage03_process import Stage03Capture

_HARDENING_FILES = (
    ".github/workflows/physical-validation.yml",
    "scripts/provision-stage03-gstreamer.ps1",
    "config/stage03-gstreamer-candidates.json",
    "config/stage03-gstreamer-reviews.json",
    "src/k5vision/adapters/stage03_process.py",
    "src/k5vision/stage03_credential_probe.py",
    "tests/integration/test_stage03_physical.py",
)


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return sha256(payload.encode("utf-8")).hexdigest()


def safe_input_fingerprint(source_uri: str) -> str:
    """Fingerprint only non-identifying stream shape, never host or credentials."""
    try:
        parsed = urlsplit(source_uri)
    except ValueError as exc:
        raise ValueError("Stage 03 source could not be fingerprinted safely") from exc
    if not parsed.scheme or not parsed.path:
        raise ValueError("Stage 03 source could not be fingerprinted safely")

    query_keys = sorted({key for key, _ in parse_qsl(parsed.query, keep_blank_values=True)})
    safe_shape = {
        "scheme": parsed.scheme.lower(),
        "path": parsed.path,
        "query_keys": query_keys,
    }
    return _canonical_sha256(safe_shape)


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hardening_hashes(repo_root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for relative in _HARDENING_FILES:
        path = repo_root / relative
        if not path.is_file():
            raise ValueError("Required Stage 03 hardening input is missing")
        result[relative] = _file_sha256(path)
    return result


def _platform_name() -> Literal["windows", "linux"]:
    system = platform.system().lower()
    if system == "windows":
        return "windows"
    if system == "linux":
        return "linux"
    raise ValueError("Stage 03 acceptance is only defined for Windows or Linux")


def _architecture() -> str:
    value = platform.machine().strip().replace("-", "_")
    if not value:
        raise ValueError("Stage 03 architecture is unavailable")
    return value


class Stage03HardeningEvidence(BaseModel):
    """Source-free proof of the exact hardened qualification boundary."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = "1"
    revision_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    workflow_run_id: int = Field(ge=1)
    runtime_version: str = Field(min_length=1, max_length=64)
    runtime_installer_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_free_capture: Literal[True] = True
    credential_output_sanitized: Literal[True] = True
    candidate_shell_disabled: Literal[True] = True
    isolated_runtime: Literal[True] = True
    upstream_sha256_verified: Literal[True] = True
    media_retained: Literal[False] = False
    file_sha256: dict[str, str]

    @model_validator(mode="after")
    def validate_file_hashes(self) -> Self:
        if set(self.file_sha256) != set(_HARDENING_FILES):
            raise ValueError("Stage 03 hardening evidence must bind every required file")
        if any(
            len(value) != 64 or any(character not in "0123456789abcdef" for character in value)
            for value in self.file_sha256.values()
        ):
            raise ValueError("Stage 03 hardening file fingerprints must be SHA-256 values")
        return self


class Stage03AcceptanceBundle(BaseModel):
    """Final source-free evidence and deterministic Gate 03 selection record."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = "1"
    evidence: Stage03Evidence
    ranking: list[Stage03CandidateScore] = Field(min_length=1, max_length=32)
    selection: Stage03SelectionRecord
    hardening: Stage03HardeningEvidence

    @model_validator(mode="after")
    def validate_derived_values(self) -> Self:
        expected_ranking = self.evidence.rank()
        expected_selection = self.evidence.selection_record()
        if self.ranking != expected_ranking:
            raise ValueError("Stage 03 retained ranking does not match retained evidence")
        if self.selection != expected_selection:
            raise ValueError("Stage 03 selection record does not match retained evidence")
        return self


def load_reviews(path: Path, *, runtime_version: str) -> list[CandidateReview]:
    """Load approved version-bound engineering reviews without relaxing eligibility."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        reviews = TypeAdapter(list[CandidateReview]).validate_python(raw)
    except (OSError, ValueError) as exc:
        raise ValueError("Stage 03 candidate reviews could not be loaded") from exc
    if len(reviews) < 2:
        raise ValueError("Stage 03 requires comparative candidate reviews")
    if len({review.candidate for review in reviews}) != len(reviews):
        raise ValueError("Stage 03 candidate reviews contain duplicate identities")
    if any(review.component_version != runtime_version for review in reviews):
        raise ValueError("Stage 03 candidate review version does not match measured runtime")
    if any(not review.is_eligible() for review in reviews):
        raise ValueError("Stage 03 candidate review is not approved for the required boundary")
    return reviews


def build_acceptance_bundle(
    *,
    capture: Stage03Capture,
    reviews: list[CandidateReview],
    source_uri: str,
    runtime_version: str,
    runtime_installer_sha256: str,
    revision_sha: str,
    workflow_run_id: int,
    repo_root: Path,
) -> Stage03AcceptanceBundle:
    """Bind measured results, reviews, safe context, and hardening to one record."""
    hardening_hashes = _hardening_hashes(repo_root)
    host_shape = {
        "platform": _platform_name(),
        "architecture": _architecture(),
        "python": platform.python_version(),
        "runtime_version": runtime_version,
        "runtime_installer_sha256": runtime_installer_sha256,
        "hardening_files": hardening_hashes,
    }
    context = QualificationContext(
        input_fingerprint=safe_input_fingerprint(source_uri),
        host_fingerprint=_canonical_sha256(host_shape),
        platform=_platform_name(),
        architecture=_architecture(),
        plan=capture.plan,
    )
    evidence = Stage03Evidence(
        context=context,
        results=capture.results,
        reviews=reviews,
        resources=capture.resources,
    )
    hardening = Stage03HardeningEvidence(
        revision_sha=revision_sha,
        workflow_run_id=workflow_run_id,
        runtime_version=runtime_version,
        runtime_installer_sha256=runtime_installer_sha256,
        file_sha256=hardening_hashes,
    )
    return Stage03AcceptanceBundle(
        evidence=evidence,
        ranking=evidence.rank(),
        selection=evidence.selection_record(),
        hardening=hardening,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Assemble source-free Stage 03 acceptance evidence")
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--reviews", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-uri", required=True)
    parser.add_argument("--runtime-version", required=True)
    parser.add_argument("--runtime-installer-sha256", required=True)
    parser.add_argument("--revision-sha", required=True)
    parser.add_argument("--workflow-run-id", type=int, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        capture = Stage03Capture.model_validate_json(args.capture.read_text(encoding="utf-8-sig"))
        reviews = load_reviews(args.reviews, runtime_version=args.runtime_version)
        bundle = build_acceptance_bundle(
            capture=capture,
            reviews=reviews,
            source_uri=args.source_uri,
            runtime_version=args.runtime_version,
            runtime_installer_sha256=args.runtime_installer_sha256.lower(),
            revision_sha=args.revision_sha.lower(),
            workflow_run_id=args.workflow_run_id,
            repo_root=args.repo_root,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(bundle.model_dump_json(indent=2), encoding="utf-8")
    except (OSError, ValueError) as exc:
        raise SystemExit("Stage 03 acceptance evidence could not be assembled safely") from exc

    print(f"Stage 03 selection: {bundle.selection.selected_candidate}")
    print(f"Stage 03 evidence SHA-256: {bundle.selection.evidence_sha256}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
