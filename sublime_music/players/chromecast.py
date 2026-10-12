import logging
from datetime import timedelta
from typing import Any, Callable, Dict, Optional, Set, Tuple, Type, Union, cast
from urllib.parse import urlparse
from uuid import UUID

import pychromecast
from gi.repository import GLib

from ..adapters import AdapterManager
from ..adapters.api_objects import Song
from .base import Player, PlayerDeviceEvent, PlayerEvent
from .lan_server import LanFileServer

try:
    from pychromecast.controllers.media import MediaStatusListener
    from pychromecast.controllers.receiver import CastStatusListener
except ImportError:  # pychromecast before 10 has no listener base classes
    MediaStatusListener = CastStatusListener = object  # type: ignore[misc, assignment]

SERVE_FILES_KEY = "Serve Local Files to Chromecasts on the LAN"


class ChromecastPlayer(Player, CastStatusListener, MediaStatusListener):
    name = "Chromecast"
    can_start_playing_with_no_latency = False

    @property
    def enabled(self) -> bool:
        return True

    @staticmethod
    def get_configuration_options() -> Dict[str, Union[Type, Tuple[str, ...]]]:
        return {SERVE_FILES_KEY: bool}

    @property
    def supported_schemes(self) -> Set[str]:
        schemes = {"http", "https"}
        # Only while the LAN server is up; otherwise stream from the music server.
        if self.config.get(SERVE_FILES_KEY) and self.server.running:
            schemes.add("file")
        return schemes

    _timepos = 0.0

    def __init__(
        self,
        on_timepos_change: Callable[[Optional[float]], None],
        on_track_end: Callable[[], None],
        on_player_event: Callable[[PlayerEvent], None],
        player_device_change_callback: Callable[[PlayerDeviceEvent], None],
        config: Dict[str, Union[str, int, bool]],
    ):
        self.server = LanFileServer("Chromecasts")
        self.on_timepos_change = on_timepos_change
        self.on_track_end = on_track_end
        self.on_player_event = on_player_event
        self.player_device_change_callback = player_device_change_callback

        self.change_settings(config)

        self._chromecasts: Dict[UUID, pychromecast.Chromecast] = {}
        self._current_chromecast: Optional[pychromecast.Chromecast] = None

        self.chromecast_browser: Any = None
        self.refresh_players()

    def chromecast_discovered_callback(self, chromecast: Any):
        chromecast = cast(pychromecast.Chromecast, chromecast)
        self._chromecasts[chromecast.cast_info.uuid] = chromecast
        self.player_device_change_callback(
            PlayerDeviceEvent(
                PlayerDeviceEvent.Delta.ADD,
                type(self),
                str(chromecast.cast_info.uuid),
                chromecast.cast_info.friendly_name or "Chromecast",
            )
        )

    def change_settings(self, config: Dict[str, Union[str, int, bool]]):
        self.config = config
        if self.config.get(SERVE_FILES_KEY):
            if not self.server.running:
                self.server.start()
        else:
            self.server.stop()

    def refresh_players(self):
        for id_, chromecast in self._chromecasts.items():
            self.player_device_change_callback(
                PlayerDeviceEvent(
                    PlayerDeviceEvent.Delta.REMOVE,
                    type(self),
                    str(id_),
                    chromecast.cast_info.friendly_name or "Chromecast",
                )
            )
        self._chromecasts = {}

        self.chromecast_browser = pychromecast.get_chromecasts(
            blocking=False, callback=self.chromecast_discovered_callback
        )

    def set_current_device_id(self, device_id: str):
        chromecast = self._chromecasts[UUID(device_id)]
        self._current_chromecast = chromecast
        chromecast.media_controller.register_status_listener(self)
        if hasattr(chromecast, "register_status_listener"):  # pychromecast before 14
            chromecast.register_status_listener(self)
        else:
            chromecast.socket_client.receiver_controller.register_status_listener(self)
        chromecast.wait()

    def new_cast_status(self, status: Any):
        assert self._current_chromecast
        self.on_player_event(
            PlayerEvent(
                PlayerEvent.EventType.VOLUME_CHANGE,
                str(self._current_chromecast.cast_info.uuid),
                volume=(status.volume_level * 100 if not status.volume_muted else 0),
            )
        )

        # This happens when the Chromecast is first activated or after "Stop Casting" is
        # pressed in the Google Home app. Reset `song_loaded` so that it calls
        # `play_media` the next time around rather than trying to toggle the play state.
        if status.session_id is None:
            self.song_loaded = False

    def new_launch_error(self, status: Any):
        logging.warning(f"The Chromecast could not launch the receiver app: {status}")

    def load_media_failed(self, queue_item_id: int, error_code: int):
        logging.warning(f"The Chromecast could not load item {queue_item_id}: error {error_code}")

    time_increment_order_token = 0

    def new_media_status(self, status: Any):
        # Detect the end of a track and go to the next one.
        if (
            status.idle_reason == "FINISHED"
            and status.player_state == "IDLE"
            and self._timepos > 0
        ):
            logging.debug("Chromecast track ended")
            self.on_track_end()
            return

        self.song_loaded = True

        self._timepos = status.current_time

        assert self._current_chromecast
        self.on_player_event(
            PlayerEvent(
                PlayerEvent.EventType.PLAY_STATE_CHANGE,
                str(self._current_chromecast.cast_info.uuid),
                playing=(status.player_state in ("PLAYING", "BUFFERING")),
            )
        )

        def increment_time(order_token: int):
            if self.time_increment_order_token != order_token or not self.playing:
                return

            self._timepos += 0.5
            self.on_timepos_change(self._timepos)
            GLib.timeout_add(500, increment_time, order_token)

        self.time_increment_order_token += 1
        GLib.timeout_add(500, increment_time, self.time_increment_order_token)

    def reset(self):
        pass

    def shutdown(self):
        self.server.stop()

        if self.chromecast_browser:
            try:
                self.chromecast_browser.stop_discovery()
            except Exception:
                logging.exception("Could not stop the Chromecast discovery")

        try:
            assert self._current_chromecast
            self._current_chromecast.quit_app()
        except Exception:
            pass

    @property
    def playing(self) -> bool:
        if not self._current_chromecast or not self._current_chromecast.media_controller:
            return False
        return self._current_chromecast.media_controller.status.player_is_playing

    def get_volume(self) -> float:
        if self._current_chromecast and self._current_chromecast.status:
            # The volume is in the range [0, 1]. Multiply by 100 to get to [0, 100].
            return self._current_chromecast.status.volume_level * 100
        else:
            return 100

    def set_volume(self, volume: float):
        if self._current_chromecast:
            # volume value is in [0, 100]. Convert to [0, 1] for Chromecast.
            self._current_chromecast.set_volume(volume / 100)

    def get_is_muted(self) -> bool:
        if not self._current_chromecast:
            return False
        # The mute state lives on the status (Chromecast.volume_muted is gone since 14).
        status = self._current_chromecast.status
        return bool(status and status.volume_muted)

    def set_muted(self, muted: bool):
        if not self._current_chromecast:
            return
        self._current_chromecast.set_volume_muted(muted)

    def play_media(self, uri: str, progress: timedelta, song: Song):
        assert self._current_chromecast
        if urlparse(uri).scheme == "file":
            uri = self.server.serve(
                uri,
                peer=getattr(self._current_chromecast.cast_info, "host", None),
                title=song.title,
            )

        assert AdapterManager._instance
        networked_scheme_priority = ("https", "http")
        scheme = sorted(
            AdapterManager._instance.ground_truth_adapter.supported_schemes,
            key=lambda s: networked_scheme_priority.index(s),
        )[0]
        cover_art_url = AdapterManager.get_cover_art_uri(
            song.cover_art, scheme, size=1000
        ).result()
        self._current_chromecast.media_controller.play_media(
            uri,
            # Just pretend that whatever we send it is mp3, even if it isn't.
            "audio/mp3",
            current_time=progress.total_seconds(),
            title=song.title,
            thumb=cover_art_url,
            metadata={
                "metadataType": 3,
                "albumName": song.album.name if song.album else None,
                "artist": song.artist.name if song.artist else None,
                "trackNumber": song.track,
            },
        )

        # Make sure to clear out the cache duration state.
        self.on_player_event(
            PlayerEvent(
                PlayerEvent.EventType.STREAM_CACHE_PROGRESS_CHANGE,
                str(self._current_chromecast.cast_info.uuid),
                stream_cache_duration=0,
            )
        )
        self._timepos = progress.total_seconds()

    def pause(self):
        if self._current_chromecast and self._current_chromecast.media_controller:
            self._current_chromecast.media_controller.pause()

    def play(self):
        if self._current_chromecast and self._current_chromecast.media_controller:
            self._current_chromecast.media_controller.play()

    def seek(self, position: timedelta):
        if not self._current_chromecast:
            return

        do_pause = not self.playing
        self._current_chromecast.media_controller.seek(position.total_seconds())
        if do_pause:
            self.pause()

    def _wait_for_playing(self):
        pass

    def next_media_cached(self, *_):
        pass
