# Sublime Music (Plus)

![Sublime Music Logo](logo/logo.png)

Sublime Music is a native, GTK3, Subsonic client for the Linux Desktop.

> Sublime Music has reached End of Maintanance for quiet a while already (See original [Maintainers Post](http://sumnerevans.com/posts/projects/sublime-music-eom)).
>
> This fork tries to remedy some of the issues that derived from that aswell as trying to add some additional features ;) All changes were only tested using my Gonic OpenSubsonic Server 
> Should also work with Navidrome and others (if they dont return all songs on empty search3 string they might be a bit slower though)
---

[![The Albums tab of Sublime Music with the Play Queue opened.](docs/_static/screenshots/albums.png)](docs/_static/screenshots/play-queue.png)

The Albums tab of Sublime Music with the Play Queue opened.

## Features and Optimizations

### New Features :

- The Songs tab: the whole library in one sortable, filterable table.
- Clear Cue Button in Cue
- Cue Replacement Warnings

### Improvements and Bugfixes :

- Better/Fixed Caching
- Playlists not loading fixed
- God knows how many other issues I stoped counting
- Bugfixes on things I broke myself or that felt off.
- Many many performance related fixes and improvements.


### Existing Features

- Switch between multiple Subsonic API (v1.8.0+) compliant servers.
- Play music through Chromecast devices on the same LAN.
- Offline Mode where Sublime Music will not make any network requests.
- DBus MPRIS interface integration for controlling Sublime Music via clients such as `playerctl`, `i3status-rust`, KDE Connect, and many commonly used desktop environments.
- Browse songs by the sever reported filesystem structure, or view them organized by ID3 tags in the Albums, Artists, and Playlists views.
- Intuitive play queue.
- Create/delete/edit playlists.
- Download songs for offline listening.

### Removed Features

- ci/cd (didnt feel like it)

## Installation

Sublime Music (Plus) is built and installed with the `Makefile` in the repository root.
You need `python3` (3.10 or newer), `pip`, GNU make and, at runtime, GTK3 with PyGObject
and libmpv. On Debian/Ubuntu:

    sudo apt install python3 python3-pip python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-notify-0.7 libmpv2

### Debian package (recommended)

    make deb
    sudo apt install ./dist/sublime-music_*_bundled.deb

The default package bundles the Python dependencies (downloaded with pip while building),
so distribution updates cannot break the app; only Python, PyGObject/GTK and libmpv come
from the system. `make deb BUNDLE=0` builds a thin package that depends on Debian's
`python3-*` packages instead (smaller, more moving parts; install it with
`--no-install-recommends` if apt wants to pull in half the archive).

### Without a package

    make stage            # as your user, so build/ stays yours
    sudo make install     # copies the staged tree under /usr/local
    sudo make uninstall   # removes it again

`PREFIX=...` changes the location and `BUNDLE=0` gives the thin flavour, for both.

### Running from the source tree

    make run                    # same as PYTHONPATH=. python3 -m sublime_music
    make run ARGS="-m debug"    # with debug logging

### macOS

`make pkg` builds `Sublime Music.app` and a `.pkg` installer; run it on a Mac with the
Xcode command line tools. GTK is not bundled: install `python@3 pygobject3 gtk+3
adwaita-icon-theme mpv` with Homebrew first. This target has not been tested yet.

### Other targets

`make build` (wheel into `dist/`), `make test`, `make lint`, `make format`, `make venv`
(a `.venv` with the dev tools on top of the system PyGObject) and `make clean`.
`make help` lists everything.

---


## Disclaimers on things

### Change of Build system/Method

Upstream built with flit and pip-tools, published to PyPI through GitHub Actions and
shipped a Nix flake. The fork is built by the root `Makefile` described above.
`pyproject.toml` still declares the metadata and dependencies (so `pip install .` keeps
working), while the pip-tools lock files, the pre-commit config, the Nix flake and the
GitHub workflows were removed. The Sphinx docs in `docs/` can still be built locally with
`make -C docs html`; they are just not published anywhere.

### Code
Because of my "cant write code in tabbed language" disability, which stems from having the "i write ugly but functional code" syndrome (rust fmt my beloved thanks for existing), most of the code changes and comments were heavily assisted by LLMs, Code has been inspected as best as I can with my rust/php coding skills, tests only inspected briefly, comments might be a bit slop like in certain places.

## Motivations, Credits and Thank You's

All credits to previous maintainer, this app is a true banger thats truly irreplacable to me, especially since its not plagued by one of these problems alternatives come with :
- Literally being a Web-App that doesnt integrate into my Desktop Theme at all
- Using an UI Framework that doesnt seem to like any sort of none Wayland Scaling