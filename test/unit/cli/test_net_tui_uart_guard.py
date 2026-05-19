#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the UART-net save guardrail in ``cli/commands/box/net_tui.py``.

Background: the box-side UART dispatcher reads a saved net's ``pin``
field as a USB serial string and falls back to ``/dev/tty*`` only when
the value starts with ``/dev/``. A previous bug had the box scanner
advertise FT4232H UART channels as bare interface indices
(``"0"/"1"/"2"/"3"``); the TUI would faithfully persist those into
``pin``, and the box would later fail with the cryptic
``UART bridge with serial 2 not found`` message at first use.

These tests pin the new behaviour:
  * ``_validate_uart_pin`` accepts ``/dev/tty*`` paths and serial-shaped
    strings, rejects bare ``"0"/"1"/...`` indices and empty values.
  * ``_save_nets_batch`` raises ``UARTNetSaveValidationError`` (without
    ever calling the box) when given a net that would round-trip badly.
  * Non-UART nets aren't validated — only UART rows have the strict
    ``pin`` contract.
"""

from __future__ import annotations

import importlib
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

tui = importlib.import_module('cli.commands.box.net_tui')


# --------------------------------------------------------------------------- #
# _validate_uart_pin                                                          #
# --------------------------------------------------------------------------- #

class TestValidateUartPin:
    @pytest.mark.parametrize('pin', [
        '/dev/ttyUSB0',
        '/dev/ttyUSB2',
        '/dev/ttyACM0',
        '/dev/serial/by-id/usb-FTDI_FT4232H-3',
    ])
    def test_dev_paths_accepted(self, pin):
        assert tui._validate_uart_pin(pin) is True

    @pytest.mark.parametrize('pin', [
        'FT5XYZAB',
        'FT4232H_ABCDEFGH',
        'AB12CD34',
        '0123456789',
    ])
    def test_real_usb_serials_accepted(self, pin):
        assert tui._validate_uart_pin(pin) is True

    @pytest.mark.parametrize('pin', ['0', '1', '2', '3'])
    def test_bare_interface_indices_rejected(self, pin):
        # These are exactly the values the old FT4232H scanner produced
        # for UART channels when the chip had no programmable serial.
        # Persisting them was the root cause of the user-visible bug.
        assert tui._validate_uart_pin(pin) is False

    @pytest.mark.parametrize('pin', ['', '  ', None, 42, ('a',)])
    def test_empty_and_non_string_rejected(self, pin):
        assert tui._validate_uart_pin(pin) is False

    def test_two_char_rejected_as_likely_index(self):
        # "12" could be a bare index too; we err on the side of refusing
        # short values because legitimate USB serials are universally
        # longer than two characters in practice.
        assert tui._validate_uart_pin('12') is False


# --------------------------------------------------------------------------- #
# _validate_nets_before_save                                                  #
# --------------------------------------------------------------------------- #

def _make_net(**overrides):
    """Construct a minimal ``Net`` for save-path tests."""
    defaults = dict(
        instrument='FTDI_FT4232H',
        chan='/dev/ttyUSB0',
        type='uart',
        net='CLI',
        addr='USB0::0x0403::0x6011::FT5XYZAB::INSTR',
    )
    defaults.update(overrides)
    return tui.Net(**defaults)


class TestValidateNetsBeforeSave:
    def test_all_clean_returns_empty(self):
        nets = [
            _make_net(),
            _make_net(net='AUX', chan='/dev/ttyUSB1'),
            _make_net(type='debug', chan='nrf52@A'),
        ]
        assert tui._validate_nets_before_save(nets) == []

    def test_bare_index_uart_flagged(self):
        bad_net = _make_net(chan='2')
        result = tui._validate_nets_before_save([bad_net])
        assert len(result) == 1
        flagged, reason = result[0]
        assert flagged is bad_net
        assert "'2'" in reason
        assert 'EEPROM' in reason  # actionable hint surfaces

    def test_non_uart_with_short_chan_ignored(self):
        # Debug nets legitimately use short ``device@channel`` values
        # like ``nrf52@A`` — only the uart role gets the strict check.
        debug_net = _make_net(type='debug', chan='@A')
        assert tui._validate_nets_before_save([debug_net]) == []


# --------------------------------------------------------------------------- #
# _save_nets_batch — refuses bad batches before touching the box              #
# --------------------------------------------------------------------------- #

class TestSaveNetsBatchGuardrail:
    def test_refuses_batch_with_invalid_uart_without_calling_box(self):
        bad = _make_net(chan='2')
        good = _make_net(net='AUX', chan='/dev/ttyUSB1')

        with patch.object(tui, '_run_script') as run_script:
            with pytest.raises(tui.UARTNetSaveValidationError) as exc:
                tui._save_nets_batch(ctx=None, dut='JUL-5', nets=[bad, good])

        assert "'2'" in str(exc.value)
        # The whole point of the guard is to fail fast — the box must
        # never get the request when validation rejects it.
        run_script.assert_not_called()

    def test_clean_batch_calls_box(self):
        clean = _make_net()

        # Pretend the box returned a success envelope.
        with patch.object(tui, '_run_script', return_value='{"ok": true}'):
            ok = tui._save_nets_batch(ctx=None, dut='JUL-5', nets=[clean])

        assert ok is True

    def test_empty_batch_short_circuits(self):
        # No nets to save, no validation needed, no box call.
        with patch.object(tui, '_run_script') as run_script:
            assert tui._save_nets_batch(ctx=None, dut='JUL-5', nets=[]) is True
        run_script.assert_not_called()
