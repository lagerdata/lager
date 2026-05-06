# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Lager Test Framework.

This package provides shared infrastructure for Lager hardware tests including:
- Bash test harness (colors.sh, harness.sh)
- Test utilities for cache management, connectivity checks, and assertions
- Pytest fixtures for hardware nets with automatic cleanup
- Common test patterns and helpers

Directory Structure:
    framework/
    ├── __init__.py          # This file - exports Python utilities
    ├── colors.sh            # Color definitions for bash test output
    ├── harness.sh           # Test tracking functions (start_section, track_test, print_summary)
    ├── test_utils.py        # Python test utilities
    └── fixtures.py          # pytest fixtures for hardware tests

Usage in bash scripts:
    #!/bin/bash
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    source "${SCRIPT_DIR}/../framework/colors.sh"
    source "${SCRIPT_DIR}/../framework/harness.sh"

    # Initialize test harness
    init_harness

    # Start a section
    start_section "My Test Section"

    # Track test results
    my_test_command && track_test "pass" || track_test "fail"

    # Print summary at end
    print_summary

Usage in Python tests:
    # Import utilities directly
    from test.framework import (
        clear_hardware_cache,
        check_box_connectivity,
        Timer,
        assert_voltage_in_range,
    )

    # Import fixtures for pytest
    from test.framework.fixtures import (
        supply_net,
        battery_net,
        hardware_cache,
    )

    # Or use in conftest.py:
    pytest_plugins = ["test.framework.fixtures"]
"""

from __future__ import annotations

__version__ = "0.1.0"

# =============================================================================
# Test Utilities
# =============================================================================

from .test_utils import (
    # Cache management
    clear_hardware_cache,
    get_cache_stats,
    # Box connectivity
    check_box_connectivity,
    check_hardware_service,
    # Net validation
    get_saved_nets,
    validate_net_exists,
    get_net_info,
    # Output helpers
    safe_disable_output,
    wait_for_condition,
    # Timing
    Timer,
    # Formatting
    print_test_header,
    print_test_footer,
    print_step,
    print_success,
    print_error,
    print_warning,
    print_value,
    # Assertions
    assert_voltage_in_range,
    assert_current_in_range,
    # Constants
    HARDWARE_SERVICE_URL,
    BOX_HTTP_URL,
    DEFAULT_TIMEOUT,
)

# =============================================================================
# Re-export fixture module for pytest plugin discovery
# =============================================================================

# Note: Fixtures are not imported here to avoid pytest collection issues.
# Instead, use one of:
#   from test.framework.fixtures import supply_net, battery_net
#   pytest_plugins = ["test.framework.fixtures"]

# =============================================================================
# Exports
# =============================================================================

__all__ = [
    # Version
    "__version__",
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
