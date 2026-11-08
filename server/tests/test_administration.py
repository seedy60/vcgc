"""Tests for the AdministrationMixin and decorators."""

import types
from types import SimpleNamespace

import pytest

from server.core import administration
from server.core.administration import AdministrationMixin, require_admin, require_server_owner
from server.core.users.base import TrustLevel, MenuItem


@pytest.fixture(autouse=True)
def fake_localization(monkeypatch):
    monkeypatch.setattr(
        administration.Localization,
        "get",
        lambda locale, key, **kwargs: f"{key}",
    )


class DummyUser:
    def __init__(self, username: str, trust: TrustLevel):
        self.username = username
        self.locale = "en"
        self.trust_level = trust
        self.spoken = []
        self.sounds = []
        self.menus = []
        self.editboxes = []

    def speak_l(self, message_id: str, **kwargs):
        self.spoken.append((message_id, kwargs))

    def play_sound(self, sound: str):
        self.sounds.append(sound)

    def show_menu(self, menu_id: str, items: list[MenuItem], **kwargs):
        self.menus.append({"menu_id": menu_id, "items": items, "kwargs": kwargs})

    def show_editbox(self, input_id: str, prompt: str, default_value: str = "", **kwargs):
        self.editboxes.append(
            {"input_id": input_id, "prompt": prompt, "default_value": default_value, **kwargs}
        )


class DummyDB:
    def __init__(self):
        self.pending_users: list[str] = []
        self.non_admin_users: list[str] = []
        self.admin_users: list[str] = []

    def get_pending_users(self):
        return [SimpleNamespace(username=name) for name in self.pending_users]

    def get_non_admin_users(self, exclude_banned=True):
        return [SimpleNamespace(username=name) for name in self.non_admin_users]

    def get_admin_users(self, include_server_owner=True):
        users = [SimpleNamespace(username=name) for name in self.admin_users]
        if not include_server_owner and self.admin_users:
            return users
        return users

    def get_user(self, username):
        if username in self.non_admin_users:
            return SimpleNamespace(username=username, trust_level=TrustLevel.USER)
        if username in self.admin_users:
            return SimpleNamespace(username=username, trust_level=TrustLevel.ADMIN)
        return None


class AdminHost(AdministrationMixin):
    def __init__(self, db=None):
        self._db = db or DummyDB()
        self._users = {}
        self._user_states = {}
        self.main_menu_calls = []

    def _show_main_menu(self, user: DummyUser) -> None:
        self.main_menu_calls.append(user.username)


@pytest.mark.asyncio
async def test_require_admin_and_server_owner_decorators():
    host = AdminHost()
    user = DummyUser("regular", TrustLevel.USER)
    owner = DummyUser("owner", TrustLevel.SERVER_OWNER)
    calls = {"admin": 0, "owner": 0}

    class Handler(AdminHost):
        def __init__(self):
            super().__init__()

        @require_admin
        async def admin_action(self, admin):
            calls["admin"] += 1

        @require_server_owner
        async def owner_action(self, owner_user):
            calls["owner"] += 1

    handler = Handler()

    await handler.admin_action(user)
    assert calls["admin"] == 0
    assert user.spoken[0][0] == "not-admin-anymore"
    assert handler.main_menu_calls == ["regular"]

    await handler.admin_action(owner)
    assert calls["admin"] == 1

    admin_user = DummyUser("admin", TrustLevel.ADMIN)
    await handler.owner_action(admin_user)
    assert calls["owner"] == 0
    assert handler.main_menu_calls.count("admin") == 1

    await handler.owner_action(owner)
    assert calls["owner"] == 1


def test_notify_admins_and_exclusions():
    host = AdminHost()
    admin_user = DummyUser("alice", TrustLevel.ADMIN)
    regular_user = DummyUser("bob", TrustLevel.USER)
    owner_user = DummyUser("carol", TrustLevel.SERVER_OWNER)
    host._users = {
        "alice": admin_user,
        "bob": regular_user,
        "carol": owner_user,
    }

    host._notify_admins("alert", "ding", exclude_username="carol")

    assert admin_user.spoken[0][0] == "alert"
    assert admin_user.sounds == ["ding"]
    assert owner_user.spoken == []  # excluded
    assert regular_user.spoken == []  # not an admin


def _get_menu_ids(user: DummyUser) -> list[str]:
    return [item.id for item in user.menus[-1]["items"]]


def test_show_admin_menu_includes_owner_actions():
    host = AdminHost()
    admin_user = DummyUser("admin", TrustLevel.ADMIN)
    owner_user = DummyUser("owner", TrustLevel.SERVER_OWNER)

    host._show_admin_menu(admin_user)
    admin_ids = _get_menu_ids(admin_user)
    assert admin_ids == [
        "account_approval",
        "reset_user_password",
        "ban_user",
        "unban_user",
        "back",
    ]
    assert host._user_states["admin"]["menu"] == "admin_menu"

    host._show_admin_menu(owner_user)
    owner_ids = _get_menu_ids(owner_user)
    assert owner_ids == [
        "account_approval",
        "reset_user_password",
        "ban_user",
        "unban_user",
        "promote_admin",
        "demote_admin",
        "virtual_bots",
        "transfer_ownership",
        "back",
    ]


def test_account_approval_menu_handles_pending_and_empty():
    db = DummyDB()
    host = AdminHost(db=db)
    admin_user = DummyUser("admin", TrustLevel.ADMIN)

    host._show_account_approval_menu(admin_user)
    assert admin_user.spoken[0][0] == "no-pending-accounts"
    assert admin_user.menus[-1]["menu_id"] == "admin_menu"

    db.pending_users = ["alice", "bob"]
    admin_user.spoken.clear()
    host._show_account_approval_menu(admin_user)
    ids = _get_menu_ids(admin_user)
    assert ids == ["pending_alice", "pending_bob", "back"]
    assert host._user_states["admin"]["menu"] == "account_approval_menu"


def test_virtual_bots_menu_shows_status_and_updates_state():
    host = AdminHost()
    owner_user = DummyUser("owner", TrustLevel.SERVER_OWNER)

    class VirtualBotManager:
        def get_status(self):
            return {"online": 2, "total": 4}

    host._virtual_bots = VirtualBotManager()
    host._show_virtual_bots_menu(owner_user)
    items = owner_user.menus[-1]["items"]
    assert items[0].id == "fill"
    assert "(2/4)" in items[0].text
    assert host._user_states["owner"]["menu"] == "virtual_bots_menu"


def test_show_promote_admin_menu_handles_empty_and_entries():
    db = DummyDB()
    host = AdminHost(db=db)
    owner_user = DummyUser("owner", TrustLevel.SERVER_OWNER)

    host._show_promote_admin_menu(owner_user)
    assert owner_user.spoken[-1][0] == "no-users-to-promote"
    assert host._user_states["owner"]["menu"] == "admin_menu"

    db.non_admin_users = ["alice"]
    owner_user.spoken.clear()
    host._show_promote_admin_menu(owner_user)
    assert owner_user.menus[-1]["menu_id"] == "promote_admin_menu"
    assert _get_menu_ids(owner_user) == ["promote_alice", "back"]


def test_show_demote_admin_menu_filters_self_and_empty():
    db = DummyDB()
    host = AdminHost(db=db)
    owner_user = DummyUser("owner", TrustLevel.SERVER_OWNER)

    host._show_demote_admin_menu(owner_user)
    assert owner_user.spoken[-1][0] == "no-admins-to-demote"
    assert host._user_states["owner"]["menu"] == "admin_menu"

    db.admin_users = ["owner", "eve"]
    owner_user.spoken.clear()
    host._show_demote_admin_menu(owner_user)
    assert owner_user.menus[-1]["menu_id"] == "demote_admin_menu"
    assert _get_menu_ids(owner_user) == ["demote_eve", "back"]


def test_reset_password_editbox_is_masked():
    host = AdminHost()
    admin_user = DummyUser("admin", TrustLevel.ADMIN)

    host._show_reset_password_editbox(admin_user, "alice")

    assert admin_user.editboxes[-1]["input_id"] == "reset_user_password"
    assert admin_user.editboxes[-1]["password"] is True
    assert host._user_states["admin"] == {
        "menu": "reset_password_editbox",
        "target_username": "alice",
    }


def test_show_reset_password_user_menu_handles_empty_and_entries():
    db = DummyDB()
    host = AdminHost(db=db)
    admin_user = DummyUser("admin", TrustLevel.ADMIN)

    host._show_reset_password_user_menu(admin_user)
    assert admin_user.spoken[-1][0] == "no-users-to-reset-password"
    assert host._user_states["admin"]["menu"] == "admin_menu"

    db.non_admin_users = ["alice"]
    admin_user.spoken.clear()
    host._show_reset_password_user_menu(admin_user)
    assert admin_user.menus[-1]["menu_id"] == "reset_password_user_menu"
    assert _get_menu_ids(admin_user) == ["reset_password_alice", "back"]


@pytest.mark.asyncio
async def test_handle_account_approval_selection_routes(monkeypatch):
    host = AdminHost()
    admin_user = DummyUser("admin", TrustLevel.ADMIN)
    calls = []

    host._show_admin_menu = types.MethodType(
        lambda self, user: calls.append(("admin", user.username)), host
    )
    host._show_pending_user_actions_menu = types.MethodType(
        lambda self, user, pending: calls.append(("pending", pending)), host
    )

    await host._handle_account_approval_selection(admin_user, "back")
    await host._handle_account_approval_selection(admin_user, "pending_alice")

    assert calls == [("admin", "admin"), ("pending", "alice")]


@pytest.mark.asyncio
async def test_handle_reset_password_user_selection_routes():
    host = AdminHost()
    admin_user = DummyUser("admin", TrustLevel.ADMIN)
    calls = []

    host._show_admin_menu = types.MethodType(
        lambda self, user: calls.append(("admin", user.username)), host
    )
    host._show_reset_password_editbox = types.MethodType(
        lambda self, user, target: calls.append(("editbox", target)), host
    )

    await host._handle_reset_password_user_selection(admin_user, "back")
    await host._handle_reset_password_user_selection(admin_user, "reset_password_alice")

    assert calls == [("admin", "admin"), ("editbox", "alice")]


@pytest.mark.asyncio
async def test_reset_user_password_updates_auth_and_disconnects_online_user():
    db = DummyDB()
    db.non_admin_users = ["alice"]
    host = AdminHost(db=db)
    admin_user = DummyUser("admin", TrustLevel.ADMIN)
    target_user = DummyUser("alice", TrustLevel.USER)
    sent = []

    async def send(payload):
        sent.append(payload)

    target_user.connection = SimpleNamespace(send=send)
    target_user.get_queued_messages = lambda: []
    host._users = {"alice": target_user}
    calls = []
    host._auth = SimpleNamespace(
        reset_password=lambda username, password: calls.append((username, password)) or True
    )

    await host._reset_user_password(admin_user, "alice", "new-secret")

    assert calls == [("alice", "new-secret")]
    assert admin_user.spoken[-1][0] == "reset-user-password-done"
    assert target_user.spoken[-1][0] == "your-password-was-reset"
    assert sent[-1]["type"] == "disconnect"
    assert sent[-1]["return_to_login"] is True


def test_owner_reset_password_menu_includes_admins():
    db = DummyDB()
    db.non_admin_users = ["alice"]
    db.admin_users = ["admin2"]
    host = AdminHost(db=db)
    owner = DummyUser("owner", TrustLevel.SERVER_OWNER)

    host._show_reset_password_user_menu(owner)

    assert owner.menus[-1]["menu_id"] == "reset_password_user_menu"
    assert _get_menu_ids(owner) == [
        "reset_password_admin2",
        "reset_password_alice",
        "back",
    ]


@pytest.mark.asyncio
async def test_reset_user_password_blocks_self_and_peers():
    db = DummyDB()
    db.admin_users = ["admin", "other"]
    host = AdminHost(db=db)
    admin_user = DummyUser("admin", TrustLevel.ADMIN)
    calls = []
    host._auth = SimpleNamespace(
        reset_password=lambda username, password: calls.append((username, password)) or True
    )

    await host._reset_user_password(admin_user, "other", "new-secret")
    await host._reset_user_password(admin_user, "admin", "new-secret")

    assert calls == []
    assert any(message == "reset-user-password-unavailable" for message, _ in admin_user.spoken)


@pytest.mark.asyncio
async def test_owner_can_reset_admin_password():
    db = DummyDB()
    db.admin_users = ["other"]
    host = AdminHost(db=db)
    owner = DummyUser("owner", TrustLevel.SERVER_OWNER)
    calls = []
    host._auth = SimpleNamespace(
        reset_password=lambda username, password: calls.append((username, password)) or True
    )

    await host._reset_user_password(owner, "other", "new-secret")

    assert calls == [("other", "new-secret")]


@pytest.mark.asyncio
async def test_handle_pending_user_actions_selection_paths(monkeypatch):
    host = AdminHost()
    admin_user = DummyUser("admin", TrustLevel.ADMIN)
    approvals = []
    declines = []
    backs = []

    async def fake_approve(self, admin, username):
        approvals.append((admin.username, username))

    host._approve_user = types.MethodType(fake_approve, host)
    host._show_decline_reason_editbox = types.MethodType(
        lambda self, user, target: declines.append(target), host
    )
    host._show_account_approval_menu = types.MethodType(
        lambda self, user: backs.append(user.username), host
    )

    state = {"pending_username": "newbie"}
    await host._handle_pending_user_actions_selection(admin_user, "approve", state)
    await host._handle_pending_user_actions_selection(admin_user, "decline", state)
    await host._handle_pending_user_actions_selection(admin_user, "back", state)
    await host._handle_pending_user_actions_selection(admin_user, "approve", {})

    assert approvals == [("admin", "newbie")]
    assert declines == ["newbie"]
    assert backs == ["admin", "admin"]


@pytest.mark.asyncio
async def test_handle_promote_confirm_selection(monkeypatch):
    host = AdminHost()
    owner_user = DummyUser("owner", TrustLevel.SERVER_OWNER)
    calls = []

    host._show_broadcast_choice_menu = types.MethodType(
        lambda self, user, action, target: calls.append((action, target)), host
    )
    host._show_promote_admin_menu = types.MethodType(
        lambda self, user: calls.append(("menu", user.username)), host
    )

    await host._handle_promote_confirm_selection(owner_user, "yes", {"target_username": "bob"})
    await host._handle_promote_confirm_selection(owner_user, "no", {"target_username": "bob"})
    await host._handle_promote_confirm_selection(owner_user, "yes", {})

    assert calls == [("promote", "bob"), ("menu", "owner"), ("menu", "owner")]


@pytest.mark.asyncio
async def test_handle_broadcast_choice_selection_dispatches(monkeypatch):
    host = AdminHost()
    owner_user = DummyUser("owner", TrustLevel.SERVER_OWNER)
    promotions = []
    bans = []
    fallbacks = []

    async def fake_promote(self, user, target, scope):
        promotions.append((scope, target))

    host._promote_to_admin = types.MethodType(fake_promote, host)
    host._show_ban_reason_editbox = types.MethodType(
        lambda self, user, target, scope: bans.append((target, scope)), host
    )
    host._show_admin_menu = types.MethodType(
        lambda self, user: fallbacks.append(user.username), host
    )

    await host._handle_broadcast_choice_selection(
        owner_user,
        "admins",
        {"action": "promote", "target_username": "alice"},
    )
    await host._handle_broadcast_choice_selection(
        owner_user,
        "all",
        {"action": "ban", "target_username": "eve"},
    )
    await host._handle_broadcast_choice_selection(owner_user, "all", {})

    assert promotions == [("admins", "alice")]
    assert bans == [("eve", "all")]
    assert fallbacks == ["owner"]


@pytest.mark.asyncio
async def test_handle_admin_menu_selection_routes_correctly(monkeypatch):
    host = AdminHost()
    admin_user = DummyUser("admin", TrustLevel.ADMIN)
    called = []

    def tracker(name):
        def _inner(self, user):
            called.append((name, user.username))

        return _inner

    monkeypatch.setattr(
        host,
        "_show_virtual_bots_menu",
        types.MethodType(tracker("virtual"), host),
    )

    await host._handle_admin_menu_selection(admin_user, "virtual_bots")
    assert called == [("virtual", "admin")]
