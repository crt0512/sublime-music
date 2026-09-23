import pickle

from sublime_music.adapters import SongQuery
from sublime_music.ui.state import UIState


def test_state_pickles_the_song_tab_settings():
    state = UIState()
    state.song_query = SongQuery(
        preset=SongQuery.Preset.YEAR_RANGE,
        sort_column="title",
        filter_text="x",
        year_range=(1990, 1999),
    )
    state.song_columns = ["title", "artist"]

    loaded = pickle.loads(pickle.dumps(state))
    loaded.migrate()
    assert loaded.song_query == state.song_query
    assert loaded.song_columns == ["title", "artist"]


def test_state_from_before_the_song_tab_gets_defaults():
    # A state file written by an older version has no song fields at all.
    state = UIState()
    old = state.__getstate__()
    del old["song_query"]
    del old["song_columns"]
    loaded = UIState.__new__(UIState)
    loaded.__setstate__(old)
    loaded.migrate()
    assert loaded.song_query == SongQuery()
    assert loaded.song_columns is None
