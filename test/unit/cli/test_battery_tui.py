#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for cli/battery/battery_tui.py.

Mirrors test_supply_tui.py — same three regression sources, battery-specific
commands and state fields.  See that file for pattern explanations.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
import threading
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

battery_tui = importlib.import_module('cli.battery.battery_tui')
BatteryState = battery_tui.BatteryState
BatteryTUI = battery_tui.BatteryTUI


def _render_text(widget) -> str:
    """Plain text from a Static widget, compatible with textual v3 and v8+."""
    rendered = widget.render()
    plain = getattr(rendered, 'plain', None)
    return plain if isinstance(plain, str) else str(rendered)


def _make_app() -> BatteryTUI:
    return BatteryTUI(ctx=None, netname="battery1", box_ip="1.2.3.4", dut="MASTER")


# --------------------------------------------------------------------------- #
# BatteryState.render()                                                         #
# --------------------------------------------------------------------------- #

class TestBatteryStateRender:
    def _state(self, **attrs) -> BatteryState:
        s = BatteryState()
        for k, v in attrs.items():
            setattr(s, k, v)
        return s

    def test_defaults_render_without_error(self):
        text = _render_text(self._state())
        assert text

    def test_enabled_on_appears(self):
        text = _render_text(self._state(enabled="ON"))
        assert "ON" in text

    def test_enabled_off_appears(self):
        text = _render_text(self._state(enabled="OFF"))
        assert "OFF" in text

    def test_terminal_voltage_formatted(self):
        text = _render_text(self._state(terminal_voltage="3.7"))
        assert "3.700" in text

    def test_soc_appears(self):
        text = _render_text(self._state(soc="80"))
        assert "80" in text

    def test_voc_appears(self):
        text = _render_text(self._state(voc="3.65"))
        assert "3.650" in text

    def test_ocp_tripped(self):
        text = _render_text(self._state(ocp_tripped="YES"))
        assert "YES" in text

    def test_ovp_tripped(self):
        text = _render_text(self._state(ovp_tripped="YES"))
        assert "YES" in text

    def test_protection_not_tripped(self):
        text = _render_text(self._state(ocp_tripped="NO", ovp_tripped="NO"))
        assert "NO" in text

    def test_capacity_appears(self):
        text = _render_text(self._state(capacity="2.5"))
        assert "2.5" in text

    def test_esr_appears(self):
        text = _render_text(self._state(esr="0.067"))
        assert "0.067" in text

    def test_non_numeric_voltage_falls_back(self):
        text = _render_text(self._state(terminal_voltage="ERR"))
        assert "00.000" in text

    def test_model_name_appears(self):
        text = _render_text(self._state(model="18650"))
        assert "18650" in text

    def test_long_model_name_truncated(self):
        text = _render_text(self._state(model="A" * 25))
        assert "..." in text

    def test_static_mode_appears(self):
        text = _render_text(self._state(mode="Static"))
        assert "Static" in text

    def test_channel_appears(self):
        text = _render_text(self._state(channel="1"))
        assert "CH1" in text

    def test_volt_full_and_empty(self):
        text = _render_text(self._state(volt_full="4.200", volt_empty="3.000"))
        assert "4.200" in text
        assert "3.000" in text


# --------------------------------------------------------------------------- #
# _parse_command (pure, no Textual runtime)                                     #
# --------------------------------------------------------------------------- #

class TestBatteryParseCommand:
    def _app(self) -> BatteryTUI:
        app = _make_app()
        app._add_log_entry = MagicMock()
        return app

    def test_soc_set(self):
        a, p, v = self._app()._parse_command("soc", ["soc", "80"])
        assert a == "soc" and p == {"value": 80.0} and v == 80.0

    def test_soc_read_only(self):
        a, p, v = self._app()._parse_command("soc", ["soc"])
        assert a == "soc" and p == {"value": None}

    def test_soc_out_of_range_returns_none(self):
        app = self._app()
        assert app._parse_command("soc", ["soc", "150"])[0] is None
        app._add_log_entry.assert_called_once()

    def test_soc_invalid_string(self):
        app = self._app()
        assert app._parse_command("soc", ["soc", "abc"])[0] is None

    def test_voc_set(self):
        a, p, v = self._app()._parse_command("voc", ["voc", "3.7"])
        assert a == "voc" and p == {"value": 3.7} and v == 3.7

    def test_batt_full_set(self):
        a, p, v = self._app()._parse_command("batt-full", ["batt-full", "4.2"])
        assert a == "batt_full" and p == {"value": 4.2}

    def test_batt_empty_set(self):
        a, p, v = self._app()._parse_command("batt-empty", ["batt-empty", "3.0"])
        assert a == "batt_empty" and p == {"value": 3.0}

    def test_capacity_set(self):
        a, p, v = self._app()._parse_command("capacity", ["capacity", "2.5"])
        assert a == "capacity" and p == {"value": 2.5}

    def test_current_limit_set(self):
        a, p, v = self._app()._parse_command("current-limit", ["current-limit", "1.0"])
        assert a == "current_limit" and p == {"value": 1.0}

    def test_ocp_set(self):
        a, p, v = self._app()._parse_command("ocp", ["ocp", "2.0"])
        assert a == "ocp" and p == {"value": 2.0}

    def test_ovp_set(self):
        a, p, v = self._app()._parse_command("ovp", ["ovp", "4.5"])
        assert a == "ovp" and p == {"value": 4.5}

    def test_mode_static(self):
        a, p, v = self._app()._parse_command("mode", ["mode", "static"])
        assert a == "mode" and p == {"mode_type": "static"} and v == "static"

    def test_mode_dynamic(self):
        a, p, _ = self._app()._parse_command("mode", ["mode", "dynamic"])
        assert a == "mode" and p == {"mode_type": "dynamic"}

    def test_mode_invalid(self):
        app = self._app()
        assert app._parse_command("mode", ["mode", "turbo"])[0] is None

    def test_model_set(self):
        a, p, v = self._app()._parse_command("model", ["model", "18650"])
        assert a == "model" and p == {"partnumber": "18650"} and v == "18650"

    def test_model_read_only(self):
        a, p, _ = self._app()._parse_command("model", ["model"])
        assert a == "model" and p == {"partnumber": None}

    def test_enable(self):
        a, p, v = self._app()._parse_command("enable", ["enable"])
        assert a == "enable" and p == {} and v is None

    def test_disable(self):
        a, _, _ = self._app()._parse_command("disable", ["disable"])
        assert a == "disable"

    def test_state(self):
        a, _, _ = self._app()._parse_command("state", ["state"])
        assert a == "state"

    def test_set(self):
        a, _, _ = self._app()._parse_command("set", ["set"])
        assert a == "set"

    def test_clear_ocp(self):
        a, _, _ = self._app()._parse_command("clear-ocp", ["clear-ocp"])
        assert a == "clear_ocp"

    def test_clear_ovp(self):
        a, _, _ = self._app()._parse_command("clear-ovp", ["clear-ovp"])
        assert a == "clear_ovp"

    def test_clear_protection(self):
        a, _, _ = self._app()._parse_command("clear-protection", ["clear-protection"])
        assert a == "clear"

    def test_clear_prot_alias(self):
        a, _, _ = self._app()._parse_command("clear-prot", ["clear-prot"])
        assert a == "clear"

    def test_unknown_command(self):
        app = self._app()
        assert app._parse_command("frobnicate", ["frobnicate"]) == (None, None, None)


# --------------------------------------------------------------------------- #
# Shared fake WebSocket client                                                  #
# --------------------------------------------------------------------------- #

class _FakeBatteryWS:
    """Fast-connect fake — safe without a real socketio client."""

    def __init__(self, box_url, netname, update_interval=1.0):
        self.box_url = box_url
        self.netname = netname
        self.connected = False
        self.on_state_update = None
        self.on_error = None
        self.on_connected = None
        self.on_disconnected = None
        self._send_gate = threading.Event()
        self._send_gate.set()
        self.send_threads: list[threading.Thread] = []
        self._response: dict = {"success": True, "message": "ok"}

    def connect(self, timeout=10.0):
        self.connected = True
        return True

    def start_monitoring(self):
        return True

    def send_command(self, action, params, timeout=5.0):
        self.send_threads.append(threading.current_thread())
        self._send_gate.wait(timeout=5)
        return self._response

    def disconnect(self):
        pass


# --------------------------------------------------------------------------- #
# Worker dispatch                                                               #
# --------------------------------------------------------------------------- #
# REGRESSION: if _run_ws_command_worker is directly awaited instead of
# dispatched via run_worker, the TUI freezes for the full WS timeout per cmd.
#
# Textual's run_worker(coroutine) runs on the event loop thread (not a
# separate OS thread), so we verify the dispatch pattern directly.

class TestBatteryWorkerThread:
    def test_execute_command_dispatches_via_run_worker(self):
        """_execute_command must call run_worker, not directly await the WS call."""
        async def main():
            with patch.object(battery_tui, 'BatteryWebSocketClient', _FakeBatteryWS):
                app = _make_app()
                async with app.run_test(size=(100, 40)) as pilot:
                    await pilot.pause()
                    await app.workers.wait_for_complete()
                    await pilot.pause()

                    dispatched: list = []
                    orig = app.run_worker

                    def spy(*args, **kwargs):
                        dispatched.append(args[0] if args else kwargs)
                        return orig(*args, **kwargs)

                    with patch.object(app, 'run_worker', side_effect=spy):
                        await app._execute_command("soc 80")

                    assert dispatched, "_execute_command must dispatch via run_worker"
                    await app.workers.wait_for_complete()

        asyncio.run(main())

    def test_send_command_called_after_execute_command(self):
        """send_command must be invoked as part of command execution."""
        captured_ws: list[_FakeBatteryWS] = []

        class _TrackingWS(_FakeBatteryWS):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                captured_ws.append(self)

        async def main():
            with patch.object(battery_tui, 'BatteryWebSocketClient', _TrackingWS):
                app = _make_app()
                async with app.run_test(size=(100, 40)) as pilot:
                    await pilot.pause()
                    await app.workers.wait_for_complete()
                    await pilot.pause()
                    await app._execute_command("enable")
                    await app.workers.wait_for_complete()
                    await pilot.pause()
            assert captured_ws[0].send_threads, "send_command was never called"

        asyncio.run(main())


# --------------------------------------------------------------------------- #
# WebSocket connection failure                                                  #
# --------------------------------------------------------------------------- #

class TestBatteryConnectionFailure:
    def test_failure_calls_ws_diagnose(self):
        class _FailWS:
            def __init__(self, box_url, netname, update_interval=1.0):
                self.connected = False
                self.on_state_update = None
                self.on_error = None
                self.on_connected = None
                self.on_disconnected = None

            def connect(self, timeout=10.0):
                raise ConnectionError("refused")

            def start_monitoring(self):
                return False

            def disconnect(self):
                pass

        async def main():
            with patch.object(battery_tui, 'BatteryWebSocketClient', _FailWS):
                with patch('cli.core.ws_diagnose.make_ws_failure_message',
                           return_value="Box unreachable — check Tailscale") as diag:
                    app = _make_app()
                    async with app.run_test(size=(100, 40)) as pilot:
                        await pilot.pause()
                        await app.workers.wait_for_complete()
                        await pilot.pause()
            assert diag.called

        asyncio.run(main())

    def test_start_monitoring_failure_calls_ws_diagnose(self):
        class _MonitorFailWS:
            def __init__(self, box_url, netname, update_interval=1.0):
                self.connected = False
                self.on_state_update = None
                self.on_error = None
                self.on_connected = None
                self.on_disconnected = None

            def connect(self, timeout=10.0):
                self.connected = True
                return False  # connect returns False — triggers the "failed" exception

            def start_monitoring(self):
                return False

            def disconnect(self):
                pass

        async def main():
            with patch.object(battery_tui, 'BatteryWebSocketClient', _MonitorFailWS):
                with patch('cli.core.ws_diagnose.make_ws_failure_message',
                           return_value="WS server missing") as diag:
                    app = _make_app()
                    async with app.run_test(size=(100, 40)) as pilot:
                        await pilot.pause()
                        await app.workers.wait_for_complete()
                        await pilot.pause()
            assert diag.called

        asyncio.run(main())


# --------------------------------------------------------------------------- #
# Startup non-blocking                                                          #
# --------------------------------------------------------------------------- #
# REGRESSION: if on_mount calls ws.connect() synchronously (not via
# run_worker), the TUI is unresponsive until the box answers.

class TestBatteryStartupNonBlocking:
    def test_on_mount_dispatches_connect_via_run_worker(self):
        """on_mount must call run_worker to connect, not directly await."""
        mount_dispatches: list = []

        class _TrackingWS(_FakeBatteryWS):
            pass

        original_run_worker = battery_tui.BatteryTUI.run_worker

        def spy_run_worker(self_inner, *args, **kwargs):
            mount_dispatches.append(args[0] if args else kwargs)
            return original_run_worker(self_inner, *args, **kwargs)

        async def main():
            with patch.object(battery_tui, 'BatteryWebSocketClient', _TrackingWS):
                with patch.object(battery_tui.BatteryTUI, 'run_worker', spy_run_worker):
                    app = _make_app()
                    async with app.run_test(size=(100, 40)) as pilot:
                        await pilot.pause()
                        await app.workers.wait_for_complete()
            assert mount_dispatches, "on_mount must dispatch connect via run_worker"

        asyncio.run(main())

    def test_input_reachable_immediately_after_mount(self):
        """TUI must be interactive as soon as on_mount returns."""
        async def main():
            with patch.object(battery_tui, 'BatteryWebSocketClient', _FakeBatteryWS):
                app = _make_app()
                async with app.run_test(size=(100, 40)) as pilot:
                    await pilot.pause()
                    inp = app.query_one("#command_input")
                    assert inp is not None
                    await app.workers.wait_for_complete()

        asyncio.run(main())


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
