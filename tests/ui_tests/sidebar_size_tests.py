from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import gi
import pytest

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")

from gi.repository import Gdk, GdkPixbuf, GLib, Gtk  # noqa: E402

from sublime_music.ui import artists, main, player_controls, playlists  # noqa: E402


def _wait_for(predicate):
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
        pytest.fail("Sidebar layout did not settle without resizing the window")


@pytest.fixture
def window(monkeypatch):
    if not Gtk.init_check([])[0]:
        pytest.skip("GTK needs a display")
    settings = Gtk.Settings.get_default()
    old = (settings.props.gtk_theme_name, settings.props.gtk_font_name)
    settings.props.gtk_theme_name, settings.props.gtk_font_name = "Adwaita", "Sans 10"
    css = Gtk.CssProvider()
    css.load_from_path(str(Path(main.__file__).with_name("app_styles.css")))
    screen = Gdk.Screen.get_default()
    Gtk.StyleContext.add_provider_for_screen(screen, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    monkeypatch.setattr(player_controls.AdapterManager, "can_get_song_rating", lambda: False)
    widget = main.MainWindow()
    widget.show_all()
    widget.stack.set_transition_type(Gtk.StackTransitionType.NONE)
    widget.stack.set_visible_child_name("browse")
    widget.set_focus(widget.player_controls.play_button)
    yield widget
    widget.destroy()
    Gtk.StyleContext.remove_provider_for_screen(screen, css)
    settings.props.gtk_theme_name, settings.props.gtk_font_name = old


def _populate(panel, kind, expanded):
    if kind == "playlists":
        sidebar = panel.playlist_list
        sidebar.playlists_store.append(sidebar.PlaylistModel("selected", "Playlist name " * 30))
        sidebar.new_playlist_row.hide()
        sidebar.loading_indicator.hide()
        detail = panel.playlist_detail_panel
        detail.playlist_name.set_text("A very long playlist name " * 20)
        detail.playlist_comment.set_text("A long playlist comment " * 30)
        detail.playlist_stats.set_text("Created by someone • 100 songs • 6 hours")
        detail.playlist_view_loading_box.hide()
        artwork, size = detail.playlist_artwork, 200 if expanded else 70
        detail.editing_playlist_song_list = True
        detail.playlist_song_store.append([True, "", "Song " * 50, "Album", "Artist", "3:45", "s"])
        detail.editing_playlist_song_list = False
        if not expanded:
            detail.playlist_name.get_style_context().add_class("collapsed")
            detail.playlist_comment.hide()
            detail.playlist_stats.hide()
    else:
        sidebar = panel.artist_list
        sidebar.artists_store.append(
            artists._ArtistModel(
                SimpleNamespace(id="selected", name="Artist name " * 30, album_count=4)
            )
        )
        sidebar.loading_indicator.hide()
        detail = panel.artist_detail_panel
        detail.artist_name.set_text("A very long artist name " * 20)
        detail.artist_bio.set_text("Biography " * 20 + "unbrokenword" * 40)
        detail.artist_stats.set_text("123 albums • 4567 songs • 23 hours")
        artwork, size = detail.artist_artwork, 300 if expanded else 70
        if not expanded:
            detail.artist_name.get_style_context().add_class("collapsed")
            detail.artist_bio.hide()
            detail.artist_stats.hide()
    pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, size, size)
    pixbuf.fill(0x446688FF)
    artwork.image.set_from_pixbuf(pixbuf)
    sidebar.list.select_row(sidebar.list.get_row_at_index(0))
    return sidebar, detail


@pytest.mark.parametrize("kind", ["playlists", "artists"])
@pytest.mark.parametrize("expanded", [False, True])
def test_loaded_tabs_do_not_resize_half_width_window(window, kind, expanded):
    panel = getattr(window, kind + "_panel")
    sidebar, detail = _populate(panel, kind, expanded)
    window.resize(731, 756)
    window.show()
    _wait_for(lambda: window.get_size().width == 731)
    for tab in (kind, "browse", kind):
        window.stack.set_visible_child_name(tab)
        _wait_for(
            lambda: window.get_size().width == 731
            and window.stack.get_visible_child().get_allocated_width() <= 731
        )
        assert window.get_child().get_preferred_width().minimum_width <= 720
    assert sidebar.list.get_selected_row().get_index() == 0
    assert panel.child_get_property(sidebar, "shrink")
    assert sidebar.get_preferred_width().minimum_width < 200
    assert detail.get_mapped()


@pytest.mark.parametrize("kind", ["playlists", "artists"])
def test_sidebar_can_be_dragged_smaller_and_restored(window, kind):
    panel = getattr(window, kind + "_panel")
    sidebar, _ = _populate(panel, kind, True)
    window.stack.set_visible_child_name(kind)
    window.resize(731, 756)
    window.show()
    for position in (160, 200, 160):
        panel.set_position(position)
        _wait_for(
            lambda: abs(sidebar.get_allocated_width() - position) <= 2
            and window.get_size().width == 731
        )
        assert sidebar.list.get_selected_row().get_index() == 0


def test_creating_playlist_does_not_expand_narrow_sidebar(window, monkeypatch):
    panel = window.playlists_panel
    sidebar, _ = _populate(panel, "playlists", True)
    window.stack.set_visible_child_name("playlists")
    window.resize(731, 756)
    panel.set_position(160)
    window.show()
    sidebar.on_new_playlist_clicked(None)
    _wait_for(lambda: window.get_size().width == 731 and sidebar.get_allocated_width() <= 162)
    assert sidebar.new_playlist_entry.get_mapped()
    assert sidebar.new_playlist_entry.get_text() == "Untitled Playlist"
    create = Mock()
    monkeypatch.setattr(sidebar, "create_playlist", create)
    sidebar.confirm_button_clicked(None)
    create.assert_called_once_with("Untitled Playlist")
