from typing import List, Tuple

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")

from gi.repository import Gio, GObject, Gtk  # noqa: E402

from sublime_music.ui import util  # noqa: E402


class _Model(GObject.GObject):
    id = GObject.Property(type=str)
    name = GObject.Property(type=str)

    def __init__(self, id: str, name: str):
        GObject.GObject.__init__(self)
        self.id = id
        self.name = name


def _model_store(*models: Tuple[str, str]) -> Gio.ListStore:
    store = Gio.ListStore()
    for id_, name in models:
        store.append(_Model(id_, name))
    return store


def _model_rows(store: Gio.ListStore) -> List[Tuple[str, str]]:
    return [(model.id, model.name) for model in store]


def test_diff_model_store_keeps_store_when_nothing_changed():
    store = _model_store(("1", "a"), ("2", "b"))
    original_models = store[:]

    # The models in the store have a different reference count and different wrapper
    # objects than the freshly created ones. Only the property values must matter.
    util.diff_model_store(store, [_Model("1", "a"), _Model("2", "b")])

    assert len(store) == 2
    assert all(model is original for model, original in zip(store, original_models))


def test_diff_model_store_replaces_store_when_changed():
    store = _model_store(("1", "a"), ("2", "b"))

    util.diff_model_store(store, [_Model("1", "a"), _Model("2", "bb"), _Model("3", "c")])
    assert _model_rows(store) == [("1", "a"), ("2", "bb"), ("3", "c")]

    util.diff_model_store(store, [_Model("3", "c")])
    assert _model_rows(store) == [("3", "c")]

    util.diff_model_store(store, [])
    assert _model_rows(store) == []

    util.diff_model_store(store, [_Model("4", "d")])
    assert _model_rows(store) == [("4", "d")]


class _SignalCounter:
    def __init__(self, store: Gtk.ListStore):
        self.changed = self.inserted = self.deleted = 0
        store.connect("row-changed", lambda *_: self._count("changed"))
        store.connect("row-inserted", lambda *_: self._count("inserted"))
        store.connect("row-deleted", lambda *_: self._count("deleted"))

    def _count(self, name: str):
        setattr(self, name, getattr(self, name) + 1)


def _song_store(*rows: List) -> Gtk.ListStore:
    store = Gtk.ListStore(bool, str, str)
    for row in rows:
        store.append(row)
    return store


def _song_rows(store: Gtk.ListStore) -> List[List]:
    return [row[:] for row in store]


def test_diff_song_store_edits_changed_cells_in_place():
    store = _song_store([True, "", "one"], [True, "", "two"], [False, "", "three"])
    counter = _SignalCounter(store)

    util.diff_song_store(
        store,
        [
            [True, "folder-download-symbolic", "one"],
            [True, "", "two"],
            [True, "view-pin-symbolic", "three"],
        ],
    )

    assert _song_rows(store) == [
        [True, "folder-download-symbolic", "one"],
        [True, "", "two"],
        [True, "view-pin-symbolic", "three"],
    ]
    assert (counter.changed, counter.inserted, counter.deleted) == (3, 0, 0)


def test_diff_song_store_removes_trailing_rows():
    store = _song_store([True, "", "one"], [True, "", "two"], [False, "", "three"])
    counter = _SignalCounter(store)

    util.diff_song_store(store, [[True, "", "one"]])

    assert _song_rows(store) == [[True, "", "one"]]
    assert (counter.changed, counter.inserted, counter.deleted) == (0, 0, 2)


def test_diff_song_store_appends_new_rows():
    store = _song_store([True, "", "one"])
    counter = _SignalCounter(store)

    # Tuples are used for the rows of some stores (for example the genre combo box).
    util.diff_song_store(store, [(True, "", "one"), (False, "", "two"), (True, "x", "three")])

    assert _song_rows(store) == [[True, "", "one"], [False, "", "two"], [True, "x", "three"]]
    assert (counter.changed, counter.inserted, counter.deleted) == (0, 2, 0)


def test_diff_song_store_from_empty_and_to_empty():
    store = _song_store()

    util.diff_song_store(store, [[True, "", "one"], [False, "", "two"]])
    assert _song_rows(store) == [[True, "", "one"], [False, "", "two"]]

    util.diff_song_store(store, [])
    assert _song_rows(store) == []
