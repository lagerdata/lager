# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

import sys
import os
import json
from lager.nets.net import Net, NetType

# ANSI color codes
GREEN = '\033[92m'
RED = '\033[91m'
RESET = '\033[0m'

def main() -> int:
    try:
        data = json.loads(sys.argv[1])
        net_name = data["netname"]
        net = Net.get(net_name, type=NetType.DAC)
        if "voltage" in data:
            value = float(data["voltage"])
            net.output(value)
            sys.stdout.write(f"{GREEN}DAC '{net_name}' set to {value:.6f} V{RESET}\n")
            sys.stdout.flush()
        else:
            volts = float(net.input())
            sys.stdout.write(f"{GREEN}DAC '{net_name}': {volts:.6f} V{RESET}\n")
            sys.stdout.flush()
        return 0
    except KeyError as e:
        # Handle invalid net names specifically
        sys.stderr.write(f"{RED}Error: Net '{net_name}' not found{RESET}\n")
        sys.stderr.write(f"Use 'lager nets --box [BOX_NAME]' to list available nets\n")
        if os.getenv('LAGER_DEBUG') or os.getenv('DEBUG'):
            import traceback
            sys.stderr.write(f"\nDebug traceback:\n{traceback.format_exc()}\n")
        sys.stderr.flush()
        return 1
    except ValueError as e:
        # Handle type mismatches, conversion errors, and value errors
        error_msg = str(e)
        if "could not convert" in error_msg.lower():
            sys.stderr.write(f"{RED}Error: {e}{RESET}\n")
            sys.stderr.write(f"Voltage must be a numeric value (e.g., 3.3)\n")
        elif "wrong type" in error_msg.lower() or "expected" in error_msg.lower():
            sys.stderr.write(f"{RED}Error: Invalid net type for '{net_name}'{RESET}\n")
            sys.stderr.write(f"This command requires a DAC net. Use 'lager nets --box [BOX_NAME]' to verify net types\n")
        else:
            sys.stderr.write(f"{RED}Error: {e}{RESET}\n")
        if os.getenv('LAGER_DEBUG') or os.getenv('DEBUG'):
            import traceback
            sys.stderr.write(f"\nDebug traceback:\n{traceback.format_exc()}\n")
        sys.stderr.flush()
        return 1
    except Exception as e:
        import traceback

        # Show user-friendly error message
        sys.stderr.write(f"{RED}Error: {e}{RESET}\n")

        # Only show full traceback in debug mode
        if os.getenv('LAGER_DEBUG') or os.getenv('DEBUG'):
            sys.stderr.write(f"\nDebug traceback:\n{traceback.format_exc()}\n")
            sys.stderr.write("(Set LAGER_DEBUG=0 to hide traceback)\n")
        else:
            sys.stderr.write(f"(Set LAGER_DEBUG=1 to see full traceback)\n")

        sys.stderr.flush()
        return 1

if __name__ == "__main__":
    sys.exit(main())