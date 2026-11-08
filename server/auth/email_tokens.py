"""One-time email verification and password reset codes."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from ..persistence.database import Database, EmailTokenRecord

EmailTokenPurpose = Literal["verify", "reset", "change_email"]

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 10

DEFAULT_CODE_TTL_SECONDS = 15 * 60
DEFAULT_MAX_ATTEMPTS = 5


@dataclass(frozen=True)
class TokenVerification:
    """Result of checking an emailed code.

    Attributes:
        ok: Whether the code matched.
        reason: ``success``, ``missing``, ``expired``, ``invalid``, or
            ``locked``.
        attempts_remaining: Attempts left before the token is discarded.
        email: Verified target address for an email-change token, else empty.
    """

    ok: bool
    reason: str
    attempts_remaining: int = 0
    email: str = ""


class EmailTokenService:
    """Issue and verify short-lived codes, storing only keyed hashes."""

    def __init__(
        self,
        database: "Database",
        *,
        secret: str,
        ttl_seconds: int = DEFAULT_CODE_TTL_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ):
        """Initialize the token service.

        Args:
            database: Persistence backend.
            secret: Server-side key for hashing codes. Required.
            ttl_seconds: Code lifetime in seconds.
            max_attempts: Incorrect attempts allowed per code.
        """
        if not secret:
            raise ValueError("Email token secret must not be empty")
        self._db = database
        self._secret = secret.encode("utf-8")
        self._ttl_seconds = max(60, int(ttl_seconds))
        self._max_attempts = max(1, int(max_attempts))

    @property
    def ttl_seconds(self) -> int:
        """Code lifetime in seconds."""
        return self._ttl_seconds

    @property
    def max_attempts(self) -> int:
        """Maximum incorrect attempts per code."""
        return self._max_attempts

    def issue(
        self, username: str, purpose: EmailTokenPurpose, email: str = ""
    ) -> tuple[str, int]:
        """Create a fresh code and invalidate any previous one.

        Returns:
            ``(plaintext_code, expires_at_epoch_seconds)``. The plaintext
            code is returned only for immediate delivery by email.
        """
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        now = int(time.time())
        expires_at = now + self._ttl_seconds
        self._db.create_email_token(
            username,
            purpose,
            self._hash(username, purpose, code),
            expires_at,
            now,
            email=email,
        )
        return code, expires_at

    def verify(
        self, username: str, purpose: EmailTokenPurpose, code: str
    ) -> TokenVerification:
        """Verify and consume a code."""
        record = self._db.get_email_token(username, purpose)
        if record is None:
            return TokenVerification(False, "missing")

        now = int(time.time())
        if record.expires_at <= now:
            self._db.delete_email_tokens(username, purpose)
            return TokenVerification(False, "expired")

        normalized = _normalize_code(code)
        expected = self._hash(username, purpose, normalized)
        if not hmac.compare_digest(expected, record.token_hash):
            attempts = self._db.increment_email_token_attempts(record.id)
            remaining = max(0, self._max_attempts - attempts)
            if attempts >= self._max_attempts:
                self._db.delete_email_tokens(username, purpose)
                return TokenVerification(False, "locked", 0)
            return TokenVerification(False, "invalid", remaining)

        if not self._db.consume_email_token(record.id, now):
            return TokenVerification(False, "missing")
        return TokenVerification(True, "success", 0, record.email)

    def clear(self, username: str, purpose: EmailTokenPurpose | None = None) -> None:
        """Delete outstanding codes for a user."""
        self._db.delete_email_tokens(username, purpose)

    def _hash(self, username: str, purpose: EmailTokenPurpose, code: str) -> str:
        """Return a keyed hash that binds the code to its account and purpose."""
        payload = f"{purpose}:{username.strip().lower()}:{_normalize_code(code)}"
        return hmac.new(self._secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()


def _normalize_code(code: str) -> str:
    """Normalize user-entered codes without changing their entropy."""
    return "".join(ch for ch in (code or "").upper() if ch.isalnum())
