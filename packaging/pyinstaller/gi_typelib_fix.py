"""
Build-time fixes for PyInstaller's GI typelib collection.

PyInstaller rewrites GIR files on macOS so collected typelibs load bundled dylibs via
@loader_path. Some Homebrew Gio GIRs reference POSIX typedefs directly in
GCredentials (pid_t and uid_t) without defining GIR aliases for them. Newer
`g-ir-compiler` rejects those unresolved type references, which means PyInstaller
silently returns typelib paths that were never created.

Patch only the temporary, rewritten Gio GIR in PyInstaller's work directory before it
is compiled. The source Homebrew GIR and the resulting app's runtime search paths are
unchanged, so Homebrew remains a build dependency only.

Thx ai i am too tiered for this - crt0512
"""
import os
import re
import subprocess

from PyInstaller import compat
from PyInstaller import log as logging
from PyInstaller.utils.hooks import gi as gi_hooks

logger = logging.getLogger(__name__)

_ORIGINAL_GIR_LIBRARY_PATH_FIX = gi_hooks.gir_library_path_fix
_POSIX_TYPE_ALIASES = {
    "pid_t": ("gint", "gint"),
    "time_t": ("gint64", "gint64"),
    "uid_t": ("guint", "guint"),
}


def _rewrite_shared_library_paths(lines):
    """Return GIR lines with shared libraries rewritten to @loader_path."""
    rewritten = []
    for line in lines:
        if "shared-library" in line:
            split = re.split("(=)", line)
            files = re.split('(["|,])', split[2])
            for count, item in enumerate(files):
                if "lib" in item:
                    files[count] = "@loader_path/" + os.path.basename(item)
            line = "".join(split[0:2]) + "".join(files)
        rewritten.append(line)
    return rewritten


def _add_posix_type_aliases(gir_name, text):
    """Add aliases for direct POSIX typedef references when missing."""
    aliases = []
    for alias, (target_name, target_c_type) in _POSIX_TYPE_ALIASES.items():
        has_reference = f'<type name="{alias}" c:type="{alias}"/>' in text
        has_alias = f'<alias name="{alias}"' in text
        if has_reference and not has_alias:
            aliases.append(
                f'    <alias name="{alias}" c:type="{alias}">\n'
                f'      <type name="{target_name}" c:type="{target_c_type}"/>\n'
                f"    </alias>\n"
            )

    if not aliases:
        return text

    match = re.search(r"(<namespace\b[^>]*>\n)", text, re.S)
    if not match:
        logger.warning("Could not add POSIX typedef aliases; namespace marker not found in %s", gir_name)
        return text

    alias_names = [re.search(r'name="([^"]+)"', alias).group(1) for alias in aliases]
    logger.info("Adding missing POSIX typedef aliases for bundled %s: %s", gir_name, ", ".join(alias_names))
    return text[: match.end()] + "".join(aliases) + text[match.end() :]


def _fixed_gir_library_path_fix(path):
    """
    macOS replacement for PyInstaller's gir_library_path_fix.

    This intentionally mirrors PyInstaller's implementation, with two changes:
    unresolved POSIX typedef aliases are added before compilation, and
    g-ir-compiler failures are surfaced immediately instead of returning a missing
    output path.
    """
    from PyInstaller.config import CONF

    path = os.path.abspath(path)

    if not compat.is_darwin:
        return _ORIGINAL_GIR_LIBRARY_PATH_FIX(path)

    common_path = os.path.commonprefix([compat.base_prefix, path])
    if common_path == "/":
        logger.debug("virtualenv detected? fixing the gir path...")
        common_path = os.path.abspath(os.path.join(path, "..", "..", ".."))

    gir_path = os.path.join(common_path, "share", "gir-1.0")
    typelib_name = os.path.basename(path)
    gir_name = os.path.splitext(typelib_name)[0] + ".gir"
    gir_file = os.path.join(gir_path, gir_name)

    if not os.path.exists(gir_path):
        logger.error(
            "Unable to find gir directory: %s.\nTry installing your platform's gobject-introspection package.",
            gir_path,
        )
        return None
    if not os.path.exists(gir_file):
        logger.error(
            "Unable to find gir file: %s.\nTry installing your platform's gobject-introspection package.",
            gir_file,
        )
        return None

    with open(gir_file, "r", encoding="utf-8") as f:
        lines = f.readlines()

    fixed_gir = os.path.join(CONF["workpath"], gir_name)
    fixed_typelib = os.path.join(CONF["workpath"], typelib_name)
    text = "".join(_rewrite_shared_library_paths(lines))
    text = _add_posix_type_aliases(gir_name, text)

    with open(fixed_gir, "w", encoding="utf-8") as f:
        f.write(text)

    subprocess.run(("g-ir-compiler", "--includedir", gir_path, fixed_gir, "-o", fixed_typelib), check=True)
    return fixed_typelib, "gi_typelibs"


def install_macos_gio_typedef_fix():
    """Install the PyInstaller GI collection shim on macOS builds."""
    if compat.is_darwin:
        gi_hooks.gir_library_path_fix = _fixed_gir_library_path_fix
