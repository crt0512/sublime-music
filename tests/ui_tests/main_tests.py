from typing import Callable, List
from unittest.mock import Mock

import gi
import pytest

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

from sublime_music.adapters import Result, SearchResult
from sublime_music.config import AppConfiguration
from sublime_music.ui import main
from sublime_music.ui.state import UIState


@pytest.fixture
def window(monkeypatch: pytest.MonkeyPatch):
    if not Gtk.init_check([])[0]:
        pytest.skip("GTK needs a display")
    widget = main.MainWindow()
    monkeypatch.setattr(widget, "_show_search", Mock())
    yield widget
    widget.destroy()
    context = GLib.MainContext.default()
    while context.pending():
        context.iteration(False)


def test_search_debounces_typing_before_starting_work(
    window: main.MainWindow, monkeypatch: pytest.MonkeyPatch
):
    search = Mock(return_value=Result(False))
    monkeypatch.setattr(main.AdapterManager, "search", search)
    window.search_entry.set_text("f")
    window.search_entry.set_text("fo")
    window.search_entry.set_text("foo")
    search.assert_not_called()
    loop = GLib.MainLoop()
    GLib.timeout_add(300, lambda: loop.quit() or False)
    loop.run()
    search.assert_called_once()
    assert search.call_args.args == ("foo",)
    assert not window._search_pending


def test_stale_queued_results_and_completion_do_not_replace_new_search(
    window: main.MainWindow, monkeypatch: pytest.MonkeyPatch
):
    requests: List[Callable[[SearchResult], None]] = []

    def search(
        query: str, search_callback: Callable[[SearchResult], None], before_download: Callable
    ) -> Result[bool]:
        requests.append(search_callback)
        return Result(False)

    monkeypatch.setattr(main.AdapterManager, "search", search)
    render = Mock()
    loading = Mock()
    monkeypatch.setattr(window, "_update_search_results", render)
    monkeypatch.setattr(window, "_set_search_loading", loading)
    window._last_search_query = "first"
    window._start_search("first", window.search_idx)
    requests[0](SearchResult("first"))
    window.search_entry.set_text("second")
    context = GLib.MainContext.default()
    while context.pending():
        context.iteration(False)
    render.assert_not_called()
    loading.assert_not_called()
    assert window._search_pending


def test_clearing_query_cancels_pending_search(
    window: main.MainWindow, monkeypatch: pytest.MonkeyPatch
):
    search = Mock()
    monkeypatch.setattr(main.AdapterManager, "search", search)
    window.search_entry.set_text("foo")
    assert window._search_timeout_id is not None
    window.search_entry.set_text("")
    assert window._search_timeout_id is None
    assert not window._search_pending
    search.assert_not_called()


def test_unchanged_player_settings_keep_existing_widgets(
    window: main.MainWindow, monkeypatch: pytest.MonkeyPatch
):
    config = AppConfiguration()
    state = UIState(current_tab="browse")
    monkeypatch.setattr(AppConfiguration, "state", property(lambda _: state))
    player_manager = Mock()
    player_manager.get_configuration_options.return_value = {"Local Playback": {"Gain": int}}
    config.player_config = {"Local Playback": {"Gain": 5}}
    monkeypatch.setattr(main.AdapterManager, "ground_truth_adapter_is_networked", lambda: False)
    monkeypatch.setattr(window.browse_panel, "update", Mock())
    monkeypatch.setattr(window.player_controls, "update", Mock())
    window.update(config, player_manager)
    children = window.player_settings_box.get_children()
    window.update(config, player_manager)
    assert window.player_settings_box.get_children() == children
    config.player_config["Local Playback"]["Gain"] = 10
    window.update(config, player_manager)
    assert window.player_settings_box.get_children() != children
    assert not window.mpris_raise_switch.get_active()
