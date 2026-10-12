from typing import Any, List

import pytest

from sublime_music.players import manager
from sublime_music.players.base import PlayerDeviceEvent


class FakePlayer:
    name = "Fake"
    instances: List["FakePlayer"] = []

    def __init__(self, *args: Any):
        self.shut_down = False
        type(self).instances.append(self)

    def shutdown(self):
        self.shut_down = True


class FakeLocalPlayer(FakePlayer):
    name = "Local"


class FakeChromecastPlayer(FakePlayer):
    name = "Chromecast"


class FakeUPnPPlayer(FakePlayer):
    name = "UPnP / DLNA"


@pytest.fixture
def fake_players(monkeypatch: pytest.MonkeyPatch):
    FakePlayer.instances = []
    monkeypatch.setattr(manager, "ChromecastPlayer", FakeChromecastPlayer)
    monkeypatch.setattr(manager, "UPnPPlayer", FakeUPnPPlayer)
    monkeypatch.setattr(
        manager.PlayerManager,
        "available_player_types",
        [FakeLocalPlayer, FakeChromecastPlayer, FakeUPnPPlayer],
    )


def make_manager(chromecast_enabled: bool, upnp_enabled: bool = True) -> manager.PlayerManager:
    return manager.PlayerManager(
        lambda *a: None,
        lambda *a: None,
        lambda *a: None,
        lambda *a: None,
        {},
        chromecast_enabled=chromecast_enabled,
        upnp_enabled=upnp_enabled,
    )


def test_chromecast_disabled_is_never_created(fake_players: None):
    pm = make_manager(chromecast_enabled=False)
    assert list(pm.players) == [FakeLocalPlayer, FakeUPnPPlayer]
    assert not any(isinstance(p, FakeChromecastPlayer) for p in FakePlayer.instances)


def test_upnp_disabled_is_never_created(fake_players: None):
    pm = make_manager(chromecast_enabled=True, upnp_enabled=False)
    assert list(pm.players) == [FakeLocalPlayer, FakeChromecastPlayer]
    assert not any(isinstance(p, FakeUPnPPlayer) for p in FakePlayer.instances)


def test_upnp_can_be_toggled_without_touching_the_chromecast(fake_players: None):
    pm = make_manager(chromecast_enabled=True)
    chromecast, renderer = pm.players[FakeChromecastPlayer], pm.players[FakeUPnPPlayer]
    pm.player_device_change_callback(
        PlayerDeviceEvent(PlayerDeviceEvent.Delta.ADD, FakeChromecastPlayer, "cc-1", "TV")
    )
    pm.player_device_change_callback(
        PlayerDeviceEvent(PlayerDeviceEvent.Delta.ADD, FakeUPnPPlayer, "upnp:uuid:1", "Amp")
    )

    pm.set_upnp_enabled(False)
    assert renderer.shut_down and not chromecast.shut_down
    assert list(pm.players) == [FakeLocalPlayer, FakeChromecastPlayer]
    assert pm.device_id_type_map == {"cc-1": FakeChromecastPlayer}

    pm.set_upnp_enabled(True)
    assert isinstance(pm.players[FakeUPnPPlayer], FakeUPnPPlayer)
    assert pm.players[FakeUPnPPlayer] is not renderer


def test_chromecast_can_be_toggled(fake_players: None):
    pm = make_manager(chromecast_enabled=True)
    chromecast = pm.players[FakeChromecastPlayer]

    # A discovered Chromecast is forgotten once the player is disabled.
    pm.player_device_change_callback(
        PlayerDeviceEvent(PlayerDeviceEvent.Delta.ADD, FakeChromecastPlayer, "cc-1", "TV")
    )
    assert pm.device_id_type_map == {"cc-1": FakeChromecastPlayer}

    pm.set_chromecast_enabled(False)
    assert chromecast.shut_down
    assert FakeChromecastPlayer not in pm.players
    assert pm.device_id_type_map == {}

    pm.set_chromecast_enabled(False)  # already off: nothing to do

    pm.set_chromecast_enabled(True)
    assert isinstance(pm.players[FakeChromecastPlayer], FakeChromecastPlayer)
    assert pm.players[FakeChromecastPlayer] is not chromecast
    pm.set_chromecast_enabled(True)  # already on: the same player is kept
    assert len([p for p in FakePlayer.instances if isinstance(p, FakeChromecastPlayer)]) == 2
