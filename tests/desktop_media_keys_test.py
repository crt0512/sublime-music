from typing import Any, Callable, Dict, List, Optional

import pytest
from gi.repository import Gio, GLib

from sublime_music import desktop_media_keys
from sublime_music.desktop_media_keys import (
    GRAB_METHOD,
    KEY_PRESSED_SIGNAL,
    RELEASE_METHOD,
    SETTINGS_DAEMONS,
    DesktopMediaKeys,
)

CINNAMON = SETTINGS_DAEMONS[1]
APP = "Sublime Music"


class FakeConnection:
    """Records the D-Bus traffic the grabber would send, without a bus."""

    def __init__(self):
        self.calls: List[Dict[str, Any]] = []
        self.subscriptions: Dict[int, Dict[str, Any]] = {}
        self.unsubscribed: List[int] = []
        self._next_subscription = 1

    def signal_subscribe(
        self,
        sender: Optional[str],
        interface: str,
        member: str,
        path: str,
        arg0: Optional[str],
        flags: Any,
        callback: Callable[..., None],
    ) -> int:
        subscription_id = self._next_subscription
        self._next_subscription += 1
        self.subscriptions[subscription_id] = {
            "sender": sender,
            "interface": interface,
            "member": member,
            "path": path,
            "callback": callback,
        }
        return subscription_id

    def signal_unsubscribe(self, subscription_id: int) -> None:
        self.unsubscribed.append(subscription_id)
        self.subscriptions.pop(subscription_id, None)

    def call(
        self,
        name: str,
        path: str,
        iface: str,
        method: str,
        params: GLib.Variant,
        reply: Any,
        flags: Any,
        timeout: int,
        cancel: Any,
        callback: Callable[..., None],
    ) -> None:
        self.calls.append(
            {"name": name, "path": path, "iface": iface, "method": method, "params": params}
        )
        callback(self, None)

    def call_finish(self, result: Any) -> GLib.Variant:
        return GLib.Variant("()", ())

    def call_sync(
        self,
        name: str,
        path: str,
        iface: str,
        method: str,
        params: GLib.Variant,
        reply: Any,
        flags: Any,
        timeout: int,
        cancel: Any,
    ) -> GLib.Variant:
        self.calls.append(
            {"name": name, "path": path, "iface": iface, "method": method, "params": params}
        )
        return GLib.Variant("()", ())

    def press(self, application: str, key: str) -> None:
        """Deliver a key press the way the daemon does: as a signal to our connection."""
        for subscription in list(self.subscriptions.values()):
            subscription["callback"](
                self,
                ":1.27",
                subscription["path"],
                subscription["interface"],
                KEY_PRESSED_SIGNAL,
                GLib.Variant("(ss)", (application, key)),
            )


class Watchers:
    """Stand-in for Gio.bus_watch_name so tests can make daemons appear and vanish."""

    def __init__(self):
        self.watched: Dict[str, Dict[str, Any]] = {}
        self.unwatched: List[int] = []
        self._next_id = 1

    def watch(
        self,
        bus_type: Any,
        name: str,
        flags: Any,
        appeared: Callable[..., None],
        vanished: Callable[..., None],
    ) -> int:
        assert bus_type == Gio.BusType.SESSION
        assert flags == Gio.BusNameWatcherFlags.NONE
        watcher_id = self._next_id
        self._next_id += 1
        self.watched[name] = {"id": watcher_id, "appeared": appeared, "vanished": vanished}
        return watcher_id

    def unwatch(self, watcher_id: int) -> None:
        self.unwatched.append(watcher_id)

    def appear(self, name: str, connection: FakeConnection) -> None:
        self.watched[name]["appeared"](connection, name, ":1.27")

    def vanish(self, name: str, connection: Optional[FakeConnection]) -> None:
        self.watched[name]["vanished"](connection, name)


@pytest.fixture
def watchers(monkeypatch: pytest.MonkeyPatch) -> Watchers:
    watchers = Watchers()
    monkeypatch.setattr(desktop_media_keys.Gio, "bus_watch_name", watchers.watch)
    monkeypatch.setattr(desktop_media_keys.Gio, "bus_unwatch_name", watchers.unwatch)
    return watchers


@pytest.fixture
def pressed() -> List[str]:
    return []


@pytest.fixture
def media_keys(pressed: List[str]) -> DesktopMediaKeys:
    return DesktopMediaKeys(
        APP,
        play_pause=lambda: pressed.append("play_pause"),
        pause=lambda: pressed.append("pause"),
        next_track=lambda: pressed.append("next"),
        previous_track=lambda: pressed.append("previous"),
        repeat=lambda: pressed.append("repeat"),
        shuffle=lambda: pressed.append("shuffle"),
    )


def test_start_watches_every_known_daemon(watchers: Watchers, media_keys: DesktopMediaKeys):
    media_keys.start()
    assert set(watchers.watched) == {d.bus_name for d in SETTINGS_DAEMONS}
    assert not media_keys.grabbed


def test_grabs_keys_when_daemon_appears(watchers: Watchers, media_keys: DesktopMediaKeys):
    media_keys.start()
    connection = FakeConnection()
    watchers.appear(CINNAMON.bus_name, connection)

    assert media_keys.grabbed
    assert connection.calls == [
        {
            "name": CINNAMON.bus_name,
            "path": CINNAMON.object_path,
            "iface": CINNAMON.interface,
            "method": GRAB_METHOD,
            "params": GLib.Variant("(su)", (APP, 0)),
        }
    ]
    (subscription,) = connection.subscriptions.values()
    assert subscription["sender"] == CINNAMON.bus_name
    assert subscription["interface"] == CINNAMON.interface
    assert subscription["member"] == KEY_PRESSED_SIGNAL
    assert subscription["path"] == CINNAMON.object_path

    # Appearing twice (e.g. a name owner change) must not double-subscribe.
    watchers.appear(CINNAMON.bus_name, connection)
    assert len(connection.subscriptions) == 1
    assert len(connection.calls) == 1


def test_key_presses_are_routed_to_handlers(
    watchers: Watchers, media_keys: DesktopMediaKeys, pressed: List[str]
):
    media_keys.start()
    connection = FakeConnection()
    watchers.appear(CINNAMON.bus_name, connection)

    for key in ("Play", "Pause", "Stop", "Next", "Previous", "Repeat", "Shuffle", "Rewind"):
        connection.press(APP, key)
    assert pressed == ["play_pause", "pause", "pause", "next", "previous", "repeat", "shuffle"]

    # Keys grabbed by another application are not ours.
    connection.press("Rhythmbox", "Play")
    assert pressed[-1] == "shuffle"


def test_handler_errors_do_not_propagate(watchers: Watchers):
    def boom() -> None:
        raise RuntimeError("no song")

    media_keys = DesktopMediaKeys(APP, boom, boom, boom, boom)
    media_keys.start()
    connection = FakeConnection()
    watchers.appear(CINNAMON.bus_name, connection)
    connection.press(APP, "Play")


def test_regrab_repeats_the_grab_call(watchers: Watchers, media_keys: DesktopMediaKeys):
    media_keys.start()
    media_keys.grab()  # No daemon yet: nothing to do.
    connection = FakeConnection()
    watchers.appear(CINNAMON.bus_name, connection)
    media_keys.grab()
    assert [c["method"] for c in connection.calls] == [GRAB_METHOD, GRAB_METHOD]


def test_daemon_vanishing_drops_the_grab(watchers: Watchers, media_keys: DesktopMediaKeys):
    media_keys.start()
    connection = FakeConnection()
    watchers.appear(CINNAMON.bus_name, connection)
    (subscription_id,) = connection.subscriptions

    watchers.vanish(CINNAMON.bus_name, connection)
    assert not media_keys.grabbed
    assert connection.unsubscribed == [subscription_id]

    # The bus itself going away hands us no connection; that must not crash either.
    watchers.appear(CINNAMON.bus_name, connection)
    watchers.vanish(CINNAMON.bus_name, None)
    assert not media_keys.grabbed


def test_shutdown_releases_keys_and_stops_watching(
    watchers: Watchers, media_keys: DesktopMediaKeys
):
    media_keys.start()
    connection = FakeConnection()
    watchers.appear(CINNAMON.bus_name, connection)

    media_keys.shutdown()
    assert [c["method"] for c in connection.calls] == [GRAB_METHOD, RELEASE_METHOD]
    assert connection.calls[-1]["params"] == GLib.Variant("(s)", (APP,))
    assert not media_keys.grabbed
    assert len(connection.subscriptions) == 0
    assert sorted(watchers.unwatched) == sorted(w["id"] for w in watchers.watched.values())
