"""Tests for the NetworkUser implementation."""

from server.core.users.base import EscapeBehavior, MenuItem, TrustLevel
from server.core.users.network_user import NetworkUser
from server.core.users.preferences import UserPreferences


class DummyConnection:
    """Minimal stand-in for a websocket connection."""


def drain_messages(user: NetworkUser) -> list[dict]:
    return user.get_queued_messages()


def test_network_user_show_and_update_menu_tracks_state():
    user = NetworkUser(
        username="alice",
        locale="en",
        connection=DummyConnection(),
        uuid="test-uuid",
        trust_level=TrustLevel.ADMIN,
        preferences=UserPreferences(),
        approved=True,
    )

    items = [
        "Start",
        MenuItem(text="Settings", id="settings", sound="click"),
    ]
    user.show_menu(
        "lobby",
        items,
        multiletter=False,
        escape_behavior=EscapeBehavior.SELECT_LAST,
        position=2,
        grid_enabled=True,
        grid_width=3,
    )
    packet = drain_messages(user)[0]
    assert packet["menu_id"] == "lobby"
    assert packet["items"][1]["id"] == "settings"
    assert packet["position"] == 1  # converted to zero-based
    state = user._current_menus["lobby"]
    assert state["escape_behavior"] == EscapeBehavior.SELECT_LAST.value
    assert state["grid_enabled"] and state["grid_width"] == 3

    user.update_menu("lobby", ["Resume"], position=3, selection_id="resume")
    packet = drain_messages(user)[0]
    assert packet["selection_id"] == "resume"
    assert packet["position"] == 2
    assert user._current_menus["lobby"]["position"] == 3

    user.remove_menu("lobby")
    packet = drain_messages(user)[0]
    assert packet["items"] == []
    assert "lobby" not in user._current_menus


def test_network_user_show_menu_reuses_previous_position_when_not_specified():
    user = NetworkUser(
        username="alice",
        locale="en",
        connection=DummyConnection(),
        uuid="test-uuid",
        trust_level=TrustLevel.USER,
        preferences=UserPreferences(),
        approved=True,
    )

    user.show_menu("options_menu", [MenuItem(text="One", id="one")], position=1)
    drain_messages(user)

    user.show_menu(
        "options_menu",
        [
            MenuItem(text="One", id="one"),
            MenuItem(text="Two", id="two"),
        ],
    )
    packet = drain_messages(user)[0]
    assert packet["position"] == 0  # 1-based stored -> 0-based packet


def test_network_user_audio_and_clear_ui_packets():
    user = NetworkUser("bob", "en", DummyConnection())
    user.show_menu("main", ["Play"])
    user.show_editbox("chat", "Say something", default_value="Hi")
    drain_messages(user)  # discard menu + editbox packets

    user.play_sound("shuffle", volume=80, pan=-10, pitch=120)
    user.play_music("theme", looping=False)
    user.stop_music()
    user.play_ambience("rain", intro="wind", outro="fade")
    user.stop_ambience()
    user.clear_ui()

    packets = drain_messages(user)
    types = [p["type"] for p in packets]
    assert types == [
        "play_sound",
        "play_music",
        "stop_music",
        "play_ambience",
        "stop_ambience",
        "clear_ui",
    ]
    assert user._current_music is None
    assert user._current_menus == {}
    assert user._current_editboxes == {}


def test_network_user_password_editbox_packet():
    user = NetworkUser("bob", "en", DummyConnection())

    user.show_editbox("secret", "Enter password", password=True)

    packet = drain_messages(user)[0]
    assert packet["password"] is True
    assert packet["multiline"] is False
    assert user._current_editboxes["secret"]["password"] is True


def test_network_user_editboxes_and_speak_queue():
    user = NetworkUser("carol", "en", DummyConnection())

    user.speak("hello")
    user.speak("warning", buffer="activity")
    user.show_editbox("input", "Prompt", multiline=True, read_only=True)

    packets = drain_messages(user)
    assert packets[0] == {"type": "speak", "text": "hello"}
    assert packets[1] == {"type": "speak", "text": "warning", "buffer": "activity"}
    edit_packet = packets[2]
    assert edit_packet["type"] == "request_input"
    assert edit_packet["multiline"] and edit_packet["read_only"]
    assert "input" in user._current_editboxes

    user.remove_editbox("input")
    assert "input" not in user._current_editboxes


def test_get_queued_messages_clears_queue_and_locale_change():
    user = NetworkUser("dave", "en", DummyConnection())

    user.speak("queued")
    user.play_sound("tick")

    packets = user.get_queued_messages()
    assert [p["type"] for p in packets] == ["speak", "play_sound"]
    assert user.get_queued_messages() == []

    user.set_locale("pl")
    assert user.locale == "pl"


def test_setters_for_trust_preferences_and_approval():
    user = NetworkUser("eve", "en", DummyConnection(), approved=False)

    user.set_trust_level(TrustLevel.USER)
    user.set_trust_level(TrustLevel.ADMIN)
    assert user.trust_level == TrustLevel.ADMIN

    prefs = UserPreferences(play_turn_sound=False)
    user.set_preferences(prefs)
    assert user.preferences is prefs

    user.set_approved(True)
    assert user.approved is True
