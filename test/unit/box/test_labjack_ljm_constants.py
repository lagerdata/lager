# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
LJM's constants file on a Mac Box (box/lager/io/labjack_handle.py).

A Mac Box's installer unpacks LJM into the host agent's own directory instead
of running LabJack's installer, so LJM's data files are not at
/usr/local/share/LabJack/LJM. Without its constants file LJM cannot resolve a
register name such as ``AIN0``. The agent's launchd job names the file in
``LAGER_LJM_CONSTANTS_FILE``; the handle module must hand it to LJM before any
device is opened, and must leave LJM alone when the variable is unset (every
Linux Box).
"""

import os
import unittest
from unittest.mock import MagicMock, patch

from lager.io import labjack_handle


class LjmConstantsFileTest(unittest.TestCase):

    def test_the_constants_file_is_handed_to_ljm_when_named(self):
        ljm = MagicMock()
        with patch.dict(os.environ, {labjack_handle.LJM_CONSTANTS_ENV: "/x/ljm_constants.json"}):
            labjack_handle._point_ljm_at_its_files(ljm)
        ljm.writeLibraryConfigStringS.assert_called_once_with(
            "LJM_CONSTANTS_FILE", "/x/ljm_constants.json")

    def test_ljm_is_left_alone_without_the_variable(self):
        ljm = MagicMock()
        env = {k: v for k, v in os.environ.items() if k != labjack_handle.LJM_CONSTANTS_ENV}
        with patch.dict(os.environ, env, clear=True):
            labjack_handle._point_ljm_at_its_files(ljm)
        ljm.writeLibraryConfigStringS.assert_not_called()

    def test_the_missing_library_message_names_this_platforms_file(self):
        name = "libLabJackM.dylib" if labjack_handle.sys.platform == "darwin" else "libLabJackM.so"
        self.assertIn(name, labjack_handle._MISSING_LIBRARY)


if __name__ == "__main__":
    unittest.main()
