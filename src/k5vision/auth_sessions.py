"""Short-lived human session boundary for the K5 control plane."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from collections.abc import Callable
from concurrent.futures import Executor
from contextvars import copy_context
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import monotonic
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
    password: str = Field(min_length=1, max_length=256, repr=False)


class UserSessionIssue(BaseModel):
    """Opaque short-lived session issued after successful password verification."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    account: UserAccount
    session_token: SessionToken = Field(repr=False)
    expires_at: datetime
    expires_in_seconds: int = Field(
        ge=MIN_SESSION_TTL_SECONDS, le=MAX_SESSION_TTL_SECONDS, strict=True
    )


@dataclass(frozen=True, slots=True)
class _SessionRecord:
    account: UserAccount
    expires_at: datetime
    deadline: float
    credential_version: bytes | None = None


class UserSessionManager:
    """Maintain one bounded opaque in-process session per durable user."""

    def __init__(
        self,
        *,
        ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
        resolve_account: Callable[[UserAccount, bytes | None], UserAccount | None] | None = None,
    ) -> None:
        if type(ttl_seconds) is not int or not (
            MIN_SESSION_TTL_SECONDS <= ttl_seconds <= MAX_SESSION_TTL_SECONDS
        ):
            raise ValueError(
                f"ttl_seconds must be between {MIN_SESSION_TTL_SECONDS} "
                f"and {MAX_SESSION_TTL_SECONDS}"
            )
        self._ttl_seconds = ttl_seconds
        self._resolve_account = resolve_account
        self._sessions: dict[bytes, _SessionRecord] = {}
        self._user_tokens: dict[UUID, bytes] = {}
        self._lock = asyncio.Lock()
        self._resolve_slots = asyncio.Semaphore(4)
        self._revocation_listeners: set[Callable[[UUID], None]] = set()

    def add_revocation_listener(self, callback: Callable[[UUID], None]) -> Callable[[], None]:
        """Register a bounded synchronous pending-auth invalidation callback.

        Listeners run after the session lock is released. They must perform
        only bounded in-memory work and must not recursively acquire a session
        lock or perform I/O. Ordinary session issue/replacement does not notify.
        """
        if not callable(callback):
            raise ValueError("Session revocation listener must be callable")
        self._revocation_listeners.add(callback)

        def remove():
            self._revocation_listeners.discard(callback)

        return remove

    def _notify_revocation(self, user_id: UUID) -> None:
        for listener in tuple(self._revocation_listeners):
            listener(user_id)

    @property
    def ttl_seconds(self) -> int:
        """Return the bounded elapsed lifetime advertised to a new login."""
        return self._ttl_seconds

    @staticmethod
    def _digest(token: str) -> bytes:
        return hashlib.sha256(token.encode("ascii")).digest()

    @staticmethod
    def _token_is_valid(token: str) -> bool:
        return (
            32 <= len(token) <= 128
            and token.isascii()
            and all(character.isalnum() or character in "_-" for character in token)
        )

    async def issue(
        self, account: UserAccount, *, credential_version: bytes | None = None
    ) -> tuple[str, datetime]:
        """Issue a new session and revoke any prior session for the same user."""
        if credential_version is not None and (
            type(credential_version) is not bytes or len(credential_version) != 32
        ):
            raise ValueError("credential version must be an opaque SHA-256 identity")
        token = secrets.token_urlsafe(32)
        digest = self._digest(token)
        async with self._lock:
            expires_at = datetime.now(UTC) + timedelta(seconds=self._ttl_seconds)
            record = _SessionRecord(
                account=account,
                expires_at=expires_at,
                deadline=monotonic() + self._ttl_seconds,
                credential_version=credential_version,
            )
            previous = self._user_tokens.get(account.id)
            if previous is not None:
                self._sessions.pop(previous, None)
            self._sessions[digest] = record
            self._user_tokens[account.id] = digest
        return token, expires_at

    async def resolve(self, token: str, *, executor: Executor | None = None) -> UserAccount | None:
        """Resolve current authority without blocking the HTTP event loop."""
        if not self._token_is_valid(token):
            return None
        digest = self._digest(token)
        async with self._lock:
            record = self._sessions.get(digest)
            if record is None:
                return None
            if record.deadline <= monotonic():
                self._sessions.pop(digest, None)
                if self._user_tokens.get(record.account.id) == digest:
                    self._user_tokens.pop(record.account.id, None)
                return None
            if self._resolve_account is None:
                return record.account

        # The durable registry read is synchronous SQLite I/O. Never run it
        # inside the event loop or hold the session lock while it is in flight.
        # The bounded slots prevent simultaneous reads from flooding a private
        # SQLite registry when many authenticated browser requests arrive.
        async with self._resolve_slots:
            if executor is None:
                worker = asyncio.create_task(
                    asyncio.to_thread(
                        self._resolve_account, record.account, record.credential_version
                    )
                )
            else:
                worker = asyncio.get_running_loop().run_in_executor(
                    executor,
                    copy_context().run,
                    self._resolve_account,
                    record.account,
                    record.credential_version,
                )
            try:
                current = await asyncio.shield(worker)
            except asyncio.CancelledError:
                # The read cannot be canceled once started in a thread. Keep
                # its slot until completion, rather than leaking unbounded
                # registry work through canceled client requests.
                try:
                    await worker
                except Exception:
                    pass
                raise

        async with self._lock:
            # Revocation, password replacement, or another login may have
            # superseded this session during the authority read.
            if self._sessions.get(digest) is not record:
                return None
            if record.deadline <= monotonic() or current is None:
                self._sessions.pop(digest, None)
                if self._user_tokens.get(record.account.id) == digest:
                    self._user_tokens.pop(record.account.id, None)
                return None
            return current

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
        self._notify_revocation(record.account.id)
        return True

    async def revoke_user(self, user_id: UUID) -> None:
        """Revoke the active session for a user after an authority-bearing change."""
        async with self._lock:
            digest = self._user_tokens.pop(user_id, None)
            if digest is not None:
                self._sessions.pop(digest, None)
        # Authority changes must also invalidate pending authentication when
        # no full session exists yet (for example SSO followed by local MFA).
        self._notify_revocation(user_id)
