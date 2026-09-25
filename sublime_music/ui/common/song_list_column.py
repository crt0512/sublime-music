from gi.repository import Gtk, Pango


class SongListColumn(Gtk.TreeViewColumn):
    def __init__(
        self,
        header: str,
        text_idx: int,
        bold: bool = False,
        align: float = 0,
        width: int | None = None,
        weight_idx: int | None = None,
    ):
        """
        Represents a column in a song list.

        :param weight_idx: a model column with the Pango weight for each row, instead of
            a fixed weight from ``bold``.
        """
        renderer = Gtk.CellRendererText(
            xalign=align,
            weight=Pango.Weight.BOLD if bold else Pango.Weight.NORMAL,
            ellipsize=Pango.EllipsizeMode.END,
        )
        renderer.set_fixed_size(width or -1, 35)

        super().__init__(header, renderer, text=text_idx, sensitive=0)
        if weight_idx is not None:
            self.add_attribute(renderer, "weight", weight_idx)
        self.set_resizable(True)
        self.set_expand(not width)
