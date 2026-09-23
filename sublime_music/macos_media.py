"""macOS Now Playing / media key integration.

This module is optional: importing it should be safe on non-macOS systems or when
PyObjC is not installed. The packaged macOS app bundles PyObjC's MediaPlayer bridge,
which lets Sublime Music appear in Control Center/Now Playing and receive remote
playback commands.
"""

from __future__ import annotations

import logging
import sys
from datetime import timedelta
from typing import Any, Callable, List, Optional, Tuple

from .adapters.api_objects import Song

# A plain boolean (rather than comparing sys.platform inline) so that the type checker
# analyses the import block on every platform.
IS_MACOS = sys.platform == "darwin"

if IS_MACOS:
    try:
        from MediaPlayer import (
            MPMediaItemPropertyAlbumTitle,
            MPMediaItemPropertyArtist,
            MPMediaItemPropertyPlaybackDuration,
            MPMediaItemPropertyTitle,
            MPNowPlayingInfoCenter,
            MPNowPlayingInfoPropertyDefaultPlaybackRate,
            MPNowPlayingInfoPropertyElapsedPlaybackTime,
            MPNowPlayingInfoPropertyPlaybackRate,
            MPRemoteCommandCenter,
            MPRemoteCommandHandlerStatusCommandFailed,
            MPRemoteCommandHandlerStatusSuccess,
        )

        MEDIAPLAYER_AVAILABLE = True
    except Exception:
        MEDIAPLAYER_AVAILABLE = False
else:
    MEDIAPLAYER_AVAILABLE = False


class MacOSMediaSession:
    """Bridge Sublime Music playback state to macOS Now Playing."""

    def __init__(
        self,
        play: Callable[[], None],
        pause: Callable[[], None],
        play_pause: Callable[[], None],
        next_track: Callable[[], None],
        previous_track: Callable[[], None],
        seek: Callable[[float], None],
    ):
        self.available = MEDIAPLAYER_AVAILABLE
        self._command_targets: List[Tuple[Any, Any]] = []
        self._last_song_id: Optional[str] = None
        self._last_playing: Optional[bool] = None
        self._last_elapsed_seconds: Optional[int] = None
        self._last_duration_seconds: Optional[int] = None

        if not self.available:
            logging.info("macOS MediaPlayer bridge is unavailable; Now Playing disabled.")
            return

        self._now_playing_center = MPNowPlayingInfoCenter.defaultCenter()
        self._command_center = MPRemoteCommandCenter.sharedCommandCenter()
        self._install_command(self._command_center.playCommand(), play)
        self._install_command(self._command_center.pauseCommand(), pause)
        self._install_command(self._command_center.togglePlayPauseCommand(), play_pause)
        self._install_command(self._command_center.nextTrackCommand(), next_track)
        self._install_command(self._command_center.previousTrackCommand(), previous_track)
        self._install_seek_command(seek)

    def _install_command(self, command: Any, callback: Callable[[], None]):
        command.setEnabled_(True)

        def handler(_event: Any) -> int:
            try:
                callback()
                return MPRemoteCommandHandlerStatusSuccess
            except Exception:
                logging.exception("macOS media command failed")
                return MPRemoteCommandHandlerStatusCommandFailed

        self._command_targets.append((command, command.addTargetWithHandler_(handler)))

    def _install_seek_command(self, callback: Callable[[float], None]):
        command = self._command_center.changePlaybackPositionCommand()
        command.setEnabled_(True)

        def handler(event: Any) -> int:
            try:
                callback(float(event.positionTime()))
                return MPRemoteCommandHandlerStatusSuccess
            except Exception:
                logging.exception("macOS media seek command failed")
                return MPRemoteCommandHandlerStatusCommandFailed

        self._command_targets.append((command, command.addTargetWithHandler_(handler)))

    def update(
        self,
        song: Optional[Song],
        progress: Optional[timedelta],
        playing: bool,
    ):
        """Publish current playback metadata to macOS Now Playing."""
        if not self.available:
            return

        if not song:
            self.clear()
            return

        elapsed_seconds = int(progress.total_seconds()) if progress else 0
        duration_seconds = int(song.duration.total_seconds()) if song.duration else 0
        if (
            song.id == self._last_song_id
            and playing == self._last_playing
            and elapsed_seconds == self._last_elapsed_seconds
            and duration_seconds == self._last_duration_seconds
        ):
            return

        info = {
            MPMediaItemPropertyTitle: song.title,
            MPNowPlayingInfoPropertyElapsedPlaybackTime: elapsed_seconds,
            MPNowPlayingInfoPropertyPlaybackRate: 1.0 if playing else 0.0,
            MPNowPlayingInfoPropertyDefaultPlaybackRate: 1.0,
        }
        if song.artist:
            info[MPMediaItemPropertyArtist] = song.artist.name
        if song.album:
            info[MPMediaItemPropertyAlbumTitle] = song.album.name
        if duration_seconds:
            info[MPMediaItemPropertyPlaybackDuration] = duration_seconds

        self._now_playing_center.setNowPlayingInfo_(info)
        self._last_song_id = song.id
        self._last_playing = playing
        self._last_elapsed_seconds = elapsed_seconds
        self._last_duration_seconds = duration_seconds

    def clear(self):
        if not self.available:
            return
        self._now_playing_center.setNowPlayingInfo_(None)
        self._last_song_id = None
        self._last_playing = None
        self._last_elapsed_seconds = None
        self._last_duration_seconds = None

    def shutdown(self):
        if not self.available:
            return
        self.clear()
        for command, target in self._command_targets:
            try:
                command.removeTarget_(target)
            except Exception:
                logging.exception("Could not remove macOS media command target")
        self._command_targets = []
