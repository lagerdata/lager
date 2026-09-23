# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
`lager debug NET flash --bin FILE,0` must program at address 0 (#501).

Both debug service clients built the request with
`'address': address or 0x08000000`. An explicit 0 is falsy, so it became the
STM32 default: a part whose flash starts at 0 (an nRF, for one) was
programmed at the wrong address, and on a DA1469x behind OpenOCD the loader
refused the address outright. `BinfileType` always parses an address, so the
default only ever replaced a 0 the user typed.

The box's own handler was already right -- it uses
`binfile.get('address', 0x08000000)` -- so the fix is in the two clients, the
CLI's (`cli/commands/development/debug/service_client.py`) and the one on-box
scripts use (`box/lager/debug/service_client.py`). They are separate files on
purpose, so each is checked here.
"""

import importlib
from unittest import mock

import pytest

box_client_mod = importlib.import_module("lager.debug.service_client")
cli_client_mod = importlib.import_module("cli.commands.development.debug.service_client")


class _Resp:
    status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return {"status": "flash_complete"}


@pytest.fixture
def binfile(tmp_path):
    path = tmp_path / "app.bin"
    path.write_bytes(b"\x00\x01\x02\x03")
    return path


def _cli_address(binfile, address):
    client = cli_client_mod.DebugServiceClient("192.0.2.5", ssh_tunnel=False)
    with mock.patch.object(client, "_request", return_value=_Resp()) as request:
        client.flash(binfile, file_type="bin", address=address)
    return request.call_args.kwargs["json"]["binfile"]["address"]


def _box_address(binfile, address):
    client = box_client_mod.DebugServiceClient("127.0.0.1")
    with mock.patch.object(client.session, "post", return_value=_Resp()) as post:
        client.flash(binfile, file_type="bin", address=address)
    return post.call_args.kwargs["json"]["binfile"]["address"]


@pytest.mark.parametrize("sent", [_cli_address, _box_address], ids=["cli", "box"])
@pytest.mark.parametrize("address, expected", [
    (0, 0),
    (0x16000000, 0x16000000),
    (None, 0x08000000),
])
def test_the_address_given_is_the_address_sent(sent, binfile, address, expected):
    assert sent(binfile, address) == expected


# --------------------------------------------------------------------------- #
# /debug/erase: erase_start / erase_size are sent only when given             #
# --------------------------------------------------------------------------- #

NET = {"name": "debug1", "role": "debug"}


def _cli_erase(**kwargs):
    client = cli_client_mod.DebugServiceClient("192.0.2.5", ssh_tunnel=False)
    with mock.patch.object(client, "_request", return_value=_Resp()) as request:
        client.erase(NET, **kwargs)
    return request.call_args.kwargs


def _box_erase(**kwargs):
    client = box_client_mod.DebugServiceClient("127.0.0.1")
    with mock.patch.object(client.session, "post", return_value=_Resp()) as post:
        client.erase(NET, **kwargs)
    return post.call_args.kwargs


@pytest.mark.parametrize("sent", [_cli_erase, _box_erase], ids=["cli", "box"])
def test_no_range_keeps_the_body_an_older_box_expects(sent):
    kwargs = sent()
    assert kwargs["json"] == {"net": NET, "speed": "4000", "transport": "SWD"}
    assert kwargs["timeout"] == 120


@pytest.mark.parametrize("sent", [_cli_erase, _box_erase], ids=["cli", "box"])
def test_a_range_is_sent_as_two_integer_keys(sent):
    kwargs = sent(erase_start=0x16000000, erase_size=0x200000)
    assert kwargs["json"]["erase_start"] == 0x16000000
    assert kwargs["json"]["erase_size"] == 0x200000
    assert kwargs["json"]["net"] == NET


@pytest.mark.parametrize("sent", [_cli_erase, _box_erase], ids=["cli", "box"])
def test_the_wait_grows_a_minute_per_mib(sent):
    assert sent(erase_start=0x16000000, erase_size=0x200000)["timeout"] == 240
    assert sent(erase_start=0x16000000, erase_size=0x200001)["timeout"] == 300
