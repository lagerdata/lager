# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the on-box scenario interpreter (scenario_runner.py).

All hardware interaction is mocked via ``lager.Net.get()`` -- no real
hardware or :8080/invoke calls are made.
"""

import json
import time
from unittest.mock import patch, MagicMock, PropertyMock

import pytest

from lager.mcp.engine.scenario_runner import (
    _HANDLER_REGISTRY,
    _net_cache,
    _uart_serials,
    _rtt_sessions,
    _cleanup_all,
    _safe_eval,
    execute_step,
    evaluate_assertions,
    run,
)


# ---------------------------------------------------------------------------
# Shared fixture: clear caches before/after every test
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clear_caches():
    _cleanup_all()
    yield
    _cleanup_all()


# ---------------------------------------------------------------------------
# Helper: build a mock that Net.get() returns, keyed by NetType
# ---------------------------------------------------------------------------

def _mock_net_get(mocks_by_type: dict):
    """Return a side_effect function for ``lager.Net.get``."""
    def side_effect(name, *, type=None):
        if type in mocks_by_type:
            return mocks_by_type[type]
        mock = MagicMock(name=f"Net<{name},{type}>")
        mocks_by_type[type] = mock
        return mock
    return side_effect


# ---------------------------------------------------------------------------
# Registry tests
# ---------------------------------------------------------------------------

class TestHandlerRegistry:
    EXPECTED_ACTIONS = sorted([
        "gpio_set", "gpio_read", "gpio_wait",
        "uart_send", "uart_expect",
        "spi_config", "spi_transfer", "spi_read", "spi_write",
        "i2c_config", "i2c_scan", "i2c_read", "i2c_write", "i2c_write_read",
        "set_voltage", "set_current", "enable_supply", "disable_supply", "measure",
        "adc_read", "dac_set",
        "usb_enable", "usb_disable",
        "debug_connect", "debug_disconnect", "debug_flash",
        "debug_reset", "debug_erase", "debug_read_memory",
        "rtt_write", "rtt_expect",
        "watt_read", "watt_read_all", "tc_read",
        "battery_enable", "battery_disable", "battery_soc", "battery_voc",
        "battery_set", "battery_state",
        "eload_set", "eload_enable", "eload_disable", "eload_state",
        "energy_read", "energy_stats",
        "wait",
    ])

    def test_all_actions_registered(self):
        for action_name in self.EXPECTED_ACTIONS:
            assert action_name in _HANDLER_REGISTRY, f"Missing handler for {action_name}"

    def test_no_unexpected_actions(self):
        for action_name in _HANDLER_REGISTRY:
            assert action_name in self.EXPECTED_ACTIONS, f"Unexpected handler: {action_name}"


# ---------------------------------------------------------------------------
# GPIO tests
# ---------------------------------------------------------------------------

class TestGPIOHandlers:
    @pytest.fixture
    def gpio_net(self):
        mock = MagicMock(name="gpio_net")
        mock.input.return_value = 0
        mock.wait_for_level.return_value = 0.05
        return mock

    @patch("lager.Net.get")
    def test_gpio_set(self, mock_get):
        gpio = MagicMock()
        mock_get.return_value = gpio
        data = _HANDLER_REGISTRY["gpio_set"]("button0", {"level": 1}, {})
        gpio.output.assert_called_once_with(1)
        assert data == {"target": "button0", "level": 1}

    @patch("lager.Net.get")
    def test_gpio_set_low(self, mock_get):
        gpio = MagicMock()
        mock_get.return_value = gpio
        data = _HANDLER_REGISTRY["gpio_set"]("button0", {"level": 0}, {})
        gpio.output.assert_called_once_with(0)
        assert data["level"] == 0

    @patch("lager.Net.get")
    def test_gpio_read(self, mock_get):
        gpio = MagicMock()
        gpio.input.return_value = 1
        mock_get.return_value = gpio
        results = {}
        data = _HANDLER_REGISTRY["gpio_read"]("led0", {"label": "after_press"}, results)
        gpio.input.assert_called_once()
        assert data == {"target": "led0", "value": 1}
        assert results["after_press"] == {"target": "led0", "value": 1}

    @patch("lager.Net.get")
    def test_gpio_read_without_label(self, mock_get):
        gpio = MagicMock()
        gpio.input.return_value = 0
        mock_get.return_value = gpio
        results = {}
        data = _HANDLER_REGISTRY["gpio_read"]("pin0", {}, results)
        assert data["value"] == 0
        assert len(results) == 0

    @patch("lager.Net.get")
    def test_gpio_wait(self, mock_get):
        gpio = MagicMock()
        gpio.wait_for_level.return_value = 0.123
        mock_get.return_value = gpio
        results = {}
        data = _HANDLER_REGISTRY["gpio_wait"]("pin0", {"level": 1, "timeout": 5.0, "label": "edge"}, results)
        gpio.wait_for_level.assert_called_once_with(1, 5.0)
        assert data["elapsed_s"] == 0.123
        assert results["edge"]["elapsed_s"] == 0.123


# ---------------------------------------------------------------------------
# UART tests
# ---------------------------------------------------------------------------

class TestUARTHandlers:
    @pytest.fixture
    def uart_mocks(self):
        mock_serial = MagicMock(name="serial_connection")
        mock_serial.timeout = 1
        mock_serial.readline.return_value = b"OK\r\n"

        uart_net = MagicMock(name="uart_net")
        uart_net.connect.return_value = mock_serial

        return {"net": uart_net, "ser": mock_serial}

    @patch("lager.Net.get")
    def test_uart_send(self, mock_get, uart_mocks):
        mock_get.return_value = uart_mocks["net"]
        data = _HANDLER_REGISTRY["uart_send"]("uart0", {"data": "status\r\n"}, {})
        uart_mocks["net"].connect.assert_called_once()
        uart_mocks["ser"].write.assert_called_once_with(b"status\r\n")
        assert data == {"target": "uart0", "data": "status\r\n"}

    @patch("lager.Net.get")
    def test_uart_send_custom_baudrate(self, mock_get, uart_mocks):
        mock_get.return_value = uart_mocks["net"]
        _HANDLER_REGISTRY["uart_send"]("uart0", {"data": "hello", "baudrate": 9600}, {})
        uart_mocks["net"].connect.assert_called_once_with(baudrate=9600, timeout=1)

    @patch("lager.Net.get")
    def test_uart_expect_match(self, mock_get, uart_mocks):
        uart_mocks["ser"].readline.side_effect = [b"boot\r\n", b"btn_pressed\r\n"]
        mock_get.return_value = uart_mocks["net"]
        results = {}
        data = _HANDLER_REGISTRY["uart_expect"](
            "uart0", {"pattern": "btn_pressed", "label": "dut_press", "timeout_ms": 2000}, results
        )
        assert data["matched"] is True
        assert "btn_pressed" in data["output"]
        assert results["dut_press"]["matched"] is True

    @patch("lager.Net.get")
    def test_uart_expect_no_match(self, mock_get, uart_mocks):
        uart_mocks["ser"].readline.return_value = b""
        mock_get.return_value = uart_mocks["net"]
        data = _HANDLER_REGISTRY["uart_expect"]("uart0", {"pattern": "never", "timeout_ms": 100}, {})
        assert data["matched"] is False

    @patch("lager.Net.get")
    def test_uart_connection_cached(self, mock_get, uart_mocks):
        mock_get.return_value = uart_mocks["net"]
        _HANDLER_REGISTRY["uart_send"]("uart0", {"data": "first"}, {})
        _HANDLER_REGISTRY["uart_send"]("uart0", {"data": "second"}, {})
        uart_mocks["net"].connect.assert_called_once()

    @patch("lager.Net.get")
    def test_cleanup_closes_serial(self, mock_get, uart_mocks):
        mock_get.return_value = uart_mocks["net"]
        _HANDLER_REGISTRY["uart_send"]("uart0", {"data": "test"}, {})
        assert "uart0" in _uart_serials
        _cleanup_all()
        assert len(_uart_serials) == 0
        uart_mocks["ser"].close.assert_called_once()


# ---------------------------------------------------------------------------
# SPI tests
# ---------------------------------------------------------------------------

class TestSPIHandlers:
    @pytest.fixture
    def spi_net(self):
        net = MagicMock(name="spi_net")
        net.read_write.return_value = [0x9F, 0x01, 0x02]
        net.read.return_value = [0xAA, 0xBB]
        return net

    @patch("lager.Net.get")
    def test_spi_config(self, mock_get, spi_net):
        mock_get.return_value = spi_net
        data = _HANDLER_REGISTRY["spi_config"]("spi0", {"mode": 0, "frequency_hz": 1000000}, {})
        spi_net.config.assert_called_once_with(mode=0, frequency_hz=1000000)
        assert data["config"]["mode"] == 0

    @patch("lager.Net.get")
    def test_spi_transfer(self, mock_get, spi_net):
        mock_get.return_value = spi_net
        results = {}
        data = _HANDLER_REGISTRY["spi_transfer"]("spi0", {"data": [0x9F], "label": "jedec"}, results)
        spi_net.read_write.assert_called_once_with([0x9F])
        assert data["rx_data"] == [0x9F, 0x01, 0x02]
        assert results["jedec"]["rx_data"] == [0x9F, 0x01, 0x02]

    @patch("lager.Net.get")
    def test_spi_read(self, mock_get, spi_net):
        mock_get.return_value = spi_net
        results = {}
        data = _HANDLER_REGISTRY["spi_read"]("spi0", {"n_words": 2, "label": "rx"}, results)
        spi_net.read.assert_called_once_with(2, fill=0x00)
        assert data["rx_data"] == [0xAA, 0xBB]

    @patch("lager.Net.get")
    def test_spi_write(self, mock_get, spi_net):
        mock_get.return_value = spi_net
        data = _HANDLER_REGISTRY["spi_write"]("spi0", {"data": [0x01, 0x02]}, {})
        spi_net.write.assert_called_once_with([0x01, 0x02])
        assert data["data"] == [0x01, 0x02]


# ---------------------------------------------------------------------------
# I2C tests
# ---------------------------------------------------------------------------

class TestI2CHandlers:
    @pytest.fixture
    def i2c_net(self):
        net = MagicMock(name="i2c_net")
        net.scan.return_value = [0x48, 0x68]
        net.read.return_value = [0x12, 0x34]
        net.write_read.return_value = [0xAB]
        return net

    @patch("lager.Net.get")
    def test_i2c_config(self, mock_get, i2c_net):
        mock_get.return_value = i2c_net
        data = _HANDLER_REGISTRY["i2c_config"]("i2c0", {"frequency_hz": 400000, "pull_ups": True}, {})
        i2c_net.config.assert_called_once_with(frequency_hz=400000, pull_ups=True)

    @patch("lager.Net.get")
    def test_i2c_scan(self, mock_get, i2c_net):
        mock_get.return_value = i2c_net
        results = {}
        data = _HANDLER_REGISTRY["i2c_scan"]("i2c0", {"label": "devs"}, results)
        assert data["addresses"] == [0x48, 0x68]
        assert results["devs"]["addresses"] == [0x48, 0x68]

    @patch("lager.Net.get")
    def test_i2c_read(self, mock_get, i2c_net):
        mock_get.return_value = i2c_net
        results = {}
        data = _HANDLER_REGISTRY["i2c_read"]("i2c0", {"address": 0x48, "num_bytes": 2, "label": "temp"}, results)
        i2c_net.read.assert_called_once_with(0x48, 2)
        assert data["rx_data"] == [0x12, 0x34]

    @patch("lager.Net.get")
    def test_i2c_write(self, mock_get, i2c_net):
        mock_get.return_value = i2c_net
        data = _HANDLER_REGISTRY["i2c_write"]("i2c0", {"address": 0x48, "data": [0x00]}, {})
        i2c_net.write.assert_called_once_with(0x48, [0x00])

    @patch("lager.Net.get")
    def test_i2c_write_read(self, mock_get, i2c_net):
        mock_get.return_value = i2c_net
        results = {}
        data = _HANDLER_REGISTRY["i2c_write_read"](
            "i2c0", {"address": 0x48, "data": [0x00], "num_bytes": 1, "label": "reg"}, results
        )
        i2c_net.write_read.assert_called_once_with(0x48, [0x00], 1)
        assert results["reg"]["rx_data"] == [0xAB]


# ---------------------------------------------------------------------------
# Power supply tests
# ---------------------------------------------------------------------------

class TestPowerSupplyHandlers:
    @pytest.fixture
    def psu_net(self):
        net = MagicMock(name="psu_net")
        net.voltage.return_value = 3.28
        net.current.return_value = 0.150
        net.power.return_value = 0.492
        return net

    @patch("lager.Net.get")
    def test_set_voltage(self, mock_get, psu_net):
        mock_get.return_value = psu_net
        data = _HANDLER_REGISTRY["set_voltage"]("psu1", {"voltage": 3.3}, {})
        psu_net.set_voltage.assert_called_once_with(3.3)
        assert data == {"target": "psu1", "voltage": 3.3}

    @patch("lager.Net.get")
    def test_set_current(self, mock_get, psu_net):
        mock_get.return_value = psu_net
        data = _HANDLER_REGISTRY["set_current"]("psu1", {"current": 0.5}, {})
        psu_net.set_current.assert_called_once_with(0.5)
        assert data == {"target": "psu1", "current": 0.5}

    @patch("lager.Net.get")
    def test_enable_supply(self, mock_get, psu_net):
        mock_get.return_value = psu_net
        data = _HANDLER_REGISTRY["enable_supply"]("psu1", {}, {})
        psu_net.enable.assert_called_once()
        assert data["enabled"] is True

    @patch("lager.Net.get")
    def test_disable_supply(self, mock_get, psu_net):
        mock_get.return_value = psu_net
        data = _HANDLER_REGISTRY["disable_supply"]("psu1", {}, {})
        psu_net.disable.assert_called_once()
        assert data["enabled"] is False

    @patch("lager.Net.get")
    def test_measure_voltage(self, mock_get, psu_net):
        mock_get.return_value = psu_net
        results = {}
        data = _HANDLER_REGISTRY["measure"]("psu1", {"type": "voltage", "label": "v_out"}, results)
        psu_net.voltage.assert_called_once()
        assert data["value"] == 3.28
        assert results["v_out"]["value"] == 3.28

    @patch("lager.Net.get")
    def test_measure_current(self, mock_get, psu_net):
        mock_get.return_value = psu_net
        results = {}
        data = _HANDLER_REGISTRY["measure"]("psu1", {"type": "current", "label": "i_out"}, results)
        assert data["value"] == 0.150


# ---------------------------------------------------------------------------
# ADC / DAC tests
# ---------------------------------------------------------------------------

class TestADCDACHandlers:
    @patch("lager.Net.get")
    def test_adc_read(self, mock_get):
        adc = MagicMock()
        adc.input.return_value = 1.65
        mock_get.return_value = adc
        results = {}
        data = _HANDLER_REGISTRY["adc_read"]("adc0", {"label": "voltage"}, results)
        adc.input.assert_called_once()
        assert data["value"] == 1.65
        assert results["voltage"]["value"] == 1.65

    @patch("lager.Net.get")
    def test_dac_set(self, mock_get):
        dac = MagicMock()
        mock_get.return_value = dac
        data = _HANDLER_REGISTRY["dac_set"]("dac0", {"voltage": 2.5}, {})
        dac.output.assert_called_once_with(2.5)
        assert data == {"target": "dac0", "voltage": 2.5}


# ---------------------------------------------------------------------------
# USB tests
# ---------------------------------------------------------------------------

class TestUSBHandlers:
    @patch("lager.Net.get")
    def test_usb_enable(self, mock_get):
        usb = MagicMock()
        mock_get.return_value = usb
        data = _HANDLER_REGISTRY["usb_enable"]("usb0", {}, {})
        usb.enable.assert_called_once()
        assert data["enabled"] is True

    @patch("lager.Net.get")
    def test_usb_disable(self, mock_get):
        usb = MagicMock()
        mock_get.return_value = usb
        data = _HANDLER_REGISTRY["usb_disable"]("usb0", {}, {})
        usb.disable.assert_called_once()
        assert data["enabled"] is False


# ---------------------------------------------------------------------------
# Debug tests
# ---------------------------------------------------------------------------

class TestDebugHandlers:
    @pytest.fixture
    def debug_net(self):
        net = MagicMock(name="debug_net")
        net.read_memory.return_value = "DEADBEEF"
        return net

    @patch("lager.Net.get")
    def test_debug_connect(self, mock_get, debug_net):
        mock_get.return_value = debug_net
        data = _HANDLER_REGISTRY["debug_connect"]("jtag0", {"speed": 4000, "transport": "swd"}, {})
        debug_net.connect.assert_called_once_with(speed=4000, transport="swd")
        assert data["connected"] is True

    @patch("lager.Net.get")
    def test_debug_disconnect(self, mock_get, debug_net):
        mock_get.return_value = debug_net
        data = _HANDLER_REGISTRY["debug_disconnect"]("jtag0", {}, {})
        debug_net.disconnect.assert_called_once()
        assert data["connected"] is False

    @patch("lager.Net.get")
    def test_debug_flash(self, mock_get, debug_net):
        mock_get.return_value = debug_net
        data = _HANDLER_REGISTRY["debug_flash"]("jtag0", {"firmware_path": "/tmp/fw.hex"}, {})
        debug_net.flash.assert_called_once_with("/tmp/fw.hex")

    @patch("lager.Net.get")
    def test_debug_reset(self, mock_get, debug_net):
        mock_get.return_value = debug_net
        data = _HANDLER_REGISTRY["debug_reset"]("jtag0", {"halt": True}, {})
        debug_net.reset.assert_called_once_with(halt=True)
        assert data["halt"] is True

    @patch("lager.Net.get")
    def test_debug_erase(self, mock_get, debug_net):
        mock_get.return_value = debug_net
        data = _HANDLER_REGISTRY["debug_erase"]("jtag0", {}, {})
        debug_net.erase.assert_called_once()
        assert data["erased"] is True

    @patch("lager.Net.get")
    def test_debug_read_memory(self, mock_get, debug_net):
        mock_get.return_value = debug_net
        results = {}
        data = _HANDLER_REGISTRY["debug_read_memory"](
            "jtag0", {"address": 0x08000000, "length": 16, "label": "mem"}, results
        )
        debug_net.read_memory.assert_called_once_with(0x08000000, 16)
        assert results["mem"]["data"] == "DEADBEEF"


# ---------------------------------------------------------------------------
# RTT tests
# ---------------------------------------------------------------------------

class TestRTTHandlers:
    @pytest.fixture
    def rtt_mocks(self):
        rtt_session = MagicMock(name="rtt_session")
        rtt_session.read_some.return_value = b"hello from RTT\n"

        ctx_mgr = MagicMock()
        ctx_mgr.__enter__ = MagicMock(return_value=rtt_session)
        ctx_mgr.__exit__ = MagicMock(return_value=False)

        debug_net = MagicMock(name="debug_net")
        debug_net.rtt.return_value = ctx_mgr

        return {"debug": debug_net, "rtt": rtt_session, "ctx": ctx_mgr}

    @patch("lager.Net.get")
    def test_rtt_write(self, mock_get, rtt_mocks):
        mock_get.return_value = rtt_mocks["debug"]
        data = _HANDLER_REGISTRY["rtt_write"]("jtag0", {"data": "test\n"}, {})
        rtt_mocks["rtt"].write.assert_called_once_with(b"test\n")
        assert data["data"] == "test\n"

    @patch("lager.Net.get")
    def test_rtt_expect_match(self, mock_get, rtt_mocks):
        rtt_mocks["rtt"].read_some.return_value = b"sensor: 42\n"
        mock_get.return_value = rtt_mocks["debug"]
        results = {}
        data = _HANDLER_REGISTRY["rtt_expect"](
            "jtag0", {"pattern": "sensor: 42", "label": "reading", "timeout_ms": 1000}, results
        )
        assert data["matched"] is True
        assert results["reading"]["matched"] is True

    @patch("lager.Net.get")
    def test_rtt_expect_no_match(self, mock_get, rtt_mocks):
        rtt_mocks["rtt"].read_some.return_value = None
        mock_get.return_value = rtt_mocks["debug"]
        data = _HANDLER_REGISTRY["rtt_expect"](
            "jtag0", {"pattern": "never", "timeout_ms": 100}, {}
        )
        assert data["matched"] is False


# ---------------------------------------------------------------------------
# WattMeter / Thermocouple tests
# ---------------------------------------------------------------------------

class TestMeasurementHandlers:
    @patch("lager.Net.get")
    def test_watt_read(self, mock_get):
        watt = MagicMock()
        watt.read.return_value = 0.125
        mock_get.return_value = watt
        results = {}
        data = _HANDLER_REGISTRY["watt_read"]("watt0", {"label": "power"}, results)
        assert data["value"] == 0.125
        assert results["power"]["value"] == 0.125

    @patch("lager.Net.get")
    def test_watt_read_all(self, mock_get):
        watt = MagicMock()
        watt.read_all.return_value = {"voltage": 3.3, "current": 0.05, "power": 0.165}
        mock_get.return_value = watt
        results = {}
        data = _HANDLER_REGISTRY["watt_read_all"]("watt0", {"label": "readings"}, results)
        assert data["readings"]["voltage"] == 3.3
        assert results["readings"]["readings"]["power"] == 0.165

    @patch("lager.Net.get")
    def test_tc_read(self, mock_get):
        tc = MagicMock()
        tc.read.return_value = 25.4
        mock_get.return_value = tc
        results = {}
        data = _HANDLER_REGISTRY["tc_read"]("tc0", {"label": "temp"}, results)
        assert data["value"] == 25.4
        assert results["temp"]["value"] == 25.4


# ---------------------------------------------------------------------------
# Wait handler
# ---------------------------------------------------------------------------

class TestWaitHandler:
    def test_wait(self):
        t0 = time.time()
        data = _HANDLER_REGISTRY["wait"](None, {"ms": 50}, {})
        elapsed = time.time() - t0
        assert data == {"ms": 50}
        assert elapsed >= 0.04


# ---------------------------------------------------------------------------
# Step execution
# ---------------------------------------------------------------------------

class TestExecuteStep:
    def test_unknown_action_aborts(self):
        step = {"action": "nonexistent", "target": "x", "params": {}}
        results, step_results, errors = {}, [], []
        with pytest.raises(RuntimeError, match="Unknown action"):
            execute_step(step, results, step_results, errors)
        assert len(errors) == 1
        assert step_results[0]["success"] is False

    def test_unknown_action_continue(self):
        step = {"action": "nonexistent", "target": "x", "params": {}, "on_failure": "continue"}
        results, step_results, errors = {}, [], []
        execute_step(step, results, step_results, errors)
        assert len(errors) == 1
        assert step_results[0]["success"] is False

    def test_successful_step_records_duration(self):
        step = {"action": "wait", "params": {"ms": 10}}
        results, step_results, errors = {}, [], []
        execute_step(step, results, step_results, errors)
        assert len(step_results) == 1
        assert step_results[0]["success"] is True
        assert step_results[0]["duration_ms"] > 0


# ---------------------------------------------------------------------------
# Assertion evaluation
# ---------------------------------------------------------------------------

class TestEvaluateAssertions:
    def test_passing_assertion(self):
        results = {"led": {"value": 1}}
        assertions = [{"name": "led_on", "expression": "results['led']['value'] == 1"}]
        outcomes = evaluate_assertions(assertions, results)
        assert outcomes[0]["passed"] is True

    def test_failing_assertion(self):
        results = {"led": {"value": 0}}
        assertions = [{"name": "led_on", "expression": "results['led']['value'] == 1"}]
        outcomes = evaluate_assertions(assertions, results)
        assert outcomes[0]["passed"] is False

    def test_eval_error(self):
        outcomes = evaluate_assertions(
            [{"name": "bad", "expression": "undefined_var"}], {}
        )
        assert outcomes[0]["passed"] is False
        assert "eval error" in outcomes[0]["detail"]

    def test_severity_preserved(self):
        outcomes = evaluate_assertions(
            [{"name": "warn", "expression": "True", "severity": "warning"}], {}
        )
        assert outcomes[0]["severity"] == "warning"


# ---------------------------------------------------------------------------
# End-to-end run() tests
# ---------------------------------------------------------------------------

class TestRunFunction:
    @patch("lager.Net.get")
    def test_gpio_button_press_release(self, mock_get):
        """The proof scenario: actuate button (int levels), verify LED."""
        gpio = MagicMock()
        gpio.input.side_effect = [1, 0]
        mock_get.return_value = gpio

        scenario = {
            "name": "gpio_button_press_release",
            "steps": [
                {"action": "gpio_set", "target": "button0", "params": {"level": 1}},
                {"action": "wait", "params": {"ms": 10}},
                {"action": "gpio_read", "target": "led0", "params": {"label": "after_press"}},
                {"action": "gpio_set", "target": "button0", "params": {"level": 0}},
                {"action": "wait", "params": {"ms": 10}},
                {"action": "gpio_read", "target": "led0", "params": {"label": "after_release"}},
            ],
            "assertions": [
                {"name": "press_detected", "expression": "results['after_press']['value'] == 1"},
                {"name": "release_detected", "expression": "results['after_release']['value'] == 0"},
            ],
        }

        result = run(json.dumps(scenario))

        assert result["status"] == "passed"
        assert result["scenario_name"] == "gpio_button_press_release"
        assert len(result["step_results"]) == 6
        assert all(s["success"] for s in result["step_results"])
        assert result["assertions"][0]["passed"] is True
        assert result["assertions"][1]["passed"] is True
        assert result["results"]["after_press"]["value"] == 1
        assert result["results"]["after_release"]["value"] == 0

    @patch("lager.Net.get")
    def test_failed_assertion(self, mock_get):
        gpio = MagicMock()
        gpio.input.return_value = 0
        mock_get.return_value = gpio
        scenario = {
            "name": "fail_test",
            "steps": [
                {"action": "gpio_read", "target": "pin0", "params": {"label": "reading"}},
            ],
            "assertions": [
                {"name": "should_be_high", "expression": "results['reading']['value'] == 1"},
            ],
        }
        result = run(json.dumps(scenario))
        assert result["status"] == "failed"
        assert result["assertions"][0]["passed"] is False

    @patch("lager.Net.get")
    def test_step_error_aborts(self, mock_get):
        gpio = MagicMock()
        gpio.output.side_effect = RuntimeError("no such pin")
        mock_get.return_value = gpio
        scenario = {
            "name": "abort_test",
            "steps": [
                {"action": "gpio_set", "target": "bad_pin", "params": {"level": 1}},
                {"action": "wait", "params": {"ms": 10}},
            ],
        }
        result = run(json.dumps(scenario))
        assert result["status"] == "aborted"
        assert len(result["errors"]) > 0
        assert len(result["step_results"]) == 1

    @patch("lager.Net.get")
    def test_cleanup_runs_even_on_abort(self, mock_get):
        gpio = MagicMock()
        gpio.output.side_effect = RuntimeError("fail")
        mock_get.return_value = gpio
        scenario = {
            "name": "cleanup_test",
            "steps": [
                {"action": "gpio_set", "target": "bad", "params": {"level": 1}},
            ],
            "cleanup": [
                {"action": "wait", "params": {"ms": 1}},
            ],
        }
        result = run(json.dumps(scenario))
        assert result["status"] == "aborted"
        cleanup_steps = [s for s in result["step_results"] if s["action"] == "wait"]
        assert len(cleanup_steps) == 1

    def test_setup_phase(self):
        scenario = {
            "name": "setup_test",
            "setup": [
                {"action": "wait", "params": {"ms": 1}},
            ],
            "steps": [
                {"action": "wait", "params": {"ms": 1}},
            ],
        }
        result = run(json.dumps(scenario))
        assert result["status"] == "passed"
        assert len(result["step_results"]) == 2

    @patch("lager.Net.get")
    def test_scenario_timeout(self, mock_get):
        """Scenario-level timeout_s triggers 'timeout' status.

        The deadline check runs *between* steps, so we use two steps
        whose combined duration exceeds the budget.
        """
        scenario = {
            "name": "timeout_test",
            "timeout_s": 1,
            "steps": [
                {"action": "wait", "params": {"ms": 800}},
                {"action": "wait", "params": {"ms": 800}},
                {"action": "wait", "params": {"ms": 800}},
            ],
        }
        result = run(json.dumps(scenario))
        assert result["status"] == "timeout"


# ---------------------------------------------------------------------------
# Battery handler tests
# ---------------------------------------------------------------------------

class TestBatteryHandlers:
    @patch("lager.Net.get")
    def test_battery_enable(self, mock_get):
        batt = MagicMock()
        mock_get.return_value = batt
        data = _HANDLER_REGISTRY["battery_enable"]("bat1", {}, {})
        batt.enable.assert_called_once()
        assert data["enabled"] is True

    @patch("lager.Net.get")
    def test_battery_disable(self, mock_get):
        batt = MagicMock()
        mock_get.return_value = batt
        data = _HANDLER_REGISTRY["battery_disable"]("bat1", {}, {})
        batt.disable.assert_called_once()
        assert data["enabled"] is False

    @patch("lager.Net.get")
    def test_battery_soc_set(self, mock_get):
        batt = MagicMock()
        mock_get.return_value = batt
        data = _HANDLER_REGISTRY["battery_soc"]("bat1", {"value": 75.0}, {})
        batt.soc.assert_called_once_with(75.0)
        assert data["soc"] == 75.0

    @patch("lager.Net.get")
    def test_battery_soc_read(self, mock_get):
        batt = MagicMock()
        mock_get.return_value = batt
        results = {}
        data = _HANDLER_REGISTRY["battery_soc"]("bat1", {"label": "soc_reading"}, results)
        batt.soc.assert_called_once_with(None)
        assert "soc_reading" in results

    @patch("lager.Net.get")
    def test_battery_voc(self, mock_get):
        batt = MagicMock()
        mock_get.return_value = batt
        data = _HANDLER_REGISTRY["battery_voc"]("bat1", {"value": 4.2}, {})
        batt.voc.assert_called_once_with(4.2)
        assert data["voc"] == 4.2

    @patch("lager.Net.get")
    def test_battery_set(self, mock_get):
        batt = MagicMock()
        mock_get.return_value = batt
        data = _HANDLER_REGISTRY["battery_set"]("bat1", {}, {})
        batt.set_mode_battery.assert_called_once()

    @patch("lager.Net.get")
    def test_battery_state(self, mock_get):
        batt = MagicMock()
        batt.terminal_voltage.return_value = 3.7
        batt.current.return_value = 0.5
        batt.esr.return_value = 0.1
        mock_get.return_value = batt
        results = {}
        data = _HANDLER_REGISTRY["battery_state"]("bat1", {"label": "state"}, results)
        assert data["terminal_voltage"] == 3.7
        assert data["current"] == 0.5
        assert results["state"]["esr"] == 0.1


# ---------------------------------------------------------------------------
# ELoad handler tests
# ---------------------------------------------------------------------------

class TestELoadHandlers:
    @patch("lager.Net.get")
    def test_eload_set_cc(self, mock_get):
        eload = MagicMock()
        mock_get.return_value = eload
        data = _HANDLER_REGISTRY["eload_set"]("eload1", {"mode": "cc", "value": 1.5}, {})
        eload.current.assert_called_once_with(1.5)
        assert data["mode"] == "cc"
        assert data["current"] == 1.5

    @patch("lager.Net.get")
    def test_eload_set_cv(self, mock_get):
        eload = MagicMock()
        mock_get.return_value = eload
        data = _HANDLER_REGISTRY["eload_set"]("eload1", {"mode": "cv", "value": 5.0}, {})
        eload.voltage.assert_called_once_with(5.0)

    @patch("lager.Net.get")
    def test_eload_set_invalid_mode(self, mock_get):
        eload = MagicMock()
        mock_get.return_value = eload
        with pytest.raises(ValueError, match="Unknown eload mode"):
            _HANDLER_REGISTRY["eload_set"]("eload1", {"mode": "xx"}, {})

    @patch("lager.Net.get")
    def test_eload_enable(self, mock_get):
        eload = MagicMock()
        mock_get.return_value = eload
        data = _HANDLER_REGISTRY["eload_enable"]("eload1", {}, {})
        eload.enable.assert_called_once()
        assert data["enabled"] is True

    @patch("lager.Net.get")
    def test_eload_disable(self, mock_get):
        eload = MagicMock()
        mock_get.return_value = eload
        data = _HANDLER_REGISTRY["eload_disable"]("eload1", {}, {})
        eload.disable.assert_called_once()

    @patch("lager.Net.get")
    def test_eload_state(self, mock_get):
        eload = MagicMock()
        eload.measured_voltage.return_value = 5.0
        eload.measured_current.return_value = 1.0
        eload.measured_power.return_value = 5.0
        eload.mode.return_value = "cc"
        mock_get.return_value = eload
        results = {}
        data = _HANDLER_REGISTRY["eload_state"]("eload1", {"label": "state"}, results)
        assert data["measured_voltage"] == 5.0
        assert data["mode"] == "cc"
        assert results["state"]["measured_current"] == 1.0


# ---------------------------------------------------------------------------
# Energy handler tests
# ---------------------------------------------------------------------------

class TestEnergyHandlers:
    @patch("lager.Net.get")
    def test_energy_read(self, mock_get):
        ea = MagicMock()
        ea.read_energy.return_value = {"energy_j": 0.5, "charge_c": 0.15}
        mock_get.return_value = ea
        results = {}
        data = _HANDLER_REGISTRY["energy_read"]("energy1", {"duration": 2.0, "label": "e"}, results)
        ea.read_energy.assert_called_once_with(2.0)
        assert data["reading"]["energy_j"] == 0.5
        assert results["e"]["duration"] == 2.0

    @patch("lager.Net.get")
    def test_energy_stats(self, mock_get):
        ea = MagicMock()
        ea.read_stats.return_value = {"current": {"mean": 0.1}}
        mock_get.return_value = ea
        results = {}
        data = _HANDLER_REGISTRY["energy_stats"]("energy1", {"duration": 1.0, "label": "s"}, results)
        ea.read_stats.assert_called_once_with(1.0)
        assert data["stats"]["current"]["mean"] == 0.1


# ---------------------------------------------------------------------------
# Retry and timeout tests
# ---------------------------------------------------------------------------

class TestRetryAndTimeout:
    @patch("lager.Net.get")
    def test_max_retries_succeeds_on_second_try(self, mock_get):
        gpio = MagicMock()
        gpio.output.side_effect = [RuntimeError("flaky"), None]
        mock_get.return_value = gpio
        step = {
            "action": "gpio_set", "target": "pin0",
            "params": {"level": 1},
            "max_retries": 1,
        }
        results, step_results, errors = {}, [], []
        execute_step(step, results, step_results, errors)
        assert len(step_results) == 1
        assert step_results[0]["success"] is True
        assert step_results[0].get("attempt") == 2

    @patch("lager.Net.get")
    def test_max_retries_exhausted(self, mock_get):
        gpio = MagicMock()
        gpio.output.side_effect = RuntimeError("always fails")
        mock_get.return_value = gpio
        step = {
            "action": "gpio_set", "target": "pin0",
            "params": {"level": 1},
            "max_retries": 2, "on_failure": "continue",
        }
        results, step_results, errors = {}, [], []
        execute_step(step, results, step_results, errors)
        assert step_results[0]["success"] is False
        assert step_results[0]["attempts"] == 3

    def test_step_timeout_fires(self):
        step = {
            "action": "wait", "params": {"ms": 5000},
            "timeout_s": 1, "on_failure": "continue",
        }
        results, step_results, errors = {}, [], []
        t0 = time.time()
        execute_step(step, results, step_results, errors)
        elapsed = time.time() - t0
        assert step_results[0]["success"] is False
        assert "timed out" in step_results[0]["error"].lower()
        assert elapsed < 3.0


# ---------------------------------------------------------------------------
# Safe eval tests
# ---------------------------------------------------------------------------

class TestSafeEval:
    def test_simple_comparison(self):
        from lager.mcp.engine.scenario_runner import _safe_eval
        assert _safe_eval("results['v'] == 3.3", {"v": 3.3}) is True

    def test_nested_access(self):
        from lager.mcp.engine.scenario_runner import _safe_eval
        assert _safe_eval("results['a']['b'] > 1", {"a": {"b": 5}}) is True

    def test_boolean_and(self):
        from lager.mcp.engine.scenario_runner import _safe_eval
        assert _safe_eval("results['x'] > 0 and results['x'] < 10", {"x": 5}) is True

    def test_boolean_or(self):
        from lager.mcp.engine.scenario_runner import _safe_eval
        assert _safe_eval("results['x'] < 0 or results['x'] > 10", {"x": 5}) is False

    def test_not(self):
        from lager.mcp.engine.scenario_runner import _safe_eval
        assert _safe_eval("not results['flag']", {"flag": False}) is True

    def test_builtin_abs(self):
        from lager.mcp.engine.scenario_runner import _safe_eval
        assert _safe_eval("abs(results['v']) < 0.1", {"v": -0.05}) is True

    def test_builtin_len(self):
        from lager.mcp.engine.scenario_runner import _safe_eval
        assert _safe_eval("len(results['items']) == 3", {"items": [1, 2, 3]}) is True

    def test_in_operator(self):
        from lager.mcp.engine.scenario_runner import _safe_eval
        assert _safe_eval("'ok' in results['msg']", {"msg": "all ok"}) is True

    def test_rejects_import(self):
        from lager.mcp.engine.scenario_runner import _safe_eval
        with pytest.raises(Exception):
            _safe_eval("__import__('os').system('echo pwned')", {})

    def test_rejects_arbitrary_names(self):
        from lager.mcp.engine.scenario_runner import _safe_eval
        with pytest.raises(NameError):
            _safe_eval("open('/etc/passwd')", {})

    def test_literal_true(self):
        from lager.mcp.engine.scenario_runner import _safe_eval
        assert _safe_eval("True", {}) is True

    def test_undefined_var_raises(self):
        from lager.mcp.engine.scenario_runner import _safe_eval
        with pytest.raises(NameError):
            _safe_eval("undefined_var", {})
