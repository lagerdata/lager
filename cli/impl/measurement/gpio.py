# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

import sys
import os
import json
import time
import signal
from lager.nets.net import Net, NetType

# ANSI color codes
GREEN = '\033[92m'
RED = '\033[91m'
RESET = '\033[0m'

def main() -> int:
    try:
        data = json.loads(sys.argv[1])
        netname = data["netname"]
        action = data["action"]
        net = Net.get(netname, type=NetType.GPIO)

        if action == "input":
            value = int(net.input())
            level_str = "HIGH" if value == 1 else "LOW"
            sys.stdout.write(f"{GREEN}GPIO '{netname}': {level_str} ({value}){RESET}\n")
            sys.stdout.flush()
            return 0

        if action == "wait_for_level":
            from lager.io.gpio.dispatcher import wait_for_level as _wait_for_level

            level = data.get("level")
            if level is None:
                raise ValueError("No level provided for wait_for_level")

            kwargs = {}
            timeout = data.get("timeout")
            if timeout is not None:
                kwargs["timeout"] = float(timeout)
            scan_rate = data.get("scan_rate")
            if scan_rate is not None:
                kwargs["scan_rate"] = int(scan_rate)
            scans_per_read = data.get("scans_per_read")
            if scans_per_read is not None:
                kwargs["scans_per_read"] = int(scans_per_read)
            poll_interval = data.get("poll_interval")
            if poll_interval is not None:
                kwargs["poll_interval"] = float(poll_interval)

            try:
                elapsed = _wait_for_level(netname, level, **kwargs)
                sys.stdout.write(
                    f"{GREEN}GPIO '{netname}' reached level {level} "
                    f"in {elapsed:.4f}s{RESET}\n"
                )
                sys.stdout.flush()
                return 0
            except TimeoutError as te:
                sys.stderr.write(f"{RED}{te}{RESET}\n")
                sys.stderr.flush()
                return 1

        if action == "output":
            level = data.get("level")
            if level is None:
                raise ValueError("No level provided for GPIO output")

            # Handle toggle - read current state and invert
            if level.lower() == "toggle":
                current_value = int(net.input())
                new_value = 1 if current_value == 0 else 0
                net.output(str(new_value))
                level_str = "HIGH" if new_value == 1 else "LOW"
                sys.stdout.write(f"{GREEN}GPIO '{netname}' toggled to {level_str}{RESET}\n")
            else:
                net.output(level)
                # Normalize level for display
                if level.lower() in ("1", "on", "high"):
                    level_str = "HIGH"
                else:
                    level_str = "LOW"
                sys.stdout.write(f"{GREEN}GPIO '{netname}' set to {level_str}{RESET}\n")
            sys.stdout.flush()

            hold = data.get("hold", False)
            if hold:
                sys.stdout.write("Holding output state (Ctrl+C to release)...\n")
                sys.stdout.flush()

                def _shutdown(signum, frame):
                    raise SystemExit(0)

                signal.signal(signal.SIGTERM, _shutdown)

                try:
                    while True:
                        time.sleep(1)
                except (KeyboardInterrupt, SystemExit):
                    pass
            return 0

        raise ValueError(f"Invalid action '{action}'")

    except KeyError as e:
        # Handle invalid net names specifically
        sys.stderr.write(f"{RED}Error: Net '{netname}' not found{RESET}\n")
        sys.stderr.write(f"Use 'lager nets --box <box>' to list available nets\n")
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
            sys.stderr.write(f"This command requires a GPIO net. Use 'lager nets --box <box>' to verify net types\n")
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
