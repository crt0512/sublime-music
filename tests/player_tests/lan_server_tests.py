"""The LAN file server, as a Chromecast or UPnP renderer sees it."""

import struct
import wave
from pathlib import Path
from typing import Iterator

import pytest
import requests

from sublime_music.players import lan_server

SONG_FILE = Path(__file__).parent.joinpath("mock_data/test-song.mp3")


@pytest.fixture
def server() -> Iterator[lan_server.LanFileServer]:
    server = lan_server.LanFileServer("tests")
    server.start()
    yield server
    server.stop()
    assert not server.running and server.port is None


def test_serves_the_song_with_ranges_and_head(server: lan_server.LanFileServer):
    assert server.running and server.port
    url = server.serve(f"file://{SONG_FILE}", peer="127.0.0.1", title="Song")
    assert url.startswith(f"http://127.0.0.1:{server.port}/s/")
    assert url.endswith("/song.mp3")  # Samsung TVs judge a stream by its URL's extension
    size = SONG_FILE.stat().st_size

    head = requests.head(url, timeout=10)
    assert head.status_code == 200
    assert head.raw.version == 11  # DLNA devices expect HTTP/1.1
    assert head.headers["Content-Type"] == "audio/mpeg"
    assert int(head.headers["Content-Length"]) == size
    assert head.headers["transferMode.dlna.org"] == "Streaming"
    assert head.headers["contentFeatures.dlna.org"].startswith("DLNA.ORG_PN=MP3;DLNA.ORG_OP=01;")
    assert head.headers["Connection"] == "close"

    partial = requests.get(url, headers={"Range": "bytes=100-199"}, timeout=10)
    assert partial.status_code == 206
    assert partial.headers["Content-Range"] == f"bytes 100-199/{size}"
    assert partial.content == SONG_FILE.read_bytes()[100:200]

    token_url = url.rsplit("/", 1)[0]
    assert requests.get(token_url, timeout=10).status_code == 200  # the bare URL works too
    assert requests.get(token_url[:-1] + "x", timeout=10).status_code == 401  # wrong token

    # A new song gets a new token; the old URL stops working.
    url2 = server.serve(f"file://{SONG_FILE}", peer="127.0.0.1")
    assert url2 != url
    assert requests.get(url, timeout=10).status_code == 401
    assert requests.get(url2, timeout=10).status_code == 200


def test_relays_songs_from_the_music_server(server: lan_server.LanFileServer):
    # A second server stands in for the music server.
    music_server = lan_server.LanFileServer("the music server")
    music_server.start()
    try:
        upstream = music_server.serve(f"file://{SONG_FILE}", peer="127.0.0.1")
        url = server.serve(upstream, peer="127.0.0.1", title="Song")
        assert url.endswith("/song.mp3")
        size = SONG_FILE.stat().st_size

        head = requests.head(url, timeout=10)
        assert head.status_code == 200
        assert head.headers["Content-Type"] == "audio/mpeg"
        assert int(head.headers["Content-Length"]) == size
        assert head.headers["Accept-Ranges"] == "bytes"
        assert head.headers["transferMode.dlna.org"] == "Streaming"
        assert head.headers["contentFeatures.dlna.org"].startswith("DLNA.ORG_PN=MP3;")

        partial = requests.get(url, headers={"Range": "bytes=100-199"}, timeout=10)
        assert partial.status_code == 206
        assert partial.headers["Content-Range"] == f"bytes 100-199/{size}"
        assert partial.content == SONG_FILE.read_bytes()[100:200]

        assert requests.get(url, timeout=10).content == SONG_FILE.read_bytes()

        # The music server says no: so do we, rather than hanging up on the device.
        url = server.serve(upstream.replace("/s/", "/s/x"), peer="127.0.0.1")
        assert requests.get(url, timeout=10).status_code == 502
    finally:
        music_server.stop()

    url = server.serve(upstream, peer="127.0.0.1")  # the music server is gone now
    assert requests.get(url, timeout=10).status_code == 502


def test_restart_picks_a_port_again(server: lan_server.LanFileServer):
    server.start()
    assert server.running and server.port
    assert requests.get(f"http://127.0.0.1:{server.port}/", timeout=10).status_code == 200


needs_ffmpeg = pytest.mark.skipif(not lan_server.ffmpeg_available(), reason="ffmpeg not installed")


@pytest.fixture
def wav_file(tmp_path: Path) -> Path:
    # One second of silence, 44.1 kHz stereo 16 bit: something that isn't MP3.
    filename = tmp_path / "song.wav"
    with wave.open(str(filename), "wb") as f:
        f.setnchannels(2)
        f.setsampwidth(2)
        f.setframerate(44100)
        f.writeframes(struct.pack("<h", 0) * 2 * 44100)
    return filename


@needs_ffmpeg
def test_transcodes_a_cached_file(server: lan_server.LanFileServer, wav_file: Path):
    url = server.serve(f"file://{wav_file}", peer="127.0.0.1", transcode="mp3")
    assert url.endswith("/song.mp3")

    # Transcoded in full first: devices want the length, and can then seek in it too.
    head = requests.head(url, timeout=30)
    assert head.status_code == 200
    assert head.headers["Content-Type"] == "audio/mpeg"
    size = int(head.headers["Content-Length"])
    assert 2000 < size < 60000  # 1 s at 320 kbps is about 40 kB
    assert head.headers["contentFeatures.dlna.org"].startswith("DLNA.ORG_PN=MP3;DLNA.ORG_OP=01;")

    mp3 = requests.get(url, timeout=30).content
    assert len(mp3) == size
    assert mp3[:3] == b"ID3" or mp3[:2] == b"\xff\xfb"
    partial = requests.get(url, headers={"Range": "bytes=10-19"}, timeout=10)
    assert partial.status_code == 206 and partial.content == mp3[10:20]
    assert len(list(server._work_dir.iterdir())) == 1  # type: ignore[union-attr]

    # An MP3 isn't re-encoded; it is served as it is, with its length and ranges.
    url = server.serve(f"file://{SONG_FILE}", peer="127.0.0.1", transcode="mp3")
    head = requests.head(url, timeout=10)
    assert int(head.headers["Content-Length"]) == SONG_FILE.stat().st_size

    # To WAV, for a device that only lists PCM.
    url = server.serve(f"file://{SONG_FILE}", peer="127.0.0.1", transcode="wav")
    assert url.endswith("/song.wav")
    wav = requests.get(url, timeout=60).content
    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE"
    assert len(list(server._work_dir.iterdir())) == 1  # type: ignore[union-attr]
    work_dir = server._work_dir
    server.stop()
    assert work_dir and not work_dir.exists()


@needs_ffmpeg
def test_transcodes_what_the_music_server_did_not(
    server: lan_server.LanFileServer, wav_file: Path
):
    music_server = lan_server.LanFileServer("the music server")
    music_server.start()
    try:
        # "The music server" ignores the request to transcode and sends the WAV.
        upstream = music_server.serve(f"file://{wav_file}", peer="127.0.0.1")
        url = server.serve(upstream + "?format=mp3", peer="127.0.0.1", transcode="mp3")
        response = requests.get(url, timeout=30)
        assert response.headers["Content-Type"] == "audio/mpeg"
        assert int(response.headers["Content-Length"]) == len(response.content)
        assert response.content[:3] == b"ID3" or response.content[:2] == b"\xff\xfb"

        # When it does transcode, its MP3 is passed through.
        upstream = music_server.serve(f"file://{SONG_FILE}", peer="127.0.0.1")
        url = server.serve(upstream, peer="127.0.0.1", transcode="mp3")
        assert requests.get(url, timeout=30).content == SONG_FILE.read_bytes()
    finally:
        music_server.stop()
