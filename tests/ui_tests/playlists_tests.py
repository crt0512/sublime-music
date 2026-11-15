from datetime import datetime, timezone
from inspect import unwrap
from types import SimpleNamespace
from typing import List
from unittest.mock import Mock, patch

import gi
import pytest

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

from sublime_music.adapters.api_objects import sort_playlists
from sublime_music.adapters.subsonic.api_objects import Playlist
from sublime_music.config import AppConfiguration
from sublime_music.ui import playlists


@pytest.mark.parametrize(
    "order,reverse,expected",
    [
        ("name", False, ["2", "1", "3"]),
        ("name", True, ["3", "1", "2"]),
        ("created", False, ["1", "2", "3"]),
        ("created", True, ["2", "1", "3"]),
        ("changed", False, ["2", "1", "3"]),
        ("changed", True, ["1", "2", "3"]),
    ],
)
def test_playlist_sort(order: str, reverse: bool, expected: List[str]):
    data = [
        Playlist(id="1", name="beta", created=datetime(2020, 1, 1), changed=datetime(2022, 1, 1)),
        Playlist(
            id="2",
            name="Alpha",
            created=datetime(2021, 1, 1, tzinfo=timezone.utc),
            changed=datetime(2021, 1, 1),
        ),
        Playlist(id="3", name="gamma", created=None, changed=None),
    ]
    assert [p.id for p in sort_playlists(data, order, reverse)] == expected


@pytest.fixture
def panel():
    if not Gtk.init_check([])[0]:
        pytest.skip("GTK needs a display")
    widget = playlists.PlaylistsPanel()
    yield widget
    widget.destroy()


def test_list_refresh_requests_full_panel_refresh(panel: playlists.PlaylistsPanel):
    refresh = Mock()
    panel.connect("refresh-window", refresh)
    panel.playlist_list.list_refresh_button.clicked()
    assert refresh.call_args.args[1:] == ({}, True)


def test_full_panel_refresh_forces_list_and_selected_contents(panel: playlists.PlaylistsPanel):
    config = Mock(spec=AppConfiguration, state=SimpleNamespace(selected_playlist_id="2"))
    with (
        patch.object(panel.playlist_list, "update") as refresh_list,
        patch.object(panel.playlist_detail_panel, "update") as refresh_details,
    ):
        panel.update(config, force=True)
    refresh_list.assert_called_once_with(app_config=config, force=True)
    refresh_details.assert_called_once_with(config, force=True)


def test_sort_preserves_selection_and_does_not_emit_during_update(
    panel: playlists.PlaylistsPanel,
):
    widget = panel.playlist_list
    refresh = Mock()
    widget.connect("refresh-window", refresh)
    config = Mock(
        spec=AppConfiguration,
        state=SimpleNamespace(selected_playlist_id="2", playlist_sort="name_asc"),
        offline_mode=False,
    )
    data = [
        SimpleNamespace(id="1", name="Zeta", created=None, changed=None),
        SimpleNamespace(id="2", name="Alpha", created=None, changed=None),
    ]
    unwrap(widget.update_list)(widget, data, app_config=config)
    assert widget.playlists_store[0].playlist_id == "2"
    assert widget.list.get_selected_row().get_index() == 0
    widget.sort_combo.set_active_id("created_desc")
    assert refresh.call_args.args[1:] == ({"playlist_sort": "created_desc"}, False)
    config.state.playlist_sort = "created_desc"
    config.offline_mode = False
    with patch.object(widget, "update_list"):
        widget.update(config)
    assert refresh.call_count == 1
