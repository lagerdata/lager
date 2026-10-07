# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""RFC 8785 JSON Canonicalization Scheme (JCS).

A run record is identified by the hash of its canonical bytes, and anyone who
holds the record has to be able to reproduce those bytes in any language. JCS
is the standard that makes that possible: object keys sorted by UTF-16 code
units, no insignificant whitespace, strings escaped minimally, and numbers
written the way ECMAScript's ``Number.prototype.toString`` writes them.

Python's ``json.dumps(sort_keys=True, separators=(',', ':'))`` gets most of
this right and two things wrong, which is why this module exists:

* ``sort_keys`` orders by code point. JCS orders by UTF-16 code unit, and the
  two disagree for characters outside the Basic Multilingual Plane.
* Floats. ``repr(1e-7)`` is ``'1e-07'`` and ``repr(1e16)`` is ``'1e+16'``;
  ECMAScript writes ``1e-7`` and ``10000000000000000``. Net configurations
  carry floats (voltage and current limits), so this is not hypothetical.

Only what JSON can hold is accepted. NaN and infinities have no JSON form and
raise, as RFC 8785 requires.
"""

import json
import math

__all__ = ['canonicalize', 'canonical_sha256']


def canonicalize(value):
    """The JCS bytes for ``value``.

    Raises:
        ValueError: a float that is NaN or infinite
        TypeError: a value JSON cannot represent
    """
    parts = []
    _write(value, parts)
    return ''.join(parts).encode('utf-8')


def canonical_sha256(value):
    """Lowercase hex SHA-256 of ``canonicalize(value)``."""
    import hashlib

    return hashlib.sha256(canonicalize(value)).hexdigest()


def _write(value, out):
    if value is None:
        out.append('null')
    elif value is True:
        out.append('true')
    elif value is False:
        out.append('false')
    elif isinstance(value, int):
        out.append(str(value))
    elif isinstance(value, float):
        out.append(_number(value))
    elif isinstance(value, str):
        out.append(_string(value))
    elif isinstance(value, (list, tuple)):
        out.append('[')
        for i, item in enumerate(value):
            if i:
                out.append(',')
            _write(item, out)
        out.append(']')
    elif isinstance(value, dict):
        for key in value:
            if not isinstance(key, str):
                raise TypeError(f'JCS object keys must be strings, got {type(key).__name__}')
        out.append('{')
        for i, key in enumerate(sorted(value, key=_utf16_units)):
            if i:
                out.append(',')
            out.append(_string(key))
            out.append(':')
            _write(value[key], out)
        out.append('}')
    else:
        raise TypeError(f'{type(value).__name__} is not JSON serializable')


def _utf16_units(key):
    return key.encode('utf-16-be')


def _string(value):
    # json.dumps with ensure_ascii=False escapes exactly what RFC 8785 asks
    # for: the quote, the backslash, and the C0 controls, using the short
    # forms (\b \f \n \r \t) where they exist and lowercase \u00XX otherwise.
    return json.dumps(value, ensure_ascii=False)


def _number(value):
    """ECMAScript Number.prototype.toString for a finite double."""
    if math.isnan(value) or math.isinf(value):
        raise ValueError('NaN and Infinity have no JSON representation')
    if value == 0:
        return '0'  # also covers -0.0, which ECMAScript prints as 0

    sign = '-' if value < 0 else ''
    # repr gives the shortest digit string that round-trips, which is what
    # ECMAScript uses too. Pull the digits and the decimal exponent out of it.
    mantissa, _, exp = repr(abs(value)).partition('e')
    int_part, _, frac_part = mantissa.partition('.')
    digits = (int_part + frac_part).lstrip('0')
    # n: position of the decimal point relative to the start of `digits`.
    n = len(int_part.lstrip('0')) if int_part.strip('0') else -(len(frac_part) - len(frac_part.lstrip('0')))
    n += int(exp) if exp else 0
    digits = digits.rstrip('0') or '0'
    k = len(digits)

    if k <= n <= 21:
        return sign + digits + '0' * (n - k)
    if 0 < n <= 21:
        return sign + digits[:n] + '.' + digits[n:]
    if -6 < n <= 0:
        return sign + '0.' + '0' * (-n) + digits
    e = n - 1
    e_str = ('+' if e >= 0 else '-') + str(abs(e))
    if k == 1:
        return sign + digits + 'e' + e_str
    return sign + digits[0] + '.' + digits[1:] + 'e' + e_str
