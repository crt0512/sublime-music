"""
Runs first inside the frozen bundle (a PyInstaller runtime hook).

PyInstaller's own hooks already point GI, GLib, GTK and gdk-pixbuf at the bundle. This
adds fontconfig on macOS (so that pango finds the system fonts), keeps the host's data
directories visible on Linux (its icon and GTK themes), and lets python-mpv, which looks
libmpv up by name, find the bundled copy.
"""
import ctypes.util
import os
import sys

BUNDLE = sys._MEIPASS

fonts = os.path.join(BUNDLE, "etc", "fonts")
if os.path.isdir(fonts):
    os.environ.setdefault("FONTCONFIG_PATH", fonts)
    os.environ.setdefault("FONTCONFIG_FILE", os.path.join(fonts, "fonts.conf"))

if sys.platform != "darwin":
    # The desktop's themes and icons live here; a hook of PyInstaller's prepends the
    # bundle's own share directory, and would leave only that if nothing was set.
    data_dirs = [d for d in os.environ.get("XDG_DATA_DIRS", "").split(":") if d]
    for system_dir in ("/usr/local/share", "/usr/share"):
        if system_dir not in data_dirs:
            data_dirs.append(system_dir)
    os.environ["XDG_DATA_DIRS"] = ":".join(data_dirs)
    # Desktops ask GTK to load their own modules (Cinnamon: xapp-gtk3-module); they are
    # not in the bundle and the bundled GTK should not load the host's, so don't try.
    os.environ.pop("GTK_MODULES", None)

_find_library = ctypes.util.find_library


def find_library(name):
    if name == "mpv":
        for candidate in ("libmpv.2.dylib", "libmpv.dylib", "libmpv.so.2", "libmpv.so"):
            path = os.path.join(BUNDLE, candidate)
            if os.path.exists(path):
                return path
    return _find_library(name)


ctypes.util.find_library = find_library
