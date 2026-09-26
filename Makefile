# Sublime Music (Plus) build system.
#
# Run `make help` for the list of targets. The usual flow on Debian/Ubuntu is
#
#     make deb                # dist/sublime-music_<version>_<arch>_bundled.deb
#     sudo dpkg -i dist/sublime-music*.deb
#
# or, without a package,
#
#     make stage              # as your user, so build/ stays yours
#     sudo make install       # copies the staged tree under /usr/local
#     sudo make uninstall
#
# Dependency flavours (BUNDLE=1 is the default):
#   BUNDLE=2  full: a self-contained build made with PyInstaller. Python, GTK,
#             PyGObject, libmpv (with ffmpeg) and the Python dependencies are all
#             inside the package; only glibc, X11 and OpenGL come from the system.
#             Big (a few hundred MB installed), but literally nothing on the system can break it.
#   BUNDLE=1  bundled: the Python dependencies are downloaded with pip and shipped inside the package.
#             Python itself, PyGObject/GTK and libmpv come from the system.
#   BUNDLE=0  thin: nothing is bundled, the package depends on the distribution's
#             python3-* packages instead (see THIN_DEPENDS below).
#
# Variables you may want to override: PYTHON, PREFIX (default /usr/local for
# install, always /usr for deb), DESTDIR, BUNDLE, MAINTAINER, MACOS_PKGMGR,
# MACOS_PREFIX.

# macOS: where GTK, PyGObject and libmpv come from, Homebrew (brew --prefix) or
# MacPorts (port's prefix). MACOS_PKGMGR picks which one: brew, macports, or auto
# (default) to prefer whichever is installed, trying Homebrew first (MacPorts'
# packages are sometimes newer/more stable, so set MACOS_PKGMGR=macports to build
# against those instead when both are installed). MACOS_PREFIX overrides the
# resulting prefix directly and skips this detection. MACOS_PYTHON is the first
# python3 in that prefix that can import PyGObject (MacPorts has no bare python3,
# only python3.13 and so on).
ifeq ($(shell uname),Darwin)
MACOS_PKGMGR ?= auto
MACOS_BREW_PREFIX     := $(shell brew --prefix 2>/dev/null)
MACOS_MACPORTS_PREFIX := $(patsubst %/bin/port,%,$(shell command -v port 2>/dev/null))
ifeq ($(MACOS_PKGMGR),brew)
MACOS_PREFIX ?= $(MACOS_BREW_PREFIX)
else ifeq ($(MACOS_PKGMGR),macports)
MACOS_PREFIX ?= $(MACOS_MACPORTS_PREFIX)
else
MACOS_PREFIX ?= $(or $(MACOS_BREW_PREFIX),$(MACOS_MACPORTS_PREFIX))
endif
MACOS_PYTHON := $(shell for p in $(MACOS_PREFIX)/bin/python3 $(MACOS_PREFIX)/bin/python3.[0-9]*; do \
    case $$p in (*-config) continue;; esac; \
    [ -x $$p ] && $$p -c 'import gi' 2>/dev/null && { echo $$p; break; }; done)
endif

PYTHON  ?= $(or $(MACOS_PYTHON),python3)
PREFIX  ?= /usr/local
DESTDIR ?=
BUNDLE  ?= 1

NAME     := sublime-music
PACKAGE  := sublime_music
APP_ID   := app.sublimemusic.SublimeMusic
VERSION  := $(shell sed -n 's/^__version__ = "\(.*\)"/\1/p' $(PACKAGE)/__init__.py)
FLAVOUR  := $(if $(filter 0,$(BUNDLE)),thin,$(if $(filter 2,$(BUNDLE)),full,bundled))
DEB_ARCH := $(shell dpkg-architecture -qDEB_HOST_ARCH 2>/dev/null || uname -m)
PY_MINOR := $(shell $(PYTHON) -c 'import sys; print("%d.%d" % sys.version_info[:2])')
PY_NEXT  := $(shell $(PYTHON) -c 'import sys; print("%d.%d" % (sys.version_info[0], sys.version_info[1] + 1))')
MAINTAINER ?= $(shell git config user.name 2>/dev/null || echo "Sublime Music (Plus)") <$(shell git config user.email 2>/dev/null || echo "noreply@localhost")>

BUILD      := build
VENDOR     := $(BUILD)/vendor
STAGE_ROOT ?= $(BUILD)/stage-$(FLAVOUR)
DEB_ROOT   := $(BUILD)/deb-$(FLAVOUR)
WHEEL      := dist/$(PACKAGE)-$(VERSION)-py3-none-any.whl
VENV       := .venv
PYI_VENV   := $(BUILD)/pyinstaller-venv
PYI_DIST   := $(BUILD)/pyi
PYI_SPEC   := packaging/pyinstaller/$(NAME).spec
PYI_FILES  := $(PYI_SPEC) packaging/pyinstaller/entry.py packaging/pyinstaller/runtime_hook.py packaging/pyinstaller/gi_typelib_fix.py
# $(call TOOL,name): the tool from .venv when it exists, otherwise whatever is on PATH.
TOOL        = $(if $(wildcard $(VENV)/bin/$(1)),$(VENV)/bin/$(1),$(1))
# The interpreter for run/test: .venv's python when it exists, otherwise $(PYTHON)
# (macOS has no bare `python`, only python3).
VENV_PYTHON = $(if $(wildcard $(VENV)/bin/python),$(VENV)/bin/python,$(PYTHON))

LIBDIR     := $(PREFIX)/lib/$(NAME)
ICON_SIZES := 16 22 24 32 36 48 64 72 96 128 192 512

# The runtime dependencies from pyproject.toml, minus PyGObject (it is compiled
# against the system's GTK, so it always comes from the system: python3-gi on
# Debian, pygobject3 from Homebrew on macOS). Keep in sync with pyproject.toml.
VENDOR_DEPS := bleach bottle dataclasses-json peewee pychromecast python-dateutil mpv requests semver thefuzz keyring
# macOS only: PyObjC for Now Playing / media keys and the in-process appearance check.
# Installed into both .venv and the PyInstaller venv so `make run` matches the bundle.
MACOS_DEPS := $(if $(filter Darwin,$(shell uname)),pyobjc-framework-MediaPlayer,)

# What both flavours need from the system.
SYSTEM_DEPENDS := python3-gi, python3-gi-cairo, gir1.2-gtk-3.0, gir1.2-glib-2.0, libmpv2
RECOMMENDS     := gir1.2-notify-0.7, gir1.2-nm-1.0
# The thin flavour gets everything from Debian's packages.
THIN_DEPENDS   := python3 (>= 3.10), $(SYSTEM_DEPENDS), python3-mpv, python3-peewee, python3-dataclasses-json, python3-requests, python3-dateutil, python3-semver, python3-bottle, python3-bleach, python3-thefuzz, python3-pychromecast
THIN_RECOMMENDS := $(RECOMMENDS), python3-keyring

SOURCES := $(shell find $(PACKAGE) -type f -not -path '*/__pycache__/*')

.PHONY: help build vendor vendor-lock freeze stage install uninstall deb pkg run test lint format venv clean distclean

help: ## Show this help
	@echo "Sublime Music (Plus) $(VERSION), flavour: $(FLAVOUR) (BUNDLE=$(BUNDLE))"
	@echo
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | sed 's/:.*## /|/' | sort | awk -F'|' '{printf "  %-12s %s\n", $$1, $$2}'
	@echo
	@echo "Variables: PYTHON=$(PYTHON) PREFIX=$(PREFIX) DESTDIR=$(DESTDIR) BUNDLE=$(BUNDLE)$(if $(filter Darwin,$(shell uname)), MACOS_PKGMGR=$(MACOS_PKGMGR) MACOS_PREFIX=$(MACOS_PREFIX))"

# ----------------------------------------------------------------------------
# Building
# ----------------------------------------------------------------------------

build: $(WHEEL) ## Build the Python wheel into dist/

$(WHEEL): pyproject.toml README.md LICENSE $(SOURCES)
	$(PYTHON) -m pip wheel --no-deps --wheel-dir dist .

vendor: $(VENDOR)/.stamp ## Download the bundled Python dependencies into build/vendor

# With the lock file every build bundles the same versions; `make vendor-lock` refreshes
# it deliberately. Without one, whatever pip resolves today is bundled.
VENDOR_LOCK := packaging/vendor-requirements.txt
VENDOR_SPEC  = $(if $(wildcard $(VENDOR_LOCK)),-r $(VENDOR_LOCK),--upgrade $(VENDOR_DEPS))

$(VENDOR)/.stamp: Makefile $(wildcard $(VENDOR_LOCK))
	rm -rf $(VENDOR)
	$(PYTHON) -m pip install --target $(VENDOR) --no-compile $(VENDOR_SPEC)
	rm -rf $(VENDOR)/bin
	touch $@

vendor-lock: ## Refresh packaging/vendor-requirements.txt with the newest versions of the bundled dependencies
	rm -rf $(BUILD)/vendor-lock
	$(PYTHON) -m pip install --target $(BUILD)/vendor-lock --no-compile --upgrade $(VENDOR_DEPS)
	$(PYTHON) -m pip freeze --path $(BUILD)/vendor-lock > $(VENDOR_LOCK)
	rm -rf $(BUILD)/vendor-lock
	@echo "Locked $$(grep -c . $(VENDOR_LOCK)) packages in $(VENDOR_LOCK); run the tests, then commit it."

# ----------------------------------------------------------------------------
# Self-contained build with PyInstaller (the "full" flavour, and the macOS bundle)
# ----------------------------------------------------------------------------

# On macOS the venv is made with Homebrew's/MacPorts' Python, the one its PyGObject is
# built for; it sees that PyGObject through the system site-packages and gets
# everything else (the app's dependencies at their locked versions, PyInstaller)
# installed into itself.
PYI_PYTHON := $(or $(MACOS_PYTHON),$(PYTHON))

$(PYI_VENV)/.stamp: pyproject.toml $(wildcard $(VENDOR_LOCK))
	rm -rf $(PYI_VENV)
	$(PYI_PYTHON) -m venv --system-site-packages $(PYI_VENV)
	$(PYI_VENV)/bin/pip install --upgrade pip
	$(PYI_VENV)/bin/pip install --ignore-installed $(VENDOR_SPEC) $(MACOS_DEPS) "pyinstaller >=6, <7"
	@$(PYI_VENV)/bin/python -c 'import gi; gi.require_version("Gtk", "3.0"); from gi.repository import Gtk' \
	    || { echo "PyGObject with GTK 3 is not available to $(PYI_PYTHON) (python3-gi on Debian, pygobject3 from Homebrew, py3xx-gobject3 from MacPorts)"; exit 1; }
	touch $@

freeze: $(PYI_DIST)/.stamp ## Freeze the app with PyInstaller into build/pyi/sublime-music (what BUNDLE=2 packages)

$(PYI_DIST)/.stamp: $(PYI_VENV)/.stamp $(SOURCES) $(PYI_FILES)
	$(PYI_VENV)/bin/pip install --no-deps --force-reinstall --quiet .
	rm -rf $(PYI_DIST)
	$(PYI_VENV)/bin/pyinstaller --noconfirm --clean --distpath $(PYI_DIST) --workpath $(BUILD)/pyi-work $(PYI_SPEC)
	touch $@

# The installed tree, assembled under $(STAGE_ROOT)$(PREFIX). `install` copies it
# to $(DESTDIR)$(PREFIX) and `deb` wraps it into a package.
stage: $(STAGE_ROOT).stamp ## Assemble the installed tree under build/ (respects PREFIX and BUNDLE)

# The stamp lives next to the tree, not inside it, so that it never ends up in a package.
$(STAGE_ROOT).stamp: Makefile $(if $(filter bundled,$(FLAVOUR)),$(VENDOR)/.stamp) $(if $(filter full,$(FLAVOUR)),$(PYI_DIST)/.stamp) $(SOURCES) packaging/launcher.sh.in packaging/launcher-full.sh.in packaging/debian/lintian-overrides packaging/$(NAME).1.in packaging/debian/copyright packaging/$(NAME).desktop $(NAME).metainfo.xml LICENSE README.md CHANGELOG.rst
	rm -rf $(STAGE_ROOT)
	install -d $(STAGE_ROOT)$(LIBDIR) $(STAGE_ROOT)$(PREFIX)/bin
ifeq ($(FLAVOUR),full)
	cp -r $(PYI_DIST)/$(NAME) $(STAGE_ROOT)$(LIBDIR)/
	sed -e 's|@LIBDIR@|$(LIBDIR)|g' packaging/launcher-full.sh.in > $(STAGE_ROOT)$(PREFIX)/bin/$(NAME)
else
	cp -r $(PACKAGE) $(STAGE_ROOT)$(LIBDIR)/
	find $(STAGE_ROOT)$(LIBDIR) -name __pycache__ -type d -prune -exec rm -rf {} +
	sed -e 's|@LIBDIR@|$(LIBDIR)|g' -e 's|@PYTHON@|python3|g' packaging/launcher.sh.in > $(STAGE_ROOT)$(PREFIX)/bin/$(NAME)
endif
ifeq ($(FLAVOUR),bundled)
	cp -r $(VENDOR) $(STAGE_ROOT)$(LIBDIR)/vendor
	@# Drop what is not needed at runtime: the stamp, peewee's pwiz tool, shell scripts.
	rm -f $(STAGE_ROOT)$(LIBDIR)/vendor/.stamp $(STAGE_ROOT)$(LIBDIR)/vendor/pwiz.py
	find $(STAGE_ROOT)$(LIBDIR)/vendor -name '*.sh' -delete
	@# Shebang lines in library modules only confuse packaging checks.
	find $(STAGE_ROOT)$(LIBDIR)/vendor -name '*.py' -exec sed -i '1{/^#!/d}' {} +
	@# Wheels ship unstripped extension modules; strip them when we can.
	if command -v strip >/dev/null 2>&1; then \
	    find $(STAGE_ROOT)$(LIBDIR)/vendor -name '*.so' -exec strip --strip-unneeded {} + ; \
	fi
endif
	chmod 755 $(STAGE_ROOT)$(PREFIX)/bin/$(NAME)
	install -Dm644 packaging/$(NAME).desktop -t $(STAGE_ROOT)$(PREFIX)/share/applications
	@# AppStream wants the metainfo file named after the component id.
	install -Dm644 $(NAME).metainfo.xml $(STAGE_ROOT)$(PREFIX)/share/metainfo/$(APP_ID).metainfo.xml
	for size in $(ICON_SIZES); do \
	    install -Dm644 logo/rendered/$$size.png $(STAGE_ROOT)$(PREFIX)/share/icons/hicolor/$${size}x$${size}/apps/$(NAME).png; \
	done
	install -Dm644 logo/icon.svg $(STAGE_ROOT)$(PREFIX)/share/icons/hicolor/scalable/apps/$(NAME).svg
	install -Dm644 packaging/debian/copyright $(STAGE_ROOT)$(PREFIX)/share/doc/$(NAME)/copyright
	install -Dm644 LICENSE README.md CHANGELOG.rst -t $(STAGE_ROOT)$(PREFIX)/share/doc/$(NAME)
	sed -e 's|@VERSION@|$(VERSION)|g' packaging/$(NAME).1.in > $(BUILD)/$(NAME).1
	install -Dm644 $(BUILD)/$(NAME).1 -t $(STAGE_ROOT)$(PREFIX)/share/man/man1
	gzip -9nf $(STAGE_ROOT)$(PREFIX)/share/man/man1/$(NAME).1
	@# Normalise the modes: the repository's files may be 0664, packages want 0644.
	find $(STAGE_ROOT) -type f -exec chmod 644 {} +
	chmod 755 $(STAGE_ROOT)$(PREFIX)/bin/$(NAME)
ifeq ($(FLAVOUR),full)
	chmod 755 $(STAGE_ROOT)$(LIBDIR)/$(NAME)/$(NAME)
	install -Dm644 packaging/debian/lintian-overrides $(STAGE_ROOT)$(PREFIX)/share/lintian/overrides/$(NAME)
	@# Wheels ship unstripped extension modules (never strip the executable itself:
	@# PyInstaller appends its archive to it).
	if command -v strip >/dev/null 2>&1; then \
	    find $(STAGE_ROOT)$(LIBDIR)/$(NAME) -name '*.so*' -type f -exec strip --strip-unneeded {} + ; \
	fi
endif
	touch $@

# ----------------------------------------------------------------------------
# Installing straight onto this machine (needs root for the default PREFIX)
# ----------------------------------------------------------------------------

install: stage ## Install under $(DESTDIR)$(PREFIX) (run `make stage` first, then `sudo make install`)
	install -d $(DESTDIR)$(PREFIX)
	cp -a $(STAGE_ROOT)$(PREFIX)/. $(DESTDIR)$(PREFIX)/
	@# Refresh the desktop caches, but only on a real install (not into a DESTDIR).
	@if [ -z "$(DESTDIR)" ]; then \
	    update-desktop-database -q $(PREFIX)/share/applications 2>/dev/null || true; \
	    gtk-update-icon-cache -q -t -f $(PREFIX)/share/icons/hicolor 2>/dev/null || true; \
	fi
	@echo "Installed $(NAME) $(VERSION) ($(FLAVOUR)) under $(DESTDIR)$(PREFIX)"

uninstall: ## Remove what `make install` put under $(DESTDIR)$(PREFIX)
	rm -rf $(DESTDIR)$(LIBDIR)
	rm -f $(DESTDIR)$(PREFIX)/bin/$(NAME)
	rm -f $(DESTDIR)$(PREFIX)/share/applications/$(NAME).desktop
	rm -f $(DESTDIR)$(PREFIX)/share/metainfo/$(APP_ID).metainfo.xml
	for size in $(ICON_SIZES); do \
	    rm -f $(DESTDIR)$(PREFIX)/share/icons/hicolor/$${size}x$${size}/apps/$(NAME).png; \
	done
	rm -f $(DESTDIR)$(PREFIX)/share/icons/hicolor/scalable/apps/$(NAME).svg
	rm -rf $(DESTDIR)$(PREFIX)/share/doc/$(NAME)
	rm -f $(DESTDIR)$(PREFIX)/share/lintian/overrides/$(NAME)
	rm -f $(DESTDIR)$(PREFIX)/share/man/man1/$(NAME).1.gz
	@# Refresh the desktop caches, but only on a real install (not into a DESTDIR).
	@if [ -z "$(DESTDIR)" ]; then \
	    update-desktop-database -q $(PREFIX)/share/applications 2>/dev/null || true; \
	    gtk-update-icon-cache -q -t -f $(PREFIX)/share/icons/hicolor 2>/dev/null || true; \
	fi
	@echo "Removed $(NAME) from $(DESTDIR)$(PREFIX)"

# ----------------------------------------------------------------------------
# Debian package
# ----------------------------------------------------------------------------

deb: ## Build a .deb into dist/ (BUNDLE=1 bundled Python deps, BUNDLE=2 fully self-contained, BUNDLE=0 thin)
	$(MAKE) stage STAGE_ROOT=$(DEB_ROOT) PREFIX=/usr
	rm -rf $(DEB_ROOT)/DEBIAN
	install -d $(DEB_ROOT)/DEBIAN dist
	@# A bundled package with compiled extension modules only works with the Python
	@# it was downloaded for; a pure-Python one works with any Python 3.10+.
	@if [ "$(FLAVOUR)" = full ]; then \
	    arch="$(DEB_ARCH)"; depends="$$(packaging/debian/host-deps.sh $(DEB_ROOT)/usr/lib/$(NAME)/$(NAME))"; \
	    recommends="$(RECOMMENDS)"; \
	elif [ "$(FLAVOUR)" = bundled ]; then \
	    if find $(DEB_ROOT)/usr/lib/$(NAME)/vendor -name '*.so' | grep -q .; then \
	        arch="$(DEB_ARCH)"; depends="python3 (>= $(PY_MINOR)), python3 (<< $(PY_NEXT)), libc6 (>= 2.17), $(SYSTEM_DEPENDS)"; \
	    else \
	        arch="all"; depends="python3 (>= 3.10), $(SYSTEM_DEPENDS)"; \
	    fi; \
	    recommends="$(RECOMMENDS)"; \
	else \
	    arch="all"; depends="$(THIN_DEPENDS)"; recommends="$(THIN_RECOMMENDS)"; \
	fi; \
	sed -e "s|@VERSION@|$(VERSION)|" -e "s|@ARCH@|$$arch|" -e "s|@MAINTAINER@|$(MAINTAINER)|" \
	    -e "s|@INSTALLED_SIZE@|$$(du -sk $(DEB_ROOT) | cut -f1)|" -e "s|@DEPENDS@|$$depends|" \
	    -e "s|@RECOMMENDS@|$$recommends|" -e "s|@FLAVOUR@|$(FLAVOUR)|" \
	    packaging/debian/control.in > $(DEB_ROOT)/DEBIAN/control; \
	printf '%s (%s) unstable; urgency=medium\n\n  * Sublime Music (Plus) %s; the changes are listed in CHANGELOG.rst.\n\n -- %s  %s\n' \
	    "$(NAME)" "$(VERSION)" "$(VERSION)" "$(MAINTAINER)" "$$(date -R)" | gzip -9n > $(DEB_ROOT)/usr/share/doc/$(NAME)/changelog.gz; \
	chmod 644 $(DEB_ROOT)/usr/share/doc/$(NAME)/changelog.gz; \
	find $(DEB_ROOT) -type d -exec chmod 755 {} +; \
	dpkg-deb --root-owner-group --build $(DEB_ROOT) dist/$(NAME)_$(VERSION)_$${arch}_$(FLAVOUR).deb

# ----------------------------------------------------------------------------
# macOS: "Sublime Music.app" wrapped in a .pkg installer (run this on macOS)
# ----------------------------------------------------------------------------
#
# BUNDLE=1 (default): a self-contained app built with PyInstaller. Homebrew or MacPorts is needed to
#   build it, not to run it: Python, GTK, PyGObject, libmpv and the Python dependencies
#   are all copied into the bundle (the same PyInstaller spec as the Linux "full"
#   flavour). See packaging/macos/README.md.
# BUNDLE=0: a thin app that runs the Homebrew Python and GTK of the Mac it is installed on.

APP      := $(BUILD)/Sublime Music.app
ICNS     := $(BUILD)/$(NAME).icns
PKG      := dist/SublimeMusic-$(VERSION)-$(FLAVOUR).pkg

# The macOS icon is its own image (logo/mac-icon.png, 1024x1024: the shape and margins
# macOS icons have), scaled to each size of the iconset.
MAC_ICON := logo/mac-icon.png

$(ICNS): $(MAC_ICON)
	rm -rf $(BUILD)/icon.iconset && mkdir -p $(BUILD)/icon.iconset
	for size in 16 32 128 256 512; do \
	    sips -z $$size $$size $(MAC_ICON) --out $(BUILD)/icon.iconset/icon_$${size}x$${size}.png >/dev/null; \
	    sips -z $$((size * 2)) $$((size * 2)) $(MAC_ICON) --out $(BUILD)/icon.iconset/icon_$${size}x$${size}@2x.png >/dev/null; \
	done
	iconutil -c icns -o $@ $(BUILD)/icon.iconset

pkg: ## macOS only: build "Sublime Music.app" and dist/SublimeMusic-<version>-<flavour>.pkg
	@test "$$(uname)" = Darwin || { echo "make pkg builds a macOS bundle and only works on macOS"; exit 1; }
	@test -n "$(MACOS_PREFIX)" || { echo "Homebrew or MacPorts is required to build the app, see packaging/macos/README.md"; exit 1; }
	$(MAKE) $(ICNS)
	rm -rf "$(APP)"
ifneq ($(FLAVOUR),thin)
	$(MAKE) $(PYI_VENV)/.stamp
	$(PYI_VENV)/bin/pip install --no-deps --force-reinstall --quiet .
	MACOS_PREFIX=$(MACOS_PREFIX) $(PYI_VENV)/bin/pyinstaller --noconfirm --clean \
	    --distpath $(BUILD) --workpath $(BUILD)/macos-work $(PYI_SPEC)
else
	$(MAKE) vendor
	mkdir -p "$(APP)/Contents/MacOS" "$(APP)/Contents/Resources/lib"
	cp -r $(PACKAGE) "$(APP)/Contents/Resources/lib/"
	find "$(APP)/Contents/Resources/lib" -name __pycache__ -type d -prune -exec rm -rf {} +
	cp -r $(VENDOR) "$(APP)/Contents/Resources/lib/vendor"
	rm -f "$(APP)/Contents/Resources/lib/vendor/.stamp"
	sed -e 's|@VERSION@|$(VERSION)|g' packaging/macos/Info.plist.in > "$(APP)/Contents/Info.plist"
	install -m755 packaging/macos/launcher.sh "$(APP)/Contents/MacOS/$(NAME)"
	cp $(ICNS) "$(APP)/Contents/Resources/$(NAME).icns"
endif
	codesign --force --deep --sign - "$(APP)"
	@# The bundle must not be "relocatable": otherwise the installer updates whatever copy
	@# of the app it finds on the disk (this build directory, say) instead of installing
	@# into /Applications.
	rm -rf $(BUILD)/pkgroot && mkdir -p $(BUILD)/pkgroot dist
	cp -R "$(APP)" $(BUILD)/pkgroot/
	pkgbuild --analyze --root $(BUILD)/pkgroot $(BUILD)/component.plist
	/usr/libexec/PlistBuddy -c "Set :0:BundleIsRelocatable false" $(BUILD)/component.plist
	pkgbuild --root $(BUILD)/pkgroot --component-plist $(BUILD)/component.plist \
	    --identifier $(APP_ID) --version $(VERSION) --install-location /Applications $(PKG)
	@echo "Built $(PKG)"

# ----------------------------------------------------------------------------
# Development
# ----------------------------------------------------------------------------

run: ## Run the app from the source tree, with .venv when it exists (ARGS="-m debug" for logging)
	PYTHONPATH=. $(VENV_PYTHON) -m $(PACKAGE) $(ARGS)

venv: $(VENV)/.stamp ## Create .venv with the app's dependencies and the dev/test tools (PyGObject from the system)

# The venv sees the system packages only for PyGObject (it must match the system GTK).
# Everything else is installed into the venv itself, so a distribution package being
# removed (apt autoremove...) cannot break `make run`, `make test` or `make lint`.
$(VENV)/.stamp: pyproject.toml $(wildcard $(VENDOR_LOCK))
	rm -rf $(VENV)
	$(PYTHON) -m venv --system-site-packages $(VENV)
	$(VENV)/bin/pip install --upgrade pip
	$(VENV)/bin/pip install --no-deps -e .
	mkdir -p $(BUILD)
	$(VENV)/bin/python -c 'import tomllib; e = tomllib.load(open("pyproject.toml", "rb"))["project"]["optional-dependencies"]; print("\n".join(r for k in ("dev", "test") for r in e[k]))' > $(BUILD)/dev-requirements.txt
	$(VENV)/bin/pip install --ignore-installed $(VENDOR_SPEC) $(MACOS_DEPS) -r $(BUILD)/dev-requirements.txt
	touch $@

test: ## Run the test suite (pytest, with doctests and coverage as configured in setup.cfg)
	PYTHONPATH=. $(VENV_PYTHON) -m pytest

lint: ## Check formatting, imports, style and types
	$(call TOOL,black) --check $(PACKAGE) tests
	$(call TOOL,isort) --check-only $(PACKAGE) tests
	$(call TOOL,flake8) $(PACKAGE) tests
	$(call TOOL,mypy) $(PACKAGE) tests

format: ## Reformat the code with black and isort
	$(call TOOL,black) $(PACKAGE) tests
	$(call TOOL,isort) $(PACKAGE) tests

clean: ## Remove build/ and dist/
	rm -rf $(BUILD) dist

distclean: clean ## Also remove .venv and caches
	rm -rf $(VENV) .mypy_cache .pytest_cache htmlcov .coverage
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
