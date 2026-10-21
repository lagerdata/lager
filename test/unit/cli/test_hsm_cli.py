#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""`lager hsm` (cli/commands/security/hsm.py).

Covers where the password comes from (stdin, then LAGER_HSM_PASSWORD, then a
hidden prompt, and never an option), the confirmation on destructive
commands, the request each subcommand sends, the sign data and output forms,
and that the CLI's copies of the choice lists match the box driver's tables.
"""

from __future__ import annotations

import base64
import importlib
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
sys.path.insert(0, _REPO)

hsm_mod = importlib.import_module("cli.commands.security.hsm")
hsm_group = hsm_mod.hsm

_PASSWORD = "correct-horse-battery"


class _Obj:
    """Settable stand-in for the LagerContext (the group stashes attrs on it)."""


def _run(args, value=None, env=None, input=None):
    """Invoke the hsm group with the box boundary mocked.

    Returns (result, calls) where calls records each post_net_command request.
    """
    calls: list[dict] = []

    def fake_post(ctx, box_ip, netname, action, role=None, quiet=False,
                  http_timeout=None, **params):
        calls.append({"box_ip": box_ip, "netname": netname, "action": action,
                      "role": role, "http_timeout": http_timeout, "params": params})
        return {"success": True, "value": value, "message": "ok"}

    environ = {hsm_mod.PASSWORD_ENV: ""}
    environ.update(env or {})
    with patch.object(hsm_mod, "post_net_command", fake_post), \
         patch.object(hsm_mod, "resolve_box", lambda ctx, box, read_only=False: "1.2.3.4"), \
         patch.object(hsm_mod, "resolve_box_locked", lambda ctx, box, cmd: "1.2.3.4"), \
         patch.object(hsm_mod, "validate_net_exists",
                      lambda ctx, ip, name, role: {"name": name}), \
         patch.object(hsm_mod, "display_nets", MagicMock()):
        result = CliRunner().invoke(hsm_group, args, obj=_Obj(), env=environ, input=input)
    return result, calls


# ----- password sources -----

def test_password_from_stdin():
    result, calls = _run(["hsm1", "objects", "--password-stdin"], value=[],
                         env={hsm_mod.PASSWORD_ENV: "from-env"}, input=_PASSWORD + "\n")
    assert result.exit_code == 0, result.output
    assert calls[0]["params"]["password"] == _PASSWORD


def test_password_from_env():
    result, calls = _run(["hsm1", "objects"], value=[], env={hsm_mod.PASSWORD_ENV: _PASSWORD})
    assert result.exit_code == 0, result.output
    assert calls[0]["params"]["password"] == _PASSWORD


def test_password_from_prompt_is_hidden():
    result, calls = _run(["hsm1", "objects"], value=[], input=_PASSWORD + "\n")
    assert result.exit_code == 0, result.output
    assert calls[0]["params"]["password"] == _PASSWORD
    assert "Password for auth key 1" in result.output
    assert _PASSWORD not in result.output


def test_empty_stdin_is_a_usage_error():
    result, calls = _run(["hsm1", "objects", "--password-stdin"], input="\n")
    assert result.exit_code == 2
    assert calls == []


def test_there_is_no_password_option():
    for command in hsm_group.commands.values():
        names = {opt for param in command.params for opt in getattr(param, "opts", [])}
        assert "--password" not in names, command.name


def test_auth_key_id_is_sent():
    result, calls = _run(["hsm1", "blink", "--auth-key-id", "3", "--seconds", "2"],
                         env={hsm_mod.PASSWORD_ENV: _PASSWORD})
    assert result.exit_code == 0, result.output
    assert calls[0]["params"] == {"seconds": 2, "auth_key_id": 3, "password": _PASSWORD}
    assert calls[0]["http_timeout"] == 32.0


def test_info_does_not_ask_for_a_password():
    value = {"firmware": "2.4.0", "serial": 12345678, "log_used": 3, "log_size": 62,
             "algorithms": ["ec-p256"]}
    result, calls = _run(["hsm1", "info", "--json"], value=value)
    assert result.exit_code == 0, result.output
    assert calls[0]["params"] == {}
    assert json.loads(result.output) == {"netname": "hsm1", **value}


# ----- confirmation -----

@pytest.mark.parametrize("args", [["delete", "--id", "7"], ["reset"]])
def test_destructive_commands_ask_first(args):
    result, calls = _run(["hsm1", *args], env={hsm_mod.PASSWORD_ENV: _PASSWORD}, input="n\n")
    assert result.exit_code == 1
    assert "You cannot undo this" in result.output
    assert calls == []


@pytest.mark.parametrize("args", [["delete", "--id", "7"], ["reset"]])
def test_password_stdin_needs_yes(args):
    result, calls = _run(["hsm1", *args, "--password-stdin"], input=_PASSWORD + "\n")
    assert result.exit_code == 2
    assert "--yes" in result.output
    assert calls == []


def test_delete_with_yes():
    result, calls = _run(["hsm1", "delete", "--id", "0x0007", "--yes", "--password-stdin"],
                         input=_PASSWORD + "\n")
    assert result.exit_code == 0, result.output
    assert calls[0]["action"] == "delete"
    assert calls[0]["params"]["object_id"] == 7
    assert calls[0]["params"]["object_type"] == "asymmetric-key"


def test_reset_with_yes_uses_long_timeout():
    result, calls = _run(["hsm1", "reset", "--yes"], env={hsm_mod.PASSWORD_ENV: _PASSWORD})
    assert result.exit_code == 0, result.output
    assert calls[0]["action"] == "reset"
    assert calls[0]["http_timeout"] == 90.0


# ----- requests -----

def test_generate_request():
    value = {"id": 0x42, "public_key_pem": "-----BEGIN PUBLIC KEY-----\n"}
    result, calls = _run(["hsm1", "generate", "--algorithm", "ed25519", "--id", "0x10",
                          "--label", "k", "--domains", "1,3",
                          "--capability", "sign-eddsa", "--capability", "exportable-under-wrap"],
                         value=value, env={hsm_mod.PASSWORD_ENV: _PASSWORD})
    assert result.exit_code == 0, result.output
    assert calls[0]["role"] == "hsm"
    assert calls[0]["params"] == {
        "algorithm": "ed25519", "object_id": 0x10, "label": "k", "domains": [1, 3],
        "capabilities": ["sign-eddsa", "exportable-under-wrap"],
        "auth_key_id": 1, "password": _PASSWORD,
    }
    assert "BEGIN PUBLIC KEY" in result.output


def test_generate_offers_no_rsa():
    result, calls = _run(["hsm1", "generate", "--algorithm", "rsa2048"],
                         env={hsm_mod.PASSWORD_ENV: _PASSWORD})
    assert result.exit_code == 2
    assert calls == []


@pytest.mark.parametrize("domains", ["0", "17", "a,b", ","])
def test_generate_rejects_bad_domains(domains):
    result, calls = _run(["hsm1", "generate", "--algorithm", "ec-p256", "--domains", domains],
                         env={hsm_mod.PASSWORD_ENV: _PASSWORD})
    assert result.exit_code == 2
    assert calls == []


@pytest.mark.parametrize("object_id", ["abc", "0x10000", "-1"])
def test_object_id_validation(object_id):
    result, calls = _run(["hsm1", "pubkey", "--id", object_id],
                         env={hsm_mod.PASSWORD_ENV: _PASSWORD})
    assert result.exit_code == 2
    assert calls == []


def test_objects_table():
    rows = [{"id": 5, "type": "asymmetric-key", "algorithm": "ec-p256",
             "domains": [1, 2], "label": "signer"}]
    result, calls = _run(["hsm1", "objects", "--type", "asymmetric-key"], value=rows,
                         env={hsm_mod.PASSWORD_ENV: _PASSWORD})
    assert result.exit_code == 0, result.output
    assert calls[0]["params"]["type"] == "asymmetric-key"
    assert "label" not in calls[0]["params"]
    assert "0x0005" in result.output and "signer" in result.output


def test_objects_table_fits_long_values():
    rows = [{"id": 1, "type": "authentication-key", "algorithm": "aes128-yubico-authentication",
             "domains": list(range(1, 17)), "label": "DEFAULT AUTHKEY CHANGE THIS ASAP"},
            {"id": 5, "type": "asymmetric-key", "algorithm": "ec-p256",
             "domains": [1, 2, 3, 5], "label": "signer"}]
    result, _ = _run(["hsm1", "objects"], value=rows, env={hsm_mod.PASSWORD_ENV: _PASSWORD})
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[1].split()[:4] == ["0x0001", "authentication-key",
                                    "aes128-yubico-authentication", "1-16"]
    assert lines[2].split()[3] == "1-3,5"
    for header in ("Algorithm", "Domains", "Label"):
        column = lines[0].index(header)
        assert lines[1][column - 1] == lines[2][column - 1] == " "
        assert lines[1][column] != " " and lines[2][column] != " "


# ----- sign -----

def _sign(args, signature=b"\x01\x02\xff", **kwargs):
    value = {"signature": base64.b64encode(signature).decode()}
    return _run(["hsm1", "sign", "--id", "5", "--mechanism", "ecdsa-sha256", *args],
                value=value, env={hsm_mod.PASSWORD_ENV: _PASSWORD}, **kwargs)


def test_sign_data_hex_prints_hex():
    result, calls = _sign(["--data-hex", "deadbeef"])
    assert result.exit_code == 0, result.output
    assert base64.b64decode(calls[0]["params"]["data"]) == b"\xde\xad\xbe\xef"
    assert calls[0]["params"]["mechanism"] == "ecdsa-sha256"
    assert "0102ff" in result.output.splitlines()


def test_sign_data_file_and_out(tmp_path):
    data = tmp_path / "msg.bin"
    data.write_bytes(b"\x00message")
    out = tmp_path / "sig.bin"
    result, calls = _sign(["--data-file", str(data), "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert base64.b64decode(calls[0]["params"]["data"]) == b"\x00message"
    assert out.read_bytes() == b"\x01\x02\xff"


@pytest.mark.parametrize("args", [[], ["--data-hex", "00", "--data-file", __file__]])
def test_sign_needs_exactly_one_data_source(args):
    result, calls = _sign(args)
    assert result.exit_code == 2
    assert calls == []


def test_sign_rejects_bad_hex():
    result, calls = _sign(["--data-hex", "xyz"])
    assert result.exit_code == 2
    assert calls == []


def test_sign_bad_reply_is_an_error():
    result, _ = _run(["hsm1", "sign", "--id", "5", "--mechanism", "eddsa", "--data-hex", "00"],
                     value={}, env={hsm_mod.PASSWORD_ENV: _PASSWORD})
    assert result.exit_code == 1
    assert "no valid signature" in result.output


# ----- parity with the box -----

def test_choice_lists_match_the_box_driver():
    sys.path.insert(0, os.path.join(_REPO, "box"))
    try:
        driver = importlib.import_module("lager.security.hsm.yubihsm2")
    finally:
        sys.path.remove(os.path.join(_REPO, "box"))
    assert hsm_mod.ALGORITHMS == tuple(a for a in driver.ALGORITHMS if not a.startswith("rsa"))
    assert tuple(driver.MECHANISMS) == hsm_mod.MECHANISMS
    assert tuple(driver.OBJECT_TYPES) == hsm_mod.OBJECT_TYPES


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
