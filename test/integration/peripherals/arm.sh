#!/bin/bash

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
# Hardware test suite for lager arm (Rotrics Dexarm).
#
# Usage: ./arm.sh <BOX_NAME_OR_IP> <ARM_NET> [USB_NET]
#
#   USB_NET  Optional usb hub net that carries the arm's USB cable. When given,
#            the reconnect section switches that hub port off and on again.
#            A self-powered arm keeps its pose; only its USB link drops.
#
# THIS MOVES THE ARM. Every target stays inside X -100..100, Y 200..320,
# Z 0..40 (mm), around home (X0 Y300 Z0). Clear that space first.
#
# The script never recalibrates the arm. It runs read-and-save-position only
# to decline its prompt, and checks that the arm's stored calibration (the
# M889 line of its M503 settings report) is byte-identical at the end.
#
# The arm must run DexArm firmware V2.1.4 or later. Older firmware swaps the X
# and Y axes, and lager refuses moves on it.
#
# Optional environment:
#   ARM_ACCELERATION, ARM_TRAVEL, ARM_RETRACT
#       Values set-acceleration writes and must read back (defaults 61, 62, 43:
#       unlike any firmware default, so a write that did not land is caught).
#       The arm's own values are read first and restored at exit.
#   ARM_TRAVERSE_X
#       Half-width of the X traverse the move-timeout check makes at Y300 Z30
#       (default 100, so a 200 mm move).
#   ARM_TEST_DISABLE_MOTOR=1, ARM_DROP_POSE="X Y Z"
#       Also run disable-motor from ARM_DROP_POSE (default home). The arm goes
#       limp and drops onto whatever is below that pose.

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
  echo "  $0 my-box arm1 usb5     (also run the reconnect section on hub net usb5)"
  echo ""
  exit 1
fi

BOX_INPUT="$1"
ARM_NET="$2"
USB_NET="${3:-}"
ARM_ACCELERATION="${ARM_ACCELERATION:-61}"
ARM_TRAVEL="${ARM_TRAVEL:-62}"
ARM_RETRACT="${ARM_RETRACT:-43}"
ARM_TRAVERSE_X="${ARM_TRAVERSE_X:-100}"
ARM_DROP_POSE="${ARM_DROP_POSE:-0 300 0}"
TOL_MM=2
# A disconnected arm must fail a command within this many seconds, with the
# arm's own error rather than hardware_service's "did not respond".
SILENT_ARM_BOUND_S=25

register_box_from_ip "$BOX_INPUT"

print_script_header "LAGER ARM TEST SUITE" "$BOX" "$ARM_NET"
echo "IMPORTANT: This test physically moves the arm."
echo "Clear X -100..100, Y 200..320, Z 0..40 mm around home (X0 Y300 Z0) before you continue."
echo ""

# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

lager_arm() {
  lager arm "$ARM_NET" "$@" --box "$BOX"
}

# bounded SECONDS COMMAND...: run COMMAND, killing it after SECONDS. Returns
# the command's exit status (143 when it was killed). Portable: macOS has no
# timeout(1).
#
# The watcher kills its own sleep when it is stopped, and writes nowhere. A
# plain `( sleep N; kill ) &` leaves the sleep running when the watcher is
# killed, and inside `$(...)` that sleep holds the output open, so every
# bounded command took the full N seconds.
bounded() {
  local secs="$1" pid watcher rc
  shift
  "$@" &
  pid=$!
  (
    s=
    trap 'kill "$s" 2>/dev/null; exit 0' TERM
    sleep "$secs" &
    s=$!
    wait "$s"
    kill -TERM "$pid" 2>/dev/null
  ) >/dev/null 2>&1 &
  watcher=$!
  wait "$pid"
  rc=$?
  kill "$watcher" 2>/dev/null
  wait "$watcher" 2>/dev/null
  return "$rc"
}

# Print "X Y Z" from `lager arm <net> position`. Fails if the read fails or
# does not carry three numbers.
read_position() {
  local out
  out=$(bounded 45 lager arm "$ARM_NET" position --box "$BOX" 2>&1) || return 1
  parse_position "$out"
}

# parse_position TEXT: print "X Y Z" from "X: <n> Y: <n> Z: <n>", or fail.
parse_position() {
  echo "$1" | awk '
    { for (i = 1; i <= NF; i++) if ($i == "X:" || $i == "Y:" || $i == "Z:") v[$i] = $(i + 1) }
    END {
      if (!(("X:" in v) && ("Y:" in v) && ("Z:" in v))) exit 1
      for (k in v) if (v[k] !~ /^-?[0-9]+(\.[0-9]+)?$/) exit 1
      print v["X:"], v["Y:"], v["Z:"]
    }'
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

# Print "P T R" (print, travel, retract) from get-acceleration, or fail.
read_acceleration() {
  local out
  out=$(bounded 45 lager arm "$ARM_NET" get-acceleration --box "$BOX" 2>&1) || return 1
  echo "$out" | sed -n 's/.*print=\([0-9.]*\) travel=\([0-9.]*\) retract=\([0-9.]*\).*/\1 \2 \3/p' | grep .
}

# Print the arm's stored calibration line ("M889 X.. Y.. Z..") from its M503
# settings report, or fail. M503 only reads settings.
SETTINGS_DIR=$(mktemp -d)
harness_add_cleanup "rm -rf '$SETTINGS_DIR'"
cat > "$SETTINGS_DIR/arm_settings.py" <<'PYEOF'
import os

from lager import Net, NetType

arm = Net.get(os.environ["ARM_NET"], type=NetType.Arm)
try:
    print("\n".join(arm.read_settings()))
finally:
    arm.close()
PYEOF
read_calibration() {
  local out
  out=$(bounded 60 lager python "$SETTINGS_DIR/arm_settings.py" --box "$BOX" \
    --env "ARM_NET=$ARM_NET" 2>&1) || return 1
  echo "$out" | grep -o 'M889 X-\{0,1\}[0-9][0-9]* Y-\{0,1\}[0-9][0-9]* Z-\{0,1\}[0-9][0-9]*' | head -1 | grep .
}

# The arm's tty as the instrument scan lists it (e.g. /dev/ttyACM2), or "?".
arm_tty() {
  bounded 60 lager instruments --box "$BOX" 2>&1 \
    | awk '/Rotrix_Dexarm/ { for (i = 1; i <= NF; i++) if ($i ~ /^\/dev\//) { print $i; exit } }' \
    | grep . || echo "?"
}

# Cleanup runs at exit however the suite ends (a failed check, a step timeout,
# Ctrl+C): the hub port back on, motors on, the arm's own acceleration back,
# and the arm parked at home. Each step is bounded so a dead arm cannot hang it.
ACC0=""
cleanup_arm() {
  echo "Cleanup: re-enabling the arm and returning it home..."
  if [ -n "$USB_NET" ]; then
    bounded 30 lager usb "$USB_NET" enable --box "$BOX" >/dev/null 2>&1
  fi
  bounded 45 lager arm "$ARM_NET" enable-motor --box "$BOX" >/dev/null 2>&1
  if [ -n "$ACC0" ]; then
    local p t r
    read -r p t r <<< "$ACC0"
    bounded 45 lager arm "$ARM_NET" set-acceleration --acceleration "${p%.*}" \
      --travel "${t%.*}" --retract "${r%.*}" --box "$BOX" >/dev/null 2>&1
  fi
  bounded 45 lager arm "$ARM_NET" go-home --yes --box "$BOX" >/dev/null 2>&1
}
harness_add_cleanup cleanup_arm

# ============================================================================
# SECTION 1: PRECONDITIONS AND CLI SURFACE (no motion)
# ============================================================================
print_section_header "SECTION 1: PRECONDITIONS AND CLI SURFACE"
start_section "Preconditions"

# An unreachable arm ends the suite here, failed, instead of every later
# check waiting out its own timeout.
POS=$(read_position)
if [ -n "$POS" ]; then
  track_test_msg "pass" "arm answers a position read: $POS"
else
  track_test_msg "fail" "arm $ARM_NET did not answer a position read within 45 s; stopping"
  print_summary
  exit 1
fi

if lager arm --box "$BOX" 2>&1 | awk -v net="$ARM_NET" '$1 == net { found = 1 } END { exit !found }'; then
  track_test_msg "pass" "lager arm --box lists $ARM_NET"
else
  track_test_msg "fail" "lager arm --box does not list $ARM_NET"
fi

# The group-level --box form, with the net name after it.
if parse_position "$(lager arm --box "$BOX" "$ARM_NET" position 2>&1)" >/dev/null; then
  track_test_msg "pass" "lager arm --box BOX NET position reads the position"
else
  track_test_msg "fail" "lager arm --box BOX NET position did not read the position"
fi

# With the net name omitted the command runs on the default net, or asks for
# one. It used to fail to parse: "No such command '--box'".
OUT=$(lager arm position --box "$BOX" 2>&1)
if echo "$OUT" | grep -q "No such command"; then
  track_test_msg "fail" "lager arm position --box BOX does not parse: $OUT"
else
  track_test_msg "pass" "lager arm position --box BOX parses"
fi

if lager arm --help 2>&1 | grep -q "get-acceleration"; then
  track_test_msg "pass" "lager arm --help lists get-acceleration"
else
  track_test_msg "fail" "lager arm --help does not list get-acceleration"
fi

CAL0=$(read_calibration)
if [ -n "$CAL0" ]; then
  track_test_msg "pass" "stored calibration read: $CAL0"
else
  track_test_msg "fail" "could not read the stored calibration (M503)"
fi

ACC0=$(read_acceleration)
if [ -n "$ACC0" ]; then
  track_test_msg "pass" "acceleration read: print/travel/retract $ACC0"
else
  track_test_msg "fail" "get-acceleration did not report print, travel and retract"
fi
echo ""

# ============================================================================
# SECTION 2: ERROR CASES AND DECLINED PROMPTS (no motion)
# ============================================================================
print_section_header "SECTION 2: ERROR CASES AND DECLINED PROMPTS"
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

# Declined prompts must send nothing. For read-and-save-position that means no
# M889: the calibration comparison at the end of the suite proves it.
OUT=$(printf 'n\n' | lager_arm read-and-save-position 2>&1)
RC=$?
if [ $RC -eq 0 ] && echo "$OUT" | grep -q "Aborting"; then
  track_test_msg "pass" "read-and-save-position: declining the prompt aborts"
else
  track_test_msg "fail" "read-and-save-position did not abort when declined (exit $RC): $OUT"
fi

OUT=$(printf 'n\n' | lager_arm disable-motor 2>&1)
RC=$?
if [ $RC -eq 0 ] && echo "$OUT" | grep -q "Aborting"; then
  track_test_msg "pass" "disable-motor: declining the prompt aborts"
else
  track_test_msg "fail" "disable-motor did not abort when declined (exit $RC): $OUT"
fi
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

# The move's own output is the position it reports on arrival.
OUT=$(lager_arm move --x 0 --y 300 --z 0 --yes 2>&1)
if [ $? -eq 0 ] && near 0 300 0 && [ "$(parse_position "$OUT")" != "" ]; then
  track_test_msg "pass" "move back to home arrives and reports its position"
else
  track_test_msg "fail" "move back to (0, 300, 0): $OUT / $(read_position)"
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
# SECTION 6: MOVE TIMEOUT
# ============================================================================
print_section_header "SECTION 6: MOVE TIMEOUT"
start_section "Move Timeout"

# A 200 mm traverse takes about 9 s, so a 1 s timeout must expire. The arm
# keeps going to the target: the timeout only ends the wait.
if lager_arm move --x "-$ARM_TRAVERSE_X" --y 300 --z 30 --yes >/dev/null 2>&1 \
    && near "-$ARM_TRAVERSE_X" 300 30; then
  track_test_msg "pass" "move to the start of the traverse (-$ARM_TRAVERSE_X, 300, 30)"
else
  track_test_msg "fail" "move to (-$ARM_TRAVERSE_X, 300, 30) did not arrive: $(read_position)"
fi

expect_fail "a move past its --timeout fails and suggests a longer timeout" "longer timeout" \
  lager_arm move --x "$ARM_TRAVERSE_X" --y 300 --z 30 --timeout 1 --yes

if wait_near "$ARM_TRAVERSE_X" 300 30 20; then
  track_test_msg "pass" "the arm still reaches the target after its wait timed out"
else
  track_test_msg "fail" "arm did not reach ($ARM_TRAVERSE_X, 300, 30) after the timeout: $(read_position)"
fi

if lager_arm go-home --yes >/dev/null 2>&1 && near 0 300 0; then
  track_test_msg "pass" "go-home after the traverse"
else
  track_test_msg "fail" "go-home after the traverse: $(read_position)"
fi
echo ""

# ============================================================================
# SECTION 7: ACCELERATION
# ============================================================================
print_section_header "SECTION 7: ACCELERATION"
start_section "Acceleration"

OUT=$(lager_arm set-acceleration --acceleration "$ARM_ACCELERATION" \
  --travel "$ARM_TRAVEL" --retract "$ARM_RETRACT" 2>&1)
if [ $? -eq 0 ] && [ "$(read_acceleration)" = "$ARM_ACCELERATION $ARM_TRAVEL $ARM_RETRACT" ]; then
  track_test_msg "pass" "set-acceleration $ARM_ACCELERATION/$ARM_TRAVEL/$ARM_RETRACT reads back exactly"
else
  track_test_msg "fail" "set-acceleration $ARM_ACCELERATION/$ARM_TRAVEL/$ARM_RETRACT: $OUT; read back $(read_acceleration)"
fi

# Without --retract the arm keeps its retract value; it used to be reset to 60.
OUT=$(lager_arm set-acceleration --acceleration "$ARM_TRAVEL" --travel "$ARM_ACCELERATION" 2>&1)
if [ $? -eq 0 ] && [ "$(read_acceleration)" = "$ARM_TRAVEL $ARM_ACCELERATION $ARM_RETRACT" ]; then
  track_test_msg "pass" "set-acceleration without --retract keeps retract $ARM_RETRACT"
else
  track_test_msg "fail" "set-acceleration without --retract: $OUT; read back $(read_acceleration)"
fi

# Put the arm's own values back, and prove they landed.
if [ -n "$ACC0" ]; then
  read -r P0 T0 R0 <<< "$ACC0"
  lager_arm set-acceleration --acceleration "${P0%.*}" --travel "${T0%.*}" \
    --retract "${R0%.*}" >/dev/null 2>&1
  if [ "$(read_acceleration)" = "$ACC0" ]; then
    track_test_msg "pass" "original acceleration $ACC0 restored"
  else
    track_test_msg "fail" "acceleration not restored to $ACC0: $(read_acceleration)"
  fi
else
  track_test_msg "fail" "original acceleration unknown, so it cannot be restored"
fi
echo ""

# ============================================================================
# SECTION 8: MOTORS
# ============================================================================
print_section_header "SECTION 8: MOTORS"
start_section "Motors"

# enable-motor must leave the arm able to move: a move after it has to land.
if lager_arm enable-motor >/dev/null 2>&1 \
    && lager_arm move-by --dz 5 --yes >/dev/null 2>&1 && near 0 300 5 \
    && lager_arm move-by --dz -5 --yes >/dev/null 2>&1 && near 0 300 0; then
  track_test_msg "pass" "enable-motor, then a move lands"
else
  track_test_msg "fail" "enable-motor then move: $(read_position)"
fi

if [ "${ARM_TEST_DISABLE_MOTOR:-}" = "1" ]; then
  read -r DX DY DZ <<< "$ARM_DROP_POSE"
  if lager_arm move --x "$DX" --y "$DY" --z "$DZ" --yes >/dev/null 2>&1 \
      && lager_arm disable-motor --yes >/dev/null 2>&1; then
    track_test_msg "pass" "disable-motor at ($DX, $DY, $DZ)"
  else
    track_test_msg "fail" "disable-motor at ($DX, $DY, $DZ) failed"
  fi
  sleep 2
  # A dropped arm has to be homed before the next move.
  if lager_arm enable-motor >/dev/null 2>&1 \
      && lager_arm go-home --yes >/dev/null 2>&1 && near 0 300 0; then
    track_test_msg "pass" "enable-motor and go-home recover the arm after disable-motor"
  else
    track_test_msg "fail" "arm did not recover after disable-motor: $(read_position)"
  fi
else
  skip_test "disable-motor" "set ARM_TEST_DISABLE_MOTOR=1; the arm goes limp and can drop"
fi
echo ""

# ============================================================================
# SECTION 9: INSTRUMENT SCAN DURING ARM COMMANDS
# ============================================================================
print_section_header "SECTION 9: INSTRUMENT SCAN DURING ARM COMMANDS"
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
# SECTION 10: USB DROP AND RECONNECT
# ============================================================================
print_section_header "SECTION 10: USB DROP AND RECONNECT"
start_section "Reconnect"

if [ -z "$USB_NET" ]; then
  skip_test "USB drop and reconnect" "pass a USB_NET that carries the arm's cable"
else
  TTY_BEFORE=$(arm_tty)
  read_position >/dev/null   # the box now holds the arm's port open

  if lager usb "$USB_NET" disable --box "$BOX" >/dev/null 2>&1; then
    track_test_msg "pass" "lager usb $USB_NET disable"
  else
    track_test_msg "fail" "lager usb $USB_NET disable failed"
  fi
  sleep 3

  # A disconnected arm must fail fast with the arm's own error, not hold the
  # request until hardware_service gives up on it.
  START=$SECONDS
  OUT=$(bounded 60 lager arm "$ARM_NET" position --box "$BOX" 2>&1)
  RC=$?
  ELAPSED=$((SECONDS - START))
  if [ $RC -ne 0 ] && [ $ELAPSED -le $SILENT_ARM_BOUND_S ] \
      && ! echo "$OUT" | grep -qi "did not respond"; then
    track_test_msg "pass" "a disconnected arm fails in ${ELAPSED} s: $(echo "$OUT" | tail -1)"
  else
    track_test_msg "fail" "disconnected arm: exit $RC after ${ELAPSED} s (bound ${SILENT_ARM_BOUND_S} s): $(echo "$OUT" | tail -1)"
  fi

  if lager usb "$USB_NET" enable --box "$BOX" >/dev/null 2>&1; then
    track_test_msg "pass" "lager usb $USB_NET enable"
  else
    track_test_msg "fail" "lager usb $USB_NET enable failed"
  fi

  # The net finds its arm by USB serial, so the tty may move and it still works.
  if wait_near 0 300 0 30; then
    track_test_msg "pass" "arm answers at home again after $USB_NET came back"
  else
    track_test_msg "fail" "arm did not answer within 30 s after $USB_NET came back"
  fi
  echo "  tty before the drop: $TTY_BEFORE, after: $(arm_tty)"
fi
echo ""

# ============================================================================
# SECTION 11: CALIBRATION UNCHANGED
# ============================================================================
print_section_header "SECTION 11: CALIBRATION UNCHANGED"
start_section "Calibration"

CAL1=$(read_calibration)
if [ -n "$CAL0" ] && [ "$CAL1" = "$CAL0" ]; then
  track_test_msg "pass" "stored calibration unchanged: $CAL1"
else
  track_test_msg "fail" "stored calibration changed or unreadable: before '$CAL0', after '$CAL1'"
fi
echo ""

print_summary
exit_with_status
