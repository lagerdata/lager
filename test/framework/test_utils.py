#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Test utilities for Lager hardware tests.

Provides helpers for managing hardware service cache, test isolation,
box connectivity, net validation, and common test patterns.

Example usage:
    from test.framework.test_utils import (
        clear_hardware_cache,
        get_cache_stats,
        check_box_connectivity,
        validate_net_exists,
        safe_disable_output,
    )

    # Clear cache between test runs
    clear_hardware_cache()

    # Validate prerequisites
    assert check_box_connectivity()
    assert validate_net_exists('psu1', role='power-supply')
"""

from __future__ import annotations

import os
import json
import time
from typing import Any, Dict, List, Optional, Callable, TypeVar

import requests

# =============================================================================
# Configuration Constants
# =============================================================================

HARDWARE_SERVICE_URL = os.environ.get("LAGER_HARDWARE_URL", "http://localhost:8080")
BOX_HTTP_URL = os.environ.get("LAGER_BOX_URL", "http://localhost:5000")
DEFAULT_TIMEOUT = 5

# =============================================================================
# Hardware Cache Management
# =============================================================================


def clear_hardware_cache(verbose: bool = True) -> bool:
    """Clear the hardware service device cache.

    This is necessary when running both Net API tests and module-level function tests
    in the same script, as Net API tests cache VISA connections that prevent
    module-level functions from creating their own connections.

    Args:
        verbose: If True, print status messages

    Returns:
        True if cache was cleared successfully, False otherwise
    """
    try:
        response = requests.post(
            f"{HARDWARE_SERVICE_URL}/cache/clear",
            timeout=DEFAULT_TIMEOUT
        )
        if response.status_code == 200:
            if verbose:
                result = response.json()
                count = result.get("cleared", 0)
                print(f"\n[Cache] Cleared hardware service cache ({count} devices)")
            return True
        else:
            if verbose:
                print(f"\n[Cache] Warning: Failed to clear cache (HTTP {response.status_code})")
            return False
    except requests.exceptions.RequestException as e:
        if verbose:
            print(f"\n[Cache] Warning: Could not clear cache: {e}")
        return False


def get_cache_stats(verbose: bool = True) -> Optional[Dict[str, Any]]:
    """Get hardware service cache statistics.

    Args:
        verbose: If True, print cache stats

    Returns:
        dict with cache stats, or None if request failed
    """
    try:
        response = requests.get(
            f"{HARDWARE_SERVICE_URL}/cache/stats",
            timeout=DEFAULT_TIMEOUT
        )
        if response.status_code == 200:
            stats = response.json()
            if verbose:
                count = stats.get("cached_devices", 0)
                print(f"\n[Cache] Hardware service has {count} cached device(s)")
                for dev in stats.get("devices", []):
                    print(f"  - {dev.get('name')}: {dev.get('net_info', {}).get('address', 'N/A')}")
            return stats
        else:
            return None
    except requests.exceptions.RequestException:
        return None


# =============================================================================
# Box Connectivity Helpers
# =============================================================================


def check_box_connectivity(
    url: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT,
    verbose: bool = True
) -> bool:
    """Check if the box HTTP service is reachable.

    Args:
        url: Box URL to check (defaults to LAGER_BOX_URL or localhost:5000)
        timeout: Request timeout in seconds
        verbose: If True, print status messages

    Returns:
        True if box is reachable, False otherwise
    """
    check_url = url or BOX_HTTP_URL
    try:
        response = requests.get(f"{check_url}/hello", timeout=timeout)
        if response.status_code == 200:
            if verbose:
                print(f"[Box] Connection OK: {check_url}")
            return True
        else:
            if verbose:
                print(f"[Box] Unexpected status {response.status_code} from {check_url}")
            return False
    except requests.exceptions.RequestException as e:
        if verbose:
            print(f"[Box] Connection failed to {check_url}: {e}")
        return False


def check_hardware_service(
    url: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT,
    verbose: bool = True
) -> bool:
    """Check if the hardware service is reachable.

    Args:
        url: Hardware service URL (defaults to LAGER_HARDWARE_URL or localhost:8080)
        timeout: Request timeout in seconds
        verbose: If True, print status messages

    Returns:
        True if hardware service is reachable, False otherwise
    """
    check_url = url or HARDWARE_SERVICE_URL
    try:
        response = requests.get(f"{check_url}/health", timeout=timeout)
        if response.status_code == 200:
            if verbose:
                print(f"[Hardware] Service OK: {check_url}")
            return True
        else:
            if verbose:
                print(f"[Hardware] Unexpected status {response.status_code} from {check_url}")
            return False
    except requests.exceptions.RequestException as e:
        if verbose:
            print(f"[Hardware] Service unreachable at {check_url}: {e}")
        return False


# =============================================================================
# Net Validation Helpers
# =============================================================================


def get_saved_nets(verbose: bool = False) -> List[Dict[str, Any]]:
    """Get list of all saved nets from the box.

    Args:
        verbose: If True, print net list

    Returns:
        List of net dictionaries
    """
    try:
        response = requests.get(
            f"{HARDWARE_SERVICE_URL}/nets",
            timeout=DEFAULT_TIMEOUT
        )
        if response.status_code == 200:
            nets = response.json()
            if verbose:
                print(f"[Nets] Found {len(nets)} saved nets:")
                for net in nets:
                    print(f"  - {net.get('name')}: {net.get('role', 'unknown')}")
            return nets
        return []
    except requests.exceptions.RequestException:
        return []


def validate_net_exists(
    name: str,
    role: Optional[str] = None,
    verbose: bool = True
) -> bool:
    """Check if a net with the given name (and optional role) exists.

    Args:
        name: Net name to look for
        role: Optional role to match (e.g., 'power-supply', 'battery')
        verbose: If True, print status messages

    Returns:
        True if net exists (and matches role if specified), False otherwise
    """
    nets = get_saved_nets(verbose=False)
    for net in nets:
        if net.get("name") == name:
            if role is None or net.get("role") == role:
                if verbose:
                    print(f"[Nets] Found net '{name}' (role: {net.get('role', 'unknown')})")
                return True
            elif verbose:
                print(f"[Nets] Net '{name}' exists but has role '{net.get('role')}' (expected '{role}')")
    if verbose:
        print(f"[Nets] Net '{name}' not found")
    return False


def get_net_info(name: str) -> Optional[Dict[str, Any]]:
    """Get detailed information about a specific net.

    Args:
        name: Net name to look up

    Returns:
        Net dictionary if found, None otherwise
    """
    nets = get_saved_nets(verbose=False)
    for net in nets:
        if net.get("name") == name:
            return net
    return None


# =============================================================================
# Test Output Helpers
# =============================================================================


def safe_disable_output(
    net_obj: Any,
    verbose: bool = True
) -> bool:
    """Safely disable output on a net, catching any errors.

    Use this in cleanup/teardown to ensure outputs are disabled even if
    the test failed or the device is in an unexpected state.

    Args:
        net_obj: Net object with a disable() method
        verbose: If True, print status messages

    Returns:
        True if disable succeeded, False otherwise
    """
    try:
        if hasattr(net_obj, "disable"):
            net_obj.disable()
            if verbose:
                print("[Cleanup] Output disabled")
            return True
        return False
    except Exception as e:
        if verbose:
            print(f"[Cleanup] Failed to disable output: {e}")
        return False


def wait_for_condition(
    condition: Callable[[], bool],
    timeout: float = 10.0,
    interval: float = 0.5,
    description: str = "condition"
) -> bool:
    """Wait for a condition to become true.

    Args:
        condition: Callable that returns True when condition is met
        timeout: Maximum time to wait in seconds
        interval: Time between checks in seconds
        description: Description for error messages

    Returns:
        True if condition was met, False if timeout expired
    """
    start_time = time.time()
    while time.time() - start_time < timeout:
        if condition():
            return True
        time.sleep(interval)
    print(f"[Timeout] Waiting for {description} timed out after {timeout}s")
    return False


# =============================================================================
# Test Timing Utilities
# =============================================================================


class Timer:
    """Context manager for timing test operations.

    Example:
        with Timer("voltage measurement") as t:
            result = psu.voltage()
        print(f"Measurement took {t.elapsed:.3f}s")
    """

    def __init__(self, description: str = "operation", verbose: bool = True):
        self.description = description
        self.verbose = verbose
        self.start_time: float = 0
        self.end_time: float = 0
        self.elapsed: float = 0

    def __enter__(self) -> "Timer":
        self.start_time = time.time()
        return self

    def __exit__(self, *args) -> None:
        self.end_time = time.time()
        self.elapsed = self.end_time - self.start_time
        if self.verbose:
            print(f"[Timer] {self.description}: {self.elapsed:.3f}s")


# =============================================================================
# Test Result Formatting
# =============================================================================


def print_test_header(name: str) -> None:
    """Print a formatted test header.

    Args:
        name: Test name to display
    """
    print(f"\n{'=' * 50}")
    print(f" {name}")
    print(f"{'=' * 50}\n")


def print_test_footer(name: str, passed: bool = True) -> None:
    """Print a formatted test footer.

    Args:
        name: Test name to display
        passed: Whether the test passed
    """
    status = "PASS" if passed else "FAIL"
    print(f"\n{'=' * 50}")
    print(f" {name} - {status}")
    print(f"{'=' * 50}\n")


def print_step(step_num: int, description: str) -> None:
    """Print a formatted test step.

    Args:
        step_num: Step number
        description: Step description
    """
    print(f"\n{step_num}. {description}...")


def print_success(message: str) -> None:
    """Print a success message."""
    print(f"   [OK] {message}")


def print_error(message: str) -> None:
    """Print an error message."""
    print(f"   [ERROR] {message}")


def print_warning(message: str) -> None:
    """Print a warning message."""
    print(f"   [WARN] {message}")


def print_value(name: str, value: Any, unit: str = "") -> None:
    """Print a named value with optional unit.

    Args:
        name: Value name/label
        value: The value to display
        unit: Optional unit string
    """
    if isinstance(value, float):
        print(f"   {name}: {value:.3f}{unit}")
    else:
        print(f"   {name}: {value}{unit}")


# =============================================================================
# Assertion Helpers
# =============================================================================


def assert_voltage_in_range(
    measured: float,
    expected: float,
    tolerance_pct: float = 5.0,
    name: str = "Voltage"
) -> bool:
    """Assert that a measured voltage is within tolerance of expected.

    Args:
        measured: Measured voltage value
        expected: Expected voltage value
        tolerance_pct: Allowed tolerance as percentage (default 5%)
        name: Name for error messages

    Returns:
        True if within tolerance

    Raises:
        AssertionError: If outside tolerance
    """
    tolerance = expected * (tolerance_pct / 100.0)
    lower = expected - tolerance
    upper = expected + tolerance
    if not (lower <= measured <= upper):
        raise AssertionError(
            f"{name} {measured:.3f}V outside range [{lower:.3f}, {upper:.3f}]V "
            f"(expected {expected:.3f}V +/- {tolerance_pct}%)"
        )
    return True


def assert_current_in_range(
    measured: float,
    expected: float,
    tolerance_pct: float = 10.0,
    name: str = "Current"
) -> bool:
    """Assert that a measured current is within tolerance of expected.

    Args:
        measured: Measured current value
        expected: Expected current value
        tolerance_pct: Allowed tolerance as percentage (default 10%)
        name: Name for error messages

    Returns:
        True if within tolerance

    Raises:
        AssertionError: If outside tolerance
    """
    tolerance = expected * (tolerance_pct / 100.0)
    lower = expected - tolerance
    upper = expected + tolerance
    if not (lower <= measured <= upper):
        raise AssertionError(
            f"{name} {measured:.3f}A outside range [{lower:.3f}, {upper:.3f}]A "
            f"(expected {expected:.3f}A +/- {tolerance_pct}%)"
        )
    return True


# =============================================================================
# Exports
# =============================================================================

__all__ = [
    # Cache management
    "clear_hardware_cache",
    "get_cache_stats",
    # Box connectivity
    "check_box_connectivity",
    "check_hardware_service",
    # Net validation
    "get_saved_nets",
    "validate_net_exists",
    "get_net_info",
    # Output helpers
    "safe_disable_output",
    "wait_for_condition",
    # Timing
    "Timer",
    # Formatting
    "print_test_header",
    "print_test_footer",
    "print_step",
    "print_success",
    "print_error",
    "print_warning",
    "print_value",
    # Assertions
    "assert_voltage_in_range",
    "assert_current_in_range",
    # Constants
    "HARDWARE_SERVICE_URL",
    "BOX_HTTP_URL",
    "DEFAULT_TIMEOUT",
]
