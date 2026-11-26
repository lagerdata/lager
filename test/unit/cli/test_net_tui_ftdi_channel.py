#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the FTDI channel picker in the Net-Manager TUI.

On a multi-channel FTDI (FT2232H, FT4232H) the channel a gpio, i2c or spi net
uses is saved as ``params.interface``. The scanner offers one row per pin with
no channel, so the TUI asks for the channel when the net is added, and keys
every duplicate check on it: a net on channel C pin 1 must not hide pin 1 on
channel D. The channel rules are ``nets add --interface``'s own (nets.py), so
both accept and refuse the same values.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
from unittest.mock import patch

import pytest
from click.testing import CliRunner

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

tui = importlib.import_module('cli.commands.box.net_tui')
nets_mod = importlib.import_module('cli.commands.box.nets')
from cli.commands.box.nets import nets as nets_group  # noqa: E402
from cli.errors import LagerError  # noqa: E402
from test.unit.cli.nets_http_fake import FakeBoxHTTP  # noqa: E402

from textual.widgets import Button, Select, Static  # noqa: E402

FT232H_ADDR = "USB0::0x0403::0x6014::FT1ABCD::INSTR"
FT2232H_ADDR = "USB0::0x0403::0x6010::FT2ABCD::INSTR"
FT4232H_ADDR = "USB0::0x0403::0x6011::FT4ABCD::INSTR"


def _static_text(widget) -> str:
    rendered = widget.render()
    plain = getattr(rendered, 'plain', None)
    return plain if isinstance(plain, str) else str(rendered)


def _net(**overrides):
    defaults = dict(instrument='FTDI_FT4232H', chan='1', type='gpio',
                    net='gpio1', addr=FT4232H_ADDR)
    defaults.update(overrides)
    return tui.Net(**defaults)


def _saved(name, chan, interface=None, **overrides):
    return _net(net=name, chan=chan, saved=True, interface=interface, **overrides)


def _cli_error(role, instrument, letter) -> str:
    with pytest.raises(LagerError) as exc:
        nets_mod._ftdi_interface_params(role, instrument, letter)
    return exc.value.problem


class _Recorder:
    """Stands in for the box's PUT /nets/<name>."""

    def __init__(self):
        self.records = []

    def __call__(self, dut, record, old_name=None):
        self.records.append(json.loads(json.dumps(record)))


def _save(nets, interfaces=None, custom_names=None):
    rec = _Recorder()
    with patch.object(tui, "_save_net_http", rec):
        assert tui._save_nets_batch("box", nets, custom_names, interfaces)
    return rec.records


def _addable(nets):
    remaining, _warnings, _notices = tui.AddScreen(nets)._get_addable_nets()
    return remaining


# --------------------------------------------------------------------------- #
# Channel choices                                                             #
# --------------------------------------------------------------------------- #

class TestChannelChoices:
    def test_ft4232h_gpio_offers_all_four(self):
        assert tui._ftdi_channel_choices(_net()) == "ABCD"

    @pytest.mark.parametrize("role,chan", [("i2c", "I2C0"), ("spi", "SPI0")])
    def test_ft4232h_i2c_spi_offer_only_mpsse_channels(self, role, chan):
        assert tui._ftdi_channel_choices(_net(type=role, chan=chan)) == "AB"

    def test_ft2232h_gpio_offers_a_and_b(self):
        net = _net(instrument='FTDI_FT2232H', addr=FT2232H_ADDR, chan='4')
        assert tui._ftdi_channel_choices(net) == "AB"

    @pytest.mark.parametrize("role,chan", [("i2c", "I2C0"), ("spi", "SPI0")])
    @pytest.mark.parametrize("letter", ["C", "D"])
    def test_ft4232h_i2c_spi_refuse_c_and_d_with_cli_message(self, role, chan, letter):
        net = _net(type=role, chan=chan)
        params, error = tui._resolve_ftdi_channel(net, letter, [])
        assert params is None
        assert error == _cli_error(role, 'FTDI_FT4232H', letter)

    def test_valid_pick_returns_interface_params(self):
        params, error = tui._resolve_ftdi_channel(_net(), "c", [])
        assert (params, error) == ({"interface": "C"}, None)

    def test_channel_taken_on_this_pin_is_refused(self):
        saved = _saved("MUX_SEL1", "1", "C")
        params, error = tui._resolve_ftdi_channel(_net(), "C", [saved])
        assert params is None
        assert "MUX_SEL1" in error

    def test_debug_and_uart_get_no_prompt(self):
        assert tui._ftdi_channel_choices(_net(type='debug', chan='DEVICE_TYPE@A')) is None
        assert tui._ftdi_channel_choices(_net(type='uart', chan='/dev/ttyUSB0')) is None


class TestSingleChannelFt232h:
    def test_no_prompt_and_no_interface(self):
        net = _net(instrument='FTDI_FT232H', addr=FT232H_ADDR, chan='5')
        assert tui._ftdi_channel_choices(net) is None
        records = _save([net])
        assert "params" not in records[0]
        assert tui._display_chan(net) == "5"

    def test_add_flow_skips_the_dialog(self):
        net = _net(instrument='FTDI_FT232H', addr=FT232H_ADDR, chan='5')
        screen = tui.AddScreen([net])
        ran = []
        screen._prompt_ftdi_channels(None, [net], lambda: ran.append(True))
        assert ran == [True]
        assert screen.ftdi_interfaces == {}


# --------------------------------------------------------------------------- #
# Identity and duplicate checks                                               #
# --------------------------------------------------------------------------- #

class TestIdentity:
    def test_saved_net_without_interface_counts_as_a(self):
        legacy = _saved("RST", "1")
        explicit = _saved("RST2", "1", "A")
        assert tui._net_identity(legacy) == tui._net_identity(explicit)
        assert tui._free_ftdi_channels([legacy], _net()) == "BCD"
        params, error = tui._resolve_ftdi_channel(_net(), "A", [legacy])
        assert params is None and "RST" in error
        assert tui._display_chan(legacy) == "A:1"

    def test_numeric_interface_normalizes_like_the_cli(self):
        assert tui._ftdi_iface(_saved("X", "1", "2")) == "C"

    def test_channels_c_and_d_are_distinct(self):
        assert (tui._net_identity(_saved("MUX_SEL1", "1", "C"))
                != tui._net_identity(_saved("RESET_N", "1", "D")))

    def test_saved_c_pin_does_not_hide_the_pin_row(self):
        row = _net()
        nets = [_saved("MUX_SEL1", "1", "C"), row]
        assert row in _addable(nets)

    def test_pin_row_hidden_once_every_channel_is_used(self):
        row = _net()
        nets = [_saved(f"n{c}", "1", c) for c in "ABCD"] + [row]
        assert row not in _addable(nets)

    def test_i2c_row_hidden_once_both_mpsse_channels_are_used(self):
        row = _net(type='i2c', chan='I2C0', net='i2c1')
        nets = [_saved("I2C_A", "I2C0", "A", type='i2c'),
                _saved("I2C_B", "I2C0", "B", type='i2c'), row]
        assert row not in _addable(nets)

    def test_non_ftdi_duplicate_check_unchanged(self):
        row = tui.Net('Rigol_DP811', '1', 'power-supply', 'supply1', 'USB0::X')
        saved = tui.Net('Rigol_DP811', '1', 'power-supply', 'vdd', 'USB0::X', saved=True)
        assert row not in _addable([saved, row])

    def test_display_shows_channel(self):
        assert tui._display_chan(_saved("MUX_SEL1", "1", "C")) == "C:1"
        assert tui._display_chan(_net()) == "1"      # unsaved row: not chosen yet
        tree = tui.SavedNetsTree()
        assert "Ch: C:1" in tree._get_net_label(_saved("MUX_SEL1", "1", "C"))


# --------------------------------------------------------------------------- #
# Adding C:1 then D:1 through the app's own refresh path                       #
# --------------------------------------------------------------------------- #

class TestAddTwoChannelsOnOnePin:
    def test_c_then_d(self):
        app = tui.NetApp(ctx=None, dut="box", inst_list=[], nets=[])
        row = _net()
        app.nets = [row]

        first = _save([row], {row.key(): "C"}, {row.key(): "MUX_SEL1"})
        assert first[0]["params"] == {"interface": "C"}
        app._apply_saved_records(first)

        assert row.interface is None          # the row stays channel-less
        assert row in _addable(app.nets)      # and is still offered for D
        assert tui._free_ftdi_channels(app.nets, row) == "ABD"

        second = _save([row], {row.key(): "D"}, {row.key(): "RESET_N"})
        app._apply_saved_records(first + second)

        saved = {n.net: n for n in app.nets if n.saved}
        assert saved["MUX_SEL1"].interface == "C"
        assert saved["RESET_N"].interface == "D"
        assert row in _addable(app.nets)
        assert tui._free_ftdi_channels(app.nets, row) == "AB"

    def test_autogen_rebuild_keeps_one_row_per_pin(self):
        inst = [{"name": "FTDI_FT4232H", "address": FT4232H_ADDR,
                 "channels": {"gpio": ["1"]}}]
        app = tui.NetApp(ctx=None, dut="box", inst_list=inst, nets=[])
        app._ensure_autogen_unsaved()
        app._apply_saved_records([{
            "name": "MUX_SEL1", "role": "gpio", "instrument": "FTDI_FT4232H",
            "address": FT4232H_ADDR, "pin": "1", "params": {"interface": "C"}}])
        rows = [n for n in app.nets if not n.saved]
        assert len(rows) == 1 and rows[0].interface is None


# --------------------------------------------------------------------------- #
# Edits keep params.interface                                                 #
# --------------------------------------------------------------------------- #

class TestEditsKeepInterface:
    def _records(self):
        return [{"name": "MUX_SEL1", "role": "gpio", "instrument": "FTDI_FT4232H",
                 "address": FT4232H_ADDR, "pin": "1", "params": {"interface": "C"}}]

    def test_metadata_edit(self):
        target = tui._merge_net_metadata(self._records(), "MUX_SEL1", "gpio",
                                         "selects a mux input", "", [])
        assert target["params"] == {"interface": "C"}

    def test_existing_params_survive_alongside_interface(self):
        net = _net(params={"other": 1})
        records = _save([net], {net.key(): "B"})
        assert records[0]["params"] == {"other": 1, "interface": "B"}

    def test_resave_of_loaded_net_keeps_interface(self):
        app = tui.NetApp(ctx=None, dut="box", inst_list=[], nets=[])
        app._apply_saved_records(self._records())
        loaded = next(n for n in app.nets if n.saved)
        assert _save([loaded])[0]["params"] == {"interface": "C"}


# --------------------------------------------------------------------------- #
# The dialog                                                                  #
# --------------------------------------------------------------------------- #

class TestDialogFlow:
    def _run(self, nets, selected, actions):
        """Run the add flow's channel prompts; ``actions`` drives each dialog."""
        result = {}

        async def main():
            app = tui.NetApp(ctx=None, dut="box", inst_list=[], nets=list(nets))
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                screen = tui.AddScreen(app.nets)
                app.push_screen(screen)
                await pilot.pause()
                screen._prompt_ftdi_channels(
                    app, selected, lambda: result.setdefault("done", True))
                for act in actions:
                    await pilot.pause()
                    dialog = app.screen
                    assert isinstance(dialog, tui.FtdiChannelDialog)
                    await act(dialog, pilot, result)
                await pilot.pause()
                result["interfaces"] = dict(screen.ftdi_interfaces)
        asyncio.run(main())
        return result

    def test_i2c_dialog_offers_a_and_b_and_saves_pick(self):
        row = _net(type='i2c', chan='I2C0', net='i2c1')

        async def pick_b(dialog, pilot, result):
            select = dialog.query_one("#ftdi_channel", Select)
            result["options"] = dialog.free
            select.value = "B"
            await pilot.pause()
            dialog._on_ftdi_button(
                Button.Pressed(dialog.query_one("#ftdi-confirm", Button)))

        result = self._run([row], [row], [pick_b])
        assert result["options"] == "AB"
        assert result["done"] is True
        assert result["interfaces"] == {row.key(): "B"}

    def test_taken_channels_are_not_offered_and_last_pick_is_preselected(self):
        rows = [_net(chan=str(p), net=f"gpio{p}") for p in (1, 2)]
        saved = _saved("RESET_N", "1", "D")

        async def pick_c(dialog, pilot, result):
            result.setdefault("free", []).append(dialog.free)
            dialog.query_one("#ftdi_channel", Select).value = "C"
            await pilot.pause()
            dialog._on_ftdi_button(
                Button.Pressed(dialog.query_one("#ftdi-confirm", Button)))

        async def accept(dialog, pilot, result):
            result.setdefault("free", []).append(dialog.free)
            result["preselected"] = dialog.query_one("#ftdi_channel", Select).value
            dialog._on_ftdi_button(
                Button.Pressed(dialog.query_one("#ftdi-confirm", Button)))

        result = self._run(rows + [saved], rows, [pick_c, accept])
        assert result["free"] == ["ABC", "ABCD"]
        assert result["preselected"] == "C"
        assert result["interfaces"] == {rows[0].key(): "C", rows[1].key(): "C"}

    def test_cancel_aborts(self):
        row = _net()

        async def cancel(dialog, pilot, result):
            dialog._on_ftdi_button(
                Button.Pressed(dialog.query_one("#ftdi-cancel", Button)))

        result = self._run([row], [row], [cancel])
        assert "done" not in result
        assert result["interfaces"] == {}

    def test_resolver_error_keeps_dialog_up(self):
        row = _net(type='spi', chan='SPI0', net='spi1')
        holder = {}

        async def refuse(dialog, pilot, result):
            # Offer a value the dialog never would, to reach the shared
            # validation behind it.
            select = dialog.query_one("#ftdi_channel", Select)
            select.set_options([(c, c) for c in "ABCD"])
            select.value = "D"
            await pilot.pause()
            dialog._on_ftdi_button(
                Button.Pressed(dialog.query_one("#ftdi-confirm", Button)))
            await pilot.pause()
            holder["still_up"] = pilot.app.screen is dialog
            holder["warn"] = _static_text(dialog.query_one("#ftdi_warn", Static))
            dialog._on_ftdi_button(
                Button.Pressed(dialog.query_one("#ftdi-cancel", Button)))

        self._run([row], [row], [refuse])
        assert holder["still_up"] is True
        assert _cli_error('spi', 'FTDI_FT4232H', 'D') in holder["warn"]


# --------------------------------------------------------------------------- #
# Parity with `lager nets add-batch`                                          #
# --------------------------------------------------------------------------- #

_TABLE = (
    [(name, "gpio", str(pin), "C")
     for pin, name in enumerate(
         ["MUX_SEL1", "MUX_SEL2", "MUX_SEL3", "MUX_SEL4", "MUX_SEL5", "MUX_SEL6"], start=1)]
    + [("RESET_N", "gpio", "1", "D"), ("SENSOR_I2C", "i2c", "I2C0", "B")]
)

_KEYS = ("name", "role", "address", "instrument", "pin", "params")


class TestAddBatchParity:
    def _tui_records(self):
        records = []
        saved: list = []
        for name, role, pin, letter in _TABLE:
            row = _net(type=role, chan=pin, net=f"{role}{pin}")
            params, error = tui._resolve_ftdi_channel(row, letter, saved)
            assert error is None, error
            got = _save([row], {row.key(): params["interface"]}, {row.key(): name})
            records.extend(got)
            saved.append(_saved(name, pin, params["interface"], type=role))
        return records

    def _batch_records(self, tmp_path):
        ft4232h = {
            "name": "FTDI_FT4232H", "vid": "0403", "pid": "6011", "serial": "FT4ABCD",
            "address": FT4232H_ADDR,
            "net_type": ["debug", "uart", "spi", "i2c", "gpio"],
            "channels": {"debug": ["DEVICE_TYPE@A", "DEVICE_TYPE@B"], "uart": [],
                         "spi": ["SPI0"], "i2c": ["I2C0"],
                         "gpio": [str(n) for n in range(8)]},
        }
        box = FakeBoxHTTP([ft4232h])
        batch = [{"name": name, "role": role, "channel": pin, "address": FT4232H_ADDR,
                  "instrument": "FTDI_FT4232H", "params": {"interface": letter}}
                 for name, role, pin, letter in _TABLE]
        path = tmp_path / "nets.json"
        path.write_text(json.dumps(batch))
        with patch("requests.request", box.request), \
             patch("cli.box_storage.resolve_and_validate_box",
                   lambda ctx, name: name or "testbox"), \
             patch.object(nets_mod, "_resolve_box", lambda ctx, name: name or "testbox"):
            result = CliRunner().invoke(
                nets_group, ["add-batch", str(path), "--box", "b"],
                catch_exceptions=False)
        assert result.exit_code == 0, result.output
        return box.saved_nets

    def test_tui_records_match_add_batch(self, tmp_path):
        def norm(records):
            return sorted(({k: r.get(k) for k in _KEYS} for r in records),
                          key=lambda r: r["name"])
        tui_records = norm(self._tui_records())
        batch_records = norm(self._batch_records(tmp_path))
        assert tui_records == batch_records
        assert len(tui_records) == 8
