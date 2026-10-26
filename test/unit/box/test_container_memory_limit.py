# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
start_box.sh caps the lager container's memory below the host's.

Uncapped, a leak in the container starved the whole host: the scope daemon
reached 15 GB, systemd-networkd timed out and gave up on the Wi-Fi, and the
box stayed off the network until it was power-cycled.

The block runs here verbatim, extracted by its BEGIN/END sentinels, against
a fake /proc/meminfo. What is pinned:
  * the cap is three quarters of RAM, leaving the host at least 1 GiB;
  * a host too small to leave 1 GiB and still give the container 1 GiB, or a
    meminfo that cannot be read, gets no cap rather than a broken one;
  * LAGER_CONTAINER_MEMORY_MB overrides the cap, and 0 removes it;
  * docker run is handed the cap with swap disabled on top of it.
"""

import os
import subprocess
import tempfile
import unittest

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
_START_BOX = os.path.join(_REPO, 'box', 'start_box.sh')
_TOPIC = 'container memory limit'


def _extract(topic):
    """Return the shell between the BEGIN/END sentinels naming `topic`."""
    begin, end = f"# --- BEGIN {topic}", f"# --- END {topic}"
    body, inside, seen = [], False, False
    with open(_START_BOX, encoding='utf-8') as f:
        for line in f.read().splitlines():
            if line.startswith(begin):
                inside, seen = True, True
                continue
            if line.startswith(end):
                inside = False
                continue
            if inside:
                body.append(line)
    assert seen, f"sentinel {begin!r} not found in {_START_BOX}"
    assert body, f"no shell extracted for {topic!r}"
    return "\n".join(body)


class ContainerMemoryLimit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _args(self, mem_total_kb=None, override=None):
        """The docker run arguments the block builds for a host of this size."""
        meminfo = os.path.join(self.tmp.name, 'meminfo')
        if mem_total_kb is not None:
            with open(meminfo, 'w', encoding='utf-8') as f:
                f.write(f"MemTotal:       {mem_total_kb} kB\nMemFree:        1000 kB\n")
        block = _extract(_TOPIC).replace(
            '"${1:-/proc/meminfo}"', '"${1:-' + meminfo + '}"')
        env = {k: v for k, v in os.environ.items() if k != 'LAGER_CONTAINER_MEMORY_MB'}
        if override is not None:
            env['LAGER_CONTAINER_MEMORY_MB'] = override
        script = f'set -e\n{block}\nprintf "%s\\n" "${{LAGER_MEMORY_ARGS[@]}}"\n'
        out = subprocess.run(['bash', '-c', script], env=env, capture_output=True,
                             text=True, check=True)
        return out.stdout.split()

    def test_a_16_gb_box_gives_the_container_three_quarters(self):
        # 15.36 GiB, as STG-1 and PRD-2 report it.
        self.assertEqual(self._args(16106127),
                         ['--memory', '11796m', '--memory-swap', '11796m'])

    def test_a_small_box_still_leaves_the_host_a_gigabyte(self):
        self.assertEqual(self._args(3 * 1024 * 1024),
                         ['--memory', '2048m', '--memory-swap', '2048m'])

    def test_a_box_too_small_for_both_gets_no_cap(self):
        self.assertEqual(self._args(1536 * 1024), [])

    def test_an_unreadable_meminfo_gets_no_cap(self):
        self.assertEqual(self._args(None), [])

    def test_the_override_wins_and_zero_removes_the_cap(self):
        self.assertEqual(self._args(16106127, override='4096'),
                         ['--memory', '4096m', '--memory-swap', '4096m'])
        self.assertEqual(self._args(16106127, override='0'), [])

    def test_docker_run_is_handed_the_cap(self):
        with open(_START_BOX, encoding='utf-8') as f:
            text = f.read()
        run = text[text.index('docker run -d \\'):text.index('lager 2>"$_RUN_ERR"')]
        self.assertIn('"${LAGER_MEMORY_ARGS[@]}"', run)


if __name__ == '__main__':
    unittest.main()
