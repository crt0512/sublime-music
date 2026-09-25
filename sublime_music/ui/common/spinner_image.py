import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from gi.repository import GdkPixbuf, GLib, Gtk

# Decodes images for set_from_file_async.
_decoder = ThreadPoolExecutor(max_workers=2, thread_name_prefix="image-decode")


class SpinnerImage(Gtk.Overlay):
    _decode_token: Optional[object] = None

    def __init__(
        self,
        loading: bool = True,
        image_name: str | None = None,
        spinner_name: str | None = None,
        image_size: int | None = None,
        **kwargs,
    ):
        """An image with a loading overlay."""
        Gtk.Overlay.__init__(self)
        self.image_size = image_size
        self.filename: Optional[str] = None

        self.image = Gtk.Image(name=image_name, **kwargs)
        self.add(self.image)

        self.spinner = Gtk.Spinner(
            name=spinner_name,
            active=loading,
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
        )
        self.add_overlay(self.spinner)

    def set_from_file_async(self, filename: Optional[str]):
        """
        Like set_from_file, but decodes the image on a worker thread, so that creating
        many of these at once (a batch of album tiles) doesn't hold up the interface.
        """
        if not filename or self.image_size is None:
            self.set_from_file(filename)
            return
        self.filename = filename
        self._decode_token = token = object()
        size = self.image_size

        def apply(pixbuf: GdkPixbuf.Pixbuf) -> bool:
            if self._decode_token is token:  # not replaced by another image since
                self.image.set_from_pixbuf(pixbuf)
            return False

        def decode():
            try:
                pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(filename, size, size, True)
            except Exception as e:
                logging.warning(f"could not load {filename}. Probably not an image. {e}")
                return
            GLib.idle_add(apply, pixbuf)

        _decoder.submit(decode)

    def set_from_file(self, filename: Optional[str]):
        """Set the image to the given filename."""
        self._decode_token = None  # an image still being decoded must not replace this
        if filename == "":
            filename = None
        self.filename = filename
        if self.image_size is not None and filename:
            try:
                pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                    filename, self.image_size, self.image_size, True
                )
                self.image.set_from_pixbuf(pixbuf)
            except Exception as e:
                logging.warn(f"could not load {filename}. Probably not an image. {e}")
        else:
            self.image.set_from_file(filename)

    def set_loading(self, loading_status: bool):
        if loading_status:
            self.spinner.start()
            self.spinner.show()
        else:
            self.spinner.stop()
            self.spinner.hide()

    def set_image_size(self, size: int):
        self.image_size = size
        self.set_from_file(self.filename)
