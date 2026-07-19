# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for BlufiClient.scan() -- BLE advertisement presence checks.

Run with:
    python -m pytest test/unit/blufi/ -v
"""
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Ensure box/lager is importable, and mock bleak if not installed
# ---------------------------------------------------------------------------
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
BOX_DIR = os.path.join(REPO_ROOT, "box")
if BOX_DIR not in sys.path:
    sys.path.insert(0, BOX_DIR)

# bleak is a BLE library only available on boxes. Mock it so we can import
# the BluFi package locally for unit-testing pure-logic code.
if "bleak" not in sys.modules:
    _mock_bleak = MagicMock()
    sys.modules["bleak"] = _mock_bleak
    sys.modules["bleak.backends"] = _mock_bleak.backends
    sys.modules["bleak.backends.characteristic"] = _mock_bleak.backends.characteristic

from lager.blufi import client as client_mod


@pytest.fixture
def client():
    c = client_mod.BlufiClient()
    yield c
    import atexit
    try:
        atexit.unregister(c._cleanup)
    except Exception:
        pass
    try:
        c._bleak_loop.call_soon_threadsafe(c._bleak_loop.stop)
    except Exception:
        pass


def _fake_discovered():
    """Shape of BleakScanner.discover(return_adv=True): {address: (BLEDevice, AdvertisementData)}."""
    def entry(name, address, rssi):
        return (SimpleNamespace(name=name, address=address), SimpleNamespace(rssi=rssi))
    return {
        "AA:BB:CC:DD:EE:01": entry("MyDevice-0001", "AA:BB:CC:DD:EE:01", -40),
        "AA:BB:CC:DD:EE:02": entry("MyDevice-0002", "AA:BB:CC:DD:EE:02", -70),
        "AA:BB:CC:DD:EE:03": entry("OtherSensor", "AA:BB:CC:DD:EE:03", -50),
        # Many advertisers carry no local name at all
        "AA:BB:CC:DD:EE:04": entry(None, "AA:BB:CC:DD:EE:04", -30),
    }


@pytest.fixture
def scanner():
    fake = MagicMock()
    fake.discover = AsyncMock(return_value=_fake_discovered())
    with patch.object(client_mod, "BleakScanner", fake):
        yield fake


class TestScan:
    def test_returns_all_devices_sorted_by_rssi(self, client, scanner):
        devices = client.scan(timeout=2.5)
        assert [d["rssi"] for d in devices] == [-30, -40, -50, -70]
        for d in devices:
            assert set(d.keys()) == {"name", "address", "rssi"}

    def test_passes_timeout_and_requests_adv_data(self, client, scanner):
        client.scan(timeout=2.5)
        scanner.discover.assert_called_once_with(timeout=2.5, return_adv=True)

    def test_default_timeout(self, client, scanner):
        client.scan()
        scanner.discover.assert_called_once_with(timeout=10.0, return_adv=True)

    def test_name_prefix_filters(self, client, scanner):
        devices = client.scan(timeout=1.0, name_prefix="MyDevice-")
        assert [d["name"] for d in devices] == ["MyDevice-0001", "MyDevice-0002"]
        assert [d["rssi"] for d in devices] == [-40, -70]

    def test_name_prefix_is_prefix_not_substring(self, client, scanner):
        assert client.scan(timeout=1.0, name_prefix="Device-") == []

    def test_name_prefix_excludes_unnamed_devices(self, client, scanner):
        devices = client.scan(timeout=1.0, name_prefix="MyDevice-")
        assert all(d["name"] is not None for d in devices)

    def test_no_matches_returns_empty_list(self, client, scanner):
        assert client.scan(timeout=1.0, name_prefix="Nonexistent-") == []
