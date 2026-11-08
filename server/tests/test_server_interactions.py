"""Tests for chat handling, keybind announcements, and logout flow in core.server."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from server.core.server import Server
from server.core.users.base import TrustLevel


class DummyConn:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, payload):
        self.sent.append(payload)


class DummyUser:
    def __init__(self, username: str, approved: bool = True, locale: str = "en"):
        self.username = username
        self.approved = approved
        self.locale = locale
        self.trust_level = TrustLevel.USER
        self.connection = DummyConn()
        self.spoken: list[tuple[str, dict]] = []
        self.menus: list[str] = []

    def speak(self, text: str, buffer: str = "misc") -> None:
        pass

    def speak_l(self, message_id: str, buffer: str = "misc", **kwargs) -> None:
        self.spoken.append((message_id, kwargs))

    def show_menu(self, menu_id: str, *args, **kwargs) -> None:
        self.menus.append(menu_id)


class FakeTables:
    def __init__(self, mapping: dict[str, object] | None = None):
        self.mapping = mapping or {}

    def find_user_table(self, username: str):
        return self.mapping.get(username)


@pytest.fixture
def server(tmp_path, monkeypatch):
    srv = Server(host="127.0.0.1", port=0, db_path=tmp_path / "db.sqlite", preload_locales=True)
    # deterministic localization helpers
    monkeypatch.setattr(
        "server.messages.localization.Localization.format_list_and",
        lambda _locale, names: " and ".join(names),
    )
    monkeypatch.setattr(
        "server.messages.localization.Localization.get",
        lambda _locale, key, **kwargs: key,
    )
    return srv


@pytest.mark.asyncio
async def test_handle_chat_local_only_approved_receive(server):
    alice = DummyUser("alice", approved=True)
    bob = DummyUser("bob", approved=True)
    carol = DummyUser("carol", approved=False)
    table = SimpleNamespace(
        members=[
            SimpleNamespace(username="alice"),
            SimpleNamespace(username="bob"),
            SimpleNamespace(username="carol"),
        ]
    )
    server._users = {"alice": alice, "bob": bob, "carol": carol}
    server._tables = FakeTables({"alice": table})
    client = SimpleNamespace(username="alice")

    await server._handle_chat(client, {"convo": "local", "message": "hi", "language": "en"})

    assert bob.connection.sent and bob.connection.sent[0]["message"] == "hi"
    assert carol.connection.sent == []  # unapproved not notified


@pytest.mark.asyncio
async def test_handle_chat_global_only_approved(server):
    alice = DummyUser("alice", approved=True)
    bob = DummyUser("bob", approved=False)
    server._users = {"alice": alice, "bob": bob}
    server._tables = FakeTables()  # no tables -> global path
    client = SimpleNamespace(username="alice")

    await server._handle_chat(client, {"convo": "global", "message": "hey", "language": "en"})

    assert alice.connection.sent and alice.connection.sent[0]["message"] == "hey"
    assert bob.connection.sent == []


@pytest.mark.asyncio
async def test_handle_keybind_who_online_none(server):
    alice = DummyUser("alice")
    server._users = {"alice": alice}
    server._tables = FakeTables()
    client = SimpleNamespace(username="alice")

    await server._handle_keybind(client, {"key": "w", "control": True})

    assert alice.spoken == [("online-users-one", {"count": 1, "users": "alice"})]


@pytest.mark.asyncio
async def test_handle_keybind_who_online_lists_players(server):
    alice = DummyUser("alice")
    bob = DummyUser("bob")
    server._users = {"alice": alice, "bob": bob}
    server._tables = FakeTables()
    client = SimpleNamespace(username="alice")

    await server._handle_keybind(client, {"key": "w", "control": True})

    assert alice.spoken == [("online-users-many", {"count": 2, "users": "alice and bob"})]


@pytest.mark.asyncio
async def test_main_menu_logout_disconnects(server):
    user = DummyUser("alice")
    server._users = {"alice": user}
    client = SimpleNamespace(username="alice")

    await server._handle_main_menu_selection(user, "logout")

    # Logout now shows a confirmation menu first
    assert user.menus[-1] == "logout_confirm_menu"

    # Confirming "yes" triggers goodbye + disconnect
    await server._handle_logout_confirm_selection(user, "yes")

    assert ("goodbye", {}) in user.spoken
    disconnect = next(pkt for pkt in user.connection.sent if pkt.get("type") == "disconnect")
    assert disconnect["return_to_login"] is True
