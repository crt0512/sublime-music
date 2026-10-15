import pickle
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

import gi
import pytest

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")

from gi.repository import Gdk, Gtk  # noqa: E402

from sublime_music.adapters import SongQuery  # noqa: E402
from sublime_music.ui import songs  # noqa: E402
from sublime_music.ui.state import UIState  # noqa: E402


@pytest.fixture
def panel():
    if not Gtk.init_check([])[0]:
        pytest.skip("GTK needs a display")
    widget = songs.SongsPanel()
    widget._updating = True
    widget._apply_query_to_widgets()
    widget._updating = False

    # Match the app's state round-trip without fetching a library.
    def update(_, changes, force):
        widget.query = changes["song_query"]
        widget._updating = True
        widget._apply_query_to_widgets()
        widget._updating = False

    widget.connect("refresh-window", update)
    yield widget
    widget.destroy()


def test_rating_filter_controls_emit_combined_query(panel):
    panel.query = replace(panel.query, filter_text="Rock", filter_column="genre")
    panel.min_rating_combo.set_active_id("3")
    panel.max_rating_combo.set_active_id("4")
    assert (panel.query.min_rating, panel.query.max_rating) == (3, 4)
    assert (panel.query.filter_text, panel.query.filter_column) == ("Rock", "genre")
    panel.min_rating_combo.set_active_id("any")
    panel.max_rating_combo.set_active_id("0")
    assert (panel.query.min_rating, panel.query.max_rating) == (None, 0)


def test_rating_filter_controls_keep_bounds_consistent(panel):
    panel.max_rating_combo.set_active_id("2")
    panel.min_rating_combo.set_active_id("4")
    assert (panel.query.min_rating, panel.query.max_rating) == (4, 4)
    assert panel.max_rating_combo.get_active_id() == "4"
    panel.max_rating_combo.set_active_id("1")
    assert (panel.query.min_rating, panel.query.max_rating) == (1, 1)
    assert panel.min_rating_combo.get_active_id() == "1"


def test_randomize_preserves_filters_and_column_sort_exits_random_mode(panel, monkeypatch):
    seeds = iter((123, 456))
    monkeypatch.setattr(songs, "getrandbits", lambda _: next(seeds))
    panel.query = replace(panel.query, min_rating=4, filter_text="favorite")
    panel.randomize_button.clicked()
    assert panel.query.sort_column == "random"
    assert panel.query.random_seed == 123
    assert panel.query.min_rating == 4 and panel.query.filter_text == "favorite"
    assert not any(column.get_sort_indicator() for column in panel.columns.values())
    panel.randomize_button.clicked()
    assert panel.query.random_seed == 456
    panel.columns["title"].clicked()
    assert panel.query.sort_column == "title"
    assert panel.columns["title"].get_sort_indicator()


def test_restoring_query_does_not_emit_changes(panel):
    changed = Mock()
    panel.connect("refresh-window", changed)
    panel.query = SongQuery(min_rating=2, max_rating=5, sort_column="random", random_seed=999)
    panel._updating = True
    panel._apply_query_to_widgets()
    panel._updating = False
    assert panel.min_rating_combo.get_active_id() == "2"
    assert panel.max_rating_combo.get_active_id() == "5"
    changed.assert_not_called()


def test_rating_and_random_order_survive_state_pickle():
    state = UIState()
    state.song_query = SongQuery(min_rating=2, max_rating=4, sort_column="random", random_seed=42)
    restored = pickle.loads(pickle.dumps(state))
    restored.migrate()
    assert restored.song_query == state.song_query


def test_query_from_before_rating_filters_uses_defaults():
    query = SongQuery()
    for name in ("min_rating", "max_rating", "random_seed"):
        del query.__dict__[name]
    restored = pickle.loads(pickle.dumps(query))
    assert restored.min_rating is None and restored.max_rating is None
    assert restored.random_seed == 0
    assert replace(restored, min_rating=3).min_rating == 3


@pytest.mark.parametrize("preset", [SongQuery.Preset.ALL, SongQuery.Preset.YEAR_RANGE])
def test_song_controls_fit_split_window(panel, preset):
    settings = Gtk.Settings.get_default()
    old_theme, old_font = settings.props.gtk_theme_name, settings.props.gtk_font_name
    settings.props.gtk_theme_name, settings.props.gtk_font_name = "Adwaita", "Sans 10"
    css = Gtk.CssProvider()
    css.load_from_path(str(Path(songs.__file__).with_name("app_styles.css")))
    screen = Gdk.Screen.get_default()
    Gtk.StyleContext.add_provider_for_screen(screen, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    try:
        panel.show_all()
        panel.query = SongQuery(preset=preset, min_rating=1, max_rating=5)
        panel._updating = True
        panel._apply_query_to_widgets()
        panel._updating = False
        panel.song_count_label.set_text("100,000 songs")
        panel._update_last_synced_label(datetime(2026, 10, 15, 12, 30))
        assert panel.get_preferred_width().minimum_width <= 720
    finally:
        Gtk.StyleContext.remove_provider_for_screen(screen, css)
        settings.props.gtk_theme_name, settings.props.gtk_font_name = old_theme, old_font
