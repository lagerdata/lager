# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Filesystem-safe path construction for values that arrive off the wire.

This lives in ``util`` rather than beside any one caller because the debug
layer, the HTTP handler layer and the drivers all build paths out of strings a
client supplied, and ``util`` is the only layer all three may import -- the
same reasoning as ``lager.util.usb_sysfs``.

``fs_slug`` reduces a value to characters that cannot change the shape of a
path, so a value carrying separators collapses to one flat filename. That is
the defence, and it is shared.

The containment check that follows it is deliberately NOT shared, and this is
the part worth reading before you tidy it up. Every caller repeats::

    path = os.path.normpath(os.path.join(ROOT, f'name_{fs_slug(value)}.ext'))
    if not path.startswith(ROOT + os.sep):
        raise ValueError(...)

That repetition is not an oversight. A static analyser recognises a path guard
only inside the function that builds the path -- the barrier does not survive a
return. Extracting these four lines into a helper here leaves the code correct
and the analysis blind, which is measurably worse than it sounds: doing exactly
that left 35 path-injection findings open against code that could not be made
to traverse. Keep the check next to the join it guards.
"""

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
