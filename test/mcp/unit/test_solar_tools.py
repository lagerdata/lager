# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for MCP solar simulator tools (lager.mcp.tools.solar)."""

import json
from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.unit
class TestSolarTools:
    """Verify each solar simulator tool calls SolarDispatcher and driver APIs."""

    @staticmethod
    def _mock_dispatcher_chain(mock_dispatcher_class, mock_drv):
        dispatcher = MagicMock()
        dispatcher.resolve_driver.return_value = mock_drv
        mock_dispatcher_class.return_value = dispatcher
        return dispatcher

    # -- set / stop ------------------------------------------------------

    @patch("lager.power.solar.dispatcher.SolarDispatcher")
    def test_set(self, mock_dispatcher_class):
        mock_drv = MagicMock()
        self._mock_dispatcher_chain(mock_dispatcher_class, mock_drv)
        from lager.mcp.tools.solar import solar_set

        result = json.loads(solar_set(net="solar1"))
        mock_dispatcher_class.assert_called()
        dispatcher = mock_dispatcher_class.return_value
        dispatcher.resolve_driver.assert_called_with("solar1")
        mock_drv.connect_instrument.assert_called_once_with()
        assert result["status"] == "ok"
        assert result["net"] == "solar1"
        assert result["action"] == "set_solar_mode"

    @patch("lager.power.solar.dispatcher.SolarDispatcher")
    def test_stop(self, mock_dispatcher_class):
        mock_drv = MagicMock()
        self._mock_dispatcher_chain(mock_dispatcher_class, mock_drv)
        from lager.mcp.tools.solar import solar_stop

        result = json.loads(solar_stop(net="solar1"))
        dispatcher = mock_dispatcher_class.return_value
        dispatcher.resolve_driver.assert_called_with("solar1")
        mock_drv.disconnect_instrument.assert_called_once_with()
        assert result["status"] == "ok"
        assert result["net"] == "solar1"
        assert result["action"] == "stop"

    # -- irradiance ------------------------------------------------------

    @patch("lager.power.solar.dispatcher.SolarDispatcher")
    def test_irradiance_read(self, mock_dispatcher_class):
        mock_drv = MagicMock()
        mock_drv.irradiance.return_value = " 950.0 "
        self._mock_dispatcher_chain(mock_dispatcher_class, mock_drv)
        from lager.mcp.tools.solar import solar_irradiance

        result = json.loads(solar_irradiance(net="solar1"))
        mock_drv.irradiance.assert_called_once_with(value=None)
        assert result["status"] == "ok"
        assert result["net"] == "solar1"
        assert result["irradiance"] == "950.0"

    @patch("lager.power.solar.dispatcher.SolarDispatcher")
    def test_irradiance_set(self, mock_dispatcher_class):
        mock_drv = MagicMock()
        self._mock_dispatcher_chain(mock_dispatcher_class, mock_drv)
        from lager.mcp.tools.solar import solar_irradiance

        result = json.loads(solar_irradiance(net="solar1", value=1000.0))
        mock_drv.irradiance.assert_called_once_with(value=1000.0)
        assert result["irradiance"] == 1000.0

    # -- mpp_current / mpp_voltage (read-only) ---------------------------

    @patch("lager.power.solar.dispatcher.SolarDispatcher")
    def test_mpp_current(self, mock_dispatcher_class):
        mock_drv = MagicMock()
        mock_drv.mpp_current.return_value = 1.23
        self._mock_dispatcher_chain(mock_dispatcher_class, mock_drv)
        from lager.mcp.tools.solar import solar_mpp_current

        result = json.loads(solar_mpp_current(net="solar1"))
        mock_drv.mpp_current.assert_called_once_with()
        assert result["mpp_current"] == 1.23

    @patch("lager.power.solar.dispatcher.SolarDispatcher")
    def test_mpp_voltage(self, mock_dispatcher_class):
        mock_drv = MagicMock()
        mock_drv.mpp_voltage.return_value = 18.7
        self._mock_dispatcher_chain(mock_dispatcher_class, mock_drv)
        from lager.mcp.tools.solar import solar_mpp_voltage

        result = json.loads(solar_mpp_voltage(net="solar1"))
        mock_drv.mpp_voltage.assert_called_once_with()
        assert result["mpp_voltage"] == 18.7

    # -- resistance ------------------------------------------------------

    @patch("lager.power.solar.dispatcher.SolarDispatcher")
    def test_resistance_read(self, mock_dispatcher_class):
        mock_drv = MagicMock()
        mock_drv.resistance.return_value = " 0.42 "
        self._mock_dispatcher_chain(mock_dispatcher_class, mock_drv)
        from lager.mcp.tools.solar import solar_resistance

        result = json.loads(solar_resistance(net="solar1"))
        mock_drv.resistance.assert_called_once_with(value=None)
        assert result["resistance"] == "0.42"

    @patch("lager.power.solar.dispatcher.SolarDispatcher")
    def test_resistance_set(self, mock_dispatcher_class):
        mock_drv = MagicMock()
        self._mock_dispatcher_chain(mock_dispatcher_class, mock_drv)
        from lager.mcp.tools.solar import solar_resistance

        result = json.loads(solar_resistance(net="solar1", value=0.5))
        mock_drv.resistance.assert_called_once_with(0.5)
        assert result["resistance"] == 0.5

    # -- temperature (read-only) -----------------------------------------

    @patch("lager.power.solar.dispatcher.SolarDispatcher")
    def test_temperature(self, mock_dispatcher_class):
        mock_drv = MagicMock()
        mock_drv.temperature.return_value = 25.0
        self._mock_dispatcher_chain(mock_dispatcher_class, mock_drv)
        from lager.mcp.tools.solar import solar_temperature

        result = json.loads(solar_temperature(net="solar1"))
        mock_drv.temperature.assert_called_once_with()
        assert result["temperature"] == 25.0

    # -- voc -------------------------------------------------------------

    @patch("lager.power.solar.dispatcher.SolarDispatcher")
    def test_voc_read(self, mock_dispatcher_class):
        mock_drv = MagicMock()
        mock_drv.voc.return_value = 21.5
        self._mock_dispatcher_chain(mock_dispatcher_class, mock_drv)
        from lager.mcp.tools.solar import solar_voc

        result = json.loads(solar_voc(net="solar1"))
        mock_drv.voc.assert_called_once_with()
        assert result["voc"] == 21.5

    # -- dispatcher / driver errors --------------------------------------

    @patch("lager.power.solar.dispatcher.SolarDispatcher")
    def test_set_resolve_failure(self, mock_dispatcher_class):
        dispatcher = MagicMock()
        dispatcher.resolve_driver.side_effect = RuntimeError("device not found")
        mock_dispatcher_class.return_value = dispatcher
        from lager.mcp.tools.solar import solar_set

        with pytest.raises(RuntimeError, match="device not found"):
            solar_set(net="solar1")

    @patch("lager.power.solar.dispatcher.SolarDispatcher")
    def test_stop_resolve_failure(self, mock_dispatcher_class):
        dispatcher = MagicMock()
        dispatcher.resolve_driver.side_effect = RuntimeError("device not found")
        mock_dispatcher_class.return_value = dispatcher
        from lager.mcp.tools.solar import solar_stop

        with pytest.raises(RuntimeError, match="device not found"):
            solar_stop(net="solar1")

    @patch("lager.power.solar.dispatcher.SolarDispatcher")
    def test_irradiance_resolve_failure(self, mock_dispatcher_class):
        dispatcher = MagicMock()
        dispatcher.resolve_driver.side_effect = RuntimeError("device not found")
        mock_dispatcher_class.return_value = dispatcher
        from lager.mcp.tools.solar import solar_irradiance

        with pytest.raises(RuntimeError, match="device not found"):
            solar_irradiance(net="solar1")

    @patch("lager.power.solar.dispatcher.SolarDispatcher")
    def test_mpp_current_resolve_failure(self, mock_dispatcher_class):
        dispatcher = MagicMock()
        dispatcher.resolve_driver.side_effect = RuntimeError("device not found")
        mock_dispatcher_class.return_value = dispatcher
        from lager.mcp.tools.solar import solar_mpp_current

        with pytest.raises(RuntimeError, match="device not found"):
            solar_mpp_current(net="solar1")

    @patch("lager.power.solar.dispatcher.SolarDispatcher")
    def test_mpp_voltage_resolve_failure(self, mock_dispatcher_class):
        dispatcher = MagicMock()
        dispatcher.resolve_driver.side_effect = RuntimeError("device not found")
        mock_dispatcher_class.return_value = dispatcher
        from lager.mcp.tools.solar import solar_mpp_voltage

        with pytest.raises(RuntimeError, match="device not found"):
            solar_mpp_voltage(net="solar1")

    @patch("lager.power.solar.dispatcher.SolarDispatcher")
    def test_resistance_resolve_failure(self, mock_dispatcher_class):
        dispatcher = MagicMock()
        dispatcher.resolve_driver.side_effect = RuntimeError("device not found")
        mock_dispatcher_class.return_value = dispatcher
        from lager.mcp.tools.solar import solar_resistance

        with pytest.raises(RuntimeError, match="device not found"):
            solar_resistance(net="solar1")

    @patch("lager.power.solar.dispatcher.SolarDispatcher")
    def test_temperature_resolve_failure(self, mock_dispatcher_class):
        dispatcher = MagicMock()
        dispatcher.resolve_driver.side_effect = RuntimeError("device not found")
        mock_dispatcher_class.return_value = dispatcher
        from lager.mcp.tools.solar import solar_temperature

        with pytest.raises(RuntimeError, match="device not found"):
            solar_temperature(net="solar1")

    @patch("lager.power.solar.dispatcher.SolarDispatcher")
    def test_voc_resolve_failure(self, mock_dispatcher_class):
        dispatcher = MagicMock()
        dispatcher.resolve_driver.side_effect = RuntimeError("device not found")
        mock_dispatcher_class.return_value = dispatcher
        from lager.mcp.tools.solar import solar_voc

        with pytest.raises(RuntimeError, match="device not found"):
            solar_voc(net="solar1")
