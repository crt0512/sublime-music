# How to Build and Install Sublime Music (Plus)

Sublime Music (Plus) is built and installed with the `Makefile` in the repository root, there are no prebuilt packages or exeutables as I've decided to ditch the CI/CD Build system in favor of a Makefile (as I have quiet weird requirements interms of deployment)

## Linux

You need `python3` (3.10 or newer), `pip`, GNU make and at runtime GTK3 with PyGObject and libmpv for building nomatter what Linux you're on.


### Installation via deb Package
This is kinda the way I'd recommend installing it.


1. Install build/runtime dependencies :
```bash
sudo apt install python3 python3-pip python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-notify-0.7 libmpv2`
```
2. Build and install the app
```bash
make deb
sudo apt install ./dist/sublime-music_*_bundled.deb
```
- The deb file should be installable and runnable on most distros that ship the same glibc or newer
    - If you want to save some space and have used bundle 1 or 2 you can technically uninstall these packages again :
    `python3 python3-pip python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-notify-0.7 libmpv2`
    - The ones that are still going to be needed for running the app only should be pulled by the deb file automatically in most cases, if the deb file seems to pull unnecessarily many packages on your distro use apt with `--no-install-recommends` and see if the app still runs. On most distros ive tested it ran fine without them.

### Installation without a package

1. Make sure build/runtime deps are met for your plattform
2. Cobble together everthing to install/run
```bash
make stage
```
3. Install the app rawdog style
```bash
sudo make install
```


- To uninstall rawdog syle
```bash
sudo make uninstall
```

`PREFIX=...` changes the location where it would be installed.

### Running from the the files only.

1. Same as all above, deps and stuff.
2. Run normally
```bash
make run
```
3. Run with debugging
```bash
make run ARGS="-m debug"
```

## macOS
The guide covers both MacOS on aarch64 (M-Series) and MacOS on x86-64-v3 (Intel), If you are planing on building it on an Intel MacBook Pro make sure you have around 45 Minutes of spare time available and a charger nearby.

- **You will need the following before even starting:**
   -  [**Xcodes Command Line Utilities**](https://developer.apple.com/documentation/xcode/installing-the-command-line-tools#Install-the-Command-Line-Tools-package-in-Terminal) installed.
   -  Either [**Homebre**](https://brew.sh) or [**MacPorts**](https://github.com/macports/macports-base/releases/tag/v2.12.6) installed.
- Once installed continue on with installing the Build/Runtime dependencies for your package manager

For Homebrew (M-Series usually) :
```bash
brew install python@3 pygobject3 gtk+3 gobject-introspection adwaita-icon-theme librsvg mpv fontconfig
```
-> fyi at the time of writing this (2026-09-23) Notifications from Sublime Music are broken on Homebrew builds (dont care enough to investigate why, if you need them use MacPorts)

For MacPorts (Intel usually) :
1. Make MacPorts not use X11 for everything (it loves doing that)
```bash
echo "-x11 +quartz" | sudo tee -a /opt/local/etc/macports/variants.conf
```

1. Install the build/runtime depenencies (takes around 45 Minutes on my MacBook Pro 2017)
```bash
sudo port install python313 py313-pip py313-gobject3 gtk3 gobject-introspection adwaita-icon-theme librsvg mpv +libmpv fontconfig
```

- After the build/runtime dependencies are installed :
`make pkg`
- After which you'll find a runnable app in the build folder and an installable pkg in the dist folder

### Troubleshootin on Mac :
If your app doesnt launch after building on x86-64-v3 (Intel) just in case try installing [Xquartz](https://www.xquartz.org) and try opening the App after installing that, if it launches with that installed, If it does you managed to miss the "making MacPorts not use X11 for everything" step (like I did at first too), to fix do this in order :
```bash
sudo port -n upgrade --enforce-variants glib2 +quartz -x11
sudo port -n upgrade --enforce-variants libepoxy +quartz -x11
sudo port -n upgrade --enforce-variants gtk3 +quartz -x11
sudo port uninstall inactive
sudo port -n upgrade --enforce-variants cairo +quartz -x11
sudo port -n upgrade --enforce-variants pango +quartz -x11
sudo port -n upgrade --enforce-variants py313-cairo +quartz -x11
sudo port uninstall inactive
make clean
make pkg
```
Then install the newly generated pkg file and the app should run.

If you use `make pkg BUNDLE=0` it builds a thin app that uses the Homebrew/MacPorts Python and GTK instead of bundling it in.

## Bundles Explained
Normally installing Sublime Music requires alot of dependencies via the Systems Package manager. This is something that is not always available or feasable, For example I cant expect someone on an Intel Mac to wait 45 Minutes during which their CPU reaches temparatures hot enough to Cook with or if you want to run this somewhat portably on a platform where you either dont have a package manager or cant modify the root partition.

Therefore the makefile allows bundling them in, did I just reinvent what is basically an AppImage or a Flatpak? Yes but because i dont have libfuse2 available of one of my targets I've built this monstrosity.

Anyways theres Three levels of how fat you want your app to get which you choose with `BUNDLE=`:

| `make <target> BUNDLE=`     | what is inside the package                                                         | rough install size |
|-----------------------------|------------------------------------------------------------------------------------|--------------------|
| `2`, *full*                 | bundles literally as much as is at the limit of sane (dont use unless you need to) | 300 ish MB         |
| `1`, *bundled*, **default** | Basic minimum needed for running on a Mac without homebrew/macports                | 42 MB              |
| `0`, *thin*                 | only the apps python, rest needs to installed through your systems packageman.     | 1.5 MB             |

TLDR:
- Want to be Storage efficient with the risk of system updates breaking the app? Use `0`
- Want to run on Mac or have smaller change of system updates breaking the app? Use `1` or dont define the BUNDLE flag at all as thats the default
- Need to run it on something weird with very little system packages available and no way to install the runtime deps.? Use `2`