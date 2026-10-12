"""
Play music on UPnP / DLNA media renderers (network speakers, AV receivers, smart TVs,
Kodi, gmediarender, ...) on the LAN.

Only the standard library and ``requests`` are needed:

* Renderers are found with an SSDP ``M-SEARCH`` (multicast UDP), and their description
  XML tells us where the AVTransport and RenderingControl services live.
* Playback is controlled with SOAP calls to those services (``SetAVTransportURI``,
  ``Play``, ``Pause``, ``Seek``, ``SetVolume``...).
* Renderers can be controlled from elsewhere (their own remote, another control point),
  so the transport state and position are polled once a second while a renderer is
  selected. That is also how the end of a song is noticed.

The renderer fetches the audio itself. By default it gets everything from the LAN file
server on this computer (cached songs directly, the rest relayed from the music server),
so that it never has to cope with HTTPS, VPNs or a picky music server; renderers are
unpredictable enough about what they will fetch. That can be switched off, in which
case the renderer streams from the music server directly.
"""

import logging
import re
import select
import socket
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
from typing import (
    Any,
    Callable,
    Dict,
    FrozenSet,
    Iterator,
    List,
    NamedTuple,
    Optional,
    Set,
    Tuple,
    Type,
    Union,
)
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from xml.sax.saxutils import escape

import requests
from gi.repository import GLib

from ..adapters import AdapterManager
from ..adapters.api_objects import Song
from .base import Player, PlayerDeviceEvent, PlayerEvent
from .lan_server import (
    TRANSCODE_BIT_RATE,
    TRANSCODE_FORMAT,
    TRANSCODE_MIME,
    TRANSCODE_TARGETS,
    LanFileServer,
    dlna_content_features,
    ffmpeg_available,
    get_lan_ips,
)

STREAM_THROUGH_KEY = "Stream Songs Through This Computer"

# Device IDs are "upnp:" + the renderer's UDN, so that the app can tell a saved UPnP
# device from a Chromecast without discovering it first.
DEVICE_ID_PREFIX = "upnp:"

SSDP_ADDRESS = ("239.255.255.250", 1900)
MEDIA_RENDERER = "urn:schemas-upnp-org:device:MediaRenderer"
# The versioned service types go into the SOAP calls; a renderer's own services are
# matched on the unversioned prefix (AVTransport:2 does everything :1 does).
AV_TRANSPORT = "urn:schemas-upnp-org:service:AVTransport:1"
RENDERING_CONTROL = "urn:schemas-upnp-org:service:RenderingControl:1"
CONNECTION_MANAGER = "urn:schemas-upnp-org:service:ConnectionManager:1"

HTTP_TIMEOUT = 5  # seconds, for every HTTP request to a renderer
POLL_INTERVAL = 1.0  # seconds, between polls of the renderer's state
VOLUME_POLL_EVERY = 5  # polls; the volume changes rarely and from elsewhere
POLL_FAILURES_BEFORE_DISCONNECT = 10
RESCAN_INTERVAL = 120  # seconds, between background searches for new renderers
# After telling the renderer to play, ignore a STOPPED state for this long: the renderer
# may still be fetching the song, and that must not count as "the song is over".
LOADING_GRACE_PERIOD = 8  # seconds
# A song that stops within this many seconds of its end has finished; one that stops
# earlier was stopped from elsewhere.
TRACK_END_SLACK = 3  # seconds
# A song that stops this soon after starting is one the renderer can't decode.
EARLY_STOP = 2  # seconds

_MIME_TYPES = {
    "mp3": "audio/mpeg",
    "flac": "audio/flac",
    "ogg": "audio/ogg",
    "oga": "audio/ogg",
    "opus": "audio/ogg",
    "m4a": "audio/mp4",
    "mp4": "audio/mp4",
    "aac": "audio/aac",
    "wav": "audio/wav",
    "wma": "audio/x-ms-wma",
    "aif": "audio/aiff",
    "aiff": "audio/aiff",
}


# MIME types that mean the same thing, so that a renderer's list can be compared with
# what a song is.
_MIME_ALIASES = {
    "audio/mp3": "audio/mpeg",
    "audio/mpeg3": "audio/mpeg",
    "audio/x-mpeg": "audio/mpeg",
    "audio/x-flac": "audio/flac",
    "application/ogg": "audio/ogg",
    "audio/x-ogg": "audio/ogg",
    "audio/x-m4a": "audio/mp4",
    "audio/m4a": "audio/mp4",
    "audio/x-wav": "audio/wav",
    "audio/vnd.wave": "audio/wav",
    "audio/x-aiff": "audio/aiff",
    "audio/wma": "audio/x-ms-wma",
}


class UPnPError(Exception):
    """A renderer answered a SOAP call with an error."""


@dataclass(frozen=True)
class UPnPRenderer:
    udn: str
    name: str
    av_transport_url: str
    rendering_control_url: str
    connection_manager_url: Optional[str] = None

    @property
    def id(self) -> str:
        return DEVICE_ID_PREFIX + self.udn


class PlayRequest(NamedTuple):
    """What the app asked to play, kept so that it can be retried transcoded."""

    uri: str
    progress: timedelta
    song: Song
    transcode: Optional[str]  # a TRANSCODE_TARGETS key, or None to play it as it is


# ######## SSDP, device descriptions and SOAP (pure functions, tested on their own) #### #


def parse_ssdp_response(data: bytes) -> Dict[str, str]:
    """
    :returns: the headers of an SSDP response (``HTTP/1.1 200 OK`` + headers), with the
        header names in upper case. An empty dict if it is not a response.

    >>> parse_ssdp_response(b"HTTP/1.1 200 OK\\r\\nLocation: http://x/d.xml\\r\\n\\r\\n")
    {'LOCATION': 'http://x/d.xml'}
    >>> parse_ssdp_response(b"NOTIFY * HTTP/1.1\\r\\nNT: a\\r\\n")
    {}
    """
    text = data.decode("utf-8", errors="replace")
    lines = text.split("\r\n") if "\r\n" in text else text.split("\n")
    if not lines or not lines[0].upper().startswith("HTTP/1.1 200"):
        return {}
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            name, value = line.split(":", 1)
            headers[name.strip().upper()] = value.strip()
    return headers


def discover_renderer_locations(timeout: float = 3.0, attempts: int = 2) -> Iterator[str]:
    """
    Search the LAN for media renderers with SSDP.

    :param timeout: how long to listen for answers in total, in seconds.
    :param attempts: how often to send the search (UDP is lossy).
    :returns: the (unique) URLs of the description documents of the renderers that
        answered.
    """
    message = "\r\n".join(
        [
            "M-SEARCH * HTTP/1.1",
            f"HOST: {SSDP_ADDRESS[0]}:{SSDP_ADDRESS[1]}",
            'MAN: "ssdp:discover"',
            "MX: 2",
            f"ST: {MEDIA_RENDERER}:1",
            "",
            "",
        ]
    ).encode()

    # One socket per interface: multicast only goes out on one interface per socket, and
    # the default one may well be a VPN rather than the LAN with the speakers on it.
    sockets: List[socket.socket] = []
    for ip in get_lan_ips() or [""]:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            if ip:
                sock.bind((ip, 0))
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
            sock.setblocking(False)
            sockets.append(sock)
        except OSError as e:
            logging.info(f"Can't search for UPnP devices via {ip or 'the default route'}: {e}")
            sock.close()

    seen: Set[str] = set()
    try:
        for _ in range(attempts):
            for sock in sockets:
                try:
                    sock.sendto(message, SSDP_ADDRESS)
                except OSError as e:
                    logging.info(f"Could not send the UPnP search: {e}")
            deadline = time.monotonic() + timeout / attempts
            while (remaining := deadline - time.monotonic()) > 0:
                readable, _, _ = select.select(sockets, [], [], remaining)
                for sock in readable:
                    try:
                        data, _addr = sock.recvfrom(65507)
                    except OSError:
                        continue
                    headers = parse_ssdp_response(data)
                    location = headers.get("LOCATION")
                    if not location or MEDIA_RENDERER not in headers.get("ST", ""):
                        continue
                    if location not in seen:
                        seen.add(location)
                        yield location
    finally:
        for sock in sockets:
            sock.close()


def _local_name(tag: str) -> str:
    """The tag without its ``{namespace}``."""
    return tag.rsplit("}", 1)[-1]


def _child_text(element: Optional[ET.Element], tag: str) -> str:
    if element is None:
        return ""
    for child in element:
        if _local_name(child.tag) == tag:
            return (child.text or "").strip()
    return ""


def _children(element: Optional[ET.Element], tag: str) -> List[ET.Element]:
    if element is None:
        return []
    return [child for child in element if _local_name(child.tag) == tag]


def _service_matches(service_type: str, wanted: str) -> bool:
    return service_type.startswith(wanted.rsplit(":", 1)[0] + ":")


def parse_device_description(xml_text: str, location: str) -> List[UPnPRenderer]:
    """
    :param xml_text: a UPnP device description document.
    :param location: the URL it was fetched from (relative control URLs are resolved
        against it, or against the document's ``URLBase`` if it has one).
    :returns: every media renderer in it (the root device and embedded devices) that
        has both an AVTransport and a RenderingControl service.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        logging.warning(f"Could not parse the UPnP device description at {location}: {e}")
        return []

    base_url = _child_text(root, "URLBase") or location
    renderers = []
    for device in root.iter():
        if _local_name(device.tag) != "device":
            continue
        if not _child_text(device, "deviceType").startswith(MEDIA_RENDERER):
            continue
        udn = _child_text(device, "UDN")
        if not udn:
            continue
        name = _child_text(device, "friendlyName")
        # An embedded renderer is often named after its device plus what it is (Sonos:
        # "Office - Sonos Play:3 Media Renderer"); the device list says what it is.
        name = re.sub(r"\s+media\s*renderer$", "", name, flags=re.IGNORECASE) or name
        name = name or urlparse(location).hostname or udn

        control_urls: Dict[str, str] = {}
        service_list = next(iter(_children(device, "serviceList")), None)
        for service in _children(service_list, "service"):
            service_type = _child_text(service, "serviceType")
            control_url = _child_text(service, "controlURL")
            if not control_url:
                continue
            for wanted in (AV_TRANSPORT, RENDERING_CONTROL, CONNECTION_MANAGER):
                if _service_matches(service_type, wanted):
                    control_urls.setdefault(wanted, urljoin(base_url, control_url))

        if AV_TRANSPORT not in control_urls or RENDERING_CONTROL not in control_urls:
            logging.info(f"Ignoring UPnP device {udn}: no AVTransport/RenderingControl service")
            continue

        renderers.append(
            UPnPRenderer(
                udn=udn,
                name=name,
                av_transport_url=control_urls[AV_TRANSPORT],
                rendering_control_url=control_urls[RENDERING_CONTROL],
                connection_manager_url=control_urls.get(CONNECTION_MANAGER),
            )
        )
    return renderers


def normalize_mime(mime: str) -> str:
    """
    >>> normalize_mime("Audio/MP3")
    'audio/mpeg'
    >>> normalize_mime("audio/flac; charset=binary")
    'audio/flac'
    """
    mime = mime.split(";")[0].strip().lower()
    return _MIME_ALIASES.get(mime, mime)


def mime_for_song(song: Song) -> Optional[str]:
    """
    :returns: the song's MIME type (from its file suffix), or ``None`` if unknown.
    """
    return _MIME_TYPES.get((getattr(song, "suffix", None) or "").lower())


def parse_protocol_info(sink: str) -> Optional[FrozenSet[str]]:
    """
    :param sink: a renderer's ``Sink`` from ``GetProtocolInfo``: comma separated
        ``protocol:network:mime:additional`` entries.
    :returns: the MIME types it says it plays over HTTP, or ``None`` if it doesn't say
        (no HTTP entries, or a wildcard: then anything may work).

    >>> sink = "http-get:*:audio/mp3:*,http-get:*:audio/flac:*,x-rincon:*:*:*"
    >>> sorted(parse_protocol_info(sink))
    ['audio/flac', 'audio/mpeg']
    >>> parse_protocol_info("http-get:*:*:*") is None
    True
    >>> parse_protocol_info("") is None
    True
    """
    mimes = set()
    for entry in sink.split(","):
        parts = entry.strip().split(":")
        if len(parts) < 3 or parts[0].lower() != "http-get":
            continue
        if parts[2] == "*":
            return None
        mimes.add(normalize_mime(parts[2]))
    return frozenset(mimes) or None


def with_transcoding_params(uri: str, target: str = TRANSCODE_FORMAT) -> str:
    """
    Ask the music server for the song in another format: the Subsonic ``stream``
    endpoint takes ``format`` and ``maxBitRate``. Other URLs are returned unchanged.

    >>> with_transcoding_params("https://x/rest/stream.view?id=1&u=me")
    'https://x/rest/stream.view?id=1&u=me&format=mp3&maxBitRate=320'
    >>> with_transcoding_params("https://x/rest/stream.view?id=1", "wav")
    'https://x/rest/stream.view?id=1&format=wav'
    >>> with_transcoding_params("file:///a.flac")
    'file:///a.flac'
    """
    parsed = urlparse(uri)
    if parsed.scheme not in ("http", "https") or "/rest/stream" not in parsed.path:
        return uri
    query = [(k, v) for k, v in parse_qsl(parsed.query) if k not in ("format", "maxBitRate")]
    query.append(("format", target))
    if target == "mp3":
        query.append(("maxBitRate", str(TRANSCODE_BIT_RATE)))
    return urlunparse(parsed._replace(query=urlencode(query)))


def build_soap_envelope(service_type: str, action: str, arguments: Dict[str, Any]) -> str:
    args_xml = "".join(f"<{k}>{escape(str(v))}</{k}>" for k, v in arguments.items())
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f'<s:Body><u:{action} xmlns:u="{service_type}">{args_xml}</u:{action}></s:Body>'
        "</s:Envelope>"
    )


def parse_soap_response(xml_text: str, action: str) -> Dict[str, str]:
    """
    :returns: the output arguments of a SOAP response as a dict.
    :raises UPnPError: if the response is a SOAP fault (or not a response at all).
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise UPnPError(f"Invalid SOAP response to {action}: {e}")

    for element in root.iter():
        if _local_name(element.tag) == "Fault":
            error = next((e for e in element.iter() if _local_name(e.tag) == "UPnPError"), None)
            code = _child_text(error, "errorCode")
            description = _child_text(error, "errorDescription")
            raise UPnPError(f"{action} failed: {description or 'error'} ({code or 'no code'})")

    for element in root.iter():
        if _local_name(element.tag) == f"{action}Response":
            return {_local_name(child.tag): (child.text or "").strip() for child in element}
    raise UPnPError(f"No {action}Response in the SOAP response")


def soap_call(
    control_url: str,
    service_type: str,
    action: str,
    arguments: Dict[str, Any],
    timeout: float = HTTP_TIMEOUT,
) -> Dict[str, str]:
    """
    Invoke a UPnP action.

    :raises UPnPError: if the renderer rejects the action.
    :raises requests.RequestException: if the renderer can't be reached.
    """
    response = requests.post(
        control_url,
        data=build_soap_envelope(service_type, action, arguments).encode("utf-8"),
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": f'"{service_type}#{action}"',
            "Connection": "close",
        },
        timeout=timeout,
    )
    # A SOAP fault comes as HTTP 500 and is turned into a UPnPError when parsed.
    if response.status_code not in (200, 500):
        raise UPnPError(f"{action} failed: HTTP {response.status_code}")
    return parse_soap_response(response.text, action)


def parse_upnp_time(value: Optional[str]) -> Optional[float]:
    """
    :returns: an UPnP ``H:MM:SS[.fff]`` time in seconds, or ``None`` if the renderer
        doesn't report it (``NOT_IMPLEMENTED``, empty...).

    >>> parse_upnp_time("0:03:25")
    205.0
    >>> parse_upnp_time("1:00:00.500")
    3600.5
    >>> parse_upnp_time("NOT_IMPLEMENTED") is None
    True
    """
    if not value:
        return None
    match = re.fullmatch(r"\s*(\d+):(\d{1,2}):(\d{1,2})(?:[.,](\d+))?\s*", value)
    if not match:
        return None
    hours, minutes, seconds, fraction = match.groups()
    total = float(int(hours) * 3600 + int(minutes) * 60 + int(seconds))
    if fraction:
        total += float(f"0.{fraction}")
    return total


def format_upnp_time(seconds: float) -> str:
    """
    >>> format_upnp_time(205.7)
    '0:03:25'
    >>> format_upnp_time(3661)
    '1:01:01'
    """
    seconds = max(0, int(seconds))
    return f"{seconds // 3600}:{seconds // 60 % 60:02}:{seconds % 60:02}"


def build_didl_metadata(
    song: Song,
    uri: str,
    cover_art_url: Optional[str],
    mime: Optional[str] = None,
    transcoded: bool = False,
) -> str:
    """
    :returns: the DIDL-Lite document describing the song. Renderers show the title,
        artist, album and cover art from it, and some won't play anything without one.
    """
    mime = mime or mime_for_song(song) or "audio/mpeg"
    features = dlna_content_features(mime, seekable=not transcoded, converted=transcoded)
    artist = song.artist.name if song.artist else ""
    parts = [
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"',
        ' xmlns:dc="http://purl.org/dc/elements/1.1/"',
        ' xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">',
        f'<item id="{escape(song.id)}" parentID="0" restricted="1">',
        f"<dc:title>{escape(song.title or '')}</dc:title>",
        f"<upnp:artist>{escape(artist)}</upnp:artist>",
        f"<dc:creator>{escape(artist)}</dc:creator>",
        f"<upnp:album>{escape(song.album.name if song.album else '')}</upnp:album>",
    ]
    if song.track:
        parts.append(f"<upnp:originalTrackNumber>{song.track}</upnp:originalTrackNumber>")
    if cover_art_url:
        parts.append(f"<upnp:albumArtURI>{escape(cover_art_url)}</upnp:albumArtURI>")
    parts.append("<upnp:class>object.item.audioItem.musicTrack</upnp:class>")
    duration = ""
    if song.duration:
        duration = f' duration="{format_upnp_time(song.duration.total_seconds())}"'
    parts.append(f'<res protocolInfo="http-get:*:{mime}:{features}"{duration}>{escape(uri)}</res>')
    parts.append("</item></DIDL-Lite>")
    return "".join(parts)


# ######## The player ######## #


class UPnPPlayer(Player):
    name = "UPnP / DLNA"
    can_start_playing_with_no_latency = False

    @property
    def enabled(self) -> bool:
        return True

    @staticmethod
    def get_configuration_options() -> Dict[str, Union[Type, Tuple[str, ...]]]:
        return {STREAM_THROUGH_KEY: bool}

    @property
    def supported_schemes(self) -> Set[str]:
        schemes = {"http", "https"}
        # Cached files can only be played through the LAN server; if it isn't up (for
        # whatever reason), stream from the music server instead.
        if self.server.running:
            schemes.add("file")
        return schemes

    def __init__(
        self,
        on_timepos_change: Callable[[Optional[float]], None],
        on_track_end: Callable[[], None],
        on_player_event: Callable[[PlayerEvent], None],
        player_device_change_callback: Callable[[PlayerDeviceEvent], None],
        config: Dict[str, Union[str, int, bool]],
    ):
        self.on_timepos_change = on_timepos_change
        self.on_track_end = on_track_end
        self.on_player_event = on_player_event
        self.player_device_change_callback = player_device_change_callback

        self.server = LanFileServer("UPnP / DLNA devices")
        self.config: Dict[str, Union[str, int, bool]] = {}
        self.change_settings(config or {})

        self._renderers: Dict[str, UPnPRenderer] = {}
        self._current: Optional[UPnPRenderer] = None
        # What the current renderer says it plays (None: it doesn't say), and formats
        # it turned out not to play after all.
        self._sink_mimes: Optional[FrozenSet[str]] = None
        self._unplayable_mimes: Set[str] = set()
        self._current_request: Optional[PlayRequest] = None

        # Every call to the renderer goes through this one worker, in order, so that the
        # UI never waits for the network and commands can't overtake each other.
        self._commands = ThreadPoolExecutor(max_workers=1, thread_name_prefix="upnp-cmd")
        self._state_lock = threading.Lock()
        self._playing = False
        self._timepos = 0.0
        self._duration: Optional[float] = None
        self._volume = 100.0
        self._muted = False
        self._volume_set_at = 0.0
        # Until when a STOPPED state is not the end of the song: while a Stop that we sent
        # is in flight (_expect_stop), and for a while after loading a song.
        self._expect_stop = False
        self._ignore_stopped_until = 0.0

        self._poll_generation = 0
        self._discovering = threading.Lock()
        self._shutting_down = threading.Event()

        self.refresh_players()
        threading.Thread(target=self._rescan_loop, name="upnp-rescan", daemon=True).start()

    # ---- helpers -------------------------------------------------------------------

    def _dispatch(self, fn: Callable, *args: Any):
        """Run a UI callback on the main thread (the player's threads never touch GTK)."""

        def run() -> bool:
            fn(*args)
            return False  # once

        GLib.idle_add(run)

    def _emit(self, event: PlayerEvent):
        self._dispatch(self.on_player_event, event)

    def _device_event(self, delta: PlayerDeviceEvent.Delta, renderer: UPnPRenderer):
        self._dispatch(
            self.player_device_change_callback,
            PlayerDeviceEvent(delta, type(self), renderer.id, renderer.name),
        )

    def _submit(self, fn: Callable, *args: Any, **kwargs: Any) -> Optional[Future]:
        """Queue a command for the renderer; errors are logged, never raised to the UI."""

        def run():
            try:
                fn(*args, **kwargs)
            except (UPnPError, requests.RequestException) as e:
                logging.warning(f"UPnP command failed: {e}")
            except Exception:
                logging.exception("UPnP command failed")

        if self._shutting_down.is_set():
            return None
        return self._commands.submit(run)

    def _av(self, action: str, **arguments: Any) -> Dict[str, str]:
        """Call an AVTransport action on the current renderer."""
        assert self._current
        return soap_call(
            self._current.av_transport_url, AV_TRANSPORT, action, {"InstanceID": 0, **arguments}
        )

    def _rc(self, action: str, **arguments: Any) -> Dict[str, str]:
        """Call a RenderingControl action on the current renderer."""
        assert self._current
        return soap_call(
            self._current.rendering_control_url,
            RENDERING_CONTROL,
            action,
            {"InstanceID": 0, "Channel": "Master", **arguments},
        )

    # ---- settings and discovery ----------------------------------------------------

    def change_settings(self, config: Dict[str, Union[str, int, bool]]):
        self.config = config or {}
        if self.config.get(STREAM_THROUGH_KEY, True):
            if not self.server.running:
                self.server.start()
        else:
            self.server.stop()

    def refresh_players(self):
        for renderer in self._renderers.values():
            self._device_event(PlayerDeviceEvent.Delta.REMOVE, renderer)
        self._renderers = {}
        threading.Thread(target=self._discover, name="upnp-discover", daemon=True).start()

    def _discover(self):
        """Search the LAN once and announce every renderer that isn't known yet."""
        with self._discovering:  # one search at a time
            try:
                for location in discover_renderer_locations():
                    if self._shutting_down.is_set():
                        return
                    self._add_renderers_at(location)
            except Exception:
                logging.exception("UPnP discovery failed")

    def _add_renderers_at(self, location: str):
        try:
            description = requests.get(location, timeout=HTTP_TIMEOUT).text
        except requests.RequestException as e:
            logging.info(f"Could not fetch the UPnP description at {location}: {e}")
            return
        for renderer in parse_device_description(description, location):
            if renderer.udn in self._renderers:
                continue
            logging.info(f"Found UPnP renderer '{renderer.name}' at {location}")
            self._renderers[renderer.udn] = renderer
            self._device_event(PlayerDeviceEvent.Delta.ADD, renderer)

    def _rescan_loop(self):
        """Keep looking for renderers that appear later (e.g. a speaker switched on)."""
        while not self._shutting_down.wait(RESCAN_INTERVAL):
            self._discover()

    def set_current_device_id(self, device_id: str):
        renderer = self._renderers.get(device_id.removeprefix(DEVICE_ID_PREFIX))
        if renderer is None:
            logging.warning(f"Unknown UPnP renderer {device_id}")
            return
        self._current = renderer
        self.song_loaded = False
        self._sink_mimes = None
        self._unplayable_mimes = set()
        self._current_request = None
        with self._state_lock:
            self._playing = False
            self._timepos = 0.0
            self._duration = None
        self._submit(self._read_volume)
        self._submit(self._read_capabilities)
        self._start_polling()

    def _read_capabilities(self):
        """Ask the renderer which formats it plays (if it has a ConnectionManager)."""
        renderer = self._current
        if not renderer or not renderer.connection_manager_url:
            return
        try:
            info = soap_call(
                renderer.connection_manager_url, CONNECTION_MANAGER, "GetProtocolInfo", {}
            )
        except (UPnPError, requests.RequestException) as e:
            logging.info(f"'{renderer.name}' won't say what it plays: {e}")
            return
        self._sink_mimes = parse_protocol_info(info.get("Sink", ""))
        if self._sink_mimes:
            logging.info(f"'{renderer.name}' plays: {', '.join(sorted(self._sink_mimes))}")

    def _needs_transcoding(self, mime: Optional[str]) -> bool:
        if mime is None or mime == TRANSCODE_MIME:
            return False
        if mime in self._unplayable_mimes:
            return True
        return self._sink_mimes is not None and mime not in self._sink_mimes

    def _transcode_target(self) -> str:
        """The format to transcode to: the first one the renderer says it plays."""
        if self._sink_mimes:
            for target, (mime, _) in TRANSCODE_TARGETS.items():
                if mime in self._sink_mimes and mime not in self._unplayable_mimes:
                    return target
        return TRANSCODE_FORMAT

    def _read_volume(self) -> bool:
        """
        Read the renderer's volume and mute state.

        :returns: whether they differ from what this player last knew.
        """
        volume = self._rc("GetVolume").get("CurrentVolume", "")
        muted = self._rc("GetMute").get("CurrentMute", "")
        changed = False
        if volume.isdigit() and float(volume) != self._volume:
            self._volume = float(volume)
            changed = True
        if muted and (muted in ("1", "true", "True")) != self._muted:
            self._muted = muted in ("1", "true", "True")
            changed = True
        return changed

    # ---- polling the renderer's state ---------------------------------------------

    def _start_polling(self):
        self._poll_generation += 1
        threading.Thread(
            target=self._poll_loop, args=(self._poll_generation,), name="upnp-poll", daemon=True
        ).start()

    def _stop_polling(self):
        self._poll_generation += 1

    def _poll_loop(self, generation: int):
        failures = 0
        polls_until_volume_check = 0
        while (
            generation == self._poll_generation
            and (renderer := self._current) is not None
            and not self._shutting_down.is_set()
        ):
            try:
                transport = self._av("GetTransportInfo")
                position = self._av("GetPositionInfo")
                volume_changed = False
                if polls_until_volume_check <= 0 and time.monotonic() - self._volume_set_at > 3:
                    volume_changed = self._read_volume()
                    polls_until_volume_check = VOLUME_POLL_EVERY
                failures = 0
            except (UPnPError, requests.RequestException) as e:
                failures += 1
                logging.debug(f"Polling {renderer.name} failed ({failures}): {e}")
                if failures >= POLL_FAILURES_BEFORE_DISCONNECT:
                    logging.warning(f"Lost the connection to UPnP renderer '{renderer.name}'")
                    self._current = None
                    self.song_loaded = False
                    self._emit(PlayerEvent(PlayerEvent.EventType.DISCONNECT, renderer.id))
                    return
            else:
                if generation == self._poll_generation:
                    self._handle_poll(renderer, transport, position)
                    if volume_changed:
                        self._emit(
                            PlayerEvent(
                                PlayerEvent.EventType.VOLUME_CHANGE,
                                renderer.id,
                                volume=0 if self._muted else self._volume,
                            )
                        )
            polls_until_volume_check -= 1
            self._shutting_down.wait(POLL_INTERVAL)

    def _handle_poll(
        self, renderer: UPnPRenderer, transport: Dict[str, str], position: Dict[str, str]
    ):
        state = transport.get("CurrentTransportState", "")
        rel_time = parse_upnp_time(position.get("RelTime"))
        duration = parse_upnp_time(position.get("TrackDuration"))
        stopped = state in ("STOPPED", "NO_MEDIA_PRESENT")

        if stopped and (self._expect_stop or time.monotonic() < self._ignore_stopped_until):
            return  # we stopped it ourselves, or it is still loading the song

        with self._state_lock:
            was_playing = self._playing
            last_timepos = self._timepos
            if duration:
                self._duration = duration
            known_duration = self._duration

            if state in ("PLAYING", "TRANSITIONING"):
                self._playing = True
                if rel_time is not None:
                    self._timepos = rel_time
            elif state == "PAUSED_PLAYBACK":
                self._playing = False
                if rel_time is not None:
                    self._timepos = rel_time
            elif stopped:
                self._playing = False
            playing = self._playing

        if stopped and was_playing and self.song_loaded:
            # A song that was playing has stopped: either it ran out, or somebody stopped
            # the renderer from elsewhere. Only the former means "next song".
            self.song_loaded = False
            request = self._current_request
            if last_timepos < EARLY_STOP and request and not request.transcode:
                # Gave up right away: it took the file but can't decode it.
                self._retry_transcoded(request, "stopped right after starting")
                return
            if known_duration is None or last_timepos >= known_duration - TRACK_END_SLACK:
                logging.debug("UPnP track ended")
                self._dispatch(self.on_track_end)
            else:
                logging.info(f"'{renderer.name}' was stopped from elsewhere")
                self._emit(
                    PlayerEvent(
                        PlayerEvent.EventType.PLAY_STATE_CHANGE, renderer.id, playing=False
                    )
                )
            return

        if playing != was_playing:
            self._emit(
                PlayerEvent(PlayerEvent.EventType.PLAY_STATE_CHANGE, renderer.id, playing=playing)
            )
        if playing and rel_time is not None and self.song_loaded:
            self._dispatch(self.on_timepos_change, rel_time)

    # ---- the Player API ------------------------------------------------------------

    def reset(self):
        if self._current:
            self._expect_stop = True
            self._submit(self._stop_renderer)
        self.song_loaded = False
        with self._state_lock:
            self._playing = False
            self._timepos = 0.0

    def _stop_renderer(self, timeout: float = HTTP_TIMEOUT):
        assert self._current
        try:
            soap_call(
                self._current.av_transport_url,
                AV_TRANSPORT,
                "Stop",
                {"InstanceID": 0},
                timeout=timeout,
            )
        finally:
            self._expect_stop = False

    def shutdown(self):
        self._shutting_down.set()
        self._stop_polling()
        self.server.stop()
        self._commands.shutdown(wait=False)
        if self._current:
            try:
                self._expect_stop = True
                self._stop_renderer(timeout=1.5)  # don't hold up quitting for a dead speaker
            except Exception:
                pass

    @property
    def playing(self) -> bool:
        return self._current is not None and self._playing

    def get_volume(self) -> float:
        return self._volume

    def set_volume(self, volume: float):
        self._volume = volume
        self._volume_set_at = time.monotonic()
        if self._current:
            self._submit(self._rc, "SetVolume", DesiredVolume=int(max(0, min(100, volume))))

    def get_is_muted(self) -> bool:
        return self._muted

    def set_muted(self, muted: bool):
        self._muted = muted
        self._volume_set_at = time.monotonic()
        if self._current:
            self._submit(self._rc, "SetMute", DesiredMute=1 if muted else 0)

    def play_media(self, uri: str, progress: timedelta, song: Song):
        transcode = None
        if self._needs_transcoding(mime := mime_for_song(song)):
            transcode = self._transcode_target()
            logging.info(f"'{song.title}' ({mime}) will be transcoded to {transcode.upper()}")
        self._start(PlayRequest(uri, progress, song, transcode))

    def _retry_transcoded(self, request: PlayRequest, why: str):
        """The renderer wouldn't play the song as it is: try it in a format it plays."""
        mime = mime_for_song(request.song)
        if mime:
            self._unplayable_mimes.add(mime)
        target = self._transcode_target()
        logging.warning(
            f"The renderer {why} on '{request.song.title}' ({mime or 'unknown format'}); "
            f"retrying it transcoded to {target.upper()}"
        )
        self._start(request._replace(transcode=target))

    def _start(self, request: PlayRequest):
        if not (renderer := self._current):
            return
        self._current_request = request
        uri, song = request.uri, request.song
        mime = None
        if request.transcode:
            mime = TRANSCODE_TARGETS[request.transcode].mime
            # Ask the music server to transcode; if it can't, the LAN server does it.
            uri = with_transcoding_params(uri, request.transcode)
            if not self.server.running and not uri.startswith(("http://", "https://")):
                logging.warning("Can't transcode a cached song without the LAN server")
            elif not ffmpeg_available():
                logging.info("ffmpeg isn't installed: only the music server can transcode")
        if self.server.running:
            uri = self.server.serve(
                uri,
                peer=urlparse(renderer.av_transport_url).hostname,
                verify_cert=self._verify_cert(),
                title=song.title,
                transcode=request.transcode,
                suffix=getattr(song, "suffix", None),
            )

        progress = request.progress
        metadata = build_didl_metadata(
            song, uri, self._cover_art_url(song), mime, transcoded=bool(request.transcode)
        )
        self.song_loaded = True
        self._ignore_stopped_until = time.monotonic() + LOADING_GRACE_PERIOD
        with self._state_lock:
            self._playing = True
            self._timepos = progress.total_seconds()
            self._duration = song.duration.total_seconds() if song.duration else None

        # Make sure to clear out the cache duration state.
        self._emit(
            PlayerEvent(
                PlayerEvent.EventType.STREAM_CACHE_PROGRESS_CHANGE,
                renderer.id,
                stream_cache_duration=0,
            )
        )
        self._submit(self._load_and_play, uri, metadata, progress.total_seconds(), request)

    def _verify_cert(self) -> bool:
        """Whether the music server's certificate is to be verified (its adapter's setting)."""
        if not AdapterManager._instance:
            return True
        return bool(getattr(AdapterManager._instance.ground_truth_adapter, "verify_cert", True))

    def _cover_art_url(self, song: Song) -> Optional[str]:
        """The song's cover art on the music server, for the renderer to show."""
        if not song.cover_art or not AdapterManager._instance:
            return None
        try:
            schemes = AdapterManager._instance.ground_truth_adapter.supported_schemes
            scheme = next(s for s in ("https", "http") if s in schemes)
            return AdapterManager.get_cover_art_uri(song.cover_art, scheme, size=500).result()
        except Exception:
            return None

    def _load_and_play(self, uri: str, metadata: str, start_at: float, request: PlayRequest):
        self._expect_stop = True
        try:
            self._av("Stop")
        except (UPnPError, requests.RequestException):
            pass  # nothing was playing, or the renderer doesn't like Stop here
        finally:
            self._expect_stop = False

        try:
            self._av("SetAVTransportURI", CurrentURI=uri, CurrentURIMetaData=metadata)
            self._ignore_stopped_until = time.monotonic() + LOADING_GRACE_PERIOD
            self._av("Play", Speed="1")
        except UPnPError as e:
            if not request.transcode and self._current_request is request:
                # 701 "Transition not available", 714 "Illegal MIME-type", ...: it took
                # the URL but won't play it. Maybe it can't decode it: try it as MP3.
                self._retry_transcoded(request, f"refused ({e})")
                return
            logging.warning(
                f"The renderer could not start {uri}: {e}. The renderer fetches the song "
                "itself, so it must be able to reach that address (a music server only "
                "reachable through a VPN, a certificate it doesn't trust, or a format it "
                "can't play won't work)."
            )
            self._playback_failed()
            return
        except requests.RequestException as e:
            logging.warning(f"The renderer could not be reached: {e}")
            self._playback_failed()
            return

        if start_at > 0:
            # Seeking only works once the renderer has started playing the new URI.
            for _ in range(10):
                if self._shutting_down.wait(0.5):
                    return
                if self._av("GetTransportInfo").get("CurrentTransportState") == "PLAYING":
                    break
            self._av("Seek", Unit="REL_TIME", Target=format_upnp_time(start_at))

    def _playback_failed(self):
        """Tell the app that nothing is playing after all."""
        self.song_loaded = False
        self._ignore_stopped_until = 0
        with self._state_lock:
            self._playing = False
        if self._current:
            self._emit(
                PlayerEvent(
                    PlayerEvent.EventType.PLAY_STATE_CHANGE, self._current.id, playing=False
                )
            )

    def pause(self):
        if self._current:
            with self._state_lock:
                self._playing = False
            self._submit(self._pause_if_playing)

    def _pause_if_playing(self):
        # Renderers answer Pause with 701 "Transition not available" when they are
        # already stopped (e.g. the song just ended), so only pause what is playing.
        if self._av("GetTransportInfo").get("CurrentTransportState") in (
            "PLAYING",
            "TRANSITIONING",
        ):
            try:
                self._av("Pause")
            except UPnPError as e:
                logging.debug(f"Pause not accepted: {e}")

    def play(self):
        if self._current:
            with self._state_lock:
                self._playing = True
            self._submit(self._av, "Play", Speed="1")

    def seek(self, position: timedelta):
        if not self._current:
            return
        with self._state_lock:
            self._timepos = position.total_seconds()
        self._submit(
            self._av, "Seek", Unit="REL_TIME", Target=format_upnp_time(position.total_seconds())
        )

    def next_media_cached(self, *_):
        pass
