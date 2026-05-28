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
        netname = data["netname"]
        net = Net.get(netname, type=NetType.ADC)
        volts = float(net.input())
        sys.stdout.write(f"{GREEN}ADC '{netname}': {volts:.6f} V{RESET}\n")
        sys.stdout.flush()
        return 0
    except KeyError as e:
        # Handle invalid net names specifically
        sys.stderr.write(f"{RED}Error: Net '{netname}' not found{RESET}\n")
        sys.stderr.write(f"Use 'lager nets --box [BOX_NAME]' to list available nets\n")
        if os.getenv('LAGER_DEBUG') or os.getenv('DEBUG'):
            import traceback
            sys.stderr.write(f"\nDebug traceback:\n{traceback.format_exc()}\n")
        sys.stderr.flush()
        return 1
    except ValueError as e:
        # Handle type mismatches and value errors
        error_msg = str(e)
        if "wrong type" in error_msg.lower() or "expected" in error_msg.lower():
            sys.stderr.write(f"{RED}Error: Invalid net type for '{netname}'{RESET}\n")
            sys.stderr.write(f"This command requires an ADC net. Use 'lager nets --box [BOX_NAME]' to verify net types\n")
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
