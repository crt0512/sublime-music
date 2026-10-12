"""
The UPnP player talks to renderers with SSDP and SOAP over the network; these tests
exercise the parsing and the playback logic against a fake renderer instead.
"""

import time
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Tuple
from unittest import mock

import pytest

from sublime_music.players import upnp
from sublime_music.players.base import PlayerEvent
from sublime_music.players.upnp import (
    AV_TRANSPORT,
    UPnPError,
    UPnPPlayer,
    UPnPRenderer,
    build_didl_metadata,
    build_soap_envelope,
    parse_device_description,
    parse_soap_response,
)


def Song(**fields: Any) -> Any:  # noqa: N802
    """The parts of an api_objects.Song (an abstract class) that the player reads."""
    defaults = {
        "id": "s1",
        "title": "Song",
        "suffix": None,
        "track": None,
        "duration": None,
        "artist": None,
        "album": None,
        "cover_art": None,
    }
    return SimpleNamespace(**{**defaults, **fields})


DESCRIPTION = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <device>
    <deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>
    <friendlyName>Living Room</friendlyName>
    <UDN>uuid:1234-5678</UDN>
    <serviceList>
      <service>
        <serviceType>urn:schemas-upnp-org:service:AVTransport:2</serviceType>
        <controlURL>/AVTransport/control</controlURL>
      </service>
      <service>
        <serviceType>urn:schemas-upnp-org:service:RenderingControl:1</serviceType>
        <controlURL>RenderingControl/control</controlURL>
      </service>
      <service>
        <serviceType>urn:schemas-upnp-org:service:ConnectionManager:1</serviceType>
        <controlURL>/ConnectionManager/control</controlURL>
      </service>
    </serviceList>
  </device>
</root>
"""

# A media server with an embedded renderer that can't be controlled (no AVTransport).
NESTED_DESCRIPTION = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <URLBase>http://10.0.0.5:49152/</URLBase>
  <device>
    <deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType>
    <friendlyName>10.0.0.5 - NAS</friendlyName>
    <UDN>uuid:server</UDN>
    <deviceList>
      <device>
        <deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>
        <friendlyName>Kitchen Media Renderer</friendlyName>
        <UDN>uuid:kitchen</UDN>
        <serviceList>
          <service>
            <serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>
            <controlURL>avt</controlURL>
          </service>
          <service>
            <serviceType>urn:schemas-upnp-org:service:RenderingControl:1</serviceType>
            <controlURL>rc</controlURL>
          </service>
        </serviceList>
      </device>
      <device>
        <deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>
        <friendlyName>Broken</friendlyName>
        <UDN>uuid:broken</UDN>
        <serviceList>
          <service>
            <serviceType>urn:schemas-upnp-org:service:RenderingControl:1</serviceType>
            <controlURL>rc</controlURL>
          </service>
        </serviceList>
      </device>
    </deviceList>
  </device>
</root>
"""


def test_parse_device_description_resolves_control_urls_against_the_location():
    renderers = parse_device_description(DESCRIPTION, "http://192.168.1.20:8080/desc.xml")
    assert renderers == [
        UPnPRenderer(
            udn="uuid:1234-5678",
            name="Living Room",
            av_transport_url="http://192.168.1.20:8080/AVTransport/control",
            rendering_control_url="http://192.168.1.20:8080/RenderingControl/control",
            connection_manager_url="http://192.168.1.20:8080/ConnectionManager/control",
        )
    ]
    assert renderers[0].id == "upnp:uuid:1234-5678"


def test_parse_device_description_finds_embedded_renderers_and_uses_urlbase():
    renderers = parse_device_description(NESTED_DESCRIPTION, "http://10.0.0.5:49152/desc.xml")
    # "Broken" has no AVTransport, and "Kitchen Media Renderer" loses the redundant suffix.
    assert [r.name for r in renderers] == ["Kitchen"]
    assert renderers[0].av_transport_url == "http://10.0.0.5:49152/avt"
    assert renderers[0].connection_manager_url is None


def test_parse_device_description_tolerates_garbage():
    assert parse_device_description("<not xml", "http://x/desc.xml") == []
    assert parse_device_description("<root/>", "http://x/desc.xml") == []


def test_soap_envelope_escapes_arguments():
    envelope = build_soap_envelope(AV_TRANSPORT, "SetAVTransportURI", {"CurrentURI": "a&b<c"})
    assert 'xmlns:u="urn:schemas-upnp-org:service:AVTransport:1"' in envelope
    assert "<u:SetAVTransportURI" in envelope
    assert "<CurrentURI>a&amp;b&lt;c</CurrentURI>" in envelope


def test_parse_soap_response_returns_the_output_arguments():
    response = """<?xml version="1.0"?>
    <s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">
      <s:Body>
        <u:GetPositionInfoResponse xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">
          <Track>1</Track><TrackDuration>0:03:25</TrackDuration><RelTime>0:01:02</RelTime>
        </u:GetPositionInfoResponse>
      </s:Body>
    </s:Envelope>"""
    assert parse_soap_response(response, "GetPositionInfo") == {
        "Track": "1",
        "TrackDuration": "0:03:25",
        "RelTime": "0:01:02",
    }


def test_parse_soap_response_raises_on_faults():
    fault = """<?xml version="1.0"?>
    <s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">
      <s:Body><s:Fault>
        <faultcode>s:Client</faultcode><faultstring>UPnPError</faultstring>
        <detail><UPnPError xmlns="urn:schemas-upnp-org:control-1-0">
          <errorCode>716</errorCode><errorDescription>Resource not found</errorDescription>
        </UPnPError></detail>
      </s:Fault></s:Body>
    </s:Envelope>"""
    with pytest.raises(UPnPError, match=r"Seek failed: Resource not found \(716\)"):
        parse_soap_response(fault, "Seek")
    with pytest.raises(UPnPError):
        parse_soap_response("<s:Envelope/>", "Play")
    with pytest.raises(UPnPError):
        parse_soap_response("not xml", "Play")


def test_didl_metadata_describes_the_song():
    song = Song(
        id="s1",
        title="Rock & Roll",
        suffix="flac",
        track=7,
        duration=timedelta(minutes=3, seconds=25),
        artist=SimpleNamespace(name="AC/DC"),
        album=SimpleNamespace(name="<Unknown>"),
    )
    metadata = build_didl_metadata(song, "http://host/s/abc?x=1&y=2", "http://host/cover.jpg")
    assert "<dc:title>Rock &amp; Roll</dc:title>" in metadata
    assert "<upnp:artist>AC/DC</upnp:artist>" in metadata
    assert "<upnp:album>&lt;Unknown&gt;</upnp:album>" in metadata
    assert "<upnp:originalTrackNumber>7</upnp:originalTrackNumber>" in metadata
    assert "<upnp:albumArtURI>http://host/cover.jpg</upnp:albumArtURI>" in metadata
    assert 'protocolInfo="http-get:*:audio/flac:DLNA.ORG_OP=01;' in metadata
    transcoded = build_didl_metadata(song, "http://h/s/t/song.mp3", None, "audio/mpeg", True)
    assert "http-get:*:audio/mpeg:DLNA.ORG_PN=MP3;DLNA.ORG_OP=00;DLNA.ORG_CI=1;" in transcoded
    assert 'duration="0:03:25"' in metadata
    assert ">http://host/s/abc?x=1&amp;y=2</res>" in metadata


# ######## The player, against a fake renderer ######## #


class FakeRenderer:
    """Records every SOAP call and answers with its transport state."""

    def __init__(self):
        self.calls: List[Tuple[str, str, Dict[str, Any]]] = []
        self.state = "STOPPED"
        self.volume = 42
        self.muted = False
        self.can_play = True
        self.sink = ""  # what GetProtocolInfo says it plays ("" = no ConnectionManager)

    def soap_call(
        self, control_url: str, service_type: str, action: str, arguments: Dict[str, Any], **_
    ) -> Dict[str, str]:
        self.calls.append((service_type, action, arguments))
        if action == "Play" and not self.can_play:
            raise UPnPError("Play failed: Transition not available (701)")
        if action == "GetProtocolInfo":
            return {"Source": "", "Sink": self.sink}
        if action == "GetTransportInfo":
            return {"CurrentTransportState": self.state}
        if action == "GetVolume":
            return {"CurrentVolume": str(self.volume)}
        if action == "GetMute":
            return {"CurrentMute": "1" if self.muted else "0"}
        if action == "Play":
            self.state = "PLAYING"
        if action in ("Stop", "Pause"):
            self.state = "STOPPED" if action == "Stop" else "PAUSED_PLAYBACK"
        return {}

    def actions(self) -> List[str]:
        return [action for _, action, _ in self.calls]


class Events:
    def __init__(self):
        self.player_events: List[PlayerEvent] = []
        self.track_ends = 0
        self.timepos: List[Any] = []
        self.devices: List[Any] = []


@pytest.fixture
def renderer(monkeypatch: pytest.MonkeyPatch) -> FakeRenderer:
    fake = FakeRenderer()
    monkeypatch.setattr(upnp, "soap_call", fake.soap_call)
    # No network: nothing to discover, callbacks run right away instead of on the GTK loop.
    monkeypatch.setattr(upnp, "discover_renderer_locations", lambda *a, **k: iter(()))
    monkeypatch.setattr(upnp.GLib, "idle_add", lambda fn, *a: fn(*a))
    # The poll loop is driven by hand in these tests.
    monkeypatch.setattr(UPnPPlayer, "_start_polling", lambda self: None)
    return fake


@pytest.fixture
def events() -> Events:
    return Events()


@pytest.fixture
def player(renderer: FakeRenderer, events: Events) -> Iterator[UPnPPlayer]:
    player = UPnPPlayer(
        on_timepos_change=events.timepos.append,
        on_track_end=lambda: setattr(events, "track_ends", events.track_ends + 1),
        on_player_event=events.player_events.append,
        player_device_change_callback=events.devices.append,
        config={upnp.STREAM_THROUGH_KEY: False},  # no real LAN server in the tests
    )
    device = UPnPRenderer("uuid:tv", "TV", "http://tv/avt", "http://tv/rc", "http://tv/cm")
    player._renderers[device.udn] = device
    player.set_current_device_id(device.id)
    player._commands.shutdown(wait=True)  # flush the volume and capabilities reads
    player._commands = _SyncExecutor()  # type: ignore
    yield player
    player._shutting_down.set()


class _SyncExecutor:
    """Runs commands inline so that tests see their effect immediately."""

    def submit(self, fn: Any, *args: Any, **kwargs: Any):
        fn(*args, **kwargs)

    def shutdown(self, wait: bool = True):
        pass


def test_selecting_a_device_reads_its_volume(player: UPnPPlayer, renderer: FakeRenderer):
    assert player.get_volume() == 42
    assert player.get_is_muted() is False
    assert renderer.actions() == ["GetVolume", "GetMute", "GetProtocolInfo"]
    assert player._sink_mimes is None  # it didn't say what it plays


def test_play_media_loads_and_plays(player: UPnPPlayer, renderer: FakeRenderer, events: Events):
    renderer.calls.clear()
    song = Song(id="s1", title="Song", duration=timedelta(seconds=200))
    player.play_media("http://music/stream?id=s1", timedelta(0), song)

    assert renderer.actions() == ["Stop", "SetAVTransportURI", "Play"]
    _, _, args = renderer.calls[1]
    assert args["CurrentURI"] == "http://music/stream?id=s1"
    assert "<dc:title>Song</dc:title>" in args["CurrentURIMetaData"]
    assert renderer.calls[2][2]["Speed"] == "1"
    assert player.playing
    assert player.song_loaded
    assert events.player_events[-1].type == PlayerEvent.EventType.STREAM_CACHE_PROGRESS_CHANGE


def test_a_renderer_that_cannot_fetch_the_song_reports_not_playing(
    player: UPnPPlayer, renderer: FakeRenderer, events: Events
):
    renderer.can_play = False
    player.play_media("http://unreachable/s1", timedelta(0), Song(id="s1"))
    assert not player.playing and not player.song_loaded
    assert events.player_events[-1].type == PlayerEvent.EventType.PLAY_STATE_CHANGE
    assert events.player_events[-1].playing is False
    # And a later STOPPED poll is not taken for the end of a song.
    _poll(player, "STOPPED")
    assert events.track_ends == 0


def test_local_files_are_only_offered_while_the_server_runs(player: UPnPPlayer):
    player.server = mock.Mock(running=False)
    assert player.supported_schemes == {"http", "https"}
    player.server = mock.Mock(running=True)
    assert player.supported_schemes == {"http", "https", "file"}


def test_play_media_seeks_when_resuming_mid_song(player: UPnPPlayer, renderer: FakeRenderer):
    renderer.calls.clear()
    player.play_media("http://music/stream?id=s1", timedelta(seconds=65), Song(id="s1"))
    assert renderer.actions()[-1] == "Seek"
    assert renderer.calls[-1][2] == {"InstanceID": 0, "Unit": "REL_TIME", "Target": "0:01:05"}


def test_songs_go_through_the_lan_server_when_it_runs(player: UPnPPlayer, renderer: FakeRenderer):
    player.server = mock.Mock(
        running=True, serve=mock.Mock(return_value="http://10.0.0.2:40000/s/tok")
    )
    player.play_media("file:///home/me/.cache/song.mp3", timedelta(0), Song(id="s1"))
    assert renderer.calls[-2][2]["CurrentURI"] == "http://10.0.0.2:40000/s/tok"
    # Served from the address that routes to the renderer.
    player.server.serve.assert_called_once_with(
        "file:///home/me/.cache/song.mp3",
        peer="tv",
        verify_cert=True,
        title="Song",
        transcode=None,
        suffix=None,
    )

    # Songs on the music server are relayed too, so the renderer never talks to it.
    player.server.serve.reset_mock()
    player.play_media("https://music/stream?id=s1", timedelta(0), Song(id="s1"))
    assert renderer.calls[-2][2]["CurrentURI"] == "http://10.0.0.2:40000/s/tok"
    player.server.serve.assert_called_once_with(
        "https://music/stream?id=s1",
        peer="tv",
        verify_cert=True,
        title="Song",
        transcode=None,
        suffix=None,
    )


def test_songs_stream_directly_when_the_lan_server_is_off(
    player: UPnPPlayer, renderer: FakeRenderer
):
    assert not player.server.running  # the fixture's config leaves it off
    player.play_media("https://music/stream?id=s1", timedelta(0), Song(id="s1"))
    assert renderer.calls[-2][2]["CurrentURI"] == "https://music/stream?id=s1"


def test_transport_commands(player: UPnPPlayer, renderer: FakeRenderer):
    renderer.state = "PLAYING"
    renderer.calls.clear()
    player.pause()
    assert not player.playing
    player.play()
    assert player.playing
    player.seek(timedelta(minutes=2))
    player.set_volume(80)
    player.set_muted(True)
    assert player.get_volume() == 80 and player.get_is_muted()
    assert [(s.rsplit(":", 2)[1], a) for s, a, _ in renderer.calls] == [
        ("AVTransport", "GetTransportInfo"),
        ("AVTransport", "Pause"),
        ("AVTransport", "Play"),
        ("AVTransport", "Seek"),
        ("RenderingControl", "SetVolume"),
        ("RenderingControl", "SetMute"),
    ]
    assert renderer.calls[3][2]["Target"] == "0:02:00"
    assert renderer.calls[4][2] == {"InstanceID": 0, "Channel": "Master", "DesiredVolume": 80}
    assert renderer.calls[5][2]["DesiredMute"] == 1


def test_pause_is_not_sent_to_a_stopped_renderer(player: UPnPPlayer, renderer: FakeRenderer):
    # Renderers answer 701 to Pause when stopped (e.g. right after the song ended).
    renderer.state = "STOPPED"
    renderer.calls.clear()
    player.pause()
    assert renderer.actions() == ["GetTransportInfo"]


# ######## Transcoding ######## #


def _flac_song() -> Any:
    return Song(id="s1", title="Flac", suffix="flac", duration=timedelta(seconds=200))


def _current_uri(renderer: FakeRenderer) -> str:
    return [args for _, action, args in renderer.calls if action == "SetAVTransportURI"][-1][
        "CurrentURI"
    ]


def test_a_format_the_renderer_does_not_list_is_transcoded(
    player: UPnPPlayer, renderer: FakeRenderer
):
    renderer.sink = "http-get:*:audio/mpeg:*,http-get:*:audio/wav:*"
    player._read_capabilities()
    assert player._sink_mimes == {"audio/mpeg", "audio/wav"}
    assert player._needs_transcoding("audio/flac")
    assert not player._needs_transcoding("audio/mpeg")
    assert not player._needs_transcoding(None)  # unknown format: try it as it is

    # Without the LAN server, the music server is asked to transcode.
    player.play_media("https://x/rest/stream.view?id=s1&u=me", timedelta(0), _flac_song())
    assert (
        _current_uri(renderer) == "https://x/rest/stream.view?id=s1&u=me&format=mp3&maxBitRate=320"
    )
    metadata = renderer.calls[-2][2]["CurrentURIMetaData"]
    assert "http-get:*:audio/mpeg:" in metadata

    # With it, it is asked too, and the LAN server transcodes if it didn't.
    player.server = mock.Mock(running=True, serve=mock.Mock(return_value="http://me/s/t"))
    player.play_media("https://x/rest/stream.view?id=s1", timedelta(0), _flac_song())
    player.server.serve.assert_called_once_with(
        "https://x/rest/stream.view?id=s1&format=mp3&maxBitRate=320",
        peer="tv",
        verify_cert=True,
        title="Flac",
        transcode="mp3",
        suffix="flac",
    )
    player.server.serve.reset_mock()
    player.play_media("file:///cache/s1.flac", timedelta(0), _flac_song())
    player.server.serve.assert_called_once_with(
        "file:///cache/s1.flac",
        peer="tv",
        verify_cert=True,
        title="Flac",
        transcode="mp3",
        suffix="flac",
    )


def test_the_target_format_is_one_the_renderer_lists(player: UPnPPlayer, renderer: FakeRenderer):
    assert player._transcode_target() == "mp3"  # nothing known: MP3 plays everywhere
    renderer.sink = "http-get:*:audio/L16:DLNA.ORG_PN=LPCM,http-get:*:audio/x-wav:*"
    player._read_capabilities()
    assert player._transcode_target() == "wav"
    player.play_media("https://x/rest/stream.view?id=s1", timedelta(0), _flac_song())
    assert _current_uri(renderer) == "https://x/rest/stream.view?id=s1&format=wav"
    assert "http-get:*:audio/wav:" in renderer.calls[-2][2]["CurrentURIMetaData"]


def test_a_refused_song_is_retried_transcoded(
    player: UPnPPlayer, renderer: FakeRenderer, events: Events
):
    renderer.sink = "http-get:*:audio/flac:*"  # claims to play it, but...
    player._read_capabilities()
    renderer.can_play = False
    original_play = renderer.soap_call

    def refuse_once(
        control_url: str, service_type: str, action: str, args: Any, **kw: Any
    ) -> Dict[str, str]:
        if action == "Play" and "format=mp3" in _current_uri(renderer):
            renderer.can_play = True  # the MP3 is fine
        return original_play(control_url, service_type, action, args, **kw)

    renderer.soap_call = refuse_once  # type: ignore[method-assign, assignment]
    upnp.soap_call = refuse_once  # type: ignore[assignment]

    player.play_media("https://x/rest/stream.view?id=s1", timedelta(seconds=0), _flac_song())
    assert renderer.actions().count("SetAVTransportURI") == 2
    assert _current_uri(renderer).endswith("format=mp3&maxBitRate=320")
    assert player.playing and player.song_loaded
    assert "audio/flac" in player._unplayable_mimes
    # From now on this format is transcoded straight away.
    assert player._needs_transcoding("audio/flac")


def test_a_song_that_stops_at_once_is_retried_transcoded(
    player: UPnPPlayer, renderer: FakeRenderer, events: Events
):
    player.play_media("https://x/rest/stream.view?id=s1", timedelta(0), _flac_song())
    player._ignore_stopped_until = 0
    _poll(player, "PLAYING", "0:00:00", "0:03:20")
    _poll(player, "STOPPED", "0:00:00", "0:03:20")
    assert events.track_ends == 0
    assert _current_uri(renderer).endswith("format=mp3&maxBitRate=320")
    assert player.playing and player.song_loaded

    # The transcoded one stopping at once is not retried again: it is reported.
    player._ignore_stopped_until = 0
    _poll(player, "PLAYING", "0:00:00", "0:03:20")
    _poll(player, "STOPPED", "0:00:00", "0:03:20")
    assert renderer.actions().count("SetAVTransportURI") == 2
    assert not player.playing


def _poll(player: UPnPPlayer, state: str, rel_time: str = "", duration: str = ""):
    assert player._current
    player._handle_poll(
        player._current,
        {"CurrentTransportState": state},
        {"RelTime": rel_time, "TrackDuration": duration},
    )


def test_polling_reports_progress_and_play_state(player: UPnPPlayer, events: Events):
    player.play_media("http://music/s1", timedelta(0), Song(id="s1"))
    events.player_events.clear()

    _poll(player, "PLAYING", "0:00:05", "0:03:20")
    assert events.timepos == [5.0]
    assert events.player_events == []  # already playing: nothing changed

    _poll(player, "PAUSED_PLAYBACK", "0:00:06", "0:03:20")
    assert not player.playing
    assert [e.playing for e in events.player_events] == [False]

    # Somebody pressed play on the speaker itself.
    _poll(player, "PLAYING", "0:00:07", "0:03:20")
    assert player.playing
    assert [e.playing for e in events.player_events] == [False, True]
    assert events.timepos == [5.0, 7.0]


def test_a_song_that_runs_out_ends_the_track(player: UPnPPlayer, events: Events):
    player.play_media("http://music/s1", timedelta(0), Song(id="s1"))
    player._ignore_stopped_until = 0  # the song has long been loaded
    _poll(player, "PLAYING", "0:03:18", "0:03:20")
    _poll(player, "STOPPED", "0:00:00", "0:03:20")  # renderers reset the position
    assert events.track_ends == 1
    assert not player.song_loaded
    assert not player.playing

    # Stopped again (nothing loaded now): not another track end.
    _poll(player, "STOPPED")
    assert events.track_ends == 1


def test_a_song_stopped_from_elsewhere_just_stops(player: UPnPPlayer, events: Events):
    player.play_media("http://music/s1", timedelta(0), Song(id="s1"))
    player._ignore_stopped_until = 0
    _poll(player, "PLAYING", "0:01:00", "0:03:20")
    events.player_events.clear()
    _poll(player, "STOPPED", "0:00:00", "0:03:20")
    assert events.track_ends == 0
    assert [e.playing for e in events.player_events] == [False]
    assert not player.song_loaded


def test_stopped_is_ignored_while_the_song_is_loading(player: UPnPPlayer, events: Events):
    player.play_media("http://music/s1", timedelta(0), Song(id="s1"))
    assert player._ignore_stopped_until > time.monotonic()
    _poll(player, "STOPPED", "0:00:00", "0:00:00")
    assert events.track_ends == 0
    assert player.playing and player.song_loaded


def test_reset_stops_the_renderer(player: UPnPPlayer, renderer: FakeRenderer):
    player.play_media("http://music/s1", timedelta(0), Song(id="s1"))
    renderer.calls.clear()
    player.reset()
    assert renderer.actions() == ["Stop"]
    assert not player.song_loaded and not player.playing
    assert player._expect_stop is False  # cleared once the Stop went through


def test_unknown_device_is_ignored(player: UPnPPlayer):
    before = player._current
    player.set_current_device_id("upnp:uuid:nope")
    assert player._current is before


def test_refresh_forgets_and_rediscovers(
    player: UPnPPlayer, events: Events, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        upnp, "discover_renderer_locations", lambda *a, **k: iter(["http://tv/desc.xml"])
    )
    monkeypatch.setattr(
        upnp.requests, "get", lambda *a, **k: mock.Mock(text=DESCRIPTION.replace("uuid:1234", "x"))
    )
    events.devices.clear()
    player.refresh_players()
    player._discover()  # the thread refresh_players started raced with this one; either wins
    assert [(d.delta.name, d.name) for d in events.devices][:2] == [
        ("REMOVE", "TV"),
        ("ADD", "Living Room"),
    ]
    assert "x-5678" in player._renderers
