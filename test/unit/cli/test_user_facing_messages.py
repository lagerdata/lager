# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Messages that told the user something untrue (#517).

Each of these was a sentence, not a behavior, so nothing failed when it was
wrong:

  * `lager debug gdbserver` and `disconnect` named JLinkGDBServer in their help
    and in the disconnect output, on a net whose backend runs OpenOCD;
  * `lager nets state` against an old box said the endpoint needs box version
    0.33. It shipped in 0.34.0;
  * the host-networking warning after `lager box-config apply` sent the user to
    `lager box-config network-mode show`, which prints only the mode.

The `lager boxes` summary line is pinned in `test_boxes_live_listing.py`, and
the doubled `Erase failed:` in `test_debug_flash_erase_reconnect.py`, next to
the harnesses those commands already use.
"""

import importlib
import pathlib
import re
from unittest import mock

import pytest
from click.testing import CliRunner

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]

debug_mod = importlib.import_module("cli.commands.development.debug.commands")
nets_mod = importlib.import_module("cli.commands.box.nets")
config_mod = importlib.import_module("cli.commands.box.config")


# ---------------------------------------------------------------------------
# lager debug: the GDB server is named for the net's backend
# ---------------------------------------------------------------------------

class _Obj:
    """Settable stand-in for the LagerContext (the group stashes `net_name`)."""


class _DisconnectClient:
    def __init__(self, response):
        self.response = response
        self.closed = False

    def disconnect(self, net, keep_jlink_running=False):
        return self.response

    def close(self):
        self.closed = True


def _disconnect(response, args=()):
    obj = _Obj()
    obj.net_name = "debug1"
    net = {"name": "debug1", "role": "debug"}
    with mock.patch.object(debug_mod, "_resolve_box_with_username",
                           lambda ctx, box: ("192.0.2.4", "lagerdata")), \
            mock.patch.object(debug_mod, "_get_debug_net",
                              lambda ctx, box, net_name=None: net), \
            mock.patch.object(debug_mod, "_get_service_client",
                              lambda box: _DisconnectClient(response)):
        return CliRunner().invoke(debug_mod.disconnect, ["--box", "b", *args], obj=obj,
                                  catch_exceptions=False)


@pytest.mark.parametrize("backend, label", [
    ("openocd", "OpenOCD"),
    ("jlink", "JLinkGDBServer"),
    (None, "JLinkGDBServer"),       # a box too old to send `backend`
])
def test_disconnect_names_the_server_the_backend_runs(backend, label):
    response = {"status": "disconnected", "gdb_port": 2331}
    if backend:
        response["backend"] = backend
    result = _disconnect(response)
    assert result.exit_code == 0, result.output
    assert f"{label} stopped" in result.output

    result = _disconnect(response, ["--keep-server"])
    assert f"{label} still running on 192.0.2.4:2331" in result.output


def test_an_openocd_disconnect_does_not_mention_jlink():
    result = _disconnect({"status": "disconnected", "backend": "openocd"})
    assert "JLink" not in result.output


@pytest.mark.parametrize("command", ["gdbserver", "disconnect"])
def test_the_help_does_not_name_one_backend_only(command):
    help_line = getattr(debug_mod, command).get_short_help_str(limit=200)
    assert "OpenOCD" in help_line, help_line
    assert "GDB server" in help_line, help_line


def test_keep_server_help_is_backend_neutral():
    option = next(p for p in debug_mod.disconnect.params if p.name == "keep_server")
    assert "JLinkGDBServer" not in option.help


# ---------------------------------------------------------------------------
# lager nets state: the version the endpoint shipped in
# ---------------------------------------------------------------------------

def test_nets_state_on_an_old_box_names_the_right_version():
    with mock.patch.object(nets_mod, "_resolve_box", return_value="192.0.2.4"), \
            mock.patch.object(nets_mod, "_fetch_saved_nets", return_value=[]), \
            mock.patch.object(nets_mod, "_fetch_net_state", return_value=None):
        result = CliRunner().invoke(nets_mod.nets, ["state", "--box", "b"], obj=_Obj())
    assert "requires box version 0.34.0 or later" in result.output, result.output
    assert "0.33" not in result.output


def test_the_nets_state_endpoint_shipped_in_0_34_0():
    """The version in the message is the one the release notes record."""
    notes = (REPO_ROOT / "docs/source/release-notes/v0.34.0.mdx").read_text()
    assert "nets state" in notes


# ---------------------------------------------------------------------------
# lager box-config apply: the host-networking warning points somewhere useful
# ---------------------------------------------------------------------------

def test_the_host_networking_warning_does_not_send_users_to_show():
    source = (REPO_ROOT / "cli/commands/box/config.py").read_text()
    assert not re.search(r"network-mode\s*\"?\s*\"?\s*show` explains", source)
    assert "{_NETWORK_MODE_DOCS_URL}" in source


def test_the_docs_url_names_a_real_page_and_heading():
    url = config_mod._NETWORK_MODE_DOCS_URL
    match = re.fullmatch(
        r"https://docs\.lagerdata\.com/(source/reference/cli/[\w-]+)#([\w-]+)", url)
    assert match, url
    page = REPO_ROOT / "docs" / f"{match.group(1)}.mdx"
    assert page.exists(), page
    headings = re.findall(r"^#+\s+`?([^`\n]+?)`?\s*$", page.read_text(), re.M)
    anchors = {re.sub(r"[^a-z0-9]+", "-", h.lower()).strip("-") for h in headings}
    assert match.group(2) in anchors, sorted(anchors)
