# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for the self-contained builds: the Python interpreter, GTK, PyGObject,
libmpv (with ffmpeg) and the Python dependencies all end up inside the bundle, so only
the base system libraries (glibc, X11, OpenGL) are needed on the machine that runs it.

  Linux:  `make deb BUNDLE=2` (or `make stage BUNDLE=2`) freezes into build/pyi/ and
          packages that directory.
  macOS:  `make pkg` freezes into "Sublime Music.app", with Homebrew providing the build
          inputs; see packaging/macos/README.md.

PyInstaller's own hooks do the GTK work: they collect the typelibs and the libraries they
name, rewrite the typelibs' library paths for the bundle, and point GI, GLib, GTK and
gdk-pixbuf at the bundle at start-up. runtime_hook.py adds fontconfig (macOS), libmpv and
the host's data directories.
"""
import glob
import os
import re
import subprocess
import sys

from PyInstaller.utils.hooks import collect_data_files

MACOS = sys.platform == "darwin"
ROOT = os.path.abspath(os.path.join(SPECPATH, "..", ".."))  # noqa: F821 (PyInstaller global)
with open(os.path.join(ROOT, "sublime_music", "__init__.py")) as f:
    VERSION = re.search(r'^__version__ = "(.*)"', f.read(), re.M).group(1)
ICNS = os.path.join(ROOT, "build", "sublime-music.icns")


def find_libmpv():
    """The libmpv shared library to bundle; python-mpv loads it with ctypes."""
    if MACOS:
        brew = os.environ.get("HOMEBREW_PREFIX") or subprocess.run(
            ["brew", "--prefix"], capture_output=True, text=True, check=True
        ).stdout.strip()
        candidates = [os.path.join(brew, "lib", "libmpv.2.dylib")]
        hint = "brew install mpv"
    else:
        candidates = glob.glob("/usr/lib/*/libmpv.so.2") + glob.glob("/usr/lib*/libmpv.so.2")
        hint = "apt install libmpv2"
    for path in candidates:
        if os.path.exists(path):
            return path
    raise SystemExit(f"libmpv not found ({hint})")


datas = collect_data_files("sublime_music")  # icons, CSS, API specs, MPRIS XML
if MACOS:
    # fontconfig's configuration: without it pango finds no fonts and text renders as
    # boxes. Linux hosts have their own /etc/fonts.
    brew = os.path.dirname(os.path.dirname(find_libmpv()))
    datas.append((os.path.join(brew, "etc", "fonts"), "etc/fonts"))

# PyInstaller follows libmpv's dependencies (ffmpeg, libass, ...) and makes them load
# from inside the bundle.
binaries = [(find_libmpv(), ".")]

a = Analysis(  # noqa: F821
    [os.path.join(SPECPATH, "entry.py")],  # noqa: F821
    binaries=binaries,
    datas=datas,
    hiddenimports=(
        ["keyring.backends.macOS"]
        if MACOS
        else ["keyring.backends.SecretService", "keyring.backends.kwallet"]
    )
    + ["keyring.backends.chainer", "keyring.backends.fail"],
    hooksconfig={
        "gi": {
            "module-versions": {"Gtk": "3.0"},
            # macOS has no icon theme of its own, so the bundle carries Adwaita; a Linux
            # desktop always has one (and its theme), found through XDG_DATA_DIRS.
            "icons": ["Adwaita", "hicolor"] if MACOS else [],
            "themes": ["Default"] if MACOS else [],
            "languages": ["en_US"],
        }
    },
    runtime_hooks=[os.path.join(SPECPATH, "runtime_hook.py")],  # noqa: F821
    excludes=["tkinter", "_tkinter", "FixTk", "tcl", "tk"],
)
pyz = PYZ(a.pure)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="sublime-music",
    console=False,
    icon=ICNS if MACOS else None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="sublime-music")  # noqa: F821

if MACOS:
    app = BUNDLE(  # noqa: F821
        coll,
        name="Sublime Music.app",
        icon=ICNS,
        bundle_identifier="app.sublimemusic.SublimeMusic",
        version=VERSION,
        info_plist={
            "CFBundleName": "Sublime Music",
            "CFBundleDisplayName": "Sublime Music",
            "CFBundleShortVersionString": VERSION,
            "LSMinimumSystemVersion": "12.0",
            "LSApplicationCategoryType": "public.app-category.music",
            "NSHighResolutionCapable": True,
            "NSRequiresAquaSystemAppearance": False,
            # Chromecast discovery uses mDNS; without these macOS silently blocks it.
            "NSLocalNetworkUsageDescription": (
                "Sublime Music looks for Chromecast devices on the local network."
            ),
            "NSBonjourServices": ["_googlecast._tcp"],
        },
    )
