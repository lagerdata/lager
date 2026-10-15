# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
`lager nets set-capture`: pairing a Rigol scope with the HDMI capture card
its live view comes from.
"""

from __future__ import annotations

import importlib
import os
import sys
from unittest.mock import patch

import pytest
from click.testing import CliRunner

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

importlib.import_module("cli.commands.box.nets")
from cli.commands.box.nets import nets as nets_group  # noqa: E402
from test.unit.cli.nets_http_fake import FakeBoxHTTP  # noqa: E402

ADDRESS = "USB0::0x1AB1::0x0515::MS5A1::INSTR"


def _card(port):
    return {"name": "HDMI_Capture_MS2109", "vid": "534d", "pid": "2109",
            "serial": None, "port_path": port, "address": "usb-port:%s" % port,
            "net_type": ["capture"], "channels": {"capture": ["/dev/video0"]}}


@pytest.fixture
def box():
    fake = FakeBoxHTTP(instruments=[_card("1-1.3")])
    fake.saved_nets = [
        {"name": "rigol", "role": "scope", "instrument": "Rigol_MSO5204",
         "address": ADDRESS},
        {"name": "rigol ch1", "role": "scope-channel", "instrument": "Rigol_MSO5204",
         "address": ADDRESS, "pin": 1,
         "capture": {"vid": "534d", "pid": "2109", "port_path": "9-9"}},
        {"name": "pico", "role": "scope", "instrument": "picoscope_2000"},
    ]
    with patch("requests.request", fake.request), \
         patch("cli.box_storage._check_box_lock", lambda *a, **k: None), \
         patch("cli.box_storage.get_box_ip", lambda name: None):
        yield fake


def _invoke(*args):
    return CliRunner().invoke(nets_group, ["set-capture", *args, "--box", "10.0.0.9"],
                              obj=None, catch_exceptions=False)


def _net(box, name):
    return next(n for n in box.saved_nets if n["name"] == name)


def test_the_only_card_attached_is_paired(box):
    result = _invoke("rigol")
    assert result.exit_code == 0, result.output
    assert _net(box, "rigol")["capture"] == {"vid": "534d", "pid": "2109",
                                             "port_path": "1-1.3"}
    # The scope's other net gave up the card it named, so they cannot disagree.
    assert "capture" not in _net(box, "rigol ch1")


def test_with_several_cards_one_is_named_by_port(box):
    box.instruments.append(_card("1-1.4"))
    refused = _invoke("rigol")
    assert refused.exit_code == 1
    assert "--port 1-1.3" in refused.output and "--port 1-1.4" in refused.output
    assert _invoke("rigol", "--port", "1-1.4").exit_code == 0
    assert _net(box, "rigol")["capture"]["port_path"] == "1-1.4"


def test_a_card_that_is_not_there_is_refused(box):
    result = _invoke("rigol", "--port", "2-2")
    assert result.exit_code == 1
    assert "USB port 2-2" in result.output


def test_clearing_unpairs_every_net_on_the_scope(box):
    assert _invoke("rigol", "--clear").exit_code == 0
    assert all("capture" not in n for n in box.saved_nets)


def test_a_picoscope_needs_no_card(box):
    result = _invoke("pico")
    assert result.exit_code == 1
    assert "without a capture card" in result.output


def test_the_screenshot_stands_in_for_a_card(box):
    result = _invoke("rigol", "--screen")
    assert result.exit_code == 0, result.output
    assert _net(box, "rigol")["capture"] == {"device": "screen"}
    assert "capture" not in _net(box, "rigol ch1")
    assert _invoke("rigol", "--screen", "--clear").exit_code == 1
