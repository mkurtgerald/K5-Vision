"""Assemble source-free Stage 03 evidence and deterministic selection records."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import platform
from pathlib import Path

from pydantic import TypeAdapter

from k5vision.adapters.runtime import CandidateReview
from k5vision.adapters.stage03_evidence import QualificationContext, Stage03Evidence
from k5vision.adapters.stage03_process import Stage03Capture

_REVIEWS_ADAPTER = TypeAdapter(list[CandidateReview])


def _secret_key(secret_material: str) -> bytes:
    if not secret_material.strip():
        raise ValueError("Stage 03 fingerprint key material is required")
    return hashlib.sha256(secret_material.encode("utf-8")).digest()


def _fingerprint(key: bytes, value: bytes) -> str:
    return hmac.new(key, value, hashlib.sha256).hexdigest()


def _candidate_config_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_context(
    capture: Stage03Capture,
    *,
    source_uri: str,
    secret_material: str,
    candidate_config_path: Path,
    installer_sha256: str,
) -> QualificationContext:
    """Build only secret-bound fingerprints; never retain raw source or host identity."""
    if not source_uri.strip():
        raise ValueError("Stage 03 source is required")
    installer_sha256 = installer_sha256.strip().lower()
    if len(installer_sha256) != 64 or any(char not in "0123456789abcdef" for char in installer_sha256):
        raise ValueError("Reviewed runtime SHA-256 is required")

    key = _secret_key(secret_material)
    system = platform.system().lower()
    if system not in {"windows", "linux"}:
        raise ValueError("Stage 03 final evidence supports Windows or Linux only")

    architecture = platform.machine().strip()
    if not architecture:
        raise ValueError("Stage 03 host architecture is unavailable")

    input_fingerprint = _fingerprint(key, source_uri.encode("utf-8"))
    host_payload = json.dumps(
        {
            "architecture": architecture,
            "candidate_config_sha256": _candidate_config_digest(candidate_config_path),
            "host": platform.node(),
            "installer_sha256": installer_sha256,
            "platform": system,
            "python": platform.python_version(),
            "runner": os.getenv("RUNNER_NAME", ""),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    host_fingerprint = _fingerprint(key, host_payload)

    return QualificationContext(
        input_fingerprint=input_fingerprint,
        host_fingerprint=host_fingerprint,
        platform=system,
        architecture=architecture,
        plan=capture.plan,
    )


def assemble_evidence(
    capture: Stage03Capture,
    reviews: list[CandidateReview],
    context: QualificationContext,
) -> Stage03Evidence:
    """Attach versioned review/context evidence to a retained physical capture."""
    return Stage03Evidence(
        context=context,
        results=capture.results,
        reviews=reviews,
        resources=capture.resources,
    )


def finalize(
    *,
    capture_path: Path,
    review_path: Path,
    candidate_config_path: Path,
    evidence_output: Path,
    selection_output: Path,
    source_uri: str,
    secret_material: str,
    installer_sha256: str,
) -> str:
    """Create source-free final evidence and return the measured selected candidate."""
    capture = Stage03Capture.model_validate_json(capture_path.read_text(encoding="utf-8-sig"))
    reviews = _REVIEWS_ADAPTER.validate_json(review_path.read_text(encoding="utf-8-sig"))
    context = build_context(
        capture,
        source_uri=source_uri,
        secret_material=secret_material,
        candidate_config_path=candidate_config_path,
        installer_sha256=installer_sha256,
    )
    evidence = assemble_evidence(capture, reviews, context)
    selection = evidence.selection_record()

    evidence_output.parent.mkdir(parents=True, exist_ok=True)
    selection_output.parent.mkdir(parents=True, exist_ok=True)
    evidence_output.write_text(
        evidence.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    selection_output.write_text(
        selection.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )

    retained = evidence_output.read_text(encoding="utf-8") + selection_output.read_text(
        encoding="utf-8"
    )
    if source_uri in retained or secret_material in retained:
        raise RuntimeError("Stage 03 retained evidence contained sensitive configuration")
    return selection.selected_candidate


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Finalize source-free Stage 03 evidence")
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--reviews", type=Path, required=True)
    parser.add_argument("--candidate-config", type=Path, required=True)
    parser.add_argument("--evidence-output", type=Path, required=True)
    parser.add_argument("--selection-output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    source_uri = os.getenv("K5_STAGE03_SOURCE", "")
    secret_material = os.getenv("K5_STAGE03_CAM_CRED", "")
    installer_sha256 = os.getenv("K5_GSTREAMER_INSTALLER_SHA256", "")
    selected = finalize(
        capture_path=args.capture,
        review_path=args.reviews,
        candidate_config_path=args.candidate_config,
        evidence_output=args.evidence_output,
        selection_output=args.selection_output,
        source_uri=source_uri,
        secret_material=secret_material,
        installer_sha256=installer_sha256,
    )
    print(f"Stage 03 final evidence assembled; selected candidate: {selected}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
