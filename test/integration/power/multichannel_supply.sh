#!/bin/bash

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
#
# Two channels of one power supply: setpoints, outputs and protection limits
# must stay independent.
#
# Every value is read back from `lager supply <net> state`, and with an output
# enabled the measured voltage is compared with the setpoint. The suite uses
# at most 5 V and 0.2 A, which every channel of a supported multi-channel
# supply can source unloaded.
#
# The channels' setpoints, OVP and OCP are recorded first and put back at the
# end through `lager python`, because the CLI refuses an OVP above the
# channel's rated voltage and so cannot restore a factory limit set above it.
#
# Refuses a Keithley net: the Keithley driver clamps an out-of-range voltage
# to its maximum instead of refusing it.
#
# Usage: multichannel_supply.sh <BOX> <NET1> <NET2>

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/../../framework/colors.sh"
source "${SCRIPT_DIR}/../../framework/harness.sh"

set +e

if [ $# -lt 3 ]; then
  echo "Usage: $0 <BOX> <NET1> <NET2>   (two channels of one supply)"
  exit 1
fi

BOX="$1"
NET1="$2"
NET2="$3"
TOL="${SUPPLY_MEASURE_TOL:-0.1}"   # volts, unloaded output vs setpoint
WORK="$(mktemp -d)"

# Print field $2 of saved net $1 (from `nets show --json`).
net_field() {
  lager nets show "$1" --box "$BOX" --json 2>/dev/null | python3 -c '
import json, sys
try:
    print(json.load(sys.stdin).get(sys.argv[1], ""))
except ValueError:
    pass
' "$2"
}

for n in "$NET1" "$NET2"; do
  case "$(net_field "$n" instrument)" in
    *[Kk]eithley*)
      echo "Refusing Keithley net '$n': its driver clamps out-of-range voltages instead of refusing them." >&2
      exit 2 ;;
  esac
done

init_harness

# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------

check() {
  local desc="$1" detail="$2"
  shift 2
  echo -n "Test: $desc... "
  if "$@"; then
    track_test "pass"
  else
    track_test "fail"
    [ -n "$detail" ] && echo "    detail: $(printf '%s' "$detail" | head -3 | tr '\n' ' ')"
  fi
}

refused() { [ "$1" -ne 0 ] && printf '%s' "$2" | grep -qiE "$3"; }

state_line() { lager supply "$1" state --box "$BOX" 2>&1; }

# Field of a state line: output (ON/OFF), set_v, set_i, meas_v, meas_i, ocp, ovp.
field() {
  printf '%s' "$1" | python3 -c '
import re, sys
m = re.search(r"Channel \d+: (\w+), Set: ([-\d.]+)V/([-\d.]+)A, "
              r"Measured: ([-\d.]+)V/([-\d.]+)A, OCP: ([-\d.]+)A, OVP: ([-\d.]+)V",
              sys.stdin.read())
keys = ["output", "set_v", "set_i", "meas_v", "meas_i", "ocp", "ovp"]
if m:
    print(m.group(keys.index(sys.argv[1]) + 1))
' "$2"
}

f() { field "$(state_line "$1")" "$2"; }

near() {
  python3 -c '
import sys
try:
    a, b, tol = (float(x) for x in sys.argv[1:4])
except ValueError:
    sys.exit(1)
sys.exit(0 if abs(a - b) <= tol else 1)
' "$1" "$2" "$3"
}

sup() { lager supply "$@" --box "$BOX" 2>&1; }

# Wait up to 5 s for $1's measured voltage to reach $2 (outputs slew).
settles_at() {
  local _
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    near "$(f "$1" meas_v)" "$2" "$TOL" && return 0
    sleep 0.5
  done
  return 1
}

# ------------------------------------------------------------
# Record and restore
# ------------------------------------------------------------

S1=$(state_line "$NET1"); S2=$(state_line "$NET2")
echo "Before: $S1"
echo "Before: $S2"

cat > "$WORK/restore.py" <<EOF
from lager import Net, NetType
for name, ovp, ocp, v, i, out in [
    ("$NET1", $(field "$S1" ovp), $(field "$S1" ocp), $(field "$S1" set_v), $(field "$S1" set_i), "$(field "$S1" output)"),
    ("$NET2", $(field "$S2" ovp), $(field "$S2" ocp), $(field "$S2" set_v), $(field "$S2" set_i), "$(field "$S2" output)"),
]:
    s = Net.get(name, type=NetType.PowerSupply)
    # The set_* methods address the net's own channel. Plain ovp()/ocp()
    # fall through to the driver and act on ITS channel, not the net's.
    s.disable()
    s.set_ovp(ovp)      # raise first: OVP may not be set below the setpoint
    s.set_ocp(ocp)
    s.set_voltage(v)
    s.set_current(i)
    if out == "ON":
        s.enable()
    print("restored", name)
EOF

restore_supplies() {
  lager supply "$NET1" disable --yes --box "$BOX" >/dev/null 2>&1
  lager supply "$NET2" disable --yes --box "$BOX" >/dev/null 2>&1
  (cd "$WORK" && lager python restore.py --box "$BOX" 2>&1 | tail -2)
  rm -rf "$WORK"
}

echo "========================================================================"
echo "MULTI-CHANNEL SUPPLY SUITE"
echo "========================================================================"
echo "Box: $BOX   Channels: $NET1, $NET2   Measure tolerance: ${TOL} V"
echo ""

# ============================================================
# SECTION 1: THE TWO NETS
# ============================================================
start_section "Nets"

check "1.1 both states parse" "$S1 | $S2" \
  bash -c '[ -n "$1" ] && [ -n "$2" ]' _ "$(field "$S1" ovp)" "$(field "$S2" ovp)"
INST1=$(net_field "$NET1" instrument); INST2=$(net_field "$NET2" instrument)
check "1.2 both nets are channels of one instrument" "$INST1 / $INST2" \
  bash -c '[ -n "$1" ] && [ "$1" = "$2" ]' _ "$INST1" "$INST2"
CH1=$(net_field "$NET1" pin); CH2=$(net_field "$NET2" pin)
check "1.3 on different channels" "$CH1 / $CH2" \
  bash -c '[ -n "$1" ] && [ "$1" != "$2" ]' _ "$CH1" "$CH2"

# Only restore what was recorded: the restore script needs both states.
if [ -n "$(field "$S1" ovp)" ] && [ -n "$(field "$S2" ovp)" ]; then
  harness_add_cleanup restore_supplies
else
  echo "Cannot record the starting state; stopping before changing anything."
  print_summary
  exit 1
fi

sup "$NET1" disable --yes >/dev/null; sup "$NET2" disable --yes >/dev/null

# ============================================================
# SECTION 2: INDEPENDENT SETPOINTS
# ============================================================
start_section "Setpoints"

sup "$NET1" voltage 1.0 --yes >/dev/null; sup "$NET1" current 0.10 --yes >/dev/null
sup "$NET2" voltage 2.5 --yes >/dev/null; sup "$NET2" current 0.20 --yes >/dev/null
check "2.1 $NET1 holds 1.0 V / 0.10 A" "$(state_line "$NET1")" \
  bash -c '[ "$1" = 1.0 ] && [ "$2" = 0.1 ]' _ "$(f "$NET1" set_v)" "$(f "$NET1" set_i)"
check "2.2 $NET2 holds 2.5 V / 0.20 A" "$(state_line "$NET2")" \
  bash -c '[ "$1" = 2.5 ] && [ "$2" = 0.2 ]' _ "$(f "$NET2" set_v)" "$(f "$NET2" set_i)"

sup "$NET1" voltage 1.5 --yes >/dev/null
check "2.3 changing $NET1 leaves $NET2 at 2.5 V" "$(state_line "$NET2")" [ "$(f "$NET2" set_v)" = 2.5 ]

# ============================================================
# SECTION 3: INDEPENDENT OUTPUTS
# ============================================================
start_section "Outputs"

OUT=$(sup "$NET1" enable --yes); RC=$?
check "3.1 enable $NET1" "$OUT" [ "$RC" -eq 0 ]
check "3.2 $NET1 ON, $NET2 still OFF" "$(f "$NET1" output)/$(f "$NET2" output)" \
  [ "$(f "$NET1" output)/$(f "$NET2" output)" = "ON/OFF" ]
check "3.3 $NET1 measures 1.5 V" "$(state_line "$NET1")" settles_at "$NET1" 1.5
check "3.4 $NET2 measures 0 V while off" "$(state_line "$NET2")" near "$(f "$NET2" meas_v)" 0 "$TOL"

sup "$NET2" enable --yes >/dev/null
check "3.5 both ON" "$(f "$NET1" output)/$(f "$NET2" output)" \
  [ "$(f "$NET1" output)/$(f "$NET2" output)" = "ON/ON" ]
check "3.6 $NET2 measures 2.5 V" "$(state_line "$NET2")" settles_at "$NET2" 2.5

sup "$NET1" voltage 1.0 --yes >/dev/null
check "3.7 $NET1 follows a live change to 1.0 V" "$(state_line "$NET1")" settles_at "$NET1" 1.0
check "3.8 $NET2 still 2.5 V" "$(state_line "$NET2")" settles_at "$NET2" 2.5

sup "$NET1" disable --yes >/dev/null
check "3.9 disabling $NET1 leaves $NET2 ON at 2.5 V" "$(state_line "$NET2")" \
  bash -c '[ "$1" = OFF ] && [ "$2" = ON ]' _ "$(f "$NET1" output)" "$(f "$NET2" output)"
check "3.10 $NET2 still measures 2.5 V" "$(state_line "$NET2")" settles_at "$NET2" 2.5

BAD=0
for _ in 1 2 3 4 5; do
  sup "$NET1" enable --yes >/dev/null || BAD=$((BAD + 1))
  sup "$NET1" disable --yes >/dev/null || BAD=$((BAD + 1))
done
check "3.11 five on/off cycles of $NET1 succeed" "$BAD failed" [ "$BAD" -eq 0 ]
check "3.12 $NET2 held 2.5 V through them" "$(state_line "$NET2")" settles_at "$NET2" 2.5

sup "$NET2" disable --yes >/dev/null
check "3.13 both OFF" "$(f "$NET1" output)/$(f "$NET2" output)" \
  [ "$(f "$NET1" output)/$(f "$NET2" output)" = "OFF/OFF" ]

# ============================================================
# SECTION 4: INDEPENDENT PROTECTION LIMITS
# ============================================================
start_section "Protection"

OVP2_BEFORE=$(f "$NET2" ovp)
OUT=$(sup "$NET1" voltage 1.0 --ovp 3.0 --yes); RC=$?
check "4.1 $NET1 OVP set to 3.0 V" "$OUT" \
  bash -c '[ "$1" -eq 0 ] && [ "$2" = 3.0 ]' _ "$RC" "$(f "$NET1" ovp)"
check "4.2 $NET2 OVP unchanged" "$(state_line "$NET2")" [ "$(f "$NET2" ovp)" = "$OVP2_BEFORE" ]

OUT=$(sup "$NET1" voltage 4.0 --yes); RC=$?
check "4.3 $NET1 refuses 4.0 V above its 3.0 V OVP" "$OUT" refused "$RC" "$OUT" "exceed|OVP"
check "4.4 $NET1 setpoint unchanged at 1.0 V" "$(state_line "$NET1")" [ "$(f "$NET1" set_v)" = 1.0 ]
OUT=$(sup "$NET2" voltage 4.0 --yes); RC=$?
check "4.5 $NET2 still accepts 4.0 V" "$OUT" \
  bash -c '[ "$1" -eq 0 ] && [ "$2" = 4.0 ]' _ "$RC" "$(f "$NET2" set_v)"

OCP2_BEFORE=$(f "$NET2" ocp)
OUT=$(sup "$NET1" current 0.10 --ocp 0.5 --yes); RC=$?
check "4.6 $NET1 OCP set to 0.5 A" "$OUT" \
  bash -c '[ "$1" -eq 0 ] && [ "$2" = 0.5 ]' _ "$RC" "$(f "$NET1" ocp)"
check "4.7 $NET2 OCP unchanged" "$(state_line "$NET2")" [ "$(f "$NET2" ocp)" = "$OCP2_BEFORE" ]

# ============================================================
# SECTION 5: REFUSALS
# ============================================================
start_section "Refusals"

V2=$(f "$NET2" set_v)
OUT=$(sup "$NET2" voltage 100.5 --yes); RC=$?
check "5.1 100.5 V is refused" "$OUT" refused "$RC" "$OUT" "exceed|range|between|limit|invalid"
OUT=$(sup "$NET2" voltage -1 --yes); RC=$?
check "5.2 -1 V is refused" "$OUT" refused "$RC" "$OUT" "negative|range|between|invalid|no such option|positive"
OUT=$(sup "$NET2" current -0.1 --yes); RC=$?
check "5.3 -0.1 A is refused" "$OUT" refused "$RC" "$OUT" "negative|range|between|invalid|no such option|positive"
check "5.4 $NET2 setpoint unchanged by the refusals" "$(state_line "$NET2")" [ "$(f "$NET2" set_v)" = "$V2" ]
check "5.5 both outputs still OFF" "$(f "$NET1" output)/$(f "$NET2" output)" \
  [ "$(f "$NET1" output)/$(f "$NET2" output)" = "OFF/OFF" ]

# ============================================================
# SECTION 6: RESTORE
# ============================================================
start_section "Restore"

restore_supplies
_HARNESS_CLEANUP=()
A1=$(state_line "$NET1"); A2=$(state_line "$NET2")
echo "After:  $A1"
echo "After:  $A2"
check "6.1 $NET1 back to its starting state" "$A1" [ "$(field "$A1" output)/$(field "$A1" set_v)/$(field "$A1" set_i)/$(field "$A1" ocp)/$(field "$A1" ovp)" = "$(field "$S1" output)/$(field "$S1" set_v)/$(field "$S1" set_i)/$(field "$S1" ocp)/$(field "$S1" ovp)" ]
check "6.2 $NET2 back to its starting state" "$A2" [ "$(field "$A2" output)/$(field "$A2" set_v)/$(field "$A2" set_i)/$(field "$A2" ocp)/$(field "$A2" ovp)" = "$(field "$S2" output)/$(field "$S2" set_v)/$(field "$S2" set_i)/$(field "$S2" ocp)/$(field "$S2" ovp)" ]

print_summary
exit_with_status
