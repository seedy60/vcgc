"""Secure SMTP delivery for authentication emails.

The transport uses either implicit TLS (``smtps``/port 465) or STARTTLS
(port 587). Certificate verification is always performed by
:func:`ssl.create_default_context`; there is no code path that disables it.
Plaintext SMTP is only available when the server owner explicitly opts in
with ``allow_plaintext = true``, which is intended for local development.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
from typing import Any

LOG = logging.getLogger("playpalace.mail")

_VALID_SECURITY = {"starttls", "ssl", "none"}


def _coerce_bool(value: Any, default: bool = False) -> bool:
    """Parse a boolean from TOML or a string without truthy-string surprises."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


@dataclass(frozen=True)
class SMTPConfig:
    """Validated SMTP connection settings.

    Attributes:
        host: SMTP server hostname.
        port: SMTP server port.
        from_address: Envelope and header sender.
        username: Optional SMTP authentication username.
        password: Optional SMTP authentication password.
        security: ``starttls``, ``ssl``, or ``none``.
        timeout_seconds: Socket timeout.
        server_name: Product name used in message bodies.
        allow_plaintext: Whether ``security = "none"`` is permitted.
    """

    host: str
    port: int
    from_address: str
    username: str = ""
    password: str = ""
    security: str = "starttls"
    timeout_seconds: int = 15
    server_name: str = "PlayPalace"
    allow_plaintext: bool = False

    @classmethod
    def from_config(cls, section: Any, *, server_name: str = "PlayPalace") -> "SMTPConfig | None":
        """Build and validate an SMTP configuration.

        Args:
            section: The ``[smtp]`` mapping from config.toml, if present.
            server_name: Product name used in message bodies.

        Returns:
            A validated configuration, or None when SMTP is not configured.

        Raises:
            ValueError: If a configured SMTP section is incomplete or invalid.
        """
        if not isinstance(section, dict):
            return None

        raw_host = section.get("host")
        if not isinstance(raw_host, str) or not raw_host.strip():
            if any(key in section for key in ("port", "from_address", "username", "password")):
                raise ValueError("[smtp].host is required when SMTP options are present")
            return None

        host = raw_host.strip()
        raw_from = section.get("from_address")
        if not isinstance(raw_from, str) or not raw_from.strip():
            raise ValueError("[smtp].from_address is required")

        from_address = raw_from.strip()
        if "\r" in from_address or "\n" in from_address:
            raise ValueError("[smtp].from_address must not contain newlines")
        _, parsed_address = parseaddr(from_address)
        if "@" not in parsed_address or parsed_address.startswith("@") or parsed_address.endswith("@"):
            raise ValueError("[smtp].from_address must be a valid email address")

        raw_security = str(section.get("security", "starttls")).strip().lower()
        if raw_security not in _VALID_SECURITY:
            raise ValueError(
                "[smtp].security must be 'starttls', 'ssl', or 'none'"
            )

        allow_plaintext = _coerce_bool(section.get("allow_plaintext"), False)
        if raw_security == "none" and not allow_plaintext:
            raise ValueError(
                "[smtp].security='none' requires allow_plaintext = true"
            )

        try:
            port = int(section.get("port", 465 if raw_security == "ssl" else 587))
        except (TypeError, ValueError) as exc:
            raise ValueError("[smtp].port must be an integer") from exc
        if port < 1 or port > 65535:
            raise ValueError("[smtp].port must be between 1 and 65535")

        try:
            timeout_seconds = int(section.get("timeout_seconds", 15))
        except (TypeError, ValueError) as exc:
            raise ValueError("[smtp].timeout_seconds must be an integer") from exc
        timeout_seconds = max(1, min(timeout_seconds, 120))

        username = section.get("username")
        password = section.get("password")
        username_text = username.strip() if isinstance(username, str) else ""
        password_text = password if isinstance(password, str) else ""
        if bool(username_text) != bool(password_text):
            raise ValueError("[smtp].username and password must be set together")
        return cls(
            host=host,
            port=port,
            from_address=from_address,
            username=username_text,
            password=password_text,
            security=raw_security,
            timeout_seconds=timeout_seconds,
            server_name=server_name,
            allow_plaintext=allow_plaintext,
        )


class EmailService:
    """Send authentication email over secure SMTP."""

    def __init__(self, config: SMTPConfig):
        """Initialize the service with validated settings."""
        self.config = config

    @classmethod
    def from_config(
        cls, section: Any, *, server_name: str = "PlayPalace"
    ) -> "EmailService | None":
        """Create an email service when SMTP is configured."""
        config = SMTPConfig.from_config(section, server_name=server_name)
        if config is None:
            return None
        if config.security == "none":
            LOG.warning(
                "SMTP is configured without TLS; use only for local development"
            )
        LOG.info(
            "SMTP configured for %s:%s with sender %s",
            config.host,
            config.port,
            config.from_address,
        )
        return cls(config)

    @property
    def enabled(self) -> bool:
        """Whether this service can deliver mail."""
        return True

    async def send_verification_code(
        self, recipient: str, username: str, code: str, expires_minutes: int
    ) -> None:
        """Send an account verification code."""
        subject = f"Your {self.config.server_name} verification code"
        body = (
            f"Hello {username},\n\n"
            f"Your {self.config.server_name} verification code is:\n\n"
            f"{code}\n\n"
            f"This code expires in {expires_minutes} minutes. "
            "If you did not create this account, you can ignore this message.\n"
        )
        await self._send(recipient, subject, body)

    async def send_password_reset_code(
        self, recipient: str, username: str, code: str, expires_minutes: int
    ) -> None:
        """Send a password reset code."""
        subject = f"Your {self.config.server_name} password reset code"
        body = (
            f"Hello {username},\n\n"
            f"Your {self.config.server_name} password reset code is:\n\n"
            f"{code}\n\n"
            f"This code expires in {expires_minutes} minutes. "
            "If you did not request a password reset, you can ignore this message. "
            "Your password will not change unless the code is used.\n"
        )
        await self._send(recipient, subject, body)

    async def send_email_change_code(
        self, recipient: str, username: str, code: str, expires_minutes: int
    ) -> None:
        """Send a code that confirms ownership of a new email address."""
        subject = f"Your {self.config.server_name} email confirmation code"
        body = (
            f"Hello {username},\n\n"
            f"Use this code to confirm the new email address for your "
            f"{self.config.server_name} account:\n\n"
            f"{code}\n\n"
            f"This code expires in {expires_minutes} minutes. "
            "If you did not request this change, you can ignore this message. "
            "Your current email address will remain unchanged until the code "
            "is entered.\n"
        )
        await self._send(recipient, subject, body)

    async def _send(self, recipient: str, subject: str, body: str) -> None:
        """Send one message outside the event loop."""
        await asyncio.to_thread(
            self._send_sync, recipient.strip(), subject, body
        )

    def _send_sync(self, recipient: str, subject: str, body: str) -> None:
        """Deliver a message synchronously using verified TLS."""
        if not recipient or "@" not in recipient or "\r" in recipient or "\n" in recipient:
            raise ValueError("Invalid recipient email address")

        message = EmailMessage()
        message["From"] = self.config.from_address
        message["To"] = recipient
        message["Subject"] = subject
        message["Date"] = formatdate(localtime=True)
        _, sender_address = parseaddr(self.config.from_address)
        message["Message-ID"] = make_msgid(
            domain=sender_address.rpartition("@")[2] or None
        )
        message.set_content(body)

        LOG.info(
            "SMTP sending to %s via %s:%s (message-id %s)",
            recipient,
            self.config.host,
            self.config.port,
            message["Message-ID"],
        )
        context = ssl.create_default_context()
        try:
            if self.config.security == "ssl":
                with smtplib.SMTP_SSL(
                    self.config.host,
                    self.config.port,
                    timeout=self.config.timeout_seconds,
                    context=context,
                ) as smtp:
                    smtp.ehlo()
                    self._authenticate(smtp)
                    smtp.send_message(message)
            else:
                with smtplib.SMTP(
                    self.config.host,
                    self.config.port,
                    timeout=self.config.timeout_seconds,
                ) as smtp:
                    smtp.ehlo()
                    if self.config.security == "starttls":
                        smtp.starttls(context=context)
                        smtp.ehlo()
                    elif not self.config.allow_plaintext:
                        raise ValueError("Plaintext SMTP is not permitted")
                    self._authenticate(smtp)
                    smtp.send_message(message)
        except Exception:
            LOG.exception(
                "SMTP delivery failed for %s (message-id %s)",
                recipient,
                message["Message-ID"],
            )
            raise
        LOG.info(
            "SMTP accepted message for %s (message-id %s)",
            recipient,
            message["Message-ID"],
        )

    def _authenticate(self, smtp: smtplib.SMTP) -> None:
        """Authenticate when a username is configured."""
        if self.config.username:
            smtp.login(self.config.username, self.config.password)
