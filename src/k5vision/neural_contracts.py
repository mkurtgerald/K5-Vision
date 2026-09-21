"""Versioned, transport-neutral contracts for the K5 Vision neural boundary.

These models define the seam between production K5 Vision and reasoning,
learning, inference, correlation, simulation, LLM, and Virtual Guard subsystems.
They do not themselves grant device authority; execution remains owned by the
K5 policy/authority layer.
"""

from enum import StrEnum
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


ContractVersion = Literal["1.0"]


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


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: ContractVersion = "1.0"


class ObservationEnvelope(ContractModel):
    observation_id: str = Field(min_length=1, max_length=128)
    source_id: str = Field(min_length=1, max_length=128)
    observed_at: AwareDatetime
    kind: str = Field(min_length=1, max_length=128)
    attributes: dict[str, Any] = Field(default_factory=dict)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    provenance: tuple[str, ...] = ()
    evidence_ref: str | None = None


class ActionProposal(ContractModel):
    proposal_id: str = Field(min_length=1, max_length=128)
    created_at: AwareDatetime
    expires_at: AwareDatetime
    action_type: str = Field(min_length=1, max_length=128)
    target_ref: str = Field(min_length=1, max_length=256)
    rationale: str = Field(min_length=1, max_length=4096)
    confidence: float = Field(ge=0.0, le=1.0)
    requested_authority: AuthorityLevel = AuthorityLevel.RECOMMEND
    correlation_id: str | None = None
    producer_version: str = Field(min_length=1, max_length=128)
    evidence_refs: tuple[str, ...] = ()
    constraints: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_window(self) -> "ActionProposal":
        if self.expires_at <= self.created_at:
            raise ValueError("proposal expiration must be after creation")
        return self


class AuthorityDecision(ContractModel):
    proposal_id: str = Field(min_length=1, max_length=128)
    decided_at: AwareDatetime
    decision: Literal["allow", "deny", "modify"]
    policy_version: str = Field(min_length=1, max_length=128)
    decided_by: str = Field(min_length=1, max_length=256)
    autonomy_mode: AutonomyMode = AutonomyMode.MANUAL
    modifications: dict[str, Any] = Field(default_factory=dict)


class ExecutionGrant(ContractModel):
    grant_id: str = Field(min_length=1, max_length=128)
    proposal_id: str = Field(min_length=1, max_length=128)
    issued_at: AwareDatetime
    expires_at: AwareDatetime
    action_type: str = Field(min_length=1, max_length=128)
    target_ref: str = Field(min_length=1, max_length=256)
    policy_version: str = Field(min_length=1, max_length=128)
    autonomy_mode: AutonomyMode
    issued_by: str = Field(min_length=1, max_length=256)
    single_use: Literal[True] = True

    @model_validator(mode="after")
    def validate_window(self) -> "ExecutionGrant":
        if self.expires_at <= self.issued_at:
            raise ValueError("execution grant expiration must be after issuance")
        return self


class ActionReceipt(ContractModel):
    receipt_id: str = Field(min_length=1, max_length=128)
    proposal_id: str = Field(min_length=1, max_length=128)
    completed_at: AwareDatetime
    decision: Literal["allow", "deny", "modify"]
    executed: bool
    result: str = Field(min_length=1, max_length=4096)
    policy_version: str = Field(min_length=1, max_length=128)
    authority_ref: str = Field(min_length=1, max_length=256)
    failure_reason: str | None = None


class CapabilityHandshake(ContractModel):
    generated_at: AwareDatetime
    producer_version: str = Field(min_length=1, max_length=128)
    contract_versions: tuple[str, ...] = ("1.0",)
    capabilities: frozenset[str] = frozenset()
    runtime_mode: RuntimeMode
