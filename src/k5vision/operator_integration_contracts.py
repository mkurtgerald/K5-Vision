"""Independent K5-side codecs for published, non-authorizing lab contracts.

These are data adapters, not copied research implementations or execution grants.
Neither upstream lab currently publishes a shared Neural service endpoint.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from k5vision.integration_contracts import EventStatus, NormalizedEvent
from k5vision.neural_contracts import ObservationEnvelope

_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_LAB_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_MAX_EVENT_BYTES = 64 * 1024


class IntegrationContractError(ValueError):
    """Finite contract refusal; remote/private strings are never echoed."""


def _token(value: Any) -> str:
    if type(value) is not str or not _TOKEN.fullmatch(value):
        raise IntegrationContractError("integration_invalid_event")
    return value


def _lab_token(value: Any) -> str:
    if type(value) is not str or not _LAB_TOKEN.fullmatch(value):
        raise IntegrationContractError("integration_invalid_response")
    return value


def _confidence(value: Any) -> float:
    if type(value) not in (float, int) or not math.isfinite(value) or not 0 <= value <= 1:
        raise IntegrationContractError("integration_invalid_event")
    return float(value)


def _time(value: Any) -> datetime:
    if type(value) is not str or not 20 <= len(value) <= 40:
        raise IntegrationContractError("integration_invalid_event")
    try:
        result = datetime.fromisoformat(value)
        if result.utcoffset() is None:
            raise ValueError
        return result.astimezone(UTC)
    except (ValueError, OverflowError):
        raise IntegrationContractError("integration_invalid_event") from None


def bounded_document(value: Any) -> dict:
    try:
        if type(value) is not dict:
            raise ValueError
        raw = json.dumps(value, allow_nan=False, ensure_ascii=True, separators=(",", ":"))
        if len(raw) > _MAX_EVENT_BYTES:
            raise ValueError
        return json.loads(raw)
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise IntegrationContractError("integration_invalid_event") from None


def analytics_lab_event(
    value: dict, *, connection_id: UUID, site_id: str, ingested_at: datetime
) -> NormalizedEvent:
    """Map Analytics Lab's public draft0.1 envelope without accepting authority.

    Camera source UUIDs are checked separately against K5's current registry.
    Unsupported evidence/metadata remain outside the trusted canonical envelope;
    recommended_actions never grant or trigger any production action.
    """
    event = bounded_document(value)
    required = {
        "schema_version",
        "event_id",
        "analytic_id",
        "event_type",
        "source_id",
        "start_time",
        "end_time",
        "confidence",
        "status",
    }
    allowed = required | {
        "site_id",
        "severity",
        "objects",
        "zones",
        "observations",
        "evidence",
        "relationships",
        "recommended_actions",
        "metadata",
    }
    if not required <= event.keys() or not event.keys() <= allowed:
        raise IntegrationContractError("integration_invalid_event")
    if event["schema_version"] != "0.1" or event.get("site_id", site_id) not in (None, site_id):
        raise IntegrationContractError("integration_incompatible")
    try:
        UUID(event["event_id"])
        camera = str(UUID(event["source_id"]))
        analytic = _token(event["analytic_id"])
        kind = _token(event["event_type"])
        first, last = _time(event["start_time"]), _time(event["end_time"])
        if not isinstance(ingested_at, datetime) or ingested_at.utcoffset() is None:
            raise ValueError
        ingested_at = ingested_at.astimezone(UTC)
        if last < first:
            raise IntegrationContractError("integration_time_inconsistent")
        if ingested_at < first:
            # Preserve observation UTC. Never invent later ingestion to hide a
            # future source timestamp or backward host correction.
            raise IntegrationContractError("integration_source_clock_ahead")
        state = EventStatus(event["status"])
        for key in (
            "objects",
            "zones",
            "observations",
            "evidence",
            "relationships",
            "recommended_actions",
        ):
            if key in event and (type(event[key]) is not list or len(event[key]) > 128):
                raise ValueError
        if "metadata" in event and type(event["metadata"]) is not dict:
            raise ValueError
        digest = hashlib.sha256(
            (str(connection_id) + ":" + str(UUID(event["event_id"]))).encode("ascii")
        ).hexdigest()
        return NormalizedEvent(
            event_id=digest,
            producer_id="analytics-" + str(connection_id),
            source_id=camera,
            event_type=kind,
            observed_at=first,
            ingested_at=ingested_at,
            processed_at=ingested_at,
            status=state,
            confidence=_confidence(event["confidence"]),
            extensions={
                "analytic_id": analytic,
                "source_schema": "0.1",
                "source_event_id": str(UUID(event["event_id"])),
                "end_time": last.isoformat(),
                "authority": "observe_only",
            },
        )
    except IntegrationContractError:
        raise
    except (ValueError, TypeError, KeyError, OverflowError):
        raise IntegrationContractError("integration_invalid_event") from None


def recorded_person_observation(site_id: str, event: dict) -> ObservationEnvelope:
    """Use committed person-event identity and recorded frame UTC, never inference UTC."""
    try:
        camera = str(UUID(event["camera_id"]))
        identifier = event["id"]
        count = event["person_count"]
        if (
            type(identifier) is not int
            or identifier < 1
            or type(count) is not int
            or not 1 <= count <= 200
        ):
            raise ValueError
        digest = hashlib.sha256(f"{site_id}:{camera}:{identifier}".encode()).hexdigest()
        return ObservationEnvelope(
            observation_id=digest,
            source_id=camera,
            observed_at=_time(event["occurred_at"]),
            kind="person_detected",
            confidence=_confidence(event["confidence"]),
            attributes={"person_count": count},
            provenance=("k5-recorded-person",),
            evidence_ref="k5-recording:" + camera + ":" + event["occurred_at"],
        )
    except IntegrationContractError:
        raise
    except (ValueError, TypeError, KeyError):
        raise IntegrationContractError("integration_invalid_event") from None


def negotiate_k5_lab(versions: Any) -> str:
    """Negotiate the highest exact supported public K5-Lab adapter version (v2)."""
    if (
        type(versions) not in (list, tuple)
        or not 1 <= len(versions) <= 8
        or any(type(v) is not str for v in versions)
        or len(set(versions)) != len(versions)
    ):
        raise IntegrationContractError("integration_incompatible")
    for version in versions:
        _lab_token(version)
    if "2" not in versions:
        raise IntegrationContractError("integration_incompatible")
    return "2"


def k5_lab_request(observation_id: str, partition: str, *, timeout_ms: int = 3000) -> dict:
    """Encode exactly the published opaque-reference v2 request fields."""
    if type(timeout_ms) is not int or not 1 <= timeout_ms <= 5000:
        raise IntegrationContractError("integration_invalid_request")
    observation_id, partition = _lab_token(observation_id), _lab_token(partition)
    request_id = hashlib.sha256((partition + ":" + observation_id).encode()).hexdigest()
    return {
        "contract_version": "2",
        "request_id": request_id,
        "partition": partition,
        "record_id": observation_id,
        "timeout_ms": timeout_ms,
    }


def k5_lab_response(request: dict, value: dict) -> dict:
    """Validate exact v2 correlation and permanent non-authorization."""
    response = bounded_document(value)
    expected = {
        "contract_version",
        "request_id",
        "partition",
        "record_id",
        "status",
        "reason_code",
        "operation",
        "confidence",
        "authorized",
    }
    if response.keys() != expected or response["authorized"] is not False:
        raise IntegrationContractError("integration_invalid_response")
    if any(
        response[key] != request[key]
        for key in ("contract_version", "request_id", "partition", "record_id")
    ):
        raise IntegrationContractError("integration_correlation_mismatch")
    _lab_token(response["reason_code"])
    if response["status"] not in {
        "abstain",
        "error",
        "incompatible",
        "recommendation",
        "unavailable",
    }:
        raise IntegrationContractError("integration_invalid_response")
    if response["status"] == "recommendation":
        _lab_token(response["operation"])
        _confidence(response["confidence"])
    elif response["operation"] is not None or response["confidence"] is not None:
        raise IntegrationContractError("integration_invalid_response")
    return response
