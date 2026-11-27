#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for FTDI channels in the Net-Manager TUI.

On a multi-channel FTDI (FT2232H, FT4232H) the channel a gpio, i2c or spi net
uses is saved as ``params.interface``. The scanner reports pins without a
channel, so the Add list shows each pin once per channel its role may use
(``A:0`` to ``D:7`` for gpio on an FT4232H, ``A``/``B`` for i2c and spi), and
every duplicate check keys on the channel: a net on channel C pin 1 hides only
the ``C:1`` row. The channel rules are ``nets add --interface``'s own
(nets.py), so both accept the same values.
"""

from __future__ import annotations

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
from test.unit.cli.nets_http_fake import FakeBoxHTTP  # noqa: E402

FT232H_ADDR = "USB0::0x0403::0x6014::FT1ABCD::INSTR"
FT2232H_ADDR = "USB0::0x0403::0x6010::FT2ABCD::INSTR"
FT4232H_ADDR = "USB0::0x0403::0x6011::FT4ABCD::INSTR"


def _scan(name="FTDI_FT4232H", addr=FT4232H_ADDR, gpio=8):
    """One scanned FTDI part, as the box's instrument list reports it."""
    return {"name": name, "address": addr,
            "channels": {"debug": ["DEVICE_TYPE@A", "DEVICE_TYPE@B"],
                         "i2c": ["I2C0"], "spi": ["SPI0"],
                         "gpio": [str(n) for n in range(gpio)]}}


def _net(**overrides):
    defaults = dict(instrument='FTDI_FT4232H', chan='1', type='gpio',
                    net='gpio1', addr=FT4232H_ADDR)
    defaults.update(overrides)
    return tui.Net(**defaults)


def _row(chan='1', interface='A', **overrides):
    return _net(chan=chan, interface=interface, **overrides)


def _saved(name, chan, interface=None, **overrides):
    return _net(net=name, chan=chan, saved=True, interface=interface, **overrides)


class _Recorder:
    """Stands in for the box's PUT /nets/<name>."""

    def __init__(self):
        self.records = []

    def __call__(self, dut, record, old_name=None):
        self.records.append(json.loads(json.dumps(record)))


def _save(nets, custom_names=None):
    rec = _Recorder()
    with patch.object(tui, "_save_net_http", rec):
        assert tui._save_nets_batch("box", nets, custom_names)
    return rec.records


def _addable(nets):
    remaining, _warnings, _notices = tui.AddScreen(nets)._get_addable_nets()
    return remaining


def _autogen(inst_list, saved_records=()):
    app = tui.NetApp(ctx=None, dut="box", inst_list=inst_list, nets=[])
    app._ensure_autogen_unsaved()
    if saved_records:
        app._apply_saved_records(list(saved_records))
    return app


def _launch_rows(inst_list, saved_records=()):
    """The rows launch_tui builds before the app starts."""
    built = {}

    class _App:
        def __init__(self, ctx, dut, inst, nets):
            built["nets"] = nets

        def run(self):
            pass

    with patch.object(tui, "_fetch_instruments_http", lambda dut: inst_list), \
         patch.object(tui, "_fetch_saved_nets_http", lambda dut: list(saved_records)), \
         patch.object(tui, "NetApp", _App):
        tui.launch_tui(None, "box")
    return built["nets"]


def _shown(nets, role):
    return [tui._display_chan(n) for n in nets if not n.saved and n.type == role]


# --------------------------------------------------------------------------- #
# Every channel is a row                                                      #
# --------------------------------------------------------------------------- #

GPIO_FT4232H = [f"{c}:{p}" for c in "ABCD" for p in range(8)]


class TestRowsPerChannel:
    @pytest.mark.parametrize("build", [lambda i: _autogen(i).nets, _launch_rows])
    def test_ft4232h_shows_every_channel_and_pin(self, build):
        nets = build([_scan()])
        assert _shown(nets, "gpio") == GPIO_FT4232H
        assert _shown(nets, "i2c") == ["A:I2C0", "B:I2C0"]
        assert _shown(nets, "spi") == ["A:SPI0", "B:SPI0"]

    @pytest.mark.parametrize("build", [lambda i: _autogen(i).nets, _launch_rows])
    def test_ft2232h_shows_channels_a_and_b(self, build):
        nets = build([_scan("FTDI_FT2232H", FT2232H_ADDR, gpio=2)])
        assert _shown(nets, "gpio") == ["A:0", "A:1", "B:0", "B:1"]
        assert _shown(nets, "i2c") == ["A:I2C0", "B:I2C0"]

    @pytest.mark.parametrize("build", [lambda i: _autogen(i).nets, _launch_rows])
    def test_ft232h_and_debug_rows_are_unchanged(self, build):
        nets = build([_scan("FTDI_FT232H", FT232H_ADDR, gpio=2)])
        assert _shown(nets, "gpio") == ["0", "1"]
        assert all(n.interface is None for n in nets)
        ft4232h = build([_scan()])
        assert _shown(ft4232h, "debug") == ["DEVICE_TYPE@A", "DEVICE_TYPE@B"]

    def test_every_row_key_is_unique(self):
        nets = _launch_rows([_scan()])
        assert len({n.key() for n in nets}) == len(nets)

    def test_rows_get_distinct_names(self):
        nets = _autogen([_scan()]).nets
        names = [n.net for n in nets if n.type == "gpio"]
        assert len(set(names)) == 32

    def test_every_offered_channel_is_one_nets_add_accepts(self):
        for n in _autogen([_scan()]).nets:
            if n.interface is not None:
                params = nets_mod._ftdi_interface_params(n.type, n.instrument, n.interface)
                assert params == {"interface": n.interface}

    def test_rebuild_does_not_duplicate_rows(self):
        app = _autogen([_scan()])
        count = len(app.nets)
        app._ensure_autogen_unsaved()
        assert len(app.nets) == count


# --------------------------------------------------------------------------- #
# Identity and duplicate checks                                               #
# --------------------------------------------------------------------------- #

class TestIdentity:
    def test_saved_net_without_interface_counts_as_a(self):
        legacy = _saved("RST", "1")
        assert tui._net_identity(legacy) == tui._net_identity(_row("1", "A"))
        assert tui._display_chan(legacy) == "A:1"
        nets = [legacy, _row("1", "A"), _row("1", "B")]
        assert [tui._display_chan(n) for n in _addable(nets)] == ["B:1"]

    def test_numeric_interface_normalizes_like_the_cli(self):
        assert tui._ftdi_iface(_saved("X", "1", "2")) == "C"

    def test_a_saved_c1_hides_only_the_c1_row(self):
        rows = [_row("1", c) for c in "ABCD"]
        nets = [_saved("MUX_SEL1", "1", "C")] + rows
        assert [tui._display_chan(n) for n in _addable(nets)] == ["A:1", "B:1", "D:1"]

    def test_launch_hides_saved_channels(self):
        saved = [{"name": "MUX_SEL1", "role": "gpio", "instrument": "FTDI_FT4232H",
                  "address": FT4232H_ADDR, "pin": "1", "params": {"interface": "C"}}]
        nets = _launch_rows([_scan()], saved)
        shown = [tui._display_chan(n) for n in _addable(nets) if n.type == "gpio"]
        assert "C:1" not in shown
        assert len(shown) == 31

    def test_non_ftdi_duplicate_check_unchanged(self):
        row = tui.Net('Rigol_DP811', '1', 'power-supply', 'supply1', 'USB0::X')
        saved = tui.Net('Rigol_DP811', '1', 'power-supply', 'vdd', 'USB0::X', saved=True)
        assert row not in _addable([saved, row])

    def test_labels_show_the_channel(self):
        assert "Ch: C:1" in tui.SavedNetsTree()._get_net_label(_saved("MUX_SEL1", "1", "C"))
        assert "Ch: D:7" in tui.AddNetsTree()._get_net_label(_row("7", "D"), False)


# --------------------------------------------------------------------------- #
# Saving                                                                      #
# --------------------------------------------------------------------------- #

class TestSave:
    def test_the_rows_channel_is_saved_as_the_interface(self):
        row = _row("1", "C")
        records = _save([row], {row.key(): "MUX_SEL1"})
        assert records[0]["params"] == {"interface": "C"}
        assert records[0]["pin"] == "1"

    def test_ft232h_saves_no_interface(self):
        net = _net(instrument='FTDI_FT232H', addr=FT232H_ADDR, chan='5')
        assert "params" not in _save([net])[0]

    def test_existing_params_survive_alongside_interface(self):
        assert _save([_row("1", "B", params={"other": 1})])[0]["params"] == {
            "other": 1, "interface": "B"}

    def test_c1_then_d1_on_the_same_pin(self):
        app = _autogen([_scan()])
        c1 = next(n for n in app.nets if tui._display_chan(n) == "C:1")
        d1 = next(n for n in app.nets if tui._display_chan(n) == "D:1")
        first = _save([c1], {c1.key(): "MUX_SEL1"})
        second = _save([d1], {d1.key(): "RESET_N"})
        app._apply_saved_records(first + second)
        saved = {n.net: n.interface for n in app.nets if n.saved}
        assert saved == {"MUX_SEL1": "C", "RESET_N": "D"}
        shown = [tui._display_chan(n) for n in _addable(app.nets) if n.type == "gpio"]
        assert "C:1" not in shown and "D:1" not in shown and "A:1" in shown


class TestEditsKeepInterface:
    def _records(self):
        return [{"name": "MUX_SEL1", "role": "gpio", "instrument": "FTDI_FT4232H",
                 "address": FT4232H_ADDR, "pin": "1", "params": {"interface": "C"}}]

    def test_metadata_edit(self):
        target = tui._merge_net_metadata(self._records(), "MUX_SEL1", "gpio",
                                         "selects a mux input", "", [])
        assert target["params"] == {"interface": "C"}

    def test_resave_of_loaded_net_keeps_interface(self):
        app = tui.NetApp(ctx=None, dut="box", inst_list=[], nets=[])
        app._apply_saved_records(self._records())
        loaded = next(n for n in app.nets if n.saved)
        assert _save([loaded])[0]["params"] == {"interface": "C"}

    def test_rename_keeps_the_channel_in_the_row_key(self):
        a1, c1 = _row("1", "A", net="x"), _row("1", "C", net="x")
        assert a1.key() != c1.key()
        c1.net = "y"
        c1.rekey()
        assert c1.key() != _row("1", "A", net="y").key()


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
        rows = {(n.type, str(n.chan), n.interface): n
                for n in _autogen([_scan()]).nets if not n.saved}
        records = []
        for name, role, pin, letter in _TABLE:
            row = rows[(role, pin, letter)]
            records.extend(_save([row], {row.key(): name}))
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
