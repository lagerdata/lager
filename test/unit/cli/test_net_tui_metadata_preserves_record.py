#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for metadata editing in the Net-Manager TUI.

Background: the "Edit Details" dialog used to build a *fresh partial record*
from the five fields it displayed (``name``, ``role``, ``address``,
``instrument``, ``pin``) plus the three it edits, and send that to
``PUT /nets/<name>``. That route replaces a net wholesale, so every field the
dialog did not model was silently discarded the moment somebody wrote a
description: a debug net lost its ``jlink_script``, a supply lost its
``safety_limits`` ceiling, and a UART lost the ``usb_identity`` its dispatcher
resolves against.

``lager nets describe`` never had the bug -- it fetches the stored record and
mutates it in place. ``_merge_net_metadata`` makes the TUI do the same, and
these tests pin it:

  * every unrelated field survives a metadata edit;
  * the right record is picked when one name is saved under two roles;
  * a net deleted out from under the dialog raises rather than being recreated
    from the partial data the dialog happens to be holding.
"""

from __future__ import annotations

import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

tui = importlib.import_module('cli.commands.box.net_tui')


def _records():
    """A debug net with side-car fields, and one name saved under two roles."""
    return [
        {
            "name": "swd", "role": "debug", "instrument": "SEGGER_JLink",
            "address": "USB0::0x1366::0x0101::000123::INSTR", "pin": "0",
            "jlink_script": "c2NyaXB0",
            "openocd_config": "Y29uZmln",
            "safety_limits": {"max_voltage": 3.6},
            "usb_identity": {"vid": "1366", "pid": "0101", "serial": "000123"},
            "params": {"speed": 4000},
            "device_path": "/dev/bus/usb/001/004",
            "channel_key": "swd0",
            "purpose": "old purpose",
            "tags": ["old"],
        },
        {"name": "vbat", "role": "power-supply", "instrument": "Keysight_E36313A",
         "address": "USB0::0x2A8D::0x1002::MY1::INSTR", "channel": 1},
        {"name": "vbat", "role": "battery", "instrument": "Keysight_E36731A",
         "address": "USB0::0x2A8D::0x1102::MY2::INSTR", "channel": 1},
    ]


class TestMergePreservesRecord:

    @pytest.mark.parametrize('field', [
        'jlink_script', 'openocd_config', 'safety_limits', 'usb_identity',
        'params', 'device_path', 'channel_key', 'address', 'instrument', 'pin',
    ])
    def test_unrelated_field_survives_a_metadata_edit(self, field):
        records = _records()
        before = records[0][field]
        out = tui._merge_net_metadata(records, 'swd', 'debug', 'new', 'notes', ['t'])
        assert out[field] == before

    def test_metadata_is_applied(self):
        out = tui._merge_net_metadata(_records(), 'swd', 'debug', 'new', 'n', ['a', 'b'])
        assert out['purpose'] == 'new'
        assert out['notes'] == 'n'
        assert out['tags'] == ['a', 'b']

    def test_previous_tags_are_replaced_not_merged(self):
        # The dialog shows the full tag list and the user edits it as a whole,
        # so what they see on screen is what gets stored.
        out = tui._merge_net_metadata(_records(), 'swd', 'debug', 'p', 'n', ['only'])
        assert out['tags'] == ['only']

    def test_clearing_every_field_is_allowed(self):
        out = tui._merge_net_metadata(_records(), 'swd', 'debug', '', '', [])
        assert out['purpose'] == ''
        assert out['notes'] == ''
        assert out['tags'] == []
        # ...and still does not take the debug script with it.
        assert out['jlink_script'] == 'c2NyaXB0'

    def test_tags_are_copied_not_aliased(self):
        # The caller's list is the dialog's live state; storing it by reference
        # would let a later edit mutate the record that was already sent.
        tags = ['a']
        out = tui._merge_net_metadata(_records(), 'swd', 'debug', 'p', 'n', tags)
        tags.append('b')
        assert out['tags'] == ['a']


class TestRecordSelection:

    def test_role_disambiguates_a_duplicated_name(self):
        out = tui._merge_net_metadata(_records(), 'vbat', 'battery', 'p', 'n', [])
        assert out['role'] == 'battery'
        assert out['instrument'] == 'Keysight_E36731A'

    def test_falls_back_to_name_when_the_role_does_not_match(self):
        out = tui._merge_net_metadata(_records(), 'vbat', 'nonesuch', 'p', 'n', [])
        assert out['name'] == 'vbat'

    def test_missing_net_raises_rather_than_being_recreated(self):
        # Writing here would resurrect a net somebody deleted in another
        # session, rebuilt from whatever partial state this dialog is holding.
        with pytest.raises(RuntimeError, match='no longer saved'):
            tui._merge_net_metadata(_records(), 'gone', 'uart', 'p', 'n', [])

    def test_empty_record_list_raises(self):
        with pytest.raises(RuntimeError, match='no longer saved'):
            tui._merge_net_metadata([], 'swd', 'debug', 'p', 'n', [])
