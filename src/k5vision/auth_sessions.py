"""Short-lived human session boundary for the K5 control plane."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from k5vision.domain.users import UserAccount, Username

DEFAULT_SESSION_TTL_SECONDS = 8 * 60 * 60
MIN_SESSION_TTL_SECONDS = 5 * 60
MAX_SESSION_TTL_SECONDS = 24 * 60 * 60

SessionToken = Annotated[
    str,
    StringConstraints(
        min_length=32,
        max_length=128,
        pattern=r"^[A-Za-z0-9_-]{32,128}$",
    ),
]


class UserLogin(BaseModel):
    """Password-login request for an initialized K5 user account."""

    model_config = ConfigDict(extra="forbid")

    username: Username
    password: str = Field(min_length=1, max_length=256)


class UserSessionIssue(BaseModel):
    """Opaque short-lived session issued after successful password verification."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    account: UserAccount
    session_token: SessionToken
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class _SessionRecord:
    account: UserAccount
    expires_at: datetime


class UserSessionManager:
    """Maintain one bounded opaque in-process session per durable user."""

    def __init__(self, *, ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS) -> None:
        if not MIN_SESSION_TTL_SECONDS <= ttl_seconds <= MAX_SESSION_TTL_SECONDS:
            raise ValueError(
                f"ttl_seconds must be between {MIN_SESSION_TTL_SECONDS} "
                f"and {MAX_SESSION_TTL_SECONDS}"
            )
        self._ttl_seconds = ttl_seconds
        self._sessions: dict[bytes, _SessionRecord] = {}
        self._user_tokens: dict[UUID, bytes] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _digest(token: str) -> bytes:
        return hashlib.sha256(token.encode("ascii")).digest()

    @staticmethod
    def _token_is_valid(token: str) -> bool:
        return 32 <= len(token) <= 128 and token.isascii() and all(
            character.isalnum() or character in "_-" for character in token
        )

    async def issue(self, account: UserAccount) -> tuple[str, datetime]:
        """Issue a new session and revoke any prior session for the same user."""
        token = secrets.token_urlsafe(32)
        digest = self._digest(token)
        expires_at = datetime.now(UTC) + timedelta(seconds=self._ttl_seconds)
        record = _SessionRecord(account=account, expires_at=expires_at)
        async with self._lock:
            previous = self._user_tokens.get(account.id)
            if previous is not None:
                self._sessions.pop(previous, None)
            self._sessions[digest] = record
            self._user_tokens[account.id] = digest
        return token, expires_at

    async def resolve(self, token: str) -> UserAccount | None:
        """Resolve one non-expired session without retaining its plaintext token."""
        if not self._token_is_valid(token):
            return None
        digest = self._digest(token)
        now = datetime.now(UTC)
        async with self._lock:
            record = self._sessions.get(digest)
            if record is None:
                return None
            if record.expires_at <= now:
                self._sessions.pop(digest, None)
                if self._user_tokens.get(record.account.id) == digest:
                    self._user_tokens.pop(record.account.id, None)
                return None
            return record.account

    async def revoke(self, token: str) -> bool:
        """Revoke one session token without exposing session state."""
        if not self._token_is_valid(token):
            return False
        digest = self._digest(token)
        async with self._lock:
            record = self._sessions.pop(digest, None)
            if record is None:
                return False
            if self._user_tokens.get(record.account.id) == digest:
                self._user_tokens.pop(record.account.id, None)
            return True

    async def revoke_user(self, user_id: UUID) -> None:
        """Revoke the active session for a user after an authority-bearing change."""
        async with self._lock:
            digest = self._user_tokens.pop(user_id, None)
            if digest is not None:
                self._sessions.pop(digest, None)
