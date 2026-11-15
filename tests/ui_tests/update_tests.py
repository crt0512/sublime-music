from types import SimpleNamespace
from unittest.mock import Mock

import gi
import pytest

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")

from sublime_music import app
from sublime_music.adapters import Result
from sublime_music.config import AppConfiguration
from sublime_music.dbus import DBusManager


def test_window_updates_are_coalesced_without_losing_force(monkeypatch: pytest.MonkeyPatch):
    callbacks = []
    monkeypatch.setattr(app.GLib, "idle_add", lambda fn: callbacks.append(fn))
    application = Mock(
        window=SimpleNamespace(update=Mock()),
        app_config=object(),
        player_manager=object(),
        update_macos_media_session=Mock(),
        _window_update_pending=False,
        _window_update_force=False,
    )
    for force in (False, True, False):
        app.SublimeMusicApp.update_window(application, force=force)
    assert len(callbacks) == 1
    assert callbacks[0]() is False
    application.window.update.assert_called_once_with(
        application.app_config, application.player_manager, force=True
    )
    assert not application._window_update_pending


def test_mpris_raise_checks_live_setting():
    application = Mock(
        app_config=SimpleNamespace(mpris_allow_raise=False),
        window=SimpleNamespace(present=Mock()),
    )
    app.SublimeMusicApp.on_mpris_raise(application)
    application.window.present.assert_not_called()
    application.app_config.mpris_allow_raise = True
    app.SublimeMusicApp.on_mpris_raise(application)
    application.window.present.assert_called_once()


def test_mpris_advertises_raise_only_when_enabled(monkeypatch: pytest.MonkeyPatch):
    config = AppConfiguration()
    manager = Mock(second_microsecond_conversion=1_000_000)
    manager.get_config_and_player_manager.return_value = (config, Mock(song_loaded=False))
    monkeypatch.setattr(app.AdapterManager, "get_playlists", lambda **kwargs: Result([]))
    for enabled in (False, True, False):
        config.mpris_allow_raise = enabled
        assert DBusManager.property_dict(manager)["org.mpris.MediaPlayer2"]["CanRaise"] is enabled


def test_programmatic_tab_change_does_not_queue_duplicate_update():
    application = Mock(
        app_config=SimpleNamespace(state=SimpleNamespace(current_tab="albums")),
        update_window=Mock(),
    )
    stack = SimpleNamespace(get_visible_child_name=lambda: "albums")
    app.SublimeMusicApp.on_stack_change(application, stack, None)
    application.update_window.assert_not_called()
