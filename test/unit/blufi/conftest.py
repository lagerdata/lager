# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Bootstrap for the blufi unit suite: the real ``lager`` package with a
mocked ``bleak``.

BluFi talks BLE straight to the DUT via ``bleak``, a library that only
exists on boxes (and deliberately NOT in test/requirements-unit.txt -- it
drags in a dbus stack). Stub it so ``lager.blufi`` imports on a hosted
runner; the tests exercise pure-logic code (framing, CRC, crypto) and
mock the transport per-test.

This bootstrap used to be duplicated at the top of both test files in
this directory; it lives here once now. Same process contract as the box
and measurement suites: this suite wants the real ``lager`` -- one suite
per pytest invocation, never bare ``pytest test/unit/``.
"""

import os
import sys
from unittest.mock import MagicMock

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
BOX_DIR = os.path.join(REPO_ROOT, "box")

# FRONT of sys.path, remove-then-insert -- see test/unit/box/conftest.py.
if BOX_DIR in sys.path:
    sys.path.remove(BOX_DIR)
sys.path.insert(0, BOX_DIR)

# bleak is a BLE library only available on boxes. Mock it so we can import
# the BluFi package locally for unit-testing pure-logic code.
if "bleak" not in sys.modules:
    _mock_bleak = MagicMock()
    sys.modules["bleak"] = _mock_bleak
    sys.modules["bleak.backends"] = _mock_bleak.backends
    sys.modules["bleak.backends.characteristic"] = _mock_bleak.backends.characteristic
