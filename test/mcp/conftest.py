# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Shared fixtures, marks, and config for MCP server tests."""

import sys
from pathlib import Path

_box_dir = str(Path(__file__).resolve().parents[2] / "box")
if _box_dir not in sys.path:
    sys.path.insert(0, _box_dir)

import pytest
from unittest.mock import patch, MagicMock


# --- Marks ---
def pytest_configure(config):
    config.addinivalue_line("markers", "unit: unit tests (mocked subprocess)")
    config.addinivalue_line("markers", "integration: live integration tests")
    config.addinivalue_line("markers", "power: power supply tests")
    config.addinivalue_line("markers", "battery: battery simulator tests")
    config.addinivalue_line("markers", "eload: electronic load tests")
    config.addinivalue_line("markers", "i2c: I2C communication tests")
    config.addinivalue_line("markers", "spi: SPI communication tests")
    config.addinivalue_line("markers", "measurement: ADC/DAC/GPIO/watt tests")
    config.addinivalue_line("markers", "usb: USB hub tests")
    config.addinivalue_line("markers", "box: box management tests")
    config.addinivalue_line("markers", "defaults: CLI defaults tests")


# --- CLI Options ---
def pytest_addoption(parser):
    parser.addoption("--box1", default=None, help="First test box name")
    parser.addoption("--box3", default=None, help="Second test box name")


@pytest.fixture
def box1(request):
    value = request.config.getoption("--box1")
    if value is None:
        pytest.skip("--box1 not provided")
    return value


@pytest.fixture
def box3(request):
    value = request.config.getoption("--box3")
    if value is None:
        pytest.skip("--box3 not provided")
    return value


# --- Mock Fixtures ---
@pytest.fixture
def mock_subprocess():
    """Patch subprocess.run and return the mock for assertion."""
    with patch("lager.mcp.server.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="mock output",
            stderr="",
        )
        yield mock_run


def assert_lager_called_with(mock_run, *expected_args):
    """Helper to verify run_lager built the correct CLI command."""
    mock_run.assert_called_once()
    actual_cmd = mock_run.call_args[0][0]  # positional arg 0
    expected_cmd = ["lager"] + list(expected_args)
    assert actual_cmd == expected_cmd, f"Expected {expected_cmd}, got {actual_cmd}"
