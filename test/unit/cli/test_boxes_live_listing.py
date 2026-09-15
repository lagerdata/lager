#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for the concurrent `lager boxes` listing in
``cli/commands/box/boxes.py``, and for the thread safety in
``cli/gateway_auth.py`` that the fan-out depends on.

The listing used to probe boxes one at a time behind a spinner, so a single
powered-down box delayed every other row by its full timeout and nothing
printed until the last box answered. It now probes them together and renders
as answers arrive. What lives only here:

  * the fan-out is genuinely concurrent. Asserted with a `threading.Barrier`
    rather than a stopwatch: all boxes must be in flight at once for the
    barrier to trip, and a sequential regression fails it deterministically
    instead of flaking on a slow runner.
  * a box that never answers is abandoned at the deadline and named, rather
    than hanging the command. This is the powered-off / mid-update case.
  * gateway (Stout) denials stay counted apart from unreachable boxes, for
    every label `gateway_auth.denial_label` can return -- including
    'token rejected', which a label allow-list would have mistakenly counted
    as an unreachable box.
  * versions reach the on-disk cache in their bare form and from the calling
    thread. `update_box_version` read-modify-writes ~/.lager, so doing it on
    the workers would drop edits; and the display form can carry a
    "(branch)" annotation that must never be cached.
  * `access_token_for` is single-flight. Stout rotates the refresh cookie on
    every successful refresh, so N workers refreshing one near-expiry token
    would spend it N times and log the user out. Exactly one refresh must
    reach the wire.
  * the store is written atomically, so a concurrent reader never parses a
    half-written file as "no session".

Nothing here opens a socket: `requests.get`/`requests.post` are patched on
the modules under test.
"""

import base64
import json
import os
import threading
import time
import unittest
from importlib import import_module
from unittest import mock

import requests

boxes_mod = import_module('cli.commands.box.boxes')
gateway_auth = import_module('cli.gateway_auth')

CLI_VERSION = import_module('cli').__version__


def make_response(status, body=None, headers=None, request_headers=None):
    """A requests.Response with a JSON body and an attached prepared request,
    which is the shape the gateway check inspects."""
    resp = requests.Response()
    resp.status_code = status
    resp.headers.update(headers or {})
    resp._content = json.dumps(body if body is not None else {}).encode()
    prepared = requests.PreparedRequest()
    prepared.method, prepared.url, prepared.body = 'GET', 'http://box/x', None
    prepared.headers = dict(request_headers or {})
    resp.request = prepared
    return resp


def make_jwt(exp, iat=None):
    def seg(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b'=').decode()
    return f"{seg({'alg': 'none'})}.{seg({'exp': exp, 'iat': iat or (exp - 900)})}.sig"


class BoxesListingTestCase(unittest.TestCase):
    """Drives ``_list_boxes_live`` directly, the way the click group does."""

    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        # Shrink every budget so the deadline paths run in milliseconds. The
        # production values are seconds-scale on purpose.
        mock.patch.object(boxes_mod, '_LOCK_TIMEOUT', 0.1).start()
        mock.patch.object(boxes_mod, '_STRAGGLER_GRACE', 0.3).start()
        mock.patch.object(boxes_mod, '_POLL_INTERVAL', 0.02).start()
        # Version caching writes ~/.lager; record the calls instead of doing
        # it, along with the thread each came from.
        self.cached = []

        def record(name, version):
            self.cached.append((name, version, threading.current_thread().name))

        # `_list_boxes_live` imports it from box_storage at call time, so
        # patch it where it is looked up.
        mock.patch.object(import_module('cli.box_storage'),
                          'update_box_version', new=record).start()

    def _patch_storage(self, boxes):
        mock.patch.object(boxes_mod, 'list_boxes', return_value=boxes).start()

    def _run(self, boxes, fake_get):
        self._patch_storage(boxes)
        mock.patch.object(requests, 'get', new=fake_get).start()
        with mock.patch('sys.stdout.isatty', return_value=False):
            from click.testing import CliRunner
            runner = CliRunner()
            # Invoke through the group so the real entry path is covered.
            result = runner.invoke(boxes_mod.boxes, [])
        self.assertIsNone(result.exception, msg=result.output)
        return result.output

    def test_all_boxes_are_probed_concurrently(self):
        # Every box must be in flight at once for the barrier to trip, so a
        # sequential regression fails this deterministically rather than
        # flaking on a slow runner.
        #
        # The barrier gates /status, not /lock: a failure on the lock call is
        # deliberately swallowed (lock state is decoration), so a barrier
        # there would break without changing any row and the test would pass
        # either way.
        names = {f'BOX-{i}': f'10.0.0.{i}' for i in range(4)}
        barrier = threading.Barrier(len(names))

        def fake_get(url, timeout=None, headers=None):
            if url.endswith('/lock'):
                return make_response(200, {'locked': False})
            barrier.wait(timeout=10)
            return make_response(200, {'version': CLI_VERSION})

        out = self._run(names, fake_get)
        self.assertEqual(out.count('current'), len(names), msg=out)
        self.assertNotIn('error', out)

    def test_one_dead_box_does_not_hide_the_others(self):
        # The original failure: nothing printed until the slowest box
        # answered. A box that never replies must not stop the rest being
        # reported, and must be named rather than dropped.
        release = threading.Event()
        self.addCleanup(release.set)

        def fake_get(url, timeout=None, headers=None):
            if '10.0.0.9' in url:
                release.wait(timeout=30)      # never answers within the test
                raise requests.exceptions.ConnectionError('released')
            if url.endswith('/lock'):
                return make_response(200, {'locked': False})
            return make_response(200, {'version': CLI_VERSION})

        out = self._run({'GOOD': '10.0.0.1', 'DEAD': '10.0.0.9'}, fake_get)

        self.assertIn('current', out)                  # the healthy box reported
        self.assertIn('no response', out)              # the dead one is named
        self.assertIn('1 box did not report a version', out)

    def test_the_gateway_retry_is_held_to_each_probes_budget(self):
        # A gated box's first contact is retried inside check_gateway_status,
        # and that retry used to get a fixed 30s regardless of what the
        # caller allowed. The collect loop abandons a box at its deadline, so
        # a retry outliving it made us label an answering box 'no response'.
        budgets = []

        def fake_get(url, timeout=None, headers=None):
            body = {'locked': False} if url.endswith('/lock') \
                else {'version': CLI_VERSION}
            resp = make_response(200, body)
            resp.request.url = url          # so the spy can tell them apart
            return resp

        def spy(resp, ip, *, timeout=None, stream=None):
            endpoint = resp.request.url.rsplit('/', 1)[-1]
            budgets.append((endpoint, timeout, stream))
            return resp, None

        mock.patch.object(import_module('cli.box_storage'),
                          'check_gateway_status', new=spy).start()

        self._run({'GATED': '10.0.0.1'}, fake_get)

        # stream=False mirrors the buffered probe calls, as the retry demands.
        self.assertEqual(budgets, [
            ('lock', boxes_mod._LOCK_TIMEOUT, False),
            ('status', boxes_mod._DEFAULT_STATUS_TIMEOUT, False),
        ])

    def test_boxes_with_no_ip_need_no_network(self):
        def fake_get(url, timeout=None, headers=None):
            raise AssertionError(f'should not have been called: {url}')

        out = self._run({'NOIP': {'user': 'charles'}}, fake_get)
        self.assertIn('no IP', out)

    def test_gateway_denials_are_counted_apart_from_unreachable_boxes(self):
        # Each of these is a Stout verdict, not an unreachable box, so none of
        # them may land in the "did not report a version" tally. 'token
        # rejected' is the one an allow-list of labels would have missed.
        gated = {'A': '10.1.0.1', 'B': '10.1.0.2', 'C': '10.1.0.3'}
        codes = {'10.1.0.1': 403, '10.1.0.2': 503, '10.1.0.3': 401}

        def fake_get(url, timeout=None, headers=None):
            ip = url.split('//')[1].split(':')[0]
            return make_response(
                codes[ip], {'error': 'denied'},
                headers={gateway_auth.DISCOVERY_HEADER: 'http://stout'},
                request_headers=headers,
            )

        out = self._run(gated, fake_get)

        self.assertIn('no access', out)                 # 403
        self.assertIn('auth server down', out)          # 503
        self.assertIn('sign-in required', out)          # 401, no credential
        self.assertIn('3 boxes need sign-in or an access grant', out)
        self.assertNotIn('did not report a version', out)

    def test_only_the_bare_version_is_cached(self):
        # The version cell can read "0.1.0 (my-branch)" so a box left on a
        # branch is visible in a fleet listing, but caching that string would
        # put an unparseable version in ~/.lager.
        def fake_get(url, timeout=None, headers=None):
            if url.endswith('/lock'):
                return make_response(200, {'locked': False})
            return make_response(200, {'version': '0.1.0', 'ref': 'my-branch'})

        out = self._run({'BRANCHED': '10.2.0.1'}, fake_get)

        self.assertIn('0.1.0 (my-branch)', out)
        self.assertEqual([(n, v) for n, v, _ in self.cached],
                         [('BRANCHED', '0.1.0')])

    def test_versions_are_cached_from_the_calling_thread(self):
        # update_box_version read-modify-writes ~/.lager. Called from the
        # workers, concurrent boxes would drop each other's edits.
        main_thread = threading.current_thread().name

        def fake_get(url, timeout=None, headers=None):
            if url.endswith('/lock'):
                return make_response(200, {'locked': False})
            return make_response(200, {'version': CLI_VERSION})

        self._run({f'B{i}': f'10.3.0.{i}' for i in range(3)}, fake_get)

        threads_used = [t for _, _, t in self.cached]
        self.assertEqual(len(threads_used), 3)
        self.assertEqual(set(threads_used), {main_thread})

    def test_non_tty_output_carries_no_live_scaffolding(self):
        # Piped into a file or a CI log, the output must be the plain final
        # table: no pending rows, no countdown, no cursor escapes.
        def fake_get(url, timeout=None, headers=None):
            if url.endswith('/lock'):
                return make_response(200, {'locked': False})
            return make_response(200, {'version': CLI_VERSION})

        out = self._run({'BOX': '10.4.0.1'}, fake_get)

        self.assertNotIn('pending', out)
        self.assertNotIn('Ctrl+C', out)
        self.assertNotIn('\033[', out)
        self.assertIn('current', out)


class LiveTableTestCase(unittest.TestCase):
    """The repaint arithmetic, which is what corrupts a terminal when wrong."""

    def _table(self, boxes, cols=80, lines=40):
        table = boxes_mod._LiveTable(boxes, countdown_to=time.monotonic() + 5)
        table._live = True
        mock.patch('shutil.get_terminal_size',
                   return_value=os.terminal_size((cols, lines))).start()
        self.addCleanup(mock.patch.stopall)
        return table

    def _painted_lines(self, table):
        import re
        written = []
        with mock.patch('sys.stdout.write', side_effect=written.append), \
                mock.patch('sys.stdout.flush'):
            table.paint()
        plain = re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', ''.join(written))
        # A carriage return also begins a fresh terminal line.
        return [l for l in re.split(r'[\r\n]', plain) if l]

    def test_every_live_line_fits_the_window(self):
        # A line wider than the window wraps onto a second terminal row,
        # while the repaint rewinds by the number of lines written -- the
        # mismatch is what shreds the display.
        #
        # Swept across widths on purpose. At a single width the row prefix is
        # either comfortably inside the window or already past it, and the
        # interesting case -- the status cell straddling the right edge -- is
        # only reached at the widths in between.
        boxes = [('BOX-ALPHA', '10.0.0.100', 'charles'),
                 ('BOX-BETA', '10.0.0.2', 'charles')]
        for cols in range(20, 81):
            with self.subTest(cols=cols):
                mock.patch.stopall()
                table = self._table(boxes, cols=cols)
                table.record(boxes_mod._Row(
                    'BOX-BETA', '10.0.0.2', 'charles', '0.1.0', 'auth server down'))
                for line in self._painted_lines(table):
                    self.assertLessEqual(len(line), cols, msg=repr(line))

    def test_a_fleet_taller_than_the_terminal_disables_the_live_path(self):
        # The top of an over-tall block has already scrolled off, so cursor-up
        # would land on the wrong rows. Print once at the end instead.
        boxes = [(f'BOX-{i}', '10.0.0.1', 'charles') for i in range(30)]
        with mock.patch('sys.stdout.isatty', return_value=True), \
                mock.patch('shutil.get_terminal_size',
                           return_value=os.terminal_size((80, 10))):
            table = boxes_mod._LiveTable(boxes, countdown_to=time.monotonic() + 5)
        self.assertFalse(table._live)

    def test_a_settled_table_stops_repainting(self):
        # Once nothing is outstanding the wheel stops, so the frame is stable
        # and the loop must not keep rewriting the same block.
        boxes = [('A', '10.0.0.1', 'charles')]
        table = self._table(boxes)
        table.record(boxes_mod._Row('A', '10.0.0.1', 'charles', '0.1.0', 'current'))

        written = []
        with mock.patch('sys.stdout.write', side_effect=written.append), \
                mock.patch('sys.stdout.flush'):
            table.paint()
            first = len(written)
            table.paint()          # nothing changed, so nothing is written
            self.assertEqual(len(written), first)

    def test_the_wheel_visits_every_glyph_in_order(self):
        # Rewinding `_started` is how the clock is driven here: no sleeping,
        # and no patching of time.monotonic, which the footer and the collect
        # loop also read.
        boxes = [('A', '10.0.0.1', 'charles')]
        table = self._table(boxes)

        seen = []
        for step in range(len(table._SPINNER)):
            table._started = time.monotonic() - step * table._SPINNER_PERIOD
            seen.append(table._spinner())

        self.assertEqual(''.join(seen), table._SPINNER)

    def test_an_outstanding_box_keeps_repainting_as_the_wheel_turns(self):
        # A pending row has no status to report, so the advancing wheel is
        # the only sign the command is alive. Were the frame treated as
        # unchanged, it would freeze for the whole wait.
        boxes = [('A', '10.0.0.1', 'charles')]
        table = self._table(boxes, cols=200)

        first = self._painted_lines(table)
        table._started -= table._SPINNER_PERIOD      # advance the wheel one step
        second = self._painted_lines(table)

        self.assertTrue(second, 'the wheel advanced but nothing was repainted')
        pending_line = next(l for l in second if 'pending' in l)
        self.assertNotEqual(next(l for l in first if 'pending' in l), pending_line)

    def test_locked_by_is_present_from_the_first_frame(self):
        # The column has to be there before any box answers: introducing it
        # later would shift every row underneath it mid-wait.
        boxes = [('A', '10.0.0.1', 'charles')]
        table = self._table(boxes, cols=200)

        header = self._painted_lines(table)[0]
        self.assertIn('locked by', header)

    def test_a_resolved_row_shows_its_lock_holder_and_drops_the_wheel(self):
        boxes = [('A', '10.0.0.1', 'charles')]
        table = self._table(boxes, cols=200)
        table.record(boxes_mod._Row('A', '10.0.0.1', 'charles', '0.1.0',
                                    'current', 'alice'))

        body = self._painted_lines(table)[2]
        self.assertIn('alice', body)
        self.assertNotIn('pending', body)
        for glyph in table._SPINNER:
            self.assertNotIn(glyph, body)

    def test_erase_rewinds_exactly_what_it_painted(self):
        boxes = [('A', '10.0.0.1', 'charles'), ('B', '10.0.0.2', 'charles')]
        table = self._table(boxes)
        with mock.patch('sys.stdout.write'), mock.patch('sys.stdout.flush'):
            table.paint()
            painted = table._painted
            written = []
            with mock.patch('sys.stdout.write', side_effect=written.append):
                table.erase()
        # header + rule + 2 rows + footer
        self.assertEqual(painted, 5)
        self.assertEqual(''.join(written).count('\033[1A'), painted)
        self.assertEqual(table._painted, 0)


class GatewayAuthConcurrencyTestCase(unittest.TestCase):
    """Thread safety the fan-out relies on."""

    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = os.path.join(self.tmp.name, 'gateway_auth.json')
        mock.patch.dict(os.environ, {'LAGER_GATEWAY_AUTH_FILE': self.store},
                        clear=False).start()
        os.environ.pop(gateway_auth.PINNED_TOKEN_ENV, None)

    def test_one_refresh_serves_every_concurrent_caller(self):
        # Stout rotates the refresh cookie on every successful refresh. N
        # threads each spending the same near-expiry cookie means one wins and
        # the rest rotate it out from under it, logging the user out -- from
        # the very command they ran.
        url = 'http://stout'
        gateway_auth.save_login(url, make_jwt(time.time() + 1), {'refresh': 'r0'})

        calls = []
        fresh = make_jwt(time.time() + 3600)

        def fake_post(post_url, cookies=None, timeout=None):
            calls.append(cookies)
            time.sleep(0.05)          # widen the window for a storm to happen
            return make_response(200, {'accessToken': fresh})

        mock.patch.object(requests, 'post', new=fake_post).start()

        got = []
        threads = [threading.Thread(target=lambda: got.append(
            gateway_auth.access_token_for(url))) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(len(calls), 1, msg=f'{len(calls)} refreshes reached the wire')
        self.assertEqual(set(got), {fresh})

    def test_concurrent_discovery_writes_do_not_drop_entries(self):
        # Every mutator read-modify-writes the whole store, so unsynchronised
        # writers silently lose each other's entries.
        ips = [f'10.9.0.{i}' for i in range(25)]
        threads = [threading.Thread(target=gateway_auth.record_box_auth_server,
                                    args=(ip, 'http://stout')) for ip in ips]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        recorded = gateway_auth._load_store().get('boxes', {})
        self.assertEqual(sorted(recorded), sorted(ips))

    def test_the_store_is_never_left_half_written(self):
        # A reader that catches the old truncate-then-write mid-flight parses
        # an empty file as "no session" and re-prompts for a login the user
        # already has.
        url = 'http://stout'
        gateway_auth.save_login(url, make_jwt(time.time() + 3600), {'refresh': 'r0'})

        stop = threading.Event()
        bad = []

        def reader():
            while not stop.is_set():
                if not gateway_auth._load_store().get('authServers'):
                    bad.append('store read as empty')

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        for i in range(200):
            gateway_auth.save_login(url, make_jwt(time.time() + 3600 + i),
                                    {'refresh': f'r{i}'})
        stop.set()
        t.join(timeout=5)

        self.assertEqual(bad, [])
        # No temp files survive a successful run.
        leftovers = [f for f in os.listdir(self.tmp.name) if '.tmp' in f]
        self.assertEqual(leftovers, [])

    def test_the_store_keeps_owner_only_permissions(self):
        gateway_auth.save_login('http://stout', make_jwt(time.time() + 3600),
                                {'refresh': 'r0'})
        self.assertEqual(os.stat(self.store).st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
