#!/bin/bash

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
#
# MCC USB-202 ADC, DAC and GPIO through the CLI (adc, dac, gpo, gpi).
#
# Values are read from each command's --json output. When a supply channel is
# wired to one of the ADC inputs (USB202_SUPPLY_NET + USB202_SUPPLY_ADC_NET, a
# bench fixture), the supply is set to a few voltages and each one is measured
# on that ADC. The supply's setpoints and output state are put back at the end;
# its OVP and OCP are never touched.
#
# Run this before anything power-cycles the USB-202's hub port: after a port
# cycle the box's hardware service no longer finds the device until it
# restarts (issue #729), and every check here would fail for that reason.
#
# Usage: usb202.sh <BOX> <ADC_NETS> <DAC_NETS> <GPIO_NETS>   (comma-separated)
#
# Environment:
#   USB202_SUPPLY_NET       supply net wired to USB202_SUPPLY_ADC_NET
#   USB202_SUPPLY_ADC_NET   the ADC net it is wired to
#   USB202_ADC_TOL          fixed part of the allowed supply-to-ADC error, in
#                           volts (default 0.05)
#   USB202_ADC_GAIN_TOL     proportional part, as a fraction of the setpoint
#                           (default 0.03). The bench's USB-202 reads about 2.3%
#                           high through the fixture wire at 3.3 V and 5 V.
#   LAGER_PROTECTED_NETS    nets this suite refuses to drive
#                           (default: RIGOL_POWER KEITHLEY_POWER)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/../../framework/colors.sh"
source "${SCRIPT_DIR}/../../framework/harness.sh"

set +e

if [ $# -lt 4 ]; then
  echo "Usage: $0 <BOX> <ADC_NETS> <DAC_NETS> <GPIO_NETS>   (comma-separated)"
  exit 1
fi

BOX="$1"
IFS=, read -r -a ADCS <<< "$2"
IFS=, read -r -a DACS <<< "$3"
IFS=, read -r -a GPIOS <<< "$4"
SUPPLY="${USB202_SUPPLY_NET:-}"
SUPPLY_ADC="${USB202_SUPPLY_ADC_NET:-}"
TOL="${USB202_ADC_TOL:-0.05}"
GAIN_TOL="${USB202_ADC_GAIN_TOL:-0.03}"
PROTECTED="${LAGER_PROTECTED_NETS:-RIGOL_POWER KEITHLEY_POWER}"

if [ "${#ADCS[@]}" -eq 0 ] || [ "${#DACS[@]}" -eq 0 ] || [ "${#GPIOS[@]}" -eq 0 ]; then
  echo "Each of ADC_NETS, DAC_NETS and GPIO_NETS needs at least one net." >&2
  exit 1
fi
if { [ -n "$SUPPLY" ] && [ -z "$SUPPLY_ADC" ]; } || { [ -z "$SUPPLY" ] && [ -n "$SUPPLY_ADC" ]; }; then
  echo "Set both USB202_SUPPLY_NET and USB202_SUPPLY_ADC_NET, or neither." >&2
  exit 1
fi
for n in $PROTECTED; do
  for g in "${GPIOS[@]}" "${DACS[@]}"; do
    if [ "$g" = "$n" ]; then
      echo "Refusing to drive protected net '$n'." >&2
      exit 2
    fi
  done
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

# Exit 0 when $1 is listed by `lager $2 --box`.
listed() {
  lager "$2" --box "$BOX" 2>/dev/null | grep -qE "(^|[[:space:]])$1([[:space:]]|$)"
}

adc_read() { lager adc "$1" --json --box "$BOX" 2>/dev/null | json_field voltage; }
gpo_set()  { lager gpo "$1" "$2" --json --box "$BOX" 2>/dev/null | json_field value; }

# Supply state line field: output, set_v or set_i.
supply_field() {
  lager supply "$SUPPLY" state --box "$BOX" 2>&1 | python3 -c '
import re, sys
m = re.search(r"Channel \d+: (\w+), Set: ([-\d.]+)V/([-\d.]+)A", sys.stdin.read())
if m:
    print(m.group(["output", "set_v", "set_i"].index(sys.argv[1]) + 1))
' "$1"
}

SUPPLY_V0=""; SUPPLY_I0=""; SUPPLY_OUT0=""
restore() {
  local d g
  for d in "${DACS[@]}"; do lager dac "$d" 0 --box "$BOX" >/dev/null 2>&1; done
  for g in "${GPIOS[@]}"; do lager gpo "$g" low --box "$BOX" >/dev/null 2>&1; done
  if [ -n "$SUPPLY_V0" ]; then
    lager supply "$SUPPLY" disable --yes --box "$BOX" >/dev/null 2>&1
    lager supply "$SUPPLY" voltage "$SUPPLY_V0" --yes --box "$BOX" >/dev/null 2>&1
    lager supply "$SUPPLY" current "$SUPPLY_I0" --yes --box "$BOX" >/dev/null 2>&1
    [ "$SUPPLY_OUT0" = ON ] && lager supply "$SUPPLY" enable --yes --box "$BOX" >/dev/null 2>&1
  fi
}

echo "========================================================================"
echo "LAGER MCC USB-202 SUITE (adc / dac / gpo / gpi)"
echo "========================================================================"
echo "Box: $BOX"
echo "ADC: ${ADCS[*]}   DAC: ${DACS[*]}   GPIO: ${GPIOS[*]}"
echo "Supply fixture: ${SUPPLY:-none}${SUPPLY:+ -> $SUPPLY_ADC (tolerance $TOL V + $GAIN_TOL of the setpoint)}"
echo ""

# ============================================================
# SECTION 1: LISTING
# ============================================================
start_section "Listing"

MISSING=""
for n in "${ADCS[@]}"; do listed "$n" adc || MISSING="$MISSING adc:$n"; done
for n in "${DACS[@]}"; do listed "$n" dac || MISSING="$MISSING dac:$n"; done
for n in "${GPIOS[@]}"; do listed "$n" gpo || MISSING="$MISSING gpo:$n"; done
check "1.1 every net is listed by its command" "missing:$MISSING" [ -z "$MISSING" ]
INST=$(lager nets show "${ADCS[0]}" --box "$BOX" --json 2>/dev/null | json_field instrument)
check "1.2 ${ADCS[0]} is a USB-202 net" "instrument=$INST" bash -c 'printf "%s" "$1" | grep -qi "usb.\?202"' _ "$INST"

# ============================================================
# SECTION 2: REFUSALS
# ============================================================
start_section "Refusals"

D="${DACS[0]}"; G="${GPIOS[0]}"; A="${ADCS[0]}"
OUT=$(lager dac "$D" 5.5 --box "$BOX" 2>&1); RC=$?
check "2.1 dac 5.5 V is refused" "$OUT" refused "$RC" "$OUT" "between 0.0 and 5.0"
OUT=$(lager dac "$D" -0.1 --box "$BOX" 2>&1); RC=$?
check "2.2 dac -0.1 V is refused" "$OUT" refused "$RC" "$OUT" "between 0.0 and 5.0|no such option|invalid"
OUT=$(lager dac "$D" abc --box "$BOX" 2>&1); RC=$?
check "2.3 dac 'abc' is refused" "$OUT" refused "$RC" "$OUT" "not a valid"
OUT=$(lager gpo "$G" sideways --box "$BOX" 2>&1); RC=$?
check "2.4 gpo level 'sideways' is refused" "$OUT" refused "$RC" "$OUT" "invalid|level|choose"
OUT=$(lager gpo "$A" high --box "$BOX" 2>&1); RC=$?
check "2.5 gpo on an ADC net is refused" "$OUT" refused "$RC" "$OUT" "not found"
OUT=$(lager adc "$G" --box "$BOX" 2>&1); RC=$?
check "2.6 adc on a GPIO net is refused" "$OUT" refused "$RC" "$OUT" "not found"
# The USB-202 cannot read its DAC back. That must be an error that says so,
# not a made-up value.
OUT=$(lager dac "$D" --box "$BOX" 2>&1); RC=$?
check "2.7 dac read is refused as unsupported" "$OUT" refused "$RC" "$OUT" "readback|not support"

# ============================================================
# SECTION 3: DAC
# ============================================================
start_section "DAC"

harness_add_cleanup restore
BAD=""
for d in "${DACS[@]}"; do
  for v in 0.0 1.25 2.5 5.0 0.0; do
    SET=$(lager dac "$d" "$v" --json --box "$BOX" 2>/dev/null | json_field voltage)
    near "$SET" "$v" 1e-9 || BAD="$BAD $d@$v=${SET:-none}"
  done
done
check "3.1 every DAC accepts 0 / 1.25 / 2.5 / 5.0 / 0 V and reports it" "wrong:$BAD" [ -z "$BAD" ]

# ============================================================
# SECTION 4: ADC
# ============================================================
start_section "ADC"

BAD=""
for a in "${ADCS[@]}"; do
  V=$(adc_read "$a")
  near "${V:-x}" 0 10.5 || BAD="$BAD $a=${V:-none}"
done
check "4.1 every ADC reads a number in the +/-10 V range" "bad:$BAD" [ -z "$BAD" ]

READS=""
for _ in 1 2 3 4 5; do READS="$READS $(adc_read "$A")"; done
check "4.2 five reads of $A all return a value" "$READS" \
  python3 -c 'import sys; v = sys.argv[1].split(); sys.exit(0 if len(v) == 5 and all(abs(float(x)) <= 10.5 for x in v) else 1)' "$READS"

if [ -n "$SUPPLY" ]; then
  SUPPLY_V0=$(supply_field set_v); SUPPLY_I0=$(supply_field set_i); SUPPLY_OUT0=$(supply_field output)
  check "4.3 $SUPPLY state recorded" "$SUPPLY_V0/$SUPPLY_I0/$SUPPLY_OUT0" \
    bash -c '[ -n "$1" ] && [ -n "$2" ] && [ -n "$3" ]' _ "$SUPPLY_V0" "$SUPPLY_I0" "$SUPPLY_OUT0"
  if [ -n "$SUPPLY_V0" ] && [ -n "$SUPPLY_I0" ]; then
    lager supply "$SUPPLY" disable --yes --box "$BOX" >/dev/null 2>&1
    lager supply "$SUPPLY" current 0.1 --yes --box "$BOX" >/dev/null 2>&1
    lager supply "$SUPPLY" voltage 0 --yes --box "$BOX" >/dev/null 2>&1
    sleep 1
    V=$(adc_read "$SUPPLY_ADC")
    check "4.4 $SUPPLY_ADC reads ~0 V with $SUPPLY off" "adc=$V" near "$V" 0 "$TOL"
    lager supply "$SUPPLY" enable --yes --box "$BOX" >/dev/null 2>&1
    for v in 1.0 3.3 5.0; do
      VTOL=$(python3 -c 'import sys; print(float(sys.argv[1]) + float(sys.argv[2]) * float(sys.argv[3]))' "$TOL" "$GAIN_TOL" "$v")
      lager supply "$SUPPLY" voltage "$v" --yes --box "$BOX" >/dev/null 2>&1
      # The output slews; take the first read inside tolerance within 5 s.
      V=""
      for _ in 1 2 3 4 5 6 7 8 9 10; do
        V=$(adc_read "$SUPPLY_ADC")
        near "$V" "$v" "$VTOL" && break
        sleep 0.5
      done
      check "4.x $SUPPLY at $v V reads ${V:-nothing} V on $SUPPLY_ADC (+/- $VTOL)" "adc=$V" near "$V" "$v" "$VTOL"
    done
    lager supply "$SUPPLY" disable --yes --box "$BOX" >/dev/null 2>&1
  fi
fi

# ============================================================
# SECTION 5: GPIO
# ============================================================
start_section "GPIO"

BAD=""
for g in "${GPIOS[@]}"; do
  [ "$(gpo_set "$g" high)" = 1 ] || BAD="$BAD $g:high"
  [ "$(gpo_set "$g" low)" = 0 ] || BAD="$BAD $g:low"
done
check "5.1 every GPIO drives high then low" "wrong:$BAD" [ -z "$BAD" ]

BAD=0
EXPECT=1
gpo_set "$G" low >/dev/null
for _ in 1 2 3 4 5 6; do
  [ "$(gpo_set "$G" toggle)" = "$EXPECT" ] || BAD=$((BAD + 1))
  EXPECT=$((1 - EXPECT))
done
check "5.2 six toggles of $G alternate from low" "$BAD wrong" [ "$BAD" -eq 0 ]

# Reading a pin makes it an input, and nothing drives these pins, so the
# level is not asserted; the read must succeed and its two fields agree.
OUT=$(lager gpi "$G" --json --box "$BOX" 2>&1); RC=$?
VAL=$(printf '%s' "$OUT" | json_field value)
LVL=$(printf '%s' "$OUT" | json_field level)
check "5.3 gpi reads 0 or 1 with a matching level" "$OUT" \
  bash -c '[ "$1" -eq 0 ] && { [ "$2/$3" = "0/LOW" ] || [ "$2/$3" = "1/HIGH" ]; }' _ "$RC" "$VAL" "$LVL"

# ============================================================
# SECTION 6: RESTORE
# ============================================================
start_section "Restore"

restore
_HARNESS_CLEANUP=()
BAD=""
for g in "${GPIOS[@]}"; do [ "$(gpo_set "$g" low)" = 0 ] || BAD="$BAD $g"; done
check "6.1 every GPIO left low" "not low:$BAD" [ -z "$BAD" ]
if [ -n "$SUPPLY_V0" ]; then
  NOW="$(supply_field output)/$(supply_field set_v)/$(supply_field set_i)"
  check "6.2 $SUPPLY back to $SUPPLY_OUT0 $SUPPLY_V0 V / $SUPPLY_I0 A" "now $NOW" \
    [ "$NOW" = "$SUPPLY_OUT0/$SUPPLY_V0/$SUPPLY_I0" ]
fi

print_summary
exit_with_status
