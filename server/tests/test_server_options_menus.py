"""Tests for options and language/dice style menu handlers in core.server."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from server.core.server import Server
from server.core.users.preferences import UserPreferences, DiceKeepingStyle
from server.messages.localization import Localization


class DummyDB:
    def __init__(self):
        self.preferences_updates: list[tuple[str, str]] = []
        self.locale_updates: list[tuple[str, str]] = []
        self.fluent_languages_updates: list[tuple[str, list[str]]] = []
        self.password_updates: list[tuple[str, str]] = []
        self.revoked_tokens: list[tuple[str, int]] = []

    def get_user(self, username: str):
        return SimpleNamespace(username=username, password_hash="current-hash")

    def update_user_password(self, username: str, password_hash: str) -> None:
        self.password_updates.append((username, password_hash))

    def revoke_user_refresh_tokens(self, username: str, revoked_at: int) -> None:
        self.revoked_tokens.append((username, revoked_at))

    def update_user_preferences(self, username: str, prefs_json: str) -> None:
        self.preferences_updates.append((username, prefs_json))

    def update_user_locale(self, username: str, locale: str) -> None:
        self.locale_updates.append((username, locale))

    def set_user_fluent_languages(self, username: str, languages: list[str]) -> None:
        self.fluent_languages_updates.append((username, list(languages)))


class DummyUser:
    def __init__(self, username: str, locale: str = "en"):
        self.username = username
        self.locale = locale
        self.preferences = UserPreferences()
        self.fluent_languages: list[str] = []
        self.spoken: list[tuple[str, dict]] = []
        self.menu_id: str | None = None
        self.music_played: list[str] = []
        self.sounds_played: list[str] = []
        self.editboxes: list[dict] = []
        self.menu_items: list[str | None] = []

        async def _send(payload):
            return None

        self.connection = SimpleNamespace(send=_send)

    def speak_l(self, message_id: str, buffer: str = "misc", **kwargs) -> None:
        self.spoken.append((message_id, kwargs))

    def speak(self, text: str, buffer: str = "misc") -> None:
        self.spoken.append((text, {"buffer": buffer}))

    def show_menu(self, menu_id: str, items: list, **kwargs) -> None:
        self.menu_id = menu_id
        self.menu_items = [getattr(item, "id", item) for item in items]

    def play_music(self, name: str, looping: bool = True) -> None:
        self.music_played.append(name)

    def play_sound(self, name: str, volume: int = 100, pan: int = 0, pitch: int = 100) -> None:
        self.sounds_played.append(name)

    def set_locale(self, locale: str) -> None:
        self.locale = locale

    def show_editbox(self, input_id: str, prompt: str, default_value: str = "", **kwargs) -> None:
        self.editboxes.append(
            {"input_id": input_id, "prompt": prompt, "default_value": default_value, **kwargs}
        )


@pytest.fixture
def server(tmp_path, monkeypatch):
    srv = Server(host="127.0.0.1", port=0, db_path=tmp_path / "db.sqlite", preload_locales=True)
    srv._db = DummyDB()
    # simplify localization helpers
    monkeypatch.setattr(
        "server.messages.localization.Localization.get",
        lambda _locale, key, **kwargs: f"{key}:{kwargs}" if kwargs else key,
    )
    monkeypatch.setattr(
        "server.messages.localization.Localization.get_available_languages",
        lambda _locale="en", fallback="en": {"en": "English", "es": "Español"},
    )
    monkeypatch.setattr(
        "server.messages.localization.Localization.get_available_locale_codes",
        lambda: ["en", "es"],
    )
    return srv


@pytest.mark.asyncio
async def test_handle_pref_category_toggle_turn_sound(server, monkeypatch):
    user = DummyUser("alice")
    user.preferences.play_turn_sound = True
    shown = {}
    monkeypatch.setattr(
        server, "_show_pref_category_menu", lambda u, c, **kw: shown.setdefault("called", True)
    )
    server._user_states["alice"] = {"menu": "pref_category_menu", "pref_category": "sounds"}

    await server._handle_pref_category_selection(user, "pref_play_turn_sound")

    assert user.preferences.play_turn_sound is False
    assert user.sounds_played[-1] == "checkbox_list_off.wav"
    assert server._db.preferences_updates  # saved
    assert shown.get("called")


@pytest.mark.asyncio
async def test_handle_options_selection_language_opens_menu(server, monkeypatch):
    user = DummyUser("alice")
    opened = {}
    monkeypatch.setattr(
        "server.core.ui.common_flows.show_language_menu",
        lambda *a, **kw: opened.setdefault("lang", True),
    )

    await server._handle_options_selection(user, "language")

    assert opened.get("lang")


@pytest.mark.asyncio
async def test_handle_options_selection_language_warmup_in_progress(server, monkeypatch):
    user = DummyUser("alice")
    Localization.set_warmup_active(True)

    try:
        await server._handle_options_selection(user, "language")
    finally:
        Localization.set_warmup_active(False)

    # User stays on the options menu (no re-show), only hears the message
    assert user.spoken[-1][0] == "localization-in-progress-try-again"


@pytest.mark.asyncio
async def test_handle_dice_style_selection_changes_pref(server, monkeypatch):
    user = DummyUser("alice")
    user.preferences.dice_keeping_style = DiceKeepingStyle.PLAYPALACE
    shown = {}
    monkeypatch.setattr(server, "_show_options_menu", lambda u: shown.setdefault("options", True))
    monkeypatch.setattr(server, "_save_user_preferences", lambda u: shown.setdefault("saved", True))

    await server._handle_dice_keeping_style_selection(user, "style_quentin_c")

    assert user.preferences.dice_keeping_style == DiceKeepingStyle.QUENTIN_C
    assert shown.get("saved")
    assert shown.get("options")
    assert ("dice-keeping-style-changed", {"style": "dice-keeping-style-values"}) in user.spoken


@pytest.mark.asyncio
async def test_handle_language_selection_updates_locale(server, monkeypatch):
    user = DummyUser("alice")
    shown = {}
    monkeypatch.setattr(server, "_show_options_menu", lambda u: shown.setdefault("options", True))

    await server._apply_locale_change(user, "es")

    assert user.locale == "es"
    assert server._db.locale_updates == [("alice", "es")]
    assert ("language-changed", {"language": "Español"}) in user.spoken
    assert shown.get("options")


def test_show_language_menu_warmup_in_progress(server, monkeypatch):
    from server.core.ui.common_flows import show_language_menu

    user = DummyUser("alice")
    Localization.set_warmup_active(True)

    try:
        result = show_language_menu(user)
    finally:
        Localization.set_warmup_active(False)

    assert result is False
    assert user.spoken[-1][0] == "localization-in-progress-try-again"


def test_options_menu_includes_change_password_option(server):
    user = DummyUser("alice")

    server._show_options_menu(user)

    assert user.menu_id == "options_menu"
    assert "change_password" in user.menu_items


def _install_change_password_auth(server, invalidated: list[str]) -> None:
    valid = {("current-secret", "current-hash"), ("new-secret", "new-hash")}
    server._auth = SimpleNamespace(
        verify_password=lambda password, password_hash: (password, password_hash) in valid,
        hash_password=lambda password: "new-hash" if password == "new-secret" else "other-hash",
        invalidate_user_sessions=lambda username: invalidated.append(username),
    )


@pytest.mark.asyncio
async def test_change_password_flow_updates_hash_and_revokes_tokens(server):
    user = DummyUser("alice")
    invalidated: list[str] = []
    _install_change_password_auth(server, invalidated)

    await server._handle_options_selection(user, "change_password")
    assert user.editboxes[-1]["input_id"] == "change_password_current"
    assert user.editboxes[-1]["password"] is True
    assert server._user_states["alice"]["menu"] == "change_password_current_editbox"

    server._handle_change_password_current(user, "current-secret")
    assert user.editboxes[-1]["input_id"] == "change_password_new"
    assert user.editboxes[-1]["password"] is True

    server._handle_change_password_new(user, "new-secret")
    assert user.editboxes[-1]["input_id"] == "change_password_confirm"
    assert user.editboxes[-1]["password"] is True

    server._handle_change_password_confirm(user, "new-secret", server._user_states["alice"])

    assert server._db.password_updates == [("alice", "new-hash")]
    assert invalidated == ["alice"]
    assert server._db.revoked_tokens
    assert user.spoken[-1][0] == "change-password-success"
    assert server._user_states["alice"]["menu"] == "options_menu"


@pytest.mark.asyncio
async def test_change_password_rejects_wrong_current_and_mismatch(server):
    user = DummyUser("alice")
    invalidated: list[str] = []
    _install_change_password_auth(server, invalidated)

    await server._handle_options_selection(user, "change_password")
    server._handle_change_password_current(user, "wrong-password")

    assert user.spoken[-1][0] == "change-password-wrong-current"
    assert user.editboxes[-1]["input_id"] == "change_password_current"

    server._handle_change_password_current(user, "current-secret")
    server._handle_change_password_new(user, "new-secret")
    server._handle_change_password_confirm(user, "not-the-same", server._user_states["alice"])

    assert user.spoken[-1][0] == "change-password-mismatch"
    assert user.editboxes[-1]["input_id"] == "change_password_confirm"
    assert server._db.password_updates == []


@pytest.mark.asyncio
async def test_change_password_enforces_length_limits(server):
    user = DummyUser("alice")
    invalidated: list[str] = []
    _install_change_password_auth(server, invalidated)
    server._password_min_length = 8

    await server._handle_options_selection(user, "change_password")
    server._handle_change_password_current(user, "current-secret")
    server._handle_change_password_new(user, "short")

    assert user.spoken[-1][0] == "credential-password-length"
    assert user.editboxes[-1]["input_id"] == "change_password_new"
    assert server._db.password_updates == []


@pytest.mark.asyncio
async def test_fluent_languages_warmup_in_progress(server, monkeypatch):
    user = DummyUser("alice")
    Localization.set_warmup_active(True)

    try:
        await server._handle_options_selection(user, "fluent_languages")
    finally:
        Localization.set_warmup_active(False)

    # User stays on the options menu (no re-show), only hears the message
    assert user.spoken[-1][0] == "localization-in-progress-try-again"


@pytest.mark.asyncio
async def test_fluent_languages_opens_language_menu(server, monkeypatch):
    user = DummyUser("alice")
    opened = {}
    monkeypatch.setattr(
        "server.core.ui.common_flows.show_language_menu",
        lambda *a, **kw: opened.setdefault("lang", True),
    )

    await server._handle_options_selection(user, "fluent_languages")

    assert opened.get("lang")
    assert server._user_states["alice"]["menu"] == "language_menu"


@pytest.mark.asyncio
async def test_fluent_language_toggle_deferred(server, monkeypatch):
    """Toggling a language plays a sound but does not touch user state or DB."""
    from server.core.ui.common_flows import handle_language_menu_selection

    user = DummyUser("alice")
    user.fluent_languages = ["en"]
    server._show_fluent_languages_menu(user)

    # Toggle es on — only the menu's internal selected set changes
    await handle_language_menu_selection(user, "lang_es")

    assert user.sounds_played[-1] == "checkbox_list_on.wav"
    assert user.fluent_languages == ["en"]  # unchanged
    assert server._db.fluent_languages_updates == []

    # Toggle es off again
    await handle_language_menu_selection(user, "lang_es")

    assert user.sounds_played[-1] == "checkbox_list_off.wav"
    assert server._db.fluent_languages_updates == []


@pytest.mark.asyncio
async def test_fluent_language_done_saves(server, monkeypatch):
    """Pressing Done applies the selected set and writes to the DB."""
    from server.core.ui.common_flows import handle_language_menu_selection

    user = DummyUser("alice")
    user.fluent_languages = ["en"]
    shown = {}
    monkeypatch.setattr(server, "_show_options_menu", lambda u: shown.setdefault("options", True))
    server._show_fluent_languages_menu(user)

    # Toggle es on, then press done
    await handle_language_menu_selection(user, "lang_es")
    await handle_language_menu_selection(user, "done")

    assert set(user.fluent_languages) == {"en", "es"}
    assert len(server._db.fluent_languages_updates) == 1
    assert shown.get("options")


@pytest.mark.asyncio
async def test_fluent_language_cancel_reverts(server, monkeypatch):
    """Pressing Cancel restores original fluent languages, nothing written."""
    from server.core.ui.common_flows import handle_language_menu_selection

    user = DummyUser("alice")
    user.fluent_languages = ["en"]
    shown = {}
    monkeypatch.setattr(server, "_show_options_menu", lambda u: shown.setdefault("options", True))
    server._show_fluent_languages_menu(user)

    # Toggle es on, then cancel
    await handle_language_menu_selection(user, "lang_es")
    await handle_language_menu_selection(user, "cancel")

    assert user.fluent_languages == ["en"]  # unchanged
    assert server._db.fluent_languages_updates == []  # nothing written
    assert shown.get("options")
