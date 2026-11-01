#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Net names that `lager nets add-all` picks.

A scope offers a `scope` net for the whole instrument and one `scope-channel`
net per input. Both roles used the `scope` prefix and add-all counted indices
per role, so the instrument net and channel 1 were both named `scope1`. Names
are now allocated per name, and the `scope` role uses the `oscilloscope` prefix.
"""

from __future__ import annotations

import importlib
import os
import sys
from unittest.mock import patch

import pytest
from click.testing import CliRunner

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

from test.unit.cli.nets_http_fake import FakeBoxHTTP  # noqa: E402

nets_mod = importlib.import_module("cli.commands.box.nets")
from cli.commands.box.nets import nets as nets_group  # noqa: E402

SCOPE_ADDR = "USB0::0x1AB1::0x04CE::DS1ZA000000001::INSTR"
SCOPE = {
    "name": "Rigol_DS1054Z", "vid": "1ab1", "pid": "04ce", "serial": "DS1ZA000000001",
    "address": SCOPE_ADDR,
    "net_type": ["scope", "scope-channel"],
    "channels": {"scope": [], "scope-channel": ["1", "2", "3", "4"]},
}


@pytest.fixture
def box():
    fake = FakeBoxHTTP([dict(SCOPE)])
    patchers = (
        patch("requests.request", fake.request),
        patch("cli.box_storage.resolve_and_validate_box",
              lambda _ctx, name: name or "testbox"),
        patch.object(nets_mod, "_resolve_box", lambda _ctx, name: name or "testbox"),
    )
    for p in patchers:
        p.start()
    yield fake
    for p in reversed(patchers):
        p.stop()


def _add_all():
    return CliRunner().invoke(nets_group, ["add-all", "--box", "b", "--yes"],
                              catch_exceptions=False)


def test_four_channel_scope_names_are_unique(box):
    result = _add_all()
    assert result.exit_code == 0, result.output
    names = [n["name"] for n in box.saved_nets]
    assert len(names) == 5, names
    assert len(set(names)) == len(names), names
    by_role = {}
    for n in box.saved_nets:
        by_role.setdefault(n["role"], set()).add(n["name"])
    assert by_role["scope"] == {"oscilloscope1"}
    assert by_role["scope-channel"] == {"scope1", "scope2", "scope3", "scope4"}


def test_saved_name_of_another_role_is_not_reused(box):
    # A box saved by the old add-all may hold the instrument net as `scope1`.
    box.saved_nets.append({"name": "scope1", "role": "scope", "instrument": "Rigol_DS1054Z",
                           "address": SCOPE_ADDR, "pin": ""})
    result = _add_all()
    assert result.exit_code == 0, result.output
    names = [n["name"] for n in box.saved_nets]
    assert len(set(names)) == len(names), names
    channels = {n["name"] for n in box.saved_nets if n["role"] == "scope-channel"}
    assert channels == {"scope2", "scope3", "scope4", "scope5"}


def test_second_scope_continues_the_indices(box):
    second = dict(SCOPE, serial="DS1ZA000000002",
                  address="USB0::0x1AB1::0x04CE::DS1ZA000000002::INSTR")
    box.instruments.append(second)
    result = _add_all()
    assert result.exit_code == 0, result.output
    names = [n["name"] for n in box.saved_nets]
    assert len(names) == 10 and len(set(names)) == 10, names
    assert {"oscilloscope1", "oscilloscope2"} <= set(names)
