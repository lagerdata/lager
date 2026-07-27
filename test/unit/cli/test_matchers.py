#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for ``cli/core/matchers.py`` -- the test-output matchers that
``lager python`` streams box output through, plus the v1 stream framing parser.

The matcher decides a command's EXIT CODE, so a defect here turns a failing
firmware test green. ``UnityMatcher`` and ``EndsWithMatcher`` are the two that
can report failure, and both parse across chunk boundaries -- output arrives in
arbitrarily-sized pieces, so a marker split across two ``feed()`` calls must
still be detected. That is the main thing these tests pin.

NOTE ON IMPORTS: this module is imported as ``matchers`` and its members are
reached through it. ``cli/core/matchers.py`` defines a module-level function
literally named ``test_matcher_factory``; importing that name directly into a
test module makes pytest collect it as a test case, which then errors on a
missing ``test_runner`` fixture.
"""

import os
import unittest
from unittest import mock

from cli.core import matchers


class FakeIO:
    """Stand-in for the io object matchers write through."""

    def __init__(self):
        self.writes = []
        self.primary = None

    def output(self, data, fg=None, flush=False):
        self.writes.append((data, fg))

    @property
    def written(self):
        return b''.join(w for w, _ in self.writes if isinstance(w, bytes))


class MatcherFactoryTests(unittest.TestCase):

    def test_none_and_the_string_none_both_give_EmptyMatcher(self):
        self.assertIs(matchers.test_matcher_factory(None), matchers.EmptyMatcher)
        self.assertIs(matchers.test_matcher_factory('none'), matchers.EmptyMatcher)

    def test_named_matchers(self):
        for name, expected in (('unity', matchers.UnityMatcher),
                               ('fixture', matchers.FixtureMatcher),
                               ('ptty', matchers.PTTYMatcher)):
            with self.subTest(name=name):
                self.assertIs(matchers.test_matcher_factory(name), expected)

    def test_endswith_prefix(self):
        self.assertIs(matchers.test_matcher_factory('endswith:DONE'),
                      matchers.EndsWithMatcher)

    def test_unknown_runner_raises(self):
        with self.assertRaises(ValueError) as ctx:
            matchers.test_matcher_factory('nope')
        self.assertIn('nope', str(ctx.exception))

    def test_endswith_without_colon_is_unknown(self):
        with self.assertRaises(ValueError):
            matchers.test_matcher_factory('endswith')

    def test_every_matcher_shares_the_three_arg_constructor(self):
        """All matchers are constructed identically by the caller."""
        io = FakeIO()
        for cls in (matchers.EmptyMatcher, matchers.UnityMatcher,
                    matchers.EndsWithMatcher, matchers.FixtureMatcher,
                    matchers.PTTYMatcher):
            with self.subTest(cls=cls.__name__):
                m = cls(io, None, None)
                self.assertTrue(hasattr(m, 'feed'))
                self.assertTrue(hasattr(m, 'done'))
                self.assertEqual(m.exit_code, 0)


class SafeDecodeTests(unittest.TestCase):

    def test_valid_utf8(self):
        self.assertEqual(matchers.safe_decode(b'hello'), 'hello')

    def test_invalid_utf8_falls_back_to_escaped_repr(self):
        self.assertEqual(matchers.safe_decode(b'\xff\xfe'), '\\xff\\xfe')

    def test_empty(self):
        self.assertEqual(matchers.safe_decode(b''), '')

    def test_multibyte_utf8_survives(self):
        self.assertEqual(matchers.safe_decode('café'.encode()), 'café')


class EchoLineTests(unittest.TestCase):

    def test_decodable_line_is_colored(self):
        with mock.patch.object(matchers.click, 'secho') as secho:
            matchers.echo_line(b'hello', 'green')
        secho.assert_called_once_with('hello', fg='green')

    def test_undecodable_line_is_emitted_raw(self):
        with mock.patch.object(matchers.click, 'secho') as secho, \
             mock.patch.object(matchers.click, 'echo') as echo:
            matchers.echo_line(b'\xff\xfe', 'green')
        secho.assert_not_called()
        echo.assert_called_once_with(b'\xff\xfe')


class UnityMatcherTests(unittest.TestCase):
    """Unity is the matcher that can fail a build."""

    def setUp(self):
        self.io = FakeIO()
        self.m = matchers.UnityMatcher(self.io, None, None)

    def test_clean_run_exits_zero(self):
        self.m.feed(b'test_foo:PASS\n')
        self.assertEqual(self.m.exit_code, 0)

    def test_a_fail_marker_sets_exit_code_one(self):
        self.m.feed(b'test_foo:FAIL: expected 1 was 2\n')
        self.assertEqual(self.m.exit_code, 1)

    def test_failure_is_sticky(self):
        """A later PASS must not clear an earlier FAIL."""
        self.m.feed(b'test_a:FAIL\n')
        self.m.feed(b'test_b:PASS\n')
        self.assertEqual(self.m.exit_code, 1)

    def test_marker_split_across_feeds_is_still_detected(self):
        """Output arrives in arbitrary chunks; the FAIL here spans two.

        This is the regression that matters: buffering that only inspected the
        current chunk would report success on a failing run.
        """
        self.m.feed(b'test_foo:FA')
        self.assertEqual(self.m.exit_code, 0)   # nothing complete yet
        self.m.feed(b'IL: boom\n')
        self.assertEqual(self.m.exit_code, 1)

    def test_incomplete_trailing_line_is_held_not_emitted(self):
        with mock.patch.object(matchers.click, 'echo') as echo:
            self.m.feed(b'partial line without newline')
        echo.assert_not_called()
        self.assertEqual(self.m.state, b'partial line without newline')

    def test_fail_inside_a_longer_line_still_counts(self):
        self.m.feed(b'some/path/test_x.c:42:test_x:FAIL: nope\n')
        self.assertEqual(self.m.exit_code, 1)

    def test_summary_separator_switches_to_summary_mode(self):
        self.m.feed(matchers.UnityMatcher.summary_separator + b'\n')
        self.assertTrue(self.m.in_summary)

    def test_summary_is_green_when_clean_and_red_after_a_failure(self):
        for feed_first, expected in ((b'', 'green'), (b'test:FAIL\n', 'red')):
            with self.subTest(expected=expected):
                io = FakeIO()
                m = matchers.UnityMatcher(io, None, None)
                if feed_first:
                    m.feed(feed_first)
                m.feed(matchers.UnityMatcher.summary_separator + b'\n')
                with mock.patch.object(matchers, 'echo_line') as echo_line:
                    m.feed(b'1 Tests 0 Failures\n')
                echo_line.assert_called_once_with(b'1 Tests 0 Failures', expected)

    def test_done_is_a_noop(self):
        self.assertIsNone(self.m.done())


class EndsWithMatcherTests(unittest.TestCase):

    def test_failure_regex_sets_exit_code(self):
        m = matchers.EndsWithMatcher(FakeIO(), None, 'ERROR')
        m.feed(b'something ERROR here\n')
        self.assertEqual(m.exit_code, 1)

    def test_success_regex_does_not_change_exit_code(self):
        m = matchers.EndsWithMatcher(FakeIO(), 'OK', None)
        m.feed(b'all OK\n')
        self.assertEqual(m.exit_code, 0)

    def test_failure_wins_over_success_on_the_same_line(self):
        """`failed` short-circuits the success check."""
        io = FakeIO()
        m = matchers.EndsWithMatcher(io, 'OK', 'ERROR')
        m.feed(b'OK but ERROR\n')
        self.assertEqual(m.exit_code, 1)
        self.assertEqual(io.writes[0][1], 'red')

    def test_no_regexes_means_everything_passes_through(self):
        io = FakeIO()
        m = matchers.EndsWithMatcher(io, None, None)
        m.feed(b'anything\n')
        self.assertEqual(m.exit_code, 0)
        self.assertEqual(io.writes[0], (b'anything', None))

    def test_marker_split_across_feeds_is_detected(self):
        m = matchers.EndsWithMatcher(FakeIO(), None, 'ERROR')
        m.feed(b'lead ERR')
        self.assertEqual(m.exit_code, 0)
        m.feed(b'OR trail\n')
        self.assertEqual(m.exit_code, 1)

    def test_partial_line_is_buffered_until_newline(self):
        io = FakeIO()
        m = matchers.EndsWithMatcher(io, None, None)
        m.feed(b'no newline yet')
        self.assertEqual(io.writes, [])
        self.assertEqual(m.state, b'no newline yet')

    def test_done_flushes_the_buffered_remainder(self):
        """Without this, output with no trailing newline is lost."""
        io = FakeIO()
        m = matchers.EndsWithMatcher(io, None, None)
        m.feed(b'dangling')
        m.done()
        self.assertEqual(io.writes[-1][0], b'dangling')

    def test_done_emits_nothing_when_buffer_is_empty(self):
        io = FakeIO()
        m = matchers.EndsWithMatcher(io, None, None)
        m.feed(b'complete\n')
        before = len(io.writes)
        m.done()
        self.assertEqual(len(io.writes), before)

    def test_multiple_lines_in_one_feed(self):
        io = FakeIO()
        m = matchers.EndsWithMatcher(io, None, None)
        m.feed(b'a\nb\nc\n')
        payloads = [w for w, _ in io.writes if w != b'\n']
        self.assertEqual(payloads[:3], [b'a', b'b', b'c'])

    def test_feed_ending_on_a_newline_emits_a_spurious_blank_line(self):
        """Pins a real defect, and deliberately does not paper over it.

        When ``data`` ends with b'\\n', split() leaves a trailing b'' that the
        loop still emits, followed by its own b'\\n'. So every chunk that lands
        on a line boundary adds a blank line to the user's output -- and since
        box output arrives in arbitrarily many chunks, the padding accumulates.

        The fix is to drop the trailing element in the endswith branch the same
        way the else branch already does. Left alone here because this change
        is test-only and the matcher decides exit codes; it is written up in
        test/COVERAGE.md instead.
        """
        io = FakeIO()
        m = matchers.EndsWithMatcher(io, None, None)
        m.feed(b'a\n')
        self.assertEqual([w for w, _ in io.writes], [b'a', b'\n', b'', b'\n'])

    def test_feed_not_ending_on_a_newline_does_not_pad(self):
        """The else branch drops the remainder correctly -- contrast above."""
        io = FakeIO()
        m = matchers.EndsWithMatcher(io, None, None)
        m.feed(b'a\nb')
        self.assertEqual([w for w, _ in io.writes], [b'a', b'\n'])
        self.assertEqual(m.state, b'b')

    def test_matched_lines_are_decoded_unmatched_stay_bytes(self):
        """Only a matched line goes through safe_decode."""
        io = FakeIO()
        m = matchers.EndsWithMatcher(io, 'OK', None)
        m.feed(b'plain\nthis is OK\n')
        payloads = [w for w, _ in io.writes if w != b'\n']
        self.assertEqual(payloads[0], b'plain')
        self.assertEqual(payloads[1], 'this is OK')

    def test_undecodable_matched_line_does_not_raise(self):
        io = FakeIO()
        m = matchers.EndsWithMatcher(io, None, 'ERR')
        m.feed(b'\xff\xfe ERR\n')
        self.assertEqual(m.exit_code, 1)


class EmptyAndPttyMatcherTests(unittest.TestCase):

    def test_empty_matcher_passes_data_straight_through(self):
        io = FakeIO()
        m = matchers.EmptyMatcher(io, None, None)
        m.feed(b'raw bytes')
        self.assertEqual(io.writes, [(b'raw bytes', None)])

    def test_ptty_matcher_writes_to_the_pty_fd(self):
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        io = FakeIO()
        io.primary = write_fd
        m = matchers.PTTYMatcher(io, None, None)
        m.feed(b'hi')
        os.close(write_fd)
        self.assertEqual(os.read(read_fd, 16), b'hi')


class FixtureMatcherTests(unittest.TestCase):

    def test_uart_rx_frame_is_rendered_with_its_channel(self):
        io = FakeIO()
        m = matchers.FixtureMatcher(io, None, None)
        m.got_frame(bytes([0x5A, 0x02]) + b'hello')
        self.assertEqual(io.writes[0][0], b'UART 2> hello\r\n')

    def test_non_uart_frame_is_ignored(self):
        io = FakeIO()
        m = matchers.FixtureMatcher(io, None, None)
        m.got_frame(bytes([0x01, 0x02]) + b'payload')
        self.assertEqual(io.writes, [])

    def test_feed_pushes_bytes_into_the_hdlc_decoder(self):
        io = FakeIO()
        m = matchers.FixtureMatcher(io, None, None)
        with mock.patch.object(m.hdlc, '_readByte') as read_byte:
            m.feed(b'abc')
        self.assertEqual([c.args[0] for c in read_byte.call_args_list],
                         [ord('a'), ord('b'), ord('c')])


class FakeResponse:
    """Minimal stand-in for a requests response streaming one byte at a time."""

    def __init__(self, payload):
        self._payload = payload

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self._payload), chunk_size):
            yield self._payload[i:i + chunk_size]


class IterStreamsTests(unittest.TestCase):
    """The v1 wire format: `<fileno> <length> <payload>`, repeated."""

    def test_single_stdout_chunk(self):
        self.assertEqual(list(matchers.iter_streams(FakeResponse(b'1 5 hello'))),
                         [(1, b'hello')])

    def test_two_chunks_back_to_back(self):
        self.assertEqual(
            list(matchers.iter_streams(FakeResponse(b'1 5 hello2 5 world'))),
            [(1, b'hello'), (2, b'world')])

    def test_stderr_fileno(self):
        self.assertEqual(list(matchers.iter_streams(FakeResponse(b'2 3 err'))),
                         [(2, b'err')])

    def test_negative_fileno_is_a_bare_dash_not_the_digits_minus_one(self):
        """The wire format for fileno -1 is a single '-' character.

        Non-obvious and worth pinning: the parser consumes exactly ONE byte for
        the fileno, so it is `- 2 hi`, not `-1 2 hi`. Feeding the two-character
        form makes the '1' get eaten as the separator and the length parse
        explode on an empty string (asserted below), which is a confusing way
        to find out.
        """
        self.assertEqual(list(matchers.iter_streams(FakeResponse(b'- 2 hi'))),
                         [(-1, b'hi')])

    def test_two_character_negative_fileno_is_a_parse_error(self):
        with self.assertRaises(ValueError):
            list(matchers.iter_streams(FakeResponse(b'-1 2 hi')))

    def test_zero_length_chunk_yields_nothing_and_resets(self):
        self.assertEqual(
            list(matchers.iter_streams(FakeResponse(b'1 0 1 2 ok'))),
            [(1, b'ok')])

    def test_multi_digit_length(self):
        payload = b'x' * 12
        self.assertEqual(list(matchers.iter_streams(FakeResponse(b'1 12 ' + payload))),
                         [(1, payload)])

    def test_truncated_stream_yields_nothing(self):
        """A chunk that never completes must not yield a short read."""
        self.assertEqual(list(matchers.iter_streams(FakeResponse(b'1 10 short'))), [])

    def test_payload_may_contain_spaces_and_digits(self):
        self.assertEqual(list(matchers.iter_streams(FakeResponse(b'1 7 a 1 2 b'))),
                         [(1, b'a 1 2 b')])

    def test_content_branch_is_a_truthiness_check_not_a_comparison(self):
        """Pins a latent bug in iter_streams.

        Line 87 reads ``elif V1ParseStates.Content:`` -- it is missing the
        ``parse_state ==``, so it evaluates the enum MEMBER, which is always
        truthy. It behaves correctly today only because the three branches
        above it are exhaustive, leaving Content as the sole remaining state.

        Adding a sixth parser state would silently route it here. Not fixed in
        this test-only change, but pinned so the parser's correct behaviour is
        locked in before anyone edits the state machine.
        """
        import inspect
        src = inspect.getsource(matchers.iter_streams)
        self.assertIn('elif V1ParseStates.Content:', src,
                      'the latent bug this test documents appears to be fixed -- '
                      'delete this test and its note in test/COVERAGE.md')


if __name__ == '__main__':
    unittest.main()
