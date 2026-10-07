# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
lager.run_assertions - let a script put facts into its run record.

The box records what it can observe about a run. Some facts only the script
knows, the firmware on the device under test above all. ``record_value``
writes one of those into the run record's ``scriptAsserted`` section::

    from lager import record_value

    record_value('dut.firmware', dut.read_version())
    record_value('dut.serial', '7Q2-00419')

The value is recorded verbatim and is not verified: it is the script's claim,
in the same way ``clientAsserted`` is the client's. The last value written for
a key wins.

Keys are 1-128 characters of letters, digits and ``. _ : / -``, starting with
a letter or digit. Values are strings (up to 4096 characters), numbers,
booleans or None. A run records at most 256 keys.

Outside a recorded run (``LAGER_RUN_ASSERTIONS`` unset), this does nothing and
returns False, so a script that uses it still runs anywhere.
"""

import json
import math
import os
import re

__all__ = ['record_value']

_ENV = 'LAGER_RUN_ASSERTIONS'
_KEY_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$')
_MAX_VALUE_LEN = 4096


def record_value(key, value):
    """Record ``key = value`` in this run's record. See the module docstring.

    Returns:
        bool: True when written, False outside a recorded run.

    Raises:
        ValueError: the key or value is not one the record can hold.
    """
    if not isinstance(key, str) or not _KEY_RE.match(key):
        raise ValueError(f'invalid run record key {key!r}')
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        raise ValueError('NaN and infinity cannot be recorded')
    if isinstance(value, str):
        if len(value) > _MAX_VALUE_LEN:
            raise ValueError(f'run record values are limited to {_MAX_VALUE_LEN} characters')
    elif value is not None and not isinstance(value, (bool, int, float)):
        raise ValueError(f'run record values must be str, int, float, bool or None, not {type(value).__name__}')

    path = os.environ.get(_ENV)
    if not path:
        return False
    line = json.dumps({'key': key, 'value': value}, ensure_ascii=False) + '\n'
    with open(path, 'a', encoding='utf-8') as f:
        f.write(line)
    return True
