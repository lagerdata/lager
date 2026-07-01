# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

import sys
import os
import json
from lager.nets.net import Net, NetType
from lager.measurement.format_utils import fmt_si
from lager.measurement.watt.watt_net import UnsupportedInstrumentError

# ANSI color codes
GREEN = '\033[92m'
CYAN = '\033[96m'
RED = '\033[91m'
RESET = '\033[0m'

# mode -> (human label, unit, net method name)
_QUANTITIES = {
    "power": ("Power", "W", "read"),
    "current": ("Current", "A", "read_current"),
    "voltage": ("Voltage", "V", "read_voltage"),
}


def _emit_single(netname, mode, value, duration, as_json):
    label, unit, _ = _QUANTITIES[mode]
    if as_json:
        sys.stdout.write(json.dumps({
            "netname": netname,
            mode: value,
            "duration_s": duration,
        }) + "\n")
    else:
        sys.stdout.write(f"{GREEN}{label} '{netname}': {fmt_si(value, unit)}{RESET}\n")
    sys.stdout.flush()


def _emit_all(netname, result, duration, as_json):
    current = float(result["current"])
    voltage = float(result["voltage"])
    power = float(result["power"])
    if as_json:
        sys.stdout.write(json.dumps({
            "netname": netname,
            "current": current,
            "voltage": voltage,
            "power": power,
            "duration_s": duration,
        }) + "\n")
    else:
        w = sys.stdout.write
        w(f"{CYAN}Measurements '{netname}' ({duration:g}s):{RESET}\n")
        w(f"  Current: {fmt_si(current, 'A')}\n")
        w(f"  Voltage: {fmt_si(voltage, 'V')}\n")
        w(f"  Power:   {fmt_si(power, 'W')}\n")
    sys.stdout.flush()


def main() -> int:
    netname = None
    try:
        data = json.loads(sys.argv[1])
        netname = data["netname"]
        mode = data.get("mode", "power")
        duration = float(data.get("duration", 0.1))
        as_json = bool(data.get("json", False))

        if mode not in _QUANTITIES and mode != "all":
            sys.stderr.write(f"{RED}Error: Unknown measurement mode '{mode}'{RESET}\n")
            sys.stderr.flush()
            return 1

        net = Net.get(netname, type=NetType.WattMeter)
        try:
            if mode == "all":
                result = net.read_all(duration)
                _emit_all(netname, result, duration, as_json)
            else:
                _, _, method = _QUANTITIES[mode]
                value = float(getattr(net, method)(duration))
                _emit_single(netname, mode, value, duration, as_json)
        finally:
            net.close()

        return 0
    except UnsupportedInstrumentError as e:
        # Watt meter (e.g. Yocto-Watt) that can only report power, not I/V
        sys.stderr.write(f"{RED}Error: {e}{RESET}\n")
        sys.stderr.flush()
        return 1
    except KeyError:
        # Handle invalid net names specifically
        sys.stderr.write(f"{RED}Error: Net '{netname}' not found{RESET}\n")
        sys.stderr.write("Use 'lager nets --box [BOX_NAME]' to list available nets\n")
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
            sys.stderr.write("This command requires a watt-meter net. Use 'lager nets --box [BOX_NAME]' to verify net types\n")
        else:
            sys.stderr.write(f"{RED}Error: {e}{RESET}\n")
        if os.getenv('LAGER_DEBUG') or os.getenv('DEBUG'):
            import traceback
            sys.stderr.write(f"\nDebug traceback:\n{traceback.format_exc()}\n")
        sys.stderr.flush()
        return 1
    except Exception as e:
        # Show user-friendly error message
        sys.stderr.write(f"{RED}Error: {e}{RESET}\n")

        # Only show full traceback in debug mode
        if os.getenv('LAGER_DEBUG') or os.getenv('DEBUG'):
            import traceback
            sys.stderr.write(f"\nDebug traceback:\n{traceback.format_exc()}\n")
            sys.stderr.write("(Set LAGER_DEBUG=0 to hide traceback)\n")
        else:
            sys.stderr.write("(Set LAGER_DEBUG=1 to see full traceback)\n")

        sys.stderr.flush()
        return 1


if __name__ == "__main__":
    sys.exit(main())
