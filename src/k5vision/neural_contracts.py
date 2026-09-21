"""Versioned, transport-neutral contracts for the K5 Vision neural boundary.

These models define the seam between production K5 Vision and reasoning,
learning, inference, correlation, simulation, LLM, and Virtual Guard subsystems.
They do not themselves grant device authority; execution remains owned by the
K5 policy/authority layer.
"""

import hashlib
import json
import math
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

import pydantic

ContractVersion = Literal["1.0"]

MAX_EXTENSION_DEPTH = 6
MAX_EXTENSION_ITEMS = 256
MAX_EXTENSION_BYTES = 16_384
MAX_EXTENSION_STRING_BYTES = 4_096
MAX_EXTENSION_KEY_BYTES = 128


def _bounded_json_object(value: Any) -> dict[str, Any]:
    item_count = 0

    def copy_value(item: Any, *, depth: int) -> Any:
        nonlocal item_count
        item_count += 1
        if item_count > MAX_EXTENSION_ITEMS:
            raise ValueError("extension payload item limit exceeded")
        if depth > MAX_EXTENSION_DEPTH:
            raise ValueError("extension payload depth limit exceeded")

        if item is None or isinstance(item, (bool, int)):
            return item
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError("extension payload numbers must be finite")
            return item
        if isinstance(item, str):
            if len(item.encode("utf-8")) > MAX_EXTENSION_STRING_BYTES:
                raise ValueError("extension payload string limit exceeded")
            return item
        if isinstance(item, list):
            return [copy_value(member, depth=depth + 1) for member in item]
        if isinstance(item, dict):
            copied: dict[str, Any] = {}
            for key, member in item.items():
                if not isinstance(key, str):
                    raise ValueError("extension payload keys must be strings")
                if not key or len(key.encode("utf-8")) > MAX_EXTENSION_KEY_BYTES:
                    raise ValueError("extension payload key limit exceeded")
                copied[key] = copy_value(member, depth=depth + 1)
            return copied
        raise ValueError("extension payload must contain only JSON-safe values")

    copied = copy_value(value, depth=0)
    if not isinstance(copied, dict):
        raise ValueError("extension payload must be a JSON object")
    encoded = json.dumps(
        copied,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > MAX_EXTENSION_BYTES:
        raise ValueError("extension payload byte limit exceeded")
    return copied


def _canonical_model_bytes(model: pydantic.BaseModel) -> bytes:
    return json.dumps(
        model.model_dump(mode="json", exclude_none=False),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


class AuthorityLevel(StrEnum):
    OBSERVE = "observe"
    RECOMMEND = "recommend"
    CONFIRM = "confirm"
    BOUNDED = "bounded"
    HIGH = "high"
    EMERGENCY = "emergency"


class AutonomyMode(StrEnum):
    MANUAL = "manual"
    ASSISTED = "assisted"
    AUTO = "auto"
    EMERGENCY = "emergency"


class RuntimeMode(StrEnum):
    EMBEDDED = "embedded"
    LOCAL_SERVICE = "local-service"
    REMOTE_SERVICE = "remote-service"


class ExecutionOutcome(StrEnum):
    NOT_ATTEMPTED = "not-attempted"
    VERIFIED_SUCCESS = "verified-success"
    VERIFIED_FAILURE = "verified-failure"
    UNKNOWN = "unknown"
    DENIED_BUT_OBSERVED = "denied-but-observed"


class ContractModel(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid", frozen=True)

    schema_version: ContractVersion = "1.0"

    @pydantic.field_validator("*", mode="after")
    @classmethod
    def reject_naive_datetimes(cls, value: Any) -> Any:
        if isinstance(value, datetime) and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("timezone-aware timestamp required")
        return value


class ObservationEnvelope(ContractModel):
    observation_id: str = pydantic.Field(min_length=1, max_length=128)
    source_id: str = pydantic.Field(min_length=1, max_length=128)
    observed_at: datetime
    kind: str = pydantic.Field(min_length=1, max_length=128)
    attributes: dict[str, Any] = pydantic.Field(default_factory=dict)
    confidence: float | None = pydantic.Field(default=None, ge=0.0, le=1.0)
    provenance: tuple[str, ...] = ()
    evidence_ref: str | None = None

    @pydantic.field_validator("attributes", mode="before")
    @classmethod
    def bound_attributes(cls, value: Any) -> dict[str, Any]:
        return _bounded_json_object(value)


class ActionProposal(ContractModel):
    proposal_id: str = pydantic.Field(min_length=1, max_length=128)
    created_at: datetime
    expires_at: datetime
    action_type: str = pydantic.Field(min_length=1, max_length=128)
    target_ref: str = pydantic.Field(min_length=1, max_length=256)
    rationale: str = pydantic.Field(min_length=1, max_length=4096)
    confidence: float = pydantic.Field(ge=0.0, le=1.0)
    requested_authority: AuthorityLevel = AuthorityLevel.RECOMMEND
    correlation_id: str | None = None
    producer_version: str = pydantic.Field(min_length=1, max_length=128)
    evidence_refs: tuple[str, ...] = ()
    constraints: dict[str, Any] = pydantic.Field(default_factory=dict)

    @pydantic.field_validator("constraints", mode="before")
    @classmethod
    def bound_constraints(cls, value: Any) -> dict[str, Any]:
        return _bounded_json_object(value)

    @pydantic.model_validator(mode="after")
    def validate_window(self) -> "ActionProposal":
        if self.expires_at <= self.created_at:
            raise ValueError("proposal expiration must be after creation")
        return self

    def canonical_digest(self) -> str:
        """Return the v1 canonical SHA-256 binding for this exact proposal."""

        return hashlib.sha256(_canonical_model_bytes(self)).hexdigest()


class AuthorityDecision(ContractModel):
    proposal_id: str = pydantic.Field(min_length=1, max_length=128)
    decided_at: datetime
    decision: Literal["allow", "deny", "modify"]
    policy_version: str = pydantic.Field(min_length=1, max_length=128)
    decided_by: str = pydantic.Field(min_length=1, max_length=256)
    autonomy_mode: AutonomyMode = AutonomyMode.MANUAL
    modifications: dict[str, Any] = pydantic.Field(default_factory=dict)

    @pydantic.field_validator("modifications", mode="before")
    @classmethod
    def bound_modifications(cls, value: Any) -> dict[str, Any]:
        return _bounded_json_object(value)


class ExecutionGrant(ContractModel):
    grant_id: str = pydantic.Field(min_length=1, max_length=128)
    proposal_id: str = pydantic.Field(min_length=1, max_length=128)
    proposal_digest: str = pydantic.Field(pattern=r"^[0-9a-f]{64}$")
    issued_at: datetime
    expires_at: datetime
    action_type: str = pydantic.Field(min_length=1, max_length=128)
    target_ref: str = pydantic.Field(min_length=1, max_length=256)
    policy_version: str = pydantic.Field(min_length=1, max_length=128)
    autonomy_mode: AutonomyMode
    issued_by: str = pydantic.Field(min_length=1, max_length=256)
    single_use: Literal[True] = True

    @pydantic.model_validator(mode="after")
    def validate_window(self) -> "ExecutionGrant":
        if self.expires_at <= self.issued_at:
            raise ValueError("execution grant expiration must be after issuance")
        return self

    def binds_proposal(self, proposal: ActionProposal) -> bool:
        return (
            self.proposal_id == proposal.proposal_id
            and self.action_type == proposal.action_type
            and self.target_ref == proposal.target_ref
            and self.proposal_digest == proposal.canonical_digest()
        )


class ActionReceipt(ContractModel):
    receipt_id: str = pydantic.Field(min_length=1, max_length=128)
    proposal_id: str = pydantic.Field(min_length=1, max_length=128)
    completed_at: datetime
    decision: Literal["allow", "deny", "modify"]
    outcome: ExecutionOutcome
    result: str = pydantic.Field(min_length=1, max_length=4096)
    policy_version: str = pydantic.Field(min_length=1, max_length=128)
    authority_ref: str = pydantic.Field(min_length=1, max_length=256)
    failure_reason: str | None = pydantic.Field(default=None, max_length=4096)

    @pydantic.model_validator(mode="after")
    def validate_outcome(self) -> "ActionReceipt":
        if self.decision == "deny":
            if self.outcome not in {
                ExecutionOutcome.NOT_ATTEMPTED,
                ExecutionOutcome.DENIED_BUT_OBSERVED,
            }:
                raise ValueError("denied decision cannot report authorized execution outcome")
        elif self.outcome is ExecutionOutcome.DENIED_BUT_OBSERVED:
            raise ValueError("denied-but-observed outcome requires a denied decision")

        if self.outcome in {
            ExecutionOutcome.VERIFIED_FAILURE,
            ExecutionOutcome.UNKNOWN,
            ExecutionOutcome.DENIED_BUT_OBSERVED,
        } and not self.failure_reason:
            raise ValueError("non-success execution outcome requires failure_reason")

        if (
            self.outcome is ExecutionOutcome.NOT_ATTEMPTED
            and self.decision != "deny"
            and not self.failure_reason
        ):
            raise ValueError("authorized action not attempted requires failure_reason")

        if (
            self.outcome is ExecutionOutcome.VERIFIED_SUCCESS
            and self.failure_reason is not None
        ):
            raise ValueError("verified success cannot include failure_reason")

        return self


class CapabilityHandshake(ContractModel):
    generated_at: datetime
    producer_version: str = pydantic.Field(min_length=1, max_length=128)
    contract_versions: tuple[str, ...] = ("1.0",)
    capabilities: frozenset[str] = frozenset()
    runtime_mode: RuntimeMode
