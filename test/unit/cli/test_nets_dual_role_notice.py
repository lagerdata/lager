#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Dual-role single-channel instruments: warn once instead of hard-blocking.

A chip in ``_SINGLE_CHANNEL_INST`` (Keithley 2281S, EA PSB, Rigol DP711) has
one physical output that fills one role at a time, but the drivers re-assert
their own entry mode on every write — so a deliberate two-net setup (battery
AND power-supply on one 2281S, alternated across a test) works. A saved net
must therefore not hide the other role's add row; it gets an informational
notice (``dual_role_notice``) instead, and stays unselected by default.

The FT232H (``_MODE_EXCLUSIVE_INST``) keeps the hard block: its MPSSE-vs-UART
mode is fixed per open with no driver-side switching, so a second role there
genuinely cannot work.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
from unittest.mock import patch

import pytest
from click.testing import CliRunner

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

tui = importlib.import_module('cli.commands.box.net_tui')
nets_mod = importlib.import_module('cli.commands.box.nets')
from cli.commands.box.nets import nets as nets_group  # noqa: E402
from test.unit.cli.nets_http_fake import FakeBoxHTTP  # noqa: E402

from textual.widgets import Button, Static  # noqa: E402


KEITHLEY_ADDR = "USB0::0x05E6::0x2281::4519728::INSTR"
FT232H_ADDR = "USB0::0x0403::0x6014::FT7ADBQ0::INSTR"
DP711_ADDR = "serial://00000006"

NOTICE_MARKER = "single output that fills one role at a time"


def _keithley_net(role, name, saved):
    return tui.Net('Keithley_2281S', '1', role, name, KEITHLEY_ADDR, saved=saved)


def _static_text(widget) -> str:
    rendered = widget.render()
    plain = getattr(rendered, 'plain', None)
    return plain if isinstance(plain, str) else str(rendered)


def _notice_texts(screen) -> list[str]:
    return [_static_text(w) for w in screen.query("#add_notices Static")]


def _push_add_screen(all_nets):
    """Run the TUI, push an AddScreen over *all_nets*, and return
    (displayed_keys, chosen, block_texts)."""
    async def main():
        app = tui.NetApp(ctx=None, dut="box", inst_list=[], nets=list(all_nets))
        async with app.run_test(size=(120, 50)) as pilot:
            await pilot.pause()
            screen = tui.AddScreen(list(all_nets))
            app.push_screen(screen)
            await pilot.pause()
            chosen = set(screen.add_tree.chosen) if screen.add_tree else set()
            return set(screen.displayed_nets), chosen, _notice_texts(screen)
    return asyncio.run(main())


# --------------------------------------------------------------------------- #
# dual_role_notice: the shared wording                                        #
# --------------------------------------------------------------------------- #

class TestNoticeText:
    def test_interpolates_instrument_address_and_roles(self):
        text = tui.dual_role_notice("Keithley_2281S", KEITHLEY_ADDR, "battery")
        assert text.startswith(
            f"Keithley_2281S at {KEITHLEY_ADDR} already has a battery net.")
        assert NOTICE_MARKER in text
        assert text.endswith("only if you intend to use both roles.")

    def test_cli_imports_the_same_message(self):
        # nets.py must not grow its own copy of the wording.
        assert nets_mod.dual_role_notice is tui.dual_role_notice


# --------------------------------------------------------------------------- #
# Add screen: taken 2281S offers the second role, unselected, with notice     #
# --------------------------------------------------------------------------- #

class TestAddScreenDualRole:
    def test_second_role_row_present_unselected_with_notice(self):
        batt = _keithley_net('battery', 'batt1', saved=True)
        supply = _keithley_net('power-supply', 'supply1', saved=False)
        displayed, chosen, notices = _push_add_screen([batt, supply])
        assert supply.key() in displayed
        assert chosen == set()
        assert any(NOTICE_MARKER in t for t in notices)
        assert any(
            f"Keithley_2281S at {KEITHLEY_ADDR} already has a battery net."
            in t for t in notices)
        # The old hidden-row warning is gone for this bucket.
        assert not any("already has a net." in t for t in notices)

    def test_fresh_chip_gets_no_notice(self):
        batt = _keithley_net('battery', 'batt1', saved=False)
        supply = _keithley_net('power-supply', 'supply1', saved=False)
        displayed, _chosen, notices = _push_add_screen([batt, supply])
        assert {batt.key(), supply.key()} <= displayed
        assert not any(NOTICE_MARKER in t for t in notices)

    def test_taken_ft232h_rows_stay_hidden_with_old_warning(self):
        saved_spi = tui.Net('FTDI_FT232H', '0', 'spi', 'spi1', FT232H_ADDR,
                            saved=True)
        cand_i2c = tui.Net('FTDI_FT232H', '0', 'i2c', 'i2c1', FT232H_ADDR,
                           saved=False)
        cand_gpio = tui.Net('FTDI_FT232H', 'AD4', 'gpio', 'gpio1', FT232H_ADDR,
                            saved=False)
        displayed, _chosen, texts = _push_add_screen(
            [saved_spi, cand_i2c, cand_gpio])
        assert displayed == set()
        assert any(
            f"FTDI_FT232H at {FT232H_ADDR} already has a net." in t
            for t in texts)
        assert not any(NOTICE_MARKER in t for t in texts)

    def test_dp711_with_saved_net_offers_no_row_and_no_notice(self):
        # One role, one channel: the ordinary already-saved-channel rule
        # hides the row, so no special casing (and no notice) is needed.
        saved = tui.Net('Rigol_DP711', '1', 'power-supply', 'supply1',
                        DP711_ADDR, saved=True)
        cand = tui.Net('Rigol_DP711', '1', 'power-supply', 'supply1',
                       DP711_ADDR, saved=False)
        displayed, _chosen, texts = _push_add_screen([saved, cand])
        assert displayed == set()
        assert not any(NOTICE_MARKER in t for t in texts)


# --------------------------------------------------------------------------- #
# Add screen confirm: conflicts count selected rows only                      #
# --------------------------------------------------------------------------- #

class TestBatchConflicts:
    def _confirm(self, all_nets, selection):
        """Push an AddScreen over *all_nets*, select *selection*, press
        Add Selected; return (save_count, hint_text)."""
        async def main():
            app = tui.NetApp(ctx=None, dut="box", inst_list=[], nets=all_nets)
            app._fetch_saved_records = lambda: []
            with patch.object(tui, '_save_nets_batch', return_value=True) as sb:
                async with app.run_test(size=(120, 50)) as pilot:
                    await pilot.pause()
                    screen = tui.AddScreen(all_nets)
                    app.push_screen(screen)
                    await pilot.pause()
                    for n in selection:
                        screen.add_tree.toggle_net(n.key())
                    screen.on_button_pressed(
                        Button.Pressed(
                            screen.query_one("#add-confirm", Button)))
                    await app.workers.wait_for_complete()
                    await pilot.pause()
                    hint = ""
                    if isinstance(app.screen, tui.AddScreen):
                        try:
                            hint = _static_text(
                                app.screen.query_one("#keithley_hint", Static))
                        except Exception:
                            pass
                    return sb.call_count, hint
        return asyncio.run(main())

    def test_two_selected_rows_on_one_chip_still_conflict(self):
        batt = _keithley_net('battery', 'batt1', saved=False)
        supply = _keithley_net('power-supply', 'supply1', saved=False)
        saves, hint = self._confirm([batt, supply], [batt, supply])
        assert saves == 0
        assert 'Only one net' in hint

    def test_one_selected_row_next_to_a_saved_net_saves(self):
        batt = _keithley_net('battery', 'batt1', saved=True)
        supply = _keithley_net('power-supply', 'supply1', saved=False)
        saves, hint = self._confirm([batt, supply], [supply])
        assert saves == 1
        assert 'Only one net' not in hint


# --------------------------------------------------------------------------- #
# CLI: nets add / add-all                                                     #
# --------------------------------------------------------------------------- #

KEITHLEY_INST = {
    "name": "Keithley_2281S",
    "vid": "05e6", "pid": "2281", "serial": "4519728",
    "address": KEITHLEY_ADDR,
    "net_type": ["battery", "power-supply"],
    "channels": {"battery": ["1"], "power-supply": ["1"]},
}
FT232H_INST = {
    "name": "FTDI_FT232H",
    "vid": "0403", "pid": "6014", "serial": "FT7ADBQ0",
    "address": FT232H_ADDR,
    "net_type": ["spi", "i2c", "gpio"],
    "channels": {"spi": ["0"], "i2c": ["0"], "gpio": ["AD4"]},
}

SAVED_BATT = {
    "name": "batt1", "role": "battery", "instrument": "Keithley_2281S",
    "pin": "1", "address": KEITHLEY_ADDR,
}
SAVED_SPI = {
    "name": "spi1", "role": "spi", "instrument": "FTDI_FT232H",
    "pin": "0", "address": FT232H_ADDR,
}


@pytest.fixture
def box_factory():
    started: list = []

    def _make(instruments):
        box = FakeBoxHTTP(instruments)
        for patcher in (
            patch("requests.request", box.request),
            patch("cli.box_storage.resolve_and_validate_box",
                  lambda _ctx, name: name or "testbox"),
            patch.object(nets_mod, "_resolve_box",
                         lambda _ctx, name: name or "testbox"),
        ):
            patcher.start()
            started.append(patcher)
        return box

    yield _make

    for patcher in reversed(started):
        patcher.stop()


def _invoke(args, input=None):
    result = CliRunner().invoke(nets_group, args, input=input,
                                catch_exceptions=False)
    try:
        stderr = result.stderr
    except ValueError:
        stderr = ""
    output = result.output
    if stderr and stderr not in output:
        output += stderr
    return result, output


class TestCliAdd:
    def test_second_role_add_succeeds_with_notice(self, box_factory):
        box = box_factory([KEITHLEY_INST])
        box.saved_nets.append(dict(SAVED_BATT))
        result, output = _invoke(
            ["add", "supply1", "power-supply", "1", KEITHLEY_ADDR, "--box", "b"])
        assert result.exit_code == 0, output
        assert NOTICE_MARKER in output
        assert "already has a battery net." in output
        roles = {n["role"] for n in box.saved_nets}
        assert roles == {"battery", "power-supply"}

    def test_notice_names_legacy_saved_roles_canonically(self, box_factory):
        # A legacy net stored with the short "batt" token still reads as
        # "battery" in the notice.
        box = box_factory([KEITHLEY_INST])
        box.saved_nets.append(dict(SAVED_BATT, role="batt"))
        result, output = _invoke(
            ["add", "supply1", "power-supply", "1", KEITHLEY_ADDR, "--box", "b"])
        assert result.exit_code == 0, output
        assert "already has a battery net." in output

    def test_first_net_on_a_fresh_chip_gets_no_notice(self, box_factory):
        box = box_factory([KEITHLEY_INST])
        result, output = _invoke(
            ["add", "batt1", "battery", "1", KEITHLEY_ADDR, "--box", "b"])
        assert result.exit_code == 0, output
        assert NOTICE_MARKER not in output


class TestCliAddAll:
    def test_second_role_created_with_notice(self, box_factory):
        box = box_factory([KEITHLEY_INST])
        box.saved_nets.append(dict(SAVED_BATT))
        result, output = _invoke(["add-all", "--box", "b"], input="y\n")
        assert result.exit_code == 0, output
        assert NOTICE_MARKER in output
        roles = {n["role"] for n in box.saved_nets}
        assert roles == {"battery", "power-supply"}

    def test_fresh_chip_is_refused_with_pick_one_guidance(self, box_factory):
        # No saved net + candidates for both roles: add-all can't pick for
        # the user and must not double-book the chip's one output. (Before
        # this feature the chip never even got here — the scanner-duplicate
        # detector keyed channels per device only, so battery "1" and
        # power-supply "1" read as a duplicate and the chip was skipped
        # with a warning blaming the scanner.)
        box = box_factory([KEITHLEY_INST])
        result, output = _invoke(["add-all", "--box", "b"], input="y\n")
        assert "offers multiple roles (battery, power-supply)" in output
        assert "offered the same channel twice" not in output
        assert box.saved_nets == []

    def test_taken_ft232h_still_refused_with_old_warning(self, box_factory):
        box = box_factory([FT232H_INST])
        box.saved_nets.append(dict(SAVED_SPI))
        result, output = _invoke(["add-all", "--box", "b"], input="y\n")
        assert f"FTDI_FT232H at {FT232H_ADDR} already has a net." in output
        assert NOTICE_MARKER not in output
        assert box.saved_nets == [SAVED_SPI]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
