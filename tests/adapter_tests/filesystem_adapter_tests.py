import shutil
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Generator, Iterable, List, Tuple, cast

import pytest
from peewee import SelectQuery

from sublime_music.adapters import (
    AlbumSearchQuery,
    CacheMissError,
    SongCacheStatus,
    SongQuery,
    api_objects as SublimeAPI,
)
from sublime_music.adapters.filesystem import FilesystemAdapter, models
from sublime_music.adapters.filesystem.models import Artist, Directory
from sublime_music.adapters.subsonic import api_objects as SubsonicAPI

MOCK_DATA_FILES = Path(__file__).parent.joinpath("mock_data")
MOCK_ALBUM_ART = MOCK_DATA_FILES.joinpath("album-art.png")
MOCK_ALBUM_ART2 = MOCK_DATA_FILES.joinpath("album-art2.png")
MOCK_ALBUM_ART3 = MOCK_DATA_FILES.joinpath("album-art3.png")
MOCK_SONG_FILE = MOCK_DATA_FILES.joinpath("test-song.mp3")
MOCK_SONG_FILE2 = MOCK_DATA_FILES.joinpath("test-song2.mp3")
MOCK_ALBUM_ART_HASH = "3ac0c5267c92c9da4a4beb552f45ff3198780986"
MOCK_ALBUM_ART2_HASH = "031a8a1ca01f64f851a22d5478e693825a00fb23"
MOCK_ALBUM_ART3_HASH = "46a8af0f8fe370e59202a545803e8bbb3a4a41ee"
MOCK_SONG_FILE_HASH = "fe12d0712dbfd6ff7f75ef3783856a7122a78b0a"
MOCK_SONG_FILE2_HASH = "c32597c724e2e484dbf5856930b2e5bb80de13b7"

MOCK_SUBSONIC_SONGS = [
    SubsonicAPI.Song(
        "2",
        title="Song 2",
        parent_id="d1",
        _album="foo",
        album_id="a1",
        _artist="cool",
        artist_id="art1",
        duration=timedelta(seconds=20.8),
        path="foo/song2.mp3",
        cover_art="s2",
        _genre="Bar",
    ),
    SubsonicAPI.Song(
        "1",
        title="Song 1",
        parent_id="d1",
        _album="foo",
        album_id="a1",
        _artist="foo",
        artist_id="art2",
        duration=timedelta(seconds=10.2),
        path="foo/song1.mp3",
        cover_art="s1",
        _genre="Foo",
    ),
    SubsonicAPI.Song(
        "1",
        title="Song 1",
        parent_id="d1",
        _album="foo",
        album_id="a1",
        _artist="foo",
        artist_id="art2",
        duration=timedelta(seconds=10.2),
        path="foo/song1.mp3",
        cover_art="s1",
        _genre="Foo",
    ),
]

KEYS = FilesystemAdapter.CachedDataKey


@pytest.fixture
def adapter(tmp_path: Path):
    adapter = FilesystemAdapter({}, tmp_path)
    yield adapter
    adapter.shutdown()


@pytest.fixture
def cache_adapter(tmp_path: Path):
    adapter = FilesystemAdapter({}, tmp_path, is_cache=True)
    yield adapter
    adapter.shutdown()


def mock_data_files(
    request_name: str,
    mode: str = "r",
) -> Generator[Tuple[Path, Any], None, None]:
    """
    Yields all of the files in the mock_data directory that start with ``request_name``.
    """
    for file in MOCK_DATA_FILES.iterdir():
        if file.name.split("-")[0] in request_name:
            with open(file, mode) as f:
                yield file, f.read()


def verify_songs(
    actual_songs: Iterable[SublimeAPI.Directory | SublimeAPI.Song],
    expected_songs: Iterable[SubsonicAPI.Song],
):
    actual_songs, expected_songs = (list(actual_songs), list(expected_songs))
    assert len(actual_songs) == len(expected_songs)
    for actual, song in zip(actual_songs, expected_songs):
        for k, v in asdict(song).items():
            if k in (
                "_genre",
                "_album",
                "_artist",
                "album_id",
                "artist_id",
                "_album_artists",
                "_genres",
            ):
                continue
            print(k, "->", v)  # noqa: T201

            actual_value = getattr(actual, k)

            if k == "album":
                assert ("a1", "foo") == (actual_value.id, actual_value.name)
            elif k == "genre":
                assert v["name"] == actual_value.name
            elif k == "artist":
                assert (v["id"], v["name"]) == (actual_value.id, actual_value.name)
            else:
                assert actual_value == v


def test_caching_get_playlists(cache_adapter: FilesystemAdapter):
    with pytest.raises(CacheMissError):
        cache_adapter.get_playlists()

    # Ingest an empty list (for example, no playlists added yet to server).
    cache_adapter.ingest_new_data(KEYS.PLAYLISTS, None, [])

    # After the first cache miss of get_playlists, even if an empty list is
    # returned, the next one should not be a cache miss.
    cache_adapter.get_playlists()

    # Ingest two playlists.
    cache_adapter.ingest_new_data(
        KEYS.PLAYLISTS,
        None,
        [
            SubsonicAPI.Playlist("1", "test1", comment="comment"),
            SubsonicAPI.Playlist("2", "test2"),
        ],
    )

    playlists = cache_adapter.get_playlists()
    assert len(playlists) == 2
    assert (playlists[0].id, playlists[0].name, playlists[0].comment) == (
        "1",
        "test1",
        "comment",
    )
    assert (playlists[1].id, playlists[1].name) == ("2", "test2")

    # Ingest a new playlist list with one of them deleted.
    cache_adapter.ingest_new_data(
        KEYS.PLAYLISTS,
        None,
        [
            SubsonicAPI.Playlist("1", "test1", comment="comment"),
            SubsonicAPI.Playlist("3", "test3"),
        ],
    )

    # Now, Playlist 2 should be gone.
    playlists = cache_adapter.get_playlists()
    assert len(playlists) == 2
    assert (playlists[0].id, playlists[0].name, playlists[0].comment) == (
        "1",
        "test1",
        "comment",
    )
    assert (playlists[1].id, playlists[1].name) == ("3", "test3")


def test_no_caching_get_playlists(adapter: FilesystemAdapter):
    adapter.get_playlists()

    # TODO (#188): Create a playlist (that should be allowed only if this is acting as
    # a ground truth adapter)
    # cache_adapter.create_playlist()

    adapter.get_playlists()
    # TODO (#188): verify playlist


def test_caching_get_playlist_details(cache_adapter: FilesystemAdapter):
    with pytest.raises(CacheMissError):
        cache_adapter.get_playlist_details("1")

    # Simulate the playlist being retrieved from Subsonic.
    cache_adapter.ingest_new_data(
        KEYS.PLAYLIST_DETAILS,
        "1",
        SubsonicAPI.Playlist("1", "test1", songs=MOCK_SUBSONIC_SONGS[:2]),
    )

    playlist = cache_adapter.get_playlist_details("1")
    assert playlist.id == "1"
    assert playlist.name == "test1"
    assert playlist.song_count == 2
    assert playlist.duration == timedelta(seconds=31)
    verify_songs(playlist.songs, MOCK_SUBSONIC_SONGS[:2])

    # "Force refresh" the playlist and add a new song (duplicate).
    cache_adapter.ingest_new_data(
        KEYS.PLAYLIST_DETAILS,
        "1",
        SubsonicAPI.Playlist("1", "foo", songs=MOCK_SUBSONIC_SONGS),
    )

    playlist = cache_adapter.get_playlist_details("1")
    assert playlist.id == "1"
    assert playlist.name == "foo"
    assert playlist.song_count == 3
    assert playlist.duration == timedelta(seconds=41.2)
    verify_songs(playlist.songs, MOCK_SUBSONIC_SONGS)

    with pytest.raises(CacheMissError):
        cache_adapter.get_playlist_details("2")

    # Now ingest the playlist list and make sure that it doesn't override the songs in
    # the first Playlist.
    cache_adapter.ingest_new_data(
        KEYS.PLAYLISTS,
        None,
        [
            SubsonicAPI.Playlist("1", "foo", song_count=3, duration=timedelta(seconds=41.2)),
            SubsonicAPI.Playlist("3", "test3", song_count=3, duration=timedelta(seconds=30)),
        ],
    )

    playlist = cache_adapter.get_playlist_details("1")
    verify_songs(playlist.songs, MOCK_SUBSONIC_SONGS)


def test_no_caching_get_playlist_details(adapter: FilesystemAdapter):
    with pytest.raises(Exception, match="<Model: Playlist> with id=1 does not exist"):
        adapter.get_playlist_details("1")

    # TODO (#188): Create a playlist (that should be allowed only if this is acting as
    # a ground truth adapter)
    # cache_adapter.create_playlist()

    # adapter.get_playlist_details('1')
    # TODO (#188): verify playlist details


def test_caching_get_playlist_then_details(cache_adapter: FilesystemAdapter):
    # Ingest a list of playlists (like the sidebar, without songs)
    cache_adapter.ingest_new_data(
        KEYS.PLAYLISTS,
        None,
        [SubsonicAPI.Playlist("1", "test1"), SubsonicAPI.Playlist("2", "test2")],
    )

    # Trying to get playlist details should generate a cache miss, but should
    # include the data that we know about.
    try:
        cache_adapter.get_playlist_details("1")
        assert 0, "DID NOT raise CacheMissError"
    except CacheMissError as e:
        assert e.partial_data
        assert e.partial_data.id == "1"
        assert e.partial_data.name == "test1"

    # Simulate getting playlist details for id=1, then id=2
    cache_adapter.ingest_new_data(
        KEYS.PLAYLIST_DETAILS,
        "1",
        SubsonicAPI.Playlist("1", "test1"),
    )

    cache_adapter.ingest_new_data(
        KEYS.PLAYLIST_DETAILS,
        "2",
        SubsonicAPI.Playlist("2", "test2", songs=MOCK_SUBSONIC_SONGS),
    )

    # Going back and getting playlist details for the first one should not
    # cache miss.
    playlist = cache_adapter.get_playlist_details("1")
    assert playlist.id == "1"
    assert playlist.name == "test1"


def test_cache_cover_art(cache_adapter: FilesystemAdapter):
    with pytest.raises(CacheMissError):
        cache_adapter.get_cover_art_uri("pl_test1", "file", size=300)

    # After ingesting the data, reading from the cache should give the exact same file.
    cache_adapter.ingest_new_data(KEYS.COVER_ART_FILE, "pl_test1", MOCK_ALBUM_ART)
    cached = Path(cache_adapter.get_cover_art_uri("pl_test1", "file", size=300))
    assert cached == cache_adapter.cover_art_dir.joinpath(MOCK_ALBUM_ART_HASH)
    assert cached.read_bytes() == MOCK_ALBUM_ART.read_bytes()


def test_invalidate_playlist(cache_adapter: FilesystemAdapter):
    cache_adapter.ingest_new_data(
        KEYS.PLAYLISTS,
        None,
        [SubsonicAPI.Playlist("1", "test1"), SubsonicAPI.Playlist("2", "test2")],
    )
    cache_adapter.ingest_new_data(
        KEYS.COVER_ART_FILE,
        "pl_test1",
        MOCK_ALBUM_ART,
    )
    cache_adapter.ingest_new_data(
        KEYS.PLAYLIST_DETAILS,
        "2",
        SubsonicAPI.Playlist("2", "test2", cover_art="pl_2", songs=[]),
    )
    cache_adapter.ingest_new_data(
        KEYS.COVER_ART_FILE,
        "pl_2",
        MOCK_ALBUM_ART2,
    )

    stale_uri_1 = cache_adapter.get_cover_art_uri("pl_test1", "file", size=300)
    stale_uri_2 = cache_adapter.get_cover_art_uri("pl_2", "file", size=300)

    cache_adapter.invalidate_data(KEYS.PLAYLISTS, None)
    cache_adapter.invalidate_data(KEYS.PLAYLIST_DETAILS, "2")
    cache_adapter.invalidate_data(KEYS.COVER_ART_FILE, "pl_test1")

    # After invalidating the data, it should cache miss, but still have the old, stale,
    # data.
    try:
        cache_adapter.get_playlists()
        assert 0, "DID NOT raise CacheMissError"
    except CacheMissError as e:
        assert e.partial_data
        assert len(e.partial_data) == 2

    try:
        cache_adapter.get_cover_art_uri("pl_test1", "file", size=300)
        assert 0, "DID NOT raise CacheMissError"
    except CacheMissError as e:
        assert e.partial_data
        assert e.partial_data == stale_uri_1

    try:
        cache_adapter.get_playlist_details("2")
        assert 0, "DID NOT raise CacheMissError"
    except CacheMissError as e:
        assert e.partial_data

    # Even though the pl_2 cover art file wasn't explicitly invalidated, it should have
    # been invalidated with the playlist details invalidation.
    try:
        cache_adapter.get_cover_art_uri("pl_2", "file", size=300)
        assert 0, "DID NOT raise CacheMissError"
    except CacheMissError as e:
        assert e.partial_data
        assert e.partial_data == stale_uri_2


def test_invalidate_song_file(cache_adapter: FilesystemAdapter):
    cache_adapter.ingest_new_data(KEYS.SONG, "2", MOCK_SUBSONIC_SONGS[0])
    cache_adapter.ingest_new_data(KEYS.SONG, "1", MOCK_SUBSONIC_SONGS[1])
    cache_adapter.ingest_new_data(
        KEYS.COVER_ART_FILE,
        "s1",
        MOCK_ALBUM_ART,
    )
    cache_adapter.ingest_new_data(KEYS.SONG_FILE, "1", (None, MOCK_SONG_FILE, None))
    cache_adapter.ingest_new_data(KEYS.SONG_FILE, "2", (None, MOCK_SONG_FILE2, None))

    cache_adapter.invalidate_data(KEYS.SONG_FILE, "1")
    cache_adapter.invalidate_data(KEYS.COVER_ART_FILE, "s1")

    with pytest.raises(CacheMissError):
        cache_adapter.get_song_file_uri("1", "file")

    with pytest.raises(CacheMissError):
        cache_adapter.get_cover_art_uri("s1", "file", size=300)

    # Make sure it didn't delete the other song.
    assert cache_adapter.get_song_file_uri("2", "file").endswith("song2.mp3")


def test_malformed_song_path(cache_adapter: FilesystemAdapter):
    cache_adapter.ingest_new_data(KEYS.SONG, "1", MOCK_SUBSONIC_SONGS[1])
    cache_adapter.ingest_new_data(KEYS.SONG, "2", MOCK_SUBSONIC_SONGS[0])
    cache_adapter.ingest_new_data(KEYS.SONG_FILE, "1", ("/malformed/path", MOCK_SONG_FILE, None))
    cache_adapter.ingest_new_data(
        KEYS.SONG_FILE, "2", ("fine/path/song2.mp3", MOCK_SONG_FILE2, None)
    )

    song_uri = cache_adapter.get_song_file_uri("1", "file")
    assert song_uri.endswith(f"/music/{MOCK_SONG_FILE_HASH}")

    song_uri2 = cache_adapter.get_song_file_uri("2", "file")
    assert song_uri2.endswith("fine/path/song2.mp3")


def test_get_cached_statuses(cache_adapter: FilesystemAdapter):
    cache_adapter.ingest_new_data(KEYS.SONG, "1", MOCK_SUBSONIC_SONGS[1])
    assert cache_adapter.get_cached_statuses(["1"]) == {"1": SongCacheStatus.NOT_CACHED}

    cache_adapter.ingest_new_data(KEYS.SONG_FILE, "1", (None, MOCK_SONG_FILE, None))
    assert cache_adapter.get_cached_statuses(["1"]) == {"1": SongCacheStatus.CACHED}

    cache_adapter.ingest_new_data(KEYS.SONG_FILE_PERMANENT, "1", None)
    assert cache_adapter.get_cached_statuses(["1"]) == {"1": SongCacheStatus.PERMANENTLY_CACHED}

    cache_adapter.invalidate_data(KEYS.SONG_FILE, "1")
    assert cache_adapter.get_cached_statuses(["1"]) == {"1": SongCacheStatus.CACHED_STALE}

    cache_adapter.delete_data(KEYS.SONG_FILE, "1")
    assert cache_adapter.get_cached_statuses(["1"]) == {"1": SongCacheStatus.NOT_CACHED}


def test_delete_playlists(cache_adapter: FilesystemAdapter):
    cache_adapter.ingest_new_data(
        KEYS.PLAYLIST_DETAILS,
        "1",
        SubsonicAPI.Playlist("1", "test1", cover_art="pl_1", songs=[]),
    )
    cache_adapter.ingest_new_data(
        KEYS.PLAYLIST_DETAILS,
        "2",
        SubsonicAPI.Playlist("2", "test1", cover_art="pl_2", songs=[]),
    )
    cache_adapter.ingest_new_data(
        KEYS.COVER_ART_FILE,
        "pl_1",
        MOCK_ALBUM_ART,
    )

    # Deleting a playlist should get rid of it entirely.
    cache_adapter.delete_data(KEYS.PLAYLIST_DETAILS, "2")
    try:
        cache_adapter.get_playlist_details("2")
        assert 0, "DID NOT raise CacheMissError"
    except CacheMissError as e:
        assert e.partial_data is None

    # Deleting a playlist with associated cover art should get rid the cover art too.
    cache_adapter.delete_data(KEYS.PLAYLIST_DETAILS, "1")
    try:
        cache_adapter.get_cover_art_uri("pl_1", "file", size=300)
        assert 0, "DID NOT raise CacheMissError"
    except CacheMissError as e:
        assert e.partial_data is None

    # Even if the cover art failed to be deleted, it should cache miss.
    shutil.copy(
        MOCK_ALBUM_ART,
        str(cache_adapter.cover_art_dir.joinpath(MOCK_ALBUM_ART_HASH)),
    )
    try:
        cache_adapter.get_cover_art_uri("pl_1", "file", size=300)
        assert 0, "DID NOT raise CacheMissError"
    except CacheMissError as e:
        assert e.partial_data is None


def test_delete_song_data(cache_adapter: FilesystemAdapter):
    cache_adapter.ingest_new_data(KEYS.SONG, "1", MOCK_SUBSONIC_SONGS[1])
    cache_adapter.ingest_new_data(KEYS.SONG_FILE, "1", (None, MOCK_SONG_FILE, None))
    cache_adapter.ingest_new_data(
        KEYS.COVER_ART_FILE,
        "s1",
        MOCK_ALBUM_ART,
    )

    music_file_path = cache_adapter.get_song_file_uri("1", "file")
    cover_art_path = cache_adapter.get_cover_art_uri("s1", "file", size=300)

    cache_adapter.delete_data(KEYS.SONG_FILE, "1")
    cache_adapter.delete_data(KEYS.COVER_ART_FILE, "s1")

    assert not Path(music_file_path).exists()
    assert not Path(cover_art_path).exists()

    try:
        cache_adapter.get_song_file_uri("1", "file")
        assert 0, "DID NOT raise CacheMissError"
    except CacheMissError as e:
        assert e.partial_data is None

    try:
        cache_adapter.get_cover_art_uri("s1", "file", size=300)
        assert 0, "DID NOT raise CacheMissError"
    except CacheMissError as e:
        assert e.partial_data is None


def test_caching_get_genres(cache_adapter: FilesystemAdapter):
    with pytest.raises(CacheMissError):
        cache_adapter.get_genres()

    cache_adapter.ingest_new_data(KEYS.SONG, "2", MOCK_SUBSONIC_SONGS[0])
    cache_adapter.ingest_new_data(KEYS.SONG, "1", MOCK_SUBSONIC_SONGS[1])

    # Getting genres now should look at what's on the songs. This sould cache miss, but
    # still give some data.
    try:
        cache_adapter.get_genres()
        assert 0, "DID NOT raise CacheMissError"
    except CacheMissError as e:
        assert [g.name for g in cast(Iterable, e.partial_data)] == ["Bar", "Foo"]

    # After we actually ingest the actual list, it should be returned instead.
    cache_adapter.ingest_new_data(
        KEYS.GENRES,
        None,
        [
            SubsonicAPI.Genre("Bar", 10, 20),
            SubsonicAPI.Genre("Baz", 10, 20),
            SubsonicAPI.Genre("Foo", 10, 20),
        ],
    )
    assert {g.name for g in cache_adapter.get_genres()} == {"Bar", "Baz", "Foo"}


def test_caching_get_song_details(cache_adapter: FilesystemAdapter):
    with pytest.raises(CacheMissError):
        cache_adapter.get_song_details("1")

    # Simulate the song details being retrieved from Subsonic.
    cache_adapter.ingest_new_data(KEYS.SONG, "1", MOCK_SUBSONIC_SONGS[1])

    song = cache_adapter.get_song_details("1")
    assert song.id == "1"
    assert song.title == "Song 1"
    assert song.album
    assert (song.album.id, song.album.name) == ("a1", "foo")
    assert song.artist and song.artist.name == "foo"
    assert song.parent_id == "d1"
    assert song.duration == timedelta(seconds=10.2)
    assert song.path == "foo/song1.mp3"
    assert song.genre and song.genre.name == "Foo"

    # "Force refresh" the song details
    cache_adapter.ingest_new_data(
        KEYS.SONG,
        "1",
        SubsonicAPI.Song(
            "1",
            title="Song 1",
            parent_id="bar",
            _album="bar",
            album_id="a2",
            _artist="bar",
            artist_id="art2",
            duration=timedelta(seconds=10.2),
            path="bar/song1.mp3",
            _genre="Bar",
        ),
    )

    song = cache_adapter.get_song_details("1")
    assert song.id == "1"
    assert song.title == "Song 1"
    assert song.album and song.artist
    assert (song.album.id, song.album.name) == ("a2", "bar")
    assert (song.artist.id, song.artist.name) == ("art2", "bar")
    assert song.parent_id == "bar"
    assert song.duration == timedelta(seconds=10.2)
    assert song.path == "bar/song1.mp3"
    assert song.genre and song.genre.name == "Bar"

    with pytest.raises(CacheMissError):
        cache_adapter.get_playlist_details("2")


def test_caching_get_song_details_missing_data(cache_adapter: FilesystemAdapter):
    with pytest.raises(CacheMissError):
        cache_adapter.get_song_details("1")

    # Ingest a song without an album ID and artist ID, but with album and artist name.
    cache_adapter.ingest_new_data(
        KEYS.SONG,
        "1",
        SubsonicAPI.Song(
            "1",
            title="Song 1",
            parent_id="bar",
            _album="bar",
            _artist="foo",
            duration=timedelta(seconds=10.2),
            path="foo/bar/song1.mp3",
            _genre="Bar",
        ),
    )

    song = cache_adapter.get_song_details("1")
    assert song.id == "1"
    assert song.title == "Song 1"
    assert song.album
    assert (song.album.id, song.album.name) == (
        "invalid:62cdb7020ff920e5aa642c3d4066950dd1f01f4d",
        "bar",
    )
    assert song.artist
    assert (song.artist.id, song.artist.name) == (
        "invalid:0beec7b5ea3f0fdbc95d0dd47f3c5bc275da8a33",
        "foo",
    )
    assert song.parent_id == "bar"
    assert song.duration == timedelta(seconds=10.2)
    assert song.path == "foo/bar/song1.mp3"
    assert song.genre and song.genre.name == "Bar"

    # Because the album and artist are invalid (doesn't have an album/artist ID), it
    # shouldn't show up in any results.
    try:
        list(
            cache_adapter.get_albums(AlbumSearchQuery(AlbumSearchQuery.Type.ALPHABETICAL_BY_NAME))
        )
    except CacheMissError as e:
        assert e.partial_data is not None
        assert len(e.partial_data) == 0

    albums = list(cache_adapter.get_all_albums())
    assert len(albums) == 0

    with pytest.raises(CacheMissError):
        cache_adapter.get_album("invalid:62cdb7020ff920e5aa642c3d4066950dd1f01f4d")

    try:
        list(cache_adapter.get_artists())
    except CacheMissError as e:
        assert e.partial_data is not None
        assert len(e.partial_data) == 0

    with pytest.raises(CacheMissError):
        cache_adapter.get_artist("invalid:0beec7b5ea3f0fdbc95d0dd47f3c5bc275da8a33")


def test_caching_less_info(cache_adapter: FilesystemAdapter):
    cache_adapter.ingest_new_data(
        KEYS.SONG,
        "1",
        SubsonicAPI.Song(
            "1",
            title="Song 1",
            parent_id="bar",
            _album="bar",
            album_id="a2",
            _artist="bar",
            artist_id="art2",
            duration=timedelta(seconds=10.2),
            path="bar/song1.mp3",
            _genre="Bar",
        ),
    )
    cache_adapter.ingest_new_data(
        KEYS.SONG,
        "1",
        SubsonicAPI.Song(
            "1",
            title="Song 1",
            parent_id="bar",
            duration=timedelta(seconds=10.2),
            path="bar/song1.mp3",
        ),
    )

    song = cache_adapter.get_song_details("1")
    assert song.album and song.album.name == "bar"
    assert song.artist and song.artist.name == "bar"
    assert song.genre and song.genre.name == "Bar"


def test_caching_get_artists(cache_adapter: FilesystemAdapter):
    with pytest.raises(CacheMissError):
        cache_adapter.get_artists()

    # Ingest artists.
    cache_adapter.ingest_new_data(
        KEYS.ARTISTS,
        None,
        [
            SubsonicAPI.ArtistAndArtistInfo(id="1", name="test1", album_count=3, albums=[]),
            SubsonicAPI.ArtistAndArtistInfo(id="2", name="test2", album_count=4),
        ],
    )

    artists = cache_adapter.get_artists()
    assert len(artists) == 2
    assert (artists[0].id, artists[0].name, artists[0].album_count) == ("1", "test1", 3)
    assert (artists[1].id, artists[1].name, artists[1].album_count) == ("2", "test2", 4)

    # Ingest a new artists list with one of them deleted.
    cache_adapter.ingest_new_data(
        KEYS.ARTISTS,
        None,
        [
            SubsonicAPI.ArtistAndArtistInfo(id="1", name="test1", album_count=3),
            SubsonicAPI.ArtistAndArtistInfo(id="3", name="test3", album_count=8),
        ],
    )

    # Now, artist 2 should be gone.
    artists = cache_adapter.get_artists()
    assert len(artists) == 2
    assert (artists[0].id, artists[0].name, artists[0].album_count) == ("1", "test1", 3)
    assert (artists[1].id, artists[1].name, artists[1].album_count) == ("3", "test3", 8)


def test_caching_get_ignored_articles(cache_adapter: FilesystemAdapter):
    with pytest.raises(CacheMissError):
        cache_adapter.get_ignored_articles()

    # Ingest ignored_articles.
    cache_adapter.ingest_new_data(KEYS.IGNORED_ARTICLES, None, {"Foo", "Bar"})
    artists = cache_adapter.get_ignored_articles()
    assert {"Foo", "Bar"} == artists

    # Ingest a new artists list with one of them deleted.
    cache_adapter.ingest_new_data(KEYS.IGNORED_ARTICLES, None, {"Foo", "Baz"})
    artists = cache_adapter.get_ignored_articles()
    assert {"Foo", "Baz"} == artists


def test_caching_get_artist(cache_adapter: FilesystemAdapter):
    with pytest.raises(CacheMissError):
        cache_adapter.get_artist("1")

    # Simulate the artist details being retrieved from Subsonic.
    cache_adapter.ingest_new_data(
        KEYS.ARTIST,
        "1",
        SubsonicAPI.ArtistAndArtistInfo(
            id="1",
            name="Bar",
            album_count=1,
            artist_image_url="image",
            similar_artists=[
                SubsonicAPI.ArtistAndArtistInfo(id="A", name="B"),
                SubsonicAPI.ArtistAndArtistInfo(id="C", name="D"),
            ],
            biography="this is a bio",
            music_brainz_id="mbid",
            albums=[SubsonicAPI.Album(id="1", name="Foo", _artist="Bar", artist_id="1")],
        ),
    )

    artist = cache_adapter.get_artist("1")
    assert artist.artist_image_url and (
        artist.id,
        artist.name,
        artist.album_count,
        artist.artist_image_url,
        artist.biography,
        artist.music_brainz_id,
    ) == ("1", "Bar", 1, "image", "this is a bio", "mbid")
    assert artist.similar_artists == [
        SubsonicAPI.ArtistAndArtistInfo(id="A", name="B"),
        SubsonicAPI.ArtistAndArtistInfo(id="C", name="D"),
    ]
    assert artist.albums and len(artist.albums) == 1
    assert cast(SelectQuery, artist.albums).dicts() == [SubsonicAPI.Album(id="1", name="Foo")]

    # Simulate "force refreshing" the artist details being retrieved from Subsonic.
    cache_adapter.ingest_new_data(
        KEYS.ARTIST,
        "1",
        SubsonicAPI.ArtistAndArtistInfo(
            id="1",
            name="Baz",
            album_count=2,
            artist_image_url="image2",
            similar_artists=[
                SubsonicAPI.ArtistAndArtistInfo(id="A", name="B"),
                SubsonicAPI.ArtistAndArtistInfo(id="E", name="F"),
            ],
            biography="this is a bio2",
            music_brainz_id="mbid2",
            albums=[
                SubsonicAPI.Album(id="1", name="Foo", _artist="Baz", artist_id="1"),
                SubsonicAPI.Album(id="2", name="OHEA", _artist="Baz", artist_id="1"),
            ],
        ),
    )

    artist = cache_adapter.get_artist("1")
    assert artist.artist_image_url and (
        artist.id,
        artist.name,
        artist.album_count,
        artist.artist_image_url,
        artist.biography,
        artist.music_brainz_id,
    ) == ("1", "Baz", 2, "image2", "this is a bio2", "mbid2")
    assert artist.similar_artists == [
        SubsonicAPI.ArtistAndArtistInfo(id="A", name="B"),
        SubsonicAPI.ArtistAndArtistInfo(id="E", name="F"),
    ]
    assert artist.albums is not None
    expected = [
        SubsonicAPI.Album(id="1", name="Foo"),
        SubsonicAPI.Album(id="2", name="OHEA"),
    ]
    count = 0
    for album, exp in zip(artist.albums, expected):
        assert album.id == exp.id
        assert album.name == exp.name
        count += 1
    assert count == 2


def test_caching_get_album(cache_adapter: FilesystemAdapter):
    with pytest.raises(CacheMissError):
        cache_adapter.get_album("1")

    # Simulate the artist details being retrieved from Subsonic.
    cache_adapter.ingest_new_data(
        KEYS.ALBUM,
        "a1",
        SubsonicAPI.Album(
            id="a1",
            name="foo",
            cover_art="c",
            song_count=2,
            year=2020,
            duration=timedelta(seconds=31),
            play_count=20,
            _artist="cool",
            artist_id="art1",
            songs=MOCK_SUBSONIC_SONGS[:2],
        ),
    )

    album = cache_adapter.get_album("a1")
    assert album and album.cover_art
    assert (
        album.id,
        album.name,
        album.cover_art,
        album.song_count,
        album.year,
        album.play_count,
    ) == (
        "a1",
        "foo",
        "c",
        2,
        2020,
        20,
    )
    assert album.artist
    assert (album.artist.id, album.artist.name) == ("art1", "cool")
    assert album.songs
    verify_songs(album.songs, MOCK_SUBSONIC_SONGS[:2])


def test_caching_invalidate_artist(cache_adapter: FilesystemAdapter):
    # Simulate the artist details being retrieved from Subsonic.
    cache_adapter.ingest_new_data(
        KEYS.ARTIST,
        "artist1",
        SubsonicAPI.ArtistAndArtistInfo(
            id="artist1",
            name="Bar",
            album_count=1,
            artist_image_url="image",
            similar_artists=[
                SubsonicAPI.ArtistAndArtistInfo(id="A", name="B"),
                SubsonicAPI.ArtistAndArtistInfo(id="C", name="D"),
            ],
            biography="this is a bio",
            music_brainz_id="mbid",
            albums=[
                SubsonicAPI.Album(id="1", name="Foo", _artist="Bar", artist_id="artist1"),
                SubsonicAPI.Album(id="2", name="Bar", _artist="Bar", artist_id="artist1"),
            ],
        ),
    )
    cache_adapter.ingest_new_data(
        KEYS.ALBUM,
        "1",
        SubsonicAPI.Album(id="1", name="Foo", artist_id="artist1", cover_art="1"),
    )
    cache_adapter.ingest_new_data(
        KEYS.ALBUM,
        "2",
        SubsonicAPI.Album(id="2", name="Bar", artist_id="artist1", cover_art="2"),
    )
    cache_adapter.ingest_new_data(KEYS.COVER_ART_FILE, "image", MOCK_ALBUM_ART3)
    cache_adapter.ingest_new_data(KEYS.COVER_ART_FILE, "1", MOCK_ALBUM_ART)
    cache_adapter.ingest_new_data(KEYS.COVER_ART_FILE, "2", MOCK_ALBUM_ART2)

    stale_artist = cache_adapter.get_artist("artist1")
    stale_album_1 = cache_adapter.get_album("1")
    stale_album_2 = cache_adapter.get_album("2")
    stale_artist_artwork = cache_adapter.get_cover_art_uri("image", "file", size=300)
    stale_cover_art_1 = cache_adapter.get_cover_art_uri("1", "file", size=300)
    stale_cover_art_2 = cache_adapter.get_cover_art_uri("2", "file", size=300)

    cache_adapter.invalidate_data(KEYS.ARTIST, "artist1")

    # Test the cascade of cache invalidations.
    try:
        cache_adapter.get_artist("artist1")
        assert 0, "DID NOT raise CacheMissError"
    except CacheMissError as e:
        assert e.partial_data
        assert e.partial_data == stale_artist

    try:
        cache_adapter.get_album("1")
        assert 0, "DID NOT raise CacheMissError"
    except CacheMissError as e:
        assert e.partial_data
        assert e.partial_data == stale_album_1

    try:
        cache_adapter.get_album("2")
        assert 0, "DID NOT raise CacheMissError"
    except CacheMissError as e:
        assert e.partial_data
        assert e.partial_data == stale_album_2

    try:
        cache_adapter.get_cover_art_uri("image", "file", size=300)
        assert 0, "DID NOT raise CacheMissError"
    except CacheMissError as e:
        assert e.partial_data
        assert e.partial_data == stale_artist_artwork

    try:
        cache_adapter.get_cover_art_uri("1", "file", size=300)
        assert 0, "DID NOT raise CacheMissError"
    except CacheMissError as e:
        assert e.partial_data
        assert e.partial_data == stale_cover_art_1

    try:
        cache_adapter.get_cover_art_uri("2", "file", size=300)
        assert 0, "DID NOT raise CacheMissError"
    except CacheMissError as e:
        assert e.partial_data
        assert e.partial_data == stale_cover_art_2


def _subsonic_child(song: SubsonicAPI.Song) -> Dict[str, Any]:
    """
    The JSON a Subsonic server sends for ``song`` inside a directory listing.
    (dataclasses-json cannot encode a Song since 0.6: two fields share the ``artist`` key.)
    """
    child = {
        "id": song.id,
        "title": song.title,
        "parent": song.parent_id,
        "album": song._album,
        "albumId": song.album_id,
        "artist": song._artist,
        "artistId": song.artist_id,
        "duration": song.duration.total_seconds() if song.duration else None,
        "path": song.path,
        "coverArt": song.cover_art,
        "genre": song._genre,
    }
    return {k: v for k, v in child.items() if v is not None}


def test_get_music_directory(cache_adapter: FilesystemAdapter):
    dir_id = "d1"
    with pytest.raises(CacheMissError):
        cache_adapter.get_directory(dir_id)

    # Simulate the directory details being retrieved from Subsonic.
    cache_adapter.ingest_new_data(
        KEYS.DIRECTORY,
        dir_id,
        SubsonicAPI.Directory(
            dir_id,
            title="foo",
            parent_id=None,
            _children=[_subsonic_child(s) for s in MOCK_SUBSONIC_SONGS[:2]]
            + [
                {
                    "id": "542",
                    "parent": dir_id,
                    "isDir": True,
                    "title": "Crash My Party",
                }
            ],
        ),
    )

    directory = cache_adapter.get_directory(dir_id)
    assert directory and directory.id == dir_id
    assert directory.name == "foo"
    assert directory.parent_id == "root"

    dir_child, *song_children = directory.children
    verify_songs(song_children, MOCK_SUBSONIC_SONGS[:2])
    assert isinstance(dir_child, Directory)
    dir_child = cast(Directory, dir_child)
    assert dir_child.id == "542"
    assert dir_child.parent_id
    assert dir_child.name == "Crash My Party"


def test_search(cache_adapter: FilesystemAdapter):
    with pytest.raises(CacheMissError):
        cache_adapter.get_artist("artist1")
    with pytest.raises(CacheMissError):
        cache_adapter.get_album("album1")
    with pytest.raises(CacheMissError):
        cache_adapter.get_song_details("s1")

    search_result = SublimeAPI.SearchResult("")
    search_result.add_results(
        "albums",
        [
            SubsonicAPI.Album(
                id="album1",
                name="Foo",
                artist_id="artist1",
                _artist="foo",
                cover_art="cal1",
            ),
            SubsonicAPI.Album(
                id="album2",
                name="Boo",
                artist_id="artist1",
                _artist="foo",
                cover_art="cal2",
            ),
        ],
    )
    search_result.add_results(
        "artists",
        [
            SubsonicAPI.ArtistAndArtistInfo(id="artist1", name="foo", cover_art="car1"),
            SubsonicAPI.ArtistAndArtistInfo(id="artist2", name="better boo", cover_art="car2"),
        ],
    )
    search_result.add_results(
        "songs",
        [
            SubsonicAPI.Song(
                "s1",
                "amazing boo",
                cover_art="s1",
                _artist="artist3",
                artist_id="ohea1",
            ),
            SubsonicAPI.Song(
                "s2",
                "foo of all foo",
                cover_art="s2",
                _artist="artist4",
                artist_id="ohea2",
            ),
        ],
    )
    cache_adapter.ingest_new_data(KEYS.SEARCH_RESULTS, None, search_result)

    search_result = cache_adapter.search("foo")
    assert [(s.title, s.artist.name if s.artist else None) for s in search_result.songs] == [
        ("foo of all foo", "artist4"),
        ("amazing boo", "artist3"),
    ]
    assert [a.name for a in search_result.artists] == ["foo", "better boo"]
    assert [a.name for a in search_result.albums] == ["Foo", "Boo"]


def test_caching_get_artists_keeps_artists_referenced_by_songs(cache_adapter: FilesystemAdapter):
    # Song 1 refers to artist "art2", which is not part of the artist index (servers which
    # only index album artists never list track artists).
    cache_adapter.ingest_new_data(KEYS.SONG, "1", MOCK_SUBSONIC_SONGS[1])
    cache_adapter.ingest_new_data(
        KEYS.ARTISTS,
        None,
        [
            SubsonicAPI.ArtistAndArtistInfo(id="1", name="test1", album_count=3),
            SubsonicAPI.ArtistAndArtistInfo(id="2", name="test2", album_count=4),
        ],
    )
    cache_adapter.ingest_new_data(
        KEYS.ARTISTS,
        None,
        [SubsonicAPI.ArtistAndArtistInfo(id="1", name="test1", album_count=3)],
    )

    # Artist 2 is unreferenced, so it is gone. Artist "art2" is still needed by song 1, but
    # only the index is listed.
    assert {a.id for a in cache_adapter.get_artists()} == {"1"}
    assert Artist.get_by_id("art2").name == "foo"
    song = cache_adapter.get_song_details("1")
    assert song.artist is not None
    assert (song.artist.id, song.artist.name) == ("art2", "foo")


def test_song_with_missing_artist_row_has_no_artist(cache_adapter: FilesystemAdapter):
    cache_adapter.ingest_new_data(KEYS.SONG, "1", MOCK_SUBSONIC_SONGS[1])
    Artist.delete().where(Artist.id == "art2").execute()

    # A dangling reference must read as "no artist", not raise.
    song = cache_adapter.get_song_details("1")
    assert song.artist is None
    assert song.album is not None
    assert (song.album.id, song.album.name) == ("a1", "foo")


def test_stale_parent_sentinel_is_migrated(tmp_path: Path):
    first = FilesystemAdapter({}, tmp_path, is_cache=True)
    Directory.create(id="al-1", name=".", parent_id="-1")
    Directory.create(id="al-2", name="music", parent_id="al-1")
    first.shutdown()

    # Opening the cache again migrates the "-1" parent that older versions cached.
    second = FilesystemAdapter({}, tmp_path, is_cache=True)
    assert Directory.get_by_id("al-1").parent_id == "root"
    assert Directory.get_by_id("al-2").parent_id == "al-1"
    second.shutdown()


# Song library
# ======================================================================================
def _library_songs(count: int) -> List[SubsonicAPI.Song]:
    songs = []
    for i in range(count):
        songs.append(
            SubsonicAPI.Song(
                f"tr-{i}",
                title=f"Song {str(i).zfill(5)}",
                parent_id=f"al-{i % 7}",
                _album=f"Album {i % 7}",
                album_id=f"al-{i % 7}",
                _artist=f"Artist {i % 3}",
                artist_id=f"ar-{i % 3}",
                _genre=("Pop", "Rock", None)[i % 3],
                path=f"Artist {i % 3}/Album {i % 7}/{str(i).zfill(5)}.flac",
                size=1000 + i,
                duration=timedelta(seconds=100 + i),
                cover_art=f"tr-{i}",
                track=i % 12 + 1,
                year=1990 + i % 30,
                bit_rate=320,
                suffix="flac",
                created=datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(days=i),
                play_count=i % 5,
                user_rating=(i % 6) or None,
                starred=datetime(2024, 6, 1, tzinfo=timezone.utc) if i % 10 == 0 else None,
            )
        )
    return songs


def test_song_library_not_synced(cache_adapter: FilesystemAdapter):
    assert cache_adapter.song_library_last_synced() is None
    with pytest.raises(CacheMissError):
        cache_adapter.get_song_library(SongQuery())


def test_song_library_sync_and_query(cache_adapter: FilesystemAdapter):
    songs = _library_songs(5000)
    start = time.time()
    cache_adapter.ingest_new_data(KEYS.SONGS, None, songs)
    assert time.time() - start < 10, "bulk ingestion must be fast"
    assert cache_adapter.song_library_last_synced() is not None

    # Synced songs are complete: their details are served from the cache.
    details = cache_adapter.get_song_details("tr-3")
    assert (details.title, details.artist.name if details.artist else None) == (
        "Song 00003",
        "Artist 0",
    )

    library = cache_adapter.get_song_library(SongQuery())
    assert len(library) == 5000
    # Default sort: date added, newest first.
    assert library[0].id == "tr-4999" and library[-1].id == "tr-0"
    by_artist = cache_adapter.get_song_library(
        SongQuery(sort_column="artist", sort_descending=False)
    )
    assert [s.artist for s in by_artist[:3]] == ["Artist 0"] * 3
    first = next(s for s in library if s.id == "tr-3")
    assert (first.title, first.album, first.artist, first.genre) == (
        "Song 00003",
        "Album 3",
        "Artist 0",
        "Pop",
    )
    assert first.duration == timedelta(seconds=103)
    assert first.created == datetime(2024, 1, 4, tzinfo=timezone.utc)
    assert (first.bit_rate, first.suffix, first.play_count, first.cover_art) == (
        320,
        "flac",
        3,
        "tr-3",
    )
    assert first.cache_status == SongCacheStatus.NOT_CACHED

    # Sorting, with empty values last.
    by_genre = cache_adapter.get_song_library(
        SongQuery(sort_column="genre", sort_descending=False)
    )
    assert by_genre[0].genre == "Pop" and by_genre[-1].genre is None
    by_year_desc = cache_adapter.get_song_library(
        SongQuery(sort_column="year", sort_descending=True)
    )
    assert by_year_desc[0].year == 2019 and by_year_desc[-1].year == 1990

    # Presets.
    starred = cache_adapter.get_song_library(SongQuery(preset=SongQuery.Preset.STARRED))
    assert len(starred) == 500 and all(s.starred for s in starred)
    played = cache_adapter.get_song_library(
        SongQuery(
            preset=SongQuery.Preset.MOST_PLAYED, sort_column="play_count", sort_descending=True
        )
    )
    assert len(played) == 4000 and played[0].play_count == 4
    rated = cache_adapter.get_song_library(SongQuery(preset=SongQuery.Preset.TOP_RATED))
    assert all(s.user_rating for s in rated)
    years = cache_adapter.get_song_library(
        SongQuery(preset=SongQuery.Preset.YEAR_RANGE, year_range=(2000, 2001))
    )
    assert years and {s.year for s in years} == {2000, 2001}

    # Filtering.
    titled = cache_adapter.get_song_library(
        SongQuery(filter_text="song 0004", filter_column="title")
    )
    assert len(titled) == 10 and all(s.title.startswith("Song 0004") for s in titled)
    anywhere = cache_adapter.get_song_library(SongQuery(filter_text="artist 1"))
    assert len(anywhere) == 1667 and all(s.artist == "Artist 1" for s in anywhere)
    with pytest.raises(ValueError):
        cache_adapter.get_song_library(SongQuery(sort_column="nope"))


def test_song_library_resync_keeps_cached_files_and_drops_vanished(
    cache_adapter: FilesystemAdapter,
):
    songs = _library_songs(20)
    cache_adapter.ingest_new_data(KEYS.SONGS, None, songs)

    # Download one song "by hand".
    cache_adapter.ingest_new_data(KEYS.SONG_FILE, "tr-1", (None, str(MOCK_SONG_FILE), None))
    assert cache_adapter.get_cached_statuses(["tr-1"])["tr-1"] == SongCacheStatus.CACHED

    # Sync again: the song's title changed on the server, one song vanished.
    songs[1].title = "Renamed"
    del songs[5]
    cache_adapter.ingest_new_data(KEYS.SONGS, None, songs)

    library = {s.id: s for s in cache_adapter.get_song_library(SongQuery())}
    assert len(library) == 19 and "tr-5" not in library
    assert library["tr-1"].title == "Renamed"
    assert library["tr-1"].cache_status == SongCacheStatus.CACHED
    assert cache_adapter.get_cached_statuses(["tr-1"])["tr-1"] == SongCacheStatus.CACHED
    assert cache_adapter.get_song_file_uri("tr-1", "file").startswith("file://")


def test_song_library_migration_adds_columns(tmp_path: Path):
    first = FilesystemAdapter({}, tmp_path, is_cache=True)
    # Pretend the cache was created by an older version without the library columns.
    for column in ("created", "play_count", "size"):
        models.database.execute_sql(f"ALTER TABLE song DROP COLUMN {column}")
    first.shutdown()

    second = FilesystemAdapter({}, tmp_path, is_cache=True)
    columns = {c.name for c in models.database.get_columns("song")}
    assert {"created", "play_count", "size"} <= columns
    second.ingest_new_data(KEYS.SONGS, None, _library_songs(3))
    assert len(second.get_song_library(SongQuery())) == 3
    second.shutdown()


def test_song_starred_ingestion(cache_adapter: FilesystemAdapter):
    cache_adapter.ingest_new_data(KEYS.SONGS, None, _library_songs(3))
    assert cache_adapter.get_song_details("tr-1").starred is None

    cache_adapter.ingest_new_data(KEYS.SONG_STARRED, "tr-1", True)
    assert cache_adapter.get_song_details("tr-1").starred is not None
    starred = cache_adapter.get_song_library(SongQuery(preset=SongQuery.Preset.STARRED))
    assert {s.id for s in starred} == {"tr-0", "tr-1"}  # tr-0 is starred by _library_songs

    cache_adapter.ingest_new_data(KEYS.SONG_STARRED, "tr-1", False)
    assert cache_adapter.get_song_details("tr-1").starred is None


def test_song_played_ingestion_and_resync(cache_adapter: FilesystemAdapter):
    songs = _library_songs(3)
    for song in songs:
        song.play_count = None
        song.played = None
    cache_adapter.ingest_new_data(KEYS.SONGS, None, songs)

    cache_adapter.ingest_new_data(KEYS.SONG_PLAYED, "tr-1", None)
    cache_adapter.ingest_new_data(KEYS.SONG_PLAYED, "tr-1", None)
    played = cache_adapter.get_song_details("tr-1")
    assert played.play_count == 2 and played.played is not None

    # A server which doesn't report plays leaves the local count alone...
    cache_adapter.ingest_new_data(KEYS.SONGS, None, songs)
    assert cache_adapter.get_song_details("tr-1").play_count == 2
    # ...and one which does wins.
    songs[1].play_count = 7
    cache_adapter.ingest_new_data(KEYS.SONGS, None, songs)
    assert cache_adapter.get_song_details("tr-1").play_count == 7


def test_artist_index_flag_is_migrated(tmp_path: Path):
    first = FilesystemAdapter({}, tmp_path, is_cache=True)
    first.ingest_new_data(
        KEYS.ARTISTS,
        None,
        [SubsonicAPI.ArtistAndArtistInfo(id="1", name="indexed", album_count=3)],
    )
    first.ingest_new_data(KEYS.SONG, "1", MOCK_SUBSONIC_SONGS[1])  # adds artist "art2"
    models.database.execute_sql("ALTER TABLE artist DROP COLUMN in_index")
    first.shutdown()

    # An older cache: the flag is guessed from the album count until the next index
    # request, which the migration forces by invalidating the cached index.
    second = FilesystemAdapter({}, tmp_path, is_cache=True)
    with pytest.raises(CacheMissError) as e:
        second.get_artists()
    assert [a.id for a in cast(CacheMissError, e.value).partial_data] == ["1"]
    second.shutdown()


def test_album_list_bulk_ingestion(cache_adapter: FilesystemAdapter):
    # A fully ingested album keeps what the list doesn't provide.
    cache_adapter.ingest_new_data(
        KEYS.ALBUM,
        "a1",
        SubsonicAPI.Album(
            id="a1", name="Full", song_count=9, year=1999, _artist="X", artist_id="x"
        ),
    )
    query = AlbumSearchQuery(AlbumSearchQuery.Type.ALPHABETICAL_BY_NAME)
    albums = [SubsonicAPI.Album(id="a1", name="Full", _artist="X", artist_id="x", cover_art="c1")]
    albums += [
        SubsonicAPI.Album(
            id=f"b{i}",
            name=f"Album {i:04}",
            _artist="Y",
            artist_id="y",
            _genre="Pop",
            year=2000 + i % 20,
            cover_art=f"cb{i}",
        )
        for i in range(2000)
    ]
    albums.append(SubsonicAPI.Album(id=None, name="No id", _artist="Nameless", artist_id=None))
    start = time.time()
    cache_adapter.ingest_new_data(KEYS.ALBUMS, query.strhash(), albums)
    assert time.time() - start < 5, "the list must be ingested in bulk"

    result = list(cache_adapter.get_albums(query))
    assert len(result) == 2002
    assert [a.id for a in result[:2]] == ["a1", "b0"]
    assert (result[-1].id or "").startswith("invalid:")
    full = cache_adapter.get_album("a1")
    assert (full.song_count, full.year, full.cover_art) == (9, 1999, "c1")
    assert (artist := result[1].artist) and (genre := result[1].genre)
    assert (artist.name, genre.name, result[1].year) == ("Y", "Pop", 2000)


def test_empty_cached_cover_art_is_a_cache_miss(cache_adapter: FilesystemAdapter):
    # Before empty responses were rejected, an empty download could be cached as valid
    # cover art; it must not be served (the image loader can't do anything with it).
    cache_adapter.ingest_new_data(KEYS.COVER_ART_FILE, "pl_empty", MOCK_ALBUM_ART)
    cached = cache_adapter.get_cover_art_uri("pl_empty", "file", size=300)
    open(cached, "wb").close()

    with pytest.raises(CacheMissError):
        cache_adapter.get_cover_art_uri("pl_empty", "file", size=300)


def test_cached_album_songs_without_track_numbers(cache_adapter: FilesystemAdapter):
    # Untagged files have no track number; ordering the album's songs must not compare
    # None with an int (this crashed the album details after a song library sync).
    cache_adapter.ingest_new_data(
        KEYS.ALBUM,
        "a1",
        SubsonicAPI.Album(
            id="a1",
            name="foo",
            songs=[
                SubsonicAPI.Song("3", title="Three", _album="foo", album_id="a1", track=2),
                SubsonicAPI.Song("4", title="Untagged", _album="foo", album_id="a1"),
                SubsonicAPI.Song("5", title="One", _album="foo", album_id="a1", track=1),
            ],
        ),
    )

    album = cache_adapter.get_album("a1")
    assert [s.title for s in album.songs or []] == ["Untagged", "One", "Three"]
