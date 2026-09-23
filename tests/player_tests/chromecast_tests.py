"""
The Chromecast player has to work with every pychromecast from Debian's 9.x to the 14.x
that gets bundled: the listener registration and the mute state moved between them.
"""

from collections import namedtuple
from typing import Any, List
from unittest import mock
from uuid import uuid4

import pytest

from sublime_music.players.chromecast import ChromecastPlayer

CastStatus = namedtuple("CastStatus", ["volume_level", "volume_muted", "session_id"])


class _Recorder:
    def __init__(self):
        self.listeners: List[Any] = []

    def register_status_listener(self, listener: Any):
        self.listeners.append(listener)


class _FakeChromecast:
    """Enough of pychromecast.Chromecast for selecting a device and reading its state."""

    def __init__(self, old_api: bool):
        self.cast_info = mock.Mock(uuid=uuid4(), friendly_name="Kitchen")
        self.media_controller = _Recorder()
        self.socket_client = mock.Mock(receiver_controller=_Recorder())
        self.status: Any = CastStatus(volume_level=0.5, volume_muted=True, session_id="s")
        self.waited = False
        if old_api:
            # Before pychromecast 14 the Chromecast itself took the status listener.
            self.receiver = _Recorder()
            self.register_status_listener = self.receiver.register_status_listener

    def wait(self):
        self.waited = True


@pytest.fixture
def player() -> ChromecastPlayer:
    with mock.patch("pychromecast.get_chromecasts", return_value=mock.Mock()):
        return ChromecastPlayer(
            on_timepos_change=lambda *a: None,
            on_track_end=lambda: None,
            on_player_event=lambda *a: None,
            player_device_change_callback=lambda *a: None,
            config={},
        )


@pytest.mark.parametrize("old_api", [True, False])
def test_selecting_a_device_registers_both_listeners(player: ChromecastPlayer, old_api: bool):
    chromecast = _FakeChromecast(old_api)
    player.chromecast_discovered_callback(chromecast)

    player.set_current_device_id(str(chromecast.cast_info.uuid))

    assert chromecast.media_controller.listeners == [player]
    receiver = chromecast.receiver if old_api else chromecast.socket_client.receiver_controller
    assert receiver.listeners == [player]
    assert chromecast.waited


def test_mute_state_comes_from_the_status(player: ChromecastPlayer):
    assert player.get_is_muted() is False  # no device selected
    chromecast = _FakeChromecast(old_api=False)
    player.chromecast_discovered_callback(chromecast)
    player.set_current_device_id(str(chromecast.cast_info.uuid))

    assert player.get_is_muted() is True
    chromecast.status = None
    assert player.get_is_muted() is False


def test_listener_callbacks_the_base_classes_require_exist(player: ChromecastPlayer):
    # pychromecast calls these on errors; missing ones would raise inside its threads.
    player.new_launch_error(mock.Mock())
    player.load_media_failed(1, 2)
