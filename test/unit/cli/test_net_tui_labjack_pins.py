#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the TUI LabJack i2c/spi pin-picker
(cli/commands/box/net_tui.py + cli/commands/box/labjack_pins.py).

The Add Nets flow shows a pin dialog before saving LabJack i2c/spi nets.
Accepting the prefilled defaults must save the exact record the TUI saved
before the dialog existed (legacy channel string, no params); custom
selections are persisted via the net record's ``params`` dict — the format
the box dispatchers already consume.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

tui = importlib.import_module('cli.commands.box.net_tui')
lj = importlib.import_module('cli.commands.box.labjack_pins')

LJ_ADDR = "USB0::0x0CD5::0x0007::470012345::INSTR"


def _static_text(widget) -> str:
    """A Static's content as plain text across textual versions.

    ``Static.renderable`` was removed in textual 8.x; ``render()`` exists on
    both old and new versions (returning a str/Text on 3.x and a Content on
    8.x, both of which carry the plain text).
    """
    rendered = widget.render()
    plain = getattr(rendered, 'plain', None)
    return plain if isinstance(plain, str) else str(rendered)


def _make_net(**overrides):
    defaults = dict(
        instrument='LabJack_T7',
        chan='FIO0-FIO3',
        type='spi',
        net='spi1',
        addr=LJ_ADDR,
    )
    defaults.update(overrides)
    return tui.Net(**defaults)


# --------------------------------------------------------------------------- #
# labjack_pins: pure helpers                                                  #
# --------------------------------------------------------------------------- #

class TestPinHelpers:
    def test_all_pin_names_roundtrip(self):
        assert len(lj.ALL_PIN_NAMES) == 23
        for dio, name in enumerate(lj.ALL_PIN_NAMES):
            assert lj.try_parse_pin(name) == dio
            assert lj.pin_name(dio) == name

    @pytest.mark.parametrize('bad', ['FIO8', 'EIO9', 'CIO4', 'MIO3', '23', '-1', 'x'])
    def test_invalid_pins_return_none(self, bad):
        assert lj.try_parse_pin(bad) is None


class TestClaimedPinsFromChan:
    def test_gpio_chan(self):
        assert lj.claimed_pins_from_chan('gpio', 'FIO3') == ['FIO3']
        assert lj.claimed_pins_from_chan('gpio', 'EIO0') == ['EIO0']
        assert lj.claimed_pins_from_chan('gpio', 'bogus') == []

    def test_default_spi_strings(self):
        assert lj.claimed_pins_from_chan('spi', 'FIO0-FIO3') == [
            'FIO0', 'FIO1', 'FIO2', 'FIO3']
        assert lj.claimed_pins_from_chan('spi', 'FIO1-FIO3') == [
            'FIO1', 'FIO2', 'FIO3']

    def test_default_i2c_string(self):
        assert lj.claimed_pins_from_chan('i2c', 'FIO4-FIO5') == ['FIO4', 'FIO5']

    def test_labeled_custom_strings(self):
        assert lj.claimed_pins_from_chan('i2c', 'SDA:EIO0 SCL:EIO1') == [
            'EIO0', 'EIO1']
        assert lj.claimed_pins_from_chan(
            'spi', 'CS:FIO6 SCK:FIO7 MOSI:EIO0 MISO:EIO1'
        ) == ['FIO6', 'FIO7', 'EIO0', 'EIO1']
        # 3-pin SPI label (no CS token)
        assert lj.claimed_pins_from_chan('spi', 'SCK:FIO7 MOSI:EIO0 MISO:EIO1') == [
            'FIO7', 'EIO0', 'EIO1']

    def test_other_roles_claim_nothing(self):
        assert lj.claimed_pins_from_chan('adc', 'AIN0') == []
        assert lj.claimed_pins_from_chan('dac', 'DAC0') == []


class TestCurrentPinSelection:
    def test_no_params_returns_defaults(self):
        assert lj.current_pin_selection('i2c', None) == lj.I2C_DEFAULT_PINS
        assert lj.current_pin_selection('spi', None) == lj.SPI_DEFAULT_PINS
        assert lj.current_pin_selection('i2c', {}) == lj.I2C_DEFAULT_PINS

    def test_custom_params_decode(self):
        assert lj.current_pin_selection('i2c', {'sda_pin': 8, 'scl_pin': 9}) == {
            'SDA': 'EIO0', 'SCL': 'EIO1'}
        assert lj.current_pin_selection(
            'spi', {'cs_pin': 6, 'clk_pin': 7, 'mosi_pin': 8, 'miso_pin': 9}
        ) == {'CS': 'FIO6', 'SCK': 'FIO7', 'MOSI': 'EIO0', 'MISO': 'EIO1'}

    def test_spi_without_cs_round_trips_to_no_cs(self):
        assert lj.current_pin_selection(
            'spi', {'clk_pin': 1, 'mosi_pin': 2, 'miso_pin': 3}
        ) == {'CS': lj.NO_CS, 'SCK': 'FIO1', 'MOSI': 'FIO2', 'MISO': 'FIO3'}

    def test_round_trip_with_resolve(self):
        # resolve -> current_pin_selection must reproduce the chosen pins.
        chosen = {'SDA': 'CIO0', 'SCL': 'MIO1'}
        _label, params, error = lj.resolve_pin_selection('i2c', chosen)
        assert error is None
        assert lj.current_pin_selection('i2c', params) == chosen


class TestResolvePinSelection:
    def test_i2c_defaults_return_none(self):
        label, params, error = lj.resolve_pin_selection(
            'i2c', {'SDA': 'FIO4', 'SCL': 'FIO5'})
        assert (label, params, error) == (None, None, None)

    def test_spi_defaults_return_none(self):
        label, params, error = lj.resolve_pin_selection(
            'spi', {'CS': 'FIO0', 'SCK': 'FIO1', 'MOSI': 'FIO2', 'MISO': 'FIO3'})
        assert (label, params, error) == (None, None, None)

    def test_i2c_custom(self):
        label, params, error = lj.resolve_pin_selection(
            'i2c', {'SDA': 'EIO0', 'SCL': 'EIO1'})
        assert error is None
        assert label == 'SDA:EIO0 SCL:EIO1'
        assert params == {'sda_pin': 8, 'scl_pin': 9}

    def test_spi_custom_with_cs(self):
        label, params, error = lj.resolve_pin_selection(
            'spi', {'CS': 'FIO6', 'SCK': 'FIO7', 'MOSI': 'EIO0', 'MISO': 'EIO1'})
        assert error is None
        assert label == 'CS:FIO6 SCK:FIO7 MOSI:EIO0 MISO:EIO1'
        assert params == {'cs_pin': 6, 'clk_pin': 7, 'mosi_pin': 8, 'miso_pin': 9}

    def test_spi_no_cs_is_custom_even_with_default_others(self):
        # Dropping CS from the default mapping is a meaningful change
        # (3-pin SPI, manual chip select) and must produce params.
        label, params, error = lj.resolve_pin_selection(
            'spi', {'CS': lj.NO_CS, 'SCK': 'FIO1', 'MOSI': 'FIO2', 'MISO': 'FIO3'})
        assert error is None
        assert label == 'SCK:FIO1 MOSI:FIO2 MISO:FIO3'
        assert params == {'clk_pin': 1, 'mosi_pin': 2, 'miso_pin': 3}
        assert 'cs_pin' not in params

    def test_duplicate_pins_error(self):
        label, params, error = lj.resolve_pin_selection(
            'i2c', {'SDA': 'EIO0', 'SCL': 'EIO0'})
        assert label is None and params is None
        assert 'EIO0' in error

    def test_single_changed_pin_is_custom(self):
        label, params, error = lj.resolve_pin_selection(
            'i2c', {'SDA': 'FIO4', 'SCL': 'FIO6'})
        assert error is None
        assert label == 'SDA:FIO4 SCL:FIO6'
        assert params == {'sda_pin': 4, 'scl_pin': 6}


# --------------------------------------------------------------------------- #
# _labjack_claimed_pin_map: saved-net conflict source                          #
# --------------------------------------------------------------------------- #

class TestClaimedPinMap:
    def test_collects_saved_nets_at_same_address(self):
        target = _make_net(type='i2c', chan='FIO4-FIO5', net='i2c1')
        all_nets = [
            target,
            _make_net(net='gpio_led', type='gpio', chan='EIO0', saved=True),
            _make_net(net='spi1', type='spi', chan='FIO0-FIO3', saved=True),
            # Unsaved nets and other addresses don't claim pins.
            _make_net(net='gpio_x', type='gpio', chan='EIO5', saved=False),
            _make_net(net='gpio_y', type='gpio', chan='EIO6', saved=True,
                      addr='USB0::other::INSTR'),
            # Non-LabJack instruments are ignored.
            _make_net(net='aard', type='spi', chan='SPI0', saved=True,
                      instrument='Aardvark'),
        ]
        claimed = tui._labjack_claimed_pin_map(all_nets, target)
        assert claimed == {
            'EIO0': 'gpio_led',
            'FIO0': 'spi1', 'FIO1': 'spi1', 'FIO2': 'spi1', 'FIO3': 'spi1',
        }


# --------------------------------------------------------------------------- #
# AddNetsTree: default-pin messaging + pin button on LabJack i2c/spi rows      #
# --------------------------------------------------------------------------- #

class TestAddTreeRows:
    def test_rows_show_plain_channel_and_single_edit_button(self):
        tree = tui.AddNetsTree()
        net = _make_net(type='i2c', chan='FIO4-FIO5', net='i2c1')
        label = tree._get_net_label(net)
        assert 'Ch: FIO4-FIO5' in label
        assert '(default)' not in label
        assert label.count('[✎]') == 1
        assert '[⚙]' not in label

    def test_button_hit_region(self):
        tree = tui.AddNetsTree()
        net = _make_net(type='i2c', chan='FIO4-FIO5', net='i2c1')
        content_end = len(f"[ADD] {net.net} | I2C | Ch: {net.chan}   ")
        edit_start = content_end + 4 + 3
        assert tree._get_button_at_position(net, edit_start) == 'edit'
        assert tree._get_button_at_position(net, edit_start + 4) is None
        assert tree._get_button_at_position(net, 0) is None


class TestAddTreeEditDispatch:
    def _app_with_screen(self, nets):
        app = tui.NetApp(ctx=None, dut="box", inst_list=[], nets=list(nets))
        return app

    def test_labjack_net_gets_combined_editor(self):
        net = _make_net(type='i2c', chan='FIO4-FIO5', net='i2c1')

        async def main():
            app = self._app_with_screen([net])
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.pause()
                screen = tui.AddScreen([net], False)
                app.push_screen(screen)
                await pilot.pause()
                screen.add_tree._edit_net(net)
                await pilot.pause()
                dialog = app.screen
                assert isinstance(dialog, tui.LabJackPinDialog)
                # Combined flavor: name field present and prefilled.
                assert dialog.query_one("#edit_name_input", Input).value == 'i2c1'
        asyncio.run(main())

    def test_other_net_gets_rename_dialog(self):
        net = _make_net(instrument='Aardvark', chan='SPI0')

        async def main():
            app = self._app_with_screen([net])
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.pause()
                screen = tui.AddScreen([net], False)
                app.push_screen(screen)
                await pilot.pause()
                screen.add_tree._edit_net(net)
                await pilot.pause()
                assert isinstance(app.screen, tui.RenameNewNetDialog)
        asyncio.run(main())

    def test_combined_editor_saves_name_and_pins(self):
        net = _make_net(type='i2c', chan='FIO4-FIO5', net='i2c1')

        async def main():
            app = self._app_with_screen([net])
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.pause()
                screen = tui.AddScreen([net], False)
                app.push_screen(screen)
                await pilot.pause()
                screen.add_tree._edit_net(net)
                await pilot.pause()
                dialog = app.screen
                dialog.query_one("#edit_name_input", Input).value = "SENSORS_I2C"
                dialog.query_one("#pin_sda", Select).value = "FIO1"
                dialog.query_one("#pin_scl", Select).value = "FIO0"
                await pilot.pause()
                dialog._on_pin_button(
                    Button.Pressed(dialog.query_one("#pin-confirm", Button)))
                await pilot.pause()
                assert app.screen is screen
                assert screen.add_tree.custom_names[net.key()] == "SENSORS_I2C"
        asyncio.run(main())
        assert net.chan == 'SDA:FIO1 SCL:FIO0'
        assert net.params == {'sda_pin': 1, 'scl_pin': 0}
        assert net.pins_confirmed is True

    def test_combined_editor_name_conflict_applies_nothing(self):
        net = _make_net(type='i2c', chan='FIO4-FIO5', net='i2c1')
        saved = _make_net(net='SENSORS_I2C', type='gpio', chan='FIO6', saved=True)

        async def main():
            app = self._app_with_screen([net, saved])
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.pause()
                screen = tui.AddScreen([net, saved], False)
                app.push_screen(screen)
                await pilot.pause()
                screen.add_tree._edit_net(net)
                await pilot.pause()
                dialog = app.screen
                dialog.query_one("#edit_name_input", Input).value = "sensors_i2c"
                dialog.query_one("#pin_sda", Select).value = "FIO1"
                await pilot.pause()
                dialog._on_pin_button(
                    Button.Pressed(dialog.query_one("#pin-confirm", Button)))
                await pilot.pause()
                # Dialog stays up; neither the name nor the pins applied.
                assert app.screen is dialog
                warn = _static_text(dialog.query_one("#pin_warn", Static))
                assert 'already used' in warn
                assert net.key() not in screen.add_tree.custom_names
        asyncio.run(main())
        assert net.chan == 'FIO4-FIO5'
        assert net.params is None
        assert net.pins_confirmed is False

    def test_add_prompt_dialog_has_no_name_field(self):
        net = _make_net(type='i2c', chan='FIO4-FIO5', net='i2c1')
        app = _run_dialog(net, {}, lambda dlg: "pin-cancel")
        assert app.result is False

        async def main():
            app = _DialogApp(net, {})
            async with app.run_test(size=(100, 50)) as pilot:
                await pilot.pause()
                assert not app.screen.query("#edit_name_input")
        asyncio.run(main())


# --------------------------------------------------------------------------- #
# AddScreen: dismissable warnings/tip block                                    #
# --------------------------------------------------------------------------- #

class TestAddScreenNotices:
    def test_dismiss_button_removes_notices_block(self):
        net = _make_net(type='i2c', chan='FIO4-FIO5', net='i2c1')

        async def main():
            app = tui.NetApp(ctx=None, dut="box", inst_list=[], nets=[net])
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.pause()
                screen = tui.AddScreen([net], False)
                app.push_screen(screen)
                await pilot.pause()
                # The pin tip renders inside the dismissable block.
                notices = screen.query_one("#add_notices")
                assert notices is not None
                screen.on_button_pressed(
                    Button.Pressed(screen.query_one("#dismiss-notices", Button)))
                await pilot.pause()
                assert not screen.query("#add_notices")
                # The net list is still there and the add flow still works.
                assert screen.add_tree is not None
        asyncio.run(main())

    def test_no_notices_block_without_warnings_or_labjack_nets(self):
        net = tui.Net("Rigol_DP832", "1", "power-supply", "supply1",
                      "USB0::0x1AB1::INSTR", saved=False)

        async def main():
            app = tui.NetApp(ctx=None, dut="box", inst_list=[], nets=[net])
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.pause()
                screen = tui.AddScreen([net], False)
                app.push_screen(screen)
                await pilot.pause()
                assert not screen.query("#add_notices")
        asyncio.run(main())


# --------------------------------------------------------------------------- #
# AddScreen confirm: single-channel conflicts scoped to the selection          #
# --------------------------------------------------------------------------- #

KEITHLEY_ADDR = "USB0::0x05E6::0x2281::800000123::INSTR"


class TestSingleChannelConflictScope:
    """Legacy boxes can hold double-booked saved nets on a single-channel
    instrument (saved before the one-net-per-chip rule). Those must not
    block adds that don't involve the instrument."""

    def _keithley(self, role, name, saved):
        return tui.Net('Keithley_2281S', '1', role, name, KEITHLEY_ADDR,
                       saved=saved)

    def _confirm(self, all_nets, selection):
        """Push an AddScreen over *all_nets*, select *selection*, press
        Add Selected; return (app, save_mock)."""
        async def main():
            app = tui.NetApp(ctx=None, dut="box", inst_list=[], nets=all_nets)
            app._fetch_saved_records = lambda: []
            with patch.object(tui, '_save_nets_batch', return_value=True) as sb:
                async with app.run_test(size=(100, 40)) as pilot:
                    await pilot.pause()
                    screen = tui.AddScreen(all_nets, False)
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

    def test_legacy_double_booked_keithley_does_not_block_gpio_add(self):
        gpio16 = _make_net(net='gpio16', type='gpio', chan='FIO4')
        gpio17 = _make_net(net='gpio17', type='gpio', chan='FIO5')
        all_nets = [
            gpio16, gpio17,
            self._keithley('battery', 'batt1', saved=True),
            self._keithley('power-supply', 'supply1', saved=True),
        ]
        saves, hint = self._confirm(all_nets, [gpio16, gpio17])
        assert saves == 1
        assert 'Only one net' not in hint

    def test_selecting_two_nets_on_same_chip_still_blocks(self):
        batt = self._keithley('battery', 'batt1', saved=False)
        supply = self._keithley('power-supply', 'supply1', saved=False)
        saves, hint = self._confirm([batt, supply], [batt, supply])
        assert saves == 0
        assert 'Only one net' in hint


# --------------------------------------------------------------------------- #
# _save_nets_batch: params passthrough                                         #
# --------------------------------------------------------------------------- #

class TestSaveBatchParams:
    def test_custom_params_included_in_payload(self):
        custom = _make_net(
            chan='CS:FIO6 SCK:FIO7 MOSI:EIO0 MISO:EIO1',
            params={'cs_pin': 6, 'clk_pin': 7, 'mosi_pin': 8, 'miso_pin': 9},
        )
        default = _make_net(net='i2c1', type='i2c', chan='FIO4-FIO5')

        with patch.object(tui, '_save_net_http') as save:
            ok = tui._save_nets_batch(dut='box', nets=[custom, default])

        assert ok is True
        payload = [call.args[1] for call in save.call_args_list]
        by_name = {rec['name']: rec for rec in payload}
        assert by_name['spi1']['params'] == {
            'cs_pin': 6, 'clk_pin': 7, 'mosi_pin': 8, 'miso_pin': 9}
        assert by_name['spi1']['pin'] == 'CS:FIO6 SCK:FIO7 MOSI:EIO0 MISO:EIO1'
        # Default nets stay byte-identical to the pre-dialog format.
        assert 'params' not in by_name['i2c1']
        assert by_name['i2c1']['pin'] == 'FIO4-FIO5'


# --------------------------------------------------------------------------- #
# LabJackPinDialog: dialog behaviour (textual test pilot)                      #
# --------------------------------------------------------------------------- #

from textual.app import App  # noqa: E402
from textual.widgets import Button, Input, Select, Static  # noqa: E402


class _DialogApp(App):
    """Bare host app that pushes the pin dialog on mount."""

    def __init__(self, net, claimed):
        super().__init__()
        self._net = net
        self._claimed = claimed
        self.result = None

    def on_mount(self):
        self.push_screen(
            tui.LabJackPinDialog(self._net, self._claimed, self._done))

    def _done(self, success):
        self.result = success


def _run_dialog(net, claimed, interact):
    """Run the dialog app; ``interact(dialog)`` manipulates widgets, then the
    given button id is pressed. Returns the app (with .result set)."""
    async def main():
        app = _DialogApp(net, claimed)
        async with app.run_test(size=(100, 50)) as pilot:
            await pilot.pause()
            dialog = app.screen
            assert isinstance(dialog, tui.LabJackPinDialog)
            button_id = interact(dialog)
            await pilot.pause()
            dialog._on_pin_button(
                Button.Pressed(dialog.query_one(f"#{button_id}", Button)))
            await pilot.pause()
        return app
    return asyncio.run(main())


class TestLabJackPinDialog:
    def test_defaults_confirm_keeps_legacy_record(self):
        net = _make_net()
        app = _run_dialog(net, {}, lambda dlg: "pin-confirm")
        assert app.result is True
        assert net.chan == 'FIO0-FIO3'
        assert net.params is None

    def test_custom_selection_sets_label_and_params(self):
        net = _make_net()

        def interact(dlg):
            dlg.query_one("#pin_cs", Select).value = "FIO6"
            dlg.query_one("#pin_sck", Select).value = "FIO7"
            dlg.query_one("#pin_mosi", Select).value = "EIO0"
            dlg.query_one("#pin_miso", Select).value = "EIO1"
            return "pin-confirm"

        app = _run_dialog(net, {}, interact)
        assert app.result is True
        assert net.chan == 'CS:FIO6 SCK:FIO7 MOSI:EIO0 MISO:EIO1'
        assert net.params == {'cs_pin': 6, 'clk_pin': 7, 'mosi_pin': 8, 'miso_pin': 9}

    def test_manual_cs_option(self):
        net = _make_net()

        def interact(dlg):
            dlg.query_one("#pin_cs", Select).value = lj.NO_CS
            return "pin-confirm"

        app = _run_dialog(net, {}, interact)
        assert app.result is True
        assert net.chan == 'SCK:FIO1 MOSI:FIO2 MISO:FIO3'
        assert net.params == {'clk_pin': 1, 'mosi_pin': 2, 'miso_pin': 3}

    def test_duplicate_pins_block_confirm(self):
        net = _make_net(type='i2c', chan='FIO4-FIO5', net='i2c1')

        async def main():
            app = _DialogApp(net, {})
            async with app.run_test(size=(100, 50)) as pilot:
                await pilot.pause()
                dialog = app.screen
                dialog.query_one("#pin_sda", Select).value = "EIO0"
                dialog.query_one("#pin_scl", Select).value = "EIO0"
                await pilot.pause()
                dialog._on_pin_button(
                    Button.Pressed(dialog.query_one("#pin-confirm", Button)))
                await pilot.pause()
                # Dialog stays up, error rendered, callback never fired.
                assert app.screen is dialog
                warn = _static_text(dialog.query_one("#pin_warn", Static))
                assert 'EIO0' in warn
                assert app.result is None
            return app
        asyncio.run(main())
        assert net.chan == 'FIO4-FIO5'
        assert net.params is None

    def test_cancel_leaves_net_untouched(self):
        net = _make_net()
        app = _run_dialog(net, {}, lambda dlg: "pin-cancel")
        assert app.result is False
        assert net.chan == 'FIO0-FIO3'
        assert net.params is None

    def test_customizing_stashes_legacy_chan(self):
        net = _make_net(type='i2c', chan='FIO4-FIO5', net='i2c1')

        def interact(dlg):
            dlg.query_one("#pin_sda", Select).value = "EIO0"
            dlg.query_one("#pin_scl", Select).value = "EIO1"
            return "pin-confirm"

        app = _run_dialog(net, {}, interact)
        assert app.result is True
        assert net.chan == 'SDA:EIO0 SCL:EIO1'
        assert net.legacy_chan == 'FIO4-FIO5'

    def test_prefills_from_existing_params(self):
        net = _make_net(
            type='i2c', chan='SDA:EIO0 SCL:EIO1', net='i2c1',
            params={'sda_pin': 8, 'scl_pin': 9}, legacy_chan='FIO4-FIO5',
        )

        async def main():
            app = _DialogApp(net, {})
            async with app.run_test(size=(100, 50)) as pilot:
                await pilot.pause()
                dialog = app.screen
                assert dialog.query_one("#pin_sda", Select).value == "EIO0"
                assert dialog.query_one("#pin_scl", Select).value == "EIO1"
        asyncio.run(main())

    def test_revert_to_defaults_restores_legacy_record(self):
        net = _make_net(
            type='i2c', chan='SDA:EIO0 SCL:EIO1', net='i2c1',
            params={'sda_pin': 8, 'scl_pin': 9}, legacy_chan='FIO4-FIO5',
        )

        def interact(dlg):
            dlg.query_one("#pin_sda", Select).value = "FIO4"
            dlg.query_one("#pin_scl", Select).value = "FIO5"
            return "pin-confirm"

        app = _run_dialog(net, {}, interact)
        assert app.result is True
        assert net.chan == 'FIO4-FIO5'
        assert net.params is None

    def test_claimed_pin_shows_warning(self):
        net = _make_net(type='i2c', chan='FIO4-FIO5', net='i2c1')

        async def main():
            app = _DialogApp(net, {'EIO0': 'gpio_led'})
            async with app.run_test(size=(100, 50)) as pilot:
                await pilot.pause()
                dialog = app.screen
                dialog.query_one("#pin_sda", Select).value = "EIO0"
                await pilot.pause()
                warn = _static_text(dialog.query_one("#pin_warn", Static))
                assert 'gpio_led' in warn and 'EIO0' in warn
        asyncio.run(main())


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
