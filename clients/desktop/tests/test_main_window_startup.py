import types

import pytest

from ui import main_window as main_mod


class DummySoundManager:
    def __init__(self):
        self.play_calls = []
        self.music_calls = []
        self.stop_calls = []

    def play(self, name, volume=1.0):
        self.play_calls.append((name, volume))

    def music(self, name):
        self.music_calls.append(name)

    def stop_music(self, fade=False):
        self.stop_calls.append(fade)

    def set_music_volume(self, *_args, **_kwargs):
        pass

    def set_ambience_volume(self, *_args, **_kwargs):
        pass


class DummyNetworkManager:
    def __init__(self, should_connect=True):
        self.calls = []
        self.should_connect = should_connect
        self.disconnect_calls = []
        self.connect_calls = 0

    def connect(self, url, username, password, refresh_token=None, refresh_expires_at=None):
        self.calls.append((url, username, password, refresh_token, refresh_expires_at))
        self.connect_calls += 1
        return self.should_connect

    def disconnect(self, wait=False):
        self.disconnect_calls.append(wait)


class DummyCallLater:
    def __init__(self, delay, callback):
        self.delay = delay
        self.callback = callback
        self.stopped = False

    def Stop(self):
        self.stopped = True


@pytest.fixture(autouse=True)
def stub_call_later(monkeypatch):
    created = []

    def fake_call_later(delay, callback, *args, **kwargs):
        call = DummyCallLater(delay, lambda: callback(*args, **kwargs))
        created.append(call)
        return call

    monkeypatch.setattr(main_mod.wx, "CallLater", fake_call_later)
    yield created


def make_window_stub(monkeypatch, should_connect=True):
    window = main_mod.MainWindow.__new__(main_mod.MainWindow)
    window.credentials = {
        "username": "alice",
        "password": "secret",
        "server_url": "wss://demo.example:443",
    }
    window.server_id = "srv-1"
    window.config_manager = types.SimpleNamespace(
        get_client_options=lambda server_id: {"audio": {"music_volume": 40, "ambience_volume": 20}}
    )
    window.sound_manager = DummySoundManager()
    window.network = DummyNetworkManager(should_connect=should_connect)
    window.expecting_reconnect = False
    window.returning_to_login = False
    window.connection_timeout_timer = None
    window.connected = False
    window.client_options = {}
    window.reconnect_attempts = 0
    window.max_reconnect_attempts = 30
    window._apply_client_audio_options = lambda: None
    window.add_history_calls = []
    window.add_history = lambda text, buffer="misc", *_: window.add_history_calls.append(
        (text, buffer)
    )
    window._show_connection_error_calls = []
    window._show_connection_error = lambda message, return_to_login=False: (
        window._show_connection_error_calls.append(message)
    )
    window.Close = lambda: None
    return window


def test_auto_connect_uses_credentials(monkeypatch, stub_call_later):
    window = make_window_stub(monkeypatch, should_connect=True)

    window._auto_connect()

    assert window.sound_manager.music_calls == ["connectloop.ogg"]
    assert window.add_history_calls[0][0].startswith("Connecting to wss://demo.example:443")
    assert ("wss://demo.example:443", "alice", "secret", None, None) in window.network.calls
    assert stub_call_later, "CallLater should schedule timeout"
    assert stub_call_later[0].delay == 10000


def test_auto_connect_reports_failure(monkeypatch, stub_call_later):
    window = make_window_stub(monkeypatch, should_connect=False)

    window._auto_connect()

    assert window.network.calls
    assert not stub_call_later, "No timeout should be scheduled on failure"
    assert window._show_connection_error_calls == ["Failed to start connection to server."]


def test_check_connection_timeout_triggers_error(monkeypatch):
    window = make_window_stub(monkeypatch, should_connect=True)
    window.connection_timeout_timer = None
    window.expecting_reconnect = False
    window.returning_to_login = False
    window.connected = False

    window._check_connection_timeout()

    assert window._show_connection_error_calls == [
        "Connection timeout: Could not connect to server."
    ]


def test_on_server_disconnect_reconnect_flow(monkeypatch, stub_call_later):
    window = make_window_stub(monkeypatch, should_connect=True)
    window.connected = False
    window.speaker = types.SimpleNamespace(speak=lambda *args, **kwargs: None)
    reconnect_calls = []
    monkeypatch.setattr(
        main_mod.wx,
        "CallLater",
        lambda delay, func, *args, **kwargs: reconnect_calls.append((delay, func, args, kwargs)),
    )

    window.on_server_disconnect({"reconnect": True})

    assert reconnect_calls, "Reconnection callbacks should be scheduled"
    delays = [call[0] for call in reconnect_calls]
    assert 3000 in delays, "Initial reconnect delay expected"


def test_on_server_disconnect_status_mode(monkeypatch):
    window = make_window_stub(monkeypatch, should_connect=True)
    window.connected = False
    window.speaker = types.SimpleNamespace(speak=lambda *args, **kwargs: None)
    window.last_server_status_packet = {
        "message": "Maintenance in progress.",
        "resume_at": "2026-02-07T18:00:00Z",
    }
    reconnect_calls = []
    monkeypatch.setattr(
        main_mod.wx,
        "CallLater",
        lambda delay, func, *args, **kwargs: reconnect_calls.append((delay, func, args, kwargs)),
    )

    window.on_server_disconnect({"status_mode": "maintenance", "retry_after": 10})

    assert window._show_connection_error_calls[-1].startswith("Maintenance in progress")
    assert reconnect_calls
    assert reconnect_calls[0][0] == 10000


def test_on_server_disconnect_logout_returns_to_login(monkeypatch):
    window = make_window_stub(monkeypatch, should_connect=True)
    window.returning_to_login = False
    calls = []
    window._return_to_login = lambda: calls.append(window.returning_to_login)

    window.on_server_disconnect({"return_to_login": True})

    assert calls == [True]
    assert window._show_connection_error_calls == []
    assert window.sound_manager.stop_calls == [False]


def test_on_server_disconnect_uses_packet_message(monkeypatch):
    window = make_window_stub(monkeypatch, should_connect=True)
    window.connected = False
    window.speaker = types.SimpleNamespace(speak=lambda *args, **kwargs: None)

    window.on_server_disconnect({"show_message": True, "message": "Please contact support."})

    assert window._show_connection_error_calls[-1] == "Please contact support."


def test_on_server_disconnect_falls_back_to_last_server_message(monkeypatch):
    window = make_window_stub(monkeypatch, should_connect=True)
    window.connected = False
    window.speaker = types.SimpleNamespace(speak=lambda *args, **kwargs: None)
    window.last_server_message = "Account banned."

    window.on_server_disconnect({"show_message": True})

    assert window._show_connection_error_calls[-1] == "Account banned."


def test_do_reconnect_resets_after_max_attempts(monkeypatch):
    window = make_window_stub(monkeypatch, should_connect=False)
    window.speaker = types.SimpleNamespace(speak=lambda *args, **kwargs: None)
    window.expecting_reconnect = True

    for _ in range(window.max_reconnect_attempts + 1):
        window._do_reconnect("ws://demo", "alice", "secret")

    assert window.expecting_reconnect is False
    assert window.reconnect_attempts == 0


def test_do_reconnect_schedules_followup_when_connecting(monkeypatch, stub_call_later):
    window = make_window_stub(monkeypatch, should_connect=True)
    window.expecting_reconnect = True
    window.speaker = types.SimpleNamespace(speak=lambda *args, **kwargs: None)

    window._do_reconnect("ws://demo", "alice", "secret")

    assert window.network.connect_calls == 1
    assert stub_call_later[-1].delay == 3000


def test_do_reconnect_noop_when_already_connected(monkeypatch, stub_call_later):
    window = make_window_stub(monkeypatch, should_connect=True)
    window.connected = True
    window.expecting_reconnect = True
    window.speaker = types.SimpleNamespace(speak=lambda *args, **kwargs: None)

    window._do_reconnect("ws://demo", "alice", "secret")

    assert window.expecting_reconnect is False
    assert window.reconnect_attempts == 0
    assert not window.network.calls
