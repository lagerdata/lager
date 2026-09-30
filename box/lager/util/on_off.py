# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Reading an instrument's on/off reply without guessing.

Output-state queries (``:OUTP?``, ``:INP?``, ``OUTPut:STATe?``) answer ``1``/
``0`` or ``ON``/``OFF`` depending on the instrument and firmware, sometimes
with trailing whitespace. Most drivers used to fold every other outcome -- a
timeout, an empty reply, a garbled one -- into ``False``, so a read that failed
was reported as a confident "off". A state report must be able to say "not
known"; this is the one place that decides what counts as an answer.
"""

_ON = frozenset(("1", "ON"))
_OFF = frozenset(("0", "OFF"))


def parse_on_off(raw):
    """``True`` for an on reply, ``False`` for an off reply, else ``None``.

    Accepts ``1``/``0`` and ``ON``/``OFF`` in any case, surrounding whitespace
    ignored. ``None``, an empty string, and anything else are ``None``: the
    instrument did not say.
    """
    if raw is None:
        return None
    text = str(raw).strip().upper()
    if text in _ON:
        return True
    if text in _OFF:
        return False
    return None
