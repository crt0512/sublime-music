#!/bin/sh
# Starts Sublime Music from inside the .app bundle.
#
# GTK and PyGObject are not bundled: install them with Homebrew first,
#     brew install python@3 pygobject3 gtk+3 adwaita-icon-theme mpv
# The pure-Python dependencies are bundled in Contents/Resources/lib/vendor.
RESOURCES="$(cd "$(dirname "$0")/../Resources" && pwd)"

PYTHON=""
for candidate in /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
        PYTHON="$candidate"
        break
    fi
done
if [ -z "$PYTHON" ]; then
    osascript -e 'display alert "Sublime Music" message "Python 3 from Homebrew is required: brew install python@3 pygobject3 gtk+3 adwaita-icon-theme mpv"' >/dev/null 2>&1
    exit 1
fi

export PYTHONPATH="$RESOURCES/lib/vendor:$RESOURCES/lib${PYTHONPATH:+:$PYTHONPATH}"
# Let python-mpv find Homebrew's libmpv.
export DYLD_FALLBACK_LIBRARY_PATH="/opt/homebrew/lib:/usr/local/lib${DYLD_FALLBACK_LIBRARY_PATH:+:$DYLD_FALLBACK_LIBRARY_PATH}"
exec "$PYTHON" -m sublime_music "$@"
