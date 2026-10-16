"""
A small HTTP server that serves songs to playback devices on the LAN.

Chromecasts and UPnP renderers fetch their audio over HTTP themselves. A song that is
only cached on this computer has to be served to them from here, and a song on the music
server can be relayed through here, so that the device only ever talks plain HTTP to a
machine on its own network (no certificates, VPNs or picky servers to worry about). The
server runs in a child process and hands out exactly one song at a time, behind a random
token that changes with every song.

Songs can also be transcoded (with ``ffmpeg``) for devices that can't play their format.
That is done in full before the song is served, into a temporary file, because devices
(Samsung TVs, for one) refuse streams without a known length; it takes a few seconds, on
the device's first request. Every response carries what DLNA devices expect: HTTP/1.1,
the DLNA profile name of the format, and the transfer flags.
"""

import logging
import mimetypes
import multiprocessing
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
from multiprocessing.process import BaseProcess
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Dict, Iterator, List, NamedTuple, Optional, Protocol, Type
from wsgiref.simple_server import ServerHandler, WSGIRequestHandler, WSGIServer

import bottle
import requests


class TranscodeTarget(NamedTuple):
    mime: str
    ffmpeg_args: List[str]


# What songs can be transcoded to, in order of preference: MP3 plays everywhere; WAV is
# for the odd device that lists only PCM.
TRANSCODE_BIT_RATE = 320
TRANSCODE_TARGETS: Dict[str, TranscodeTarget] = {
    "mp3": TranscodeTarget(
        "audio/mpeg", ["-codec:a", "libmp3lame", "-b:a", f"{TRANSCODE_BIT_RATE}k", "-f", "mp3"]
    ),
    "wav": TranscodeTarget("audio/wav", ["-codec:a", "pcm_s16le", "-f", "wav"]),
}
TRANSCODE_FORMAT = "mp3"  # the default target
TRANSCODE_MIME = TRANSCODE_TARGETS[TRANSCODE_FORMAT].mime

# DLNA profile names for the formats that have one. Strict renderers (Samsung TVs) only
# play what comes with the profile they advertise, so these go into the DLNA headers and
# the protocolInfo of the metadata.
_DLNA_PROFILES = {
    "audio/mpeg": "MP3",
    "audio/mp4": "AAC_ISO_320",
    "audio/x-ms-wma": "WMABASE",
    "audio/l16": "LPCM",
}
# Streaming and background transfer, HTTP stalling, DLNA 1.5: the usual flags for audio.
_DLNA_FLAGS = "DLNA.ORG_FLAGS=01700000000000000000000000000000"


def ffmpeg_available() -> bool:
    """Whether songs can be transcoded on this computer (the ``ffmpeg`` program)."""
    return shutil.which("ffmpeg") is not None


def dlna_content_features(mime: str, seekable: bool = True, converted: bool = False) -> str:
    """
    The 4th field of a DLNA protocolInfo (and the ``contentFeatures.dlna.org`` header):
    the profile name if the format has one, whether byte seeking works (``OP``), and
    whether the content is converted on the fly (``CI``).

    >>> dlna_content_features("audio/mpeg")
    'DLNA.ORG_PN=MP3;DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000'
    >>> dlna_content_features("audio/flac", seekable=False, converted=True)
    'DLNA.ORG_OP=00;DLNA.ORG_CI=1;DLNA.ORG_FLAGS=01700000000000000000000000000000'
    """
    parts = []
    if profile := _DLNA_PROFILES.get(mime.split(";")[0].strip().lower()):
        parts.append(f"DLNA.ORG_PN={profile}")
    parts.append(f"DLNA.ORG_OP={'01' if seekable else '00'}")
    parts.append(f"DLNA.ORG_CI={'1' if converted else '0'}")
    parts.append(_DLNA_FLAGS)
    return ";".join(parts)


class _ProcessContext(Protocol):
    """The part of a multiprocessing context that is used here.

    typeshed only declares ``Process`` on the concrete contexts (fork, spawn, default),
    not on ``BaseContext``, so the helper below is typed structurally instead. A
    read-only property (rather than an attribute) lets each context's own Process
    subclass satisfy it.
    """

    @property
    def Process(self) -> Type[BaseProcess]:  # noqa: N802 (matches multiprocessing)
        ...


def _server_process_context() -> _ProcessContext:
    """
    Return the multiprocessing context for the LAN file server.

    The server process gets its socket and shared state from the parent, so it relies on
    fork-style inheritance. macOS defaults to spawn, and frozen spawn would also need to
    pickle the whole player, including GTK callbacks from the app. Prefer fork when the
    platform offers it and fall back to the default context elsewhere.
    """
    try:
        return multiprocessing.get_context("fork")
    except ValueError:
        return multiprocessing.get_context()


def get_lan_ip(peer: Optional[str] = None) -> str:
    """
    :param peer: the IP address or host name of the device that will connect to us.
    :returns: the IP address of this computer that the device can reach: the one of the
        interface that routes to it (so a VPN that is the default route doesn't get in
        the way), or of the default route if no device is given.

    If this fails, then we are basically screwed, so don't care if it blows up.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # No packet is sent: connecting a UDP socket only picks the route.
        s.connect((peer or "8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


def get_lan_ips() -> List[str]:
    """
    :returns: this computer's IPv4 addresses on every interface but the loopback one,
        with the one of the default route first. Falls back to the default route's
        address alone when the interfaces can't be listed.
    """
    ips = []
    try:
        ips.append(get_lan_ip())
    except OSError:
        pass
    try:
        import ifaddr  # a zeroconf (so pychromecast) dependency, but not always there

        for adapter in ifaddr.get_adapters():
            for ip in adapter.ips:
                if isinstance(ip.ip, str) and not ip.ip.startswith("127.") and ip.ip not in ips:
                    ips.append(ip.ip)
    except Exception:
        pass
    return ips


class LanFileServer:
    """
    Serves one song at a time to devices on the LAN.

    :param purpose: who the server is for; shown on the server's index page.
    """

    def __init__(self, purpose: str):
        self.purpose = purpose
        self.port: Optional[int] = None
        self.process: Optional[BaseProcess] = None
        # Shared with the server process: it reads these, the player writes them. The
        # target is a file:// URI of a cached song, or the http(s):// URL to relay.
        self._serving_target = multiprocessing.Array("c", 8192)  # huge buffer, just in case
        self._serving_token = multiprocessing.Array("c", 16)
        self._verify_cert = multiprocessing.Value("b", 1)
        self._transcode = multiprocessing.Array("c", 8)  # a TRANSCODE_TARGETS key, or ""
        # Where transcoded songs go; made here so that stop() can remove it.
        self._work_dir: Optional[Path] = None

    @property
    def running(self) -> bool:
        return self.process is not None

    def start(self):
        """
        Start the server, replacing any running server.

        It listens on a free port picked by the OS (the devices get the full URL, so a
        fixed port would only ever be in the way). The socket is bound here, so that the
        port is known right away, and handed to the server process.
        """
        self.stop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("0.0.0.0", 0))
            sock.listen(16)
            self.port = sock.getsockname()[1]
            self._work_dir = Path(tempfile.mkdtemp(prefix="sublime-music-lan-"))
            self.process = _server_process_context().Process(
                target=self._run_server_process, args=(sock,), daemon=True
            )
            self.process.start()
            logging.info(f"LAN file server for {self.purpose} listening on port {self.port}")
        except OSError:
            logging.exception(f"Could not start the LAN file server for {self.purpose}")
            self.process = None
        finally:
            sock.close()  # the server process has its own copy

    def stop(self):
        if self.process is None:
            return
        try:
            self.process.terminate()
        except Exception:
            pass
        self.process = None
        self.port = None
        if self._work_dir:
            shutil.rmtree(self._work_dir, ignore_errors=True)
            self._work_dir = None

    def serve(
        self,
        target: str,
        peer: Optional[str] = None,
        verify_cert: bool = True,
        title: str = "",
        transcode: Optional[str] = None,
        suffix: Optional[str] = None,
    ) -> str:
        """
        Make the given song the one that is being served.

        :param target: a ``file://`` URI of a cached song, which is served as is, or an
            ``http(s)://`` URL on the music server, which is relayed (range requests and
            all).
        :param peer: the address of the device that will fetch it, if known, so that the
            URL uses this computer's address on the network that device is on.
        :param verify_cert: whether to verify the music server's certificate when
            relaying.
        :param title: the song's title, for the log.
        :param transcode: deliver the song in this format (a :data:`TRANSCODE_TARGETS`
            key) whatever its own: a relayed song that the music server didn't already
            transcode (or did, but without saying how long it is), and a cached file, are
            transcoded here with ``ffmpeg`` (if it is installed; otherwise the song goes
            out as it is).
        :param suffix: the song's file suffix, for the URL: some devices (Samsung TVs)
            judge a stream by its URL's extension.
        :returns: the URL at which the device can fetch the song.
        """
        if transcode and transcode not in TRANSCODE_TARGETS:
            raise ValueError(f"Can't transcode to {transcode}")
        token = os.urandom(8).hex()
        # typing doesn't support multiprocessing.Value very well
        self._serving_token.value = token.encode()  # type: ignore
        self._serving_target.value = target.encode()  # type: ignore
        self._verify_cert.value = 1 if verify_cert else 0  # type: ignore
        self._transcode.value = (transcode or "").encode()  # type: ignore
        if transcode:
            suffix = transcode
        elif not suffix:
            suffix = Path(target.split("?")[0]).suffix.lstrip(".") or "bin"
        url = f"http://{get_lan_ip(peer)}:{self.port}/s/{token}/song.{suffix.lower()}"
        what = "Serving" if target.startswith("file://") else "Relaying"
        as_ = f" as {transcode.upper()}" if transcode else ""
        logging.info(f"{what} {title or target}{as_} at {url}")
        return url

    def _run_server_process(self, sock: socket.socket):
        # See the ping worker: the inherited GLib SIGTERM handler would make terminate()
        # a no-op in this forked child.
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        app = bottle.Bottle()
        assert self._work_dir
        transcoder = _Transcoder(self._work_dir)

        @app.route("/")
        def index() -> str:
            return f"""
            <h1>Sublime Music Local Music Server</h1>
            <p>
                Sublime Music uses this port as a server for serving music to
                {self.purpose} on the same LAN.
            </p>
            """

        @app.route("/s/<token>", method=["GET", "HEAD"])
        @app.route("/s/<token>/<_name>", method=["GET", "HEAD"])
        def stream_song(token: str, _name: str = "") -> bottle.BaseResponse:
            # typing doesn't support multiprocessing.Value very well
            if token != self._serving_token.value.decode():  # type: ignore
                raise bottle.HTTPError(status=401, body="Invalid token.")
            target = self._serving_target.value.decode()  # type: ignore
            verify_cert = bool(self._verify_cert.value)  # type: ignore
            transcode = self._transcode.value.decode() or None  # type: ignore
            if transcode and not ffmpeg_available():
                logging.warning("ffmpeg isn't installed, so the song goes out untranscoded")
                transcode = None
            if target.startswith("file://"):
                filename = Path(target[7:])
                if transcode and _mime_type(filename) != TRANSCODE_TARGETS[transcode].mime:
                    filename = transcoder.transcode(token, str(filename), verify_cert, transcode)
                response = _serve_file(filename)
                _set_dlna_headers(response, _mime_type(filename))
                return response
            return _relay(token, target, verify_cert, transcode, transcoder)

        _serve_forever(app, sock)


def _mime_type(filename: Path) -> str:
    return mimetypes.guess_type(str(filename))[0] or "application/octet-stream"


def _set_dlna_headers(
    response: bottle.BaseResponse, mime: str, seekable: bool = True, converted: bool = False
):
    response.set_header("transferMode.dlna.org", "Streaming")
    response.set_header(
        "contentFeatures.dlna.org", dlna_content_features(mime, seekable, converted)
    )
    response.set_header("realTimeInfo.dlna.org", "DLNA.ORG_TLAG=*")


def _serve_file(filename: Path) -> bottle.BaseResponse:
    # static_file does Content-Length, Range requests and HEAD, which renderers rely on
    # for seeking and for probing the file before playing it.
    return bottle.static_file(filename.name, root=filename.parent, mimetype=_mime_type(filename))


_RELAYED_HEADERS = ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges")


def _relay(
    token: str,
    url: str,
    verify_cert: bool,
    transcode: Optional[str],
    transcoder: "_Transcoder",
) -> bottle.BaseResponse:
    """
    Fetch the song from the music server and pass it through, Range requests included,
    so that the device can seek in it just as it would on the music server.

    :param transcode: the format the song must go out in. The music server was asked to
        transcode it; if it didn't (its response is something else), or did without
        saying how long the result is (devices want to know), that is done here.
    """
    headers = {}
    if range_header := bottle.request.get_header("Range"):
        headers["Range"] = range_header
    try:
        upstream = requests.get(
            url, headers=headers, stream=True, timeout=(10, 60), verify=verify_cert
        )
    except requests.RequestException as e:
        logging.warning(f"Could not fetch {url} to relay it: {e}")
        raise bottle.HTTPError(status=502, body=f"Could not fetch the song: {e}")
    if upstream.status_code not in (200, 206):
        upstream.close()
        logging.warning(f"The music server answered {upstream.status_code} for {url}")
        raise bottle.HTTPError(status=502, body=f"The music server said {upstream.status_code}")

    content_type = upstream.headers.get("Content-Type", "")
    if transcode and (
        not content_type.startswith(TRANSCODE_TARGETS[transcode].mime)
        or not upstream.headers.get("Content-Length")
    ):
        upstream.close()
        logging.info(
            f"The music server sent {content_type or 'nothing'} for {url}; transcoding here"
        )
        filename = transcoder.transcode(token, url, verify_cert, transcode)
        response = _serve_file(filename)
        _set_dlna_headers(response, _mime_type(filename))
        return response

    response = bottle.HTTPResponse(status=upstream.status_code)
    for header in _RELAYED_HEADERS:
        if value := upstream.headers.get(header):
            response.set_header(header, value)
    seekable = upstream.headers.get("Accept-Ranges", "").lower() == "bytes"
    _set_dlna_headers(response, content_type, seekable=seekable)

    if bottle.request.method == "HEAD":
        upstream.close()
        return response

    def body() -> Iterator[bytes]:
        try:
            yield from upstream.iter_content(chunk_size=64 * 1024)
        finally:
            upstream.close()

    response.body = body()
    return response


class _Transcoder:
    """
    Transcodes songs into files in the work directory (one at a time; the previous one
    is removed), so that they can be served with a length and byte ranges.
    """

    def __init__(self, work_dir: Path):
        self.work_dir = work_dir
        self._lock = threading.Lock()  # the device's requests come in parallel
        self._done: Dict[str, Path] = {}

    def transcode(self, token: str, source: str, verify_cert: bool, target: str) -> Path:
        """
        :param source: a local path, or a URL that ffmpeg fetches itself.
        :returns: the transcoded file (the same one for the same token).
        :raises bottle.HTTPError: if ffmpeg can't do it.
        """
        with self._lock:
            if filename := self._done.get(token):
                return filename
            for old in self._done.values():
                old.unlink(missing_ok=True)
            self._done.clear()

            mime, ffmpeg_args = TRANSCODE_TARGETS[target]
            filename = self.work_dir / f"{token}.{target}"
            command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
            if source.startswith("https://") and verify_cert:
                command += ["-tls_verify", "1"]
            command += ["-i", source, "-vn", "-map_metadata", "-1", *ffmpeg_args, str(filename)]
            logging.info(f"Transcoding {source} to {target.upper()}")
            started = time.monotonic()
            try:
                result = subprocess.run(command, capture_output=True, timeout=600)
            except (OSError, subprocess.TimeoutExpired) as e:
                logging.warning(f"Could not transcode {source}: {e}")
                raise bottle.HTTPError(status=502, body=f"Could not transcode the song: {e}")
            if result.returncode != 0:
                error = result.stderr.decode(errors="replace").strip()
                logging.warning(f"ffmpeg failed on {source}: {error}")
                filename.unlink(missing_ok=True)
                raise bottle.HTTPError(status=502, body=f"Could not transcode the song: {error}")
            logging.info(f"Transcoded in {time.monotonic() - started:.1f} s")
            self._done[token] = filename
            return filename


class _HTTP11Handler(ServerHandler):
    """wsgiref answers with HTTP/1.0 by default; DLNA devices expect HTTP/1.1."""

    http_version = "1.1"

    def cleanup_headers(self):
        super().cleanup_headers()
        # One request per connection (that is what wsgiref does); say so, as HTTP/1.1
        # clients would otherwise assume keep-alive. (A hop-by-hop header, so the app
        # itself isn't allowed to set it.)
        self.headers["Connection"] = "close"  # type: ignore[attr-defined]


class _Server(ThreadingMixIn, WSGIServer):
    """A WSGI server on an already listening socket; a thread per request."""

    daemon_threads = True

    def __init__(self, sock: socket.socket):
        super().__init__(sock.getsockname(), _QuietHandler, bind_and_activate=False)
        self.socket.close()  # the one TCPServer made; we have our own
        self.socket = sock
        self.server_address = sock.getsockname()
        host, port = sock.getsockname()[:2]
        self.server_name = str(host)
        self.server_port = int(port)
        self.setup_environ()


class _QuietHandler(WSGIRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def handle(self):
        # WSGIRequestHandler.handle, with the HTTP/1.1 handler. One request per
        # connection, as in wsgiref (the responses say "Connection: close").
        self.raw_requestline = self.rfile.readline(65537)
        if len(self.raw_requestline) > 65536:
            self.requestline, self.request_version, self.command = "", "", ""
            self.send_error(414)
            return
        if not self.parse_request():
            return
        handler = _HTTP11Handler(
            self.rfile, self.wfile, self.get_stderr(), self.get_environ(), multithread=False
        )
        handler.request_handler = self  # type: ignore[attr-defined]
        handler.run(self.server.get_app())  # type: ignore[attr-defined]


def _serve_forever(app: bottle.Bottle, sock: socket.socket):
    server = _Server(sock)
    server.set_app(app)
    server.serve_forever()
