# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Turning a box-reported return code into a process exit status.

The box reports Python's `Popen.wait()` value, which is negative when the
process died of a signal. `SIGKILL_EXIT_CODE = 137` is written in the shell's
128+N convention, so a raw -9 matched nothing and `sys.exit(-9)` left the caller
looking at 247.

That became reachable when `--timeout` grew a `--kill-after`: GNU timeout puts
itself in the child's process group, so the SIGKILL it sends at the end of the
grace window kills the wrapper too, and the box sees -9 where a shell would have
shown 137. Measured end to end on a box: the deadline fired correctly and then
reported itself as 247 with no message, because `_do_exit` could not recognise
it.
"""

import unittest

from cli.core.utils import (
    FAILED_TO_RETRIEVE_EXIT_CODE,
    SIGKILL_EXIT_CODE,
    SIGTERM_EXIT_CODE,
    normalize_exit_code,
)


class NormalizeExitCode(unittest.TestCase):

    def test_success_is_untouched(self):
        self.assertEqual(normalize_exit_code(0), 0)

    def test_ordinary_failures_are_untouched(self):
        for raw in (1, 2, 42, 255):
            self.assertEqual(normalize_exit_code(raw), raw)

    def test_timeouts_own_code_is_untouched(self):
        # /usr/bin/timeout exits 124 normally when SIGTERM was enough, so this
        # never arrives negative.
        self.assertEqual(normalize_exit_code(SIGTERM_EXIT_CODE), SIGTERM_EXIT_CODE)

    def test_sigkill_becomes_the_code_the_cli_has_a_message_for(self):
        self.assertEqual(normalize_exit_code(-9), SIGKILL_EXIT_CODE)

    def test_other_signals_use_the_same_convention(self):
        self.assertEqual(normalize_exit_code(-15), 143)   # SIGTERM
        self.assertEqual(normalize_exit_code(-2), 130)     # SIGINT

    def test_minus_one_is_passed_through(self):
        # -1 is FAILED_TO_RETRIEVE_EXIT_CODE, and also what
        # exec.process.terminate_process returns when it had to kill something,
        # and also SIGHUP death. Those are already indistinguishable on the
        # wire; mapping it to 129 would invent a signal nobody sent.
        self.assertEqual(normalize_exit_code(-1), FAILED_TO_RETRIEVE_EXIT_CODE)

    def test_none_reads_as_failed_to_retrieve(self):
        self.assertEqual(normalize_exit_code(None), FAILED_TO_RETRIEVE_EXIT_CODE)

    def test_nothing_maps_onto_a_negative_number(self):
        """Whatever comes back, the CLI must not call sys.exit() with a
        negative value: the shell renders it as 256-N, which is what turned a
        SIGKILL into 247."""
        for raw in (0, 1, 124, 137, -2, -9, -15, -64, None):
            result = normalize_exit_code(raw)
            if result != FAILED_TO_RETRIEVE_EXIT_CODE:
                self.assertGreaterEqual(result, 0, f'{raw} -> {result}')


if __name__ == '__main__':
    unittest.main()
