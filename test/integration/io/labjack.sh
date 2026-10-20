#!/bin/bash

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
#
# LabJack GPIO, DAC and ADC through the CLI (gpo, gpi, dac, adc).
#
# Values are read from each command's --json output and compared with what
# was set. When the DAC net is wired to the ADC net (LABJACK_LOOPBACK=1), every
# DAC setpoint is also measured on the ADC.
#
# Usage: labjack.sh <BOX> <GPIO_NET> <ADC_NET> <DAC_NET>
#
# Environment:
#   LABJACK_LOOPBACK=1       DAC_NET is wired to ADC_NET (a bench fixture).
#   LABJACK_LOOPBACK_TOL     Allowed DAC-to-ADC error in volts (default 0.05).
#   LAGER_PROTECTED_NETS     Nets this suite refuses to drive
#                            (default: RIGOL_POWER KEITHLEY_POWER, the relays
#                            that switch bench instrument mains power).

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/../../framework/colors.sh"
source "${SCRIPT_DIR}/../../framework/harness.sh"

set +e

if [ $# -lt 4 ]; then
  echo "Usage: $0 <BOX> <GPIO_NET> <ADC_NET> <DAC_NET>"
  exit 1
fi

BOX="$1"
GPIO_NET="$2"
ADC_NET="$3"
DAC_NET="$4"
LOOPBACK="${LABJACK_LOOPBACK:-0}"
TOL="${LABJACK_LOOPBACK_TOL:-0.05}"
PROTECTED="${LAGER_PROTECTED_NETS:-RIGOL_POWER KEITHLEY_POWER}"

for n in $PROTECTED; do
  if [ "$GPIO_NET" = "$n" ] || [ "$DAC_NET" = "$n" ]; then
    echo "Refusing to drive protected net '$n'." >&2
    exit 2
  fi
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

# Print field $1 of the JSON object on stdin, or nothing.
json_field() {
  python3 -c '
import json, sys
try:
    v = json.load(sys.stdin).get(sys.argv[1])
except Exception:
    sys.exit(0)
if v is not None:
    print(v)
' "$1"
}

# Exit 0 when |$1 - $2| <= $3 (all numbers; empty fails).
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

dac_set()  { lager dac "$DAC_NET" "$1" --json --box "$BOX" 2>/dev/null | json_field voltage; }
dac_read() { lager dac "$DAC_NET" --json --box "$BOX" 2>/dev/null | json_field voltage; }
adc_read() { lager adc "$ADC_NET" --json --box "$BOX" 2>/dev/null | json_field voltage; }
gpo_set()  { lager gpo "$GPIO_NET" "$1" --json --box "$BOX" 2>/dev/null; }

restore() {
  lager dac "$DAC_NET" 0 --box "$BOX" >/dev/null 2>&1
  lager gpo "$GPIO_NET" low --box "$BOX" >/dev/null 2>&1
}
harness_add_cleanup restore

echo "========================================================================"
echo "LAGER LABJACK SUITE (gpo / gpi / dac / adc)"
echo "========================================================================"
echo "Box: $BOX   GPIO: $GPIO_NET   ADC: $ADC_NET   DAC: $DAC_NET"
echo "Loopback DAC->ADC: $LOOPBACK (tolerance ${TOL} V)"
echo ""

# ============================================================
# SECTION 1: LISTING
# ============================================================
start_section "Listing"

for pair in "gpo:$GPIO_NET" "adc:$ADC_NET" "dac:$DAC_NET"; do
  cmd="${pair%%:*}"; net="${pair#*:}"
  OUT=$(lager "$cmd" --box "$BOX" 2>&1); RC=$?
  check "1.x 'lager $cmd' lists $net" "$OUT" \
    bash -c '[ "$1" -eq 0 ] && printf "%s" "$2" | grep -qE "(^|[[:space:]])$3([[:space:]]|$)"' _ "$RC" "$OUT" "$net"
done

# ============================================================
# SECTION 2: REFUSALS (rejected before any output changes)
# ============================================================
start_section "Refusals"

dac_set 1.0 >/dev/null
BEFORE=$(dac_read)

OUT=$(lager dac "$DAC_NET" 5.5 --box "$BOX" 2>&1); RC=$?
check "2.1 dac 5.5 V is refused" "$OUT" refused "$RC" "$OUT" "between 0.0 and 5.0"
OUT=$(lager dac "$DAC_NET" -0.1 --box "$BOX" 2>&1); RC=$?
check "2.2 dac -0.1 V is refused" "$OUT" refused "$RC" "$OUT" "between 0.0 and 5.0|no such option|invalid"
OUT=$(lager dac "$DAC_NET" abc --box "$BOX" 2>&1); RC=$?
check "2.3 dac 'abc' is refused" "$OUT" refused "$RC" "$OUT" "invalid|not a valid|number|voltage"
check "2.4 DAC unchanged by the refusals" "before=$BEFORE now=$(dac_read)" near "$(dac_read)" "$BEFORE" 0.01

OUT=$(lager gpo "$GPIO_NET" sideways --box "$BOX" 2>&1); RC=$?
check "2.5 gpo level 'sideways' is refused" "$OUT" refused "$RC" "$OUT" "invalid|level|choose"
OUT=$(lager gpo "$ADC_NET" high --box "$BOX" 2>&1); RC=$?
check "2.6 gpo on the ADC net is refused" "$OUT" refused "$RC" "$OUT" "not found"
OUT=$(lager dac "$ADC_NET" 1.0 --box "$BOX" 2>&1); RC=$?
check "2.7 dac on the ADC net is refused" "$OUT" refused "$RC" "$OUT" "not found"
OUT=$(lager adc "$GPIO_NET" --box "$BOX" 2>&1); RC=$?
check "2.8 adc on the GPIO net is refused" "$OUT" refused "$RC" "$OUT" "not found"
OUT=$(lager adc ci_no_such_adc --box "$BOX" 2>&1); RC=$?
check "2.9 unknown ADC net is refused" "$OUT" refused "$RC" "$OUT" "not found"

# ============================================================
# SECTION 3: DAC SET AND READBACK
# ============================================================
start_section "DAC"

# 20 mV: a T7 DAC reads back about 15 mV short at its 5.0 V top.
for v in 0.0 0.5 1.25 2.5 3.3 5.0; do
  SET=$(dac_set "$v")
  READ=$(dac_read)
  check "3.x dac $v V: set reports $v, readback within 20 mV" "set=$SET read=$READ" \
    bash -c 'python3 -c "import sys; sys.exit(abs(float(sys.argv[1])-float(sys.argv[2]))>1e-9)" "$1" "$2" 2>/dev/null &&
             python3 -c "import sys; sys.exit(abs(float(sys.argv[1])-float(sys.argv[2]))>0.02)" "$3" "$2" 2>/dev/null' \
    _ "$SET" "$v" "$READ"
done

# ============================================================
# SECTION 4: DAC -> ADC LOOPBACK
# ============================================================
start_section "Loopback"

if [ "$LOOPBACK" = "1" ]; then
  for v in 0.0 0.5 1.25 2.5 3.3 4.5; do
    dac_set "$v" >/dev/null
    sleep 0.2
    A=$(adc_read)
    check "4.x dac $v V reads ${A:-nothing} V on $ADC_NET (+/- $TOL)" "adc=$A" near "$A" "$v" "$TOL"
  done

  dac_set 2.0 >/dev/null
  sleep 0.2
  READS=""
  for _ in 1 2 3 4 5 6 7 8; do READS="$READS $(adc_read)"; done
  check "4.y eight reads at 2.0 V all within tolerance" "$READS" \
    python3 -c '
import sys
vals = [float(x) for x in sys.argv[2].split()]
sys.exit(0 if len(vals) == 8 and all(abs(v - 2.0) <= float(sys.argv[1]) for v in vals) else 1)
' "$TOL" "$READS"
else
  A=$(adc_read)
  check "4.0 adc reads a number in the +/-10 V range" "adc=$A" near "${A:-x}" 0 10.5
fi

# ============================================================
# SECTION 5: GPIO OUTPUT
# ============================================================
start_section "GPIO Output"

OUT=$(gpo_set high)
check "5.1 gpo high reports value 1" "$OUT" [ "$(printf '%s' "$OUT" | json_field value)" = "1" ]
OUT=$(gpo_set low)
check "5.2 gpo low reports value 0" "$OUT" [ "$(printf '%s' "$OUT" | json_field value)" = "0" ]
OUT=$(gpo_set 1)
check "5.3 gpo 1 reports value 1" "$OUT" [ "$(printf '%s' "$OUT" | json_field value)" = "1" ]
OUT=$(gpo_set off)
check "5.4 gpo off reports value 0" "$OUT" [ "$(printf '%s' "$OUT" | json_field value)" = "0" ]

# From low, toggles must report 1,0,1,0,... and say they toggled.
BAD=0
EXPECT=1
for _ in 1 2 3 4 5 6 7 8 9 10; do
  OUT=$(gpo_set toggle)
  if [ "$(printf '%s' "$OUT" | json_field value)" != "$EXPECT" ] ||
     [ "$(printf '%s' "$OUT" | json_field toggled)" != "True" ]; then
    BAD=$((BAD + 1))
  fi
  EXPECT=$((1 - EXPECT))
done
check "5.5 ten toggles alternate from low" "$BAD wrong" [ "$BAD" -eq 0 ]

# ============================================================
# SECTION 6: GPIO INPUT
# ============================================================
start_section "GPIO Input"

# Reading a LabJack pin makes it an input, so the level is not asserted;
# the read must succeed and its two fields must agree.
OUT=$(lager gpi "$GPIO_NET" --json --box "$BOX" 2>&1); RC=$?
VAL=$(printf '%s' "$OUT" | json_field value)
LVL=$(printf '%s' "$OUT" | json_field level)
check "6.1 gpi reads 0 or 1 with a matching level" "$OUT" \
  bash -c '[ "$1" -eq 0 ] && { [ "$2/$3" = "0/LOW" ] || [ "$2/$3" = "1/HIGH" ]; }' _ "$RC" "$VAL" "$LVL"

OUT=$(lager gpi "$GPIO_NET" --box "$BOX" 2>&1); RC=$?
check "6.2 gpi text output names the net and level" "$OUT" \
  bash -c '[ "$1" -eq 0 ] && printf "%s" "$2" | grep -qE "$3.*(HIGH|LOW)"' _ "$RC" "$OUT" "$GPIO_NET"

# ============================================================
# SECTION 7: RESTORE
# ============================================================
start_section "Restore"

restore
check "7.1 DAC back at 0 V" "$(dac_read)" near "$(dac_read)" 0 0.01
OUT=$(gpo_set low)
check "7.2 GPIO left low" "$OUT" [ "$(printf '%s' "$OUT" | json_field value)" = "0" ]

print_summary
exit_with_status
