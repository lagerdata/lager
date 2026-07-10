# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the `lager nets` Channel column display rule.

Boxes annotate uart nets that carry a durable identity with `live_path` — the
node the device owns right now, which can differ from the stored pin after a
USB re-enumeration. The CLI must show reality when the box provides it, fall
back to the stored pin for older boxes / non-uart roles, and mark absent
devices instead of showing a node that no longer belongs to them.
"""

from __future__ import annotations

import importlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

nets = importlib.import_module('cli.commands.box.nets')


def test_live_path_preferred_over_stored_pin():
    rec = {"role": "uart", "pin": "/dev/ttyUSB4", "live_path": "/dev/ttyUSB1"}
    assert nets._channel_display(rec) == "/dev/ttyUSB1"


def test_absent_device_marked_disconnected():
    rec = {"role": "uart", "pin": "/dev/ttyUSB4", "live_path": None}
    assert nets._channel_display(rec) == "/dev/ttyUSB4 (disconnected)"


def test_old_box_without_field_shows_pin():
    rec = {"role": "uart", "pin": "/dev/ttyUSB4"}
    assert nets._channel_display(rec) == "/dev/ttyUSB4"


def test_non_uart_roles_unchanged():
    rec = {"role": "gpio", "pin": "FIO1", "live_path": "/dev/ttyUSB1"}
    assert nets._channel_display(rec) == "FIO1"


def test_missing_pin_handled():
    assert nets._channel_display({"role": "uart", "live_path": None}) == "(disconnected)"
    assert nets._channel_display({"role": "uart"}) == ""
