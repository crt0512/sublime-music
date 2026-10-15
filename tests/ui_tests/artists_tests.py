import pickle
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import Mock

import gi
import pytest

gi.require_version("Gtk", "3.0")

from gi.repository import GLib, Gtk  # noqa: E402

from sublime_music.adapters import Result  # noqa: E402
from sublime_music.ui import artists  # noqa: E402
from sublime_music.ui.state import UIState  # noqa: E402


def _artist(id_, name, album_count):
    return SimpleNamespace(id=id_, name=name, album_count=album_count)


def _drain_events():
    context = GLib.MainContext.default()
    while context.pending():
        context.iteration(False)


@pytest.fixture
def library(monkeypatch):
    data = [
        _artist("z", "Zulu", 2),
        _artist("a2", "Alpha", 5),
        _artist("b", "beta", None),
        _artist("a1", "Alpha", 5),
        _artist("lower", "alpha", 5),
        _artist("s", "Straße", 0),
        _artist("g", "Gamma", 2),
    ]
    fetch = Mock(side_effect=lambda *a, **kw: Result(list(data)))
    monkeypatch.setattr(artists.AdapterManager, "_get_from_cache_or_ground_truth", fetch)
    monkeypatch.setattr(artists.AdapterManager, "_get_ignored_articles", lambda _: set())

    def submit(fn, *args):
        future = Future()
        future.set_result(fn(*args))
        return future

    # Complete immediately, while still exercising get_artists and async_callback.
    monkeypatch.setattr(artists.AdapterManager, "executor", SimpleNamespace(submit=submit))
    config = SimpleNamespace(
        state=UIState(selected_artist_id="a2"),
        offline_mode=False,
        current_provider_id="source",
    )
    return SimpleNamespace(data=data, fetch=fetch, config=config)


@pytest.fixture
def artist_list(library):
    widget = artists.ArtistList()
    widget.update(app_config=library.config)
    yield widget
    _drain_events()
    widget.destroy()


def _ids(widget):
    return [model.artist_id for model in widget.artists_store]


def _selected_id(widget):
    row = widget.list.get_selected_row()
    return row.get_action_target_value().get_string() if row else None


def _persist(config, updates):
    for key, value in updates.items():
        setattr(config.state, key, value)


@pytest.mark.parametrize(
    "query,expected",
    [
        ("LPH", ["a1", "a2", "lower"]),
        ("STRASSE", ["s"]),
        ("not present", []),
        ("", ["a1", "a2", "lower", "b", "g", "s", "z"]),
    ],
)
def test_search_is_case_insensitive_local_substring(artist_list, library, query, expected):
    assert isinstance(artist_list.filter_entry, Gtk.SearchEntry)
    artist_list.filter_entry.set_text(query)

    assert _ids(artist_list) == expected
    assert library.fetch.call_count == 1
    assert library.config.state.selected_artist_id == "a2"


@pytest.mark.parametrize(
    "sort,expected",
    [
        ("name_asc", ["a1", "a2", "lower", "b", "g", "s", "z"]),
        ("name_desc", ["z", "s", "g", "b", "lower", "a2", "a1"]),
        ("albums_desc", ["a1", "a2", "lower", "g", "z", "b", "s"]),
        ("albums_asc", ["b", "s", "g", "z", "a1", "a2", "lower"]),
    ],
)
def test_sorting_is_deterministic_and_local(artist_list, library, sort, expected):
    assert isinstance(artist_list.sort_combo, Gtk.ComboBoxText)
    artist_list.sort_combo.set_active_id(sort)
    assert _ids(artist_list) == expected
    assert _selected_id(artist_list) == "a2"
    assert library.fetch.call_count == 1

    # Tie-breaking must not depend on the order returned by the adapter.
    library.data.reverse()
    library.config.state.artist_sort = sort
    artist_list.update(app_config=library.config)
    assert _ids(artist_list) == expected


def test_filtering_preserves_navigation_and_restores_highlight(artist_list, library):
    activations = Mock()
    artist_list.list.connect("row-activated", activations)
    assert _selected_id(artist_list) == "a2"

    artist_list.filter_entry.set_text("alpha")
    artist_list.sort_combo.set_active_id("name_desc")
    assert _selected_id(artist_list) == "a2"

    artist_list.filter_entry.set_text("Zulu")
    assert _ids(artist_list) == ["z"]
    assert artist_list.list.get_selected_row() is None
    assert library.config.state.selected_artist_id == "a2"

    artist_list.filter_entry.set_text("")
    assert _selected_id(artist_list) == "a2"
    activations.assert_not_called()
    assert library.fetch.call_count == 1


def test_no_selected_artist_does_not_select_an_arbitrary_row(artist_list, library):
    library.config.state.selected_artist_id = None
    artist_list.update(app_config=library.config)
    artist_list.sort_combo.set_active_id("albums_desc")
    assert artist_list.list.get_selected_row() is None


def test_data_refresh_reapplies_controls_and_updates_album_counts(artist_list, library):
    updates = Mock(side_effect=lambda _, values, force: _persist(library.config, values))
    artist_list.connect("refresh-window", updates)
    artist_list.filter_entry.set_text("a")
    artist_list.sort_combo.set_active_id("albums_desc")
    _drain_events()
    updates.reset_mock()

    library.data[:] = [_artist("new", "Delta", 10), _artist("a2", "Alpha", None)]
    artist_list.refresh_button.clicked()

    assert _ids(artist_list) == ["new", "a2"]
    assert _selected_id(artist_list) == "a2"
    assert artist_list.filter_entry.get_text() == "a"
    assert artist_list.sort_combo.get_active_id() == "albums_desc"
    assert [model.album_count for model in artist_list.artists_store] == [10, 0]
    assert library.fetch.call_count == 2
    assert library.fetch.call_args.kwargs["use_ground_truth_adapter"] is True
    updates.assert_not_called()


def test_state_restoration_does_not_emit_control_changes(library):
    library.config.state.artist_filter_text = "a"
    library.config.state.artist_sort = "albums_asc"
    library.config.state = pickle.loads(pickle.dumps(library.config.state))
    library.config.state.migrate()
    widget = artists.ArtistList()
    updates = Mock()
    widget.connect("refresh-window", updates)
    try:
        widget.update(app_config=library.config)
        _drain_events()
        assert widget.filter_entry.get_text() == "a"
        assert widget.sort_combo.get_active_id() == "albums_asc"
        assert _ids(widget) == ["b", "s", "g", "a1", "a2", "lower"]
        assert _selected_id(widget) == "a2"
        updates.assert_not_called()
    finally:
        widget.destroy()


def test_old_state_migrates_artist_control_defaults():
    old = UIState().__getstate__()
    del old["artist_filter_text"]
    del old["artist_sort"]
    state = UIState.__new__(UIState)
    state.__setstate__(old)
    state.migrate()
    state.migrate()
    assert state.__dict__["artist_filter_text"] == ""
    assert state.__dict__["artist_sort"] == "name_asc"


def test_invalid_restored_sort_falls_back_to_name(library):
    library.config.state.artist_sort = "unknown"
    widget = artists.ArtistList()
    try:
        widget.update(app_config=library.config)
        assert widget.sort_combo.get_active_id() == "name_asc"
        assert _ids(widget) == ["a1", "a2", "lower", "b", "g", "s", "z"]
    finally:
        widget.destroy()


@pytest.mark.parametrize("queued", [False, True])
def test_panel_forwards_changes_without_refetch_or_detail_reload(library, monkeypatch, queued):
    details = Mock()
    monkeypatch.setattr(artists.ArtistDetailPanel, "update", details)
    panel = artists.ArtistsPanel()
    received = []

    def on_refresh(_, values, force):
        received.append((values, force))
        _persist(library.config, values)
        if queued:
            GLib.idle_add(panel.update, library.config, force)
        else:
            panel.update(library.config, force=force)

    panel.connect("refresh-window", on_refresh)
    try:
        panel.update(library.config)
        widget = panel.artist_list
        for query in ("a", "al", "alpha"):
            widget.filter_entry.set_text(query)
        widget.sort_combo.set_active_id("albums_desc")
        _drain_events()

        assert len(received) == 4
        assert received[-1] == (
            {"artist_filter_text": "alpha", "artist_sort": "albums_desc"},
            False,
        )
        assert _ids(widget) == ["a1", "a2", "lower"]
        assert _selected_id(widget) == "a2"
        assert library.config.state.artist_filter_text == "alpha"
        assert library.config.state.artist_sort == "albums_desc"
        assert library.fetch.call_count == 1
        assert details.call_count == 1

        # A real refresh, even within the local-change idle cycle, still fetches.
        widget.filter_entry.set_text("Zulu")
        panel.update(library.config, force=True)
        assert library.fetch.call_count == 2
        assert details.call_count == 2
        _drain_events()

        # Ordinary later updates retain their existing behavior.
        panel.update(library.config)
        assert library.fetch.call_count == 3
        assert details.call_count == 3
    finally:
        _drain_events()
        panel.destroy()


@pytest.mark.parametrize(
    "field,value",
    [
        ("selected_artist_id", "z"),
        ("artist_details_expanded", False),
        ("offline_mode", True),
        ("current_provider_id", "other"),
    ],
)
def test_local_change_does_not_suppress_navigation_or_settings(library, monkeypatch, field, value):
    details = Mock()
    monkeypatch.setattr(artists.ArtistDetailPanel, "update", details)
    panel = artists.ArtistsPanel()
    panel.connect("refresh-window", lambda _, values, force: _persist(library.config, values))
    try:
        panel.update(library.config)
        panel.artist_list.filter_entry.set_text("a")
        target = library.config if hasattr(library.config, field) else library.config.state
        setattr(target, field, value)
        panel.update(library.config)
        assert library.fetch.call_count == 2
        assert details.call_count == 2
    finally:
        _drain_events()
        panel.destroy()


def test_controls_changed_while_fetch_pending_are_applied_on_arrival(library, monkeypatch):
    pending = Future()
    monkeypatch.setattr(
        artists.AdapterManager, "executor", SimpleNamespace(submit=lambda *a: pending)
    )
    widget = artists.ArtistList()
    widget.connect("refresh-window", lambda _, values, force: _persist(library.config, values))
    try:
        widget.update(app_config=library.config)
        widget.filter_entry.set_text("alpha")
        widget.sort_combo.set_active_id("name_desc")
        pending.set_result(library.data)
        _drain_events()
        assert _ids(widget) == ["lower", "a2", "a1"]
        assert _selected_id(widget) == "a2"
        assert not widget.loading_indicator.get_visible()
    finally:
        _drain_events()
        widget.destroy()
