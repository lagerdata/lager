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
        net = Net.get(netname, type=NetType.Thermocouple)
        temperature = float(net.read())
        sys.stdout.write(f"{GREEN}Temperature: {temperature}˚C{RESET}\n")
        sys.stdout.flush()
        return 0
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
