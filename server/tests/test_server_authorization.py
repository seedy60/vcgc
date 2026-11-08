"""Tests for Server._handle_authorize covering key flows."""

import asyncio
import json
import time
from types import SimpleNamespace

import pytest


from server.core.server import Server, DEFAULT_WS_MAX_MESSAGE_BYTES
from server.auth.auth import AuthResult
from server.core.users.base import TrustLevel
from server.core.tables.table import Table
from server.games.base import Player


class DummyClient:
    def __init__(self, address="127.0.0.1:12345"):
        self.username = None
        self.authenticated = False
        self.sent = []
        self.address = address
        self.closed = False
        self.replaced = False
        self.client_type = ""
        self.platform = ""

    async def send(self, payload):
        self.sent.append(payload)

    async def close(self):
        self.closed = True


class DummyAuth:
    def __init__(
        self, *, authenticate_result=AuthResult.SUCCESS, register_result=True, user_record=None
    ):
        self.authenticate_result = authenticate_result
        self.register_result = register_result
        self.calls = {"authenticate": [], "register": []}
        self.user_record = user_record

    def authenticate(self, username, password, **kwargs):
        self.calls["authenticate"].append((username, password))
        return self.authenticate_result

    def register(self, username, password, **kwargs):
        self.calls["register"].append((username, password))
        return self.register_result

    def get_user(self, username):
        return self.user_record

    def create_session(self, username, ttl_seconds):
        return f"access-{username}", 9999999999

    def create_refresh_token(self, username, ttl_seconds):
        return f"refresh-{username}", 9999999999

    def refresh_session(self, refresh_token, access_ttl_seconds, refresh_ttl_seconds):
        return None


@pytest.fixture
def server(tmp_path):
    db_path = tmp_path / "auth.db"
    srv = Server(db_path=str(db_path), locales_dir="locales", config_path=tmp_path / "missing.toml")
    return srv


@pytest.mark.asyncio
async def test_authorize_registers_and_waits_for_approval(monkeypatch, server):
    server._db = SimpleNamespace(get_user_count=lambda: 5)
    record = SimpleNamespace(
        username="newbie",
        locale="en",
        uuid="uuid-1",
        trust_level=TrustLevel.USER,
        approved=False,
        preferences_json=json.dumps({"play_turn_sound": False, "dice_keeping_style": "playpalace"}),
        fluent_languages=[],
    )
    auth = DummyAuth(
        authenticate_result=AuthResult.USER_NOT_FOUND, register_result=True, user_record=record
    )
    server._auth = auth
    server._tables = SimpleNamespace(find_user_table=lambda username: None)

    notifications = []
    server._notify_admins = lambda message_id, sound: notifications.append((message_id, sound))

    sent_game_list = []

    async def fake_send_game_list(client):
        sent_game_list.append(client.username)

    server._send_game_list = fake_send_game_list

    main_menu_calls = []

    def fake_show_main_menu(user):
        main_menu_calls.append(user.username)

    server._show_main_menu = fake_show_main_menu

    client = DummyClient()
    packet = {"username": "newbie", "password": "validpass"}

    await server._handle_authorize(client, packet)

    assert auth.calls["authenticate"] == [("newbie", "validpass")]
    assert auth.calls["register"] == [("newbie", "validpass")]
    assert notifications == [("account-request", "accountrequest.ogg")]
    assert client.authenticated and client.username == "newbie"
    assert sent_game_list == ["newbie"]
    assert main_menu_calls == ["newbie"]
    assert "newbie" in server._users


@pytest.mark.asyncio
async def test_authorize_existing_admin_announces(monkeypatch, server):
    server._db = SimpleNamespace(
        get_user_count=lambda: 0,
        get_pending_users=lambda exclude_banned=True: [],
    )
    record = SimpleNamespace(
        username="admin",
        locale="es",
        uuid="uuid-2",
        trust_level=TrustLevel.ADMIN,
        approved=True,
        preferences_json="{}",
        fluent_languages=[],
    )
    auth = DummyAuth(authenticate_result=AuthResult.SUCCESS, user_record=record)
    server._auth = auth
    server._tables = SimpleNamespace(find_user_table=lambda username: None)

    broadcasts = []
    server._broadcast_presence_l = lambda msg, player, sound: broadcasts.append(
        (msg, player, sound)
    )
    server._broadcast_admin_announcement = lambda player: broadcasts.append(("admin", player))
    server._broadcast_server_owner_announcement = lambda player: broadcasts.append(
        ("owner", player)
    )
    main_menu_calls = []
    server._show_main_menu = lambda user: main_menu_calls.append(user.username)
    server._notify_admins = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("should not notify")
    )

    async def fake_send_game_list(client):
        fake_send_game_list.calls.append(client.username)

    fake_send_game_list.calls = []

    server._send_game_list = fake_send_game_list

    client = DummyClient()
    packet = {"username": "admin", "password": "validpass"}

    await server._handle_authorize(client, packet)

    assert auth.calls["register"] == []
    assert broadcasts[0] == ("user-online", "admin", "onlineadmin.ogg")
    assert ("admin", "admin") in [(b[1], b[1]) for b in broadcasts if b[0] == "admin"]
    assert main_menu_calls == ["admin"]
    assert fake_send_game_list.calls == ["admin"]
    assert "admin" in server._users


@pytest.mark.asyncio
async def test_register_requires_username_and_password(server):
    client = DummyClient()

    await server._handle_register(client, {"username": "", "password": ""})

    assert len(client.sent) == 1
    assert client.sent[0]["type"] == "registration_error"
    assert "Username" in client.sent[0]["message"] and str(server._username_min_length) in client.sent[0]["message"]


@pytest.mark.asyncio
async def test_register_sends_verification_email(server):
    server._db = SimpleNamespace(get_user_count=lambda: 3)
    auth = DummyAuth(register_result=True)
    server._auth = auth
    server._email_tokens = SimpleNamespace(
        issue=lambda username, purpose: ("CODE123456", int(time.time()) + 900)
    )
    sent_emails = []

    async def fake_verification_email(email, username, code, expires_minutes):
        sent_emails.append((email, username, code, expires_minutes))

    server._email_service = SimpleNamespace(
        send_verification_code=fake_verification_email,
    )

    client = DummyClient()
    await server._handle_register(
        client,
        {
            "username": "fresh",
            "password": "validpass",
            "email": "Fresh@Example.com",
        },
    )

    assert client.sent[-1]["type"] == "registration_pending"
    assert client.sent[-1]["email"] == "fresh@example.com"
    assert client.sent[-1]["email_sent"] is True
    assert auth.calls["register"] == [("fresh", "validpass")]
    assert sent_emails and sent_emails[0][:3] == ("fresh@example.com", "fresh", "CODE123456")


@pytest.mark.asyncio
async def test_register_rejects_duplicate_username(server):
    server._db = SimpleNamespace(get_user_count=lambda: 0)
    auth = DummyAuth(register_result=False)
    server._auth = auth
    server._email_tokens = SimpleNamespace(
        issue=lambda username, purpose: ("CODE123456", int(time.time()) + 900)
    )

    async def fake_verification_email(email, username, code, expires_minutes):
        return None

    server._email_service = SimpleNamespace(
        send_verification_code=fake_verification_email,
    )

    client = DummyClient()
    await server._handle_register(
        client,
        {"username": "taken", "password": "validpass", "email": "taken@example.com"},
    )

    assert client.sent[-1]["type"] == "registration_error"
    assert "already taken" in client.sent[-1]["message"] or "Username" in client.sent[-1]["message"]
    assert auth.calls["register"] == [("taken", "validpass")]


@pytest.mark.asyncio
async def test_authorize_rejects_invalid_username_length(server):
    server._auth = DummyAuth()
    client = DummyClient()
    packet = {"username": "aa", "password": "validpass"}

    await server._handle_authorize(client, packet)

    assert server._auth.calls["authenticate"] == []
    assert len(client.sent) == 3
    assert "Username" in client.sent[1]["text"] and str(server._username_min_length) in client.sent[1]["text"]


@pytest.mark.asyncio
async def test_authorize_rejects_invalid_password_length(server):
    server._auth = DummyAuth()
    client = DummyClient()
    packet = {"username": "validuser", "password": "short"}

    await server._handle_authorize(client, packet)

    assert server._auth.calls["authenticate"] == []
    assert len(client.sent) == 3
    assert "Password" in client.sent[1]["text"] and str(server._password_min_length) in client.sent[1]["text"]


@pytest.mark.asyncio
async def test_authorize_handoffs_existing_session(monkeypatch, server):
    record = SimpleNamespace(
        username="alice",
        locale="en",
        uuid="uuid-alice",
        trust_level=TrustLevel.USER,
        approved=True,
        preferences_json="{}",
        fluent_languages=[],
    )
    auth = DummyAuth(user_record=record)
    server._auth = auth
    server._tables = SimpleNamespace(find_user_table=lambda username: None)

    async def fake_send_game_list(client):
        fake_send_game_list.calls.append(client.username)

    fake_send_game_list.calls = []
    server._send_game_list = fake_send_game_list
    server._show_main_menu = lambda user: None

    client1 = DummyClient()
    await server._handle_authorize(client1, {"username": "alice", "password": "validpass"})
    assert client1.authenticated

    client2 = DummyClient(address="127.0.0.1:2222")
    await server._handle_authorize(client2, {"username": "alice", "password": "validpass"})

    # Existing connection receives disconnect and is closed
    assert any(packet["type"] == "disconnect" for packet in client1.sent)
    assert client1.closed

    # New connection inherits the session
    user = server._users["alice"]
    assert user.connection is client2
    assert client2.authenticated
    assert client2.sent[0]["type"] == "authorize_success"


@pytest.mark.asyncio
async def test_authorize_rejoin_replays_transcript(monkeypatch, server):
    from server.core.users.test_user import MockUser

    record = SimpleNamespace(
        username="player",
        locale="en",
        uuid="uuid-player",
        trust_level=TrustLevel.USER,
        approved=True,
        preferences_json="{}",
        fluent_languages=[],
    )
    auth = DummyAuth(user_record=record)
    server._auth = auth

    async def fake_send_game_list(client):
        return None

    server._send_game_list = fake_send_game_list

    table = Table(table_id="tbl1", game_type="dummy", host="player")
    table._manager = server._tables
    table._server = server
    server._tables._tables[table.table_id] = table
    table.add_member("player", MockUser("player", uuid="uuid-player"), as_spectator=False)

    class DummyGame:
        def __init__(self):
            self.player = Player(id="uuid-player", name="player", is_bot=True)
            self.players = [self.player]
            self.broadcasts = []
            self.sounds = []
            self.menus_rebuilt = 0
            self.player_menu_rebuilt = 0
            self.transcript = [{"text": "Bot played card", "buffer": "table"}]

        def to_json(self):
            return "{}"

        def get_player_by_id(self, player_id):
            return self.player if self.player.id == player_id else None

        def attach_user(self, player_id, user):
            self.attached = user

        def broadcast_l(self, message_id, **kwargs):
            self.broadcasts.append((message_id, kwargs))

        def broadcast_sound(self, sound_name):
            self.sounds.append(sound_name)

        def rebuild_all_menus(self):
            self.menus_rebuilt += 1

        def rebuild_player_menu(self, player):
            self.player_menu_rebuilt += 1

        def get_transcript(self, player_id):
            return list(self.transcript)

    dummy_game = DummyGame()
    table.game = dummy_game

    client = DummyClient()
    packet = {"username": "player", "password": "validpass"}
    await server._handle_authorize(client, packet)

    assert dummy_game.player.is_bot is False
    assert ("player-took-over", {"player": "player"}) in dummy_game.broadcasts
    assert "join.ogg" in dummy_game.sounds
    assert dummy_game.menus_rebuilt == 1
    assert dummy_game.player_menu_rebuilt == 1
    assert server._user_states["player"]["menu"] == "in_game"

    user = server._users["player"]
    queued = user.get_queued_messages()
    assert any(packet.get("muted") for packet in queued)
    assert any(packet.get("text") == "Bot played card" for packet in queued)


@pytest.mark.asyncio
async def test_register_rejects_invalid_lengths(server):
    server._auth = DummyAuth()
    client = DummyClient()

    await server._handle_register(client, {"username": "aa", "password": "pw"})

    assert server._auth.calls["register"] == []
    assert "Username" in client.sent[-1]["message"] and str(server._username_min_length) in client.sent[-1]["message"]


@pytest.mark.asyncio
async def test_login_rate_limit_by_ip(server):
    server._auth = DummyAuth(authenticate_result=AuthResult.USER_NOT_FOUND, register_result=False)
    server._login_ip_limit = 1
    server._db = SimpleNamespace(get_user_count=lambda: 0)
    client = DummyClient()
    packet = {"username": "foo", "password": "validpass"}

    await server._handle_authorize(client, packet)
    client.sent.clear()
    await server._handle_authorize(client, packet)

    assert server._auth.calls["authenticate"] == [("foo", "validpass")]
    assert "Too many login attempts" in client.sent[1]["text"]


@pytest.mark.asyncio
async def test_login_rate_limit_by_username(server):
    server._auth = DummyAuth(authenticate_result=AuthResult.WRONG_PASSWORD)
    server._login_user_limit = 1
    server._db = SimpleNamespace(get_user_count=lambda: 1)
    client = DummyClient()
    packet = {"username": "repeat", "password": "validpass"}

    await server._handle_authorize(client, packet)
    client.sent.clear()
    await server._handle_authorize(client, packet)

    assert "Too many failed login attempts" in client.sent[1]["text"]


@pytest.mark.asyncio
async def test_registration_rate_limit_by_ip(server):
    server._db = SimpleNamespace(get_user_count=lambda: 0)
    server._auth = DummyAuth(register_result=True)
    server._registration_ip_limit = 1
    client = DummyClient()

    server._email_tokens = SimpleNamespace(
        issue=lambda username, purpose: ("CODE123456", int(time.time()) + 900)
    )

    async def fake_verification_email(email, username, code, expires_minutes):
        return None

    server._email_service = SimpleNamespace(
        send_verification_code=fake_verification_email,
    )

    await server._handle_register(
        client, {"username": "first", "password": "validpass", "email": "first@example.com"}
    )
    client.sent.clear()
    await server._handle_register(
        client, {"username": "second", "password": "validpass", "email": "second@example.com"}
    )

    assert "Too many registration attempts" in client.sent[-1]["message"]


@pytest.mark.asyncio
async def test_refresh_session_success(server):
    record = SimpleNamespace(
        username="alice",
        locale="en",
        uuid="uuid-3",
        trust_level=TrustLevel.USER,
        approved=True,
        preferences_json="{}",
        fluent_languages=[],
    )

    class RefreshAuth(DummyAuth):
        def refresh_session(self, refresh_token, access_ttl_seconds, refresh_ttl_seconds):
            return ("alice", "access-token", 999999, "refresh-token", 999999)

    server._auth = RefreshAuth(user_record=record)
    server._db = SimpleNamespace(get_user_count=lambda: 1)
    server._tables = SimpleNamespace(find_user_table=lambda username: None)

    sent_game_list = []

    async def fake_send_game_list(client):
        sent_game_list.append(client.username)

    server._send_game_list = fake_send_game_list

    client = DummyClient()
    await server._handle_refresh_session(
        client,
        {
            "refresh_token": "refresh-token",
            "username": "alice",
            "client_type": "Desktop",
            "platform": "Windows 11",
        },
    )

    assert client.authenticated is True
    assert client.username == "alice"
    assert any(p.get("type") == "refresh_session_success" for p in client.sent)
    assert sent_game_list == ["alice"]
    assert client.client_type == "Desktop"
    assert client.platform == "Windows 11"
    user = server._users["alice"]
    assert user.client_type == "Desktop"
    assert user.platform == "Windows 11"


@pytest.mark.asyncio
async def test_refresh_session_failure_disconnects(server):
    class RefreshAuth(DummyAuth):
        def refresh_session(self, refresh_token, access_ttl_seconds, refresh_ttl_seconds):
            return None

    server._auth = RefreshAuth()
    client = DummyClient()

    await server._handle_refresh_session(
        client, {"refresh_token": "refresh-token", "username": "alice"}
    )

    assert any(p.get("type") == "refresh_session_failure" for p in client.sent)
    assert any(p.get("type") == "disconnect" for p in client.sent)


@pytest.mark.asyncio
async def test_refresh_session_rate_limited(server):
    server._auth = DummyAuth()
    server._refresh_ip_limit = 1
    client = DummyClient()

    await server._handle_refresh_session(
        client, {"refresh_token": "refresh-token", "username": "alice"}
    )
    client.sent.clear()
    await server._handle_refresh_session(
        client, {"refresh_token": "refresh-token", "username": "alice"}
    )

    assert any(p.get("type") == "disconnect" for p in client.sent)


def test_auth_limits_loaded_from_config(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[auth]
username_min_length = 5
username_max_length = 12
password_min_length = 10
password_max_length = 42
refresh_token_ttl_seconds = 7200
[auth.rate_limits]
refresh_per_minute = 12
refresh_window_seconds = 90
[network]
max_message_bytes = 2048
"""
    )
    srv = Server(db_path=str(tmp_path / "auth.db"), locales_dir="locales", config_path=config_path)

    assert srv._username_min_length == 5
    assert srv._username_max_length == 12
    assert srv._password_min_length == 10
    assert srv._password_max_length == 42
    assert srv._ws_max_message_size == 2048
    assert srv._refresh_token_ttl_seconds == 7200
    assert srv._refresh_ip_limit == 12
    assert srv._refresh_ip_window == 90


def test_network_max_size_defaults(tmp_path):
    srv = Server(
        db_path=str(tmp_path / "auth.db"),
        locales_dir="locales",
        config_path=tmp_path / "missing.toml",
    )
    assert srv._ws_max_message_size == DEFAULT_WS_MAX_MESSAGE_BYTES
