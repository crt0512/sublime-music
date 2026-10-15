"""Measure GTK requests, not the window's initial/default size."""

from pathlib import Path
from unittest.mock import Mock

import gi
import pytest

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")

from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from sublime_music.ui import main, player_controls  # noqa: E402


@pytest.fixture
def styled_gtk():
    if not Gtk.init_check([])[0]:
        pytest.skip("GTK needs a display to measure widget sizes")
    settings = Gtk.Settings.get_default()
    old_theme, old_font = settings.props.gtk_theme_name, settings.props.gtk_font_name
    settings.props.gtk_theme_name = "Adwaita"
    settings.props.gtk_font_name = "Sans 10"
    provider = Gtk.CssProvider()
    provider.load_from_path(str(Path(main.__file__).with_name("app_styles.css")))
    screen = Gdk.Screen.get_default()
    Gtk.StyleContext.add_provider_for_screen(
        screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )
    yield
    Gtk.StyleContext.remove_provider_for_screen(screen, provider)
    settings.props.gtk_theme_name, settings.props.gtk_font_name = old_theme, old_font


@pytest.mark.parametrize("rating", [False, True])
@pytest.mark.parametrize("devices", [False, True])
@pytest.mark.parametrize("duration", ["-:--", "3:45", "1:23:45"])
def test_player_minimum_fits_split_window(styled_gtk, monkeypatch, rating, devices, duration):
    monkeypatch.setattr(player_controls.AdapterManager, "can_get_song_rating", lambda: rating)
    controls = player_controls.PlayerControls()
    try:
        controls.show_all()
        controls.device_button.set_visible(devices)
        controls.star_button.show()
        controls.song_progress_label.set_text(duration)
        controls.song_duration_label.set_text(duration)
        for label in (controls.song_title, controls.album_name, controls.artist_name):
            label.set_text("Long metadata " * 30)
        controls.song_format.set_text("FLAC  •  1024 kbps")
        assert controls.get_preferred_width().minimum_width <= 720
        assert controls.play_button.get_preferred_width().minimum_width >= 56
        assert controls.volume_slider.get_preferred_width().minimum_width >= 90
    finally:
        controls.destroy()


@pytest.fixture
def window(styled_gtk, monkeypatch):
    monkeypatch.setattr(player_controls.AdapterManager, "can_get_song_rating", lambda: False)
    widget = main.MainWindow()
    widget.show_all()
    yield widget
    widget.destroy()


def test_header_search_can_expand_without_raising_minimum(window, monkeypatch):
    monkeypatch.setattr(window.search_popup, "popup", Mock())
    before = window.titlebar.get_preferred_width()
    window._show_search()
    focused = window.titlebar.get_preferred_width()
    assert before.minimum_width <= 720
    assert focused.minimum_width == before.minimum_width
    assert focused.natural_width > before.natural_width
    assert window.search_results_scroll.get_preferred_width().minimum_width >= 300
    window._hide_search()
    assert window.titlebar.get_preferred_width() == before


def test_hidden_panel_does_not_set_active_tab_width(window):
    # Deliberately oversized inactive toolbar: do not constrain or shrink its columns.
    window.songs_panel.set_size_request(1200, -1)
    window.stack.set_visible_child_name("browse")
    assert not window.stack.get_hhomogeneous()
    assert window.stack.get_vhomogeneous()
    assert window.stack.get_preferred_width().minimum_width < 720
    assert window.get_child().get_preferred_width().minimum_width <= 720


def _wait_for(predicate):
    loop = GLib.MainLoop()

    def check():
        if predicate():
            loop.quit()
            return False
        return True

    check_id = GLib.timeout_add(5, check)
    timeout_id = GLib.timeout_add(2000, lambda: loop.quit() or False)
    loop.run()
    if predicate():
        GLib.source_remove(timeout_id)
    else:
        GLib.source_remove(check_id)
        pytest.fail("GTK layout did not reach the requested size")


def test_seekbar_prefers_original_width_without_raising_minimum(window):
    scrubber = window.player_controls.song_scrubber
    request = scrubber.get_preferred_width()
    assert request.minimum_width < 300
    assert request.natural_width >= 400
    assert window.player_controls.get_preferred_width().minimum_width <= 720


def test_search_prefers_original_unfocused_width(window):
    original = Gtk.SearchEntry()
    window.titlebar.pack_start(original)
    original.show()
    try:
        assert (
            window.search_entry.get_preferred_width().natural_width
            == original.get_preferred_width().natural_width
        )
    finally:
        original.destroy()


@pytest.mark.parametrize("focused", [False, True])
def test_controls_expand_again_after_split_window_resize(window, monkeypatch, focused):
    monkeypatch.setattr(window.search_popup, "popup", Mock())
    window.stack.set_visible_child_name("browse")
    # Avoid the header taking keyboard focus automatically when the window is mapped.
    window.set_focus(window.player_controls.play_button)
    if focused:
        window._show_search()
    else:
        window._hide_search()
    scrubber = window.player_controls.song_scrubber
    search = window.search_entry
    preferred_search_width = 300 if focused else search.get_preferred_width().natural_width
    for width in (1342, 731, 1342):
        window.resize(width, 756)
        window.show()

        def allocated():
            if window.get_size().width != width:
                return False
            if width == 1342:
                return (
                    scrubber.get_allocated_width() >= 400
                    and search.get_allocated_width() >= preferred_search_width
                )
            return scrubber.get_allocated_width() < 400 and search.get_allocated_width() < 300

        _wait_for(allocated)
        assert window.get_child().get_preferred_width().minimum_width <= 720


@pytest.mark.parametrize("width", [731, 1342, 1600])
@pytest.mark.parametrize("rating", [False, True])
@pytest.mark.parametrize("devices", [False, True])
def test_right_controls_keep_size_and_left_alignment_across_songs(
    styled_gtk, monkeypatch, width, rating, devices
):
    monkeypatch.setattr(player_controls.AdapterManager, "can_get_song_rating", lambda: rating)
    controls = player_controls.PlayerControls()
    shell = Gtk.Window()
    shell.add(controls)
    shell.set_default_size(width, 180)
    shell.show_all()
    controls.star_button.show()
    controls.device_button.set_visible(devices)
    controls.song_duration_label.set_text("4:56")
    row = controls.info_button.get_parent()
    right = row.get_parent()
    widgets = [
        controls.info_button,
        controls.play_queue_button,
        controls.volume_mute_toggle,
        controls.volume_slider,
    ]
    if rating:
        widgets += [controls.rating_buttons_box, *controls.rating_buttons_box.get_children()]

    def geometry():
        return [
            (widget.translate_coordinates(controls, 0, 0)[0], widget.get_allocated_width())
            for widget in widgets
        ]

    try:
        _wait_for(
            lambda: shell.get_size().width == width
            and controls.volume_slider.get_allocated_width() > 1
        )
        initial = None
        for text in ("Short", "A very long song title and album name " * 30, "Short again"):
            labels = (controls.song_title, controls.album_name, controls.artist_name)
            for label in labels:
                label.set_text(text)
            # Wait for a frame, not just the unchanged window size: the regression
            # only appears after GTK has reallocated for the new metadata.
            loop = GLib.MainLoop()
            controls.add_tick_callback(lambda *a: loop.quit() or False)
            loop.run()
            current = geometry()
            if initial is None:
                initial = current
            else:
                assert current == initial
            assert (
                controls.volume_slider.get_allocated_width()
                == controls.volume_slider.get_preferred_width().natural_width
            )
            assert row.get_halign() == Gtk.Align.START
            assert row.translate_coordinates(right, 0, 0)[0] == 0
            if rating:
                assert controls.rating_buttons_box.get_halign() == Gtk.Align.START
                assert controls.rating_buttons_box.translate_coordinates(right, 0, 0)[0] == 0
            _assert_playback_centered(controls)
    finally:
        shell.destroy()


def test_seekbar_range_and_increments_are_unchanged(window):
    adjustment = window.player_controls.song_scrubber.get_adjustment()
    assert (adjustment.get_lower(), adjustment.get_upper()) == (0, 100)
    assert adjustment.get_step_increment() == 5
    assert adjustment.get_page_increment() == 50


def _assert_playback_centered(controls):
    center = controls.get_allocated_width() / 2
    playback = controls.get_center_widget()
    assert playback is not None
    for widget in (playback, controls.song_scrubber, controls.play_button):
        x, _ = widget.translate_coordinates(controls, 0, 0)
        assert abs(x + widget.get_allocated_width() / 2 - center) <= 1


@pytest.mark.parametrize("devices", [False, True])
@pytest.mark.parametrize(
    "metadata", ["Track", "A much longer song title and album name " * 20], ids=["short", "long"]
)
def test_playback_is_centered_with_asymmetric_sides(window, devices, metadata):
    window.stack.set_visible_child_name("browse")
    controls = window.player_controls
    for label in (controls.song_title, controls.album_name, controls.artist_name):
        label.set_text(metadata)
    controls.star_button.show()
    controls.device_button.set_visible(devices)
    controls.song_progress_label.set_text("1:23")
    controls.song_duration_label.set_text("1:23:45")
    window.set_focus(controls.play_button)
    for width in (1342, 731, 900, 1342):
        window.resize(width, 756)
        window.show()
        _wait_for(
            lambda: window.get_size().width == width and controls.get_allocated_width() == width
        )
        _assert_playback_centered(controls)


@pytest.mark.parametrize("devices", [False, True])
@pytest.mark.parametrize("duration", ["4:56", "1:23:45"])
def test_populated_player_preserves_song_info_at_half_width(window, devices, duration):
    controls = window.player_controls
    window.stack.set_visible_child_name("browse")
    labels = (controls.song_title, controls.album_name, controls.artist_name)
    texts = (
        "A reasonably long currently playing title",
        "The name of the current album",
        "The current artist",
    )
    for label, text in zip(labels, texts):
        label.set_text(text)
    controls.song_format.set_text("FLAC  •  1024 kbps")
    controls.song_progress_label.set_text("1:23")
    controls.song_duration_label.set_text(duration)
    controls.device_button.set_visible(devices)
    controls.star_button.show()
    window.set_focus(controls.play_button)
    for width in (1342, 731, 1342):
        window.resize(width, 756)
        window.show()
        _wait_for(
            lambda: window.get_size().width == width
            and all(label.get_allocated_width() >= 85 for label in labels)
            and (
                controls.song_scrubber.get_allocated_width() >= 400
                if width == 1342
                else controls.song_scrubber.get_allocated_width() < 250
            )
        )
        assert controls.get_preferred_width().minimum_width <= 720
        _assert_playback_centered(controls)
        # All three labels remain mapped and usable; long text ellipsizes rather
        # than vanishing, and the seek bar does not overlap the song display.
        for label in labels:
            assert label.get_mapped()
            x, _ = label.translate_coordinates(controls, 0, 0)
            seek_x, _ = controls.song_scrubber.translate_coordinates(controls, 0, 0)
            assert x + label.get_allocated_width() <= seek_x


@pytest.mark.parametrize("focused", [False, True])
def test_search_compacts_and_restores_with_window_width(window, monkeypatch, focused):
    monkeypatch.setattr(window.search_popup, "popup", Mock())
    search = Mock()
    monkeypatch.setattr(main.AdapterManager, "search", search)
    window.stack.set_visible_child_name("browse")
    window.set_focus(window.player_controls.play_button)
    entry = window.search_entry
    # Changing the placeholder/layout must not clear a query or start another search.
    window._last_search_query = "existing query"
    entry.set_text("existing query")
    if focused:
        window._show_search()
    else:
        window._hide_search()
    for width in (1342, 731, 1342):
        window.resize(width, 756)
        window.show()
        compact = width == 731
        _wait_for(
            lambda: window.get_size().width == width
            and entry.compact == compact
            and (
                entry.get_allocated_width() <= 100
                if compact
                else entry.get_allocated_width() >= (300 if focused else 160)
            )
        )
        assert entry.get_placeholder_text() == ("Search..." if compact else "Search everything...")
        assert entry.get_text() == "existing query"
        assert window.titlebar.get_preferred_width().minimum_width <= 720
    search.assert_not_called()

