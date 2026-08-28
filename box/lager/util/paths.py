# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Filesystem-safe path construction for values that arrive off the wire.

This lives in ``util`` rather than beside any one caller because the debug
layer, the HTTP handler layer and the drivers all build paths out of strings a
client supplied, and ``util`` is the only layer all three may import -- the
same reasoning as ``lager.util.usb_sysfs``.

Two functions, and both earn their place:

``fs_slug`` reduces a value to characters that cannot change the shape of a
path, so a value carrying separators collapses to one flat filename.

``contained_path`` joins, normalises, and then verifies the result is still
directly inside the root it was given. That states the property a reader
actually wants -- the file lands here -- instead of leaving them to re-derive
it from a regex, and it fails loudly rather than silently writing elsewhere.

It normalises rather than resolves, on purpose. ``realpath`` would rewrite
``/tmp/x`` to ``/private/tmp/x`` on macOS, so the box and a developer's laptop
would disagree about a path production spells one way, and it would reject
every ``/sys/bus/usb/devices`` entry, all of which are symlinks into
``/sys/devices`` by design.

The limit that buys: a symlink pre-planted at the final filename is followed,
not caught. That case needs write access to the directory already, which
``SECURITY.md`` puts out of scope.
"""

import os
import re

# Everything outside this set collapses to '_'. Dot is allowed so that a
# serial or net name keeps its extension-like suffixes intact; it is safe
# because every caller embeds the slug in a longer filename, so a slug of
# '.' or '..' can never become a path component in its own right.
_UNSAFE_COMPONENT_CHARS = re.compile(r'[^A-Za-z0-9._-]')


def fs_slug(value):
    """Reduce *value* to characters that are safe in one path component.

    Returns a string with no directory separators, so the result can be
    interpolated into a filename without changing which directory it lands in.
    """
    return _UNSAFE_COMPONENT_CHARS.sub('_', str(value))


def contained_path(root, name):
    """Return ``root``/``name``, having proven *name* stayed a single component.

    Raises :class:`ValueError` if it did not. Callers that build a filename out
    of untrusted input should slug it with :func:`fs_slug` first -- this is the
    check that the slug worked, not a replacement for it.
    """
    root_norm = os.path.normpath(root)
    candidate = os.path.normpath(os.path.join(root_norm, name))
    if os.path.dirname(candidate) != root_norm:
        raise ValueError(f'refusing a path outside {root_norm!r}: {name!r}')
    return candidate
