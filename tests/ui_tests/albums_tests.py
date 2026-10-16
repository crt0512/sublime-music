"""Real FlowBox/ListStore regressions; no server or artwork downloads required."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import gi
import pytest

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")

from gi.repository import Gdk, GdkPixbuf, GLib, GObject, Gtk  # noqa: E402

from sublime_music.adapters import AlbumSearchQuery, Result  # noqa: E402
from sublime_music.ui import albums  # noqa: E402


class AlbumDetail(Gtk.Box):
    __gsignals__ = {
        "song-clicked": (GObject.SignalFlags.RUN_FIRST, None, (int, object, object)),
        "song-selected": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "album-starred": (GObject.SignalFlags.RUN_FIRST, None, (bool,)),
    }

    def __init__(self, album, **kwargs):
        super().__init__()
        self.album = album
        self.artwork = Gtk.DrawingArea(width_request=300, height_request=300)
        self.add(self.artwork)

    def update(self, **kwargs):
        pass


def _drain_events():
    context = GLib.MainContext.default()
    for _ in range(1000):
        if not context.pending():
            return
        context.iteration(False)
    pytest.fail("GTK event queue did not settle")


def _wait_for(predicate):
    """Let GTK's frame clock allocate real widgets, with a bounded main loop."""
    loop = GLib.MainLoop()

    def check():
        if predicate():
            loop.quit()
            return False
        return True

    check_id = GLib.timeout_add(5, check)
    timeout_id = GLib.timeout_add(3000, lambda: loop.quit() or False)
    loop.run()
    if predicate():
        GLib.source_remove(timeout_id)
    else:
        GLib.source_remove(check_id)
        pytest.fail("GTK layout did not settle")


@pytest.fixture
def library(monkeypatch):
    if not Gtk.init_check([])[0]:
        pytest.skip("GTK needs a display")
    settings = Gtk.Settings.get_default()
    old_animations = settings.props.gtk_enable_animations
    settings.props.gtk_enable_animations = False
    css = Gtk.CssProvider()
    css.load_from_path(str(Path(albums.__file__).with_name("app_styles.css")))
    screen = Gdk.Screen.get_default()
    Gtk.StyleContext.add_provider_for_screen(screen, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    data = [
        SimpleNamespace(
            id=str(i), name=f"Album {i:03}", artist=None, year=2000, starred=None, cover_art=None
        )
        for i in range(400)
    ]
    monkeypatch.setattr(albums.AdapterManager, "can_get_artists", lambda: True)
    monkeypatch.setattr(albums.AdapterManager, "can_get_genres", lambda: False)
    monkeypatch.setattr(
        albums.AdapterManager,
        "get_supported_artist_query_types",
        lambda: tuple(AlbumSearchQuery.Type),
    )
    monkeypatch.setattr(albums.AdapterManager, "get_albums", lambda *a, **kw: Result(data))
    monkeypatch.setattr(albums.AdapterManager, "get_cover_art_uri", lambda *a: Result(""))
    monkeypatch.setattr(albums.SpinnerImage, "set_from_file_async", Mock())
    monkeypatch.setattr(albums, "AlbumWithSongs", AlbumDetail)
    config = SimpleNamespace(
        state=SimpleNamespace(
            selected_album_id=None,
            current_song=None,
            current_album_search_query=AlbumSearchQuery(AlbumSearchQuery.Type.NEWEST),
            album_sort_direction="ascending",
            album_page_size=albums.ALL_ALBUMS,
            album_page=0,
        ),
        offline_mode=False,
        current_provider_id="test",
    )
    yield SimpleNamespace(data=data, config=config)
    _drain_events()
    Gtk.StyleContext.remove_provider_for_screen(screen, css)
    settings.props.gtk_enable_animations = old_animations


@pytest.fixture
def grid(library):
    widget = albums.AlbumsGrid()
    widget.update(widget.update_params(library.config), library.config)
    yield widget
    widget._stop_scroll()
    widget.destroy()
    _drain_events()


def _connect_app(grid, config, queued=False):
    def update(_, album_id):
        config.state.selected_album_id = album_id
        if queued:
            GLib.idle_add(grid.update, grid.order_ratchet, config)
        else:
            grid.update(grid.order_ratchet, config)

    grid.connect("cover-clicked", update)


def _select(grid, config, index):
    config.state.selected_album_id = grid._ordered(grid.current_models)[index].id
    grid.update(grid.order_ratchet, config)


def _assert_window(grid):
    shown = list(grid.list_store_top) + list(grid.list_store_bottom)
    assert [m.id for m in shown] == [m.id for m in grid._window(grid.current_models)]
    assert len(shown) <= albums.MAX_LOADED_ALBUMS
    for store, flowbox in (
        (grid.list_store_top, grid.grid_top),
        (grid.list_store_bottom, grid.grid_bottom),
    ):
        for i, model in enumerate(store):
            child = flowbox.get_child_at_index(i)
            assert child._album_model is model
            assert _tile_label(child) == model.album.name
    if grid.currently_selected_index is not None:
        relative = grid.currently_selected_index - grid._window_offset()
        assert grid.list_store_top[relative].id == grid.currently_selected_id
        assert grid.detail_box_inner.get_children()[0].album.id == grid.currently_selected_id


def _tile_label(child):
    header = child.get_child().get_children()[1]
    return header.get_children()[0].get_text()


@pytest.mark.parametrize("queued", [False, True])
def test_close_then_batch_opens_clicked_album_not_a_chunk_away(grid, library, queued):
    config = library.config
    _connect_app(grid, config, queued)
    for _ in range(3):
        grid._load_after()
    _select(grid, config, 48)
    grid.grid_top.emit("child-activated", grid.grid_top.get_child_at_index(48))
    _drain_events()
    assert grid.currently_selected_index is None
    grid._load_after()
    child = grid.grid_top.get_child_at_index(0)
    clicked = str(int(_tile_label(child).split()[-1]))
    grid.grid_top.emit("child-activated", child)
    _drain_events()
    assert config.state.selected_album_id == clicked
    assert grid.detail_box_inner.get_children()[0].album.id == clicked
    _assert_window(grid)


def test_deselection_keeps_albums_in_place(grid, library):
    _select(grid, library.config, 4)
    below = list(grid.list_store_bottom)
    tiles = [grid.grid_bottom.get_child_at_index(i) for i in range(len(below))]
    library.config.state.selected_album_id = None
    grid.update(grid.order_ratchet, library.config)
    assert not grid.detail_box_revealer.get_reveal_child()
    _drain_events()
    assert grid._pending_reflow is None
    assert grid.currently_selected_index is None
    # Not rebuilt (that made closing slow and the cover art flash).
    assert list(grid.list_store_bottom) == below
    assert [grid.grid_bottom.get_child_at_index(i) for i in range(len(below))] == tiles
    _assert_window(grid)


def test_batches_after_deselection_stay_in_order(grid, library):
    _select(grid, library.config, 4)
    library.config.state.selected_album_id = None
    grid.update(grid.order_ratchet, library.config)
    _drain_events()
    for _ in range(5):  # past MAX_LOADED_ALBUMS, so albums are dropped from the start
        grid._load_after()
        _assert_window(grid)
    for _ in range(5):
        grid._load_before()
        _assert_window(grid)
    _select(grid, library.config, 30)
    _drain_events()
    assert grid.currently_selected_index == 30
    _assert_window(grid)


def test_moved_tile_keeps_decoded_cover_art(grid, library, monkeypatch):
    monkeypatch.setattr(albums.AdapterManager, "get_cover_art_uri", lambda *a: Result("art.jpg"))
    tile = grid.grid_top.get_child_at_index(10)
    model = tile._album_model
    pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, 4, 4)
    model.artwork.filename = "art.jpg"  # as if decoded
    model.artwork.image.set_from_pixbuf(pixbuf)
    _select(grid, library.config, 4)  # moves it to the bottom grid
    _drain_events()
    found, i = grid.list_store_bottom.find(model)
    assert found
    assert grid.grid_bottom.get_child_at_index(i) is not tile
    assert model.artwork.image.get_pixbuf() is pixbuf


def test_descending_selection_of_master_index_zero(grid, library):
    library.config.state.album_sort_direction = "descending"
    library.config.state.selected_album_id = "0"
    grid.update(grid.order_ratchet, library.config)
    assert grid.currently_selected_index == 399
    assert grid.detail_box_inner.get_children()[0].album.id == "0"
    _assert_window(grid)


def test_switch_before_first_allocation_finishes_without_animations(grid, library):
    _select(grid, library.config, 4)
    _select(grid, library.config, 24)
    _drain_events()
    assert grid._pending_reflow is None
    assert grid.currently_selected_index == 24
    _assert_window(grid)


def test_latest_pending_selection_wins_and_blocks_batch_loading(grid, library):
    for _ in range(4):
        grid._load_after()
    _select(grid, library.config, 80)
    _select(grid, library.config, 100)
    assert grid._pending_reflow is not None
    before = (grid.window_start, grid.loaded_count)
    grid._load_after()
    grid._load_before()
    assert (grid.window_start, grid.loaded_count) == before
    _select(grid, library.config, 120)
    _drain_events()
    assert grid._pending_reflow is None
    assert grid.currently_selected_index == 120
    _assert_window(grid)


def test_cancel_pending_switch_back_to_open_album(grid, library):
    _select(grid, library.config, 4)
    detail = grid.detail_box_inner.get_children()[0]
    _select(grid, library.config, 24)
    _select(grid, library.config, 4)
    _drain_events()
    assert grid._pending_reflow is None
    assert grid.detail_box_revealer.get_reveal_child()
    assert grid.detail_box_inner.get_children()[0] is detail
    _assert_window(grid)


def test_resize_during_pending_switch_preserves_requested_album(grid, library):
    _select(grid, library.config, 4)
    _select(grid, library.config, 24)
    rect = Gdk.Rectangle()
    rect.width = 3 * 230
    grid.on_grid_resize(grid.grid_top, rect)
    _drain_events()
    assert grid.currently_selected_index == 24
    _assert_window(grid)


@pytest.mark.parametrize("backward", [False, True])
def test_eviction_notifies_only_after_window_commit(grid, library, backward):
    for _ in range(3):
        grid._load_after()
    if backward:
        for _ in range(4):
            grid._load_after()
        _select(grid, library.config, 399)
        load = grid._load_before
    else:
        _select(grid, library.config, 0)
        load = grid._load_after
    snapshots = []

    def observe(_, album_id):
        snapshots.append(
            (
                album_id,
                grid._changing_window,
                [m.id for m in list(grid.list_store_top) + list(grid.list_store_bottom)],
                [m.id for m in grid._window(grid.current_models)],
                grid.loaded_count,
            )
        )

    grid.connect("cover-clicked", observe)
    _connect_app(grid, library.config)
    load()
    assert len(snapshots) == 1
    album_id, changing, shown, expected, loaded = snapshots[0]
    assert album_id is None
    assert not changing
    assert shown == expected
    assert loaded <= albums.MAX_LOADED_ALBUMS
    _assert_window(grid)


def test_activation_uses_surviving_tile_identity_and_ignores_evicted_tile(grid, library):
    for _ in range(3):
        grid._load_after()
    gone = grid.grid_top.get_child_at_index(0)
    survivor = grid.grid_top.get_child_at_index(100)
    received = []
    grid.connect("cover-clicked", lambda _, album_id: received.append(album_id))
    grid._load_after()
    assert survivor.get_index() != 100
    assert _tile_label(survivor) == "Album 100"
    grid.grid_top.emit("child-activated", survivor)
    grid.on_child_activated(grid.grid_top, gone)
    assert received == ["100"]


def test_reentrant_batch_and_activation_during_splice_are_ignored(grid, library):
    for _ in range(3):
        grid._load_after()
    received = []
    grid.connect("cover-clicked", lambda _, album_id: received.append(album_id))

    def during_splice(*args):
        grid._load_after()
        grid.on_child_activated(grid.grid_top, grid.grid_top.get_child_at_index(0))

    handler = grid.list_store_top.connect("items-changed", during_splice)
    grid._load_after()
    grid.list_store_top.disconnect(handler)
    assert received == []
    assert grid.window_start == 52
    assert grid.loaded_count == 198
    _assert_window(grid)


@pytest.mark.parametrize("direction", ["ascending", "descending"])
@pytest.mark.parametrize("queued", [False, True])
def test_chunk_boundary_clicks_after_forward_and_backward_slides(grid, library, direction, queued):
    config = library.config
    config.state.album_sort_direction = direction
    grid.update(grid.order_ratchet, config)
    _connect_app(grid, config, queued)
    for _ in range(3):
        grid._load_after()
    for load in (grid._load_after, grid._load_before):
        load()
        for relative in (0, 49, 50, 99, 100, 149, 150, grid.loaded_count - 1):
            model = grid._window(grid.current_models)[relative]
            child = grid._tile_for(model)
            assert _tile_label(child) == model.album.name
            child.get_parent().emit("child-activated", child)
            _drain_events()
            assert config.state.selected_album_id == model.id
            assert grid.detail_box_inner.get_children()[0].album.id == model.id
            _assert_window(grid)


@pytest.mark.parametrize("direction", ["ascending", "descending"])
def test_forward_backward_open_selection_and_eviction(grid, library, direction):
    config = library.config
    config.state.album_sort_direction = direction
    grid.update(grid.order_ratchet, config)
    _connect_app(grid, config)
    for _ in range(3):
        grid._load_after()
    _select(grid, config, 180)
    for _ in range(3):
        grid._load_after()
        _assert_window(grid)
    # Selection 180 survived the first three drops; the next one evicts it.
    assert grid.currently_selected_index == 180
    grid._load_after()
    _assert_window(grid)
    assert config.state.selected_album_id is None
    _select(grid, config, 380)
    for _ in range(4):
        grid._load_before()
        _assert_window(grid)
    assert grid.window_start == 0
    assert config.state.selected_album_id is None


@pytest.mark.parametrize("direction", ["ascending", "descending"])
def test_discrete_wheel_scrolls_both_ways_with_open_album(grid, library, direction):
    config = library.config
    config.state.album_sort_direction = direction
    grid.update(grid.order_ratchet, config)
    _connect_app(grid, config, queued=True)
    window = Gtk.Window()
    window.set_default_size(1000, 600)
    window.add(grid)
    window.show_all()

    def ready():
        return grid._laid_out() and not grid._load_more_pending and grid._scroll_tick_id is None

    def wheel(direction):
        event = Gdk.Event.new(Gdk.EventType.SCROLL)
        event.scroll.window = grid.scrolled_window.get_window()
        event.scroll.direction = direction
        event.set_device(Gdk.Display.get_default().get_default_seat().get_pointer())
        grid.scrolled_window.emit("scroll-event", event)
        try:
            _wait_for(ready)
        except pytest.fail.Exception as e:
            adjustment = grid.scrolled_window.get_vadjustment()
            raise AssertionError(
                {
                    "window": (grid.window_start, grid.loaded_count),
                    "adjustment": (
                        adjustment.get_value(),
                        adjustment.get_upper(),
                        adjustment.get_page_size(),
                    ),
                    "hold": grid._hold,
                    "pending_load": grid._load_more_pending,
                    "pending_reflow": grid._pending_reflow,
                    "scroll_tick": grid._scroll_tick_id,
                    "edge_heights": [
                        grid._tile_for(m).get_allocated_height()
                        for m in (grid.list_store_top[0], grid._window(grid.current_models)[-1])
                    ],
                }
            ) from e
        _assert_window(grid)

    try:
        _wait_for(ready)
        _select(grid, config, 20)
        _wait_for(ready)
        adjustment = grid.scrolled_window.get_vadjustment()
        before = adjustment.get_value()
        wheel(Gdk.ScrollDirection.DOWN)
        assert adjustment.get_value() > before
        for _ in range(400):
            if grid.window_start >= 100:
                break
            wheel(Gdk.ScrollDirection.DOWN)
        assert grid.window_start >= 100
        assert config.state.selected_album_id is None
        _select(grid, config, grid.window_start + grid.loaded_count - 5)
        _wait_for(ready)
        for _ in range(400):
            if grid.window_start == 0:
                break
            wheel(Gdk.ScrollDirection.UP)
        assert grid.window_start == 0
        assert config.state.selected_album_id is None
    finally:
        grid._stop_scroll()
        window.remove(grid)
        window.destroy()


@pytest.mark.parametrize("sort", ["alphabetical", "year_range", "genre"])
@pytest.mark.parametrize("page_size", [30, albums.ALL_ALBUMS])
def test_panel_minimum_width_fits_half_screen(library, sort, page_size):
    settings = Gtk.Settings.get_default()
    old_theme, old_font = settings.props.gtk_theme_name, settings.props.gtk_font_name
    settings.props.gtk_theme_name, settings.props.gtk_font_name = "Adwaita", "Sans 10"
    css = Gtk.CssProvider()
    css.load_from_path(str(Path(albums.__file__).with_name("app_styles.css")))
    screen = Gdk.Screen.get_default()
    Gtk.StyleContext.add_provider_for_screen(screen, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    panel = albums.AlbumsPanel()
    query_type = {
        "alphabetical": AlbumSearchQuery.Type.ALPHABETICAL_BY_NAME,
        "year_range": AlbumSearchQuery.Type.YEAR_RANGE,
        "genre": AlbumSearchQuery.Type.GENRE,
    }[sort]
    library.config.state.current_album_search_query = AlbumSearchQuery(
        query_type, year_range=(1900, 2026)
    )
    library.config.state.album_page_size = page_size
    try:
        panel.show_all()
        panel.update(library.config)
        panel.genre_combo_store.append(("genre", "A very long genre name " * 8, True))
        panel.genre_combo.set_active_id("genre")
        panel.on_grid_num_pages_changed(None, 10000)
        assert panel.get_preferred_width().minimum_width <= 720
        assert panel.sort_type_combo.get_visible()
        assert panel.show_count_dropdown.get_visible()
        assert panel.refresh_button.get_visible()
        assert panel.page_widget.get_visible() == (page_size != albums.ALL_ALBUMS)
    finally:
        panel.grid._stop_scroll()
        panel.destroy()
        Gtk.StyleContext.remove_provider_for_screen(screen, css)
        settings.props.gtk_theme_name, settings.props.gtk_font_name = old_theme, old_font
