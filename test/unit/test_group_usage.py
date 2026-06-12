# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Usage-line formatting tests for CommandFirstUsageMixin / LagerGroup.

Guards three things: plain groups read `COMMAND [OPTIONS]` (bracketed
when invokable bare), NetGroup-style groups keep their own rewritten
usage, and the root SectionedGroup still renders its sectioned command
list.
"""
import unittest

import click
from click.testing import CliRunner

from cli.core.group_usage import LagerGroup
from cli.commands.box.nets import nets
from cli.commands.power.supply import supply
from cli.main import cli


class PlainGroupUsage(unittest.TestCase):
    def test_nets_usage_is_command_first(self):
        result = CliRunner().invoke(nets, ["--help"])
        self.assertEqual(result.exit_code, 0)
        # nets is invoke_without_command=True, so COMMAND is optional.
        self.assertIn("nets [COMMAND] [OPTIONS]", result.output)
        self.assertNotIn("[ARGS]", result.output)

    def test_required_subcommand_group(self):
        @click.group(cls=LagerGroup)
        def grp():
            """A group whose subcommand is mandatory."""

        result = CliRunner().invoke(grp, ["--help"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("grp COMMAND [OPTIONS]", result.output)
        self.assertNotIn("[ARGS]", result.output)


class NetGroupUnaffected(unittest.TestCase):
    def test_supply_keeps_net_style_usage(self):
        result = CliRunner().invoke(supply, ["--help"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("[NET_NAME]", result.output)
        self.assertIn("--box [BOX_NAME]", result.output)


class RootHelp(unittest.TestCase):
    def test_sections_still_render(self):
        result = CliRunner().invoke(cli, ["--help"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("[COMMAND] [OPTIONS]", result.output)
        self.assertIn("Box setup & management", result.output)
        self.assertIn("Power", result.output)


if __name__ == "__main__":
    unittest.main()
