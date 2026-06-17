#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Startup regression tests for the nets TUI (cli/commands/box/net_tui.py).

The existing test suite (test_net_tui_assign.py, test_net_tui_labjack_pins.py)
covers assignment flows and worker-thread safety.  This file adds:

  1. Tree-building with mixed net types — guards against crashes in SavedNetsTree
     when a specific instrument or role triggers a formatting error.
  2. Empty-state rendering — TUI with no saved nets must compose cleanly.
  3. Unsaved-placeholder rendering — TUI shows discovered-but-unsaved nets.

All tests pass pre-built nets to NetApp directly (as launch_tui does) and
patch _run_script so no box round-trips are made.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

tui = importlib.import_module('cli.commands.box.net_tui')


def _make_app(nets=None, inst_list=None) -> tui.NetApp:
    return tui.NetApp(
        ctx=None,
        dut="box",
        inst_list=inst_list or [],
        nets=nets or [],
    )


def _saved(instrument, chan, role, name, addr="USB0::INSTR") -> tui.Net:
    return tui.Net(instrument, chan, role, name, addr, saved=True)


def _unsaved(instrument, chan, role, name, addr="USB0::INSTR") -> tui.Net:
    return tui.Net(instrument, chan, role, name, addr, saved=False)


# --------------------------------------------------------------------------- #
# Empty state                                                                   #
# --------------------------------------------------------------------------- #

class TestNetTUIEmptyState:
    def test_composes_with_no_nets(self):
        async def main():
            with patch.object(tui, "_run_script", return_value="[]"):
                app = _make_app(nets=[])
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
        asyncio.run(main())

    def test_on_mount_makes_no_box_roundtrip_empty(self):
        async def main():
            with patch.object(tui, "_run_script") as rs:
                app = _make_app(nets=[])
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
            assert rs.call_count == 0
        asyncio.run(main())


# --------------------------------------------------------------------------- #
# Mixed net types                                                               #
# --------------------------------------------------------------------------- #
# REGRESSION: tree-row rendering for certain instruments or roles would crash
# (e.g. MarkupError on bracket characters, KeyError on unknown roles).

MIXED_SAVED_NETS = [
    _saved("Rigol_DP832", "1", "power-supply", "supply1", "USB0::0x1AB1::INSTR"),
    _saved("Rigol_DP832", "2", "power-supply", "supply2", "USB0::0x1AB1::INSTR"),
    _saved("LabJack_T7", "AIN0", "adc", "adc1", "USB0::0x0CD5::0x0007::470012345::INSTR"),
    _saved("LabJack_T7", "AIN1", "adc", "adc2", "USB0::0x0CD5::0x0007::470012345::INSTR"),
    _saved("LabJack_T7", "FIO4-FIO5", "i2c", "i2c1", "USB0::0x0CD5::0x0007::470012345::INSTR"),
    _saved("LabJack_T7", "FIO0-FIO3", "spi", "spi1", "USB0::0x0CD5::0x0007::470012345::INSTR"),
    _saved("LabJack_T7", "EIO0", "gpio", "gpio1", "USB0::0x0CD5::0x0007::470012345::INSTR"),
    _saved("FTDI", "0", "uart", "uart1", "serial://067b:23a3/serial/00000006"),
]


class TestNetTUIWithSavedNets:
    def test_composes_without_error(self):
        async def main():
            with patch.object(tui, "_run_script", return_value="[]"):
                app = _make_app(nets=MIXED_SAVED_NETS)
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
        asyncio.run(main())

    def test_on_mount_makes_no_box_roundtrip(self):
        async def main():
            with patch.object(tui, "_run_script") as rs:
                app = _make_app(nets=MIXED_SAVED_NETS)
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
            assert rs.call_count == 0
        asyncio.run(main())

    def test_saved_nets_reflected_in_app_state(self):
        async def main():
            with patch.object(tui, "_run_script", return_value="[]"):
                app = _make_app(nets=MIXED_SAVED_NETS)
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    # All supplied saved nets must appear in app.nets
                    saved_names = {n.net for n in app.nets if n.saved}
                    assert "supply1" in saved_names
                    assert "adc1" in saved_names
                    assert "i2c1" in saved_names
                    assert "spi1" in saved_names
                    assert "gpio1" in saved_names
                    assert "uart1" in saved_names
        asyncio.run(main())

    def test_serial_address_brackets_do_not_crash_tree(self):
        # REGRESSION: serial:// addresses like "serial://067b:23a3/..." contain
        # bracket-like characters that previously caused MarkupError in tree rows.
        nets = [_saved("FTDI", "0", "uart", "uart_test",
                        "serial://067b:23a3/serial/DEADBEEF")]
        async def main():
            with patch.object(tui, "_run_script", return_value="[]"):
                app = _make_app(nets=nets)
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
        asyncio.run(main())


# --------------------------------------------------------------------------- #
# Unsaved placeholder nets                                                      #
# --------------------------------------------------------------------------- #

class TestNetTUIUnsavedNets:
    def test_composes_with_only_unsaved_nets(self):
        nets = [
            _unsaved("Rigol_DP832", "1", "power-supply", "supply1"),
            _unsaved("LabJack_T7", "AIN0", "adc", "adc1"),
        ]
        async def main():
            with patch.object(tui, "_run_script", return_value="[]"):
                app = _make_app(nets=nets)
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    # Unsaved nets appear in app.nets but none are saved
                    assert any(not n.saved for n in app.nets)
                    assert not any(n.saved for n in app.nets)
        asyncio.run(main())

    def test_mix_of_saved_and_unsaved(self):
        nets = [
            _saved("Rigol_DP832", "1", "power-supply", "supply1"),
            _unsaved("Rigol_DP832", "2", "power-supply", "supply2"),
        ]
        async def main():
            with patch.object(tui, "_run_script", return_value="[]"):
                app = _make_app(nets=nets)
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    assert sum(1 for n in app.nets if n.saved) >= 1
                    assert sum(1 for n in app.nets if not n.saved) >= 1
        asyncio.run(main())


# --------------------------------------------------------------------------- #
# Instrument list passed to NetApp                                              #
# --------------------------------------------------------------------------- #

class TestNetTUIWithInstrumentList:
    def test_composes_with_inst_list(self):
        inst_list = [
            {"instrument": "Rigol_DP832", "address": "USB0::0x1AB1::INSTR",
             "channels": {"power-supply": ["1", "2"]}},
            {"instrument": "LabJack_T7", "address": "USB0::0x0CD5::INSTR",
             "channels": {"adc": ["AIN0", "AIN1"], "gpio": ["EIO0"]}},
        ]
        async def main():
            with patch.object(tui, "_run_script", return_value="[]"):
                app = _make_app(nets=[], inst_list=inst_list)
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
        asyncio.run(main())


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
