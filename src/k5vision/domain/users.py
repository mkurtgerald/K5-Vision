"""Canonical user-administration models for the K5 control plane."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Username = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=3,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$",
    ),
]
BootstrapCredential = Annotated[
    str,
    StringConstraints(
        min_length=32,
        max_length=128,
        pattern=r"^[A-Za-z0-9_-]{32,128}$",
    ),
]


class UserRole(StrEnum):
    """Initial bounded K5 application roles."""

    VIEWER = "viewer"
    OPERATOR = "operator"
    ADMINISTRATOR = "administrator"


class UserCreate(BaseModel):
    """Administrative request for a site-scoped account record."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    username: Username
    display_name: str = Field(min_length=1, max_length=128)
    role: UserRole = UserRole.OPERATOR
    enabled: bool = True


class UserPatch(BaseModel):
    """Small bounded administrative changes permitted in the initial surface."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    role: UserRole | None = None
    enabled: bool | None = None

    @model_validator(mode="after")
    def require_change(self) -> "UserPatch":
        if self.display_name is None and self.role is None and self.enabled is None:
            raise ValueError("at least one user field must be supplied")
        return self


class UserAccount(UserCreate):
    """Durable site-scoped user account metadata."""

    id: UUID = Field(default_factory=uuid4)


class UserBootstrapIssue(BaseModel):
    """One-time bootstrap credential returned only by the administrator creation path."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    account: UserAccount
    temporary_credential: BootstrapCredential
    expires_at: datetime


class UserBootstrapPasswordChange(BaseModel):
    """First-login password replacement request using a one-time bootstrap credential."""

    model_config = ConfigDict(extra="forbid")

    username: Username
    temporary_credential: BootstrapCredential
    new_password: str = Field(min_length=12, max_length=256)


class UserAuditEvent(BaseModel):
    """Credential-free durable record of an administrative account change."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: int = Field(ge=1)
    user_id: UUID
    action: str = Field(min_length=1, max_length=32)
    actor: str = Field(min_length=1, max_length=64)
    changed_fields: tuple[str, ...]
    occurred_at: str = Field(min_length=20, max_length=40)
