"""Tests for the MockUser utility used in games."""

from server.core.users.base import MenuItem, EscapeBehavior
from server.core.users.test_user import MockUser


def test_mock_user_captures_messages_and_ui_state():
    user = MockUser("Tester", locale="es", approved=False)

    user.speak("hola", buffer="activity")
    user.play_sound("ding", volume=80, pan=-5, pitch=110)
    user.play_music("theme", looping=False)
    user.stop_music()
    user.play_ambience("rain")
    user.stop_ambience()

    spoken = user.get_spoken_messages()
    assert spoken == ["hola"]
    assert user.get_last_spoken() == "hola"
    assert user.get_sounds_played() == ["ding"]

    assert user.approved is False
    assert user.locale == "es"
    assert isinstance(user.uuid, str)


def test_mock_user_menu_and_editbox_updates():
    user = MockUser("Menus")
    items = ["Play", MenuItem("Settings", id="settings")]

    user.show_menu(
        "main",
        items,
        multiletter=False,
        escape_behavior=EscapeBehavior.SELECT_LAST,
        position=2,
    )
    assert user.get_current_menu_items("main") == items
    user.update_menu("main", ["Resume"], position=1, selection_id="resume")
    assert user.menus["main"]["position"] == 1
    user.remove_menu("main")
    assert "main" not in user.menus

    user.show_editbox("chat", "Say something", default_value="Hi", multiline=True)
    assert "chat" in user.editboxes
    user.remove_editbox("chat")
    assert "chat" not in user.editboxes

    user.show_editbox("secret", "Password", password=True)
    assert user.editboxes["secret"]["password"] is True

    user.clear_ui()
    assert not user.menus and not user.editboxes
    user.clear_messages()
    assert user.messages == []
