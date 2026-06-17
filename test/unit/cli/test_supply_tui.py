#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for cli/supply/supply_tui.py.

Covers three regression sources:
  1. SupplyState.render() — output content and markup safety across textual versions
  2. SupplyTUI._parse_command() — command parsing (pure, no Textual needed)
  3. Worker thread offloading — WS send_command must not run on the UI thread
  4. WebSocket connection failure — graceful error display, not a silent crash
  5. on_mount non-blocking — WS connect happens in a worker, not synchronously
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

supply_tui = importlib.import_module('cli.supply.supply_tui')
SupplyState = supply_tui.SupplyState
SupplyTUI = supply_tui.SupplyTUI


def _render_text(widget) -> str:
    """Plain text from a Static widget, compatible with textual v3 and v8+."""
    rendered = widget.render()
    plain = getattr(rendered, 'plain', None)
    return plain if isinstance(plain, str) else str(rendered)


def _make_app() -> SupplyTUI:
    return SupplyTUI(ctx=None, netname="supply1", box="MASTER", box_ip="1.2.3.4")


# --------------------------------------------------------------------------- #
# SupplyState.render()                                                         #
# --------------------------------------------------------------------------- #

class TestSupplyStateRender:
    """SupplyState is a standalone widget — render() can be called without a
    running TUI.  These tests guard against markup-format regressions and
    version-specific textual API changes that would crash the widget."""

    def _state(self, **attrs) -> SupplyState:
        s = SupplyState()
        for k, v in attrs.items():
            setattr(s, k, v)
        return s

    def test_defaults_render_without_error(self):
        text = _render_text(self._state())
        assert text  # non-empty output, no exception

    def test_enabled_on_appears_in_output(self):
        text = _render_text(self._state(enabled="ON"))
        assert "ON" in text

    def test_enabled_off_appears_in_output(self):
        text = _render_text(self._state(enabled="OFF"))
        assert "OFF" in text

    def test_disabled_supply_renders_zero_not_actual_voltage(self):
        # REGRESSION: disabled supply must show 0.000, not the measured voltage.
        text = _render_text(self._state(enabled="OFF", voltage="5.000", current="1.000"))
        assert "00.000" in text

    def test_numeric_voltage_formatted_to_3dp(self):
        text = _render_text(self._state(enabled="ON", voltage="3.3", current="0.1"))
        assert "3.300" in text

    def test_ocp_tripped_yes_in_output(self):
        text = _render_text(self._state(ocp_tripped="YES"))
        assert "YES" in text

    def test_ovp_tripped_yes_in_output(self):
        text = _render_text(self._state(ovp_tripped="YES"))
        assert "YES" in text

    def test_protection_not_tripped_shows_no(self):
        text = _render_text(self._state(ocp_tripped="NO", ovp_tripped="NO"))
        assert "NO" in text

    def test_zero_voltage_renders_cleanly(self):
        text = _render_text(self._state(enabled="ON", voltage="0.0", current="0.0"))
        assert "00.000" in text

    def test_non_numeric_voltage_falls_back_gracefully(self):
        # REGRESSION: a bad string from the box must not raise ValueError.
        text = _render_text(self._state(enabled="ON", voltage="ERR", current="--"))
        assert "00.000" in text  # fallback value, not a traceback

    def test_protection_limits_appear(self):
        text = _render_text(self._state(ocp_limit="2.0", ovp_limit="30.0"))
        assert "2.000" in text
        assert "30.000" in text

    def test_channel_appears(self):
        text = _render_text(self._state(channel="2"))
        assert "CH2" in text

    def test_mode_appears(self):
        text = _render_text(self._state(mode="CV"))
        assert "CV" in text


# --------------------------------------------------------------------------- #
# _parse_command (pure method, no Textual runtime needed)                      #
# --------------------------------------------------------------------------- #

class TestSupplyParseCommand:
    """_parse_command is synchronous and does not touch the event loop.
    _add_log_entry is mocked so error paths don't try to query a widget."""

    def _app(self) -> SupplyTUI:
        app = _make_app()
        app._add_log_entry = MagicMock()
        return app

    def test_voltage_set(self):
        a, p, v = self._app()._parse_command("voltage", ["voltage", "3.3"])
        assert a == "voltage" and p == {"value": 3.3} and v == 3.3

    def test_voltage_read_only(self):
        a, p, v = self._app()._parse_command("voltage", ["voltage"])
        assert a == "voltage" and p == {"value": None} and v is None

    def test_voltage_invalid_returns_none_triple(self):
        app = self._app()
        result = app._parse_command("voltage", ["voltage", "abc"])
        assert result == (None, None, None)
        app._add_log_entry.assert_called_once()

    def test_current_set(self):
        a, p, v = self._app()._parse_command("current", ["current", "0.5"])
        assert a == "current" and p == {"value": 0.5} and v == 0.5

    def test_current_invalid(self):
        app = self._app()
        assert app._parse_command("current", ["current", "bad"])[0] is None

    def test_enable(self):
        a, p, v = self._app()._parse_command("enable", ["enable"])
        assert a == "enable" and p == {} and v is None

    def test_disable(self):
        a, _, _ = self._app()._parse_command("disable", ["disable"])
        assert a == "disable"

    def test_state(self):
        a, _, _ = self._app()._parse_command("state", ["state"])
        assert a == "state"

    def test_ocp_set(self):
        a, p, v = self._app()._parse_command("ocp", ["ocp", "2.0"])
        assert a == "ocp" and p == {"value": 2.0} and v == 2.0

    def test_ovp_set(self):
        a, p, v = self._app()._parse_command("ovp", ["ovp", "30.0"])
        assert a == "ovp" and p == {"value": 30.0} and v == 30.0

    def test_clear_ocp(self):
        a, _, _ = self._app()._parse_command("clear-ocp", ["clear-ocp"])
        assert a == "clear_ocp"

    def test_clear_ovp(self):
        a, _, _ = self._app()._parse_command("clear-ovp", ["clear-ovp"])
        assert a == "clear_ovp"

    def test_unknown_command_returns_none_triple(self):
        app = self._app()
        result = app._parse_command("frobnicate", ["frobnicate"])
        assert result == (None, None, None)
        app._add_log_entry.assert_called_once()

    def test_ocp_read_only(self):
        a, p, _ = self._app()._parse_command("ocp", ["ocp"])
        assert a == "ocp" and p == {"value": None}


# --------------------------------------------------------------------------- #
# Shared fake WebSocket client                                                  #
# --------------------------------------------------------------------------- #

class _FakeSupplyWS:
    """Fast-connect fake — safe to instantiate without a real socketio client."""

    def __init__(self, box_url, netname, update_interval=1.0):
        self.box_url = box_url
        self.netname = netname
        self.connected = False
        self.on_state_update = None
        self.on_error = None
        self.on_connected = None
        self.on_disconnected = None
        self._send_gate = threading.Event()
        self._send_gate.set()  # unblocked by default
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
# REGRESSION: if _run_ws_command_worker is directly awaited inside
# _execute_command (rather than dispatched via run_worker), the TUI freezes
# for the full WS round-trip timeout on every command.
#
# Textual's run_worker(coroutine) runs the coroutine as an asyncio task on the
# event loop thread — not a separate OS thread — so we verify the dispatch
# pattern (run_worker was called) rather than threading.current_thread().

class TestSupplyWorkerThread:
    def test_execute_command_dispatches_via_run_worker(self):
        """_execute_command must call run_worker, not directly await the WS call."""
        async def main():
            with patch.object(supply_tui, 'SupplyWebSocketClient', _FakeSupplyWS):
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
                        await app._execute_command("voltage 3.3")

                    assert dispatched, "_execute_command must dispatch via run_worker"
                    await app.workers.wait_for_complete()

        asyncio.run(main())

    def test_send_command_called_after_execute_command(self):
        """send_command must be invoked as part of command execution."""
        captured_ws: list[_FakeSupplyWS] = []

        class _TrackingWS(_FakeSupplyWS):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                captured_ws.append(self)

        async def main():
            with patch.object(supply_tui, 'SupplyWebSocketClient', _TrackingWS):
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
# REGRESSION: before 0.20.0 a connection failure exited with no message.
# make_ws_failure_message is now called to produce an actionable diagnostic.

class TestSupplyConnectionFailure:
    def test_failure_calls_ws_diagnose(self):
        class _FailWS:
            def __init__(self, box_url, netname, update_interval=1.0):
                self.connected = False
                self.on_state_update = None
                self.on_error = None
                self.on_connected = None
                self.on_disconnected = None

            def connect(self, timeout=10.0):
                raise ConnectionError("connection refused")

            def start_monitoring(self):
                return False

            def disconnect(self):
                pass

        async def main():
            with patch.object(supply_tui, 'SupplyWebSocketClient', _FailWS):
                with patch('cli.core.ws_diagnose.make_ws_failure_message',
                           return_value="Box unreachable — check Tailscale") as diag:
                    app = _make_app()
                    async with app.run_test(size=(100, 40)) as pilot:
                        await pilot.pause()
                        await app.workers.wait_for_complete()
                        await pilot.pause()
            assert diag.called, "make_ws_failure_message should have been called"

        asyncio.run(main())

    def test_failure_sets_exit_error(self):
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
            with patch.object(supply_tui, 'SupplyWebSocketClient', _FailWS):
                with patch('cli.core.ws_diagnose.make_ws_failure_message',
                           return_value="actionable message"):
                    app = _make_app()
                    async with app.run_test(size=(100, 40)) as pilot:
                        await pilot.pause()
                        await app.workers.wait_for_complete()
                        await pilot.pause()
            assert app.exit_error == "actionable message"

        asyncio.run(main())


# --------------------------------------------------------------------------- #
# Startup non-blocking                                                          #
# --------------------------------------------------------------------------- #
# REGRESSION: if on_mount calls ws.connect() synchronously (not via
# run_worker), the TUI is unresponsive until the box answers.

class TestSupplyStartupNonBlocking:
    def test_on_mount_dispatches_connect_via_run_worker(self):
        """on_mount must call run_worker to connect, not directly await."""
        mount_dispatches: list = []

        class _TrackingWS(_FakeSupplyWS):
            pass

        original_run_worker = supply_tui.SupplyTUI.run_worker

        def spy_run_worker(self_inner, *args, **kwargs):
            mount_dispatches.append(args[0] if args else kwargs)
            return original_run_worker(self_inner, *args, **kwargs)

        async def main():
            with patch.object(supply_tui, 'SupplyWebSocketClient', _TrackingWS):
                with patch.object(supply_tui.SupplyTUI, 'run_worker', spy_run_worker):
                    app = _make_app()
                    async with app.run_test(size=(100, 40)) as pilot:
                        await pilot.pause()
                        await app.workers.wait_for_complete()
            assert mount_dispatches, "on_mount must dispatch connect via run_worker"

        asyncio.run(main())

    def test_input_reachable_immediately_after_mount(self):
        """TUI must be interactive as soon as on_mount returns."""
        async def main():
            with patch.object(supply_tui, 'SupplyWebSocketClient', _FakeSupplyWS):
                app = _make_app()
                async with app.run_test(size=(100, 40)) as pilot:
                    await pilot.pause()
                    inp = app.query_one("#command_input")
                    assert inp is not None
                    await app.workers.wait_for_complete()

        asyncio.run(main())


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
