"""Tests for SMTP-backed email verification and password recovery."""

from __future__ import annotations

import smtplib
import ssl
import time
from types import SimpleNamespace

import pytest

from server.auth.auth import AuthManager, AuthResult
from server.auth.email_tokens import EmailTokenService
from server.core.server import Server
from server.mail.smtp import EmailService, SMTPConfig
from server.messages.localization import Localization
from server.persistence.database import Database


class DummyClient:
    """Minimal websocket client for handler tests."""

    def __init__(self, address: str = "127.0.0.1:12345"):
        self.address = address
        self.username = None
        self.authenticated = False
        self.sent: list[dict] = []
        self.client_type = ""
        self.platform = ""

    async def send(self, payload: dict) -> None:
        self.sent.append(payload)


class FakeEmailService:
    """Capture codes instead of sending mail."""

    def __init__(self):
        self.verification: list[tuple[str, str, str, int]] = []
        self.reset: list[tuple[str, str, str, int]] = []
        self.email_change: list[tuple[str, str, str, int]] = []

    async def send_verification_code(self, recipient, username, code, expires_minutes):
        self.verification.append((recipient, username, code, expires_minutes))

    async def send_password_reset_code(self, recipient, username, code, expires_minutes):
        self.reset.append((recipient, username, code, expires_minutes))

    async def send_email_change_code(self, recipient, username, code, expires_minutes):
        self.email_change.append((recipient, username, code, expires_minutes))


@pytest.fixture
def email_server(tmp_path):
    server = Server(
        host="127.0.0.1",
        port=0,
        db_path=tmp_path / "email.db",
        config_path=tmp_path / "missing.toml",
        preload_locales=True,
    )
    server._db.connect()
    server._auth = AuthManager(server._db)
    server._email_service = FakeEmailService()
    server._email_tokens = EmailTokenService(
        server._db, secret="test-secret", ttl_seconds=900, max_attempts=5
    )
    yield server
    server._db.close()


def test_token_is_hashed_one_time_and_limited(email_server):
    """Codes are stored hashed, consumed once, and locked after failures."""
    tokens = email_server._email_tokens
    code, expires_at = tokens.issue("alice", "verify")
    record = email_server._db.get_email_token("alice", "verify")

    assert record is not None
    assert record.token_hash != code
    assert expires_at > int(time.time())
    assert tokens.verify("alice", "verify", "WRONG").ok is False
    assert tokens.verify("alice", "verify", code.lower()).ok is True
    assert tokens.verify("alice", "verify", code).ok is False

    # Consumption is atomic: a second consumer cannot reuse the same row.
    code, _ = tokens.issue("alice", "verify")
    record = email_server._db.get_email_token("alice", "verify")
    assert record is not None
    assert email_server._db.consume_email_token(record.id, int(time.time())) is True
    assert email_server._db.consume_email_token(record.id, int(time.time())) is False

    code, _ = tokens.issue("alice", "verify")
    for _ in range(4):
        result = tokens.verify("alice", "verify", "WRONG")
        assert result.ok is False
    locked = tokens.verify("alice", "verify", "WRONG")
    assert locked.reason == "locked"
    assert email_server._db.get_email_token("alice", "verify") is None


def test_token_expires(email_server, monkeypatch):
    """Expired codes cannot be used."""
    now = [1000]
    monkeypatch.setattr(time, "time", lambda: now[0])
    code, _ = email_server._email_tokens.issue("alice", "verify")
    now[0] += 901

    result = email_server._email_tokens.verify("alice", "verify", code)

    assert result.ok is False
    assert result.reason == "expired"


@pytest.mark.asyncio
async def test_email_change_requires_confirmation_before_updating(email_server):
    """A new address is only applied after its emailed code is verified."""
    client = DummyClient()
    await email_server._handle_register(
        client,
        {"username": "alice", "password": "old-password", "email": "old@example.com"},
    )
    code = email_server._email_service.verification[-1][2]
    await email_server._handle_verify_email(client, {"username": "alice", "code": code})

    client.username = "alice"
    client.authenticated = True
    email_server._users["alice"] = SimpleNamespace(username="alice", locale="en")

    await email_server._handle_request_email_change(
        client, {"email": "New@Example.com"}
    )

    pending = client.sent[-1]
    assert pending["type"] == "email_change_pending"
    assert pending["email"] == "new@example.com"
    assert email_server._auth.get_user("alice").email == "old@example.com"
    assert email_server._email_service.email_change[-1][0] == "new@example.com"

    change_code = email_server._email_service.email_change[-1][2]
    await email_server._handle_verify_email_change(client, {"code": change_code})

    assert client.sent[-1]["type"] == "email_change_complete"
    updated = email_server._auth.get_user("alice")
    assert updated.email == "new@example.com"
    assert updated.email_verified is True
    assert email_server._db.get_email_token("alice", "change_email") is None


async def _prepare_verified_account(email_server):
    """Register and verify an account for authenticated email-change tests."""
    client = DummyClient()
    await email_server._handle_register(
        client,
        {"username": "alice", "password": "old-password", "email": "old@example.com"},
    )
    code = email_server._email_service.verification[-1][2]
    await email_server._handle_verify_email(client, {"username": "alice", "code": code})
    client.sent.clear()
    return client


def _expire_email_token(server, username: str, purpose: str) -> None:
    """Move a persisted token into the past without waiting for its TTL."""
    record = server._db.get_email_token(username, purpose)
    assert record is not None
    conn = server._db._get_conn()
    conn.execute(
        "UPDATE email_tokens SET expires_at = ? WHERE id = ?",
        (int(time.time()) - 1, record.id),
    )
    conn.commit()


@pytest.mark.parametrize(
    ("state", "expected_key", "attempts"),
    [
        ("invalid", "password-reset-code-invalid", 1),
        ("expired", "password-reset-code-expired", 1),
        ("locked", "password-reset-code-locked", 5),
    ],
)
@pytest.mark.asyncio
async def test_password_reset_failures_use_localized_messages(
    email_server, state: str, expected_key: str, attempts: int
):
    """Reset failures map invalid, expired, and locked states to localized text."""
    client = await _prepare_verified_account(email_server)
    await email_server._handle_request_password_reset(client, {"username": "alice"})
    code = email_server._email_service.reset[-1][2]
    if state == "expired":
        _expire_email_token(email_server, "alice", "reset")
        submitted_code = code
    else:
        submitted_code = "WRONGCODE"

    for _ in range(attempts):
        await email_server._handle_reset_password(
            client,
            {
                "username": "alice",
                "code": submitted_code,
                "new_password": "new-password",
            },
        )

    packet = client.sent[-1]
    assert packet["type"] == "password_reset_failed"
    assert packet["message"] == Localization.get("en", expected_key)


@pytest.mark.parametrize(
    ("state", "expected_key", "attempts"),
    [
        ("invalid", "email-change-code-invalid", 1),
        ("expired", "email-change-code-expired", 1),
        ("locked", "email-change-code-locked", 5),
    ],
)
@pytest.mark.asyncio
async def test_email_change_failures_use_localized_messages(
    email_server, state: str, expected_key: str, attempts: int
):
    """Email-change failures map invalid, expired, and locked states to localized text."""
    client = await _prepare_verified_account(email_server)
    client.username = "alice"
    client.authenticated = True
    email_server._users["alice"] = SimpleNamespace(username="alice", locale="en")

    await email_server._handle_request_email_change(
        client, {"email": "new@example.com"}
    )
    code = email_server._email_service.email_change[-1][2]
    if state == "expired":
        _expire_email_token(email_server, "alice", "change_email")
        submitted_code = code
    else:
        submitted_code = "WRONGCODE"

    for _ in range(attempts):
        await email_server._handle_verify_email_change(
            client, {"code": submitted_code}
        )

    packet = client.sent[-1]
    assert packet["type"] == "email_change_failed"
    assert packet["message"] == Localization.get("en", expected_key)
    assert email_server._auth.get_user("alice").email == "old@example.com"


@pytest.mark.asyncio
async def test_new_password_reset_code_invalidates_previous_code(email_server):
    """Requesting another reset code makes the prior code unusable."""
    client = await _prepare_verified_account(email_server)
    email_server._password_reset_user_limit = 0

    await email_server._handle_request_password_reset(client, {"username": "alice"})
    first_code = email_server._email_service.reset[-1][2]
    await email_server._handle_request_password_reset(client, {"username": "alice"})
    second_code = email_server._email_service.reset[-1][2]
    assert first_code != second_code

    await email_server._handle_reset_password(
        client,
        {
            "username": "alice",
            "code": first_code,
            "new_password": "new-password",
        },
    )
    assert client.sent[-1]["type"] == "password_reset_failed"
    assert client.sent[-1]["message"] == Localization.get(
        "en", "password-reset-code-invalid"
    )
    assert email_server._auth.authenticate("alice", "new-password") != AuthResult.SUCCESS

    await email_server._handle_reset_password(
        client,
        {
            "username": "alice",
            "code": second_code,
            "new_password": "new-password",
        },
    )
    assert client.sent[-1]["type"] == "password_reset_complete"
    assert email_server._auth.authenticate("alice", "new-password") == AuthResult.SUCCESS


@pytest.mark.asyncio
async def test_new_email_change_code_invalidates_previous_code(email_server):
    """Requesting another email-change code makes the prior code unusable."""
    client = await _prepare_verified_account(email_server)
    client.username = "alice"
    client.authenticated = True
    email_server._users["alice"] = SimpleNamespace(username="alice", locale="en")

    await email_server._handle_request_email_change(
        client, {"email": "new@example.com"}
    )
    first_code = email_server._email_service.email_change[-1][2]
    await email_server._handle_request_email_change(
        client, {"email": "new@example.com"}
    )
    second_code = email_server._email_service.email_change[-1][2]
    assert first_code != second_code

    await email_server._handle_verify_email_change(client, {"code": first_code})
    assert client.sent[-1]["type"] == "email_change_failed"
    assert client.sent[-1]["message"] == Localization.get(
        "en", "email-change-code-invalid"
    )
    assert email_server._auth.get_user("alice").email == "old@example.com"

    await email_server._handle_verify_email_change(client, {"code": second_code})
    assert client.sent[-1]["type"] == "email_change_complete"
    assert email_server._auth.get_user("alice").email == "new@example.com"


def test_account_settings_packet_reports_email_delivery_availability(email_server):
    """The settings packet disables the client flow without email support."""
    packets = []
    user = SimpleNamespace(username="alice", queue_packet=packets.append)

    email_server._show_account_settings(user)
    assert packets[-1]["type"] == "open_account_settings"
    assert packets[-1]["available"] is True

    email_server._email_service = None
    email_server._show_account_settings(user)
    assert packets[-1]["available"] is False


@pytest.mark.parametrize("failing_send", [False, True], ids=["not-configured", "send-error"])
@pytest.mark.asyncio
async def test_email_change_reports_unavailable_delivery(email_server, failing_send: bool):
    """Account settings report missing or failing email delivery to the client."""
    client = await _prepare_verified_account(email_server)
    client.username = "alice"
    client.authenticated = True
    email_server._users["alice"] = SimpleNamespace(username="alice", locale="en")

    if failing_send:
        async def fail_send(*_args):
            raise RuntimeError("SMTP unavailable")

        email_server._email_service.send_email_change_code = fail_send
        expected_key = "email-change-send-failed"
    else:
        email_server._email_service = None
        expected_key = "email-change-unavailable"

    await email_server._handle_request_email_change(
        client, {"email": "new@example.com"}
    )

    packet = client.sent[-1]
    assert packet["type"] == "email_change_failed"
    assert packet["message"] == Localization.get("en", expected_key)


def test_email_change_token_carries_target_address(email_server):
    """The pending target address survives issue and verification."""
    code, _ = email_server._email_tokens.issue(
        "alice", "change_email", email="new@example.com"
    )
    record = email_server._db.get_email_token("alice", "change_email")
    assert record is not None and record.email == "new@example.com"

    result = email_server._email_tokens.verify("alice", "change_email", code)
    assert result.ok is True
    assert result.email == "new@example.com"


@pytest.mark.asyncio
async def test_registration_verification_and_password_reset(email_server):
    """A new account must verify email before login and can reset its password."""
    client = DummyClient()
    await email_server._handle_register(
        client,
        {"username": "alice", "password": "old-password", "email": "Alice@Example.com"},
    )

    assert client.sent[-1]["type"] == "registration_pending"
    assert client.sent[-1]["email"] == "alice@example.com"
    user = email_server._auth.get_user("alice")
    assert user is not None
    assert user.email == "alice@example.com"
    assert user.email_verified is False

    code = email_server._email_service.verification[-1][2]
    await email_server._handle_verify_email(
        client, {"username": "alice", "code": code}
    )
    assert client.sent[-1]["type"] == "email_verified"
    assert email_server._auth.get_user("alice").email_verified is True

    # Password login is now possible.
    assert email_server._auth.authenticate("alice", "old-password") == AuthResult.SUCCESS

    await email_server._handle_request_password_reset(client, {"username": "alice"})
    assert client.sent[-1]["type"] == "password_reset_requested"
    reset_code = email_server._email_service.reset[-1][2]
    await email_server._handle_reset_password(
        client,
        {
            "username": "alice",
            "code": reset_code,
            "new_password": "new-password",
        },
    )
    assert client.sent[-1]["type"] == "password_reset_complete"
    assert email_server._auth.authenticate("alice", "old-password") != AuthResult.SUCCESS
    assert email_server._auth.authenticate("alice", "new-password") == AuthResult.SUCCESS


@pytest.mark.asyncio
async def test_unverified_account_is_rejected_at_login(email_server):
    """Password authentication succeeds, but unverified accounts cannot enter."""
    client = DummyClient()
    await email_server._handle_register(
        client,
        {"username": "alice", "password": "old-password", "email": "alice@example.com"},
    )
    client.sent.clear()

    await email_server._handle_authorize(
        client, {"username": "alice", "password": "old-password"}
    )

    assert client.sent[-1]["type"] == "email_verification_required"
    assert client.authenticated is False


@pytest.mark.asyncio
async def test_unknown_user_requires_explicit_registration_when_smtp_configured(email_server):
    """The legacy first-login registration path is disabled with SMTP."""
    client = DummyClient()

    await email_server._handle_authorize(
        client, {"username": "unknown", "password": "valid-password"}
    )

    assert client.sent[-1]["type"] == "registration_required"
    assert email_server._auth.get_user("unknown") is None


def test_smtp_requires_tls_and_valid_from_address(tmp_path):
    """SMTP configuration cannot silently disable TLS."""
    with pytest.raises(ValueError, match="allow_plaintext"):
        SMTPConfig.from_config(
            {
                "host": "smtp.example.com",
                "from_address": "PlayPalace <no-reply@example.com>",
                "security": "none",
            }
        )

    with pytest.raises(ValueError, match="allow_plaintext"):
        SMTPConfig.from_config(
            {
                "host": "smtp.example.com",
                "from_address": "PlayPalace <no-reply@example.com>",
                "security": "none",
                "allow_plaintext": "false",
            }
        )

    with pytest.raises(ValueError, match="from_address"):
        SMTPConfig.from_config({"host": "smtp.example.com"})

    config = SMTPConfig.from_config(
        {
            "host": "smtp.example.com",
            "port": 587,
            "from_address": "PlayPalace <no-reply@example.com>",
            "security": "starttls",
        }
    )
    assert config is not None
    assert config.security == "starttls"


def test_smtp_starttls_uses_verified_context(monkeypatch):
    """STARTTLS is negotiated with a default, certificate-validating context."""
    calls = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            calls["host"] = host
            calls["port"] = port
            calls["timeout"] = timeout

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def ehlo(self):
            return None

        def starttls(self, *, context):
            calls["context"] = context

        def login(self, username, password):
            calls["login"] = (username, password)

        def send_message(self, message):
            calls["message"] = message

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    service = EmailService(
        SMTPConfig(
            host="smtp.example.com",
            port=587,
            from_address="no-reply@example.com",
            username="user",
            password="secret",
        )
    )
    service._send_sync("alice@example.com", "Subject", "Body")

    assert calls["port"] == 587
    assert isinstance(calls["context"], ssl.SSLContext)
    assert calls["context"].verify_mode == ssl.CERT_REQUIRED
    assert calls["login"] == ("user", "secret")
    assert calls["message"]["To"] == "alice@example.com"
