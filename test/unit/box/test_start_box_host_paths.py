# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""start_box.sh pre-creates box_config mount paths that do not exist yet.

The check was `[ ! -d ]`, so a mount of an existing file (box_config's
/usr/bin/lsusb and /usr/bin/dfu-util) was taken for a missing directory and
every boot printed "[WARNING] Could not create /usr/bin/lsusb (try: sudo
mkdir -p ...)", advice that would have been wrong to follow. The loop is run
here verbatim, extracted from the script.
"""

import os
import re
import subprocess
import unittest

_SCRIPT = os.path.join(os.path.dirname(__file__), "..", "..", "..", "box", "start_box.sh")


def _loop():
    text = open(_SCRIPT).read()
    match = re.search(r'\n(\s*for _p in "\$\{BOX_CONFIG_HOST_PATHS\[@\]\}"; do\n.*?\n\s*unset _p\n)',
                      text, re.S)
    assert match, "the host-path loop moved; update this test"
    return match.group(1)


def _run(paths):
    body = "BOX_CONFIG_HOST_PATHS=(" + " ".join(f"'{p}'" for p in paths) + ")\n" + _loop()
    return subprocess.run(["bash", "-c", body], capture_output=True, text=True, timeout=10)


class HostPathLoopTest(unittest.TestCase):

    def test_an_existing_file_is_left_alone_and_not_warned_about(self):
        out = _run([_SCRIPT]).stdout
        self.assertEqual(out, "", out)

    def test_an_existing_directory_is_left_alone(self):
        self.assertEqual(_run([os.path.dirname(_SCRIPT)]).stdout, "")

    def test_a_missing_path_is_created(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "a", "b")
            out = _run([target]).stdout
            self.assertTrue(os.path.isdir(target))
            self.assertIn(f"Created host path {target}", out)


if __name__ == "__main__":
    unittest.main()
