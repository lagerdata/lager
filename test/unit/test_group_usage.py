# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Usage-line formatting tests for CommandFirstUsageMixin / LagerGroup.

Guards four things: plain groups read `COMMAND [OPTIONS]` (bracketed
when invokable bare), NetGroup-style groups keep their own rewritten
usage, net-style leaf commands read positionals-first with a
`--box [BOX_NAME]` tail, and the root SectionedGroup still renders its
sectioned command list.
"""
import unittest

import click
from click.testing import CliRunner

from cli.core.group_usage import LagerGroup
from cli.commands.box.nets import nets
from cli.commands.box.authorize import authorize
from cli.commands.power.supply import supply
from cli.main import cli


class PlainGroupUsage(unittest.TestCase):
    def test_box_group_is_command_first(self):
        from cli.commands.box.box_group import box
        result = CliRunner().invoke(box, ["--help"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("box COMMAND [OPTIONS]", result.output)
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


class BoxStyleUsage(unittest.TestCase):
    def test_nets_shows_box_name_usage(self):
        result = CliRunner().invoke(nets, ["--help"])
        self.assertEqual(result.exit_code, 0)
        # Net-style usage, but no NET_NAME (nets operates on all nets).
        self.assertIn("nets [COMMAND] --box [BOX_NAME]", result.output)
        self.assertNotIn("[NET_NAME]", result.output)
        self.assertNotIn("[ARGS]", result.output)

    def test_authorize_shows_box_name_usage(self):
        result = CliRunner().invoke(authorize, ["--help"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("authorize --box [BOX_NAME]", result.output)
        self.assertNotIn("[ARGS]", result.output)


class NetLeafUsage(unittest.TestCase):
    """Standalone leaf net commands: positionals first, then the --box tail,
    no [OPTIONS] — never Click's stock `[OPTIONS] [NET_NAME]`."""

    def _usage(self, args):
        result = CliRunner().invoke(cli, args + ["--help"], prog_name="lager")
        self.assertEqual(result.exit_code, 0, result.output)
        return result.output

    def test_uart_positionals_first(self):
        out = self._usage(["uart"])
        self.assertIn("uart [NET_NAME] --box [BOX_NAME]", out)
        self.assertNotIn("[OPTIONS]", out)
        # The serial-port action is hidden from the usage line but still
        # documented in the help body.
        self.assertIn("serial-port", out)

    def test_adc_positionals_first(self):
        out = self._usage(["adc"])
        self.assertIn("adc [NET_NAME] --box [BOX_NAME]", out)
        self.assertNotIn("[OPTIONS]", out)

    def test_gpo_keeps_level_before_box(self):
        out = self._usage(["gpo"])
        self.assertIn("gpo [NET_NAME] [LEVEL] --box [BOX_NAME]", out)
        self.assertNotIn("[OPTIONS]", out)


class NetSubCommandUsage(unittest.TestCase):
    """Subcommands of net groups get the same positionals-first tail,
    whether the group declares NETNAME (supply, i2c) or extracts it
    positionally (debug)."""

    def _usage(self, args):
        result = CliRunner().invoke(cli, args + ["--help"], prog_name="lager")
        self.assertEqual(result.exit_code, 0, result.output)
        return result.output

    def test_supply_voltage(self):
        out = self._usage(["supply", "mynet", "voltage"])
        self.assertIn("supply [NET_NAME] voltage [VALUE] --box [BOX_NAME]", out)

    def test_i2c_write(self):
        out = self._usage(["i2c", "mynet", "write"])
        self.assertIn("i2c [NET_NAME] write [DATA] --box [BOX_NAME]", out)

    def test_debug_flash_inserts_netname(self):
        out = self._usage(["debug", "mynet", "flash"])
        self.assertIn("debug [NET_NAME] flash --box [BOX_NAME]", out)

    def test_nets_add_no_netname_insertion(self):
        out = self._usage(["nets", "add"])
        self.assertIn("nets add NAME ROLE CHANNEL ADDRESS --box [BOX_NAME]", out)
        self.assertNotIn("nets [NET_NAME]", out)


class RootHelp(unittest.TestCase):
    def test_sections_still_render(self):
        result = CliRunner().invoke(cli, ["--help"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("[COMMAND] [OPTIONS]", result.output)
        self.assertIn("Box setup & management", result.output)
        self.assertIn("Power", result.output)


if __name__ == "__main__":
    unittest.main()
