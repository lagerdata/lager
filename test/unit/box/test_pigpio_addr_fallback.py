# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
The pigpio address fallback in box/start_box.sh.

This needs a test rather than a read-through because the bug was invisible by
inspection: the original line *looked* like it had a fallback.

    PIGPIO_ADDR=$(docker inspect ... | tr -d '\\n' || echo "172.18.0.2")

`||` tests the pipeline's status, and the pipeline ends in `tr`, which exits 0
whether or not `docker inspect` produced anything. So the fallback was dead
code, and a box with no pigpio container passed `--env PIGPIO_ADDR=` to the
container. `os.environ.get('PIGPIO_ADDR', '172.18.0.2')` does not substitute a
default for a variable that is set and empty, so neither end applied one.
Measured on a box with no pigpio container: the value reaching the container
was the empty string.

Driven by executing the real shell between the sentinels with `docker` stubbed,
so the assertion is about what the script does, not about what it looks like.
Same technique as test_firewall_port_allowlist.py.
"""

import os
import subprocess
import unittest

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
_START_BOX = os.path.join(_REPO, 'box', 'start_box.sh')

DEFAULT = "172.18.0.2"


def _extract(topic):
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


def _run_with_docker_output(stdout, rc=0):
    """Run the block with `docker` stubbed to emit `stdout` and exit `rc`.

    The payload travels in the environment rather than inline in the script:
    embedding it as a shell literal turns a trailing newline into a literal
    backslash-n, which `tr -d` then cannot strip -- an artefact of the harness
    that would look exactly like a failure of the code under test.
    """
    block = _extract("pigpio address")
    script = (
        "docker() {\n"
        '    printf "%s" "$FAKE_DOCKER_OUT"\n'
        f"    return {rc}\n"
        "}\n"
        f"{block}\n"
        'printf "%s" "$PIGPIO_ADDR"\n'
    )
    env = dict(os.environ, FAKE_DOCKER_OUT=stdout)
    proc = subprocess.run(["bash", "-c", script], env=env,
                          capture_output=True, text=True, check=True)
    return proc.stdout


class PigpioAddressFallback(unittest.TestCase):
    def test_a_real_address_is_used_as_is(self):
        self.assertEqual(_run_with_docker_output("172.18.0.7\n"), "172.18.0.7")

    def test_no_pigpio_container_falls_back(self):
        """docker inspect prints nothing and exits non-zero. This is the case
        that was broken: the result used to be the empty string."""
        self.assertEqual(_run_with_docker_output("", rc=1), DEFAULT)

    def test_the_empty_result_is_never_passed_through(self):
        self.assertNotEqual(_run_with_docker_output("", rc=1), "")

    def test_a_container_not_on_lagernet_falls_back(self):
        """The Go template resolves a missing map key to nil, which renders as
        the literal `<no value>` -- non-empty, and not an address."""
        self.assertEqual(_run_with_docker_output("<no value>\n"), DEFAULT)

    def test_whitespace_only_falls_back(self):
        self.assertEqual(_run_with_docker_output("\n\n"), DEFAULT)

    def test_garbage_falls_back(self):
        self.assertEqual(_run_with_docker_output("Error: No such object\n"), DEFAULT)

    def test_the_result_is_always_something_the_container_can_use(self):
        for out, rc in (("172.18.0.9\n", 0), ("", 1), ("<no value>\n", 0),
                        ("\n", 0), ("boom\n", 1)):
            with self.subTest(docker_output=out, rc=rc):
                got = _run_with_docker_output(out, rc)
                self.assertRegex(got, r'^\d+(\.\d+){3}$')


if __name__ == "__main__":
    unittest.main()
