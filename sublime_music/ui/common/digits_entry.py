from typing import Callable, Optional

from gi.repository import Gtk


class DigitsEntry(Gtk.Entry, Gtk.Editable):
    """
    A :class:`Gtk.Entry` which only accepts digits. ``is_allowed`` can restrict the input
    further: it is called with the text the entry would contain after an insertion, and
    the insertion is dropped if it returns ``False``.

    The filtering implements :class:`Gtk.Editable`'s ``insert_text`` instead of connecting
    to the ``insert-text`` signal: PyGObject cannot marshal that signal's in/out
    ``position`` argument, so every handler connected to it makes GLib log a
    ``g_value_get_int`` assertion failure.
    """

    def __init__(self, is_allowed: Optional[Callable[[str], bool]] = None, **kwargs):
        # Set before the GObject is initialised: a ``text`` keyword argument already
        # inserts text, which calls ``do_insert_text``.
        self.is_allowed = is_allowed
        super().__init__(**kwargs)

    def do_insert_text(self, new_text: str, length: int, position: int) -> int:
        current = self.get_text()
        candidate = current[:position] + new_text + current[position:]
        if not new_text.isdigit() or (self.is_allowed and not self.is_allowed(candidate)):
            return position
        self.get_buffer().insert_text(position, new_text, length)
        return position + length
