import datetime
import logging
import math
from typing import Any, Callable, Iterable, List, Optional, Tuple, cast

from gi.repository import Gdk, Gio, GLib, GObject, Gtk, Pango

from ..adapters import AdapterManager, AlbumSearchQuery, CacheMissError, Result, api_objects as API
from ..config import AppConfiguration
from ..ui import util
from ..ui.common import AlbumWithSongs, DigitsEntry, IconButton, LoadError, SpinnerImage

# The sorts that list every album (the others show only some: starred, one genre, the
# played ones...), and their names in the settings.
ALBUM_SORTS_LISTING_EVERY_ALBUM = {
    AlbumSearchQuery.Type.NEWEST: "Recently Added",
    AlbumSearchQuery.Type.ALPHABETICAL_BY_NAME: "Album Name",
    AlbumSearchQuery.Type.ALPHABETICAL_BY_ARTIST: "Artist Name",
}


# The "All" albums-per-page option: no pages, the next ALL_ALBUMS_BATCH albums are shown
# whenever the list is scrolled near its end.
ALL_ALBUMS = 0
ALL_ALBUMS_BATCH = 50
# With "All", at most this many albums are shown at once (more makes opening and closing
# albums laggy): scrolling on drops albums at the other end, which come back when
# scrolling back to them.
MAX_LOADED_ALBUMS = 200


def go_to_album_query(query: AlbumSearchQuery, fallback_sort: str) -> AlbumSearchQuery:
    """
    The album query to show when going to an album: ``query`` if it lists every album,
    otherwise the ``fallback_sort`` (a Type name; recently added if it isn't valid).

    >>> T = AlbumSearchQuery.Type
    >>> go_to_album_query(AlbumSearchQuery(T.ALPHABETICAL_BY_ARTIST), "NEWEST").type
    <Type.ALPHABETICAL_BY_ARTIST: 6>
    >>> go_to_album_query(AlbumSearchQuery(T.STARRED), "NEWEST").type
    <Type.NEWEST: 1>
    >>> go_to_album_query(AlbumSearchQuery(T.GENRE), "ALPHABETICAL_BY_NAME").type
    <Type.ALPHABETICAL_BY_NAME: 5>
    >>> go_to_album_query(AlbumSearchQuery(T.RANDOM), "STARRED").type
    <Type.NEWEST: 1>
    >>> go_to_album_query(AlbumSearchQuery(T.RECENT), "nonsense").type
    <Type.NEWEST: 1>
    """
    if query.type in ALBUM_SORTS_LISTING_EVERY_ALBUM:
        return query
    fallback = AlbumSearchQuery.Type.__members__.get(fallback_sort, AlbumSearchQuery.Type.NEWEST)
    if fallback not in ALBUM_SORTS_LISTING_EVERY_ALBUM:
        fallback = AlbumSearchQuery.Type.NEWEST
    # Keep the genre and years, so that switching back to those views restores them.
    return AlbumSearchQuery(fallback, genre=query.genre, year_range=query.year_range)


def _to_type(query_type: AlbumSearchQuery.Type) -> str:
    return {
        AlbumSearchQuery.Type.RANDOM: "random",
        AlbumSearchQuery.Type.NEWEST: "newest",
        AlbumSearchQuery.Type.FREQUENT: "frequent",
        AlbumSearchQuery.Type.RECENT: "recent",
        AlbumSearchQuery.Type.STARRED: "starred",
        AlbumSearchQuery.Type.ALPHABETICAL_BY_NAME: "alphabetical",
        AlbumSearchQuery.Type.ALPHABETICAL_BY_ARTIST: "alphabetical",
        AlbumSearchQuery.Type.YEAR_RANGE: "year_range",
        AlbumSearchQuery.Type.GENRE: "genre",
    }[query_type]


def _from_str(type_str: str) -> AlbumSearchQuery.Type:
    return {
        "random": AlbumSearchQuery.Type.RANDOM,
        "newest": AlbumSearchQuery.Type.NEWEST,
        "frequent": AlbumSearchQuery.Type.FREQUENT,
        "recent": AlbumSearchQuery.Type.RECENT,
        "starred": AlbumSearchQuery.Type.STARRED,
        "alphabetical_by_name": AlbumSearchQuery.Type.ALPHABETICAL_BY_NAME,
        "alphabetical_by_artist": AlbumSearchQuery.Type.ALPHABETICAL_BY_ARTIST,
        "year_range": AlbumSearchQuery.Type.YEAR_RANGE,
        "genre": AlbumSearchQuery.Type.GENRE,
    }[type_str]


class AlbumsPanel(Gtk.Box):
    __gsignals__ = {
        "song-clicked": (
            GObject.SignalFlags.RUN_FIRST,
            GObject.TYPE_NONE,
            (int, object, object),
        ),
        "refresh-window": (
            GObject.SignalFlags.RUN_FIRST,
            GObject.TYPE_NONE,
            (object, bool),
        ),
    }

    offline_mode = False
    populating_genre_combo = False
    grid_order_token: int = 0
    album_sort_direction: str = "ascending"
    album_page_size: int = 30
    album_page: int = 0
    grid_pages_count: int = 0

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)

        actionbar = Gtk.ActionBar()

        # Sort by
        actionbar.add(Gtk.Label(label="Sort / Filter"))
        self.sort_type_combo, self.sort_type_combo_store = self.make_combobox(
            (
                ("random", "randomly", True),
                ("genre", "by genre", AdapterManager.can_get_genres()),
                ("newest", "by most recently added", True),
                ("frequent", "by most played", True),
                ("recent", "by most recently played", True),
                ("alphabetical", "alphabetically", True),
                ("starred", "by starred only", True),
                ("year_range", "by year", True),
            ),
            self.on_type_combo_changed,
        )
        actionbar.pack_start(self.sort_type_combo)

        self.alphabetical_type_combo, _ = self.make_combobox(
            (("by_name", "by album name", True), ("by_artist", "by artist name", True)),
            self.on_alphabetical_type_change,
        )
        actionbar.pack_start(self.alphabetical_type_combo)

        self.genre_combo, self.genre_combo_store = self.make_combobox((), self.on_genre_change)
        actionbar.pack_start(self.genre_combo)

        next_decade = (datetime.datetime.now().year // 10) * 10 + 10

        self.from_year_label = Gtk.Label(label="from")
        actionbar.pack_start(self.from_year_label)
        self.from_year_spin_button = Gtk.SpinButton.new_with_range(0, next_decade, 1)
        self.from_year_spin_button.connect("value-changed", self.on_year_changed)
        actionbar.pack_start(self.from_year_spin_button)

        self.to_year_label = Gtk.Label(label="to")
        actionbar.pack_start(self.to_year_label)
        self.to_year_spin_button = Gtk.SpinButton.new_with_range(0, next_decade, 1)
        self.to_year_spin_button.connect("value-changed", self.on_year_changed)
        actionbar.pack_start(self.to_year_spin_button)

        self.sort_toggle = IconButton(
            "view-sort-descending-symbolic", "Sort descending", relief=True
        )
        self.sort_toggle.connect("clicked", self.on_sort_toggle_clicked)
        actionbar.pack_start(self.sort_toggle)

        # Add the page widget.
        self.page_widget = page_widget = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        self.prev_page = IconButton(
            "go-previous-symbolic", "Go to the previous page", sensitive=False
        )
        self.prev_page.connect("clicked", self.on_prev_page_clicked)
        page_widget.add(self.prev_page)
        page_widget.add(Gtk.Label(label="Page"))
        self.page_entry = DigitsEntry(is_allowed=self.page_number_exists)
        self.page_entry.set_width_chars(1)
        self.page_entry.set_max_width_chars(1)
        self.page_entry.connect("changed", self.on_page_entry_changed)
        page_widget.add(self.page_entry)
        page_widget.add(Gtk.Label(label="of"))
        self.page_count_label = Gtk.Label(label="-")
        page_widget.add(self.page_count_label)
        self.next_page = IconButton("go-next-symbolic", "Go to the next page", sensitive=False)
        self.next_page.connect("clicked", self.on_next_page_clicked)
        page_widget.add(self.next_page)
        actionbar.set_center_widget(page_widget)

        self.refresh_button = IconButton(
            "view-refresh-symbolic", "Refresh list of albums", relief=True
        )
        self.refresh_button.connect("clicked", self.on_refresh_clicked)
        actionbar.pack_end(self.refresh_button)

        actionbar.pack_end(Gtk.Label(label="albums per page"))
        self.show_count_dropdown, _ = self.make_combobox(
            [(x, x, True) for x in ("20", "30", "40", "50", "75", "100")]
            + [(str(ALL_ALBUMS), "All", True)],
            self.on_show_count_dropdown_change,
        )
        actionbar.pack_end(self.show_count_dropdown)
        actionbar.pack_end(Gtk.Label(label="Show"))

        self.add(actionbar)

        scrolled_window = Gtk.ScrolledWindow()
        self.grid = AlbumsGrid()
        self.grid.connect(
            "song-clicked",
            lambda _, *args: self.emit("song-clicked", *args),
        )
        self.grid.connect(
            "refresh-window",
            lambda _, *args: self.emit("refresh-window", *args),
        )
        self.grid.connect("cover-clicked", self.on_grid_cover_clicked)
        self.grid.connect("num-pages-changed", self.on_grid_num_pages_changed)
        scrolled_window.add(self.grid)
        self.add(scrolled_window)

    def make_combobox(
        self,
        items: Iterable[Tuple[str, str, bool]],
        on_change: Callable[[Gtk.ComboBox], None],
    ) -> Tuple[Gtk.ComboBox, Gtk.ListStore]:
        store = Gtk.ListStore(str, str, bool)
        for item in items:
            store.append(item)

        combo = Gtk.ComboBox.new_with_model(store)
        combo.set_id_column(0)
        combo.connect("changed", on_change)

        renderer_text = Gtk.CellRendererText()
        combo.pack_start(renderer_text, True)
        combo.add_attribute(renderer_text, "text", 1)
        combo.add_attribute(renderer_text, "sensitive", 2)

        return combo, store

    def populate_genre_combo(
        self,
        app_config: AppConfiguration | None = None,
        force: bool = False,
    ):
        if not AdapterManager.can_get_genres():
            self.updating_query = False
            return

        def get_genres_done(f: Result):
            try:
                genre_names = (g.name for g in f.result() or [])
                new_store = [(name, name, True) for name in sorted(genre_names)]

                util.diff_song_store(self.genre_combo_store, new_store)

                if app_config:
                    current_genre_id = self.get_id(self.genre_combo)
                    genre = app_config.state.current_album_search_query.genre
                    if genre and current_genre_id != (genre_name := genre.name):
                        self.genre_combo.set_active_id(genre_name)
            finally:
                self.updating_query = False

        try:
            force = force and (
                app_config is not None
                and (state := app_config.state) is not None
                and state.current_album_search_query.type == AlbumSearchQuery.Type.GENRE
            )
            genres_future = AdapterManager.get_genres(force=force)
            genres_future.add_done_callback(lambda f: GLib.idle_add(get_genres_done, f))
        except Exception:
            self.updating_query = False

    def update(self, app_config: AppConfiguration | None = None, force: bool = False):
        self.updating_query = True

        supported_type_strings = {
            _to_type(t) for t in AdapterManager.get_supported_artist_query_types()
        }
        for i, el in enumerate(self.sort_type_combo_store):
            self.sort_type_combo_store[i][2] = el[0] in supported_type_strings

        # (En|Dis)able getting genres.
        self.sort_type_combo_store[1][2] = AdapterManager.can_get_genres()

        if app_config:
            self.current_query = app_config.state.current_album_search_query
            self.offline_mode = app_config.offline_mode

        self.alphabetical_type_combo.set_active_id(
            {
                AlbumSearchQuery.Type.ALPHABETICAL_BY_NAME: "by_name",
                AlbumSearchQuery.Type.ALPHABETICAL_BY_ARTIST: "by_artist",
            }.get(self.current_query.type)
            or "by_name"
        )
        self.sort_type_combo.set_active_id(_to_type(self.current_query.type))

        if year_range := self.current_query.year_range:
            self.from_year_spin_button.set_value(year_range[0])
            self.to_year_spin_button.set_value(year_range[1])

        # Update the page display
        if app_config:
            self.album_page = app_config.state.album_page
            self.album_page_size = app_config.state.album_page_size
            self.refresh_button.set_sensitive(not app_config.offline_mode)

        self.prev_page.set_sensitive(self.album_page > 0)
        self.page_entry.set_text(str(self.album_page + 1))
        # "All" has no pages: it loads more albums as you scroll.
        self.page_widget.set_visible(self.album_page_size != ALL_ALBUMS)

        # Show/hide the combo boxes.
        def show_if(sort_type: Iterable[AlbumSearchQuery.Type], *elements):
            for element in elements:
                if self.current_query.type in sort_type:
                    element.show()
                else:
                    element.hide()

        show_if(
            (
                AlbumSearchQuery.Type.ALPHABETICAL_BY_NAME,
                AlbumSearchQuery.Type.ALPHABETICAL_BY_ARTIST,
            ),
            self.alphabetical_type_combo,
        )
        show_if((AlbumSearchQuery.Type.GENRE,), self.genre_combo)
        show_if(
            (AlbumSearchQuery.Type.YEAR_RANGE,),
            self.from_year_label,
            self.from_year_spin_button,
            self.to_year_label,
            self.to_year_spin_button,
        )

        # (En|Dis)able the sort button
        self.sort_toggle.set_sensitive(self.current_query.type != AlbumSearchQuery.Type.RANDOM)

        if app_config:
            self.album_sort_direction = app_config.state.album_sort_direction
            self.sort_toggle.set_icon(f"view-sort-{self.album_sort_direction}-symbolic")
            self.sort_toggle.set_tooltip_text(
                "Change sort order to " + self._get_opposite_sort_dir(self.album_sort_direction)
            )

            self.show_count_dropdown.set_active_id(str(app_config.state.album_page_size))

        # Has to be last because it resets self.updating_query
        self.populate_genre_combo(app_config, force=force)

        # At this point, the current query should be totally updated.
        if app_config:
            self.grid_order_token = self.grid.update_params(app_config)
        self.grid.update(self.grid_order_token, app_config, force=force)

    def _get_opposite_sort_dir(self, sort_dir: str) -> str:
        return ("ascending", "descending")[0 if sort_dir == "descending" else 1]

    def get_id(self, combo: Gtk.ComboBox) -> Optional[str]:
        tree_iter = combo.get_active_iter()
        if tree_iter is not None:
            return combo.get_model()[tree_iter][0]
        return None

    def on_sort_toggle_clicked(self, _):
        self.emit(
            "refresh-window",
            {
                "album_sort_direction": self._get_opposite_sort_dir(self.album_sort_direction),
                "album_page": 0,
                "selected_album_id": None,
            },
            False,
        )

    def on_refresh_clicked(self, _):
        self.emit("refresh-window", {}, True)

    class _Genre(API.Genre):
        def __init__(self, name: str):
            self.name = name

    def on_grid_num_pages_changed(self, grid: Any, pages: int):
        self.grid_pages_count = pages
        pages_str = str(self.grid_pages_count)
        self.page_count_label.set_text(pages_str)
        self.next_page.set_sensitive(self.album_page < self.grid_pages_count - 1)
        num_digits = len(pages_str)
        self.page_entry.set_width_chars(num_digits)
        self.page_entry.set_max_width_chars(num_digits)

    def on_type_combo_changed(self, combo: Gtk.ComboBox):
        id = self.get_id(combo)
        assert id
        if id == "alphabetical":
            id += "_" + cast(str, self.get_id(self.alphabetical_type_combo))
        self.emit_if_not_updating(
            "refresh-window",
            {
                "current_album_search_query": AlbumSearchQuery(
                    _from_str(id),
                    self.current_query.year_range,
                    self.current_query.genre,
                ),
                "album_page": 0,
                "selected_album_id": None,
            },
            False,
        )

    def on_alphabetical_type_change(self, combo: Gtk.ComboBox):
        id = "alphabetical_" + cast(str, self.get_id(combo))
        self.emit_if_not_updating(
            "refresh-window",
            {
                "current_album_search_query": AlbumSearchQuery(
                    _from_str(id),
                    self.current_query.year_range,
                    self.current_query.genre,
                ),
                "album_page": 0,
                "selected_album_id": None,
            },
            False,
        )

    def on_genre_change(self, combo: Gtk.ComboBox):
        genre = self.get_id(combo)
        assert genre
        self.emit_if_not_updating(
            "refresh-window",
            {
                "current_album_search_query": AlbumSearchQuery(
                    self.current_query.type,
                    self.current_query.year_range,
                    AlbumsPanel._Genre(genre),
                ),
                "album_page": 0,
                "selected_album_id": None,
            },
            False,
        )

    def on_year_changed(self, entry: Gtk.SpinButton) -> bool:
        year = int(entry.get_value())
        assert self.current_query.year_range
        if self.to_year_spin_button == entry:
            new_year_tuple = (self.current_query.year_range[0], year)
        else:
            new_year_tuple = (year, self.current_query.year_range[1])

        self.emit_if_not_updating(
            "refresh-window",
            {
                "current_album_search_query": AlbumSearchQuery(
                    self.current_query.type, new_year_tuple, self.current_query.genre
                ),
                "album_page": 0,
                "selected_album_id": None,
            },
            False,
        )

        return False

    def on_page_entry_changed(self, entry: Gtk.Entry) -> bool:
        if len(text := entry.get_text()) > 0:
            self.emit_if_not_updating(
                "refresh-window",
                {"album_page": int(text) - 1, "selected_album_id": None},
                False,
            )
        return False

    def page_number_exists(self, text: str) -> bool:
        # While the query is being updated the entry is set programmatically.
        if self.updating_query or self.grid_pages_count is None:
            return True
        return int(text) <= self.grid_pages_count

    def on_prev_page_clicked(self, _):
        self.emit_if_not_updating(
            "refresh-window",
            {"album_page": self.album_page - 1, "selected_album_id": None},
            False,
        )

    def on_next_page_clicked(self, _):
        self.emit_if_not_updating(
            "refresh-window",
            {"album_page": self.album_page + 1, "selected_album_id": None},
            False,
        )

    def on_grid_cover_clicked(self, grid: Any, id: str):
        self.emit(
            "refresh-window",
            {"selected_album_id": id},
            False,
        )

    def on_show_count_dropdown_change(self, combo: Gtk.ComboBox):
        show_count = int(self.get_id(combo) or 30)
        self.emit(
            "refresh-window",
            {"album_page_size": show_count, "album_page": 0},
            False,
        )

    def emit_if_not_updating(self, *args):
        if self.updating_query:
            return
        self.emit(*args)


class AlbumsGrid(Gtk.Overlay):
    """Defines the albums panel."""

    __gsignals__ = {
        "cover-clicked": (GObject.SignalFlags.RUN_FIRST, GObject.TYPE_NONE, (object,)),
        "refresh-window": (
            GObject.SignalFlags.RUN_FIRST,
            GObject.TYPE_NONE,
            (object, bool),
        ),
        "song-clicked": (
            GObject.SignalFlags.RUN_FIRST,
            GObject.TYPE_NONE,
            (int, object, object),
        ),
        "num-pages-changed": (GObject.SignalFlags.RUN_FIRST, GObject.TYPE_NONE, (int,)),
    }

    class _AlbumModel(GObject.Object):
        def __init__(self, album: API.Album):
            self.album = album
            # The star next to the title in this album's tile (the latest tile, as the
            # grids rebuild tiles when albums move between them).
            self.star_icon: Optional[Gtk.Image] = None
            super().__init__()

        @property
        def id(self) -> str:
            assert self.album.id
            return self.album.id

        def __repr__(self) -> str:
            return f"<AlbumsGrid._AlbumModel {self.album}>"

    current_query: AlbumSearchQuery = AlbumSearchQuery(AlbumSearchQuery.Type.NEWEST)
    current_models: List[_AlbumModel] = []
    latest_applied_order_ratchet: int = 0
    order_ratchet: int = 0
    offline_mode: bool = False

    currently_selected_index: Optional[int] = None
    currently_selected_id: Optional[str] = None
    current_song_id: Optional[str] = None
    # A reflow waiting for the details of another album to finish closing, see
    # reflow_grids: (force_reload_from_master, selected_index, models).
    _pending_reflow: Optional[Tuple[bool, int, Optional[List["_AlbumModel"]]]] = None
    sort_dir: str = ""
    page_size: int = 30
    page: int = 0
    num_pages: Optional[int] = None
    next_page_fn = None
    provider_id: Optional[str] = None

    def update_params(self, app_config: AppConfiguration) -> int:
        # If there's a diff, increase the ratchet.
        if (
            self.current_query.strhash()
            != (search_query := app_config.state.current_album_search_query).strhash()
        ):
            self.order_ratchet += 1
            self.current_query = search_query

        if self.offline_mode != (offline_mode := app_config.offline_mode):
            self.order_ratchet += 1
            self.offline_mode = offline_mode

        if self.provider_id != (provider_id := app_config.current_provider_id):
            self.order_ratchet += 1
            self.provider_id = provider_id

        return self.order_ratchet

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.items_per_row = 4

        self.scrolled_window = scrolled_window = Gtk.ScrolledWindow()
        grid_detail_grid_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._scroll_content = grid_detail_grid_box
        self._scroll_tick_id: Optional[int] = None
        self._scroll_start_time: Optional[int] = None
        self._scroll_start_value = 0.0
        self._scroll_last_value = 0.0

        # With "All": which albums are shown (from window_start, loaded_count of them).
        self.window_start = 0
        self.loaded_count = ALL_ALBUMS_BATCH
        self._load_more_pending = False
        self._hold: Optional[Tuple["AlbumsGrid._AlbumModel", int, bool]] = None
        # While switching albums the content keeps its height (see _keep_height).
        self._height_kept = False
        grid_detail_grid_box.connect("size-allocate", self._on_content_allocated)
        vadjustment = scrolled_window.get_vadjustment()
        vadjustment.connect("value-changed", self._on_scroll_changed)
        vadjustment.connect("changed", self._on_scroll_changed)

        self.error_container = Gtk.Box()
        grid_detail_grid_box.add(self.error_container)

        def create_flowbox(**kwargs) -> Gtk.FlowBox:
            flowbox = Gtk.FlowBox(
                **kwargs,
                hexpand=True,
                row_spacing=5,
                column_spacing=5,
                margin_top=5,
                homogeneous=True,
                valign=Gtk.Align.START,
                halign=Gtk.Align.CENTER,
                selection_mode=Gtk.SelectionMode.SINGLE,
            )
            flowbox.set_max_children_per_line(7)
            return flowbox

        self.grid_top = create_flowbox()
        self.grid_top.connect("child-activated", self.on_child_activated)
        self.grid_top.connect("size-allocate", self.on_grid_resize)

        self.list_store_top = Gio.ListStore()
        self.grid_top.bind_model(self.list_store_top, self._create_cover_art_widget)

        grid_detail_grid_box.add(self.grid_top)

        self.detail_box_revealer = Gtk.Revealer(valign=Gtk.Align.END)
        self.detail_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, name="artist-detail-box")
        self.detail_box.pack_start(Gtk.Box(), True, True, 0)

        self.detail_box_inner = Gtk.Box()
        self.detail_box.pack_start(self.detail_box_inner, False, False, 0)

        self.detail_box.pack_start(Gtk.Box(), True, True, 0)
        self.detail_box_revealer.add(self.detail_box)
        self.detail_box_revealer.connect(
            "notify::child-revealed", self._on_detail_box_child_revealed
        )
        grid_detail_grid_box.add(self.detail_box_revealer)

        self.grid_bottom = create_flowbox(vexpand=True)
        self.grid_bottom.connect("child-activated", self.on_child_activated)

        self.list_store_bottom = Gio.ListStore()
        self.grid_bottom.bind_model(self.list_store_bottom, self._create_cover_art_widget)

        grid_detail_grid_box.add(self.grid_bottom)

        scrolled_window.add(grid_detail_grid_box)
        # GTK 3's viewport scrolls to whatever gets focus (a clicked album, a rebuilt
        # tile), which fought the scrolling done here. Give it adjustments to move that
        # aren't the real ones (it can't be switched off: None isn't accepted).
        viewport = scrolled_window.get_child()
        viewport.set_focus_vadjustment(Gtk.Adjustment())
        viewport.set_focus_hadjustment(Gtk.Adjustment())
        self.add(scrolled_window)

        self.spinner = Gtk.Spinner(
            name="grid-spinner",
            active=True,
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
        )
        self.add_overlay(self.spinner)

    def update(
        self, order_token: int, app_config: AppConfiguration | None = None, force: bool = False
    ):
        if order_token < self.latest_applied_order_ratchet:
            return

        force_grid_reload_from_master = False
        if app_config:
            self.currently_selected_id = app_config.state.selected_album_id
            current_song = app_config.state.current_song
            self.current_song_id = current_song.id if current_song else None

            if (
                self.sort_dir != app_config.state.album_sort_direction
                or self.page_size != app_config.state.album_page_size
                or self.page != app_config.state.album_page
            ):
                force_grid_reload_from_master = True
            self.sort_dir = app_config.state.album_sort_direction
            self.page_size = app_config.state.album_page_size
            self.page = app_config.state.album_page

        self.update_grid(
            order_token,
            use_ground_truth_adapter=force,
            force_grid_reload_from_master=force_grid_reload_from_master,
        )

        # Update the detail panel.
        children = self.detail_box_inner.get_children()
        if len(children) > 0 and hasattr(children[0], "update"):
            children[0].update(app_config=app_config, force=force)

    error_dialog = None

    # Paging, or with "All" a sliding window of at most MAX_LOADED_ALBUMS albums
    # =========================================================================
    def _window_size(self) -> int:
        """How many albums are shown: the page size, or with "All" the loaded ones."""
        return self.loaded_count if self.page_size == ALL_ALBUMS else self.page_size

    def _window_offset(self) -> int:
        """Where the shown albums start in the (sorted) list of all of them."""
        return self.window_start if self.page_size == ALL_ALBUMS else self.page_size * self.page

    def _num_pages(self) -> int:
        if self.page_size == ALL_ALBUMS:
            return 1
        return math.ceil(len(self.current_models) / self.page_size)

    def _ordered(self, models: List["AlbumsGrid._AlbumModel"]) -> List["AlbumsGrid._AlbumModel"]:
        return models if self.sort_dir == "ascending" else models[::-1]

    def _window(self, models: List["AlbumsGrid._AlbumModel"]) -> List["AlbumsGrid._AlbumModel"]:
        """The albums shown (in display order): this page, or the loaded ones."""
        offset = self._window_offset()
        return self._ordered(models)[offset : offset + self._window_size()]

    def _whole_rows(self, count: int) -> int:
        """``count`` rounded up to whole rows, so that the rows below don't reshuffle."""
        per_row = max(1, self.items_per_row)
        return math.ceil(count / per_row) * per_row

    def _laid_out(self) -> bool:
        # Nothing shown, not laid out yet (the adjustment would say "empty"), or the last
        # batch not laid out yet (its tiles have no position for _hold_in_place). The
        # layout changes the adjustment, which checks again.
        if self._hold is not None or len(self.list_store_top) == 0:
            return False
        shown = list(self.list_store_top) + list(self.list_store_bottom)
        return all(
            (tile := self._tile_for(model)) is not None and tile.get_allocated_height() > 1
            for model in (shown[0], shown[-1])
        )

    def _near_end(self) -> bool:
        """With "All": whether there are more albums after the loaded ones and the view
        is within a screen of the end (so that they're there before the user is)."""
        if self.page_size != ALL_ALBUMS or not self._laid_out():
            return False
        if self.window_start + self.loaded_count >= len(self.current_models):
            return False
        adjustment = self.scrolled_window.get_vadjustment()
        page = adjustment.get_page_size()
        return adjustment.get_upper() - adjustment.get_value() - page < page

    def _near_start(self) -> bool:
        """With "All": whether albums before the loaded ones were dropped and the view is
        within a screen of the start."""
        if self.page_size != ALL_ALBUMS or self.window_start == 0 or not self._laid_out():
            return False
        adjustment = self.scrolled_window.get_vadjustment()
        return adjustment.get_value() < adjustment.get_page_size()

    def _on_scroll_changed(self, *args):
        if self._height_kept and self._scroll_tick_id is None and self._hold is None:
            self._give_back_height()
        # Check again once GTK has finished laying out (this also runs on size changes,
        # when the numbers can be half-way).
        if not self._load_more_pending and (self._near_end() or self._near_start()):
            self._load_more_pending = True
            GLib.idle_add(self._load_more)

    def _load_more(self) -> bool:
        self._load_more_pending = False
        if self._near_end():
            self._load_after()
        elif self._near_start():
            self._load_before()
        # If the grid still doesn't fill the view, the adjustment's "changed" signal
        # brings us back here for the next batch.
        return False

    def _load_after(self):
        """Shows the next batch; drops rows from the start beyond MAX_LOADED_ALBUMS."""
        shown = self._window(self.current_models)
        if self.loaded_count + ALL_ALBUMS_BATCH > MAX_LOADED_ALBUMS:
            # Albums will be dropped from the start (adding them at the end doesn't move
            # anything on the screen).
            self._hold_in_place(shown[-1])
        end = self.window_start + self.loaded_count
        new_albums = self._ordered(self.current_models)[end : end + ALL_ALBUMS_BATCH]
        self.loaded_count += len(new_albums)
        # Everything after the open album's row is in the bottom grid.
        store = (
            self.list_store_top
            if self.currently_selected_index is None
            else self.list_store_bottom
        )
        store.splice(len(store), 0, new_albums)

        if (excess := self.loaded_count - MAX_LOADED_ALBUMS) > 0:
            drop = min(self._whole_rows(excess), self.loaded_count)
            if self.currently_selected_index is not None and drop >= len(self.list_store_top):
                self._close_detail_now()  # the open album is one of them
            self.list_store_top.splice(0, drop, [])
            self.window_start += drop
            self.loaded_count -= drop

    def _load_before(self):
        """Shows the previous batch again; drops albums from the end beyond
        MAX_LOADED_ALBUMS."""
        shown = self._window(self.current_models)
        self._hold_in_place(shown[0])
        count = min(self._whole_rows(ALL_ALBUMS_BATCH), self.window_start)
        new_albums = self._ordered(self.current_models)[
            self.window_start - count : self.window_start
        ]
        self.list_store_top.splice(0, 0, new_albums)
        self.window_start -= count
        self.loaded_count += count

        if (drop := self.loaded_count - MAX_LOADED_ALBUMS) > 0:
            if self.currently_selected_index is not None and drop > len(self.list_store_bottom):
                self._close_detail_now()  # the open album is one of them
            store = (
                self.list_store_top
                if self.currently_selected_index is None
                else self.list_store_bottom
            )
            store.splice(len(store) - drop, drop, [])
            self.loaded_count -= drop

    def _close_detail_now(self):
        """Closes the open album without animating (it is far out of view) and puts all
        of the albums back into the top grid."""
        self._stop_scroll()
        self._pending_reflow = None
        self._release_hold()
        self._height_kept = False
        self._scroll_content.set_size_request(-1, -1)
        transition = self.detail_box_revealer.get_transition_type()
        self.detail_box_revealer.set_transition_type(Gtk.RevealerTransitionType.NONE)
        self.detail_box_revealer.set_reveal_child(False)
        self.detail_box_revealer.set_transition_type(transition)
        below = list(self.list_store_bottom)
        self.list_store_bottom.splice(0, len(below), [])
        self.list_store_top.splice(len(self.list_store_top), 0, below)
        self.currently_selected_index = None
        self.grid_top.unselect_all()
        self.emit("cover-clicked", None)  # so that the app forgets the selection too

    # Keeping an album in the same place on the screen while things around it change
    # =========================================================================
    def _keep_height(self):
        """
        Keeps the content at least as tall as it is now while an album closes and the
        next one opens. Otherwise, scrolled near the end, the view has to follow the
        content down as it shrinks, and back up as the next album opens.
        """
        self._height_kept = True
        self._scroll_content.set_size_request(-1, self._scroll_content.get_allocated_height())

    def _content_bottom(self) -> float:
        """Where the albums end (the kept height can make the content taller)."""
        pos = self.grid_bottom.translate_coordinates(self._scroll_content, 0, 0)
        width = self.grid_bottom.get_allocated_width()
        _, natural = self.grid_bottom.get_preferred_height_for_width(width)
        return (pos[1] if pos else 0) + natural

    def _give_back_height(self):
        """Stops keeping the height, as far as that doesn't move what's on the screen
        (the rest goes as the user scrolls back up, see _on_scroll_changed)."""
        if not self._height_kept:
            return
        adjustment = self.scrolled_window.get_vadjustment()
        needed = adjustment.get_value() + adjustment.get_page_size()
        if needed <= self._content_bottom() + 1:
            self._height_kept = False
            self._scroll_content.set_size_request(-1, -1)
        else:
            self._scroll_content.set_size_request(-1, int(math.ceil(needed)))

    def _tile_for(self, model: "AlbumsGrid._AlbumModel") -> Optional[Gtk.Widget]:
        for store, grid in (
            (self.list_store_top, self.grid_top),
            (self.list_store_bottom, self.grid_bottom),
        ):
            for i, m in enumerate(store):
                if m is model:
                    return grid.get_child_at_index(i)
        return None

    def _tile_y(self, model: "AlbumsGrid._AlbumModel") -> Optional[int]:
        if not (tile := self._tile_for(model)):
            return None
        pos = tile.translate_coordinates(self._scroll_content, 0, 0)
        return pos[1] if pos else None

    def _hold_in_place(self, model: "AlbumsGrid._AlbumModel", until_released: bool = False):
        """
        Keeps ``model``'s tile where it is on the screen through the next layout (or,
        with ``until_released``, through every layout until _release_hold): scrolls by
        however much the content above it grows or shrinks.
        """
        if (y := self._tile_y(model)) is not None:
            self._hold = (model, y, until_released)

    def _release_hold(self):
        self._hold = None

    def _on_content_allocated(self, *args):
        if not (hold := self._hold):
            return
        model, old_y, until_released = hold
        if (y := self._tile_y(model)) is None:
            return
        # Scroll by as much as the tile moved (rather than to where it was on the
        # screen): the user may have scrolled since, which must not be undone.
        if y != old_y:
            adjustment = self.scrolled_window.get_vadjustment()
            adjustment.set_value(adjustment.get_value() + y - old_y)
        self._hold = (model, y, True) if until_released else None

    def update_grid(
        self,
        order_token: int,
        use_ground_truth_adapter: bool = False,
        force_grid_reload_from_master: bool = False,
    ):
        if not AdapterManager.can_get_artists():
            self.spinner.hide()
            return

        force_grid_reload_from_master = (
            force_grid_reload_from_master
            or use_ground_truth_adapter
            or self.latest_applied_order_ratchet < order_token
        )
        if force_grid_reload_from_master:
            # A new list (or sort, or page size): with "All", start from the first batch.
            self.window_start = 0
            self.loaded_count = ALL_ALBUMS_BATCH

        def do_update_grid(selected_index: Optional[int]):
            if self.sort_dir == "descending" and selected_index:
                selected_index = len(self.current_models) - selected_index - 1

            self.reflow_grids(
                force_reload_from_master=force_grid_reload_from_master,
                selected_index=selected_index,
                models=self.current_models,
            )
            self.spinner.hide()

        if (
            force_grid_reload_from_master
            and not use_ground_truth_adapter
            and self.current_models
            and self.latest_applied_order_ratchet == order_token
        ):
            # Only the page (or its size, or the direction) changed: the albums are the
            # same, so don't read and wrap all of them again.
            self.emit(
                "num-pages-changed",
                self._num_pages(),
            )
            do_update_grid(
                next(
                    (
                        i
                        for i, m in enumerate(self.current_models)
                        if m.id == self.currently_selected_id
                    ),
                    None,
                )
            )
            return

        def reload_store(f: Result[Iterable[API.Album]]):
            # Don't override more recent results
            if order_token < self.latest_applied_order_ratchet:
                return
            self.latest_applied_order_ratchet = order_token

            is_partial = False
            try:
                albums = list(f.result())
            except CacheMissError as e:
                albums = cast(Optional[List[API.Album]], e.partial_data) or []
                is_partial = True
            except Exception as e:
                if self.error_dialog:
                    self.spinner.hide()
                    return
                # TODO (#122): make this non-modal
                self.error_dialog = Gtk.MessageDialog(
                    transient_for=self.get_toplevel(),
                    message_type=Gtk.MessageType.ERROR,
                    buttons=Gtk.ButtonsType.OK,
                    text="Failed to retrieve albums",
                )
                self.error_dialog.format_secondary_markup(
                    # TODO (#204) make this error better.
                    f"Getting albums by {self.current_query.type} failed due to the "
                    f"following error\n\n{e}"
                )
                logging.exception("Failed to retrieve albums")
                self.error_dialog.run()
                self.error_dialog.destroy()
                self.error_dialog = None
                self.spinner.hide()
                return

            for c in self.error_container.get_children():
                self.error_container.remove(c)
            if is_partial and (
                len(albums) == 0 or self.current_query.type != AlbumSearchQuery.Type.RANDOM
            ):
                load_error = LoadError(
                    "Album list",
                    "load albums",
                    has_data=albums is not None and len(albums) > 0,
                    offline_mode=self.offline_mode,
                )
                self.error_container.pack_start(load_error, True, True, 0)
                self.error_container.show_all()
            else:
                self.error_container.hide()

            selected_index = None
            self.current_models = []
            for i, album in enumerate(albums):
                model = AlbumsGrid._AlbumModel(album)

                if model.id == self.currently_selected_id:
                    selected_index = i

                self.current_models.append(model)

            self.emit(
                "num-pages-changed",
                self._num_pages(),
            )
            do_update_grid(selected_index)

        if force_grid_reload_from_master:
            albums_result = AdapterManager.get_albums(
                self.current_query, use_ground_truth_adapter=use_ground_truth_adapter
            )
            if albums_result.data_is_available:
                # Don't idle add if the data is already available.
                albums_result.add_done_callback(reload_store)
            else:
                self.spinner.show()
                albums_result.add_done_callback(lambda f: GLib.idle_add(reload_store, f))
        else:
            selected_index = None
            for i, album in enumerate(self.current_models):
                if album.id == self.currently_selected_id:
                    selected_index = i
            self.emit(
                "num-pages-changed",
                self._num_pages(),
            )
            do_update_grid(selected_index)

    # Event Handlers
    # =========================================================================
    def on_child_activated(self, flowbox: Gtk.FlowBox, child: Gtk.Widget):
        click_top = flowbox == self.grid_top
        selected_index = child.get_index()

        if click_top:
            page_offset = self._window_offset()
            if self.currently_selected_index is not None and (
                selected_index == self.currently_selected_index - page_offset
            ):
                self.emit("cover-clicked", None)
            else:
                self.emit("cover-clicked", self.list_store_top[selected_index].id)
        else:
            self.emit("cover-clicked", self.list_store_bottom[selected_index].id)

    def _on_album_starred(self, _: Any, starred: bool, model: "AlbumsGrid._AlbumModel"):
        # Also on the model, so that a tile rebuilt later (moving between the grids)
        # shows the new state too.
        model.album.starred = datetime.datetime.now().astimezone() if starred else None
        if model.star_icon:
            model.star_icon.set_visible(starred)

    def on_grid_resize(self, flowbox: Gtk.FlowBox, rect: Gdk.Rectangle):
        # TODO (#124): this doesn't work at all consistency, especially with themes that
        #              add extra padding.
        # 200     + (10      * 2) + (5      * 2) = 230
        # picture + (padding * 2) + (margin * 2)
        # The first resize can arrive before the grid has any width; never go below 1.
        new_items_per_row = max(1, min((rect.width // 230), 7))
        if new_items_per_row != self.items_per_row:
            self.items_per_row = new_items_per_row
            self.detail_box_inner.set_size_request(self.items_per_row * 230 - 10, -1)

            # The albums are the same; only where the fold between the two grids sits can
            # change. Rebuilding every tile here made the first page appear twice.
            self.reflow_grids(
                force_reload_from_master=False,
                selected_index=self.currently_selected_index,
            )

    def _on_detail_box_child_revealed(self, revealer: Gtk.Revealer, _):
        if not revealer.get_child_revealed() and self._pending_reflow is not None:
            # Closed: now open the album that was clicked (in an idle so that this
            # doesn't run inside the revealer's own property notification).
            GLib.idle_add(self._run_pending_reflow)

    def _run_pending_reflow(self) -> bool:
        if (pending := self._pending_reflow) is not None:
            self._pending_reflow = None
            self.reflow_grids(*pending)
        return False

    # Scrolling to the opened album
    # =========================================================================
    SCROLL_DURATION_US = 400_000  # a bit longer than the revealer's 250ms

    def _scroll_to_detail(self):
        """Smoothly scroll so that the opened album's cover art is in the middle."""
        self._release_hold()
        self._stop_scroll()
        adjustment = self.scrolled_window.get_vadjustment()
        self._scroll_start_time = None
        self._scroll_start_value = self._scroll_last_value = adjustment.get_value()
        self._scroll_tick_id = self.scrolled_window.add_tick_callback(self._on_scroll_tick)

    def _stop_scroll(self):
        if self._scroll_tick_id is not None:
            self.scrolled_window.remove_tick_callback(self._scroll_tick_id)
            self._scroll_tick_id = None

    def _detail_scroll_target(self, adjustment: Gtk.Adjustment) -> Optional[float]:
        children = self.detail_box_inner.get_children()
        if not children or not hasattr(children[0], "artwork"):
            return None
        artwork = children[0].artwork
        # The revealer slides its child in from above, so measure from the revealer's
        # top (which doesn't move) to where the artwork sits once it is fully open.
        revealer_pos = self.detail_box_revealer.translate_coordinates(self._scroll_content, 0, 0)
        artwork_pos = artwork.translate_coordinates(self.detail_box, 0, 0)
        height = artwork.get_allocated_height()
        if revealer_pos is None or artwork_pos is None or height <= 1:
            return None  # not laid out yet
        centre = revealer_pos[1] + artwork_pos[1] + height / 2
        target = centre - adjustment.get_page_size() / 2
        # Not into the space kept below the content while switching albums. Measured for
        # when the details are fully open: they're still growing, and aiming for the end
        # of the half-grown content made the scrolling go up and back down.
        _, detail_height = self.detail_box.get_preferred_height_for_width(
            self.detail_box.get_allocated_width()
        )
        still_to_grow = max(0, detail_height - self.detail_box_revealer.get_allocated_height())
        top = self._content_bottom() + still_to_grow - adjustment.get_page_size()
        return max(adjustment.get_lower(), min(target, top))

    def _on_scroll_tick(self, widget: Gtk.Widget, frame_clock: Gdk.FrameClock) -> bool:
        adjustment = self.scrolled_window.get_vadjustment()
        if not self.detail_box_revealer.get_reveal_child():
            self._scroll_tick_id = None
            return False  # closed again

        if abs(adjustment.get_value() - self._scroll_last_value) > 1:
            # Someone else (the user) scrolled in the meantime: leave them to it.
            self._scroll_tick_id = None
            return False

        target = self._detail_scroll_target(adjustment)
        if target is None:
            return True

        now = frame_clock.get_frame_time()
        if self._scroll_start_time is None:
            self._scroll_start_time = now
        settings = Gtk.Settings.get_default()
        animate = settings is None or settings.props.gtk_enable_animations
        elapsed = now - self._scroll_start_time
        t = min(1.0, elapsed / self.SCROLL_DURATION_US) if animate else 1.0
        eased = 1 - (1 - t) ** 3  # ease-out
        start = self._scroll_start_value
        adjustment.set_value(start + (target - start) * eased)
        self._scroll_last_value = adjustment.get_value()

        # The revealer is still growing until child-revealed; keep following it.
        if t >= 1 and self.detail_box_revealer.get_child_revealed():
            self._scroll_tick_id = None
            self._give_back_height()
            return False
        return True

    # Helper Methods
    # =========================================================================
    def _make_label(self, text: str, name: str) -> Gtk.Label:
        return Gtk.Label(
            name=name,
            label=text,
            tooltip_text=text,
            ellipsize=Pango.EllipsizeMode.END,
            max_width_chars=22,
            halign=Gtk.Align.START,
        )

    def _create_cover_art_widget(self, item: _AlbumModel) -> Gtk.Box:
        widget_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        # Cover art image
        artwork = SpinnerImage(
            loading=False,
            image_name="grid-artwork",
            spinner_name="grid-artwork-spinner",
            image_size=200,
        )
        widget_box.pack_start(artwork, False, False, 0)

        # Header for the widget: the title, with a star after it if the album is starred.
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, name="grid-header")
        header.pack_start(self._make_label(item.album.name, "grid-header-label"), False, True, 0)
        item.star_icon = Gtk.Image.new_from_icon_name("starred-symbolic", Gtk.IconSize.MENU)
        item.star_icon.set_name("grid-starred-icon")
        item.star_icon.set_pixel_size(12)
        item.star_icon.set_tooltip_text("Starred")
        item.star_icon.set_no_show_all(True)
        item.star_icon.set_visible(item.album.starred is not None)
        header.pack_start(item.star_icon, False, False, 0)
        widget_box.pack_start(header, False, False, 0)

        # Extra info for the widget
        info_text = util.dot_join(
            item.album.artist.name if item.album.artist else "-", item.album.year
        )
        if info_text:
            info_label = self._make_label(info_text, "grid-info-label")
            widget_box.pack_start(info_label, False, False, 0)

        # Download the cover art.
        def on_artwork_downloaded(filename: Result[str]):
            # Decoded off the main thread: tiles are made in batches of 50 (with "All",
            # while scrolling), and decoding them all here made the scrolling stutter.
            artwork.set_from_file_async(filename.result())
            artwork.set_loading(False)

        cover_art_filename_future = AdapterManager.get_cover_art_uri(item.album.cover_art, "file")
        if cover_art_filename_future.data_is_available:
            on_artwork_downloaded(cover_art_filename_future)
        else:
            artwork.set_loading(True)
            cover_art_filename_future.add_done_callback(
                lambda f: GLib.idle_add(on_artwork_downloaded, f)
            )

        widget_box.show_all()
        return widget_box

    def reflow_grids(
        self,
        force_reload_from_master: bool = False,
        selected_index: int | None = None,
        models: List[_AlbumModel] | None = None,
    ):
        # Calculate the page that the currently_selected_index is in. If it's a
        # different page, then update the window. With "All", show albums up to it.
        if selected_index is not None:
            if self.page_size == ALL_ALBUMS:
                end = self.window_start + self.loaded_count
                if not self.window_start <= selected_index < end:
                    # Show the albums around it instead (starting on a whole row).
                    start = max(0, selected_index - MAX_LOADED_ALBUMS // 2)
                    per_row = max(1, self.items_per_row)
                    self.window_start = (start // per_row) * per_row
                    self.loaded_count = min(
                        MAX_LOADED_ALBUMS, len(self.current_models) - self.window_start
                    )
                    force_reload_from_master = True
            else:
                page_of_selected_index = selected_index // self.page_size
                if page_of_selected_index != self.page:
                    self.emit("refresh-window", {"album_page": page_of_selected_index}, False)
                    return

        if (
            not force_reload_from_master
            and selected_index is not None
            and selected_index != self.currently_selected_index
            and (self.detail_box_revealer.get_reveal_child() or self._pending_reflow is not None)
        ):
            # Another album's details are open: close them first. The rest of this reflow
            # (moving the fold, opening the new album's details) runs once they are closed,
            # see _on_detail_box_child_revealed. Later calls replace the pending reflow.
            self._pending_reflow = (force_reload_from_master, selected_index, models)
            # Keep the clicked album where it is while the open one closes above it (the
            # albums below it would move up), until it opens and scrolls into view.
            ordered = self._ordered(models or self.current_models)
            if 0 <= selected_index < len(ordered):
                self._hold_in_place(ordered[selected_index], until_released=True)
            self._keep_height()
            self.detail_box_revealer.set_reveal_child(False)
            return
        self._pending_reflow = None
        # Nothing is closing any more (the clicked album now stays in place by itself).
        self._release_hold()

        page_offset = self._window_offset()

        # Calculate the look-at window.
        if models:
            window = self._window(models)
        else:
            window = list(self.list_store_top) + list(self.list_store_bottom)

        # Determine where the cuttoff is between the top and bottom grids.
        entries_before_fold = self._window_size()
        if selected_index is not None and self.items_per_row:
            relative_selected_index = selected_index - page_offset
            entries_before_fold = (
                (relative_selected_index // self.items_per_row) + 1
            ) * self.items_per_row

        # Unreveal the current album details first
        if selected_index is None:
            self.detail_box_revealer.set_reveal_child(False)

        if force_reload_from_master:
            # Just remove everything and re-add all of the items. It's not worth trying
            # to diff in this case.
            self.list_store_top.splice(
                0,
                len(self.list_store_top),
                window[:entries_before_fold],
            )
            self.list_store_bottom.splice(
                0,
                len(self.list_store_bottom),
                window[entries_before_fold:],
            )
        elif selected_index or entries_before_fold != self._window_size():
            # This case handles when the selection changes and the entries need to be
            # re-allocated to the top and bottom grids
            # Move entries between the two stores.
            top_store_len = len(self.list_store_top)
            bottom_store_len = len(self.list_store_bottom)
            diff = abs(entries_before_fold - top_store_len)

            if diff > 0:
                if entries_before_fold - top_store_len > 0:
                    # Move entries from the bottom store.
                    self.list_store_top.splice(top_store_len, 0, self.list_store_bottom[:diff])
                    self.list_store_bottom.splice(0, min(diff, bottom_store_len), [])
                else:
                    # Move entries to the bottom store.
                    self.list_store_bottom.splice(0, 0, self.list_store_top[-diff:])
                    self.list_store_top.splice(top_store_len - diff, diff, [])

        if selected_index is not None:
            relative_selected_index = selected_index - page_offset
            to_select = self.grid_top.get_child_at_index(relative_selected_index)
            if not to_select:
                return
            self.grid_top.select_child(to_select)

            if self.currently_selected_index == selected_index:
                return

            for c in self.detail_box_inner.get_children():
                self.detail_box_inner.remove(c)

            model = self.list_store_top[relative_selected_index]
            # This may run after the window update that would otherwise pass the app
            # config on (see _pending_reflow), so the details need the offline mode now.
            detail_element = AlbumWithSongs(
                model.album,
                cover_art_size=300,
                offline_mode=self.offline_mode,
                current_song_id=self.current_song_id,
            )
            detail_element.connect(
                "song-clicked",
                lambda _, *args: self.emit("song-clicked", *args),
            )
            detail_element.connect("song-selected", lambda *a: None)
            detail_element.connect("album-starred", self._on_album_starred, model)

            self.detail_box_inner.pack_start(detail_element, True, True, 0)
            self.detail_box_inner.show_all()
            self.detail_box_revealer.set_reveal_child(True)
            self._scroll_to_detail()
        else:
            self.grid_top.unselect_all()
            self.grid_bottom.unselect_all()

        self.currently_selected_index = selected_index
