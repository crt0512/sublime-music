from types import SimpleNamespace
from unittest.mock import Mock

import gi
import pytest

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")

from gi.repository import Gdk, Gtk  # noqa: E402

from sublime_music.ui import player_controls  # noqa: E402


@pytest.fixture
def controls(monkeypatch):
    monkeypatch.setattr(player_controls.AdapterManager, "can_get_song_rating", lambda: False)
    widget = player_controls.PlayerControls()
    widget.editing_play_queue_song_list = True
    # Duplicate IDs must still remove only the currently playing occurrence.
    for song_id in ("duplicate", "other", "duplicate"):
        widget.play_queue_store.append([True, "", song_id, False, song_id])
    widget.current_playing_index = 2
    widget.info_song = SimpleNamespace(id="duplicate")
    yield widget
    widget.destroy()


@pytest.fixture
def popover(monkeypatch):
    show = Mock()
    monkeypatch.setattr(player_controls.util, "show_song_popover", show)
    return show


def _event(button=3):
    event = Gdk.Event.new(Gdk.EventType.BUTTON_PRESS)
    event.button.button = button
    event.button.x, event.button.y = 5, 7
    return event


@pytest.mark.parametrize("offline", [False, True])
def test_title_right_click_opens_queue_menu(controls, popover, offline):
    controls.offline_mode = offline
    removed = []
    controls.connect("songs-removed", lambda _, indices: removed.append(indices))
    title_event_box = controls.song_title.get_parent()

    assert isinstance(title_event_box, Gtk.EventBox)
    assert title_event_box.emit("button-press-event", _event())

    args, kwargs = popover.call_args
    assert args == (["duplicate"], 5, 7, title_event_box, offline)
    button, callback = kwargs["extra_menu_items"][0]
    assert button.get_property("text") == "Remove song from queue"
    callback(button)
    assert removed == [[2]]


@pytest.mark.parametrize(
    "button,index,has_song",
    [
        (1, 2, True),
        (2, 2, True),
        (3, None, True),
        (3, -1, True),
        (3, 3, True),
        (3, 2, False),
    ],
)
def test_title_ignores_non_right_click_or_missing_song(controls, popover, button, index, has_song):
    controls.current_playing_index = index
    if not has_song:
        controls.info_song = None

    assert not controls.song_title.get_parent().emit("button-press-event", _event(button))
    popover.assert_not_called()


@pytest.mark.parametrize("selected", [True, False])
def test_queue_menu_preserves_selection_behavior(controls, popover, selected):
    paths = [Gtk.TreePath.new_from_indices([index]) for index in (0, 1)]
    tree = Mock()
    tree.get_path_at_pos.return_value = (paths[0], None, 0, 0)
    tree.get_selection.return_value.get_selected_rows.return_value = (
        controls.play_queue_store,
        paths if selected else [],
    )
    removed = []
    controls.connect("songs-removed", lambda _, indices: removed.append(indices))

    assert controls.on_play_queue_button_press(tree, _event().button) is selected
    args, kwargs = popover.call_args
    assert args[0] == (["duplicate", "other"] if selected else ["duplicate"])
    button, callback = kwargs["extra_menu_items"][0]
    callback(button)
    assert removed == ([[0, 1]] if selected else [[0]])


def test_queue_right_click_on_empty_space_does_not_open_menu(controls, popover):
    tree = Mock()
    tree.get_path_at_pos.return_value = None

    assert not controls.on_play_queue_button_press(tree, _event().button)
    popover.assert_not_called()
