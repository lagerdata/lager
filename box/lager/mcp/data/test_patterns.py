# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
Index of test patterns mapped to real example scripts in test/api/.

Used by the ``get_test_example`` MCP tool to return proven, runnable
examples to agents.
"""

from __future__ import annotations

import os
from typing import Any

_TEST_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "test", "api")

TEST_PATTERNS: dict[str, dict[str, Any]] = {
    # ── Power ──────────────────────────────────────────────────────────
    "power_supply": {
        "description": "Comprehensive power supply control and verification",
        "net_types": ["PowerSupply"],
        "script": "power/test_supply_comprehensive.py",
        "tags": ["power", "supply", "voltage", "current"],
    },
    "battery_simulator": {
        "description": "Battery simulator SOC, VOC, and discharge testing",
        "net_types": ["Battery"],
        "script": "power/test_battery_comprehensive.py",
        "tags": ["power", "battery", "soc"],
    },
    "electronic_load": {
        "description": "Electronic load modes (CC, CV, CR, CP) and measurement",
        "net_types": ["ELoad"],
        "script": "power/test_eload_comprehensive.py",
        "tags": ["power", "eload", "load"],
    },
    "solar_simulator": {
        "description": "Solar array simulator control",
        "net_types": ["PowerSupply"],
        "script": "power/test_solar_comprehensive.py",
        "tags": ["power", "solar"],
    },

    # ── Communication ──────────────────────────────────────────────────
    "uart_loopback": {
        "description": "UART communication: connect, send, receive, loopback",
        "net_types": ["UART"],
        "script": "communication/test_uart_comprehensive.py",
        "tags": ["uart", "serial", "communication"],
    },
    "spi_api": {
        "description": "SPI bus: config, read, write, full-duplex transfer",
        "net_types": ["SPI"],
        "script": "communication/test_spi_api.py",
        "tags": ["spi", "protocol", "flash"],
    },
    "spi_flash_readback": {
        "description": "SPI write and readback verification",
        "net_types": ["SPI"],
        "script": "communication/test_spi_write_readback.py",
        "tags": ["spi", "flash", "validation"],
    },
    "i2c_aardvark": {
        "description": "I2C bus: scan, read, write via Aardvark adapter",
        "net_types": ["I2C"],
        "script": "communication/test_i2c_aardvark_api.py",
        "tags": ["i2c", "protocol", "sensor"],
    },
    "i2c_labjack": {
        "description": "I2C bus via LabJack T7",
        "net_types": ["I2C"],
        "script": "communication/test_i2c_labjack_api.py",
        "tags": ["i2c", "labjack"],
    },
    "debug_flash_and_boot": {
        "description": "Flash firmware, reset, verify boot via debug probe",
        "net_types": ["Debug"],
        "script": "communication/test_debug_comprehensive.py",
        "tags": ["debug", "flash", "firmware", "boot"],
    },
    "ble_scan": {
        "description": "BLE device scanning and connection",
        "net_types": ["BLE"],
        "script": "communication/test_ble_comprehensive.py",
        "tags": ["ble", "bluetooth", "wireless"],
    },

    # ── I/O ────────────────────────────────────────────────────────────
    "gpio_output": {
        "description": "GPIO output: drive pins high/low",
        "net_types": ["GPIO"],
        "script": "io/test_gpio_output.py",
        "tags": ["gpio", "digital", "output"],
    },
    "gpio_input": {
        "description": "GPIO input: read pin levels",
        "net_types": ["GPIO"],
        "script": "io/test_gpio_input.py",
        "tags": ["gpio", "digital", "input"],
    },
    "gpio_wait_for_level": {
        "description": "GPIO wait_for_level: block until pin changes state",
        "net_types": ["GPIO"],
        "script": "communication/test_wait_for_level.py",
        "tags": ["gpio", "wait", "interrupt"],
    },
    "adc_single": {
        "description": "ADC: single-channel voltage reading",
        "net_types": ["ADC"],
        "script": "io/test_adc_single.py",
        "tags": ["adc", "analog", "measurement"],
    },
    "adc_continuous": {
        "description": "ADC: continuous multi-sample reading",
        "net_types": ["ADC"],
        "script": "io/test_adc_continuous.py",
        "tags": ["adc", "analog", "streaming"],
    },
    "dac_output": {
        "description": "DAC: set analog output voltage",
        "net_types": ["DAC"],
        "script": "io/test_dac_output.py",
        "tags": ["dac", "analog", "output"],
    },
    "dac_adc_loopback": {
        "description": "DAC→ADC loopback: set output, read back with ADC",
        "net_types": ["DAC", "ADC"],
        "script": "io/test_dac_adc_loopback.py",
        "tags": ["dac", "adc", "loopback", "analog"],
    },

    # ── Sensors ────────────────────────────────────────────────────────
    "thermocouple": {
        "description": "Temperature measurement via thermocouple",
        "net_types": ["Thermocouple"],
        "script": "sensors/test_thermocouple_single.py",
        "tags": ["temperature", "thermocouple", "sensor"],
    },
    "watt_meter": {
        "description": "Power measurement (voltage, current, watts)",
        "net_types": ["WattMeter"],
        "script": "sensors/test_watt_meter.py",
        "tags": ["power", "watt", "measurement"],
    },
    "energy_analyzer": {
        "description": "Energy integration over time",
        "net_types": ["EnergyAnalyzer"],
        "script": "sensors/test_energy_analyzer.py",
        "tags": ["energy", "power", "measurement"],
    },

    # ── Peripherals ────────────────────────────────────────────────────
    "usb_power_cycle": {
        "description": "USB hub port enable/disable for power cycling",
        "net_types": ["Usb"],
        "script": "usb/test_usb_power_cycle.py",
        "tags": ["usb", "power-cycle", "reset"],
    },
    "oscilloscope": {
        "description": "Oscilloscope measurement and triggering",
        "net_types": ["Scope"],
        "script": "peripherals/test_scope_basic.py",
        "tags": ["scope", "oscilloscope", "waveform"],
    },
}


def find_pattern(query: str) -> list[dict[str, Any]]:
    """Find test patterns matching a query string.

    Searches pattern keys, descriptions, net_types, and tags.
    Returns matching patterns sorted by relevance.
    """
    query_lower = query.lower()
    results = []
    for key, pattern in TEST_PATTERNS.items():
        score = 0
        if query_lower in key:
            score += 10
        if query_lower in pattern["description"].lower():
            score += 5
        for tag in pattern.get("tags", []):
            if query_lower in tag:
                score += 3
        for nt in pattern.get("net_types", []):
            if query_lower in nt.lower():
                score += 3
        if score > 0:
            results.append({**pattern, "pattern_key": key, "score": score})
    results.sort(key=lambda r: r["score"], reverse=True)
    return results


def get_script_content(script_path: str) -> str | None:
    """Read the content of a test script from the test/api/ directory."""
    full_path = os.path.join(_TEST_DIR, script_path)
    try:
        with open(full_path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None
