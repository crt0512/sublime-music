# Sublime Music (Plus)

![Sublime Music Logo](logo/logo.png)

Sublime Music is a native, GTK3, Subsonic client for the Linux Desktop.

> Sublime Music has reached End of Maintanance for quiet a while already (See original [Maintainers Post](http://sumnerevans.com/posts/projects/sublime-music-eom)).
>
> This fork tries to remedy some of the issues that derived from that aswell as trying to add some additional features ;) All changes were only tested using my Gonic OpenSubsonic Server 
> Should also work with Navidrome and others (if they dont return all songs on empty search3 string they might be a bit slower though)
---

[![The Albums tab of Sublime Music with the Play Queue opened.](docs/_static/screenshots/old/albums.png)](docs/_static/screenshots/old/play-queue.png)

The Albums tab of Sublime Music with the Play Queue opened.

## Features and Optimizations

### New Features :

- The Songs tab: the whole library in one sortable, filterable table.
- Clear Cue Button in Cue
- Cue Replacement Warnings
- Keyboard media key grabber so your browser doesnt steal media keys from you (on GNOME based stuff and MacOS, rest can still be stolen by your browser unfortunately)
- Makefile and port to MacOS

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
- Browse songs by the sever reported filesystem structure, or view them organized by ID3 tags in the Albums, Artists, and Playlists views.
- Intuitive play queue.
- Create/delete/edit playlists.
- Download songs for offline listening.

### Removed Features

- ci/cd (didnt feel like it)

## Building and Installing

See [BUILD.md](BUILD.md)

### Change of Build system/Method

Upstream used to build with flit and pip-tools, published to PyPI through GitHub Actions and shipped a Nix flake.

This fork is built by the `Makefile` found in the projectroot,
The pre-commit config, Nix flake and the GitHub workflows were removed.
The Sphinx docs in `docs/` can still be built locally if you really need to

I know the makefile stuff looks like a mess and it partially is but it helped me run Sublime music on an arm64 based Linux Cash Register that I use as a Music Hub now.

### Code
Because of my "cant write code in tabbed language" disability, which stems from having the "i write ugly but functional code" syndrome (rust fmt my beloved thanks for existing), most of the code changes and comments were heavily assisted by LLMs.

Code has been inspected as best as I can with my rust/php coding skills, tests only inspected briefly, comments might be a bit slop aswell as in docs that were updated by them without asking.

## Motivations, Credits and Thank You's

All credits to previous maintainer, this app is a true banger thats truly irreplacable to me, especially since its not plagued by one of these problems alternatives come with :
- Literally being a Web-App that doesnt integrate into my Desktop Theme at all
- Using an UI Framework that doesnt seem to like any sort of none Wayland Scaling