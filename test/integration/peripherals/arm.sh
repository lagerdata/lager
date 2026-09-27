#!/bin/bash

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
# Hardware test suite for lager arm (Rotrics Dexarm).
#
# Usage: ./arm.sh <BOX_NAME_OR_IP> <ARM_NET> [USB_NET]
#
#   USB_NET  Optional usb hub net that carries the arm's USB cable. When given,
#            the reconnect test switches that hub port off and on again.
#
# THIS MOVES THE ARM. Every target stays within about 50 mm of home
# (X0 Y300 Z0). The script never runs read-and-save-position: that command
# sends M889, which overwrites the arm's stored calibration.
#
# The arm must run DexArm firmware V2.1.4 or later. Older firmware swaps the X
# and Y axes, and lager refuses moves on it.
#
# Optional environment:
#   ARM_ACCELERATION, ARM_TRAVEL, ARM_RETRACT
#       Values that set-acceleration writes (defaults 200, 200, 60: what a
#       Dexarm on Marlin 2.0.1 firmware reports). The CLI cannot read them back.
#   ARM_TEST_DISABLE_MOTOR=1
#       Also run disable-motor. The arm goes limp and can drop onto whatever is
#       below it, so this test is skipped unless you ask for it.

set +e  # DON'T exit on error - we want to track failures

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

source "${SCRIPT_DIR}/../../framework/colors.sh"
source "${SCRIPT_DIR}/../../framework/harness.sh"

init_harness

if [ $# -lt 2 ]; then
  echo "Usage: $0 <BOX_NAME_OR_IP> <ARM_NET> [USB_NET]"
  echo ""
  echo "Examples:"
  echo "  $0 my-box arm1"
  echo "  $0 my-box arm1 usb1     (also run the reconnect test on hub net usb1)"
  echo ""
  exit 1
fi

BOX_INPUT="$1"
ARM_NET="$2"
USB_NET="${3:-}"
ARM_ACCELERATION="${ARM_ACCELERATION:-200}"
ARM_TRAVEL="${ARM_TRAVEL:-200}"
ARM_RETRACT="${ARM_RETRACT:-60}"
TOL_MM=2

register_box_from_ip "$BOX_INPUT"

print_script_header "LAGER ARM TEST SUITE" "$BOX" "$ARM_NET"
echo "IMPORTANT: This test physically moves the arm."
echo "Clear the workspace within about 50 mm of home (X0 Y300 Z0) before you continue."
echo ""

# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

lager_arm() {
  lager arm "$ARM_NET" "$@" --box "$BOX"
}

# Print "X Y Z" from `lager arm <net> position`. Fails if the read fails or
# does not carry all three axes.
read_position() {
  local out pos
  out=$(lager_arm position 2>&1) || return 1
  pos=$(echo "$out" | awk '{for (i = 1; i <= NF; i++) if ($i == "X:" || $i == "Y:" || $i == "Z:") printf "%s ", $(i + 1)}')
  [ "$(echo "$pos" | wc -w | tr -d ' ')" -eq 3 ] || return 1
  echo "$pos"
}

# near X Y Z: succeed if the arm is within TOL_MM of the target on every axis.
near() {
  local pos x y z
  pos=$(read_position) || return 1
  read -r x y z <<< "$pos"
  awk -v x="$x" -v y="$y" -v z="$z" -v tx="$1" -v ty="$2" -v tz="$3" -v tol="$TOL_MM" \
    'function abs(v) { return v < 0 ? -v : v }
     BEGIN { exit !(abs(x - tx) <= tol && abs(y - ty) <= tol && abs(z - tz) <= tol) }'
}

# wait_near X Y Z SECONDS: poll until the arm is near the target.
wait_near() {
  local deadline=$((SECONDS + $4))
  while [ $SECONDS -lt $deadline ]; do
    near "$1" "$2" "$3" && return 0
    sleep 1
  done
  return 1
}

# expect_fail DESCRIPTION PATTERN COMMAND...: the command must exit non-zero and
# print PATTERN (case-insensitive).
expect_fail() {
  local desc="$1" pattern="$2" out rc
  shift 2
  out=$("$@" 2>&1)
  rc=$?
  if [ $rc -ne 0 ] && echo "$out" | grep -qi -- "$pattern"; then
    track_test_msg "pass" "$desc"
  else
    track_test_msg "fail" "$desc (exit $rc): $(echo "$out" | tail -2 | tr '\n' ' ')"
  fi
}

# ============================================================================
# SECTION 1: PRECONDITIONS AND CLI SURFACE (no motion)
# ============================================================================
print_section_header "SECTION 1: PRECONDITIONS AND CLI SURFACE"
start_section "Preconditions"

if lager arm --box "$BOX" 2>&1 | grep -q -- "$ARM_NET"; then
  track_test_msg "pass" "lager arm --box lists $ARM_NET"
else
  track_test_msg "fail" "lager arm --box does not list $ARM_NET"
fi

POS=$(read_position)
if [ -n "$POS" ]; then
  track_test_msg "pass" "position answers: $POS"
else
  track_test_msg "fail" "position did not answer with X, Y and Z"
fi

if lager arm --help 2>&1 | grep -q "Control robot arm"; then
  track_test_msg "pass" "lager arm --help"
else
  track_test_msg "fail" "lager arm --help text missing"
fi

# The recalibration gate must exist. This only reads the help text: running
# the command would send M889.
HELP=$(lager arm "$ARM_NET" read-and-save-position --help 2>&1)
if echo "$HELP" | grep -q -- "--yes" && echo "$HELP" | grep -q "M889"; then
  track_test_msg "pass" "read-and-save-position is gated behind --yes"
else
  track_test_msg "fail" "read-and-save-position has no --yes gate; do not run it"
fi
echo ""

# ============================================================================
# SECTION 2: ERROR CASES (no motion)
# ============================================================================
print_section_header "SECTION 2: ERROR CASES"
start_section "Error Cases"

expect_fail "unknown net is refused" "not found" \
  lager arm nonexistent_net position --box "$BOX"
expect_fail "move without --z is a usage error" "missing option" \
  lager_arm move --x 0 --y 300 --yes
expect_fail "move outside the workspace is refused" "out of bounds" \
  lager_arm move --x 500 --y 300 --z 0 --yes
expect_fail "move --timeout past the 25 s cap is refused" "timeout" \
  lager_arm move --x 0 --y 300 --z 0 --timeout 26 --yes
expect_fail "set-acceleration 0 is refused" "acceleration" \
  lager_arm set-acceleration --acceleration 0 --travel 10
echo ""

# ============================================================================
# SECTION 3: HOME
# ============================================================================
print_section_header "SECTION 3: HOME (X0 Y300 Z0)"
start_section "Home"

if lager_arm go-home --yes >/dev/null 2>&1; then
  track_test_msg "pass" "go-home exits 0"
else
  track_test_msg "fail" "go-home failed"
fi

# go-home must return with the arm at home, not while it is still travelling.
if near 0 300 0; then
  track_test_msg "pass" "arm is at home when go-home returns"
else
  track_test_msg "fail" "arm was not at home when go-home returned: $(read_position)"
fi

if wait_near 0 300 0 30; then
  track_test_msg "pass" "arm reaches home"
else
  track_test_msg "fail" "arm did not reach home within 30 s: $(read_position)"
fi
echo ""

# ============================================================================
# SECTION 4: ABSOLUTE MOVE
# ============================================================================
print_section_header "SECTION 4: ABSOLUTE MOVE"
start_section "Absolute Move"

if lager_arm move --x 50 --y 250 --z 30 --yes >/dev/null 2>&1 && near 50 250 30; then
  track_test_msg "pass" "move --x 50 --y 250 --z 30 arrives (within ${TOL_MM} mm)"
else
  track_test_msg "fail" "move to (50, 250, 30) did not arrive: $(read_position)"
fi

if lager_arm move --x 0 --y 300 --z 0 --yes >/dev/null 2>&1 && near 0 300 0; then
  track_test_msg "pass" "move back to home arrives"
else
  track_test_msg "fail" "move back to (0, 300, 0) did not arrive: $(read_position)"
fi
echo ""

# ============================================================================
# SECTION 5: RELATIVE MOVE
# ============================================================================
print_section_header "SECTION 5: RELATIVE MOVE"
start_section "Relative Move"

if lager_arm move-by --dz 10 --yes >/dev/null 2>&1 && near 0 300 10; then
  track_test_msg "pass" "move-by --dz 10 arrives"
else
  track_test_msg "fail" "move-by --dz 10 did not arrive at (0, 300, 10): $(read_position)"
fi

if lager_arm move-by --dz -10 --yes >/dev/null 2>&1 && near 0 300 0; then
  track_test_msg "pass" "move-by --dz -10 returns home"
else
  track_test_msg "fail" "move-by --dz -10 did not return home: $(read_position)"
fi

expect_fail "move-by past the workspace is refused" "out of bounds" \
  lager_arm move-by --dz 200 --yes
if near 0 300 0; then
  track_test_msg "pass" "a refused move-by does not move the arm"
else
  track_test_msg "fail" "arm moved after a refused move-by: $(read_position)"
fi
echo ""

# ============================================================================
# SECTION 6: ACCELERATION
# ============================================================================
print_section_header "SECTION 6: ACCELERATION"
start_section "Acceleration"

OUT=$(lager_arm set-acceleration --acceleration "$ARM_ACCELERATION" \
  --travel "$ARM_TRAVEL" --retract "$ARM_RETRACT" 2>&1)
if echo "$OUT" | grep -q "travel=$ARM_TRAVEL retract=$ARM_RETRACT"; then
  track_test_msg "pass" "set-acceleration $ARM_ACCELERATION/$ARM_TRAVEL/$ARM_RETRACT"
else
  track_test_msg "fail" "set-acceleration: $OUT"
fi
echo ""

# ============================================================================
# SECTION 7: MOTORS
# ============================================================================
print_section_header "SECTION 7: MOTORS"
start_section "Motors"

if lager_arm enable-motor >/dev/null 2>&1; then
  track_test_msg "pass" "enable-motor"
else
  track_test_msg "fail" "enable-motor failed"
fi

if [ "${ARM_TEST_DISABLE_MOTOR:-}" = "1" ]; then
  if lager_arm disable-motor >/dev/null 2>&1 && lager_arm enable-motor >/dev/null 2>&1; then
    track_test_msg "pass" "disable-motor then enable-motor"
  else
    track_test_msg "fail" "disable-motor / enable-motor failed"
  fi
  lager_arm go-home --yes >/dev/null 2>&1
else
  skip_test "disable-motor" "set ARM_TEST_DISABLE_MOTOR=1; the arm goes limp and can drop"
fi
echo ""

# ============================================================================
# SECTION 8: INSTRUMENT SCAN DURING ARM COMMANDS
# ============================================================================
print_section_header "SECTION 8: INSTRUMENT SCAN DURING ARM COMMANDS"
start_section "Concurrent Scan"

# The scan used to write M105 into the arm's open port and take its reply, so
# an arm command running at the same time failed.
FAIL_FILE=$(mktemp)
(
  fails=0
  for _ in $(seq 1 30); do
    read_position >/dev/null || fails=$((fails + 1))
  done
  echo "$fails" > "$FAIL_FILE"
) &
READER=$!
LISTED=0
for _ in 1 2 3; do
  lager instruments --box "$BOX" 2>&1 | grep -q "Rotrix_Dexarm" && LISTED=$((LISTED + 1))
done
wait "$READER"
FAILS=$(cat "$FAIL_FILE")
rm -f "$FAIL_FILE"

if [ "$FAILS" = "0" ]; then
  track_test_msg "pass" "30 position reads during 3 scans, 0 failures"
else
  track_test_msg "fail" "$FAILS of 30 position reads failed during scans"
fi
if [ "$LISTED" -eq 3 ]; then
  track_test_msg "pass" "arm listed in 3 of 3 scans"
else
  track_test_msg "fail" "arm listed in only $LISTED of 3 scans"
fi
echo ""

# ============================================================================
# SECTION 9: RECONNECT AFTER A USB DROP
# ============================================================================
print_section_header "SECTION 9: RECONNECT AFTER A USB DROP"
start_section "Reconnect"

if [ -z "$USB_NET" ]; then
  skip_test "reconnect" "pass a USB_NET that carries the arm's cable"
else
  read_position >/dev/null   # the box now holds the arm's port open
  lager usb "$USB_NET" disable --box "$BOX" >/dev/null 2>&1
  sleep 3
  if read_position >/dev/null; then
    lager usb "$USB_NET" enable --box "$BOX" >/dev/null 2>&1
    skip_test "reconnect" "position still answered with $USB_NET off; the hub port does not cut the arm's USB"
  else
    lager usb "$USB_NET" enable --box "$BOX" >/dev/null 2>&1
    if wait_near 0 300 0 20; then
      track_test_msg "pass" "arm answers again after $USB_NET is switched back on"
    else
      track_test_msg "fail" "arm did not answer within 20 s after $USB_NET came back"
    fi
  fi
fi
echo ""

# ============================================================================
# CLEANUP
# ============================================================================
print_section_header "CLEANUP"

echo "Returning the arm to home..."
lager_arm go-home --yes >/dev/null 2>&1
echo -e "${GREEN}[OK] Cleanup complete${NC}"
echo ""

print_summary
exit_with_status
