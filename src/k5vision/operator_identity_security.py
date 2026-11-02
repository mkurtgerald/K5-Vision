"""Private account second factors and bounded pre-session authentication.

TOTP follows RFC 6238's SHA-1/30-second/six-digit profile. Windows secrets are
protected with current-user DPAPI and site/user entropy. Other platforms rely
on the same strictly private profile used for the local password registry.
Second-factor challenges never constitute sessions or media credentials.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import struct
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlencode
from uuid import UUID

from k5vision import identity_state
from k5vision.domain.users import UserAccount
from k5vision.operator_component_state import ComponentStateAnchor
from k5vision.services.user_registry import UserRegistryStorageError

_LIFETIME = 300
_CAPACITY = 1024
_DATABASE = "operator-account-security.sqlite3"


class AccountSecurityError(RuntimeError):
    """Safe failure without credentials or platform exception detail."""


class AccountSecurityUnavailable(AccountSecurityError):
    """Private state or platform protection could not be admitted."""


class SecondFactorRequired(AccountSecurityError):
    def __init__(self, challenge: dict) -> None:
        super().__init__("A second factor is required")
        self.challenge = challenge


class ProfileSecretProtector:
    """Use user-scoped, non-interactive Windows DPAPI for installed secrets."""

    def __init__(self, site_id: str) -> None:
        self._site = site_id

    def _dpapi(self, payload: bytes, purpose: str, *, decrypt: bool) -> bytes:
        from ctypes import wintypes

        class Blob(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

        source_buffer = ctypes.create_string_buffer(payload)
        entropy_buffer = ctypes.create_string_buffer(
            hashlib.sha256((self._site + ":" + purpose).encode()).digest()
        )
        source = Blob(len(payload), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_ubyte)))
        entropy = Blob(32, ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_ubyte)))
        result = Blob()
        crypt = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.LocalFree.argtypes = [wintypes.HLOCAL]
        kernel.LocalFree.restype = wintypes.HLOCAL
        arguments = [
            ctypes.POINTER(Blob),
            ctypes.c_void_p,
            ctypes.POINTER(Blob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(Blob),
        ]
        function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
        function.argtypes = arguments
        function.restype = wintypes.BOOL
        if not function(source, None, entropy, None, None, 1, result):
            raise AccountSecurityUnavailable("Account secret protection unavailable")
        try:
            if not (0 if decrypt else 1) <= result.size <= 65536:
                raise AccountSecurityUnavailable("Account secret protection unavailable")
            return ctypes.string_at(result.data, result.size)
        finally:
            kernel.LocalFree(result.data)

    def protect(self, payload: bytes, purpose: str) -> bytes:
        if os.name == "nt":
            return b"dpapi-v1:" + self._dpapi(payload, purpose, decrypt=False)
        return b"private-profile-v1:" + payload

    def unprotect(self, payload: bytes, purpose: str) -> bytes:
        if os.name == "nt" and payload.startswith(b"dpapi-v1:"):
            return self._dpapi(payload[9:], purpose, decrypt=True)
        if os.name != "nt" and payload.startswith(b"private-profile-v1:"):
            return payload[19:]
        raise AccountSecurityUnavailable("Account secret protection unavailable")


def totp(secret: bytes, counter: int) -> str:
    """Return the standard RFC 4226 truncation used by RFC 6238."""
    digest = hmac.digest(secret, struct.pack(">Q", counter), "sha1")
    offset = digest[-1] & 15
    return f"{(struct.unpack('>I', digest[offset : offset + 4])[0] & 0x7FFFFFFF) % 1000000:06d}"


def _recovery_digest(value: str) -> bytes | None:
    normalized = value.replace("-", "").replace(" ", "").lower()
    if not re.fullmatch(r"[a-f0-9]{20}", normalized):
        return None
    return hashlib.sha256(normalized.encode("ascii")).digest()


def _recovery_codes() -> tuple[list[str], bytes]:
    raw = [secrets.token_hex(10) for _ in range(10)]
    shown = ["-".join(code[i : i + 4] for i in range(0, 20, 4)) for code in raw]
    return shown, b"".join(hashlib.sha256(code.encode("ascii")).digest() for code in raw)


class AccountSecurityStore:
    """Site-bound durable factors, replay counters and single-use recovery codes."""

    def __init__(self, root: Path, site_id: str, *, protector=None, utc=time.time) -> None:
        self._connection = None
        self._lock = threading.RLock()
        self._utc = utc
        self._protector = protector or ProfileSecretProtector(site_id)
        try:
            identity = identity_state.load_identity_state(root)
            if identity.site_id != site_id:
                raise ValueError
            self._root = identity.directory
            self._root_identity = self._root.lstat()
            self._path = self._root / _DATABASE
            anchor = ComponentStateAnchor(
                self._root,
                site_id,
                "account_security",
                present=self._path.exists() or self._path.is_symlink(),
            )
            created = False
            try:
                descriptor = os.open(
                    self._path,
                    os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
            except FileExistsError:
                pass
            else:
                os.close(descriptor)
                created = True
            self._identity = self._private_file(self._path)
            # SQLite can inspect/recover journal sidecars during connect.
            # Admit those leaves before the first database operation too.
            self._admit()
            self._connection = sqlite3.connect(
                self._path.as_uri() + "?mode=rw",
                uri=True,
                isolation_level=None,
                check_same_thread=False,
                timeout=3,
            )
            self._connection.execute("PRAGMA synchronous=FULL")
            if created:
                self._connection.executescript(
                    "BEGIN IMMEDIATE;"
                    "CREATE TABLE security_state (schema_version INTEGER,site_id TEXT);"
                    "CREATE TABLE mfa (user_id TEXT PRIMARY KEY,generation TEXT NOT NULL,"
                    "secret BLOB NOT NULL,last_counter INTEGER NOT NULL,recovery BLOB NOT NULL);"
                )
                self._connection.execute("INSERT INTO security_state VALUES(1,?)", (site_id,))
                self._connection.execute("COMMIT")
            if self._connection.execute("SELECT * FROM security_state").fetchall() != [
                (1, site_id)
            ]:
                raise ValueError
            for row in self._connection.execute("SELECT * FROM mfa"):
                UUID(row[0])
                if not re.fullmatch("[a-f0-9]{32}", row[1]) or not -1 <= row[3] <= 2**63 - 1:
                    raise ValueError
                if type(row[4]) is not bytes or len(row[4]) % 32 or len(row[4]) > 320:
                    raise ValueError
                if len(self._protector.unprotect(row[2], row[0])) != 20:
                    raise ValueError
            self._admit()
            anchor.seal()
        except Exception:
            self.close()
            raise AccountSecurityUnavailable("Account security state unavailable") from None

    @staticmethod
    def _private_file(path):
        info = identity_state._regular_file(path)
        if info.st_size > 128 * 1024 * 1024:
            raise ValueError
        if os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
            raise ValueError
        return info

    def _admit(self):
        identity_state._same_directory(self._root, self._root_identity)
        current = self._private_file(self._path)
        if (current.st_dev, current.st_ino) != (self._identity.st_dev, self._identity.st_ino):
            raise ValueError
        for suffix in ("-journal", "-wal", "-shm"):
            path = Path(str(self._path) + suffix)
            if path.exists() or path.is_symlink():
                self._private_file(path)

    def _execute(self, operation):
        with self._lock:
            try:
                self._admit()
                if self._connection is None:
                    raise ValueError
                value = operation(self._connection)
                self._admit()
                return value
            except Exception:
                if self._connection is not None and self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise AccountSecurityUnavailable("Account security state unavailable") from None

    def status(self, user_id: UUID) -> dict:
        def read(connection):
            row = connection.execute(
                "SELECT generation,length(recovery) FROM mfa WHERE user_id=?", (str(user_id),)
            ).fetchone()
            return {
                "mfa_enabled": row is not None,
                "recovery_codes_remaining": 0 if row is None else row[1] // 32,
                "generation": None if row is None else row[0],
            }

        return self._execute(read)

    def enable(self, user_id: UUID, secret: bytes, confirmed_counter: int) -> list[str]:
        if (
            type(secret) is not bytes
            or len(secret) != 20
            or type(confirmed_counter) is not int
            or not 0 <= confirmed_counter <= 2**63 - 1
        ):
            raise ValueError("Account factor was refused")
        codes, hashed = _recovery_codes()
        protected = self._protector.protect(secret, str(user_id))

        def save(connection):
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM mfa WHERE user_id=?", (str(user_id),)).fetchone():
                raise ValueError
            connection.execute(
                "INSERT INTO mfa VALUES(?,?,?,?,?)",
                (str(user_id), secrets.token_hex(16), protected, confirmed_counter, hashed),
            )
            connection.execute("COMMIT")

        self._execute(save)
        return codes

    def consume(self, user_id: UUID, code: str, *, generation: str | None = None) -> bool:
        if type(code) is not str or not 1 <= len(code) <= 64:
            return False

        def verify(connection):
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT generation,secret,last_counter,recovery FROM mfa WHERE user_id=?",
                (str(user_id),),
            ).fetchone()
            if row is None or (generation is not None and row[0] != generation):
                connection.execute("ROLLBACK")
                return False
            recovery = _recovery_digest(code)
            if recovery is not None:
                hashes = [row[3][i : i + 32] for i in range(0, len(row[3]), 32)]
                match = next(
                    (i for i, value in enumerate(hashes) if hmac.compare_digest(value, recovery)),
                    None,
                )
                if match is None:
                    connection.execute("ROLLBACK")
                    return False
                hashes.pop(match)
                connection.execute(
                    "UPDATE mfa SET recovery=? WHERE user_id=?", (b"".join(hashes), str(user_id))
                )
            elif re.fullmatch("[0-9]{6}", code):
                secret = self._protector.unprotect(row[1], str(user_id))
                if len(secret) != 20:
                    raise ValueError
                counter = int(self._utc() // 30)
                match = next(
                    (
                        value
                        for value in range(max(0, counter - 1), counter + 2)
                        if value > row[2] and hmac.compare_digest(totp(secret, value), code)
                    ),
                    None,
                )
                if match is None:
                    connection.execute("ROLLBACK")
                    return False
                connection.execute(
                    "UPDATE mfa SET last_counter=? WHERE user_id=?", (match, str(user_id))
                )
            else:
                connection.execute("ROLLBACK")
                return False
            connection.execute("COMMIT")
            return True

        return self._execute(verify)

    def disable(self, user_id: UUID) -> None:
        self._execute(
            lambda connection: connection.execute(
                "DELETE FROM mfa WHERE user_id=?", (str(user_id),)
            )
        )

    def replace_recovery(self, user_id: UUID) -> list[str]:
        codes, hashes = _recovery_codes()

        def save(connection):
            cursor = connection.execute(
                "UPDATE mfa SET recovery=? WHERE user_id=?", (hashes, str(user_id))
            )
            if cursor.rowcount != 1:
                raise ValueError

        self._execute(save)
        return codes

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None


@dataclass(frozen=True)
class _Pending:
    account: UserAccount
    credential_version: bytes
    deadline: float
    generation: str | None = None
    secret: bytes | None = None
    epoch: int = 0
    authority_check: Callable[[], bool] | None = None


class AccountSecurityService:
    """Primary-password snapshots and MFA gate before session issuance."""

    def __init__(
        self, *, store: AccountSecurityStore, users, elapsed=time.monotonic, utc=time.time
    ):
        self.store, self.users = store, users
        self._elapsed, self._utc = elapsed, utc
        self._lock = threading.RLock()
        self._challenges: dict[bytes, _Pending] = {}
        self._enrollments: dict[bytes, _Pending] = {}
        self._attempts: dict[UUID, deque] = {}
        self._epochs: dict[UUID, int] = {}
        self._revision = 0
        self._remove_listener = None

    def bind_sessions(self, sessions):
        with self._lock:
            if self._remove_listener is not None:
                raise ValueError("Account security sessions are already bound")
            self._remove_listener = sessions.add_revocation_listener(self.invalidate_user)

    def invalidate_user(self, user_id: UUID) -> None:
        """Bounded synchronous revocation hook; no database or session I/O."""
        with self._lock:
            self._revision += 1
            self._epochs[user_id] = self._revision
            for values in (self._challenges, self._enrollments):
                for key in tuple(values):
                    if values[key].account.id == user_id:
                        del values[key]

    def primary_started(self) -> int:
        """Capture before an unknown username or external subject is resolved."""
        with self._lock:
            return self._revision

    def primary_current(self, account, version, started_epoch: int):
        with self._lock:
            epoch = self._epochs.get(account.id, 0)
            if epoch > started_epoch:
                raise AccountSecurityError("Account authentication changed")
            return self._current(_Pending(account, version, self._elapsed(), epoch=epoch))

    def validate_primary_proof(self, account, version, epoch, authority_check=None):
        with self._lock:
            return self._current(
                _Pending(
                    account, version, self._elapsed(), epoch=epoch, authority_check=authority_check
                )
            )

    def _current(self, pending: _Pending):
        if pending.epoch != self._epochs.get(pending.account.id, 0):
            raise AccountSecurityError("Account authentication changed")
        if pending.authority_check is not None and not pending.authority_check():
            raise AccountSecurityError("Account authentication changed")
        try:
            current = self.users.resolve_session(pending.account, pending.credential_version)
        except UserRegistryStorageError:
            raise AccountSecurityUnavailable("Account security state unavailable") from None
        if current is None:
            raise AccountSecurityError("Account authentication changed")
        return current

    def _password(self, account, password):
        epoch = self._epochs.get(account.id, 0)
        try:
            verified = self.users.verify_password_snapshot(
                username=account.username, password=password
            )
        except UserRegistryStorageError:
            raise AccountSecurityUnavailable("Account security state unavailable") from None
        if verified is None or verified[0].id != account.id:
            raise AccountSecurityError("Account authentication was not accepted")
        pending = _Pending(*verified, self._elapsed() + _LIFETIME, epoch=epoch)
        self._current(pending)
        return pending

    def _prune(self):
        now = self._elapsed()
        for values in (self._challenges, self._enrollments):
            for key in tuple(values):
                if values[key].deadline <= now:
                    del values[key]
        for key in tuple(self._attempts):
            if not self._attempts[key] or self._attempts[key][-1] <= now - _LIFETIME:
                del self._attempts[key]

    def _budget(self, user_id: UUID):
        self._prune()
        if user_id not in self._attempts and len(self._attempts) >= _CAPACITY:
            raise AccountSecurityError("Account authentication temporarily limited")
        queue = self._attempts.setdefault(user_id, deque())
        now = self._elapsed()
        while queue and queue[0] <= now - _LIFETIME:
            queue.popleft()
        if len(queue) >= 10:
            raise AccountSecurityError("Account authentication temporarily limited")
        queue.append(now)

    def password_authenticated(
        self,
        account: UserAccount,
        credential_version: bytes,
        *,
        authority_check: Callable[[], bool] | None = None,
        started_epoch: int | None = None,
    ) -> int:
        """Called before *any* password-authenticated session is issued."""
        with self._lock:
            if started_epoch is not None:
                self.primary_current(account, credential_version, started_epoch)
            pending = _Pending(
                account,
                credential_version,
                self._elapsed() + _LIFETIME,
                epoch=self._epochs.get(account.id, 0),
                authority_check=authority_check,
            )
            self._current(pending)
            status = self.store.status(account.id)
            if not status["mfa_enabled"]:
                return pending.epoch
            self._prune()
            for key in tuple(self._challenges):
                if self._challenges[key].account.id == account.id:
                    del self._challenges[key]
            if len(self._challenges) >= _CAPACITY:
                raise AccountSecurityError("Account authentication temporarily limited")
            token = secrets.token_urlsafe(32)
            self._challenges[hashlib.sha256(token.encode()).digest()] = _Pending(
                account,
                credential_version,
                pending.deadline,
                status["generation"],
                epoch=pending.epoch,
                authority_check=authority_check,
            )
            raise SecondFactorRequired(
                {"mfa_required": True, "challenge_token": token, "expires_in_seconds": _LIFETIME}
            )

    def complete_challenge(self, token: str, code: str):
        with self._lock:
            self._prune()
            key = hashlib.sha256(token.encode()).digest()
            pending = self._challenges.get(key)
            if pending is None:
                raise AccountSecurityError("Account authentication expired")
            self._budget(pending.account.id)
            current = self._current(pending)
            if not self.store.consume(current.id, code, generation=pending.generation):
                raise AccountSecurityError("Second factor was not accepted")
            self._challenges.pop(key, None)
            return (
                self._current(pending),
                pending.credential_version,
                pending.generation,
                pending.epoch,
                pending.authority_check,
            )

    def validate_issued_factor(self, account, version, generation, epoch, authority_check):
        with self._lock:
            current = self._current(
                _Pending(
                    account,
                    version,
                    self._elapsed(),
                    generation,
                    epoch=epoch,
                    authority_check=authority_check,
                )
            )
            if self.store.status(account.id)["generation"] != generation:
                raise AccountSecurityError("Account authentication changed")
            return current

    def begin_enrollment(self, account: UserAccount, password: str) -> dict:
        with self._lock:
            self._budget(account.id)
            pending = self._password(account, password)
            if self.store.status(account.id)["mfa_enabled"]:
                raise AccountSecurityError("A second factor is already enabled")
            if len(self._enrollments) >= _CAPACITY:
                raise AccountSecurityError("Account authentication temporarily limited")
            secret = secrets.token_bytes(20)
            token = secrets.token_urlsafe(32)
            for key in tuple(self._enrollments):
                if self._enrollments[key].account.id == account.id:
                    del self._enrollments[key]
            self._enrollments[hashlib.sha256(token.encode()).digest()] = _Pending(
                pending.account,
                pending.credential_version,
                pending.deadline,
                secret=secret,
                epoch=pending.epoch,
            )
            encoded = base64.b32encode(secret).decode("ascii").rstrip("=")
            return {
                "enrollment_token": token,
                "secret": encoded,
                "otpauth_uri": "otpauth://totp/"
                + quote("K5 Vision:" + account.username, safe="")
                + "?"
                + urlencode(
                    {
                        "secret": encoded,
                        "issuer": "K5 Vision",
                        "algorithm": "SHA1",
                        "digits": 6,
                        "period": 30,
                    }
                ),
                "expires_in_seconds": _LIFETIME,
            }

    def confirm_enrollment(self, account: UserAccount, token: str, code: str) -> list[str]:
        with self._lock:
            self._prune()
            self._budget(account.id)
            key = hashlib.sha256(token.encode()).digest()
            pending = self._enrollments.get(key)
            if pending is None or pending.account.id != account.id or pending.secret is None:
                raise AccountSecurityError("Account enrollment expired")
            current = self._current(pending)
            counter = int(self._utc() // 30)
            matched = next(
                (
                    value
                    for value in range(max(0, counter - 1), counter + 2)
                    if hmac.compare_digest(totp(pending.secret, value), code)
                ),
                None,
            )
            if matched is None:
                raise AccountSecurityError("Second factor was not accepted")
            codes = self.store.enable(current.id, pending.secret, matched)
            self._enrollments.pop(key, None)
            return codes

    def reauthenticate_factor(self, account: UserAccount, password: str, code: str) -> None:
        with self._lock:
            self._budget(account.id)
            self._password(account, password)
            if not self.store.consume(account.id, code):
                raise AccountSecurityError("Second factor was not accepted")

    def close(self) -> None:
        with self._lock:
            if self._remove_listener is not None:
                self._remove_listener()
                self._remove_listener = None
            self._challenges.clear()
            self._enrollments.clear()
            self._attempts.clear()
            self._epochs.clear()
            self._revision = 0
            self.store.close()
