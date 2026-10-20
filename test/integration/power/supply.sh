#!/bin/bash

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
#
# The `lager supply` CLI surface against one power-supply channel.
#
# Every setting is read back from `lager supply <net> state`, and with the
# output enabled the measured voltage is compared with the setpoint, so a
# command that exits 0 without reaching the instrument fails here. Every
# refusal is checked twice: the command must exit non-zero with a reason, and
# the channel's setpoint must be unchanged afterwards.
#
# The suite drives at most 5 V and 0.5 A into an unloaded output. The channel
# rating (CHANNEL_MAX_VOLTAGE / CHANNEL_MAX_CURRENT) is used only to check
# that a value just above it is refused.
#
# The channel's setpoints, OVP and OCP are recorded first and put back at the
# end through `lager python`, because the CLI refuses an OVP above the
# channel's rated voltage and so cannot restore a factory limit set above it.
#
# Refuses a Keithley net: the Keithley driver clamps an out-of-range voltage
# to its maximum instead of refusing it.
#
# Usage: supply.sh <BOX> <SUPPLY_NET>
#   CHANNEL_MAX_VOLTAGE  rated voltage of the channel (required)
#   CHANNEL_MAX_CURRENT  rated current of the channel (required)
#   SUPPLY_MEASURE_TOL   volts, unloaded output vs setpoint (default 0.1)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/../../framework/colors.sh"
source "${SCRIPT_DIR}/../../framework/harness.sh"

set +e

if [ $# -lt 2 ] || [ -z "$CHANNEL_MAX_VOLTAGE" ] || [ -z "$CHANNEL_MAX_CURRENT" ]; then
  echo "Usage: CHANNEL_MAX_VOLTAGE=<V> CHANNEL_MAX_CURRENT=<A> $0 <BOX> <SUPPLY_NET>"
  exit 1
fi

BOX="$1"
NET="$2"
TOL="${SUPPLY_MEASURE_TOL:-0.1}"
VMAX="$CHANNEL_MAX_VOLTAGE"
IMAX="$CHANNEL_MAX_CURRENT"
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

case "$(net_field "$NET" instrument)" in
  *[Kk]eithley*)
    echo "Refusing Keithley net '$NET': its driver clamps out-of-range voltages instead of refusing them." >&2
    exit 2 ;;
esac

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
succeeded() { [ "$1" -eq 0 ] && printf '%s' "$2" | grep -q '\[OK\]'; }

sup() { lager supply "$NET" "$@" --box "$BOX" 2>&1; }

state_line() { lager supply "$NET" state --box "$BOX" 2>&1; }

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

f() { field "$(state_line)" "$1"; }

# The first number in $1.
num() { printf '%s' "$1" | grep -oE -- '-?[0-9]+(\.[0-9]+)?' | head -1; }

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

# A setpoint reads back as $1 (to the instrument's 1 mV / 1 mA resolution).
set_v_is() { near "$(f set_v)" "$1" 0.002; }
set_i_is() { near "$(f set_i)" "$1" 0.002; }

# Wait up to 5 s for the measured voltage to reach $1 (outputs slew).
settles_at() {
  local _
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    near "$(f meas_v)" "$1" "$TOL" && return 0
    sleep 0.5
  done
  return 1
}

above() { python3 -c 'import sys; print(round(float(sys.argv[1]) + float(sys.argv[2]), 3))' "$1" "$2"; }

# ------------------------------------------------------------
# Record and restore
# ------------------------------------------------------------

S0=$(state_line)
echo "Before: $S0"

cat > "$WORK/restore.py" <<EOF
from lager import Net, NetType
s = Net.get("$NET", type=NetType.PowerSupply)
# The set_* methods address the net's own channel. Plain ovp()/ocp() fall
# through to the driver and act on ITS channel, not the net's.
s.disable()
s.set_ovp($(field "$S0" ovp))   # raise first: OVP may not be set below the setpoint
s.set_ocp($(field "$S0" ocp))
s.set_voltage($(field "$S0" set_v))
s.set_current($(field "$S0" set_i))
if "$(field "$S0" output)" == "ON":
    s.enable()
print("restored $NET")
EOF

restore_supply() {
  lager supply "$NET" disable --yes --box "$BOX" >/dev/null 2>&1
  (cd "$WORK" && lager python restore.py --box "$BOX" 2>&1 | tail -2)
  rm -rf "$WORK"
}

echo "========================================================================"
echo "SUPPLY SUITE"
echo "========================================================================"
echo "Box: $BOX   Net: $NET   Rating: ${VMAX} V / ${IMAX} A   Measure tolerance: ${TOL} V"
echo ""

# ============================================================
# SECTION 1: THE NET
# ============================================================
start_section "Net"

OUT=$(lager supply --box "$BOX" 2>&1); RC=$?
check "1.1 the net is listed" "$OUT" \
  bash -c '[ "$1" -eq 0 ] && printf "%s" "$2" | grep -qw -- "$3"' _ "$RC" "$OUT" "$NET"
check "1.2 its state parses" "$S0" bash -c '[ -n "$1" ] && [ -n "$2" ]' _ "$(field "$S0" ovp)" "$(field "$S0" set_v)"

# Only change anything once the starting state is recorded.
if [ -n "$(field "$S0" ovp)" ] && [ -n "$(field "$S0" ocp)" ]; then
  harness_add_cleanup restore_supply
else
  echo "Cannot record the starting state; stopping before changing anything."
  print_summary
  exit 1
fi

OUT=$(sup disable --yes); RC=$?
check "1.3 disable" "$OUT" succeeded "$RC" "$OUT"
check "1.4 state reads OFF" "$(state_line)" [ "$(f output)" = OFF ]

# The set-by-net readings agree with the state line.
sup voltage 1.0 --yes >/dev/null; sup current 0.1 --yes >/dev/null
OUT=$(sup voltage); RC=$?
check "1.5 voltage with no value reads the setpoint" "$OUT" \
  bash -c '[ "$1" -eq 0 ] && python3 -c "import sys; sys.exit(abs(float(sys.argv[1]) - 1.0) > 0.002)" "$2" 2>/dev/null' _ "$RC" "$(num "$OUT")"
OUT=$(sup current); RC=$?
check "1.6 current with no value reads the setpoint" "$OUT" \
  bash -c '[ "$1" -eq 0 ] && python3 -c "import sys; sys.exit(abs(float(sys.argv[1]) - 0.1) > 0.002)" "$2" 2>/dev/null' _ "$RC" "$(num "$OUT")"

# ============================================================
# SECTION 2: REFUSALS
# ============================================================
# Each must exit non-zero with a reason; 2.12 then checks that none of them
# reached the channel.
start_section "Refusals"

OUT=$(lager supply no_such_net_xyz state --box "$BOX" 2>&1); RC=$?
check "2.1 unknown net" "$OUT" refused "$RC" "$OUT" "not found|no .*net|unknown"
OUT=$(lager supply "$NET" state --box NO-SUCH-BOX-XYZ 2>&1); RC=$?
check "2.2 unknown box" "$OUT" refused "$RC" "$OUT" "box|not found|don't have"
OUT=$(sup voltage -1 --yes); RC=$?
check "2.3 -1 V" "$OUT" refused "$RC" "$OUT" "positive|negative|no such option"
OUT=$(sup voltage abc --yes); RC=$?
check "2.4 non-numeric voltage" "$OUT" refused "$RC" "$OUT" "invalid|not a valid|float"
OUT=$(sup current -0.1 --yes); RC=$?
check "2.5 -0.1 A" "$OUT" refused "$RC" "$OUT" "positive|negative|no such option"
OUT=$(sup voltage 100.5 --yes); RC=$?
check "2.6 100.5 V (above any supported supply)" "$OUT" refused "$RC" "$OUT" "exceeds"
OVER_V=$(above "$VMAX" 0.5)
OUT=$(sup voltage "$OVER_V" --yes); RC=$?
check "2.7 $OVER_V V (above the ${VMAX} V rating)" "$OUT" refused "$RC" "$OUT" "exceeds hardware limit"
OVER_I=$(above "$IMAX" 0.1)
OUT=$(sup current "$OVER_I" --yes); RC=$?
check "2.8 $OVER_I A (above the ${IMAX} A rating)" "$OUT" refused "$RC" "$OUT" "exceeds hardware limit"
OUT=$(sup voltage 1.0 --ocp "$OVER_I" --yes); RC=$?
check "2.9 OCP $OVER_I A (above the rating)" "$OUT" refused "$RC" "$OUT" "exceeds"
OUT=$(sup voltage 5.0 --ovp 4.0 --yes); RC=$?
check "2.10 OVP 4.0 V below a 5.0 V setpoint" "$OUT" refused "$RC" "$OUT" "cannot be less than"
OUT=$(sup current 0.5 --ocp 0.2 --yes); RC=$?
check "2.11 OCP 0.2 A below a 0.5 A setpoint" "$OUT" refused "$RC" "$OUT" "cannot be less than"
check "2.12 the channel is unchanged: OFF, 1.0 V / 0.1 A, same OVP and OCP" "$(state_line)" \
  bash -c '[ "$1" = OFF ] && [ "$4" = "$5" ] && [ "$6" = "$7" ] && python3 -c "import sys; sys.exit(abs(float(sys.argv[1]) - 1.0) > 0.002 or abs(float(sys.argv[2]) - 0.1) > 0.002)" "$2" "$3"' \
  _ "$(f output)" "$(f set_v)" "$(f set_i)" "$(f ovp)" "$(field "$S0" ovp)" "$(f ocp)" "$(field "$S0" ocp)"

# ============================================================
# SECTION 3: SETPOINTS
# ============================================================
start_section "Setpoints"

# ok <command...>: prints "ok" when the command succeeds, for checks that
# combine several readings.
ok() { "$@" && echo ok; }

n=1
for v in 0.0 3.3 5.0 3.141; do
  OUT=$(sup voltage "$v" --yes); RC=$?
  check "3.$n voltage $v V reads back" "$OUT | $(state_line)" \
    [ "$([ "$RC" -eq 0 ] && ok set_v_is "$v")" = ok ]
  n=$((n + 1))
done
for i in 0.05 0.2 0.5; do
  OUT=$(sup current "$i" --yes); RC=$?
  check "3.$n current $i A reads back" "$OUT | $(state_line)" \
    [ "$([ "$RC" -eq 0 ] && ok set_i_is "$i")" = ok ]
  n=$((n + 1))
done

BAD=0
for v in 0.5 1.0 1.5 2.0 2.5 3.0 3.5 4.0 4.5 5.0; do
  sup voltage "$v" --yes >/dev/null || BAD=$((BAD + 1))
  set_v_is "$v" || BAD=$((BAD + 1))
done
check "3.$n ten consecutive setpoints each read back" "$BAD wrong" [ "$BAD" -eq 0 ]

# ============================================================
# SECTION 4: OUTPUT
# ============================================================
start_section "Output"

sup voltage 2.0 --yes >/dev/null; sup current 0.2 --yes >/dev/null
check "4.1 measures ~0 V while OFF" "$(state_line)" near "$(f meas_v)" 0 "$TOL"
OUT=$(sup enable --yes); RC=$?
check "4.2 enable" "$OUT" succeeded "$RC" "$OUT"
check "4.3 state reads ON" "$(state_line)" [ "$(f output)" = ON ]
check "4.4 measures 2.0 V" "$(state_line)" settles_at 2.0
check "4.5 unloaded output draws ~0 A" "$(state_line)" near "$(f meas_i)" 0 0.01

BAD=""
for v in 0.0 1.0 3.3 5.0 1.0; do
  sup voltage "$v" --yes >/dev/null
  settles_at "$v" || BAD="$BAD $v"
done
check "4.6 a live output follows 0 -> 1 -> 3.3 -> 5 -> 1 V" "missed:$BAD" [ -z "$BAD" ]
check "4.7 still ON after the voltage changes" "$(state_line)" [ "$(f output)" = ON ]

OUT=$(sup disable --yes); RC=$?
check "4.8 disable" "$OUT" succeeded "$RC" "$OUT"
check "4.9 state reads OFF" "$(state_line)" [ "$(f output)" = OFF ]
check "4.10 measured voltage falls to ~0 V" "$(state_line)" settles_at 0

BAD=0
for _ in 1 2 3 4 5; do
  sup enable --yes >/dev/null || BAD=$((BAD + 1))
  [ "$(f output)" = ON ] || BAD=$((BAD + 1))
  sup disable --yes >/dev/null || BAD=$((BAD + 1))
  [ "$(f output)" = OFF ] || BAD=$((BAD + 1))
done
check "4.11 five enable/disable cycles, each read back" "$BAD wrong" [ "$BAD" -eq 0 ]

# ============================================================
# SECTION 5: PROTECTION
# ============================================================
start_section "Protection"

OUT=$(sup voltage 3.0 --ovp 4.0 --yes); RC=$?
check "5.1 voltage 3.0 V with OVP 4.0 V" "$OUT" succeeded "$RC" "$OUT"
check "5.2 OVP reads 4.0 V, setpoint 3.0 V" "$(state_line)" \
  [ "$(near "$(f ovp)" 4.0 0.002 && ok set_v_is 3.0)" = ok ]
OUT=$(sup voltage 4.5 --yes); RC=$?
check "5.3 4.5 V is refused above the 4.0 V OVP" "$OUT" refused "$RC" "$OUT" "exceed|OVP"
check "5.4 setpoint still 3.0 V" "$(state_line)" set_v_is 3.0
OUT=$(sup current 0.1 --ocp 0.3 --yes); RC=$?
check "5.5 current 0.1 A with OCP 0.3 A" "$OUT" succeeded "$RC" "$OUT"
check "5.6 OCP reads 0.3 A" "$(state_line)" near "$(f ocp)" 0.3 0.002
OUT=$(sup clear-ovp); RC=$?
check "5.7 clear-ovp" "$OUT" succeeded "$RC" "$OUT"
OUT=$(sup clear-ocp); RC=$?
check "5.8 clear-ocp" "$OUT" succeeded "$RC" "$OUT"
check "5.9 clearing leaves 3.0 V / 0.1 A, OVP 4.0 V, OCP 0.3 A" "$(state_line)" \
  [ "$(set_v_is 3.0 && set_i_is 0.1 && near "$(f ovp)" 4.0 0.002 && ok near "$(f ocp)" 0.3 0.002)" = ok ]
OUT=$(sup set); RC=$?
check "5.10 set (supply mode)" "$OUT" succeeded "$RC" "$OUT"
check "5.11 setpoints survive set" "$(state_line)" \
  [ "$(set_v_is 3.0 && ok set_i_is 0.1)" = ok ]

# ============================================================
# SECTION 6: RESTORE
# ============================================================
start_section "Restore"

restore_supply
_HARNESS_CLEANUP=()
A0=$(state_line)
echo "After:  $A0"
check "6.1 back to its starting state" "$A0" \
  [ "$(field "$A0" output)/$(field "$A0" set_v)/$(field "$A0" set_i)/$(field "$A0" ocp)/$(field "$A0" ovp)" = "$(field "$S0" output)/$(field "$S0" set_v)/$(field "$S0" set_i)/$(field "$S0" ocp)/$(field "$S0" ovp)" ]

print_summary
exit_with_status
