import functools
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable, List, Optional, Tuple, Union

from gi.repository import Gdk, GLib, GObject, Gtk

from ..adapters import AdapterManager, CacheMissError, Result, SongCacheStatus
from ..adapters.api_objects import Playlist, Song
from ..config import AppConfiguration


def format_song_duration(duration_secs: Union[int, timedelta, None]) -> str:
    """
    Formats the song duration as mins:seconds with the seconds being
    zero-padded if necessary.

    >>> format_song_duration(80)
    '1:20'
    >>> format_song_duration(62)
    '1:02'
    >>> format_song_duration(timedelta(seconds=68.2))
    '1:08'
    >>> format_song_duration(None)
    '-:--'
    """
    if isinstance(duration_secs, timedelta):
        duration_secs = round(duration_secs.total_seconds())
    if duration_secs is None:
        return "-:--"

    duration_secs = max(duration_secs, 0)

    return f"{duration_secs // 60}:{duration_secs % 60:02}"


def pluralize(string: str, number: int, pluralized_form: str | None = None) -> str:
    """
    Pluralize the given string given the count as a number.

    >>> pluralize('foo', 1)
    'foo'
    >>> pluralize('foo', 2)
    'foos'
    >>> pluralize('foo', 0)
    'foos'
    """
    if number != 1:
        return pluralized_form or f"{string}s"
    return string


def format_sequence_duration(duration: Optional[timedelta]) -> str:
    """
    Formats duration in English.

    >>> format_sequence_duration(timedelta(seconds=90))
    '1 minute, 30 seconds'
    >>> format_sequence_duration(timedelta(seconds=(60 * 60 + 120)))
    '1 hour, 2 minutes'
    >>> format_sequence_duration(None)
    '0 seconds'
    """
    duration_secs = round(duration.total_seconds()) if duration else 0
    duration_mins = (duration_secs // 60) % 60
    duration_hrs = duration_secs // 60 // 60
    duration_secs = duration_secs % 60

    format_components = []
    if duration_hrs > 0:
        hrs = "{} {}".format(duration_hrs, pluralize("hour", duration_hrs))
        format_components.append(hrs)

    if duration_mins > 0:
        mins = "{} {}".format(duration_mins, pluralize("minute", duration_mins))
        format_components.append(mins)

    # Show seconds if there are no hours.
    if duration_hrs == 0:
        secs = "{} {}".format(duration_secs, pluralize("second", duration_secs))
        format_components.append(secs)

    return ", ".join(format_components)


def format_datetime(value: Optional[datetime]) -> str:
    if value is None:
        return ""
    if value.tzinfo is not None:
        value = value.astimezone()
    return value.strftime("%Y-%m-%d %H:%M")


def format_size(size: Optional[int]) -> str:
    if not size:
        return ""
    if size < 1024 * 1024:
        return "{} KiB".format(round(size / 1024))
    return "{:.1f} MiB".format(size / 1024 / 1024)


def format_rating(rating: Optional[int]) -> str:
    if not rating:
        return ""
    return "★" * rating + "☆" * (5 - rating)


def dot_join(*items: Any) -> str:
    """
    Joins the given strings with a dot character. Filters out ``None`` values.

    >>> dot_join(None, "foo", "bar", None, "baz")
    'foo  •  bar  •  baz'
    """
    return "  •  ".join(map(str, filter(lambda x: x is not None, items)))


def get_cached_status_icons(song_ids: List[str]) -> List[str]:
    cache_icon = {
        SongCacheStatus.CACHED: "folder-download-symbolic",
        SongCacheStatus.PERMANENTLY_CACHED: "view-pin-symbolic",
        SongCacheStatus.DOWNLOADING: "emblem-synchronizing-symbolic",
    }
    return [
        cache_icon.get(cache_status, "")
        for cache_status in AdapterManager.get_cached_statuses(song_ids)
    ]


def _model_values(model: Any) -> Any:
    """
    Returns a value which can be used to compare ``model`` against another model.

    GObjects are compared by the values of their GObject properties. Do not compare them
    by introspecting their attributes (which is what ``deepdiff`` does): the type
    metadata of a GObject (``g_type_instance``, ``props``, ...) forms a cyclic graph, and
    PyGObject creates a new wrapper object on every access to it, which defeats cycle
    detection and ends in a ``RecursionError``.
    """
    if isinstance(model, GObject.Object):
        return tuple(model.get_property(pspec.name) for pspec in model.list_properties())
    return model


def diff_song_store(store_to_edit: Any, new_store: Iterable[Any]):
    """
    Diffing song stores is nice, because we can easily make edits by modifying
    the underlying store.

    Rows are compared by position: cells which changed are updated in place, rows
    beyond the end of ``new_store`` are removed, and rows beyond the end of
    ``store_to_edit`` are appended.
    """
    old_rows = [row[:] for row in store_to_edit]
    new_rows = [list(row) for row in new_store]

    for i, (old_row, new_row) in enumerate(zip(old_rows, new_rows)):
        for column, (old_value, new_value) in enumerate(zip(old_row, new_row)):
            if old_value != new_value:
                store_to_edit[i][column] = new_value

    for i in reversed(range(len(new_rows), len(old_rows))):
        del store_to_edit[i]

    for row in new_rows[len(old_rows) :]:
        store_to_edit.append(row)


def diff_model_store(store_to_edit: Any, new_store: Iterable[Any]):
    """
    The diff here is that if there are any differences, then we refresh the
    entire list. This is because it is too hard to do editing.
    """
    new_models = list(new_store)
    old_values = [_model_values(model) for model in store_to_edit]
    new_values = [_model_values(model) for model in new_models]
    if old_values == new_values:
        return

    store_to_edit.splice(0, len(store_to_edit), new_models)


def show_song_popover(
    song_ids: List[str],
    x: int,
    y: int,
    relative_to: Any,
    offline_mode: bool,
    position: Gtk.PositionType = Gtk.PositionType.BOTTOM,
    on_download_state_change: Callable[[str], None] = lambda _: None,
    on_remove_downloads_click: Callable[[], Any] = lambda: None,
    on_playlist_state_change: Callable[[], None] = lambda: None,
    show_remove_from_playlist_button: bool = False,
    extra_menu_items: List[Tuple[Gtk.ModelButton, Any]] | None = None,
):
    def on_download_songs_click(_: Any):
        AdapterManager.batch_download_songs(
            song_ids,
            before_download=on_download_state_change,
            on_song_download_complete=on_download_state_change,
        )

    def do_on_remove_downloads_click(_: Any):
        AdapterManager.cancel_download_songs(song_ids)
        AdapterManager.batch_delete_cached_songs(
            song_ids,
            on_song_delete=on_download_state_change,
        )
        on_remove_downloads_click()

    def on_add_to_playlist_click(_: Any, playlist: Playlist):
        update_playlist_result = AdapterManager.update_playlist(
            playlist_id=playlist.id, append_song_ids=song_ids
        )
        update_playlist_result.add_done_callback(lambda _: on_playlist_state_change())

    popover = Gtk.PopoverMenu()
    vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

    # Add all of the menu items to the popover.
    song_count = len(song_ids)

    play_next_button = Gtk.ModelButton(text="Play next", sensitive=False)
    add_to_queue_button = Gtk.ModelButton(text="Add to queue", sensitive=False)
    if not offline_mode:
        play_next_button.set_action_name("app.play-next")
        play_next_button.set_action_target_value(GLib.Variant("as", song_ids))
        add_to_queue_button.set_action_name("app.add-to-queue")
        add_to_queue_button.set_action_target_value(GLib.Variant("as", song_ids))

    go_to_album_button = Gtk.ModelButton(text="Go to album", sensitive=False)
    go_to_artist_button = Gtk.ModelButton(text="Go to artist", sensitive=False)
    browse_to_song = Gtk.ModelButton(
        text=f"Browse to {pluralize('song', song_count)}", sensitive=False
    )
    download_song_button = Gtk.ModelButton(
        text=f"Download {pluralize('song', song_count)}", sensitive=False
    )
    remove_download_button = Gtk.ModelButton(
        text=f"Remove {pluralize('download', song_count)}", sensitive=False
    )

    # Retrieve songs and set the buttons as sensitive later.
    def on_get_song_details_done(songs: List[Song]):
        song_cache_statuses = AdapterManager.get_cached_statuses([s.id for s in songs])
        if not offline_mode and any(
            status == SongCacheStatus.NOT_CACHED for status in song_cache_statuses
        ):
            download_song_button.set_sensitive(True)
        if any(
            status
            in (
                SongCacheStatus.CACHED,
                SongCacheStatus.PERMANENTLY_CACHED,
                SongCacheStatus.DOWNLOADING,
            )
            for status in song_cache_statuses
        ):
            remove_download_button.set_sensitive(True)
            play_next_button.set_action_target_value(GLib.Variant("as", song_ids))
            play_next_button.set_action_name("app.play-next")
            add_to_queue_button.set_action_target_value(GLib.Variant("as", song_ids))
            add_to_queue_button.set_action_name("app.add-to-queue")

        albums, artists, parents = set(), set(), set()
        for song in songs:
            parents.add(parent_id if (parent_id := song.parent_id) else None)

            if (al := song.album) and (id_ := al.id) and not id_.startswith("invalid:"):
                albums.add(id_)

            if (a := song.artist) and (id_ := a.id) and not id_.startswith("invalid:"):
                artists.add(id_)

        if len(albums) == 1 and list(albums)[0] is not None:
            go_to_album_button.set_action_target_value(GLib.Variant("s", list(albums)[0]))
            go_to_album_button.set_action_name("app.go-to-album")
        if len(artists) == 1 and list(artists)[0] is not None:
            go_to_artist_button.set_action_target_value(GLib.Variant("s", list(artists)[0]))
            go_to_artist_button.set_action_name("app.go-to-artist")
        if len(parents) == 1 and list(parents)[0] is not None:
            browse_to_song.set_action_target_value(GLib.Variant("s", list(parents)[0]))
            browse_to_song.set_action_name("app.browse-to")

    def batch_get_song_details() -> List[Song]:
        return [AdapterManager.get_song_details(song_id).result() for song_id in song_ids]

    get_song_details_result: Result[List[Song]] = Result(batch_get_song_details)
    get_song_details_result.add_done_callback(
        lambda f: GLib.idle_add(on_get_song_details_done, f.result())
    )

    menu_items = [
        play_next_button,
        add_to_queue_button,
        Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL),
        go_to_album_button,
        go_to_artist_button,
        browse_to_song,
        Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL),
        (download_song_button, on_download_songs_click),
        (remove_download_button, do_on_remove_downloads_click),
        Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL),
        Gtk.ModelButton(
            text=f"Add {pluralize('song', song_count)} to playlist",
            menu_name="add-to-playlist",
            name="menu-item-add-to-playlist",
            sensitive=not offline_mode,
        ),
        *(extra_menu_items or []),
    ]

    for item in menu_items:
        if type(item) is tuple:
            el, fn = item
            el.connect("clicked", fn)
            el.get_style_context().add_class("menu-button")
            vbox.pack_start(item[0], False, True, 0)
        else:
            item.get_style_context().add_class("menu-button")
            vbox.pack_start(item, False, True, 0)

    popover.add(vbox)

    # Create the "Add song(s) to playlist" sub-menu.
    playlists_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

    if not offline_mode:
        # Back button
        playlists_vbox.add(Gtk.ModelButton(inverted=True, centered=True, menu_name="main"))

        # Loading indicator
        loading_indicator = Gtk.Spinner(name="menu-item-spinner")
        loading_indicator.start()
        playlists_vbox.add(loading_indicator)

        # Create a future to make the actual playlist buttons
        def on_get_playlists_done(f: Result[List[Playlist]]):
            playlists_vbox.remove(loading_indicator)

            for playlist in f.result():
                button = Gtk.ModelButton(text=playlist.name)
                button.get_style_context().add_class("menu-button")
                button.connect("clicked", on_add_to_playlist_click, playlist)
                button.show()
                playlists_vbox.pack_start(button, False, True, 0)

        playlists_result = AdapterManager.get_playlists()
        playlists_result.add_done_callback(on_get_playlists_done)

    popover.add(playlists_vbox)
    popover.child_set_property(playlists_vbox, "submenu", "add-to-playlist")

    # Positioning of the popover.
    rect = Gdk.Rectangle()
    rect.x, rect.y, rect.width, rect.height = x, y, 1, 1
    popover.set_pointing_to(rect)
    popover.set_position(position)
    popover.set_relative_to(relative_to)

    popover.popup()
    popover.show_all()


def confirm_queue_replacement(
    parent: Optional[Gtk.Window],
    queue_length: int,
    on_replace: Callable[[], None],
    on_add: Callable[[], None],
) -> Gtk.MessageDialog:
    """
    Asks whether to replace the play queue with what was clicked, add it to the queue, or
    do nothing. Whether this is asked at all is a setting.
    """
    dialog = Gtk.MessageDialog(
        transient_for=parent,
        modal=True,
        message_type=Gtk.MessageType.QUESTION,
        buttons=Gtk.ButtonsType.NONE,
        text="Replace the play queue?",
        secondary_text=(f"The play queue has {queue_length} songs. "),
    )
    dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
    dialog.add_button("Add to Queue", Gtk.ResponseType.APPLY)
    dialog.add_button("Replace Queue", Gtk.ResponseType.OK)
    dialog.set_default_response(Gtk.ResponseType.OK)

    def on_response(_: Gtk.Dialog, response: int):
        dialog.destroy()
        if response == Gtk.ResponseType.OK:
            on_replace()
        elif response == Gtk.ResponseType.APPLY:
            on_add()

    dialog.connect("response", on_response)
    dialog.show_all()
    return dialog


def async_callback(
    future_fn: Callable[..., Result],
    before_download: Callable[[Any], None] | None = None,
    on_failure: Callable[[Any, Exception], None] | None = None,
) -> Callable[[Callable], Callable]:
    """
    Defines the ``async_callback`` decorator.

    When a function is annotated with this decorator, the function becomes the done
    callback for the given result-generating lambda function. The annotated function
    will be called with the result of the Result generated by said lambda function.

    :param future_fn: a function which generates an :class:`AdapterManager.Result`.
    """

    def decorator(callback_fn: Callable) -> Callable:
        @functools.wraps(callback_fn)
        def wrapper(
            self: Any,
            *args,
            app_config: AppConfiguration | None = None,
            force: bool = False,
            order_token: int | None = None,
            **kwargs,
        ):
            def on_before_download():
                if before_download:
                    GLib.idle_add(before_download, self)

            def future_callback(is_immediate: bool, f: Result):
                try:
                    result = f.result()
                    is_partial = False
                except CacheMissError as e:
                    result = e.partial_data
                    if result is None:
                        if on_failure:
                            GLib.idle_add(on_failure, self, e)
                        return

                    is_partial = True
                except Exception as e:
                    if on_failure:
                        GLib.idle_add(on_failure, self, e)
                    return

                fn = functools.partial(
                    callback_fn,
                    self,
                    result,
                    app_config=app_config,
                    force=force,
                    order_token=order_token,
                    is_partial=is_partial,
                )

                if is_immediate:
                    # The data is available now, no need to wait for the future to
                    # finish, and no need to incur the overhead of adding to the GLib
                    # event queue.
                    fn()
                else:
                    # We don't have the data yet, meaning that it is a future, and we
                    # have to idle add so that we don't seg fault GTK.
                    GLib.idle_add(fn)

            result: Result = future_fn(
                *args,
                before_download=on_before_download,
                force=force,
                **kwargs,
            )
            result.add_done_callback(functools.partial(future_callback, result.data_is_available))

        return wrapper

    return decorator
