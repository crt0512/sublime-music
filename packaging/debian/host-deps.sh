#!/bin/sh
# Prints the Debian packages that provide the shared libraries a PyInstaller bundle still
# needs from the host (glibc, X11/xcb, OpenGL, Wayland: PyInstaller deliberately leaves
# those out; ldd reports /lib paths, dpkg knows them under /usr/lib, hence readlink).
# Used for the Depends field of the "full" package. Usage: host-deps.sh <dir>
set -e
bundle="$1"
bundled=$(find "$bundle" -name '*.so*' -printf '%f\n' | sort -u)
find "$bundle" -type f \( -name '*.so*' -o -perm -u+x \) -exec ldd {} + 2>/dev/null \
    | awk '/=> \// {print $1, $3}' | sort -u \
    | while read -r soname path; do
        echo "$bundled" | grep -qxF "$soname" || echo "$path"
      done | sort -u | xargs -r readlink -f | sort -u | xargs -r dpkg -S 2>/dev/null \
    | sed 's/: .*//; s/:[a-z0-9]*$//' | sort -u \
    | while read -r package; do
        if [ "$package" = libc6 ]; then
            echo "libc6 (>= $(ldd --version | head -1 | awk '{print $NF}'))"
        else
            echo "$package"
        fi
      done | paste -sd, | sed 's/,/, /g'
