#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Electronic Load implementation script for box execution."""

import sys
import json
from lager.power.eload.dispatcher import (
    set_constant_current,
    get_constant_current,
    set_constant_voltage,
    get_constant_voltage,
    set_constant_resistance,
    get_constant_resistance,
    set_constant_power,
    get_constant_power,
    get_state,
)

# ANSI color codes
GREEN = '\033[92m'
RED = '\033[91m'
RESET = '\033[0m'


def format_output(result):
    """Format result dictionary into human-readable output."""
    if "error" in result:
        return f"{RED}Error: {result['error']}{RESET}"

    output_parts = []

    # Handle mode changes (when setting values)
    if "mode" in result:
        output_parts.append(f"{GREEN}Mode: {result['mode']}{RESET}")

    # Handle specific value types with units
    if "current" in result:
        output_parts.append(f"{GREEN}Current: {result['current']} A{RESET}")
    elif "voltage" in result:
        output_parts.append(f"{GREEN}Voltage: {result['voltage']} V{RESET}")
    elif "resistance" in result:
        output_parts.append(f"{GREEN}Resistance: {result['resistance']} Ω{RESET}")
    elif "power" in result:
        output_parts.append(f"{GREEN}Power: {result['power']} W{RESET}")

    return ", ".join(output_parts) if output_parts else f"{GREEN}{str(result)}{RESET}"


def main():
    """Main entry point."""
    if len(sys.argv) < 2:
        print(f"{RED}Error: No command specified{RESET}")
        sys.exit(1)

    command = sys.argv[1]

    try:
        if command == "cc":
            # Constant Current
            if len(sys.argv) < 3:
                raise ValueError("Net name required")
            if len(sys.argv) > 3:
                # Set mode and value
                result = set_constant_current(sys.argv[2], float(sys.argv[3]))
            else:
                # Read value
                result = get_constant_current(sys.argv[2])

        elif command == "cv":
            # Constant Voltage
            if len(sys.argv) < 3:
                raise ValueError("Net name required")
            if len(sys.argv) > 3:
                # Set mode and value
                result = set_constant_voltage(sys.argv[2], float(sys.argv[3]))
            else:
                # Read value
                result = get_constant_voltage(sys.argv[2])

        elif command == "cr":
            # Constant Resistance
            if len(sys.argv) < 3:
                raise ValueError("Net name required")
            if len(sys.argv) > 3:
                # Set mode and value
                result = set_constant_resistance(sys.argv[2], float(sys.argv[3]))
            else:
                # Read value
                result = get_constant_resistance(sys.argv[2])

        elif command == "cp":
            # Constant Power
            if len(sys.argv) < 3:
                raise ValueError("Net name required")
            if len(sys.argv) > 3:
                # Set mode and value
                result = set_constant_power(sys.argv[2], float(sys.argv[3]))
            else:
                # Read value
                result = get_constant_power(sys.argv[2])

        elif command == "state":
            # Get comprehensive state
            if len(sys.argv) < 3:
                raise ValueError("Net name required")
            # state() prints directly, so we don't need to format/print the result
            get_state(sys.argv[2])
            return

        else:
            raise ValueError(f"Unknown command: {command}")

        print(format_output(result))

    except Exception as e:
        print(f"{RED}Error: {str(e)}{RESET}")
        sys.exit(1)


if __name__ == "__main__":
    main()
