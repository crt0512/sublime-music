import hashlib
import logging
import shutil
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple, cast

import peewee
from gi.repository import Gtk
from peewee import fn, prefetch
from playhouse.migrate import SqliteMigrator, migrate

from sublime_music.adapters import api_objects as API

from .. import (
    AlbumSearchQuery,
    CacheMissError,
    CachingAdapter,
    ConfigParamDescriptor,
    ConfigurationStore,
    ConfigureServerForm,
    LibrarySong,
    SongCacheStatus,
    SongQuery,
    UIInfo,
)
from . import models
from .sqlite_extensions import TzDateTimeField

KEYS = CachingAdapter.CachedDataKey


def _chunks(items: Sequence[Any], size: int = 500) -> Iterable[Sequence[Any]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


class FilesystemAdapter(CachingAdapter):
    """
    Defines an adapter which retrieves its data from the local filesystem.
    """

    # Configuration and Initialization Properties
    # ==================================================================================
    @staticmethod
    def get_ui_info() -> UIInfo:
        return UIInfo(
            name="Local Filesystem",
            description="Add a directory on your local filesystem",
            icon_basename="folder-music",
        )

    @staticmethod
    def get_configuration_form(config_store: ConfigurationStore) -> Gtk.Box:
        def verify_config_store() -> Dict[str, Optional[str]]:
            return {}

        return ConfigureServerForm(
            config_store,
            {
                "directory": ConfigParamDescriptor(
                    type=Path, description="Music Directory", pathtype="directory"
                )
            },
            verify_config_store,
        )

    @staticmethod
    def migrate_configuration(config_store: ConfigurationStore):
        pass

    def __init__(self, config: dict, data_directory: Path, is_cache: bool = False):
        self.data_directory = data_directory
        self.cover_art_dir = self.data_directory.joinpath("cover_art")
        self.music_dir = self.data_directory.joinpath("music")

        self.cover_art_dir.mkdir(parents=True, exist_ok=True)
        self.music_dir.mkdir(parents=True, exist_ok=True)

        self.is_cache = is_cache

        self.db_write_lock: threading.Lock = threading.Lock()
        database_filename = data_directory.joinpath("cache.db")
        models.database.init(str(database_filename))
        models.database.connect()

        with self.db_write_lock, models.database.atomic():
            models.database.create_tables(models.ALL_TABLES)
            self._migrate_db()

    def initial_sync(self):
        # TODO (#188) this is where scanning the fs should potentially happen?
        pass

    def shutdown(self):
        logging.info("Shutdown complete")

    # Database Migration
    # ==================================================================================
    def _migrate_db(self):
        # The cache has no schema versioning: add the columns which the models have and
        # an older cache lacks. Safe to run on every start.
        migrator = SqliteMigrator(models.database)
        added: Dict[str, Set[str]] = {}
        for model in models.ALL_TABLES:
            table = model._meta.table_name
            existing_columns = {column.name for column in models.database.get_columns(table)}
            missing = [
                field
                for field in model._meta.sorted_fields
                if field.column_name not in existing_columns
                and not isinstance(field, peewee.ForeignKeyField)
            ]
            if missing:
                logging.info(f"Adding {len(missing)} column(s) to the cached {table} table.")
                migrate(*(migrator.add_column(table, f.column_name, f) for f in missing))
                added[table] = {f.column_name for f in missing}

            indexed_columns = {
                tuple(index.columns) for index in models.database.get_indexes(table)
            }
            for field in model._meta.sorted_fields:
                if field.index and (field.column_name,) not in indexed_columns:
                    logging.info(f"Indexing {table}.{field.column_name} in the cache.")
                    migrate(migrator.add_index(table, (field.column_name,), False))

        if "in_index" in added.get("artist", ()):
            # Which artists the server lists is only known after the next index request:
            # guess from the album count meanwhile and make that request happen.
            models.Artist.update(in_index=True).where(
                models.Artist.album_count.is_null(False)
            ).execute()
            models.Artist.update(in_index=False).where(
                models.Artist.album_count.is_null()
            ).execute()
            models.CacheInfo.update(valid=False).where(
                models.CacheInfo.cache_key == KEYS.ARTISTS
            ).execute()

        # Directories cached before "-1" (gonic's "no parent") was normalised to "root".
        models.Directory.update(parent_id="root").where(
            models.Directory.parent_id == "-1"
        ).execute()

    # Usage and Availability Properties
    # ==================================================================================
    can_be_cached = False  # Can't be cached (there's no need).
    can_be_ground_truth = False  # TODO (#188)
    is_networked = False  # Doesn't access the network.

    def on_offline_mode_change(self, _: bool):
        pass

    # TODO (#200) make these dependent on cache state. Need to do this kinda efficiently
    can_get_cover_art_uri = True
    can_get_song_file_uri = True
    can_get_song_details = True
    can_get_song_rating = True
    can_get_artist = True
    can_get_albums = True
    can_get_album = True
    can_get_ignored_articles = True
    can_get_directory = True
    can_search = True

    def _can_get_key(self, cache_key: CachingAdapter.CachedDataKey) -> bool:
        if not self.is_cache:
            return True

        # As long as there's something in the cache (even if it's not valid) it may be
        # returned in a cache miss error.
        query = models.CacheInfo.select().where(models.CacheInfo.cache_key == cache_key)
        return query.count() > 0

    @property
    def can_get_playlists(self) -> bool:
        return self._can_get_key(KEYS.PLAYLISTS)

    @property
    def can_get_playlist_details(self) -> bool:
        return self._can_get_key(KEYS.PLAYLIST_DETAILS)

    @property
    def can_get_artists(self) -> bool:
        return self._can_get_key(KEYS.ARTISTS)

    @property
    def can_get_genres(self) -> bool:
        return self._can_get_key(KEYS.GENRES)

    @property
    def can_set_song_rating(self) -> bool:
        return self._can_get_key(KEYS.SONG_RATING)

    supported_schemes = ("file",)
    supported_artist_query_types = {
        AlbumSearchQuery.Type.RANDOM,
        AlbumSearchQuery.Type.NEWEST,
        AlbumSearchQuery.Type.FREQUENT,
        AlbumSearchQuery.Type.RECENT,
        AlbumSearchQuery.Type.STARRED,
        AlbumSearchQuery.Type.ALPHABETICAL_BY_NAME,
        AlbumSearchQuery.Type.ALPHABETICAL_BY_ARTIST,
        AlbumSearchQuery.Type.YEAR_RANGE,
        AlbumSearchQuery.Type.GENRE,
    }

    # Data Helper Methods
    # ==================================================================================
    def _get_list(
        self,
        model: Any,
        cache_key: CachingAdapter.CachedDataKey,
        ignore_cache_miss: bool = False,
        where_clauses: Tuple[Any, ...] | None = None,
        order_by: Any = None,
    ) -> Sequence:
        result = model.select()
        if where_clauses is not None:
            result = result.where(*where_clauses)

        if order_by:
            result = result.order_by(order_by)

        if self.is_cache and not ignore_cache_miss:
            # Determine if the adapter has ingested data for this key before, and if
            # not, cache miss.
            if not models.CacheInfo.get_or_none(
                models.CacheInfo.valid == True,  # noqa: 712
                models.CacheInfo.cache_key == cache_key,
            ):
                raise CacheMissError(partial_data=result)
        return result

    def _get_object_details(
        self, model: Any, id: str, cache_key: CachingAdapter.CachedDataKey
    ) -> Any:
        obj = model.get_or_none(model.id == id)

        # Handle the case that this is the ground truth adapter.
        if not self.is_cache:
            if not obj:
                raise Exception(f"{model} with id={id} does not exist")
            return obj

        # If we haven't ingested data for this item before, or it's been invalidated,
        # raise a CacheMissError with the partial data.
        cache_info = models.CacheInfo.get_or_none(
            models.CacheInfo.cache_key == cache_key,
            models.CacheInfo.parameter == id,
            models.CacheInfo.valid == True,  # noqa: 712
        )
        if not cache_info:
            raise CacheMissError(partial_data=obj)

        return obj

    def _compute_song_filename(self, cache_info: models.CacheInfo) -> Path:
        return self._song_filename(cache_info.path, cache_info.file_hash)

    def _song_filename(self, path_str: Optional[str], file_hash: Optional[str]) -> Path:
        try:
            if path_str:
                # Make sure that the path is somewhere in the cache directory and a
                # malicious server (or MITM attacker) isn't trying to override files in
                # other parts of the system.
                path = self.music_dir.joinpath(str(path_str))
                if self.music_dir in path.parents:
                    return path
        except Exception:
            pass

        # Fall back to using the song file hash as the filename. This shouldn't happen
        # with good servers, but just to be safe.
        return self.music_dir.joinpath(str(file_hash))

    # Data Retrieval Methods
    # ==================================================================================
    def get_cached_statuses(self, song_ids: Sequence[str]) -> Dict[str, SongCacheStatus]:
        def compute_song_cache_status(song: models.Song) -> SongCacheStatus:
            try:
                file = cast(models.CacheInfo, song.file)
                if self._compute_song_filename(file).exists():
                    if file.valid:
                        if file.cache_permanently:
                            return SongCacheStatus.PERMANENTLY_CACHED
                        return SongCacheStatus.CACHED

                    # The file is on disk, but marked as stale.
                    return SongCacheStatus.CACHED_STALE
            except Exception:
                pass

            return SongCacheStatus.NOT_CACHED

        cached_statuses = {song_id: SongCacheStatus.NOT_CACHED for song_id in song_ids}
        try:
            file_models = models.CacheInfo.select().where(
                models.CacheInfo.cache_key == KEYS.SONG_FILE
            )
            song_models = models.Song.select().where(models.Song.id.in_(song_ids))
            cached_statuses.update(
                {s.id: compute_song_cache_status(s) for s in prefetch(song_models, file_models)}
            )
        except Exception:
            pass

        return cached_statuses

    _playlists = None

    def get_playlists(self, ignore_cache_miss: bool = False) -> Sequence[API.Playlist]:
        if self._playlists is not None:
            return self._playlists

        self._playlists = self._get_list(
            models.Playlist,
            CachingAdapter.CachedDataKey.PLAYLISTS,
            ignore_cache_miss=ignore_cache_miss,
            order_by=fn.LOWER(models.Playlist.name),
        )
        return self._playlists

    def get_playlist_details(self, playlist_id: str) -> API.Playlist:
        return self._get_object_details(
            models.Playlist, playlist_id, CachingAdapter.CachedDataKey.PLAYLIST_DETAILS
        )

    def get_cover_art_uri(self, cover_art_id: str, scheme: str, size: int) -> str:
        cover_art = models.CacheInfo.get_or_none(
            models.CacheInfo.cache_key == CachingAdapter.CachedDataKey.COVER_ART_FILE,
            models.CacheInfo.parameter == cover_art_id,
        )
        if cover_art:
            filename = self.cover_art_dir.joinpath(str(cover_art.file_hash))
            # An empty file is a bad download cached before empty responses were
            # rejected, not cover art.
            if filename.exists() and filename.stat().st_size > 0:
                if cover_art.valid:
                    return str(filename)
                else:
                    raise CacheMissError(partial_data=str(filename))

        raise CacheMissError()

    def get_song_file_uri(self, song_id: str, schemes: Iterable[str]) -> str:
        song = models.Song.get_or_none(models.Song.id == song_id)
        if not song:
            if self.is_cache:
                raise CacheMissError()
            else:
                raise Exception(f"Song {song_id} does not exist.")

        try:
            song_file = cast(Any, song.file)
            if song_file and (filename := self._compute_song_filename(song_file)):
                if filename.exists():
                    file_uri = f"file://{filename}"
                    if song_file.valid:
                        return file_uri
                    else:
                        raise CacheMissError(partial_data=file_uri)
        except peewee.DoesNotExist:
            pass

        raise CacheMissError()

    def get_song_details(self, song_id: str) -> API.Song:
        return self._get_object_details(
            models.Song,
            song_id,
            CachingAdapter.CachedDataKey.SONG,
        )

    # Song Library
    # ==================================================================================
    _SONG_EXTRA_FIELDS = (
        "album_artist",
        "genres",
        "bit_rate",
        "suffix",
        "created",
        "played",
        "play_count",
    )

    def _song_library_cache_info(self) -> Optional[models.CacheInfo]:
        return models.CacheInfo.get_or_none(
            models.CacheInfo.cache_key == KEYS.SONGS, models.CacheInfo.parameter.is_null()
        )

    def song_library_last_synced(self) -> Optional[datetime]:
        cache_info = self._song_library_cache_info()
        return cache_info.last_ingestion_time if cache_info and cache_info.valid else None

    def get_song_library(self, query: SongQuery) -> Sequence[LibrarySong]:
        cache_info = self._song_library_cache_info()
        if not cache_info or not cache_info.valid:
            raise CacheMissError()

        Song, Album, Artist = models.Song, models.Album, models.Artist
        File = models.CacheInfo.alias()
        CoverArt = models.CacheInfo.alias()
        genre = fn.COALESCE(Song.genres, Song.genre)
        size = fn.COALESCE(File.size, Song._size)
        columns = {
            "title": Song.title,
            "artist": Artist.name,
            "album_artist": Song.album_artist,
            "album": Album.name,
            "genre": genre,
            "track": Song.track,
            "disc_number": Song.disc_number,
            "year": Song.year,
            "duration": Song.duration,
            "bit_rate": Song.bit_rate,
            "size": size,
            "suffix": Song.suffix,
            "created": Song.created,
            "played": Song.played,
            "play_count": Song.play_count,
            "user_rating": Song.user_rating,
            "starred": Song.starred,
        }
        text_columns = ("title", "artist", "album_artist", "album", "genre", "suffix")

        sql = (
            Song.select(
                Song.id.alias("id"),
                Song.artist.alias("artist_id"),
                Song.album.alias("album_id"),
                CoverArt.file_id.alias("cover_art"),
                File.valid.alias("file_valid"),
                File.cache_permanently.alias("file_permanent"),
                File.path.alias("file_path"),
                File.file_hash.alias("file_hash"),
                *(column.alias(name) for name, column in columns.items()),
            )
            .join(Album, peewee.JOIN.LEFT_OUTER)
            .switch(Song)
            .join(Artist, peewee.JOIN.LEFT_OUTER)
            .switch(Song)
            .join(File, peewee.JOIN.LEFT_OUTER, on=(Song.file == File.id))
            .switch(Song)
            .join(CoverArt, peewee.JOIN.LEFT_OUTER, on=(Song._cover_art == CoverArt.id))
        )

        Preset = SongQuery.Preset
        if query.preset == Preset.STARRED:
            sql = sql.where(Song.starred.is_null(False))
        elif query.preset == Preset.MOST_PLAYED:
            sql = sql.where(Song.play_count > 0)
        elif query.preset == Preset.TOP_RATED:
            sql = sql.where(Song.user_rating > 0)
        elif query.preset == Preset.YEAR_RANGE:
            sql = sql.where(Song.year.between(*query.year_range))

        if text := query.filter_text.strip():
            if query.filter_column == "any":
                sql = sql.where(
                    Song.title.contains(text)
                    | Artist.name.contains(text)
                    | Song.album_artist.contains(text)
                    | Album.name.contains(text)
                )
            elif query.filter_column in columns:
                sql = sql.where(columns[query.filter_column].contains(text))
            else:
                raise ValueError(f"Unknown filter column {query.filter_column!r}")

        if query.sort_column not in columns:
            raise ValueError(f"Unknown sort column {query.sort_column!r}")
        primary = columns[query.sort_column]
        if query.sort_column in text_columns:
            primary = primary.collate("NOCASE")
        sql = sql.order_by(
            # Songs without a value for the sort column always go last.
            columns[query.sort_column].is_null(),
            primary.desc() if query.sort_descending else primary.asc(),
            Artist.name.collate("NOCASE"),
            Album.name.collate("NOCASE"),
            Song.disc_number,
            Song.track,
            Song.title.collate("NOCASE"),
        )
        return [self._library_song(row) for row in sql.dicts()]

    def _library_song(self, row: Dict[str, Any]) -> LibrarySong:
        def as_timedelta(value: Any) -> Optional[timedelta]:
            if value is None or isinstance(value, timedelta):
                return value
            return timedelta(seconds=float(value))

        def as_datetime(value: Any) -> Optional[datetime]:
            if value is None or isinstance(value, datetime):
                return value
            return TzDateTimeField().python_value(value)

        # Same rules as get_cached_statuses: the file on disk decides, "valid" only says
        # whether it is stale. Every downloaded file gets a hash, so only songs with one
        # can be on disk; that saves a stat call per song for the rest of the library.
        cache_status = SongCacheStatus.NOT_CACHED
        if row["file_hash"] and self._song_filename(row["file_path"], row["file_hash"]).exists():
            if not row["file_valid"]:
                cache_status = SongCacheStatus.CACHED_STALE
            elif row["file_permanent"]:
                cache_status = SongCacheStatus.PERMANENTLY_CACHED
            else:
                cache_status = SongCacheStatus.CACHED

        return LibrarySong(
            id=row["id"],
            title=row["title"],
            artist=row["artist"],
            artist_id=row["artist_id"],
            album=row["album"],
            album_id=row["album_id"],
            album_artist=row["album_artist"],
            genre=row["genre"],
            track=row["track"],
            disc_number=row["disc_number"],
            year=row["year"],
            duration=as_timedelta(row["duration"]),
            bit_rate=row["bit_rate"],
            size=row["size"],
            suffix=row["suffix"],
            created=as_datetime(row["created"]),
            played=as_datetime(row["played"]),
            play_count=row["play_count"],
            user_rating=row["user_rating"],
            starred=as_datetime(row["starred"]),
            cover_art=row["cover_art"],
            cache_status=cache_status,
        )

    def _cache_info_ids(
        self, cache_key: CachingAdapter.CachedDataKey, parameters: Sequence[str]
    ) -> Dict[str, int]:
        """The row IDs of the cache infos with the given key and parameters."""
        ids: Dict[str, int] = {}
        for chunk in _chunks(parameters):
            infos = (
                models.CacheInfo.select(models.CacheInfo.id, models.CacheInfo.parameter)
                .where(
                    models.CacheInfo.cache_key == cache_key,
                    models.CacheInfo.parameter.in_(chunk),
                )
                .tuples()
            )
            for info_id, parameter in cast(Iterable[Tuple[int, str]], infos):
                ids[parameter] = info_id
        return ids

    def _cover_art_cache_info_ids(
        self, cover_art_ids: Sequence[str], now: datetime
    ) -> Dict[str, int]:
        """
        Makes sure every cover art ID has its bookkeeping row (like ingesting an item on
        its own does) and returns the row IDs by cover art ID.
        """
        cover_art_ids = list(dict.fromkeys(cover_art_ids))
        for chunk in _chunks(
            [
                {
                    "cache_key": KEYS.COVER_ART_FILE,
                    "parameter": cover_art,
                    "file_id": cover_art,
                    "valid": True,
                    "last_ingestion_time": now,
                }
                for cover_art in cover_art_ids
            ]
        ):
            models.CacheInfo.insert_many(chunk).on_conflict_ignore().execute()
        return self._cache_info_ids(KEYS.COVER_ART_FILE, cover_art_ids)

    def _ingest_album_list(self, albums: Sequence[API.Album]) -> List[str]:
        """
        Bulk version of ingesting each album of a list on its own, which takes seconds for
        thousands of albums. Like the per-album ingestion, a field the list doesn't provide
        keeps its cached value. Returns the album IDs in the list's order.
        """
        now = datetime.now()
        rows: Dict[str, Dict[str, Any]] = {}
        artists: Dict[str, Optional[str]] = {}
        genres: Set[str] = set()
        for album in albums:
            album_id = album.id or f"invalid:{self._strhash(album.name)}"
            artist_id = None
            if artist := album.artist:
                artist_id = artist.id or f"invalid:{self._strhash(artist.name)}"
                artists.setdefault(artist_id, artist.name)
            genre = album.genre
            genre_name = genre.name if genre and genre.name else None
            if genre_name:
                genres.add(genre_name)
            rows.setdefault(
                album_id,
                {
                    "id": album_id,
                    "name": album.name,
                    "created": album.created,
                    "duration": album.duration,
                    "play_count": album.play_count,
                    "song_count": album.song_count,
                    "starred": album.starred,
                    "year": album.year,
                    "genre": genre_name,
                    "artist": artist_id,
                    "cover_art": album.cover_art,
                },
            )

        for chunk in _chunks([{"id": id_, "name": name} for id_, name in artists.items()]):
            models.Artist.insert_many(chunk).on_conflict_ignore().execute()
        for chunk in _chunks([{"name": name} for name in genres]):
            models.Genre.insert_many(chunk).on_conflict_ignore().execute()
        cover_art_info_ids = self._cover_art_cache_info_ids(
            [row["cover_art"] for row in rows.values() if row["cover_art"]], now
        )
        for row in rows.values():
            row["_cover_art"] = cover_art_info_ids.get(row.pop("cover_art"))

        updated_fields = [f for f in models.Album._meta.sorted_fields if f.name != "id"]
        for chunk in _chunks(list(rows.values())):
            models.Album.insert_many(chunk).on_conflict(
                conflict_target=[models.Album.id],
                update={
                    field: fn.COALESCE(getattr(peewee.EXCLUDED, field.column_name), field)
                    for field in updated_fields
                },
            ).execute()
        return list(rows)

    def _ingest_song_library(self, songs: Sequence[API.Song]):
        """
        Mirrors the whole song library. Ingesting songs one by one takes minutes for a big
        library, so this bulk-inserts everything in a few statements per table. Songs
        already in the cache get their metadata updated and keep their cached files; songs
        which are no longer in the library are removed.
        """

        chunks = _chunks
        now = datetime.now()
        artists = {a.id: a.name for s in songs if (a := s.artist) and a.id}
        albums = {al.id: al.name for s in songs if (al := s.album) and al.id}
        genres = {g.name for s in songs if (g := s.genre) and g.name}
        cover_art_ids = [ca for ca in {s.cover_art for s in songs} if ca]

        # Related rows which don't exist yet. Existing rows may hold richer data (from a
        # full artist or album request), so those are left alone.
        for chunk in chunks(
            [{"id": id_, "name": name, "in_index": False} for id_, name in artists.items()]
        ):
            models.Artist.insert_many(chunk).on_conflict_ignore().execute()
        for chunk in chunks([{"id": id_, "name": name} for id_, name in albums.items()]):
            models.Album.insert_many(chunk).on_conflict_ignore().execute()
        for chunk in chunks([{"name": name} for name in genres]):
            models.Genre.insert_many(chunk).on_conflict_ignore().execute()
        # The cached-file bookkeeping rows which ingesting a song on its own creates too,
        # so that a song downloaded later is found (see _compute_song_filename).
        song_files = [(s.id, s.path, s.size) for s in songs if s.path]
        for chunk in chunks(
            [
                {
                    "cache_key": KEYS.SONG_FILE,
                    "parameter": song_id,
                    "file_id": song_id,
                    "path": path,
                    "size": size,
                    "valid": True,
                    "last_ingestion_time": now,
                }
                for song_id, path, size in song_files
            ]
        ):
            models.CacheInfo.insert_many(chunk).on_conflict(
                conflict_target=[models.CacheInfo.cache_key, models.CacheInfo.parameter],
                preserve=[models.CacheInfo.path, models.CacheInfo.size],
            ).execute()

        # A synced song carries everything a single song request would, so it counts as
        # cached song details: playing it must not cause a request per song.
        for chunk in chunks(
            [
                {
                    "cache_key": KEYS.SONG,
                    "parameter": s.id,
                    "valid": True,
                    "last_ingestion_time": now,
                }
                for s in songs
            ]
        ):
            models.CacheInfo.insert_many(chunk).on_conflict(
                conflict_target=[models.CacheInfo.cache_key, models.CacheInfo.parameter],
                preserve=[models.CacheInfo.valid, models.CacheInfo.last_ingestion_time],
            ).execute()

        cover_art_info_ids = self._cover_art_cache_info_ids(cover_art_ids, now)
        file_info_ids = self._cache_info_ids(
            KEYS.SONG_FILE, [song_id for song_id, _, _ in song_files]
        )

        rows = [
            {
                "id": s.id,
                "title": s.title,
                "duration": s.duration,
                "parent_id": s.parent_id,
                "album": al.id if (al := s.album) and al.id else None,
                "artist": ar.id if (ar := s.artist) and ar.id else None,
                "genre": g.name if (g := s.genre) and g.name else None,
                "_cover_art": cover_art_info_ids.get(s.cover_art) if s.cover_art else None,
                "track": s.track,
                "disc_number": s.disc_number,
                "year": s.year,
                "user_rating": s.user_rating,
                "starred": s.starred,
                "_size": s.size,
                "file": file_info_ids.get(s.id),
                **{extra: getattr(s, extra, None) for extra in self._SONG_EXTRA_FIELDS},
            }
            for s in songs
        ]
        # Update all the metadata. The link to the cached file is only ever set, never
        # cleared, so nothing already downloaded gets lost; and the play count and last
        # played time are only replaced when the server sends them, so that what this
        # client counted between syncs survives on servers which don't report them.
        keep_local = ("id", "file", "play_count", "played")
        updated_fields = [
            song_field
            for song_field in models.Song._meta.sorted_fields
            if song_field.name not in keep_local
        ]
        for chunk in chunks(rows):
            models.Song.insert_many(chunk).on_conflict(
                conflict_target=[models.Song.id],
                preserve=updated_fields,
                update={
                    models.Song.file: fn.COALESCE(peewee.EXCLUDED.file_id, models.Song.file),
                    models.Song.play_count: fn.COALESCE(
                        peewee.EXCLUDED.play_count, models.Song.play_count
                    ),
                    models.Song.played: fn.COALESCE(peewee.EXCLUDED.played, models.Song.played),
                },
            ).execute()

        fetched_ids = {s.id for s in songs}
        cached_ids = cast(Iterable[Tuple[str]], models.Song.select(models.Song.id).tuples())
        vanished = [song_id for (song_id,) in cached_ids if song_id not in fetched_ids]
        if vanished:
            logging.info(f"Removing {len(vanished)} song(s) which are no longer on the server.")
            playlist_songs = models.Playlist._songs.get_through_model()
            for chunk in chunks(vanished):
                for song_id in chunk:
                    self._do_delete_data(KEYS.SONG_FILE, song_id)
                playlist_songs.delete().where(playlist_songs.song.in_(chunk)).execute()
                models.Song.delete().where(models.Song.id.in_(chunk)).execute()

    def get_artists(self, ignore_cache_miss: bool = False) -> Sequence[API.Artist]:
        return self._get_list(
            models.Artist,
            CachingAdapter.CachedDataKey.ARTISTS,
            ignore_cache_miss=ignore_cache_miss,
            where_clauses=(
                ~(models.Artist.id.startswith("invalid:")),
                # Not in the index means known through songs or albums only.
                models.Artist.in_index.is_null() | (models.Artist.in_index == True),  # noqa: E712
            ),
        )

    def get_artist(self, artist_id: str) -> API.Artist:
        return self._get_object_details(
            models.Artist, artist_id, CachingAdapter.CachedDataKey.ARTIST
        )

    def get_albums(
        self,
        query: AlbumSearchQuery,
        sort_direction: str = "ascending",
        # TODO (#208) deal with sort dir here?
    ) -> Sequence[API.Album]:
        strhash = query.strhash()
        query_result = models.AlbumQueryResult.get_or_none(
            models.AlbumQueryResult.query_hash == strhash
        )
        # If we've cached the query result, then just return it. If it's stale, then
        # return the old value as a cache miss error.
        if query_result and (
            cache_info := models.CacheInfo.get_or_none(
                models.CacheInfo.cache_key == CachingAdapter.CachedDataKey.ALBUMS,
                models.CacheInfo.parameter == strhash,
            )
        ):
            if cache_info.valid:
                return query_result.albums
            else:
                raise CacheMissError(partial_data=query_result.albums)

        # If we haven't ever cached the query result, try to construct one, and return
        # it as a CacheMissError result.

        sql_query = models.Album.select().where(~(models.Album.id.startswith("invalid:")))

        Type = AlbumSearchQuery.Type
        if query.type == Type.GENRE:
            assert query.genre
        genre_name = genre.name if (genre := query.genre) else None

        ordered = {
            Type.RANDOM: sql_query.order_by(fn.Random()),
            Type.NEWEST: sql_query.order_by(models.Album.created.desc()),
            Type.FREQUENT: sql_query.order_by(models.Album.play_count.desc()),
            Type.STARRED: sql_query.where(models.Album.starred.is_null(False)).order_by(
                models.Album.name
            ),
            Type.ALPHABETICAL_BY_NAME: sql_query.order_by(models.Album.name),
            Type.ALPHABETICAL_BY_ARTIST: sql_query.order_by(models.Album.artist.name),
            Type.YEAR_RANGE: sql_query.where(
                models.Album.year.between(*query.year_range)
            ).order_by(models.Album.year, models.Album.name),
            Type.GENRE: sql_query.where(models.Album.genre == genre_name).order_by(
                models.Album.name
            ),
        }.get(query.type)

        raise CacheMissError(partial_data=ordered)

    def get_all_albums(self) -> Sequence[API.Album]:
        return self._get_list(
            models.Album,
            CachingAdapter.CachedDataKey.ALBUMS,
            ignore_cache_miss=True,
            where_clauses=(
                ~(models.Album.id.startswith("invalid:")),
                models.Album.artist.is_null(False),
            ),
        )

    def get_album(self, album_id: str) -> API.Album:
        return self._get_object_details(models.Album, album_id, CachingAdapter.CachedDataKey.ALBUM)

    def get_ignored_articles(self) -> Set[str]:
        return {
            i.name
            for i in self._get_list(
                models.IgnoredArticle, CachingAdapter.CachedDataKey.IGNORED_ARTICLES
            )
        }

    def get_directory(self, directory_id: str) -> API.Directory:
        return self._get_object_details(
            models.Directory, directory_id, CachingAdapter.CachedDataKey.DIRECTORY
        )

    def get_genres(self) -> Sequence[API.Genre]:
        return self._get_list(models.Genre, CachingAdapter.CachedDataKey.GENRES)

    def search(self, query: str) -> API.SearchResult:
        search_result = API.SearchResult(query)
        search_result.add_results("albums", self.get_all_albums())
        search_result.add_results("artists", self.get_artists(ignore_cache_miss=True))
        search_result.add_results(
            "songs",
            self._get_list(
                models.Song,
                CachingAdapter.CachedDataKey.SONG,
                ignore_cache_miss=True,
                where_clauses=(models.Song.artist.is_null(False),),
            ),
        )
        search_result.add_results(
            "playlists",
            self.get_playlists(ignore_cache_miss=True),
        )
        return search_result

    # Data Ingestion Methods
    # ==================================================================================
    def _strhash(self, string: str) -> str:
        return hashlib.sha1(bytes(string, "utf8")).hexdigest()

    def ingest_new_data(
        self,
        data_key: CachingAdapter.CachedDataKey,
        param: Optional[str],
        data: Any,
    ):
        assert self.is_cache, "FilesystemAdapter is not in cache mode!"

        # Wrap the actual ingestion function in a database lock, and an atomic
        # transaction.
        with self.db_write_lock, models.database.atomic():
            self._do_ingest_new_data(data_key, param, data)

    def invalidate_data(self, key: CachingAdapter.CachedDataKey, param: Optional[str]):
        assert self.is_cache, "FilesystemAdapter is not in cache mode!"

        # Wrap the actual ingestion function in a database lock, and an atomic
        # transaction.
        with self.db_write_lock, models.database.atomic():
            self._do_invalidate_data(key, param)

    def delete_data(self, key: CachingAdapter.CachedDataKey, param: Optional[str]):
        assert self.is_cache, "FilesystemAdapter is not in cache mode!"

        # Wrap the actual ingestion function in a database lock, and an atomic
        # transaction.
        with self.db_write_lock, models.database.atomic():
            self._do_delete_data(key, param)

    def _do_ingest_new_data(
        self,
        data_key: CachingAdapter.CachedDataKey,
        param: Optional[str],
        data: Any,
        partial: bool = False,
    ) -> Any:
        # TODO (#201): this entire function is not exactly efficient due to the nested
        # dependencies and everything. I'm not sure how to improve it, and I'm not sure
        # if it needs improving at this point.
        logging.debug(f"_do_ingest_new_data param={param} data_key={data_key} data={data}")

        def getattrs(obj: Any, keys: Iterable[str]) -> Dict[str, Any]:
            return {k: getattr(obj, k) for k in keys}

        def setattrs(obj: Any, data: Dict[str, Any]):
            for k, v in data.items():
                if v is not None:
                    setattr(obj, k, v)

        def compute_file_hash(filename: str) -> str:
            file_hash = hashlib.sha1()
            with open(filename, "rb") as f:
                while chunk := f.read(8192):
                    file_hash.update(chunk)

            return file_hash.hexdigest()

        return_val = None

        # Set the cache info.
        now = datetime.now()
        cache_info, cache_info_created = models.CacheInfo.get_or_create(
            cache_key=(
                # In the case of SONG_FILE_PERMANENT, we have to use SONG_FILE as the
                # key in the database so everything matches up when querying.
                data_key
                if data_key != KEYS.SONG_FILE_PERMANENT
                else KEYS.SONG_FILE
            ),
            parameter=param,
            defaults={
                "cache_key": data_key,
                "parameter": param,
                "last_ingestion_time": now,
                # If it's partial data, then set it to be invalid so it will only be
                # used in the event that the ground truth adapter can't service the
                # request.
                "valid": not partial,
            },
        )
        if not cache_info_created:
            cache_info.valid = cache_info.valid or not partial
            cache_info.last_ingestion_time = now
            cache_info.save()

        if data_key == KEYS.ALBUM:
            album = cast(API.Album, data)
            album_id = album.id or f"invalid:{self._strhash(album.name)}"
            album_data = {
                "id": album_id,
                **getattrs(
                    album,
                    [
                        "name",
                        "created",
                        "duration",
                        "play_count",
                        "song_count",
                        "starred",
                        "year",
                    ],
                ),
                "genre": (
                    self._do_ingest_new_data(KEYS.GENRE, None, g) if (g := album.genre) else None
                ),
                "artist": (
                    self._do_ingest_new_data(KEYS.ARTIST, ar.id, ar, partial=True)
                    if (ar := album.artist)
                    else None
                ),
                "_songs": (
                    [self._do_ingest_new_data(KEYS.SONG, s.id, s) for s in album.songs or []]
                    if not partial
                    else None
                ),
                "_cover_art": (
                    self._do_ingest_new_data(KEYS.COVER_ART_FILE, album.cover_art, data=None)
                    if album.cover_art
                    else None
                ),
            }

            db_album, created = models.Album.get_or_create(id=album_id, defaults=album_data)

            if not created:
                setattrs(db_album, album_data)
                db_album.save()

            return_val = db_album

        elif data_key == KEYS.ALBUMS:
            album_ids = self._ingest_album_list(cast(Sequence[API.Album], data))
            album_query_result, _ = models.AlbumQueryResult.get_or_create(
                query_hash=param, defaults={"query_hash": param}
            )
            album_query_result.albums = album_ids

        elif data_key == KEYS.ARTIST:
            # Ingest similar artists.
            artist = cast(API.Artist, data)
            if artist.similar_artists:
                models.SimilarArtist.delete().where(
                    models.SimilarArtist.similar_artist.not_in(
                        [sa.id for sa in artist.similar_artists or []]
                    ),
                    models.Artist == artist.id,
                ).execute()
                models.SimilarArtist.insert_many(
                    [
                        {"artist": artist.id, "similar_artist": a.id, "order": i}
                        for i, a in enumerate(artist.similar_artists or [])
                    ]
                ).on_conflict_replace().execute()

            artist_id = artist.id or f"invalid:{self._strhash(artist.name)}"
            artist_data = {
                "id": artist_id,
                **getattrs(
                    artist,
                    [
                        "name",
                        "album_count",
                        "starred",
                        "biography",
                        "music_brainz_id",
                        "last_fm_url",
                    ],
                ),
                "albums": [
                    self._do_ingest_new_data(KEYS.ALBUM, a.id, a, partial=True)
                    for a in artist.albums or []
                ],
                "_artist_image_url": (
                    self._do_ingest_new_data(
                        KEYS.COVER_ART_FILE, artist.artist_image_url, data=None
                    )
                    if artist.artist_image_url
                    else None
                ),
            }

            db_artist, created = models.Artist.get_or_create(id=artist_id, defaults=artist_data)

            if not created:
                setattrs(db_artist, artist_data)
                db_artist.save()

            return_val = db_artist

        elif data_key == KEYS.ARTISTS:
            for a in data:
                self._do_ingest_new_data(KEYS.ARTIST, a.id, a, partial=True)
            # Only the artists in the index are listed in the Artists tab; the index
            # request settles it for every cached artist.
            index_ids = [a.id for a in data]
            models.Artist.update(in_index=False).execute()
            for i in range(0, len(index_ids), 500):
                models.Artist.update(in_index=True).where(
                    models.Artist.id.in_(index_ids[i : i + 500])
                ).execute()
            # Remove the artists which are no longer on the server, but keep the ones
            # that cached songs or albums still refer to. Servers which only index album
            # artists never list the track artists, and deleting those would leave the
            # songs with dangling references.
            models.Artist.delete().where(
                models.Artist.id.not_in([a.id for a in data])
                & ~models.Artist.id.startswith("invalid")
                & models.Artist.id.not_in(
                    models.Song.select(models.Song.artist).where(models.Song.artist.is_null(False))
                )
                & models.Artist.id.not_in(
                    models.Album.select(models.Album.artist).where(
                        models.Album.artist.is_null(False)
                    )
                )
            ).execute()

        elif data_key == KEYS.COVER_ART_FILE:
            cache_info.file_id = param

            if data is not None:
                file_hash = compute_file_hash(data)
                cache_info.file_hash = file_hash

                # Copy the actual cover art file
                shutil.copy(str(data), str(self.cover_art_dir.joinpath(file_hash)))

        elif data_key == KEYS.DIRECTORY:
            api_directory = cast(API.Directory, data)
            directory_data: Dict[str, Any] = getattrs(api_directory, ["id", "name", "parent_id"])

            if not partial:
                directory_data["directory_children"] = []
                directory_data["song_children"] = []
                for c in api_directory.children:
                    if hasattr(c, "children"):  # directory
                        directory_data["directory_children"].append(
                            self._do_ingest_new_data(KEYS.DIRECTORY, c.id, c, partial=True)
                        )
                    else:
                        directory_data["song_children"].append(
                            self._do_ingest_new_data(KEYS.SONG, c.id, c)
                        )

            directory, created = models.Directory.get_or_create(
                id=api_directory.id, defaults=directory_data
            )

            if not created:
                setattrs(directory, directory_data)
                directory.save()

            return_val = directory

        elif data_key == KEYS.GENRES:
            for g in data:
                self._do_ingest_new_data(KEYS.GENRE, None, g)

        elif data_key == KEYS.GENRE:
            api_genre = cast(API.Genre, data)
            genre_data = getattrs(api_genre, ["name", "song_count", "album_count"])
            genre, created = models.Genre.get_or_create(name=api_genre.name, defaults=genre_data)

            if not created:
                setattrs(genre, genre_data)
                genre.save()

            return_val = genre

        elif data_key == KEYS.IGNORED_ARTICLES:
            models.IgnoredArticle.insert_many(
                {"name": s} for s in data
            ).on_conflict_replace().execute()
            models.IgnoredArticle.delete().where(models.IgnoredArticle.name.not_in(data)).execute()

        elif data_key == KEYS.PLAYLIST_DETAILS:
            api_playlist = cast(API.Playlist, data)
            playlist_data: Dict[str, Any] = {
                **getattrs(
                    api_playlist,
                    [
                        "id",
                        "name",
                        "song_count",
                        "duration",
                        "created",
                        "changed",
                        "comment",
                        "owner",
                        "public",
                    ],
                ),
                "_cover_art": (
                    self._do_ingest_new_data(KEYS.COVER_ART_FILE, api_playlist.cover_art, None)
                    if api_playlist.cover_art
                    else None
                ),
            }

            if not partial:
                # If it's partial, then don't ingest the songs.
                playlist_data["_songs"] = [
                    self._do_ingest_new_data(KEYS.SONG, s.id, s) for s in api_playlist.songs
                ]

            playlist, playlist_created = models.Playlist.get_or_create(
                id=playlist_data["id"], defaults=playlist_data
            )

            # Update the values if the playlist already existed.
            if not playlist_created:
                setattrs(playlist, playlist_data)
                playlist.save()

            return_val = playlist

        elif data_key == KEYS.PLAYLISTS:
            self._playlists = None
            for p in data:
                self._do_ingest_new_data(KEYS.PLAYLIST_DETAILS, p.id, p, partial=True)
            models.Playlist.delete().where(
                models.Playlist.id.not_in([p.id for p in data])
            ).execute()

        elif data_key == KEYS.SEARCH_RESULTS:
            data = cast(API.SearchResult, data)
            for a in data._artists.values():
                self._do_ingest_new_data(KEYS.ARTIST, a.id, a, partial=True)

            for a in data._albums.values():
                self._do_ingest_new_data(KEYS.ALBUM, a.id, a, partial=True)

            for s in data._songs.values():
                self._do_ingest_new_data(KEYS.SONG, s.id, s, partial=True)

            for p in data._playlists.values():
                self._do_ingest_new_data(KEYS.PLAYLIST_DETAILS, p.id, p, partial=True)

        elif data_key == KEYS.SONG:
            api_song = cast(API.Song, data)
            song_data = getattrs(
                api_song,
                [
                    "id",
                    "title",
                    "track",
                    "year",
                    "duration",
                    "parent_id",
                    "disc_number",
                    "user_rating",
                    "starred",
                ],
            )
            for extra in self._SONG_EXTRA_FIELDS:
                song_data[extra] = getattr(api_song, extra, None)
            song_data["_size"] = api_song.size
            song_data["genre"] = (
                self._do_ingest_new_data(KEYS.GENRE, None, g) if (g := api_song.genre) else None
            )
            song_data["artist"] = (
                self._do_ingest_new_data(KEYS.ARTIST, ar.id, ar, partial=True)
                if (ar := api_song.artist)
                else None
            )
            song_data["album"] = (
                self._do_ingest_new_data(KEYS.ALBUM, al.id, al, partial=True)
                if (al := api_song.album)
                else None
            )
            song_data["_cover_art"] = (
                self._do_ingest_new_data(
                    KEYS.COVER_ART_FILE,
                    api_song.cover_art,
                    data=None,
                )
                if api_song.cover_art
                else None
            )
            song_data["file"] = (
                self._do_ingest_new_data(
                    KEYS.SONG_FILE,
                    api_song.id,
                    data=(api_song.path, None, api_song.size),
                )
                if api_song.path
                else None
            )

            song, created = models.Song.get_or_create(id=song_data["id"], defaults=song_data)

            if not created:
                setattrs(song, song_data)
                song.save()

            return_val = song

        elif data_key == KEYS.SONGS:
            self._ingest_song_library(cast(Sequence[API.Song], data))

        elif data_key == KEYS.SONG_FILE:
            cache_info.file_id = param

        elif data_key == KEYS.SONG_FILE_PERMANENT:
            cache_info.cache_permanently = True

        # Special handling for Song
        if data_key == KEYS.SONG_FILE and data:
            path, buffer_filename, size = data

            if path:
                cache_info.path = path

            if size:
                cache_info.size = size

            if buffer_filename:
                cache_info.file_hash = compute_file_hash(buffer_filename)

                # Copy the actual song file from the download buffer dir to the cache
                # dir.
                filename = self._compute_song_filename(cache_info)
                filename.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(str(buffer_filename), str(filename))

        elif data_key == KEYS.SONG_RATING:
            song = models.Song.get_by_id(param)
            song.user_rating = data
            song.save()

        elif data_key == KEYS.SONG_STARRED:
            song = models.Song.get_by_id(param)
            song.starred = datetime.now().astimezone() if data else None
            song.save()

        elif data_key == KEYS.SONG_PLAYED:
            if song := models.Song.get_or_none(models.Song.id == param):
                song.play_count = (song.play_count or 0) + 1
                song.played = datetime.now().astimezone()
                song.save()

        cache_info.save()
        return return_val if return_val is not None else cache_info

    def _do_invalidate_data(
        self,
        data_key: CachingAdapter.CachedDataKey,
        param: Optional[str],
    ):
        logging.debug(f"_do_invalidate_data param={param} data_key={data_key}")
        models.CacheInfo.update({"valid": False}).where(
            models.CacheInfo.cache_key == data_key, models.CacheInfo.parameter == param
        ).execute()

        if data_key == KEYS.ALBUM:
            # Invalidate the corresponding cover art.
            if album := models.Album.get_or_none(models.Album.id == param):
                self._do_invalidate_data(KEYS.COVER_ART_FILE, album.cover_art)

        elif data_key == KEYS.ARTIST:
            # Invalidate the corresponding cover art and albums.
            if artist := models.Artist.get_or_none(models.Artist.id == param):
                self._do_invalidate_data(KEYS.COVER_ART_FILE, artist.artist_image_url)
                for album in models.Album.select().where(models.Album.artist == artist):
                    self._do_invalidate_data(CachingAdapter.CachedDataKey.ALBUM, album.id)

        elif data_key == KEYS.PLAYLIST_DETAILS:
            # Invalidate the corresponding cover art.
            if playlist := models.Playlist.get_or_none(models.Playlist.id == param):
                self._do_invalidate_data(KEYS.COVER_ART_FILE, playlist.cover_art)

        elif data_key == KEYS.SONG_FILE:
            # Invalidate the corresponding cover art.
            if song := models.Song.get_or_none(models.Song.id == param):
                self._do_invalidate_data(KEYS.COVER_ART_FILE, song.cover_art)

    def _do_delete_data(self, data_key: CachingAdapter.CachedDataKey, param: Optional[str]):
        logging.debug(f"_do_delete_data param={param} data_key={data_key}")
        cache_info = models.CacheInfo.get_or_none(
            models.CacheInfo.cache_key == data_key,
            models.CacheInfo.parameter == param,
        )

        if data_key == KEYS.COVER_ART_FILE:
            if cache_info:
                self.cover_art_dir.joinpath(str(cache_info.file_hash)).unlink(missing_ok=True)
                # The file is gone: never offer it as stale data, even if a copy lingers.
                cache_info.file_hash = None

        elif data_key == KEYS.PLAYLIST_DETAILS:
            # Delete the playlist and corresponding cover art.
            if playlist := models.Playlist.get_or_none(models.Playlist.id == param):
                if cover_art := playlist.cover_art:
                    self._do_delete_data(KEYS.COVER_ART_FILE, cover_art)

                playlist.delete_instance()

        elif data_key == KEYS.SONG_FILE:
            if cache_info:
                self._compute_song_filename(cache_info).unlink(missing_ok=True)

        elif data_key == KEYS.ALL_SONGS:
            shutil.rmtree(str(self.music_dir))
            shutil.rmtree(str(self.cover_art_dir))
            self.music_dir.mkdir(parents=True, exist_ok=True)
            self.cover_art_dir.mkdir(parents=True, exist_ok=True)

            models.CacheInfo.update({"valid": False}).where(
                models.CacheInfo.cache_key == KEYS.SONG_FILE
            ).execute()
            models.CacheInfo.update({"valid": False}).where(
                models.CacheInfo.cache_key == KEYS.COVER_ART_FILE
            ).execute()

        elif data_key == KEYS.EVERYTHING:
            self._do_delete_data(KEYS.ALL_SONGS, None)
            for table in models.ALL_TABLES:
                table.truncate_table()

        if cache_info:
            cache_info.valid = False
            cache_info.save()
