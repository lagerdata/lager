# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Values carried between a container's stand-in and the real driver.

JSON with a few tagged shapes, so that what a driver returns arrives as the
same Python type it would have been on a Linux Box: bytes stay bytes, a
tuple stays a tuple, a dict keeps keys that are not strings. numpy values
become plain numbers and lists.
"""
from __future__ import annotations

import base64

_TAGS = ("__bytes__", "__tuple__", "__set__", "__items__", "__complex__")


def encode(value, handle=None):
    """``value`` as JSON-ready data.

    ``handle`` is called with anything that has no JSON form (an open
    session, a generator) and returns what to send in its place; without it
    such a value is an error.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"__bytes__": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, tuple):
        return {"__tuple__": [encode(v, handle) for v in value]}
    if isinstance(value, list):
        return [encode(v, handle) for v in value]
    if isinstance(value, (set, frozenset)):
        return {"__set__": [encode(v, handle) for v in value]}
    if isinstance(value, dict):
        if all(isinstance(k, str) and k not in _TAGS and k != "__handle__" for k in value):
            return {k: encode(v, handle) for k, v in value.items()}
        return {"__items__": [[encode(k, handle), encode(v, handle)]
                              for k, v in value.items()]}
    if isinstance(value, complex):
        return {"__complex__": [value.real, value.imag]}
    tolist = getattr(value, "tolist", None)  # numpy arrays and scalars
    if callable(tolist) and type(value).__module__.split(".")[0] == "numpy":
        return encode(tolist(), handle)
    if handle is not None:
        return handle(value)
    raise TypeError(f"{type(value).__name__} cannot be sent between the container "
                    "and the host agent")


def decode(value, handle=None):
    """The inverse of :func:`encode`. ``handle`` rebuilds a ``__handle__``."""
    if isinstance(value, list):
        return [decode(v, handle) for v in value]
    if not isinstance(value, dict):
        return value
    if len(value) == 1:
        (tag, body), = value.items()
        if tag == "__bytes__":
            return base64.b64decode(body)
        if tag == "__tuple__":
            return tuple(decode(v, handle) for v in body)
        if tag == "__set__":
            return {decode(v, handle) for v in body}
        if tag == "__items__":
            return {decode(k, handle): decode(v, handle) for k, v in body}
        if tag == "__complex__":
            return complex(body[0], body[1])
    if "__handle__" in value:
        if handle is None:
            raise TypeError("an object handle was sent where a value was expected")
        return handle(value)
    return {k: decode(v, handle) for k, v in value.items()}
