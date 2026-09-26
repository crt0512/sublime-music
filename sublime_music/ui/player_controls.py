import copy
import math
from datetime import timedelta
from functools import partial
from typing import Any, Callable, Dict, Optional, Set, Tuple

import bleach
from gi.repository import Gdk, GdkPixbuf, GLib, GObject, Gtk, Pango

from ..adapters import AdapterManager, Result, SongCacheStatus
from ..adapters.api_objects import Song
from ..config import AppConfiguration
from ..util import resolve_path
from . import util
from .common import IconButton, IconToggleButton, RatingButtonBox, SpinnerImage
from .common.rating_button import RatingButton
from .state import RepeatType


def format_and_bit_rate(song: Song) -> str:
    """
    >>> from types import SimpleNamespace as S
    >>> format_and_bit_rate(S(suffix="flac", bit_rate=1024))
    'FLAC  •  1024 kbps'
    >>> format_and_bit_rate(S(suffix=None, bit_rate=None))
    ''
    """
    suffix, bit_rate = getattr(song, "suffix", None), getattr(song, "bit_rate", None)
    return util.dot_join(
        suffix.upper() if suffix else None, f"{bit_rate} kbps" if bit_rate else None
    )


SCALE_THUMB_CLICK_TOLERANCE_PX = 12


class PlayerControls(Gtk.ActionBar):
    """
    Defines the player controls panel that appears at the bottom of the window.
    """

    __gsignals__ = {
        "song-scrub": (GObject.SignalFlags.RUN_FIRST, GObject.TYPE_NONE, (float,)),
        "volume-change": (GObject.SignalFlags.RUN_FIRST, GObject.TYPE_NONE, (float,)),
        "device-update": (GObject.SignalFlags.RUN_FIRST, GObject.TYPE_NONE, (str,)),
        "song-starred": (GObject.SignalFlags.RUN_FIRST, GObject.TYPE_NONE, (bool,)),
        "song-clicked": (
            GObject.SignalFlags.RUN_FIRST,
            GObject.TYPE_NONE,
            (int, object, object),
        ),
        "song-rated": (GObject.SignalFlags.RUN_FIRST, GObject.TYPE_NONE, ()),
        "songs-removed": (GObject.SignalFlags.RUN_FIRST, GObject.TYPE_NONE, (object,)),
        "refresh-window": (
            GObject.SignalFlags.RUN_FIRST,
            GObject.TYPE_NONE,
            (object, bool),
        ),
    }
    editing: bool = False
    editing_play_queue_song_list: bool = False
    reordering_play_queue_song_list: bool = False
    current_song = None
    current_device = None
    current_playing_index: Optional[int] = None
    current_play_queue: Tuple[str, ...] = ()
    cover_art_update_order_token = 0
    play_queue_update_order_token = 0
    offline_mode = False
    updating_starred = False
    current_starred = False

    # Created in create_song_display(); declared here so that their types are known in
    # the methods above it.
    album_art: SpinnerImage
    album_name: Gtk.Label
    artist_name: Gtk.Label

    def __init__(self):
        Gtk.ActionBar.__init__(self)
        self.set_name("player-controls-bar")

        if AdapterManager.can_get_song_rating():
            self.create_rating_buttons()
        self.create_star_button()
        song_display = self.create_song_display()
        playback_controls = self.create_playback_controls()
        rating_play_queue_volume = self.create_rating_play_queue_volume()

        self.last_device_list_update = None

        self.pack_start(song_display)
        self.set_center_widget(playback_controls)
        self.pack_end(rating_play_queue_volume)

    connecting_to_device_token = 0
    connecting_icon_index = 0

    def update(self, app_config: AppConfiguration, force: bool = False):
        self.current_device = app_config.state.current_device
        self.update_device_list(app_config)

        duration = (
            app_config.state.current_song.duration if app_config.state.current_song else None
        )
        song_stream_cache_progress = (
            app_config.state.song_stream_cache_progress if app_config.state.current_song else None
        )
        self.update_scrubber(app_config.state.song_progress, duration, song_stream_cache_progress)

        icon = "pause" if app_config.state.playing else "start"
        self.play_button.set_icon(f"media-playback-{icon}-symbolic")
        self.play_button.set_tooltip_text("Pause" if app_config.state.playing else "Play")

        has_current_song = app_config.state.current_song is not None
        has_next_song = False
        if app_config.state.repeat_type in (
            RepeatType.REPEAT_QUEUE,
            RepeatType.REPEAT_SONG,
        ):
            has_next_song = True
        elif has_current_song:
            last_idx_in_queue = len(app_config.state.play_queue) - 1
            has_next_song = app_config.state.current_song_index < last_idx_in_queue

        # Toggle button states.
        self.repeat_button.set_action_name(None)
        self.shuffle_button.set_action_name(None)
        repeat_on = app_config.state.repeat_type in (
            RepeatType.REPEAT_QUEUE,
            RepeatType.REPEAT_SONG,
        )
        self.repeat_button.set_active(repeat_on)
        self.repeat_button.set_icon(app_config.state.repeat_type.icon)
        self.shuffle_button.set_active(app_config.state.shuffle_on)
        self.repeat_button.set_action_name("app.repeat-press")
        self.shuffle_button.set_action_name("app.shuffle-press")

        self.song_scrubber.set_sensitive(has_current_song)
        self.prev_button.set_sensitive(has_current_song)
        self.play_button.set_sensitive(has_current_song)
        self.next_button.set_sensitive(has_current_song and has_next_song)
        self.star_button.set_visible(AdapterManager.can_set_song_starred())
        self.star_button.set_sensitive(has_current_song)

        self.info_button.set_sensitive(has_current_song)
        self.info_song = app_config.state.current_song
        if not has_current_song:
            self.info_popover.popdown()
        elif self.info_popover.is_visible():
            self._fill_song_info()  # the song changed, or e.g. its star

        self.device_button.set_visible(app_config.chromecast_enabled)
        if not app_config.chromecast_enabled:
            self.device_popover.popdown()

        self.connecting_to_device = app_config.state.connecting_to_device

        def cycle_connecting(connecting_to_device_token: int):
            if (
                self.connecting_to_device_token != connecting_to_device_token
                or not self.connecting_to_device
            ):
                return
            icon = f"chromecast-connecting-{self.connecting_icon_index}-symbolic"
            self.device_button.set_icon(icon)
            self.connecting_icon_index = (self.connecting_icon_index + 1) % 3
            GLib.timeout_add(350, cycle_connecting, connecting_to_device_token)

        icon = ""
        if app_config.state.connecting_to_device:
            icon = "-connecting-0"
            self.connecting_icon_index = 0
            self.connecting_to_device_token += 1
            GLib.timeout_add(350, cycle_connecting, self.connecting_to_device_token)
        elif app_config.state.current_device != "this device":
            icon = "-connected"

        self.device_button.set_icon(f"chromecast{icon}-symbolic")

        # Volume button and slider
        if app_config.state.is_muted:
            icon_name = "muted"
        elif app_config.state.volume < 30:
            icon_name = "low"
        elif app_config.state.volume < 70:
            icon_name = "medium"
        else:
            icon_name = "high"

        self.volume_mute_toggle.set_icon(f"audio-volume-{icon_name}-symbolic")

        self.editing = True
        self.volume_slider.set_value(0 if app_config.state.is_muted else app_config.state.volume)
        self.editing = False

        # Update the current song information.
        # TODO (#126): add popup of bigger cover art photo here
        if app_config.state.current_song is not None:
            self.cover_art_update_order_token += 1
            self.update_cover_art(
                app_config.state.current_song.cover_art,
                order_token=self.cover_art_update_order_token,
            )
            if AdapterManager.can_get_song_rating():
                self.update_rating(app_config.state.current_song.user_rating)
            if AdapterManager.can_set_song_starred():
                self.update_starred(app_config.state.current_song.starred is not None)

            self.song_title.set_markup(bleach.clean(app_config.state.current_song.title))
            song_format = format_and_bit_rate(app_config.state.current_song)
            self.song_format.set_text(song_format)
            self.song_format.set_visible(bool(song_format))
            # TODO (#71): use walrus once MYPY gets its act together
            album = app_config.state.current_song.album
            artist = app_config.state.current_song.artist
            if album:
                self.album_name.set_markup(bleach.clean(album.name))
                self.artist_name.show()
            else:
                self.album_name.set_markup("")
                self.album_name.hide()
            if artist:
                self.artist_name.set_markup(bleach.clean(artist.name))
                self.artist_name.show()
            else:
                self.artist_name.set_markup("")
                self.artist_name.hide()
        else:
            # Clear out the cover art and song tite if no song
            self.album_art.set_from_file(None)
            self.album_art.set_loading(False)
            self.song_title.set_markup("")
            self.song_format.set_text("")
            self.song_format.hide()
            self.album_name.set_markup("")
            self.artist_name.set_markup("")

        self.load_play_queue_button.set_sensitive(not self.offline_mode)
        self.clear_play_queue_button.set_sensitive(len(app_config.state.play_queue) > 0)
        if app_config.state.loading_play_queue:
            self.play_queue_spinner.start()
            self.play_queue_spinner.show()
        else:
            self.play_queue_spinner.stop()
            self.play_queue_spinner.hide()

        # Short circuit if no changes to the play queue
        force |= self.offline_mode != app_config.offline_mode
        self.offline_mode = app_config.offline_mode
        if not force and (
            self.current_play_queue == app_config.state.play_queue
            and self.current_playing_index == app_config.state.current_song_index
        ):
            return
        self.current_play_queue = app_config.state.play_queue
        self.current_playing_index = app_config.state.current_song_index

        # Set the Play Queue button popup.
        play_queue_len = len(app_config.state.play_queue)
        if play_queue_len == 0:
            self.popover_label.set_markup("<b>Play Queue</b>")
        else:
            song_label = util.pluralize("song", play_queue_len)
            self.popover_label.set_markup(f"<b>Play Queue:</b> {play_queue_len} {song_label}")

        # TODO (#207) this is super freaking stupid inefficient.
        # IDEAS: batch it, don't get the queue until requested
        self.editing_play_queue_song_list = True

        new_store = []

        def calculate_label(song_details: Song) -> str:
            title = song_details.title
            # TODO (#71): use walrus once MYPY gets its act together
            # album = a.name if (a := song_details.album) else None
            # artist = a.name if (a := song_details.artist) else None
            album = song_details.album.name if song_details.album else None
            artist = song_details.artist.name if song_details.artist else None
            return bleach.clean(f"<b>{title}</b>\n{util.dot_join(album, artist)}")

        def make_idle_index_capturing_function(
            idx: int,
            order_tok: int,
            fn: Callable[[int, int, Any], None],
        ) -> Callable[[Result], None]:
            return lambda f: GLib.idle_add(fn, idx, order_tok, f.result())

        def on_cover_art_future_done(
            idx: int,
            order_token: int,
            cover_art_filename: str,
        ):
            if order_token != self.play_queue_update_order_token:
                return

            self.play_queue_store[idx][1] = cover_art_filename

        def get_cover_art_filename_or_create_future(
            cover_art_id: Optional[str], idx: int, order_token: int
        ) -> Optional[str]:
            cover_art_result = AdapterManager.get_cover_art_uri(cover_art_id, "file")
            if not cover_art_result.data_is_available:
                cover_art_result.add_done_callback(
                    make_idle_index_capturing_function(idx, order_token, on_cover_art_future_done)
                )
                return None

            # The cover art is already cached.
            return cover_art_result.result()

        def on_song_details_future_done(idx: int, order_token: int, song_details: Song):
            if order_token != self.play_queue_update_order_token:
                return

            self.play_queue_store[idx][2] = calculate_label(song_details)

            # Cover Art
            filename = get_cover_art_filename_or_create_future(
                song_details.cover_art, idx, order_token
            )
            if filename:
                self.play_queue_store[idx][1] = filename

        current_play_queue = [x[-1] for x in self.play_queue_store]
        if app_config.state.play_queue != current_play_queue:
            self.play_queue_update_order_token += 1

        song_details_results = []
        for i, (song_id, cached_status) in enumerate(
            zip(
                app_config.state.play_queue,
                AdapterManager.get_cached_statuses(app_config.state.play_queue),
            )
        ):
            song_details_result = AdapterManager.get_song_details(song_id)

            cover_art_filename = ""
            label = "\n"

            if song_details_result.data_is_available:
                # We have the details of the song already cached.
                song_details = song_details_result.result()
                label = calculate_label(song_details)

                filename = get_cover_art_filename_or_create_future(
                    song_details.cover_art, i, self.play_queue_update_order_token
                )
                if filename:
                    cover_art_filename = filename
            else:
                song_details_results.append((i, song_details_result))

            new_store.append(
                [
                    (
                        not self.offline_mode
                        or cached_status
                        in (SongCacheStatus.CACHED, SongCacheStatus.PERMANENTLY_CACHED)
                    ),
                    cover_art_filename,
                    label,
                    i == app_config.state.current_song_index,
                    song_id,
                ]
            )

        util.diff_song_store(self.play_queue_store, new_store)

        # Do this after the diff to avoid race conditions.
        for idx, song_details_result in song_details_results:
            song_details_result.add_done_callback(
                make_idle_index_capturing_function(
                    idx,
                    self.play_queue_update_order_token,
                    on_song_details_future_done,
                )
            )

        self.editing_play_queue_song_list = False

    @util.async_callback(
        partial(AdapterManager.get_cover_art_uri, scheme="file"),
        before_download=lambda self: self.album_art.set_loading(True),
        on_failure=lambda self, e: self.album_art.set_loading(False),
    )
    def update_cover_art(
        self,
        cover_art_filename: str,
        app_config: AppConfiguration,
        force: bool = False,
        order_token: int | None = None,
        is_partial: bool = False,
    ):
        if order_token != self.cover_art_update_order_token:
            return

        self.album_art.set_from_file(cover_art_filename)
        self.album_art.set_loading(False)

    def update_rating(self, rating: int | None):
        self.rating_buttons_box.rating = rating

    def update_starred(self, starred: bool):
        self.current_starred = starred
        self.star_button.set_icon("star-full" if starred else "star-empty")
        self.star_button.set_tooltip_text(
            "Unstar current song" if starred else "Star current song"
        )

    def on_star_clicked(self, button: RatingButton):
        if self.updating_starred:
            return
        starred = not self.current_starred
        self.update_starred(starred)
        self.emit("song-starred", starred)

    def on_rating_clicked(self, _, rating: int):
        if AdapterManager.can_set_song_rating():
            self.emit("song-rated")

    def on_rating_removed(self, _):
        if AdapterManager.can_set_song_rating():
            self.emit("song-rated")

    def update_scrubber(
        self,
        current: Optional[timedelta],
        duration: Optional[timedelta],
        song_stream_cache_progress: Optional[timedelta],
    ):
        if current is None or duration is None:
            self.song_duration_label.set_text("-:--")
            self.song_progress_label.set_text("-:--")
            self.song_scrubber.set_value(0)
            return

        percent_complete = current / duration * 100

        if not self.editing:
            self.song_scrubber.set_value(percent_complete)

        self.song_scrubber.set_show_fill_level(song_stream_cache_progress is not None)
        if song_stream_cache_progress is not None:
            percent_cached = song_stream_cache_progress / duration * 100
            self.song_scrubber.set_fill_level(percent_cached)

        self.song_duration_label.set_text(util.format_song_duration(duration))
        self.song_progress_label.set_text(
            util.format_song_duration(math.floor(current.total_seconds()))
        )

    def on_volume_change(self, scale: Gtk.Scale):
        if not self.editing:
            self.emit("volume-change", scale.get_value())

    def _scale_value_at_event(self, scale: Gtk.Scale, event: Gdk.EventButton) -> float:
        allocation = scale.get_allocation()
        width = max(1, allocation.width)
        x = min(max(event.x, 0), width)
        adjustment = scale.get_adjustment()
        lower = adjustment.get_lower()
        upper = adjustment.get_upper()
        return lower + (x / width) * (upper - lower)

    def _scale_current_value_x(self, scale: Gtk.Scale) -> float:
        allocation = scale.get_allocation()
        adjustment = scale.get_adjustment()
        lower = adjustment.get_lower()
        upper = adjustment.get_upper()
        if upper == lower:
            return 0
        return allocation.width * ((scale.get_value() - lower) / (upper - lower))

    def _on_scale_button_press(
        self,
        scale: Gtk.Scale,
        event: Gdk.EventButton,
        signal_name: str | None = None,
    ) -> bool:
        if event.button != 1:
            return False

        # If the click is on the thumb, let GTK handle dragging normally. Trough clicks
        # are handled here because GTK's default page-step behavior is surprising on
        # macOS: the control jumps much farther than the clicked position implies.
        if abs(event.x - self._scale_current_value_x(scale)) <= SCALE_THUMB_CLICK_TOLERANCE_PX:
            return False

        value = self._scale_value_at_event(scale, event)
        scale.set_value(value)
        if signal_name:
            self.emit(signal_name, value)
        return True

    def on_play_queue_click(self, _: Any):
        if self.play_queue_popover.is_visible():
            self.play_queue_popover.popdown()
        else:
            # TODO (#88): scroll the currently playing song into view.
            self.play_queue_popover.popup()
            self.play_queue_popover.show_all()

            # Hide the load play queue button if the adapter can't do that.
            if not AdapterManager.can_get_play_queue():
                self.load_play_queue_button.hide()

    def on_song_activated(self, t: Any, idx: Gtk.TreePath, c: Any):
        if not self.play_queue_store[idx[0]][0]:
            return
        # The song ID is in the last column of the model.
        self.emit(
            "song-clicked",
            idx.get_indices()[0],
            [m[-1] for m in self.play_queue_store],
            {"no_reshuffle": True},
        )

    _current_player_id = None
    _current_available_players: Dict[type, Set[Tuple[str, str]]] = {}

    def update_device_list(self, app_config: AppConfiguration):
        if (
            self._current_available_players == app_config.state.available_players
            and self._current_player_id == app_config.state.current_device
        ):
            return

        self._current_player_id = app_config.state.current_device
        self._current_available_players = copy.deepcopy(app_config.state.available_players)
        for c in self.device_list.get_children():
            self.device_list.remove(c)

        for i, (player_type, players) in enumerate(app_config.state.available_players.items()):
            if len(players) == 0:
                continue
            if i > 0:
                self.device_list.add(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
            self.device_list.add(
                Gtk.Label(
                    label=f"{player_type.name} Devices",
                    halign=Gtk.Align.START,
                    name="device-type-section-title",
                )
            )

            for player_id, player_name in sorted(players, key=lambda p: p[1]):
                icon = "audio-volume-high-symbolic" if player_id == self.current_device else None
                button = IconButton(icon, label=player_name)
                button.get_style_context().add_class("menu-button")
                button.connect(
                    "clicked",
                    lambda _, player_id: self.emit("device-update", player_id),
                    player_id,
                )
                self.device_list.add(button)

        self.device_list.show_all()

    info_song: Optional[Song] = None

    def on_info_click(self, _: Any):
        if self.info_popover.is_visible():
            self.info_popover.popdown()
        else:
            self._fill_song_info()
            self.info_popover.popup()
            self.info_popover.show_all()

    def _fill_song_info(self):
        for c in self.info_grid.get_children():
            self.info_grid.remove(c)
        if not (song := self.info_song):
            return

        def name(obj: Any) -> Optional[str]:
            return getattr(obj, "name", None) if obj else None

        genre = getattr(song, "genres", None) or name(song.genre)
        rows = [
            ("Title", song.title),
            ("Artist", name(song.artist)),
            ("Album", name(song.album)),
            ("Album Artist", getattr(song, "album_artist", None)),
            ("Genre", genre),
            ("Year", song.year),
            ("Track", song.track),
            ("Disc", song.disc_number),
            ("Duration", util.format_song_duration(song.duration) if song.duration else None),
            ("Format", (getattr(song, "suffix", None) or "").upper() or None),
            ("Bit Rate", f"{song.bit_rate} kbps" if getattr(song, "bit_rate", None) else None),
            ("File Size", util.format_size(song.size) or None),
            ("Rating", util.format_rating(song.user_rating) or None),
            ("Starred", util.format_datetime(song.starred) or None),
            ("Play Count", getattr(song, "play_count", None)),
            ("Last Played", util.format_datetime(getattr(song, "played", None)) or None),
            ("Added", util.format_datetime(getattr(song, "created", None)) or None),
            ("Path", song.path),
            ("ID", song.id),
        ]
        top = 0
        for label, value in rows:
            if value is None or value == "":
                continue
            key = Gtk.Label(label=label, halign=Gtk.Align.END, valign=Gtk.Align.START)
            key.get_style_context().add_class("song-info-key")
            # Selectable, so that the path or ID can be copied. Long values wrap at 45
            # characters; width_chars keeps GTK from sizing the popover as if every value
            # were squeezed to its narrowest (which makes it huge).
            text = str(value)
            value_label = Gtk.Label(
                label=text,
                halign=Gtk.Align.START,
                xalign=0,
                selectable=True,
                wrap=True,
                wrap_mode=Pango.WrapMode.WORD_CHAR,
                width_chars=min(len(text), 45),
                max_width_chars=45,
            )
            self.info_grid.attach(key, 0, top, 1, 1)
            self.info_grid.attach(value_label, 1, top, 1, 1)
            top += 1
        self.info_grid.show_all()

    def on_device_click(self, _: Any):
        if self.device_popover.is_visible():
            self.device_popover.popdown()
        else:
            self.device_popover.popup()
            self.device_popover.show_all()

    def on_play_queue_button_press(self, tree: Any, event: Gdk.EventButton) -> bool:
        if event.button == 3:  # Right click
            clicked_path = tree.get_path_at_pos(event.x, event.y)

            store, paths = tree.get_selection().get_selected_rows()
            allow_deselect = False

            def on_download_state_change(song_id: str):
                # Refresh the entire window (no force) because the song could
                # be in a list anywhere in the window.
                GLib.idle_add(lambda: self.emit("refresh-window", {}, False))

            # Use the new selection instead of the old one for calculating what
            # to do the right click on.
            if clicked_path[0] not in paths:
                paths = [clicked_path[0]]
                allow_deselect = True

            song_ids = [self.play_queue_store[p][-1] for p in paths]

            remove_text = "Remove " + util.pluralize("song", len(song_ids)) + " from queue"

            def on_remove_songs_click(_: Any):
                self.emit("songs-removed", [p.get_indices()[0] for p in paths])

            util.show_song_popover(
                song_ids,
                event.x,
                event.y,
                tree,
                self.offline_mode,
                on_download_state_change=on_download_state_change,
                extra_menu_items=[
                    (Gtk.ModelButton(text=remove_text), on_remove_songs_click),
                ],
            )

            # If the click was on a selected row, don't deselect anything.
            if not allow_deselect:
                return True

        return False

    def on_play_queue_model_row_move(self, *args):
        # If we are programatically editing the song list, don't do anything.
        if self.editing_play_queue_song_list:
            return

        # We get both a delete and insert event, I think it's deterministic
        # which one comes first, but just in case, we have this
        # reordering_play_queue_song_list flag.
        if self.reordering_play_queue_song_list:
            currently_playing_index = [
                i for i, s in enumerate(self.play_queue_store) if s[3]  # playing
            ][0]
            self.emit(
                "refresh-window",
                {
                    "current_song_index": currently_playing_index,
                    "play_queue": tuple(s[-1] for s in self.play_queue_store),
                },
                False,
            )
            self.reordering_play_queue_song_list = False
        else:
            self.reordering_play_queue_song_list = True

    def create_song_display(self) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)

        self.album_art = SpinnerImage(
            image_name="player-controls-album-artwork",
            image_size=70,
        )
        box.pack_start(self.album_art, False, False, 0)

        details_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        details_box.pack_start(Gtk.Box(), True, True, 0)

        def make_label(name: str) -> Gtk.Label:
            return Gtk.Label(
                name=name,
                halign=Gtk.Align.START,
                xalign=0,
                use_markup=True,
                ellipsize=Pango.EllipsizeMode.END,
            )

        self.song_title = make_label("song-title")
        details_box.add(self.song_title)

        # Format and bit rate, e.g. "FLAC  •  1024 kbps".
        self.song_format = make_label("song-format")
        details_box.add(self.song_format)

        self.album_name = make_label("album-name")
        details_box.add(self.album_name)

        self.artist_name = make_label("artist-name")
        details_box.add(self.artist_name)

        details_box.pack_start(Gtk.Box(), True, True, 0)
        box.pack_start(details_box, False, False, 5)

        box.pack_start(self.star_button, False, False, 0)
        self.star_button.hide()

        return box

    def create_playback_controls(self) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        # Scrubber and song progress/length labels
        scrubber_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)

        self.song_progress_label = Gtk.Label(label="-:--", xalign=1)
        self.song_progress_label.set_name("song-progress-label")
        scrubber_box.pack_start(self.song_progress_label, False, False, 5)

        self.song_scrubber = Gtk.Scale.new_with_range(
            orientation=Gtk.Orientation.HORIZONTAL, min=0, max=100, step=5
        )
        self.song_scrubber.set_name("song-scrubber")
        self.song_scrubber.set_draw_value(False)
        self.song_scrubber.set_restrict_to_fill_level(False)
        self.song_scrubber.connect(
            "button-press-event",
            self._on_scale_button_press,
            "song-scrub",
        )
        self.song_scrubber.connect("change-value", lambda s, t, v: self.emit("song-scrub", v))
        scrubber_box.pack_start(self.song_scrubber, True, True, 0)

        self.song_duration_label = Gtk.Label(label="-:--", xalign=0)
        self.song_duration_label.set_name("song-duration-label")
        scrubber_box.pack_start(self.song_duration_label, False, False, 5)

        # The action bar centres this whole widget at its natural width, so if the
        # progress label got wider or narrower as the seconds tick, every control in
        # here would shift sideways. Keep both time labels as wide as the wider one (the
        # duration, which only changes with the song). The stylesheet also asks for
        # tabular digits, so that all digits have the same width.
        self._time_label_size_group = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
        self._time_label_size_group.add_widget(self.song_progress_label)
        self._time_label_size_group.add_widget(self.song_duration_label)

        box.add(scrubber_box)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        buttons.pack_start(Gtk.Box(), True, True, 0)

        # Repeat button
        repeat_button_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.repeat_button = IconToggleButton(
            "media-playlist-repeat", "Switch between repeat modes"
        )
        self.repeat_button.set_action_name("app.repeat-press")
        repeat_button_box.pack_start(Gtk.Box(), True, True, 0)
        repeat_button_box.pack_start(self.repeat_button, False, False, 0)
        repeat_button_box.pack_start(Gtk.Box(), True, True, 0)
        buttons.pack_start(repeat_button_box, False, False, 5)

        # Previous button
        self.prev_button = IconButton(
            "media-skip-backward-symbolic",
            "Go to previous song",
            icon_size=Gtk.IconSize.LARGE_TOOLBAR,
        )
        self.prev_button.set_action_name("app.prev-track")
        buttons.pack_start(self.prev_button, False, False, 5)

        # Play button
        self.play_button = IconButton(
            "media-playback-start-symbolic",
            "Play",
            relief=True,
            icon_size=Gtk.IconSize.LARGE_TOOLBAR,
        )
        self.play_button.set_name("play-button")
        self.play_button.get_child().set_halign(Gtk.Align.CENTER)
        self.play_button.set_action_name("app.play-pause")
        buttons.pack_start(self.play_button, False, False, 0)

        # Next button
        self.next_button = IconButton(
            "media-skip-forward-symbolic",
            "Go to next song",
            icon_size=Gtk.IconSize.LARGE_TOOLBAR,
        )
        self.next_button.set_action_name("app.next-track")
        buttons.pack_start(self.next_button, False, False, 5)

        # Shuffle button
        shuffle_button_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.shuffle_button = IconToggleButton(
            "media-playlist-shuffle-symbolic", "Toggle playlist shuffling"
        )
        self.shuffle_button.set_action_name("app.shuffle-press")
        shuffle_button_box.pack_start(Gtk.Box(), True, True, 0)
        shuffle_button_box.pack_start(self.shuffle_button, False, False, 0)
        shuffle_button_box.pack_start(Gtk.Box(), True, True, 0)
        buttons.pack_start(shuffle_button_box, False, False, 5)

        buttons.pack_start(Gtk.Box(), True, True, 0)
        box.add(buttons)

        return box

    def create_rating_buttons(self):
        # Rating button
        self.rating_buttons_box = RatingButtonBox()
        self.rating_buttons_box.set_property("sensitive", True)
        self.rating_buttons_box.connect("rating-clicked", self.on_rating_clicked)
        self.rating_buttons_box.connect("rating-remove", self.on_rating_removed)

    def create_star_button(self):
        self.star_button = RatingButton(
            "star-empty",
            "Star current song",
            valign=Gtk.Align.CENTER,
            margin_left=3,
        )
        self.star_button.connect("clicked", self.on_star_clicked)

    def create_rating_play_queue_volume(self) -> Gtk.Box:
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        vbox.pack_start(Gtk.Box(), True, True, 0)
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)

        # Song info button
        self.info_button = IconButton(
            "help-about-symbolic",
            "Show information about the current song",
            icon_size=Gtk.IconSize.LARGE_TOOLBAR,
        )
        self.info_button.connect("clicked", self.on_info_click)
        box.pack_start(self.info_button, False, True, 5)

        self.info_popover = Gtk.PopoverMenu(modal=False, name="song-info-popover")
        self.info_popover.set_relative_to(self.info_button)
        info_popover_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        info_popover_box.add(
            Gtk.Label(label="<b>Song Info</b>", use_markup=True, halign=Gtk.Align.START, margin=10)
        )
        self.info_grid = Gtk.Grid(
            name="song-info-grid", column_spacing=15, row_spacing=6, margin=10, margin_top=0
        )
        info_popover_box.add(self.info_grid)
        self.info_popover.add(info_popover_box)

        # Device button (for chromecast). Hidden while Chromecast support is disabled;
        # no_show_all so that show_all() doesn't bring it back.
        self.device_button = IconButton(
            "chromecast-symbolic",
            "Show available audio output devices",
            icon_size=Gtk.IconSize.LARGE_TOOLBAR,
            no_show_all=True,
        )
        self.device_button.get_child().show_all()
        self.device_button.connect("clicked", self.on_device_click)
        box.pack_start(self.device_button, False, True, 5)

        self.device_popover = Gtk.PopoverMenu(modal=False, name="device-popover")
        self.device_popover.set_relative_to(self.device_button)

        device_popover_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            name="device-popover-box",
        )
        device_popover_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)

        self.popover_label = Gtk.Label(
            label="<b>Devices</b>",
            use_markup=True,
            halign=Gtk.Align.START,
            margin=5,
        )
        device_popover_header.add(self.popover_label)

        refresh_devices = IconButton("view-refresh-symbolic", "Refresh device list")
        refresh_devices.set_action_name("app.refresh-devices")
        device_popover_header.pack_end(refresh_devices, False, False, 0)

        device_popover_box.add(device_popover_header)

        device_list_and_loading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        self.device_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        device_list_and_loading.add(self.device_list)

        device_popover_box.pack_end(device_list_and_loading, True, True, 0)

        self.device_popover.add(device_popover_box)

        # Play Queue button
        self.play_queue_button = IconButton(
            "view-list-symbolic",
            "Open play queue",
            icon_size=Gtk.IconSize.LARGE_TOOLBAR,
        )
        self.play_queue_button.connect("clicked", self.on_play_queue_click)
        box.pack_start(self.play_queue_button, False, True, 5)

        self.play_queue_popover = Gtk.PopoverMenu(modal=False, name="up-next-popover")
        self.play_queue_popover.set_relative_to(self.play_queue_button)

        play_queue_popover_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        play_queue_popover_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)

        self.popover_label = Gtk.Label(
            label="<b>Play Queue</b>",
            use_markup=True,
            halign=Gtk.Align.START,
            margin=10,
        )
        play_queue_popover_header.add(self.popover_label)

        self.load_play_queue_button = IconButton(
            "folder-download-symbolic", "Load Queue from Server", margin=5
        )
        self.load_play_queue_button.set_action_name("app.update-play-queue-from-server")
        play_queue_popover_header.pack_end(self.load_play_queue_button, False, False, 0)

        self.clear_play_queue_button = IconButton(
            "edit-clear-all-symbolic", "Clear Queue", margin=5
        )
        self.clear_play_queue_button.set_action_name("app.clear-play-queue")
        play_queue_popover_header.pack_end(self.clear_play_queue_button, False, False, 0)

        play_queue_popover_box.add(play_queue_popover_header)

        play_queue_loading_overlay = Gtk.Overlay()
        play_queue_scrollbox = Gtk.ScrolledWindow(
            min_content_height=600,
            min_content_width=400,
        )

        self.play_queue_store = Gtk.ListStore(
            bool,  # playable
            str,  # image filename
            str,  # title, album, artist
            bool,  # playing
            str,  # song ID
        )
        self.play_queue_list = Gtk.TreeView(
            model=self.play_queue_store,
            headers_visible=False,
        )
        selection = self.play_queue_list.get_selection()
        selection.set_mode(Gtk.SelectionMode.MULTIPLE)
        selection.set_select_function(lambda _, model, path, current: model[path[0]][0])

        # Album Art column. This function defines what image to use for the play queue
        # song icon.
        def filename_to_pixbuf(
            column: Any,
            cell: Gtk.CellRendererPixbuf,
            model: Gtk.ListStore,
            tree_iter: Gtk.TreeIter,
            flags: Any,
        ):
            cell.set_property("sensitive", model.get_value(tree_iter, 0))
            filename = model.get_value(tree_iter, 1)
            if not filename:
                cell.set_property("icon_name", "")
                return

            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(filename, 50, 50, True)

            # If this is the playing song, then overlay the play icon.
            if model.get_value(tree_iter, 3):
                play_overlay_pixbuf = GdkPixbuf.Pixbuf.new_from_file(
                    str(resolve_path("ui/images/play-queue-play.png"))
                )

                # The cover keeps its aspect ratio, so it can be smaller than 50x50 in
                # one direction: fit the overlay into it, centred, or GdkPixbuf refuses
                # to draw past the cover's edge.
                width, height = pixbuf.get_width(), pixbuf.get_height()
                ow, oh = play_overlay_pixbuf.get_width(), play_overlay_pixbuf.get_height()
                scale = min(width / ow, height / oh)
                play_overlay_pixbuf.composite(
                    pixbuf,
                    0,
                    0,
                    width,
                    height,
                    (width - ow * scale) / 2,
                    (height - oh * scale) / 2,
                    scale,
                    scale,
                    GdkPixbuf.InterpType.BILINEAR,
                    200,
                )

            cell.set_property("pixbuf", pixbuf)

        renderer = Gtk.CellRendererPixbuf()
        renderer.set_fixed_size(55, 60)
        column = Gtk.TreeViewColumn("", renderer)
        column.set_cell_data_func(renderer, filename_to_pixbuf)
        column.set_resizable(True)
        self.play_queue_list.append_column(column)

        renderer = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END)
        column = Gtk.TreeViewColumn("", renderer, markup=2, sensitive=0)
        self.play_queue_list.append_column(column)

        util.make_reorderable(self.play_queue_list)
        self.play_queue_list.connect("row-activated", self.on_song_activated)
        self.play_queue_list.connect("button-press-event", self.on_play_queue_button_press)

        # Set up drag-and-drop on the song list for editing the order of the
        # playlist.
        self.play_queue_store.connect("row-inserted", self.on_play_queue_model_row_move)
        self.play_queue_store.connect("row-deleted", self.on_play_queue_model_row_move)

        play_queue_scrollbox.add(self.play_queue_list)
        play_queue_loading_overlay.add(play_queue_scrollbox)

        self.play_queue_spinner = Gtk.Spinner(
            name="play-queue-spinner",
            active=False,
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
        )
        play_queue_loading_overlay.add_overlay(self.play_queue_spinner)
        play_queue_popover_box.pack_end(play_queue_loading_overlay, True, True, 0)

        self.play_queue_popover.add(play_queue_popover_box)

        # Volume mute toggle
        self.volume_mute_toggle = IconButton("audio-volume-high-symbolic", "Toggle mute")
        self.volume_mute_toggle.set_action_name("app.mute-toggle")
        box.pack_start(self.volume_mute_toggle, False, True, 0)

        # Volume slider
        self.volume_slider = Gtk.Scale.new_with_range(
            orientation=Gtk.Orientation.HORIZONTAL, min=0, max=100, step=5
        )
        self.volume_slider.set_name("volume-slider")
        self.volume_slider.set_draw_value(False)
        self.volume_slider.connect("button-press-event", self._on_scale_button_press, None)
        self.volume_slider.connect("value-changed", self.on_volume_change)
        box.pack_start(self.volume_slider, True, True, 0)

        if AdapterManager.can_get_song_rating():
            vbox.pack_start(self.rating_buttons_box, False, True, 0)
        vbox.pack_start(box, False, True, 0)
        vbox.pack_start(Gtk.Box(), True, True, 0)
        return vbox
