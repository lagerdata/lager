# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Every box must carry a daemon built from the Rust it is actually running.

The daemon was the only compiled artifact on a box that nothing in the
deploy path built. Sources arrived with every `git pull` and were then
ignored: the binary came from running `build_daemon.sh` by hand and `scp`ing
the result to `~/third_party/oscilloscope-daemon`. So a box could sit on a
commit whose Rust it had never compiled, nothing reported the gap, and a
freshly provisioned box had no daemon at all -- plugging in a PicoScope did
nothing, which is the failure this exists to prevent.

The build itself lives in `start_box.sh`, because that is the one path
`lager install`, `lager update`, `box config apply` and a manual restart all
share. What `update.py` owns is narrower and tested here: noticing that the
daemon is stale, so `lager update` cannot take its "already up to date"
early exit and skip start_box.sh entirely.

Two properties are load-bearing here, and both are easy to break by
"simplifying" the hash bookkeeping:

  * **The daemon hash is separate from the build hash.** box.Dockerfile does
    not COPY the daemon; start_box.sh bind-mounts it. Folding the Rust tree
    into `_BUILD_HASH_SOURCE_DIRS` would make every Rust edit wipe the ~1 GB
    image and its layer cache to reinstall a 4 MB binary the image never
    contained, turning a one-minute cargo build into a full from-scratch
    rebuild.

  * **`target/` is excluded from the hash.** It is build output, so hashing
    it would mean building changes the hash, every subsequent run sees a
    mismatch, and the box rebuilds the daemon forever.

The PicoTech headers are hashed with the sources. build.rs builds each
PicoScope family whose headers are installed and leaves the others out, so a
box with only libps2000 gets a daemon for its 2204A -- and a box that later
gains another family has to rebuild to drive it.

The shell snippets are executed here rather than string-matched, because
what matters is the digest they produce on a real tree.
"""

import importlib
import os
import shutil
import subprocess
import tempfile
import unittest

_update = importlib.import_module('cli.commands.utility.update')

_daemon_hash_shell_cmd = _update._daemon_hash_shell_cmd
_daemon_needs_build = _update._daemon_needs_build
_rebuild_gate_verdict = _update._rebuild_gate_verdict


def _repo_file(*parts):
    here = os.path.abspath(__file__)          # test/unit/cli/<this file>
    repo = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(here))))
    return os.path.join(repo, *parts)


def _start_box_function(name):
    """One function's definition from start_box.sh, to run in isolation."""
    with open(_repo_file('box', 'start_box.sh')) as handle:
        text = handle.read()
    body = text[text.index(f'{name}() {{'):]
    return body[:body.index('\n}\n') + 3]


class DaemonHashCoversRustSources(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.src = os.path.join(self.home, 'box', 'oscilloscope-daemon')
        os.makedirs(os.path.join(self.src, 'daemon', 'src'))
        self.write(os.path.join(self.src, 'Cargo.toml'), '[workspace]\n')
        self.write(os.path.join(self.src, 'daemon', 'src', 'main.rs'),
                   'fn main() {}\n')
        # A stand-in for /opt/picoscope/include, so the machine running the
        # tests cannot change what they hash.
        self.sdk = os.path.join(self.home, 'picoscope-include')
        self.write(os.path.join(self.sdk, 'libps2000', 'ps2000.h'), '/* h */\n')

    def write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as handle:
            handle.write(text)

    def hash(self):
        result = subprocess.run(
            ['bash', '-c', _daemon_hash_shell_cmd(self.sdk)],
            env=dict(os.environ, HOME=self.home),
            capture_output=True, text=True,
        )
        return result.stdout.strip()

    def test_produces_a_hash(self):
        self.assertRegex(self.hash(), r'^[0-9a-f]{64}$')

    def test_stable_across_runs(self):
        self.assertEqual(self.hash(), self.hash())

    def test_editing_rust_changes_the_hash(self):
        before = self.hash()
        self.write(os.path.join(self.src, 'daemon', 'src', 'main.rs'),
                   'fn main() { println!("hi"); }\n')
        self.assertNotEqual(before, self.hash())

    def test_adding_a_file_changes_the_hash(self):
        before = self.hash()
        self.write(os.path.join(self.src, 'daemon', 'src', 'scope.rs'),
                   'pub fn scope() {}\n')
        self.assertNotEqual(before, self.hash())

    def test_deleting_a_file_changes_the_hash(self):
        before = self.hash()
        os.remove(os.path.join(self.src, 'daemon', 'src', 'main.rs'))
        self.assertNotEqual(before, self.hash())

    def test_renaming_a_file_changes_the_hash(self):
        """sha256sum prints the path, so a pure rename must register."""
        before = self.hash()
        os.rename(os.path.join(self.src, 'daemon', 'src', 'main.rs'),
                  os.path.join(self.src, 'daemon', 'src', 'entry.rs'))
        self.assertNotEqual(before, self.hash())

    def test_build_output_is_excluded(self):
        """Otherwise building changes the hash and the box rebuilds forever."""
        before = self.hash()
        self.write(os.path.join(self.src, 'target', 'release', 'daemon'),
                   'ELF\n')
        self.write(os.path.join(self.src, 'target', 'CACHEDIR.TAG'), 'x\n')
        self.assertEqual(before, self.hash())

    def test_absent_tree_yields_empty_not_a_digest_of_nothing(self):
        """A box whose sparse checkout predates the daemon must read empty.

        An empty pipe into sha256sum would hash the empty string and produce
        e3b0c442..., a perfectly stable digest that would then be compared
        against and stored as though it described real sources.
        """
        shutil.rmtree(os.path.join(self.home, 'box'))
        self.assertEqual(self.hash(), '')

    def test_installing_a_family_changes_the_hash(self):
        """Or a box that gains a family keeps the daemon built without it."""
        before = self.hash()
        self.write(os.path.join(self.sdk, 'libps5000a', 'ps5000aApi.h'),
                   '/* h */\n')
        self.assertNotEqual(before, self.hash())

    def test_removing_a_family_changes_the_hash(self):
        before = self.hash()
        shutil.rmtree(os.path.join(self.sdk, 'libps2000'))
        self.assertNotEqual(before, self.hash())

    def test_upgrading_a_header_changes_the_hash(self):
        before = self.hash()
        self.write(os.path.join(self.sdk, 'libps2000', 'ps2000.h'),
                   '/* h, newer */\n')
        self.assertNotEqual(before, self.hash())

    def test_the_shared_header_families_count_too(self):
        """libps3000a builds against a PicoConnectProbes.h from these."""
        for family in ('libps6000a', 'libpsospa'):
            with self.subTest(family=family):
                before = self.hash()
                self.write(os.path.join(self.sdk, family, 'PicoConnectProbes.h'),
                           '/* h */\n')
                self.assertNotEqual(before, self.hash())

    def test_a_family_build_rs_never_reads_changes_nothing(self):
        before = self.hash()
        self.write(os.path.join(self.sdk, 'libps3000', 'ps3000.h'), '/* h */\n')
        self.assertEqual(before, self.hash())

    def test_headers_without_the_sources_still_yield_empty(self):
        shutil.rmtree(os.path.join(self.home, 'box'))
        self.assertEqual(self.hash(), '')


class DaemonHashIsNotTheBuildHash(unittest.TestCase):
    """The image must not be wiped for a change it does not contain."""

    def test_rust_tree_is_absent_from_the_image_build_inputs(self):
        for entry in _update._BUILD_HASH_SOURCE_DIRS:
            self.assertNotIn('oscilloscope-daemon', entry)
        for entry in _update._BUILD_HASH_INPUTS:
            self.assertNotIn('oscilloscope-daemon', entry)

    def test_daemon_hash_does_not_cover_the_python_tree(self):
        """...and the converse: a Python edit must not rebuild the daemon."""
        self.assertNotIn('box/lager', _update._DAEMON_SOURCE_DIR)


class TheTwoHashImplementationsAgree(unittest.TestCase):
    """The digest is computed twice, in two languages, and must not drift.

    start_box.sh computes it locally to decide whether to rebuild;
    update.py computes it over SSH to decide whether `lager update` may take
    its "already up to date" early exit. If they disagree, the update forces
    the rebuild path on every run and start_box.sh then declines to build --
    a permanent slow no-op that no single-implementation test would catch.
    """

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.src = os.path.join(self.home, 'box', 'oscilloscope-daemon')
        os.makedirs(os.path.join(self.src, 'daemon', 'src'))
        for name, body in (('Cargo.toml', '[workspace]\n'),
                           ('daemon/src/main.rs', 'fn main() {}\n'),
                           ('daemon/build.rs', '// build\n')):
            path = os.path.join(self.src, *name.split('/'))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w') as handle:
                handle.write(body)
        # Build output must be ignored identically by both.
        noise = os.path.join(self.src, 'target', 'release', 'daemon')
        os.makedirs(os.path.dirname(noise))
        with open(noise, 'w') as handle:
            handle.write('ELF\n')
        # A header in every directory either side reads, plus one neither
        # does: a directory only one of them hashed would show up as a
        # different digest.
        self.sdk = os.path.join(self.home, 'picoscope-include')
        for family in _update._DAEMON_SDK_DIRS + ('libps3000',):
            path = os.path.join(self.sdk, family, 'header.h')
            os.makedirs(os.path.dirname(path))
            with open(path, 'w') as handle:
                handle.write(f'/* {family} */\n')

    def python_side(self):
        result = subprocess.run(
            ['bash', '-c', _daemon_hash_shell_cmd(self.sdk)],
            env=dict(os.environ, HOME=self.home),
            capture_output=True, text=True,
        )
        return result.stdout.strip()

    def shell_side(self):
        """Run start_box.sh's `oscilloscope_source_hash` in isolation."""
        script = (
            f'OSCILLOSCOPE_SRC="{self.src}"\n'
            f'OSCILLOSCOPE_SDK_INCLUDE="{self.sdk}"\n'
            f'{_start_box_function("oscilloscope_source_hash")}\n'
            'oscilloscope_source_hash\n'
        )
        result = subprocess.run(['bash', '-c', script],
                                capture_output=True, text=True)
        return result.stdout.strip()

    def test_both_produce_a_digest(self):
        self.assertRegex(self.python_side(), r'^[0-9a-f]{64}$')
        self.assertRegex(self.shell_side(), r'^[0-9a-f]{64}$')

    def test_they_produce_the_same_digest(self):
        self.assertEqual(self.python_side(), self.shell_side())

    def test_they_still_agree_after_a_source_change(self):
        with open(os.path.join(self.src, 'daemon', 'src', 'main.rs'), 'w') as h:
            h.write('fn main() { println!("changed"); }\n')
        self.assertEqual(self.python_side(), self.shell_side())

    def test_they_still_agree_after_a_header_change(self):
        with open(os.path.join(self.sdk, 'libps2000', 'header.h'), 'w') as h:
            h.write('/* newer */\n')
        self.assertEqual(self.python_side(), self.shell_side())

    def test_they_agree_with_no_sdk_installed(self):
        shutil.rmtree(self.sdk)
        self.assertRegex(self.python_side(), r'^[0-9a-f]{64}$')
        self.assertEqual(self.python_side(), self.shell_side())


class StartBoxBuildsWhenStale(unittest.TestCase):
    """Exercise start_box.sh's daemon block with a stubbed `docker`.

    The block decides whether to compile, and gets three things right or the
    box pays for it on every container start: it must build when there is no
    binary, must NOT build when the recorded hash still matches, and must
    not be fatal when the build fails -- the daemon drives PicoScopes and
    nothing else, so a box that cannot compile it still has to come up.
    """

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.src = os.path.join(self.home, 'oscilloscope-daemon')
        os.makedirs(self.src)
        with open(os.path.join(self.src, 'main.rs'), 'w') as handle:
            handle.write('fn main() {}\n')
        self.third_party = os.path.join(self.home, 'third_party')
        os.makedirs(self.third_party)

        self.bin_dir = os.path.join(self.home, 'bin')
        os.makedirs(self.bin_dir)
        self.marker = os.path.join(self.home, 'docker-was-called')
        fake_docker = os.path.join(self.bin_dir, 'docker')
        with open(fake_docker, 'w') as handle:
            handle.write(
                '#!/bin/sh\n'
                'if [ "$1" = "image" ]; then exit 0; fi\n'
                'if [ "$1" = "run" ]; then\n'
                '  echo called >> "$DOCKER_MARKER"\n'
                '  [ -n "$FAKE_BUILD_FAILS" ] && exit 1\n'
                '  printf FAKE > "$THIRD_PARTY_DIR/.oscilloscope-daemon.new"\n'
                '  exit 0\n'
                'fi\n'
                'exit 0\n'
            )
        os.chmod(fake_docker, 0o755)

        here = os.path.abspath(__file__)
        repo = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(here))))
        with open(os.path.join(repo, 'box', 'start_box.sh')) as handle:
            text = handle.read()
        begin = text.index('OSCILLOSCOPE_SRC="${SCRIPT_DIR}/oscilloscope-daemon"')
        # Anchored to column zero: the same condition appears indented
        # *inside* the block, and matching that one truncates it mid-`if`.
        end = text.index('\nif [ -f "$OSCILLOSCOPE_DAEMON" ]; then', begin)
        self.block = text[begin:end]

        # The block skips the build without the PicoTech headers, so point
        # it at a stand-in SDK that has every family's.
        self.sdk = os.path.join(self.home, 'picoscope-include')
        for header in _update._DAEMON_SDK_HEADERS + (
                _update._DAEMON_SDK_PS3000A_HEADER,):
            self.add_header(header)
        default = 'OSCILLOSCOPE_SDK_INCLUDE="/opt/picoscope/include"'
        self.assertIn(default, self.block)
        self.block = self.block.replace(
            default, f'OSCILLOSCOPE_SDK_INCLUDE="{self.sdk}"')

    def add_header(self, header):
        path = os.path.join(self.sdk, *header.split('/'))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as handle:
            handle.write('/* header */\n')

    def run_block(self, fail=False):
        # `set -e` matches start_box.sh, and matters: under it a non-zero
        # exit from a command substitution aborts the script, so a box could
        # fail to start over a daemon it did not even need.
        script = (
            f'set -eu\n'
            f'SCRIPT_DIR="{self.home}"\n'
            f'THIRD_PARTY_DIR="{self.third_party}"\n'
            f'OSCILLOSCOPE_DAEMON="$THIRD_PARTY_DIR/oscilloscope-daemon"\n'
            f'{self.block}\n'
        )
        env = dict(
            os.environ,
            PATH=self.bin_dir + os.pathsep + os.environ['PATH'],
            DOCKER_MARKER=self.marker,
            THIRD_PARTY_DIR=self.third_party,
        )
        if fail:
            env['FAKE_BUILD_FAILS'] = '1'
        return subprocess.run(['bash', '-c', script], env=env,
                              capture_output=True, text=True)

    @property
    def built(self):
        return os.path.exists(self.marker)

    def binary(self):
        path = os.path.join(self.third_party, 'oscilloscope-daemon')
        return open(path).read() if os.path.exists(path) else None

    def hash_file(self):
        path = os.path.join(self.third_party, 'oscilloscope-daemon.hash')
        return open(path).read().strip() if os.path.exists(path) else None

    def test_builds_and_installs_when_there_is_no_binary(self):
        result = self.run_block()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.built)
        self.assertEqual(self.binary(), 'FAKE')
        self.assertRegex(self.hash_file() or '', r'^[0-9a-f]{64}$')

    def test_second_run_does_nothing(self):
        self.run_block()
        os.remove(self.marker)
        self.run_block()
        self.assertFalse(
            self.built,
            'rebuilt an up-to-date daemon: every container start would pay')

    def test_a_source_change_rebuilds(self):
        self.run_block()
        os.remove(self.marker)
        with open(os.path.join(self.src, 'main.rs'), 'w') as handle:
            handle.write('fn main() { println!("new"); }\n')
        self.run_block()
        self.assertTrue(self.built)

    def test_build_output_does_not_trigger_a_rebuild(self):
        """The exclusion that stops an infinite rebuild loop."""
        self.run_block()
        os.remove(self.marker)
        target = os.path.join(self.src, 'target', 'release')
        os.makedirs(target)
        with open(os.path.join(target, 'daemon'), 'w') as handle:
            handle.write('ELF')
        self.run_block()
        self.assertFalse(self.built)

    def test_a_failed_build_is_not_fatal_and_retries(self):
        result = self.run_block(fail=True)
        self.assertEqual(result.returncode, 0, 'a failed build stopped the box')
        self.assertIn('WARNING', result.stdout)
        # No hash recorded and no half-written binary left mountable.
        self.assertIsNone(self.hash_file())
        self.assertIsNone(self.binary())
        self.assertFalse(os.path.exists(
            os.path.join(self.third_party, '.oscilloscope-daemon.new')))

        os.remove(self.marker)
        self.run_block()
        self.assertTrue(self.built, 'a failed build was not retried')
        self.assertEqual(self.binary(), 'FAKE')

    def test_absent_sources_are_a_no_op(self):
        shutil.rmtree(self.src)
        result = self.run_block()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.built)

    def test_missing_sdk_headers_skip_the_build_with_a_note(self):
        """A build without the headers can only fail, on every start."""
        shutil.rmtree(self.sdk)
        result = self.run_block()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.built, 'ran a build that cannot succeed')
        self.assertIn('PicoTech SDK headers', result.stdout)
        self.assertNotIn('WARNING', result.stdout)
        self.assertIsNone(self.hash_file())

    def test_one_family_is_enough_to_build(self):
        """A box with only libps2000 still gets a daemon for its 2204A."""
        shutil.rmtree(self.sdk)
        self.add_header('libps2000/ps2000.h')
        result = self.run_block()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.built)
        self.assertEqual(self.binary(), 'FAKE')

    def test_installing_another_family_rebuilds_on_the_next_start(self):
        shutil.rmtree(self.sdk)
        self.add_header('libps2000/ps2000.h')
        self.run_block()
        os.remove(self.marker)
        self.add_header('libps5000a/ps5000aApi.h')
        self.run_block()
        self.assertTrue(self.built, 'kept a daemon built without ps5000a')

    def test_ps3000a_alone_cannot_build(self):
        """Its headers include a PicoConnectProbes.h it does not ship."""
        shutil.rmtree(self.sdk)
        self.add_header('libps3000a/ps3000aApi.h')
        self.run_block()
        self.assertFalse(self.built, 'ran a build that cannot succeed')

    def test_ps3000a_builds_beside_a_shared_probes_header(self):
        shutil.rmtree(self.sdk)
        self.add_header('libps3000a/ps3000aApi.h')
        self.add_header('libps6000a/PicoConnectProbes.h')
        self.run_block()
        self.assertTrue(self.built)

    def test_installing_the_headers_builds_on_the_next_start(self):
        shutil.rmtree(self.sdk)
        self.run_block()
        for header in _update._DAEMON_SDK_HEADERS:
            self.add_header(header)
        self.run_block()
        self.assertTrue(self.built)
        self.assertEqual(self.binary(), 'FAKE')

    def test_an_up_to_date_daemon_says_nothing_about_headers(self):
        self.run_block()
        result = self.run_block()
        self.assertNotIn('PicoTech', result.stdout)

    def test_removing_the_sdk_keeps_the_daemon_it_built(self):
        self.run_block()
        os.remove(self.marker)
        shutil.rmtree(self.sdk)
        result = self.run_block()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.built, 'ran a build that cannot succeed')
        self.assertEqual(self.binary(), 'FAKE')
        self.assertIn('PicoTech SDK headers', result.stdout)


class TheTwoHeaderChecksAgree(unittest.TestCase):
    """start_box.sh and update.py must agree on whether the SDK is there.

    If update.py saw headers that start_box.sh does not, every `lager
    update` would take the rebuild path for a build start_box.sh then
    skips: the loop the header check exists to break.
    """

    def setUp(self):
        self.include = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.include, ignore_errors=True)

    def path(self, header):
        return os.path.join(self.include, *header.split('/'))

    def install(self, *headers):
        """Make the stand-in SDK hold exactly `headers`."""
        shutil.rmtree(self.include)
        os.makedirs(self.include)
        for header in headers:
            os.makedirs(os.path.dirname(self.path(header)), exist_ok=True)
            with open(self.path(header), 'w') as handle:
                handle.write('/* header */\n')

    def python_side(self):
        result = subprocess.run(
            ['sh', '-c', _update._daemon_headers_shell_cmd(self.include)],
            capture_output=True, text=True,
        )
        return result.stdout.strip()

    def shell_side(self):
        """Run start_box.sh's `picotech_headers_present` in isolation."""
        script = (
            'set -eu\n'
            f'OSCILLOSCOPE_SDK_INCLUDE="{self.include}"\n'
            f'{_start_box_function("picotech_headers_present")}\n'
            'if picotech_headers_present; then echo 1; else echo 0; fi\n'
        )
        result = subprocess.run(['bash', '-c', script],
                                capture_output=True, text=True)
        return result.stdout.strip()

    def assert_both(self, expected, *headers):
        self.install(*headers)
        self.assertEqual(self.python_side(), expected, f'update.py, {headers}')
        self.assertEqual(self.shell_side(), expected, f'start_box.sh, {headers}')

    def test_both_find_a_complete_sdk(self):
        self.assert_both('1', *_update._DAEMON_SDK_HEADERS,
                         _update._DAEMON_SDK_PS3000A_HEADER,
                         *_update._DAEMON_SDK_PROBES_HEADERS)

    def test_both_accept_any_one_family_alone(self):
        for header in _update._DAEMON_SDK_HEADERS:
            with self.subTest(header=header):
                self.assert_both('1', header)

    def test_both_want_a_probes_header_beside_ps3000a(self):
        self.assert_both('0', _update._DAEMON_SDK_PS3000A_HEADER)
        for probes in _update._DAEMON_SDK_PROBES_HEADERS:
            with self.subTest(probes=probes):
                self.assert_both('1', _update._DAEMON_SDK_PS3000A_HEADER, probes)

    def test_both_ignore_families_the_daemon_does_not_build(self):
        self.assert_both('0', 'libps3000/ps3000.h', 'libps6000a/ps6000aApi.h',
                         'libps6000a/PicoConnectProbes.h')

    def test_both_notice_no_sdk_at_all(self):
        shutil.rmtree(self.include)
        self.assertEqual(self.python_side(), '0')
        self.assertEqual(self.shell_side(), '0')


class DaemonBuildDecision(unittest.TestCase):
    BASE = {
        'DAEMON_SOURCE_HASH': 'aaa',
        'DAEMON_BINARY': '1',
        'DAEMON_HASH_STORED': 'aaa',
    }

    def decide(self, force=False, **overrides):
        facts = dict(self.BASE, **overrides)
        return _daemon_needs_build(facts, force=force)

    def test_up_to_date_does_not_build(self):
        build, _reason = self.decide()
        self.assertFalse(build)

    def test_changed_rust_builds(self):
        build, reason = self.decide(DAEMON_SOURCE_HASH='bbb')
        self.assertTrue(build)
        self.assertIn('changed', reason)

    def test_missing_binary_builds(self):
        build, _reason = self.decide(DAEMON_BINARY='0')
        self.assertTrue(build)

    def test_a_box_with_no_scope_still_gets_a_daemon(self):
        """The point of the whole feature.

        Gating on a PicoScope being present at update time means provisioning
        a box, plugging a scope in afterwards, and finding it does nothing --
        so scope presence is not consulted at all. Asserted by giving the
        decision both answers and requiring it not to care, which a test
        that merely passed ``PICOSCOPE='0'`` would not: an ignored key looks
        the same as a misspelled one.
        """
        for attached in ('0', '1'):
            self.assertEqual(
                self.decide(DAEMON_BINARY='0', PICOSCOPE=attached),
                self.decide(DAEMON_BINARY='0'),
            )
            self.assertTrue(self.decide(DAEMON_BINARY='0',
                                        PICOSCOPE=attached)[0])
            self.assertTrue(self.decide(DAEMON_SOURCE_HASH='bbb',
                                        PICOSCOPE=attached)[0])

    def test_absent_sources_never_build(self):
        build, reason = self.decide(DAEMON_SOURCE_HASH='')
        self.assertFalse(build)
        self.assertIn('no daemon sources', reason)

    def test_unrecorded_hash_rebuilds_once(self):
        """Adopts a hand-deployed binary of unknown provenance."""
        build, reason = self.decide(DAEMON_HASH_STORED='')
        self.assertTrue(build)
        self.assertIn('by hand', reason)

    def test_force_builds_even_when_current(self):
        build, reason = self.decide(force=True)
        self.assertTrue(build)
        self.assertIn('force', reason)

    def test_missing_sdk_headers_never_build(self):
        """A failed build records no hash, so this used to rebuild forever."""
        for overrides in ({'DAEMON_BINARY': '0'},
                          {'DAEMON_HASH_STORED': ''},
                          {'DAEMON_SOURCE_HASH': 'bbb'}):
            with self.subTest(**overrides):
                build, reason = self.decide(DAEMON_SDK_HEADERS='0',
                                            **overrides)
                self.assertFalse(build)
                self.assertIn('PicoTech SDK headers', reason)

    def test_missing_sdk_headers_win_over_force(self):
        """start_box.sh skips the build without them whatever was asked."""
        build, _reason = self.decide(force=True, DAEMON_SDK_HEADERS='0')
        self.assertFalse(build)

    def test_installed_sdk_headers_change_nothing(self):
        self.assertEqual(self.decide(DAEMON_BINARY='0', DAEMON_SDK_HEADERS='1'),
                         self.decide(DAEMON_BINARY='0'))

    def test_a_probe_without_the_header_fact_still_builds(self):
        """Fail open: an unknown answer must not switch the feature off."""
        build, _reason = self.decide(DAEMON_BINARY='0')
        self.assertTrue(build)
        build, _reason = self.decide(DAEMON_BINARY='0', DAEMON_SDK_HEADERS='')
        self.assertTrue(build)


class DaemonBuildTakesTheRebuildPath(unittest.TestCase):
    """A stale daemon must not be hidden by the "already up to date" exit.

    The binary is bind-mounted, so it can only be swapped while the
    container is down -- which only the rebuild path arranges.
    """

    IN_SYNC = dict(
        git_sync_confirmed=True, needs_pull=False, needs_flatten=False,
        hash_mismatch=False, force=False,
    )

    def test_daemon_change_alone_forces_a_rebuild(self):
        verdict = _rebuild_gate_verdict(
            {'LAGER_RUNNING': '1'}, daemon_needs_build=True, **self.IN_SYNC)
        self.assertEqual(verdict, 'rebuild')

    def test_nothing_to_do_still_skips(self):
        verdict = _rebuild_gate_verdict(
            {'LAGER_RUNNING': '1'}, daemon_needs_build=False, **self.IN_SYNC)
        self.assertEqual(verdict, 'skip')

    def test_the_flag_defaults_off_for_existing_callers(self):
        verdict = _rebuild_gate_verdict({'LAGER_RUNNING': '1'}, **self.IN_SYNC)
        self.assertEqual(verdict, 'skip')

    def test_a_box_without_the_sdk_takes_the_up_to_date_exit(self):
        """No daemon and no headers: nothing a rebuild could fix."""
        facts = {'LAGER_RUNNING': '1', 'DAEMON_SOURCE_HASH': 'aaa',
                 'DAEMON_BINARY': '0', 'DAEMON_SDK_HEADERS': '0'}
        wanted, _reason = _daemon_needs_build(facts, force=False)
        verdict = _rebuild_gate_verdict(
            facts, daemon_needs_build=wanted, **self.IN_SYNC)
        self.assertEqual(verdict, 'skip')


class ProbeReportsDaemonFacts(unittest.TestCase):
    """The decision needs four facts, and they ride the existing probe.

    Every placeholder must be substituted: an unexpanded `__DAEMON_HASH_CMD__`
    would be executed by the box's shell as a command name, and the fact
    would come back empty -- which reads as "no daemon sources" and silently
    disables the whole feature.
    """

    def setUp(self):
        self.script = _update._probe_shell_script()

    def test_no_placeholders_survive(self):
        self.assertNotIn('__DAEMON_HASH_CMD__', self.script)
        self.assertNotIn('__DAEMON_HEADERS_CMD__', self.script)
        self.assertNotIn('__PICO_VENDOR_ID__', self.script)

    def test_every_fact_the_decision_reads_is_emitted(self):
        for key in ('DAEMON_SOURCE_HASH', 'DAEMON_HASH_STORED',
                    'DAEMON_BINARY', 'DAEMON_SDK_HEADERS'):
            self.assertIn(f'{_update._PROBE_PREFIX}{key}=', self.script)

    def test_the_header_fact_reads_the_installed_sdk(self):
        self.assertIn(_update._daemon_headers_shell_cmd(), self.script)
        self.assertIn('/opt/picoscope/include/libps2000/ps2000.h',
                      self.script)

    def test_the_script_is_valid_shell(self):
        result = subprocess.run(['bash', '-n'], input=self.script,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_probe_parses_the_daemon_facts_back_out(self):
        facts = _update._parse_probe_output(
            'LAGER_PROBE_DAEMON_SOURCE_HASH=abc123\n'
            'LAGER_PROBE_DAEMON_BINARY=1\n'
            'some motd banner line\n'
        )
        self.assertEqual(facts.get('DAEMON_SOURCE_HASH'), 'abc123')
        self.assertEqual(facts.get('DAEMON_BINARY'), '1')


if __name__ == '__main__':
    unittest.main()
