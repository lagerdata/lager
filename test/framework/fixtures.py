#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Reusable pytest fixtures for Lager hardware tests.

This module provides pytest fixtures for common hardware test scenarios including:
- Box connection validation
- Net provisioning (supply, battery, adc, etc.)
- Hardware cache management
- Automatic cleanup on test completion

Example usage in a pytest test file:
    from test.framework.fixtures import (
        hardware_cache,
        box_connection,
        supply_net,
        battery_net,
    )

    def test_supply_voltage(supply_net):
        supply_net.set_voltage(3.3)
        supply_net.enable()
        assert 3.0 < supply_net.voltage() < 3.6
        # Fixture automatically disables on cleanup

Note: These fixtures are designed to be imported into conftest.py files
or directly into test modules. They assume the lager box libraries are
available in the Python path (i.e., running on a Lager box or via
`lager python`).
"""

from __future__ import annotations

import os
import pytest
from typing import Any, Generator, Optional, Dict, List

from .test_utils import (
    clear_hardware_cache,
    check_box_connectivity,
    check_hardware_service,
    validate_net_exists,
    safe_disable_output,
)

# =============================================================================
# Configuration
# =============================================================================

# Environment variables for test configuration
DEFAULT_SUPPLY_NET = os.environ.get("LAGER_TEST_SUPPLY_NET", "psu1")
DEFAULT_BATTERY_NET = os.environ.get("LAGER_TEST_BATTERY_NET", "battery1")
DEFAULT_SOLAR_NET = os.environ.get("LAGER_TEST_SOLAR_NET", "solar1")
DEFAULT_ELOAD_NET = os.environ.get("LAGER_TEST_ELOAD_NET", "eload1")
DEFAULT_ADC_NET = os.environ.get("LAGER_TEST_ADC_NET", "adc1")
DEFAULT_DAC_NET = os.environ.get("LAGER_TEST_DAC_NET", "dac1")
DEFAULT_GPIO_NET = os.environ.get("LAGER_TEST_GPIO_NET", "gpio1")
DEFAULT_USB_NET = os.environ.get("LAGER_TEST_USB_NET", "usb1")
DEFAULT_SCOPE_NET = os.environ.get("LAGER_TEST_SCOPE_NET", "scope1")
DEFAULT_THERMOCOUPLE_NET = os.environ.get("LAGER_TEST_THERMOCOUPLE_NET", "tc1")
DEFAULT_WATT_NET = os.environ.get("LAGER_TEST_WATT_NET", "watt1")


# =============================================================================
# Session-Scoped Fixtures (shared across all tests)
# =============================================================================


@pytest.fixture(scope="session")
def lager_imports():
    """Import lager modules at session start.

    Returns a dict with commonly used lager imports, allowing tests to
    access them without direct import statements (useful when running
    outside of box context).
    """
    try:
        from lager import Net, NetType
        return {
            "Net": Net,
            "NetType": NetType,
        }
    except ImportError as e:
        pytest.skip(f"Lager modules not available: {e}")


@pytest.fixture(scope="session")
def box_connection():
    """Verify box connectivity at session start.

    Skips all tests if box is not reachable.
    """
    if not check_box_connectivity(verbose=True):
        pytest.skip("Box not reachable - skipping hardware tests")
    return True


@pytest.fixture(scope="session")
def hardware_service():
    """Verify hardware service is running at session start.

    Skips all tests if hardware service is not reachable.
    """
    if not check_hardware_service(verbose=True):
        pytest.skip("Hardware service not reachable - skipping hardware tests")
    return True


# =============================================================================
# Module-Scoped Fixtures (shared across tests in a module)
# =============================================================================


@pytest.fixture(scope="module")
def hardware_cache(hardware_service):
    """Clear hardware cache at module start and end.

    This ensures test isolation between modules by clearing cached
    device connections before and after the module runs.
    """
    clear_hardware_cache(verbose=True)
    yield
    clear_hardware_cache(verbose=True)


# =============================================================================
# Power Net Fixtures
# =============================================================================


@pytest.fixture
def supply_net(lager_imports, hardware_service):
    """Provide a power supply Net object with automatic cleanup.

    The fixture automatically disables the supply output on cleanup,
    even if the test fails.

    Yields:
        Net: Power supply Net object
    """
    Net = lager_imports["Net"]
    NetType = lager_imports["NetType"]

    net_name = DEFAULT_SUPPLY_NET
    if not validate_net_exists(net_name, role="power-supply", verbose=True):
        pytest.skip(f"Supply net '{net_name}' not configured")

    supply = Net.get(net_name, type=NetType.PowerSupply)
    yield supply

    # Cleanup: disable output
    safe_disable_output(supply, verbose=True)


@pytest.fixture
def supply_net_enabled(supply_net):
    """Provide a power supply Net that is enabled before the test.

    Sets a safe default voltage (3.3V, 0.1A) and enables output.

    Yields:
        Net: Enabled power supply Net object
    """
    supply_net.set_voltage(3.3)
    supply_net.set_current(0.1)
    supply_net.enable()
    yield supply_net
    # supply_net fixture handles disable


@pytest.fixture
def battery_net(lager_imports, hardware_service):
    """Provide a battery simulator Net object with automatic cleanup.

    Yields:
        Net: Battery simulator Net object
    """
    Net = lager_imports["Net"]
    NetType = lager_imports["NetType"]

    net_name = DEFAULT_BATTERY_NET
    if not validate_net_exists(net_name, role="battery", verbose=True):
        pytest.skip(f"Battery net '{net_name}' not configured")

    battery = Net.get(net_name, type=NetType.Battery)
    yield battery

    # Cleanup: disable output
    safe_disable_output(battery, verbose=True)


@pytest.fixture
def solar_net(lager_imports, hardware_service):
    """Provide a solar simulator Net object with automatic cleanup.

    Yields:
        Net: Solar simulator Net object
    """
    Net = lager_imports["Net"]
    NetType = lager_imports["NetType"]

    net_name = DEFAULT_SOLAR_NET
    if not validate_net_exists(net_name, role="solar", verbose=True):
        pytest.skip(f"Solar net '{net_name}' not configured")

    solar = Net.get(net_name, type=NetType.Solar)
    yield solar

    # Cleanup: disable output
    safe_disable_output(solar, verbose=True)


@pytest.fixture
def eload_net(lager_imports, hardware_service):
    """Provide an electronic load Net object with automatic cleanup.

    Yields:
        Net: Electronic load Net object
    """
    Net = lager_imports["Net"]
    NetType = lager_imports["NetType"]

    net_name = DEFAULT_ELOAD_NET
    if not validate_net_exists(net_name, role="eload", verbose=True):
        pytest.skip(f"Eload net '{net_name}' not configured")

    eload = Net.get(net_name, type=NetType.Eload)
    yield eload

    # Cleanup: disable input
    safe_disable_output(eload, verbose=True)


# =============================================================================
# I/O Net Fixtures
# =============================================================================


@pytest.fixture
def adc_net(lager_imports, hardware_service):
    """Provide an ADC Net object.

    Yields:
        Net: ADC Net object
    """
    Net = lager_imports["Net"]
    NetType = lager_imports["NetType"]

    net_name = DEFAULT_ADC_NET
    if not validate_net_exists(net_name, role="analog", verbose=True):
        pytest.skip(f"ADC net '{net_name}' not configured")

    adc = Net.get(net_name, type=NetType.Analog)
    yield adc


@pytest.fixture
def dac_net(lager_imports, hardware_service):
    """Provide a DAC Net object.

    Yields:
        Net: DAC Net object
    """
    Net = lager_imports["Net"]
    NetType = lager_imports["NetType"]

    net_name = DEFAULT_DAC_NET
    if not validate_net_exists(net_name, role="analog-out", verbose=True):
        pytest.skip(f"DAC net '{net_name}' not configured")

    dac = Net.get(net_name, type=NetType.AnalogOut)
    yield dac


@pytest.fixture
def gpio_input_net(lager_imports, hardware_service):
    """Provide a GPIO input Net object.

    Yields:
        Net: GPIO input Net object
    """
    Net = lager_imports["Net"]
    NetType = lager_imports["NetType"]

    net_name = DEFAULT_GPIO_NET
    if not validate_net_exists(net_name, verbose=True):
        pytest.skip(f"GPIO net '{net_name}' not configured")

    gpio = Net.get(net_name, type=NetType.Logic)
    yield gpio


@pytest.fixture
def gpio_output_net(lager_imports, hardware_service):
    """Provide a GPIO output Net object.

    Yields:
        Net: GPIO output Net object
    """
    Net = lager_imports["Net"]
    NetType = lager_imports["NetType"]

    net_name = DEFAULT_GPIO_NET
    if not validate_net_exists(net_name, verbose=True):
        pytest.skip(f"GPIO net '{net_name}' not configured")

    gpio = Net.get(net_name, type=NetType.DigitalOutput)
    yield gpio


# =============================================================================
# Measurement Net Fixtures
# =============================================================================


@pytest.fixture
def scope_net(lager_imports, hardware_service):
    """Provide an oscilloscope Net object.

    Yields:
        Net: Oscilloscope Net object
    """
    Net = lager_imports["Net"]
    NetType = lager_imports["NetType"]

    net_name = DEFAULT_SCOPE_NET
    if not validate_net_exists(net_name, role="scope", verbose=True):
        pytest.skip(f"Scope net '{net_name}' not configured")

    scope = Net.get(net_name, type=NetType.Scope)
    yield scope


@pytest.fixture
def thermocouple_net(lager_imports, hardware_service):
    """Provide a thermocouple Net object.

    Yields:
        Net: Thermocouple Net object
    """
    Net = lager_imports["Net"]
    NetType = lager_imports["NetType"]

    net_name = DEFAULT_THERMOCOUPLE_NET
    if not validate_net_exists(net_name, role="thermocouple", verbose=True):
        pytest.skip(f"Thermocouple net '{net_name}' not configured")

    tc = Net.get(net_name, type=NetType.Thermocouple)
    yield tc


@pytest.fixture
def watt_net(lager_imports, hardware_service):
    """Provide a watt meter Net object.

    Yields:
        Net: Watt meter Net object
    """
    Net = lager_imports["Net"]
    NetType = lager_imports["NetType"]

    net_name = DEFAULT_WATT_NET
    if not validate_net_exists(net_name, role="watt", verbose=True):
        pytest.skip(f"Watt net '{net_name}' not configured")

    watt = Net.get(net_name, type=NetType.Watt)
    yield watt


# =============================================================================
# USB/Automation Net Fixtures
# =============================================================================


@pytest.fixture
def usb_net(lager_imports, hardware_service):
    """Provide a USB hub port Net object with automatic cleanup.

    Yields:
        Net: USB hub port Net object
    """
    Net = lager_imports["Net"]
    NetType = lager_imports["NetType"]

    net_name = DEFAULT_USB_NET
    if not validate_net_exists(net_name, role="usb", verbose=True):
        pytest.skip(f"USB net '{net_name}' not configured")

    usb = Net.get(net_name, type=NetType.Usb)
    yield usb


# =============================================================================
# Parameterized Fixture Factories
# =============================================================================


def create_net_fixture(
    net_name: str,
    net_type_name: str,
    role: Optional[str] = None,
    cleanup_disable: bool = True
):
    """Factory for creating custom net fixtures.

    Use this to create fixtures for nets not covered by the defaults.

    Args:
        net_name: Name of the net to provision
        net_type_name: NetType enum name (e.g., "PowerSupply", "Battery")
        role: Optional role to validate
        cleanup_disable: Whether to disable output on cleanup

    Returns:
        A pytest fixture function

    Example:
        my_supply = create_net_fixture("my-psu", "PowerSupply", role="power-supply")

        def test_my_supply(my_supply):
            my_supply.set_voltage(5.0)
    """
    @pytest.fixture
    def net_fixture(lager_imports, hardware_service):
        Net = lager_imports["Net"]
        NetType = lager_imports["NetType"]

        if role and not validate_net_exists(net_name, role=role, verbose=True):
            pytest.skip(f"Net '{net_name}' with role '{role}' not configured")
        elif not validate_net_exists(net_name, verbose=True):
            pytest.skip(f"Net '{net_name}' not configured")

        net_type = getattr(NetType, net_type_name)
        net = Net.get(net_name, type=net_type)
        yield net

        if cleanup_disable:
            safe_disable_output(net, verbose=True)

    return net_fixture


# =============================================================================
# Utility Fixtures
# =============================================================================


@pytest.fixture
def cleared_cache(hardware_service):
    """Clear hardware cache before and after the test.

    Use this when you need cache isolation for a specific test.
    """
    clear_hardware_cache(verbose=True)
    yield
    clear_hardware_cache(verbose=True)


@pytest.fixture
def test_voltages() -> List[float]:
    """Common test voltage values."""
    return [1.8, 3.3, 5.0, 12.0]


@pytest.fixture
def test_currents() -> List[float]:
    """Common test current values."""
    return [0.1, 0.5, 1.0, 2.0]


@pytest.fixture
def test_soc_levels() -> List[int]:
    """Common battery SOC test levels."""
    return [100, 75, 50, 25, 10]


# =============================================================================
# Exports
# =============================================================================

__all__ = [
    # Configuration
    "DEFAULT_SUPPLY_NET",
    "DEFAULT_BATTERY_NET",
    "DEFAULT_SOLAR_NET",
    "DEFAULT_ELOAD_NET",
    "DEFAULT_ADC_NET",
    "DEFAULT_DAC_NET",
    "DEFAULT_GPIO_NET",
    "DEFAULT_USB_NET",
    "DEFAULT_SCOPE_NET",
    "DEFAULT_THERMOCOUPLE_NET",
    "DEFAULT_WATT_NET",
    # Session fixtures
    "lager_imports",
    "box_connection",
    "hardware_service",
    # Module fixtures
    "hardware_cache",
    # Power fixtures
    "supply_net",
    "supply_net_enabled",
    "battery_net",
    "solar_net",
    "eload_net",
    # I/O fixtures
    "adc_net",
    "dac_net",
    "gpio_input_net",
    "gpio_output_net",
    # Measurement fixtures
    "scope_net",
    "thermocouple_net",
    "watt_net",
    # USB/Automation fixtures
    "usb_net",
    # Factory
    "create_net_fixture",
    # Utility fixtures
    "cleared_cache",
    "test_voltages",
    "test_currents",
    "test_soc_levels",
]
