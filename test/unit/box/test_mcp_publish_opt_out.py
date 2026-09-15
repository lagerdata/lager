# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""LAGER_MCP_NO_PUBLISH keeps port 8100 off the host, and nothing else.

`start_box.sh` publishes its ports all or nothing: `--no-publish` drops every
one. The MCP server on 8100 performs no authentication, so an operator who wants
it off the network needs a way to drop that one port and keep the box in
service. `LAGER_MCP_NO_PUBLISH` is that way. It arrives through
`lager box-config env set`, like `LAGER_DISABLE_UART_SERVICE`, and is read with
the same truthiness rule.

These tests run the two blocks of `start_box.sh` that decide publishing under
bash, with a rendered `BOX_CONFIG_ENV`, and compare the `-p` arguments that come
out. Reading the text cannot tell which arm runs:
test_firewall_port_allowlist.py collects every `-p` on both sides of an `if`, so
an opt-out that never removed 8100 would still pass it.
"""

import pathlib
import shlex
import subprocess
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
START_BOX = REPO_ROOT / "box" / "start_box.sh"

#: Every -p mapping a default box publishes.
BASELINE = {
    "5000:5000", "8301:5000", "8080:8080", "8081-8090:8081-8090", "8100:8100",
    "8765:8765", "2331-2342:2331-2342", "4444-4447:4444-4447",
    "6666-6669:6666-6669", "9090-9097:9090-9097", "9000:9000",
}


def _extract(topic):
    """Return the shell between the BEGIN/END sentinels naming `topic`."""
    begin, end = f"# --- BEGIN {topic}", f"# --- END {topic}"
    body, inside, seen = [], False, False
    for line in START_BOX.read_text(encoding="utf-8").splitlines():
        if line.startswith(begin):
            inside, seen = True, True
            continue
        if line.startswith(end):
            inside = False
            continue
        if inside:
            body.append(line)
    assert seen, f"sentinel {begin!r} not found in {START_BOX}"
    assert body, f"no shell extracted for {topic!r}"
    return "\n".join(body)


def _run(env=(), *, no_publish="", network="lagernet"):
    """Run the opt-out scan and the publish block; return (mappings, messages).

    `env` is the list of KEY=VALUE pairs `lager box-config env set` would have
    rendered into BOX_CONFIG_ENV as `--env KEY=VALUE`.
    """
    env_args = " ".join(f"--env {shlex.quote(pair)}" for pair in env)
    script = (
        f"NO_PUBLISH={shlex.quote(no_publish)}\n"
        f"BOX_CONFIG_NETWORK={shlex.quote(network)}\n"
        f"BOX_CONFIG_ENV=({env_args})\n"
        f"{_extract('service publish opt-outs')}\n"
        f"{_extract('port publishing')}\n"
        'printf "ARG=%s\\n" "${PORT_PUBLISH_ARGS[@]}"\n'
    )
    out = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    args = [line[len("ARG="):] for line in out if line.startswith("ARG=")]
    mappings = {arg for arg in args if arg and arg != "-p"}
    messages = [line for line in out if not line.startswith("ARG=")]
    return mappings, messages


class McpPublishOptOut(unittest.TestCase):
    def test_default_publishes_8100_and_everything_else(self):
        mappings, messages = _run()
        self.assertEqual(mappings, BASELINE)
        self.assertEqual(messages, [])

    def test_the_opt_out_drops_exactly_8100(self):
        for value in ("1", "true", "TRUE", "yes", "Yes"):
            with self.subTest(value=value):
                mappings, messages = _run([f"LAGER_MCP_NO_PUBLISH={value}"])
                self.assertEqual(BASELINE - mappings, {"8100:8100"})
                self.assertTrue(
                    any(m.startswith("Not publishing port 8100") for m in messages),
                    messages,
                )

    def test_other_values_keep_8100(self):
        for value in ("0", "false", "no", "", "2"):
            with self.subTest(value=value):
                mappings, messages = _run([f"LAGER_MCP_NO_PUBLISH={value}"])
                self.assertEqual(mappings, BASELINE)
                self.assertEqual(messages, [])

    def test_both_opt_outs_drop_exactly_8100_and_9000(self):
        mappings, _ = _run(
            ["LAGER_MCP_NO_PUBLISH=1", "LAGER_DISABLE_UART_SERVICE=1"],
        )
        self.assertEqual(BASELINE - mappings, {"8100:8100", "9000:9000"})

    def test_the_uart_opt_out_alone_still_publishes_8100(self):
        mappings, _ = _run(["LAGER_DISABLE_UART_SERVICE=1"])
        self.assertEqual(BASELINE - mappings, {"9000:9000"})

    def test_no_publish_still_publishes_nothing(self):
        mappings, _ = _run(["LAGER_MCP_NO_PUBLISH=1"], no_publish="1")
        self.assertEqual(mappings, set())

    def test_host_mode_warns_that_the_opt_out_has_no_effect(self):
        mappings, messages = _run(["LAGER_MCP_NO_PUBLISH=1"], network="host")
        self.assertEqual(mappings, set())
        self.assertTrue(
            any(
                m.startswith("[WARNING] LAGER_MCP_NO_PUBLISH has no effect")
                for m in messages
            ),
            messages,
        )

    def test_host_mode_without_the_opt_out_does_not_warn(self):
        _, messages = _run(network="host")
        self.assertFalse(any("LAGER_MCP_NO_PUBLISH" in m for m in messages), messages)


if __name__ == "__main__":
    unittest.main()
