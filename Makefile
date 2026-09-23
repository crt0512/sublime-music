# Sublime Music (Plus) build system.
#
# Run `make help` for the list of targets. The usual flow on Debian/Ubuntu is
#
#     make deb                # dist/sublime-music_<version>_<arch>_bundled.deb
#     sudo dpkg -i dist/*.deb
#
# or, without a package,
#
#     make stage              # as your user, so build/ stays yours
#     sudo make install       # copies the staged tree under /usr/local
#     sudo make uninstall
#
# Dependency flavours (BUNDLE=1 is the default):
#   BUNDLE=1  bundled: the Python dependencies are downloaded with pip and shipped
#             inside the package, so distribution updates cannot break the app.
#             Only Python itself, PyGObject/GTK and libmpv come from the system,
#             because they must match the system's GTK.
#   BUNDLE=0  thin: nothing is bundled, the package depends on the distribution's
#             python3-* packages instead (see THIN_DEPENDS below).
#
# Variables you may want to override: PYTHON, PREFIX (default /usr/local for
# install, always /usr for deb), DESTDIR, BUNDLE, MAINTAINER.

PYTHON  ?= python3
PREFIX  ?= /usr/local
DESTDIR ?=
BUNDLE  ?= 1

NAME     := sublime-music
PACKAGE  := sublime_music
APP_ID   := app.sublimemusic.SublimeMusic
VERSION  := $(shell sed -n 's/^__version__ = "\(.*\)"/\1/p' $(PACKAGE)/__init__.py)
FLAVOUR  := $(if $(filter 0,$(BUNDLE)),thin,bundled)
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

LIBDIR     := $(PREFIX)/lib/$(NAME)
ICON_SIZES := 16 22 24 32 36 48 64 72 96 128 192 512

# The runtime dependencies from pyproject.toml, minus PyGObject (it is compiled
# against the system's GTK, so it always comes from the system: python3-gi on
# Debian, pygobject3 from Homebrew on macOS). Keep in sync with pyproject.toml.
VENDOR_DEPS := bleach bottle dataclasses-json peewee pychromecast python-dateutil mpv requests semver thefuzz keyring

# What both flavours need from the system.
SYSTEM_DEPENDS := python3-gi, python3-gi-cairo, gir1.2-gtk-3.0, gir1.2-glib-2.0, libmpv2
RECOMMENDS     := gir1.2-notify-0.7, gir1.2-nm-1.0
# The thin flavour gets everything from Debian's packages.
THIN_DEPENDS   := python3 (>= 3.10), $(SYSTEM_DEPENDS), python3-mpv, python3-peewee, python3-dataclasses-json, python3-requests, python3-dateutil, python3-semver, python3-bottle, python3-bleach, python3-thefuzz, python3-pychromecast
THIN_RECOMMENDS := $(RECOMMENDS), python3-keyring

SOURCES := $(shell find $(PACKAGE) -type f -not -path '*/__pycache__/*')

.PHONY: help build vendor stage install uninstall deb pkg run test lint format venv clean distclean

help: ## Show this help
	@echo "Sublime Music (Plus) $(VERSION), flavour: $(FLAVOUR) (BUNDLE=$(BUNDLE))"
	@echo
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | sed 's/:.*## /|/' | sort | awk -F'|' '{printf "  %-12s %s\n", $$1, $$2}'
	@echo
	@echo "Variables: PYTHON=$(PYTHON) PREFIX=$(PREFIX) DESTDIR=$(DESTDIR) BUNDLE=$(BUNDLE)"

# ----------------------------------------------------------------------------
# Building
# ----------------------------------------------------------------------------

build: $(WHEEL) ## Build the Python wheel into dist/

$(WHEEL): pyproject.toml README.md LICENSE $(SOURCES)
	$(PYTHON) -m pip wheel --no-deps --wheel-dir dist .

vendor: $(VENDOR)/.stamp ## Download the bundled Python dependencies into build/vendor

$(VENDOR)/.stamp: Makefile
	rm -rf $(VENDOR)
	$(PYTHON) -m pip install --target $(VENDOR) --no-compile --upgrade $(VENDOR_DEPS)
	rm -rf $(VENDOR)/bin
	touch $@

# The installed tree, assembled under $(STAGE_ROOT)$(PREFIX). `install` copies it
# to $(DESTDIR)$(PREFIX) and `deb` wraps it into a package.
stage: $(STAGE_ROOT).stamp ## Assemble the installed tree under build/ (respects PREFIX and BUNDLE)

# The stamp lives next to the tree, not inside it, so that it never ends up in a package.
$(STAGE_ROOT).stamp: $(if $(filter bundled,$(FLAVOUR)),$(VENDOR)/.stamp) $(SOURCES) packaging/launcher.sh.in packaging/$(NAME).1.in packaging/debian/copyright packaging/$(NAME).desktop $(NAME).metainfo.xml LICENSE README.md CHANGELOG.rst
	rm -rf $(STAGE_ROOT)
	install -d $(STAGE_ROOT)$(LIBDIR) $(STAGE_ROOT)$(PREFIX)/bin
	cp -r $(PACKAGE) $(STAGE_ROOT)$(LIBDIR)/
	find $(STAGE_ROOT)$(LIBDIR) -name __pycache__ -type d -prune -exec rm -rf {} +
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
	sed -e 's|@LIBDIR@|$(LIBDIR)|g' -e 's|@PYTHON@|python3|g' packaging/launcher.sh.in > $(STAGE_ROOT)$(PREFIX)/bin/$(NAME)
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

deb: ## Build a .deb into dist/ (BUNDLE=0 for the thin flavour)
	$(MAKE) stage STAGE_ROOT=$(DEB_ROOT) PREFIX=/usr
	rm -rf $(DEB_ROOT)/DEBIAN
	install -d $(DEB_ROOT)/DEBIAN dist
	@# A bundled package with compiled extension modules only works with the Python
	@# it was downloaded for; a pure-Python one works with any Python 3.10+.
	@if [ "$(FLAVOUR)" = bundled ]; then \
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
# macOS: an .app bundle wrapped in a .pkg installer (run this on macOS)
# ----------------------------------------------------------------------------

APP := $(BUILD)/Sublime Music.app

pkg: ## macOS only: build "Sublime Music.app" and dist/SublimeMusic-<version>.pkg
	@test "$$(uname)" = Darwin || { echo "make pkg builds a macOS bundle and only works on macOS"; exit 1; }
	@command -v pkgbuild >/dev/null || { echo "pkgbuild (Xcode command line tools) is required"; exit 1; }
	$(MAKE) vendor
	rm -rf "$(APP)"
	mkdir -p "$(APP)/Contents/MacOS" "$(APP)/Contents/Resources/lib"
	cp -r $(PACKAGE) "$(APP)/Contents/Resources/lib/"
	find "$(APP)/Contents/Resources/lib" -name __pycache__ -type d -prune -exec rm -rf {} +
	cp -r $(VENDOR) "$(APP)/Contents/Resources/lib/vendor"
	rm -f "$(APP)/Contents/Resources/lib/vendor/.stamp"
	sed -e 's|@VERSION@|$(VERSION)|g' packaging/macos/Info.plist.in > "$(APP)/Contents/Info.plist"
	install -m755 packaging/macos/launcher.sh "$(APP)/Contents/MacOS/$(NAME)"
	rm -rf $(BUILD)/icon.iconset && mkdir -p $(BUILD)/icon.iconset
	cp logo/rendered/16.png $(BUILD)/icon.iconset/icon_16x16.png
	cp logo/rendered/32.png $(BUILD)/icon.iconset/icon_16x16@2x.png
	cp logo/rendered/32.png $(BUILD)/icon.iconset/icon_32x32.png
	cp logo/rendered/64.png $(BUILD)/icon.iconset/icon_32x32@2x.png
	cp logo/rendered/128.png $(BUILD)/icon.iconset/icon_128x128.png
	cp logo/rendered/512.png $(BUILD)/icon.iconset/icon_512x512.png
	cp logo/rendered/1024.png $(BUILD)/icon.iconset/icon_512x512@2x.png
	iconutil -c icns -o "$(APP)/Contents/Resources/$(NAME).icns" $(BUILD)/icon.iconset
	mkdir -p dist
	pkgbuild --component "$(APP)" --install-location /Applications \
	    --identifier $(APP_ID) --version $(VERSION) \
	    dist/SublimeMusic-$(VERSION).pkg

# ----------------------------------------------------------------------------
# Development
# ----------------------------------------------------------------------------

run: ## Run the app from the source tree (ARGS="-m debug" for logging)
	PYTHONPATH=. $(PYTHON) -m $(PACKAGE) $(ARGS)

venv: $(VENV)/bin/activate ## Create .venv with the dev and test tools (uses the system PyGObject)

$(VENV)/bin/activate: pyproject.toml
	$(PYTHON) -m venv --system-site-packages $(VENV)
	$(VENV)/bin/pip install --upgrade pip
	$(VENV)/bin/pip install -e '.[dev,test]'
	touch $@

# Use the tools from .venv when it exists, otherwise whatever is on PATH.
TOOL = $(if $(wildcard $(VENV)/bin/$(1)),$(VENV)/bin/$(1),$(1))

test: ## Run the test suite (pytest, with doctests and coverage as configured in setup.cfg)
	PYTHONPATH=. $(call TOOL,python) -m pytest

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
