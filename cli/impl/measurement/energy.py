# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

import sys
import os
import json
from lager.nets.net import Net, NetType
from lager.measurement.format_utils import fmt_si as _fmt_si

# ANSI color codes
GREEN = '\033[92m'
CYAN = '\033[96m'
RED = '\033[91m'
RESET = '\033[0m'


def _print_energy(netname, result):
    dur = result["duration_s"]
    e_j = result["energy_j"]
    e_wh = result["energy_wh"]
    q_c = result["charge_c"]
    q_ah = result["charge_ah"]

    w = sys.stdout.write
    w(f"{GREEN}Energy '{netname}' ({dur:.1f}s integration):{RESET}\n")
    w(f"  Energy:  {_fmt_si(e_j, 'J')}  ({_fmt_si(e_wh, 'Wh')})\n")
    w(f"  Charge:  {_fmt_si(q_c, 'C')}  ({_fmt_si(q_ah, 'Ah')})\n")
    sys.stdout.flush()


def _print_stats(netname, result):
    dur = result["duration_s"]
    w = sys.stdout.write
    w(f"{CYAN}Stats '{netname}' ({dur:.1f}s):{RESET}\n")

    labels = [("Current", "current", "A"), ("Voltage", "voltage", "V"), ("Power", "power", "W")]
    for label, key, unit in labels:
        s = result[key]
        w(f"  {label:<9s} mean={_fmt_si(s['mean'], unit):<14s}"
          f"min={_fmt_si(s['min'], unit):<14s}"
          f"max={_fmt_si(s['max'], unit):<14s}"
          f"std={_fmt_si(s['std'], unit)}\n")
    sys.stdout.flush()


def main() -> int:
    try:
        data = json.loads(sys.argv[1])
        netname = data["netname"]
        duration = float(data.get("duration", 10.0))
        mode = data.get("mode", "energy")

        net = Net.get(netname, type=NetType.EnergyAnalyzer)

        try:
            if mode == "stats":
                result = net.read_stats(duration)
                _print_stats(netname, result)
            else:
                result = net.read_energy(duration)
                _print_energy(netname, result)
        finally:
            # Close the device so its USB stream thread is torn down cleanly;
            # otherwise the process can hang or crash on exit.
            net.close()

        return 0
    except KeyError as e:
        sys.stderr.write(f"{RED}Error: Net '{netname}' not found{RESET}\n")
        sys.stderr.write("Use 'lager nets --box [BOX_NAME]' to list available nets\n")
        if os.getenv('LAGER_DEBUG') or os.getenv('DEBUG'):
            import traceback
            sys.stderr.write(f"\nDebug traceback:\n{traceback.format_exc()}\n")
        sys.stderr.flush()
        return 1
    except Exception as e:
        sys.stderr.write(f"{RED}Error: {e}{RESET}\n")
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
