"""
The Songs tab: the whole song library, mirrored in the cache by the Sync button, shown in a
sortable table. Nothing here ever fetches from the server on its own.
"""

import logging
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from gi.repository import Gdk, GLib, GObject, Gtk, Pango

from ..adapters import (
    AdapterManager,
    CacheMissError,
    LibrarySong,
    Result,
    SongCacheStatus,
    SongQuery,
)
from ..adapters.api_objects import Song
from ..config import AppConfiguration
from ..ui import util
from ..ui.common import IconButton, LoadError


@dataclass(frozen=True)
class SongColumn:
    """One column of the song table. ``name`` doubles as the sort column of the query."""

    name: str
    title: str
    label: str = ""  # for the column picker; the title if empty
    kind: str = "text"  # "text", "icon" (an icon name) or "toggle" (a check box)
    default_visible: bool = False
    sortable: bool = True
    align: float = 0.0
    width: Optional[int] = None  # None: the column takes the remaining space
    bold: bool = False


COLUMNS: List[SongColumn] = [
    SongColumn(
        "checked", "", "Select", kind="toggle", default_visible=True, sortable=False, width=30
    ),
    SongColumn("play_count", "PLAYS", "Plays", default_visible=True, align=1, width=60),
    SongColumn("starred", "", "Starred", kind="icon", default_visible=True, width=30),
    SongColumn("title", "TITLE", "Title", default_visible=True, bold=True),
    SongColumn("duration", "DURATION", "Duration", default_visible=True, align=1, width=80),
    SongColumn("user_rating", "RATING", "Rating", default_visible=True, align=0.5, width=90),
    SongColumn("album", "ALBUM", "Album", default_visible=True),
    SongColumn("artist", "ARTIST", "Artist", default_visible=True),
    SongColumn("created", "ADDED", "Date added", default_visible=True, width=130),
    SongColumn("cache_status", "", "Download status", kind="icon", sortable=False, width=30),
    SongColumn("album_artist", "ALBUM ARTIST", "Album artist"),
    SongColumn("genre", "GENRE", "Genre"),
    SongColumn("track", "#", "Track number", align=1, width=45),
    SongColumn("disc_number", "DISC", "Disc number", align=1, width=50),
    SongColumn("year", "YEAR", "Year", align=1, width=60),
    SongColumn("bit_rate", "BIT RATE", "Bit rate", align=1, width=80),
    SongColumn("suffix", "FORMAT", "Format", width=70),
    SongColumn("size", "SIZE", "Size", align=1, width=80),
    SongColumn("played", "LAST PLAYED", "Last played", width=130),
]
DEFAULT_COLUMNS = [c.name for c in COLUMNS if c.default_visible]

Preset = SongQuery.Preset
# id, label, preset, and the sort it starts with (the user can re-sort afterwards).
PRESETS = [
    ("all", "All songs", Preset.ALL, "created", True),
    ("starred", "Favorites", Preset.STARRED, "artist", False),
    ("recently_added", "Recently added", Preset.RECENTLY_ADDED, "created", True),
    ("most_played", "Most played", Preset.MOST_PLAYED, "play_count", True),
    ("top_rated", "Top rated", Preset.TOP_RATED, "user_rating", True),
    ("year_range", "By year", Preset.YEAR_RANGE, "year", False),
]
PRESET_IDS = {preset: id_ for id_, _, preset, _, _ in PRESETS}
FILTER_COLUMNS = [
    ("any", "in anything"),
    ("title", "in title"),
    ("artist", "in artist"),
    ("album_artist", "in album artist"),
    ("album", "in album"),
    ("genre", "in genre"),
]

# Layout of the list store: a few bookkeeping values, then one value per column.
PLAYABLE, SONG_ID = 0, 1
FIRST_COLUMN = 2
STORE_TYPES = [bool, str] + [bool if c.kind == "toggle" else str for c in COLUMNS]
STORE_INDEX = {c.name: FIRST_COLUMN + i for i, c in enumerate(COLUMNS)}
CHECKED = STORE_INDEX["checked"]

CACHE_STATUS_ICONS = {
    SongCacheStatus.CACHED: "folder-download-symbolic",
    SongCacheStatus.PERMANENTLY_CACHED: "view-pin-symbolic",
    SongCacheStatus.DOWNLOADING: "emblem-synchronizing-symbolic",
}
PLAYABLE_OFFLINE = (SongCacheStatus.CACHED, SongCacheStatus.PERMANENTLY_CACHED)
# Rows per event-loop turn while filling the table: small enough for redraws in between.
FILL_CHUNK = 500
# When the app starts on this tab, the library is loaded this long after the window is on
# screen, so that the window appears and settles first.
STARTUP_LOAD_DELAY_MS = 500
# "Play from here" queues the songs after the chosen one in the table, up to this many.
# The play queue is handled song by song elsewhere in the app (details, cover art, DBus),
# so queueing a whole library would freeze it.
MAX_QUEUE_LENGTH = 1000


def _format_datetime(value: Optional[datetime]) -> str:
    if value is None:
        return ""
    if value.tzinfo is not None:
        value = value.astimezone()
    return value.strftime("%Y-%m-%d %H:%M")


def _format_size(size: Optional[int]) -> str:
    if not size:
        return ""
    if size < 1024 * 1024:
        return "{} KiB".format(round(size / 1024))
    return "{:.1f} MiB".format(size / 1024 / 1024)


def _format_rating(rating: Optional[int]) -> str:
    if not rating:
        return ""
    return "★" * rating + "☆" * (5 - rating)


def song_row(song: LibrarySong, offline_mode: bool) -> List[Any]:
    """The list store row for a song."""
    values: Dict[str, Any] = {
        "checked": False,
        "play_count": str(song.play_count) if song.play_count else "",
        "starred": "starred-symbolic" if song.starred else "non-starred-symbolic",
        "title": song.title,
        "duration": "" if song.duration is None else util.format_song_duration(song.duration),
        "user_rating": _format_rating(song.user_rating),
        "album": song.album or "",
        "artist": song.artist or "",
        "created": _format_datetime(song.created),
        "cache_status": CACHE_STATUS_ICONS.get(song.cache_status, ""),
        "album_artist": song.album_artist or "",
        "genre": song.genre or "",
        "track": str(song.track) if song.track else "",
        "disc_number": str(song.disc_number) if song.disc_number else "",
        "year": str(song.year) if song.year else "",
        "bit_rate": f"{song.bit_rate} kbps" if song.bit_rate else "",
        "suffix": song.suffix or "",
        "size": _format_size(song.size),
        "played": _format_datetime(song.played),
    }
    playable = not offline_mode or song.cache_status in PLAYABLE_OFFLINE
    return [playable, song.id, *(values[c.name] for c in COLUMNS)]


class SongsPanel(Gtk.Box):
    """Defines the songs panel."""

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

    def __init__(self):
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.VERTICAL)
        self.query = SongQuery()
        self._syncing = False
        self._load_token = 0
        # True while the widgets are being set from the state, so that their change
        # handlers don't report the change back as the user's.
        self._updating = True
        # What the table currently shows, to know when it has to be reloaded.
        self._loaded_query: Optional[SongQuery] = None
        self._loaded_last_synced: Optional[datetime] = None
        self._loaded_offline_mode: Optional[bool] = None
        self._row_index: Dict[str, int] = {}
        self._applied_columns: Optional[List[str]] = None
        # A load requested before the panel was on screen; started once it is.
        self._pending_load: Optional[Tuple[int, SongQuery, Optional[datetime], bool]] = None
        self.connect("map", self.on_map)
        # The row and the prospective rating under the pointer, shown in the rating cell.
        self._hover_rating: Optional[Tuple[int, int]] = None

        # Action bar
        self.actionbar = Gtk.ActionBar()
        self.preset_combo = self._make_combobox(
            [(id_, label) for id_, label, _, _, _ in PRESETS], self.on_preset_changed
        )
        self.actionbar.pack_start(self.preset_combo)

        next_decade = (datetime.now().year // 10) * 10 + 10
        self.from_year_label = Gtk.Label(label="from")
        self.actionbar.pack_start(self.from_year_label)
        self.from_year_spin_button = Gtk.SpinButton.new_with_range(0, next_decade, 1)
        self.from_year_spin_button.connect("value-changed", self.on_year_changed)
        self.actionbar.pack_start(self.from_year_spin_button)
        self.to_year_label = Gtk.Label(label="to")
        self.actionbar.pack_start(self.to_year_label)
        self.to_year_spin_button = Gtk.SpinButton.new_with_range(0, next_decade, 1)
        self.to_year_spin_button.connect("value-changed", self.on_year_changed)
        self.actionbar.pack_start(self.to_year_spin_button)

        self.filter_entry = Gtk.SearchEntry(placeholder_text="Filter songs")
        self.filter_entry.set_size_request(220, -1)
        self.filter_entry.connect("search-changed", self.on_filter_changed)
        self.actionbar.pack_start(self.filter_entry)
        self.filter_column_combo = self._make_combobox(
            FILTER_COLUMNS, self.on_filter_column_changed
        )
        self.actionbar.pack_start(self.filter_column_combo)

        self.column_picker = Gtk.MenuButton(tooltip_text="Choose the columns")
        self.column_picker.add(
            Gtk.Image.new_from_icon_name("view-list-symbolic", Gtk.IconSize.BUTTON)
        )
        self.column_picker.set_popover(self._make_column_picker())
        self.actionbar.pack_start(self.column_picker)

        self.song_count_label = Gtk.Label(label="")
        self.actionbar.pack_start(self.song_count_label)

        self.sync_button = IconButton(
            "emblem-synchronizing-symbolic", "Sync the song library with the server", relief=True
        )
        self.sync_button.connect("clicked", self.on_sync_clicked)
        self.actionbar.pack_end(self.sync_button)
        self.last_synced_label = Gtk.Label(label="")
        self.last_synced_label.get_style_context().add_class("dim-label")
        self.actionbar.pack_end(self.last_synced_label)
        self.spinner = Gtk.Spinner()
        self.actionbar.pack_end(self.spinner)
        self.progress_label = Gtk.Label(label="")
        self.actionbar.pack_end(self.progress_label)
        self.add(self.actionbar)

        # Content: a message before the first sync, the table, or an error.
        self.content = Gtk.Stack()
        self.empty_label = Gtk.Label(justify=Gtk.Justification.CENTER)
        self.empty_label.set_line_wrap(True)
        self.content.add_named(self.empty_label, "empty")
        self.error_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.content.add_named(self.error_container, "error")

        self.store = Gtk.ListStore(*STORE_TYPES)
        self.tree = Gtk.TreeView(model=self.store, fixed_height_mode=True, enable_search=True)
        self.tree.set_search_column(STORE_INDEX["title"])
        selection = self.tree.get_selection()
        selection.set_mode(Gtk.SelectionMode.MULTIPLE)
        selection.set_select_function(lambda _, model, path, current: model[path[0]][PLAYABLE])
        self.columns: Dict[str, Gtk.TreeViewColumn] = {}
        for column in COLUMNS:
            self.columns[column.name] = self._make_column(column)
            self.tree.append_column(self.columns[column.name])
        self.tree.connect("row-activated", self.on_song_activated)
        self.tree.connect("button-press-event", self.on_song_button_press)
        self.tree.connect("columns-changed", self.on_columns_changed)
        self.tree.add_events(Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        self.tree.connect("motion-notify-event", self.on_motion)
        self.tree.connect("leave-notify-event", self.on_leave)

        scroll = Gtk.ScrolledWindow()
        scroll.add(self.tree)
        self.content.add_named(scroll, "table")
        self.pack_start(self.content, True, True, 0)
        self._show_sort_indicator()
        self._updating = False

    @staticmethod
    def _make_combobox(
        items: List[Tuple[str, str]], on_change: Callable[[Gtk.ComboBox], None]
    ) -> Gtk.ComboBox:
        store = Gtk.ListStore(str, str)
        for item in items:
            store.append(item)
        combo = Gtk.ComboBox.new_with_model(store)
        combo.set_id_column(0)
        renderer = Gtk.CellRendererText()
        combo.pack_start(renderer, True)
        combo.add_attribute(renderer, "text", 1)
        combo.connect("changed", on_change)
        return combo

    def _make_column_picker(self) -> Gtk.Popover:
        popover = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin=10, spacing=2)
        self.column_checks: Dict[str, Gtk.CheckButton] = {}
        for column in COLUMNS:
            check = Gtk.CheckButton(label=column.label or column.title)
            check.set_active(column.default_visible)
            check.connect("toggled", self.on_column_toggled)
            self.column_checks[column.name] = check
            box.add(check)
        reset = Gtk.Button(label="Reset to defaults", margin_top=8)
        reset.connect("clicked", self.on_reset_columns)
        box.add(reset)
        box.show_all()
        popover.add(box)
        return popover

    def _make_column(self, column: SongColumn) -> Gtk.TreeViewColumn:
        index = STORE_INDEX[column.name]
        if column.kind == "toggle":
            toggle = Gtk.CellRendererToggle(activatable=True)
            toggle.connect("toggled", self.on_check_toggled)
            tree_column = Gtk.TreeViewColumn(column.title, toggle, active=index)
        elif column.kind == "icon":
            icon = Gtk.CellRendererPixbuf()
            tree_column = Gtk.TreeViewColumn(
                column.title, icon, icon_name=index, sensitive=PLAYABLE
            )
        else:
            text = Gtk.CellRendererText(
                xalign=column.align,
                weight=Pango.Weight.BOLD if column.bold else Pango.Weight.NORMAL,
                ellipsize=Pango.EllipsizeMode.END,
            )
            text.set_fixed_size(-1, 35)
            tree_column = Gtk.TreeViewColumn(column.title, text, text=index, sensitive=PLAYABLE)
            tree_column.set_alignment(column.align)
            if column.name == "user_rating":
                self.rating_renderer = text
                tree_column.set_cell_data_func(text, self._rating_cell_data)

        # Fixed sizing is what makes the fixed-row-height table fast for 20k rows.
        tree_column.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
        tree_column.set_fixed_width(column.width or 150)
        tree_column.set_expand(column.width is None)
        tree_column.set_resizable(column.kind == "text")
        tree_column.set_reorderable(True)
        tree_column.set_visible(column.default_visible)
        if column.sortable:
            tree_column.set_clickable(True)
            tree_column.connect("clicked", self.on_column_clicked, column.name)
        return tree_column

    # Updating
    # =========================================================================
    def update(self, app_config: AppConfiguration, force: bool = False):
        if self.offline_mode != app_config.offline_mode:
            self.tree.get_selection().unselect_all()
        self.offline_mode = app_config.offline_mode

        self._updating = True
        try:
            self.query = app_config.state.song_query
            self._apply_query_to_widgets()
            self._apply_columns(app_config.state.song_columns)
        finally:
            self._updating = False

        can_sync = AdapterManager.can_sync_song_library()
        self.sync_button.set_sensitive(can_sync and not self.offline_mode and not self._syncing)
        last_synced = AdapterManager.song_library_last_synced() if can_sync else None
        self._update_last_synced_label(last_synced)

        if last_synced is None:
            self.empty_label.set_text(
                "The song library has not been synced yet.\n"
                "Press Sync to fetch the list of songs from the server."
                if can_sync
                else "This music source cannot list its songs."
            )
            self.content.set_visible_child_name("empty")
            self.song_count_label.set_text("")
            return

        needs_reload = (
            force
            or self._loaded_query != self.query
            or self._loaded_last_synced != last_synced
            or self._loaded_offline_mode != self.offline_mode
        )
        if needs_reload and not self._syncing:
            self._reload(last_synced)

    def _apply_query_to_widgets(self):
        query = self.query
        self.preset_combo.set_active_id(PRESET_IDS[query.preset])
        by_year = query.preset == Preset.YEAR_RANGE
        for widget in (
            self.from_year_label,
            self.from_year_spin_button,
            self.to_year_label,
            self.to_year_spin_button,
        ):
            widget.set_visible(by_year)
        self.from_year_spin_button.set_value(query.year_range[0])
        self.to_year_spin_button.set_value(query.year_range[1])
        if self.filter_entry.get_text() != query.filter_text:
            self.filter_entry.set_text(query.filter_text)
        self.filter_column_combo.set_active_id(query.filter_column)
        self._show_sort_indicator()

    def _apply_columns(self, names: Optional[List[str]]):
        """Shows the named columns in that order (``None``: the defaults)."""
        visible = [name for name in (names or DEFAULT_COLUMNS) if name in self.columns]
        self._applied_columns = names
        for name, tree_column in self.columns.items():
            tree_column.set_visible(name in visible)
            self.column_checks[name].set_active(name in visible)
        if self.visible_column_names() != visible:
            previous = None
            for name in visible:
                self.tree.move_column_after(self.columns[name], previous)
                previous = self.columns[name]

    def visible_column_names(self) -> List[str]:
        by_widget = {tree_column: name for name, tree_column in self.columns.items()}
        return [by_widget[c] for c in self.tree.get_columns() if c.get_visible()]

    def _update_last_synced_label(self, last_synced: Optional[datetime]):
        self.last_synced_label.set_text(
            f"Last synced {_format_datetime(last_synced)}" if last_synced else "Never synced"
        )

    def _reload(self, last_synced: Optional[datetime]):
        self._load_token += 1
        load = (self._load_token, self.query, last_synced, self.offline_mode)
        if self.get_mapped():
            self._start_load(*load)
        else:
            # Not on screen yet: typically at startup, when the app was left on this
            # tab. Loading now would make the window slow to appear; see on_map.
            self._pending_load = load

    def on_map(self, *_: Any):
        if self._pending_load is None:
            return
        load = self._pending_load
        self._pending_load = None

        def start() -> bool:
            if load[0] == self._load_token:
                self._start_load(*load)
            return False

        GLib.timeout_add(STARTUP_LOAD_DELAY_MS, start, priority=GLib.PRIORITY_LOW)

    def _start_load(
        self,
        token: int,
        query: SongQuery,
        last_synced: Optional[datetime],
        offline_mode: bool,
    ):
        self.spinner.start()

        def on_done(result: Result):
            try:
                # Rows are built here, off the UI thread; only the table fill is on it.
                rows = [song_row(song, offline_mode) for song in result.result()]
            except CacheMissError:
                GLib.idle_add(self._on_load_failed, token, None)
                return
            except Exception as e:
                logging.exception("Failed to load the song library")
                GLib.idle_add(self._on_load_failed, token, e)
                return
            GLib.idle_add(self._fill, token, rows, query, last_synced, offline_mode)

        AdapterManager.get_song_library(query).add_done_callback(on_done)

    def _on_load_failed(self, token: int, error: Optional[Exception]):
        if token != self._load_token:
            return
        self.spinner.stop()
        if error is None:  # never synced
            self.content.set_visible_child_name("empty")
            return
        self._show_error("load the song library")

    def _fill(
        self,
        token: int,
        rows: List[List[Any]],
        query: SongQuery,
        last_synced: Optional[datetime],
        offline_mode: bool,
    ):
        """Fills a fresh, detached list store in chunks so the window stays responsive."""
        store = Gtk.ListStore(*STORE_TYPES)
        position = 0

        def append_chunk() -> bool:
            nonlocal position
            if token != self._load_token:
                return False
            for row in rows[position : position + FILL_CHUNK]:
                store.append(row)
            position += FILL_CHUNK
            if position < len(rows):
                return True  # come back for the next chunk

            self.store = store
            self.tree.set_model(store)
            self._show_sort_indicator()  # replacing the model clears the header arrows
            self._row_index = {row[SONG_ID]: i for i, row in enumerate(rows)}
            self._loaded_query = query
            self._loaded_last_synced = last_synced
            self._loaded_offline_mode = offline_mode
            count = format(len(rows), ",")
            self.song_count_label.set_text(f"{count} {util.pluralize('song', len(rows))}")
            self.content.set_visible_child_name("table")
            self.spinner.stop()
            return False

        GLib.idle_add(append_chunk)

    def _show_error(self, action: str):
        for child in self.error_container.get_children():
            self.error_container.remove(child)
        error = LoadError(
            "Song library",
            action,
            has_data=self._loaded_last_synced is not None,
            offline_mode=self.offline_mode,
        )
        self.error_container.pack_start(error, True, True, 0)
        self.error_container.show_all()
        self.content.set_visible_child_name("error")

    def refresh_cache_status(self, song_id: str):
        """Updates the download status icon of one song in place."""
        index = self._row_index.get(song_id)
        if index is None:
            return
        status = AdapterManager.get_cached_statuses([song_id])[0]
        self.store[index][STORE_INDEX["cache_status"]] = CACHE_STATUS_ICONS.get(status, "")
        self.store[index][PLAYABLE] = not self.offline_mode or status in PLAYABLE_OFFLINE

    # Sorting
    # =========================================================================
    def _show_sort_indicator(self):
        for name, tree_column in self.columns.items():
            active = name == self.query.sort_column
            tree_column.set_sort_indicator(active)
            if active:
                tree_column.set_sort_order(
                    Gtk.SortType.DESCENDING
                    if self.query.sort_descending
                    else Gtk.SortType.ASCENDING
                )

    def _change_query(self, **changes: Any):
        """Reports a changed query; the app stores it and updates this panel."""
        if self._updating:
            return
        self.emit("refresh-window", {"song_query": replace(self.query, **changes)}, False)

    def on_column_clicked(self, _: Gtk.TreeViewColumn, name: str):
        descending = name == self.query.sort_column and not self.query.sort_descending
        self._change_query(sort_column=name, sort_descending=descending)

    def on_preset_changed(self, combo: Gtk.ComboBox):
        preset_id = combo.get_active_id()
        for id_, _, preset, sort_column, sort_descending in PRESETS:
            if id_ == preset_id and preset != self.query.preset:
                self._change_query(
                    preset=preset, sort_column=sort_column, sort_descending=sort_descending
                )

    def on_year_changed(self, _: Gtk.SpinButton):
        year_range = (
            int(self.from_year_spin_button.get_value()),
            int(self.to_year_spin_button.get_value()),
        )
        if year_range != self.query.year_range:
            self._change_query(year_range=year_range)

    def on_filter_changed(self, entry: Gtk.SearchEntry):
        if entry.get_text() != self.query.filter_text:
            self._change_query(filter_text=entry.get_text())

    def on_filter_column_changed(self, combo: Gtk.ComboBox):
        if (column := combo.get_active_id()) and column != self.query.filter_column:
            self._change_query(filter_column=column)

    # Columns
    # =========================================================================
    def _change_columns(self, names: Optional[List[str]]):
        if self._updating:
            return
        self.emit("refresh-window", {"song_columns": names}, False)

    def on_column_toggled(self, check: Gtk.CheckButton):
        if self._updating:
            return
        checked = {name for name, c in self.column_checks.items() if c.get_active()}
        # Keep the current order for the visible ones, append newly checked ones.
        names = [n for n in self.visible_column_names() if n in checked]
        names += [c.name for c in COLUMNS if c.name in checked and c.name not in names]
        self._change_columns(names)

    def on_reset_columns(self, _: Gtk.Button):
        self.column_picker.get_popover().popdown()
        self._change_columns(None)

    def on_columns_changed(self, _: Gtk.TreeView):
        # Fired for every column added at construction and for header drags.
        if self._updating or len(self.tree.get_columns()) < len(COLUMNS):
            return
        names = self.visible_column_names()
        if names != (self._applied_columns or DEFAULT_COLUMNS):
            self._change_columns(names)

    # Syncing
    # =========================================================================
    def on_sync_clicked(self, _: Any):
        self.start_sync()

    def start_sync(self):
        """Syncs the song library, showing progress; no-op if one is already running."""
        if self._syncing or self.offline_mode or not AdapterManager.can_sync_song_library():
            return
        self._syncing = True
        self._load_token += 1  # abandon a fill in progress; the sync reloads anyway
        self.sync_button.set_sensitive(False)
        self.spinner.start()
        self.progress_label.set_text("Syncing…")
        for child in self.error_container.get_children():
            self.error_container.remove(child)

        def on_progress(done: int, total: Optional[int]):
            GLib.idle_add(self._show_sync_progress, done, total)

        AdapterManager.sync_song_library(on_progress).add_done_callback(
            lambda result: GLib.idle_add(self._on_sync_done, result)
        )

    def _show_sync_progress(self, done: int, total: Optional[int]):
        if not self._syncing:
            return
        progress = format(done, ",")
        if total:
            progress += " / " + format(total, ",")
        self.progress_label.set_text(f"Syncing… {progress}")

    def _on_sync_done(self, result: Result):
        self._syncing = False
        self.spinner.stop()
        self.progress_label.set_text("")
        self.sync_button.set_sensitive(
            AdapterManager.can_sync_song_library() and not self.offline_mode
        )
        if (error := result.exception()) is not None:
            logging.error(f"Syncing the song library failed: {error}")
            self._show_error("sync the song library")
            return

        last_synced = AdapterManager.song_library_last_synced()
        self._update_last_synced_label(last_synced)
        self._reload(last_synced)

    # Event Handlers
    # =========================================================================
    def on_check_toggled(self, _: Gtk.CellRendererToggle, path: str):
        self.store[path][CHECKED] = not self.store[path][CHECKED]

    def checked_song_ids(self) -> List[str]:
        return [row[SONG_ID] for row in self.store if row[CHECKED]]

    # Playing
    # =========================================================================
    def play(self, song_ids: List[str]):
        """Replaces the play queue with the songs and plays the first one."""
        self.emit("song-clicked", 0, song_ids, {"active_playlist_id": None})

    def play_from(self, index: int):
        """Plays the song at ``index`` followed by the songs after it in the table."""
        end = min(index + MAX_QUEUE_LENGTH, len(self.store))
        self.play([self.store[i][SONG_ID] for i in range(index, end)])

    # Starring and rating
    # =========================================================================
    def toggle_starred(self, index: int):
        if not AdapterManager.can_set_song_starred():
            return
        song_id = self.store[index][SONG_ID]
        column = STORE_INDEX["starred"]
        starred = self.store[index][column] != "starred-symbolic"
        icons = {True: "starred-symbolic", False: "non-starred-symbolic"}
        self.store[index][column] = icons[starred]  # optimistic; reverted on failure

        def on_done(result: Result):
            if result.exception() is not None:
                GLib.idle_add(self._set_cell, index, song_id, column, icons[not starred])

        AdapterManager.set_song_starred(song_id, starred).add_done_callback(on_done)

    def _rating_cell_data(
        self,
        column: Gtk.TreeViewColumn,
        renderer: Gtk.CellRendererText,
        model: Gtk.TreeModel,
        tree_iter: Gtk.TreeIter,
        _: Any = None,
    ):
        """Shows the prospective rating while the pointer is over the cell."""
        if self._hover_rating is None:
            return
        row, rating = self._hover_rating
        if model.get_path(tree_iter).get_indices()[0] == row:
            renderer.props.text = "★" * rating + "☆" * (5 - rating)

    def _rating_at(self, column: Gtk.TreeViewColumn, cell_x: float) -> int:
        """The star (1 to 5) at ``cell_x`` in the rating cell, whose text is centred."""
        layout = self.tree.create_pango_layout("★★★★★")
        stars_width = layout.get_pixel_size()[0]
        xpad = self.rating_renderer.props.xpad
        start = xpad + (column.get_width() - 2 * xpad - stars_width) / 2
        return min(5, max(1, int((cell_x - start) * 5 / max(stars_width, 1)) + 1))

    def on_motion(self, tree: Gtk.TreeView, event: Any) -> bool:
        hover = None
        if hit := tree.get_path_at_pos(int(event.x), int(event.y)):
            path, column, cell_x, _ = hit
            if column is self.columns["user_rating"]:
                hover = (path.get_indices()[0], self._rating_at(column, cell_x))
        self._set_hover_rating(hover)
        return False

    def on_leave(self, tree: Gtk.TreeView, event: Any) -> bool:
        self._set_hover_rating(None)
        return False

    def _set_hover_rating(self, hover: Optional[Tuple[int, int]]):
        if hover == self._hover_rating:
            return
        previous = self._hover_rating
        self._hover_rating = hover
        for row in {h[0] for h in (previous, hover) if h is not None}:
            if row < len(self.store):
                path = Gtk.TreePath.new_from_indices([row])
                self.store.row_changed(path, self.store.get_iter(path))

    def rate(self, index: int, rating: int):
        """Rates the song 1 to 5; rating it with its current rating clears the rating."""
        if not AdapterManager.can_set_song_rating():
            return
        song_id = self.store[index][SONG_ID]
        column = STORE_INDEX["user_rating"]
        current = self.store[index][column]
        new_rating: Optional[int] = None if rating == current.count("★") else rating
        self.store[index][column] = _format_rating(new_rating)  # optimistic

        song = Song()
        song.id = song_id

        def on_done(result: Result):
            if result.exception() is not None:
                GLib.idle_add(self._set_cell, index, song_id, column, current)

        AdapterManager.set_song_rating(song, new_rating).add_done_callback(on_done)

    def _set_cell(self, index: int, song_id: str, column: int, value: Any):
        # The table may have been reloaded meanwhile; only touch the same song.
        if index < len(self.store) and self.store[index][SONG_ID] == song_id:
            self.store[index][column] = value

    def on_song_activated(self, _: Any, path: Gtk.TreePath, col: Any):
        index = path.get_indices()[0]
        if self.store[index][PLAYABLE]:
            self.play([self.store[index][SONG_ID]])

    def on_song_button_press(self, tree: Gtk.TreeView, event: Gdk.EventButton) -> bool:
        clicked_path = tree.get_path_at_pos(event.x, event.y)
        if not clicked_path:
            return False
        if event.button == 1 and event.type == Gdk.EventType.BUTTON_PRESS:
            path, column, cell_x, _ = clicked_path
            index = path.get_indices()[0]
            if column is self.columns["starred"]:
                self.toggle_starred(index)
                return True
            if column is self.columns["user_rating"]:
                # The click lands on the star that the hover preview showed.
                self.rate(index, self._rating_at(column, cell_x))
                return True
            return False
        if event.button != 3:  # Otherwise only the right click
            return False

        # The menu acts on the checked songs if there are any, else on the selection
        # (which the right-clicked song becomes a part of if it wasn't already).
        song_ids = self.checked_song_ids()
        allow_deselect = False
        if not song_ids:
            store, paths = tree.get_selection().get_selected_rows()
            if clicked_path[0] not in paths:
                paths = [clicked_path[0]]
                allow_deselect = True
            song_ids = [self.store[p][SONG_ID] for p in paths]

        # Used to adjust for the header row.
        bin_coords = tree.convert_tree_to_bin_window_coords(event.x, event.y)
        widget_coords = tree.convert_tree_to_widget_coords(event.x, event.y)

        clicked_index = clicked_path[0].get_indices()[0]
        play_from_here = Gtk.ModelButton(
            text="Play from here", sensitive=self.store[clicked_index][PLAYABLE]
        )

        util.show_song_popover(
            song_ids,
            event.x,
            event.y + abs(bin_coords.by - widget_coords.wy),
            tree,
            self.offline_mode,
            on_download_state_change=lambda song_id: GLib.idle_add(
                self.refresh_cache_status, song_id
            ),
            on_remove_downloads_click=(
                lambda: self.offline_mode and tree.get_selection().unselect_all()
            ),
            extra_menu_items=[(play_from_here, lambda _: self.play_from(clicked_index))],
        )

        # If the click was on a selected row, don't deselect anything.
        return not allow_deselect
