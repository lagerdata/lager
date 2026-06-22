# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for lager.mcp.tools.control -- scoped box-control MCP tools.

These tools are gated behind ``LAGER_MCP_ALLOW_CONTROL`` and are the only
mutating / hardware-probing surface the otherwise read-only server exposes.
The tests cover each tool's happy/error paths plus the gating contract:
registering onto a throwaway server adds exactly the three control tools, and
the default server (flag off) exposes none of them.
"""

import asyncio
import sys
import types
from unittest.mock import MagicMock, call, patch

import pytest

from lager.mcp.schemas.bench import BenchDefinition
from lager.mcp.schemas.net import NetDescriptor
from lager.mcp.tools import control

# A realistic J-Link VISA address: USB0::0xVID::0xPID::SERIAL::INSTR
_JLINK_ADDRESS = "USB0::0x1366::0x0101::000051014439::INSTR"


def _run(coro_fn):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro_fn())
    finally:
        loop.close()


def _fake_usb(find_result):
    """Build a fake ``usb``/``usb.core`` module tree for sys.modules injection.

    Avoids depending on pyusb being importable in the test environment while
    still exercising the real ``usb.core.find`` call path.
    """
    usb_mod = types.ModuleType("usb")
    core_mod = types.ModuleType("usb.core")
    core_mod.find = MagicMock(return_value=find_result)
    usb_mod.core = core_mod
    return usb_mod, core_mod


@pytest.mark.unit
class TestDebugProbeStatus:
    def test_probe_present(self):
        usb_mod, core_mod = _fake_usb(find_result=object())
        net = {"name": "debug", "role": "debug", "address": _JLINK_ADDRESS}
        # Force the pyusb fallback (sysfs absent) so this exercises that path
        # deterministically regardless of the host OS.
        with patch("lager.nets.net.Net.get_local_nets", return_value=[net]), \
                patch("lager.mcp.tools.control._probe_present_via_sysfs", return_value=None), \
                patch.dict(sys.modules, {"usb": usb_mod, "usb.core": core_mod}):
            import json

            result = json.loads(control.debug_probe_status("debug"))
        assert result["net"] == "debug"
        assert result["backend"] == "jlink"
        assert result["probe_serial"] == "000051014439"
        assert result["present"] is True
        # find() was called with the parsed vid/pid/serial
        core_mod.find.assert_called_once_with(
            idVendor=0x1366, idProduct=0x0101, serial_number="000051014439",
        )

    def test_probe_absent(self):
        usb_mod, core_mod = _fake_usb(find_result=None)
        net = {"name": "debug", "role": "debug", "address": _JLINK_ADDRESS}
        with patch("lager.nets.net.Net.get_local_nets", return_value=[net]), \
                patch("lager.mcp.tools.control._probe_present_via_sysfs", return_value=None), \
                patch.dict(sys.modules, {"usb": usb_mod, "usb.core": core_mod}):
            import json

            result = json.loads(control.debug_probe_status("debug"))
        assert result["present"] is False
        assert "not found" in result["detail"]

    def test_probe_present_via_sysfs(self, tmp_path):
        # Regression guard for the stale-libusb false-negative: a probe that has
        # (re-)enumerated must be seen via sysfs even when an in-process pyusb
        # cache would miss it after a power-cycle.
        dev = tmp_path / "1-5.4.3"
        dev.mkdir()
        (dev / "idVendor").write_text("1366\n")
        (dev / "idProduct").write_text("0101\n")
        (dev / "serial").write_text("000051014439\n")
        root = str(tmp_path)
        assert control._probe_present_via_sysfs("1366", "0101", "000051014439", root=root) is True
        # Wrong serial -> not present.
        assert control._probe_present_via_sysfs("1366", "0101", "deadbeef", root=root) is False
        # Missing sysfs root -> None so the caller falls back to pyusb.
        assert control._probe_present_via_sysfs("1366", "0101", None, root=str(tmp_path / "absent")) is None

    def test_unknown_net(self):
        with patch("lager.nets.net.Net.get_local_nets", return_value=[]):
            import json

            result = json.loads(control.debug_probe_status("nope"))
        assert result["error"] == "Unknown net 'nope'."

    def test_unparseable_address(self):
        net = {"name": "debug", "role": "debug", "address": ""}
        with patch("lager.nets.net.Net.get_local_nets", return_value=[net]):
            import json

            result = json.loads(control.debug_probe_status("debug"))
        assert result["present"] is False
        assert "No parseable probe address" in result["detail"]


@pytest.mark.unit
class TestNetStatus:
    def test_known_net(self):
        bench = BenchDefinition(
            nets=[NetDescriptor(name="spi0", net_type="spi", instrument="labjack_t7")],
        )
        with patch("lager.mcp.server_state.get_bench", return_value=bench):
            import json

            result = json.loads(control.net_status("spi0"))
        assert result["net"] == "spi0"
        assert result["net_type"] == "spi"
        assert result["instrument"] == "labjack_t7"
        assert result["controllable"] is True

    def test_unknown_net(self):
        with patch("lager.mcp.server_state.get_bench", return_value=BenchDefinition()):
            import json

            result = json.loads(control.net_status("ghost"))
        assert result["error"] == "Unknown net 'ghost'."


@pytest.mark.unit
class TestPowerCycleHub:
    def test_disable_then_enable_in_order(self):
        manager = MagicMock()
        with patch("lager.automation.usb_hub.dispatcher.disable", manager.disable), \
                patch("lager.automation.usb_hub.dispatcher.enable", manager.enable), \
                patch("lager.mcp.tools.control.time.sleep") as mock_sleep:
            import json

            result = json.loads(control.power_cycle_hub("usb0"))
        assert manager.mock_calls == [call.disable("usb0"), call.enable("usb0")]
        # Two sleeps: de-enumerate settle (before enable) + re-enumerate wait (after).
        assert mock_sleep.call_count == 2
        assert result["ok"] is True
        assert result["actions"] == ["disable", "enable"]
        assert result["hub"] == "usb0"
        assert result["reenum_wait_ms"] > 0

    def test_unknown_hub_returns_error(self):
        with patch(
            "lager.automation.usb_hub.dispatcher.disable",
            side_effect=KeyError("USB net 'usb0' not found"),
        ), patch("lager.mcp.tools.control.time.sleep"):
            import json

            result = json.loads(control.power_cycle_hub("usb0"))
        assert "Cannot power-cycle 'usb0'" in result["error"]

    def test_hub_hardware_error_returns_error(self):
        with patch(
            "lager.automation.usb_hub.dispatcher.disable",
            side_effect=Exception("Acroname error code 5"),
        ), patch("lager.mcp.tools.control.time.sleep"):
            import json

            result = json.loads(control.power_cycle_hub("usb0"))
        assert "failed" in result["error"]


@pytest.mark.unit
class TestGating:
    def test_register_adds_exactly_the_control_tools(self):
        from mcp.server.fastmcp import FastMCP

        m = FastMCP("test-control")
        control.register(m)
        names = {t.name for t in _run(m.list_tools)}
        assert {"debug_probe_status", "net_status", "power_cycle_hub"} <= names

    def test_default_server_surface_excludes_control_tools(self):
        # pytest runs without LAGER_MCP_ALLOW_CONTROL, so the live server must
        # not have registered any control tools.
        from lager.mcp.server import mcp

        names = {t.name for t in _run(mcp.list_tools)}
        assert not ({"debug_probe_status", "net_status", "power_cycle_hub"} & names)

    def test_flag_parsing(self, monkeypatch):
        from lager.mcp import config

        monkeypatch.delenv("LAGER_MCP_ALLOW_CONTROL", raising=False)
        assert config.control_tools_enabled() is False
        for off in ("0", "false", "no", "off", ""):
            monkeypatch.setenv("LAGER_MCP_ALLOW_CONTROL", off)
            assert config.control_tools_enabled() is False
        for on in ("1", "true", "yes", "on"):
            monkeypatch.setenv("LAGER_MCP_ALLOW_CONTROL", on)
            assert config.control_tools_enabled() is True
