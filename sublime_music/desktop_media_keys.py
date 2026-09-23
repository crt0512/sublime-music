"""Keyboard media key integration for GNOME-style Linux desktops.

GNOME, Cinnamon and MATE route the keyboard's media keys (play/pause, next, ...)
through their settings daemon, which delivers each key press to exactly one player. If
any application has grabbed the keys through the daemon's ``GrabMediaPlayerKeys`` API,
the most recent grabber gets them. Only when nobody has grabbed them does the daemon
fall back to MPRIS, and then it sticks with whichever MPRIS player appeared on the
session bus first, for as long as that player exists. Electron applications such as
VS Code or Discord register an idle MPRIS player the moment they start, so the fallback
often sends every key press to a window that plays nothing.

Grabbing the keys through the daemon sidesteps that: Sublime Music receives the keys
directly, and re-grabs them whenever its window gains focus so that it also wins over
other grabbers. The MPRIS interface stays in place for everything else (``playerctl``,
panel applets, KDE Connect, ...).

The module does nothing on desktops without such a daemon (KDE, sway, ...), where the
media keys keep reaching Sublime Music through MPRIS as before.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from gi.repository import Gio, GLib

# A media key handler. Its return value is ignored, so callbacks written for
# GLib.idle_add (which return a bool) can be passed as they are.
KeyHandler = Callable[[], Any]


@dataclass(frozen=True)
class SettingsDaemon:
    """Where a desktop's settings daemon exposes its media keys API on the session bus."""

    bus_name: str
    object_path: str
    interface: str


# Ordered by how recent the desktop is. Every entry uses the same method and signal
# names; only the bus name, path and interface differ.
SETTINGS_DAEMONS = (
    # GNOME since 3.24: each settings daemon plugin owns its own bus name.
    SettingsDaemon(
        "org.gnome.SettingsDaemon.MediaKeys",
        "/org/gnome/SettingsDaemon/MediaKeys",
        "org.gnome.SettingsDaemon.MediaKeys",
    ),
    # Cinnamon (which keeps GNOME's names for compatibility) and older GNOME.
    SettingsDaemon(
        "org.gnome.SettingsDaemon",
        "/org/gnome/SettingsDaemon/MediaKeys",
        "org.gnome.SettingsDaemon.MediaKeys",
    ),
    SettingsDaemon(
        "org.mate.SettingsDaemon",
        "/org/mate/SettingsDaemon/MediaKeys",
        "org.mate.SettingsDaemon.MediaKeys",
    ),
)

GRAB_METHOD = "GrabMediaPlayerKeys"
RELEASE_METHOD = "ReleaseMediaPlayerKeys"
KEY_PRESSED_SIGNAL = "MediaPlayerKeyPressed"

# Passing 0 as the grab time tells the daemon to use the current time, which puts us in
# front of every earlier grabber.
GRAB_TIME_NOW = 0
CALL_TIMEOUT_MS = 2000


@dataclass
class _Grab:
    daemon: SettingsDaemon
    connection: Gio.DBusConnection
    subscription_id: int


class DesktopMediaKeys:
    """Grab the keyboard's media keys from the desktop's settings daemon."""

    def __init__(
        self,
        app_name: str,
        play_pause: KeyHandler,
        pause: KeyHandler,
        next_track: KeyHandler,
        previous_track: KeyHandler,
        repeat: Optional[KeyHandler] = None,
        shuffle: Optional[KeyHandler] = None,
    ):
        self.app_name = app_name
        # Keyed by the key names the daemon sends. XF86AudioPlay is the play/pause
        # toggle on nearly every keyboard, and the daemon reports it as "Play".
        self._handlers: Dict[str, KeyHandler] = {
            "Play": play_pause,
            "Pause": pause,
            "Stop": pause,
            "Next": next_track,
            "Previous": previous_track,
        }
        if repeat:
            self._handlers["Repeat"] = repeat
        if shuffle:
            self._handlers["Shuffle"] = shuffle

        self._watcher_ids: Dict[str, int] = {}
        self._grabs: Dict[str, _Grab] = {}

    @property
    def grabbed(self) -> bool:
        """Whether a settings daemon currently hands the media keys to us."""
        return len(self._grabs) > 0

    def start(self):
        """Watch the session bus for a settings daemon and grab the keys when one is there.

        Watching (instead of calling once at startup) also covers the daemon starting
        after us during login, and the daemon being restarted later on.
        """
        for daemon in SETTINGS_DAEMONS:
            try:
                self._watcher_ids[daemon.bus_name] = Gio.bus_watch_name(
                    Gio.BusType.SESSION,
                    daemon.bus_name,
                    Gio.BusNameWatcherFlags.NONE,
                    self._on_daemon_appeared,
                    self._on_daemon_vanished,
                )
            except Exception:
                logging.exception(f"Could not watch the session bus for {daemon.bus_name}")

    def grab(self):
        """(Re-)grab the media keys from every daemon that is currently present."""
        for grab in self._grabs.values():
            self._call_grab(grab)

    def shutdown(self):
        for grab in list(self._grabs.values()):
            self._release(grab)
        for watcher_id in self._watcher_ids.values():
            Gio.bus_unwatch_name(watcher_id)
        self._watcher_ids = {}

    def _daemon_for(self, bus_name: str) -> Optional[SettingsDaemon]:
        return next((d for d in SETTINGS_DAEMONS if d.bus_name == bus_name), None)

    def _on_daemon_appeared(self, connection: Gio.DBusConnection, bus_name: str, owner: str):
        daemon = self._daemon_for(bus_name)
        if daemon is None or bus_name in self._grabs:
            return

        logging.info(f"Media keys: settings daemon {bus_name} is on the bus, grabbing keys")
        subscription_id = connection.signal_subscribe(
            bus_name,
            daemon.interface,
            KEY_PRESSED_SIGNAL,
            daemon.object_path,
            None,
            Gio.DBusSignalFlags.NONE,
            self._on_key_pressed,
        )
        grab = _Grab(daemon, connection, subscription_id)
        self._grabs[bus_name] = grab
        self._call_grab(grab)

    def _on_daemon_vanished(self, connection: Optional[Gio.DBusConnection], bus_name: str):
        grab = self._grabs.pop(bus_name, None)
        if grab is None:
            return
        logging.info(f"Media keys: settings daemon {bus_name} left the bus")
        if connection is not None:
            connection.signal_unsubscribe(grab.subscription_id)

    def _call_grab(self, grab: _Grab):
        def on_done(connection: Gio.DBusConnection, result: Gio.AsyncResult):
            try:
                connection.call_finish(result)
            except GLib.Error as e:
                logging.warning(
                    f"Media keys: could not grab them from {grab.daemon.bus_name}: {e.message}"
                )

        grab.connection.call(
            grab.daemon.bus_name,
            grab.daemon.object_path,
            grab.daemon.interface,
            GRAB_METHOD,
            GLib.Variant("(su)", (self.app_name, GRAB_TIME_NOW)),
            None,
            Gio.DBusCallFlags.NO_AUTO_START,
            CALL_TIMEOUT_MS,
            None,
            on_done,
        )

    def _release(self, grab: _Grab):
        # Best effort: the daemon drops the grab anyway once our connection goes away.
        try:
            grab.connection.call_sync(
                grab.daemon.bus_name,
                grab.daemon.object_path,
                grab.daemon.interface,
                RELEASE_METHOD,
                GLib.Variant("(s)", (self.app_name,)),
                None,
                Gio.DBusCallFlags.NO_AUTO_START,
                CALL_TIMEOUT_MS,
                None,
            )
        except GLib.Error as e:
            logging.info(f"Media keys: could not release them at {grab.daemon.bus_name}: {e}")
        grab.connection.signal_unsubscribe(grab.subscription_id)
        self._grabs.pop(grab.daemon.bus_name, None)

    def _on_key_pressed(
        self,
        connection: Gio.DBusConnection,
        sender: str,
        object_path: str,
        interface: str,
        signal: str,
        params: GLib.Variant,
    ):
        application, key = params.unpack()
        if application != self.app_name:
            return

        handler = self._handlers.get(key)
        if handler is None:
            logging.debug(f"Media keys: ignoring unhandled key {key}")
            return
        try:
            handler()
        except Exception:
            logging.exception(f"Media keys: handling {key} failed")
