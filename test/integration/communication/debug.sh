#!/bin/bash

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
#
# lager debug: one debug net, end to end.
#
# Health and status, the GDB server, memory reads, resets, and (only when
# DEBUG_ALLOW_ERASE=1) one chip erase followed by a reflash. Memory is the
# verdict wherever there is one: after a flash, the first 16 bytes of the
# target must equal the first 16 bytes of the image, and after an erase they
# must all read 0xFF.
#
# The erase section needs FIRMWARE_HEX, the image the target normally runs.
# Its bytes are decoded from the file and the target must hold them before
# anything is erased, so the suite only ever erases a target it can put back.
# If the run stops between the erase and the reflash, the cleanup reflashes.
#
# Usage: debug.sh <BOX> <DEBUG_NET> [FIRMWARE_HEX]
#
# Environment:
#   DEBUG_ALLOW_ERASE=1   run the erase / reflash section (needs FIRMWARE_HEX)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/../../framework/colors.sh"
source "${SCRIPT_DIR}/../../framework/harness.sh"

set +e

if [ $# -lt 2 ]; then
  echo "Usage: $0 <BOX> <DEBUG_NET> [FIRMWARE_HEX]"
  exit 1
fi

BOX="$1"
NET="$2"
HEXFILE="${3:-}"
ALLOW_ERASE="${DEBUG_ALLOW_ERASE:-0}"

if [ "$ALLOW_ERASE" = "1" ] && [ ! -f "$HEXFILE" ]; then
  echo "DEBUG_ALLOW_ERASE=1 needs FIRMWARE_HEX, the image to put back after the erase ('$HEXFILE' not found)." >&2
  exit 2
fi

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
has() { [ "$1" -eq 0 ] && printf '%s' "$2" | grep -qE "$3"; }

dbg() { timeout 180 lager debug "$NET" "$@" --box "$BOX" 2>&1; }

# Print field $1 of the JSON object on stdin, or nothing.
json_field() {
  python3 -c '
import json, sys
try:
    v = json.load(sys.stdin)
    for k in sys.argv[1].split("."):
        v = v[k]
except Exception:
    sys.exit(0)
print(v)
' "$1"
}

# "Field: value" from `lager debug <net> status`.
status_field() { dbg status | sed -nE "s/^ *$1: *//p" | head -1; }

# The 16 bytes at 0x0 as "00 00 08 20 ...", from a JSON memrd. Prints nothing
# when the read fails or returns anything other than 16 bytes. Reads stdout
# only: --json output must parse even when the command connects first.
first16() {
  timeout 180 lager debug "$NET" memrd 0x0 16 --json --box "$BOX" 2>/dev/null | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    out = []
    for line in d["data"]:
        out += [int(x, 16) for x in line.split("\t")[1:]]
except Exception:
    sys.exit(0)
if len(out) == 16:
    print(" ".join("%02x" % b for b in out))
'
}

# The first 16 bytes an Intel HEX image puts at 0x0 (0xFF where it has none).
image16() {
  python3 -c '
import sys
data, base = {}, 0
for line in open(sys.argv[1]):
    line = line.strip()
    if not line.startswith(":"):
        continue
    b = bytes.fromhex(line[1:])
    n, addr, kind = b[0], (b[1] << 8) | b[2], b[3]
    if kind == 4:
        base = ((b[4] << 8) | b[5]) << 16
    elif kind == 0:
        for i in range(n):
            data[base + addr + i] = b[4 + i]
print(" ".join("%02x" % data.get(a, 0xFF) for a in range(16)))
' "$1"
}

BLANK16="ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff"

ERASED=0
cleanup() {
  if [ "$ERASED" = "1" ]; then
    echo "Cleanup: the target was erased and not reflashed; reflashing $HEXFILE"
    dbg flash --hex "$HEXFILE" | tail -2
  fi
  lager debug "$NET" disconnect --box "$BOX" >/dev/null 2>&1
}
harness_add_cleanup cleanup

echo "========================================================================"
echo "LAGER DEBUG SUITE"
echo "========================================================================"
echo "Box: $BOX   Net: $NET   Erase section: $([ "$ALLOW_ERASE" = 1 ] && echo "on ($HEXFILE)" || echo off)"
echo ""

# Start from a stopped GDB server.
lager debug "$NET" disconnect --box "$BOX" >/dev/null 2>&1

# ============================================================
# SECTION 1: HEALTH AND STATUS
# ============================================================
start_section "Status"

OUT=$(timeout 60 lager debug health --box "$BOX" 2>&1); RC=$?
check "1.1 the debug service reports healthy" "$OUT" has "$RC" "$OUT" "Status: healthy"
OUT=$(dbg status); RC=$?
check "1.2 status names the net" "$OUT" has "$RC" "$OUT" "Name: $NET\$"
DEVICE=$(status_field "Device Type"); PROBE=$(status_field "Probe")
check "1.3 status reports a device type and a probe" "$DEVICE / $PROBE" \
  bash -c '[ -n "$1" ] && [ -n "$2" ]' _ "$DEVICE" "$PROBE"
check "1.4 GDB server not running at the start" "$(dbg status)" [ "$(status_field "GDB server running")" = False ]

# ============================================================
# SECTION 2: REFUSALS
# ============================================================
# Each is rejected by the CLI before it reaches the probe.
start_section "Refusals"

OUT=$(timeout 60 lager debug no_such_debug_net_xyz status --box "$BOX" 2>&1); RC=$?
check "2.1 unknown net" "$OUT" refused "$RC" "$OUT" "not found"
OUT=$(timeout 60 lager debug "$NET" status --box NO-SUCH-BOX-XYZ 2>&1); RC=$?
check "2.2 unknown box" "$OUT" refused "$RC" "$OUT" "box|not found|don't have"
OUT=$(dbg gdbserver --speed abc --no-tunnel); RC=$?
check "2.3 gdbserver --speed abc" "$OUT" refused "$RC" "$OUT" "Invalid speed value"
OUT=$(dbg gdbserver --speed 0 --no-tunnel); RC=$?
check "2.4 gdbserver --speed 0" "$OUT" refused "$RC" "$OUT" "greater than 0"
OUT=$(dbg memrd 0xGG 16); RC=$?
check "2.5 memrd with a non-hex address" "$OUT" refused "$RC" "$OUT" "not a valid hex"
OUT=$(dbg erase --yes --erase-start 0x0); RC=$?
check "2.6 erase --erase-start without --erase-size" "$OUT" refused "$RC" "$OUT" "must be given together"
OUT=$(dbg flash --hex /nonexistent/firmware_xyz.hex); RC=$?
check "2.7 flash with a missing image file" "$OUT" refused "$RC" "$OUT" "does not exist"
check "2.8 GDB server still not running after the refusals" "$(dbg status)" \
  [ "$(status_field "GDB server running")" = False ]

# ============================================================
# SECTION 3: GDB SERVER
# ============================================================
start_section "GDB server"

OUT=$(dbg gdbserver --no-tunnel --json); RC=$?
GDB_STATUS=$(printf '%s' "$OUT" | json_field status)
GDB_PORT=$(printf '%s' "$OUT" | json_field gdb_server.gdb_port)
check "3.1 gdbserver starts and reports connected" "$OUT" \
  bash -c '[ "$1" -eq 0 ] && [ "$2" = connected ]' _ "$RC" "$GDB_STATUS"
check "3.2 it reports a GDB port" "port=$GDB_PORT" \
  bash -c '[ "$1" -ge 1 ] 2>/dev/null && [ "$1" -le 65535 ]' _ "$GDB_PORT"
check "3.3 status: server running, target attached" "$(dbg status)" \
  [ "$(status_field "GDB server running")/$(status_field "Target attached")" = "True/Yes" ]
OUT=$(dbg gdbserver --no-tunnel --json); RC=$?
check "3.4 a second gdbserver reuses the running one on the same port" "$OUT" \
  bash -c '[ "$1" -eq 0 ] && [ -n "$3" ] && [ "$2" = "$3" ]' _ "$RC" "$(printf '%s' "$OUT" | json_field gdb_server.gdb_port)" "$GDB_PORT"
OUT=$(dbg disconnect); RC=$?
check "3.5 disconnect stops it" "$OUT" has "$RC" "$OUT" "stopped"
check "3.6 status: server not running" "$(dbg status)" [ "$(status_field "GDB server running")" = False ]

# ============================================================
# SECTION 4: MEMORY
# ============================================================
start_section "Memory"

M1=$(first16)
check "4.1 memrd --json returns 16 bytes at 0x0" "got '$M1'" [ -n "$M1" ]
OUT=$(dbg memrd 0x0 16); RC=$?
TEXT16=$(printf '%s\n' "$OUT" | grep -E '^0x' | cut -f2- | tr '\t' ' ' | sed 's/0x//g' | tr '\n' ' ' | sed 's/ *$//')
check "4.2 text memrd agrees with --json" "text='$TEXT16' json='$M1'" \
  bash -c '[ "$1" -eq 0 ] && [ -n "$2" ] && [ "$2" = "$3" ]' _ "$RC" "$TEXT16" "$M1"
M2=$(first16); M3=$(first16)
check "4.3 three reads agree" "$M1 | $M2 | $M3" bash -c '[ -n "$1" ] && [ "$1" = "$2" ] && [ "$2" = "$3" ]' _ "$M1" "$M2" "$M3"
OUT=$(dbg memrd 0x0 64 --json); RC=$?
check "4.4 a 64-byte read reports length 64" "$OUT" \
  bash -c '[ "$1" -eq 0 ] && [ "$2" = 64 ]' _ "$RC" "$(printf '%s' "$OUT" | json_field length)"

# ============================================================
# SECTION 5: RESET
# ============================================================
start_section "Reset"

OUT=$(dbg reset); RC=$?
check "5.1 reset leaves the target running" "$OUT" has "$RC" "$OUT" "Reset complete \(running"
OUT=$(dbg reset --halt); RC=$?
check "5.2 reset --halt leaves it halted" "$OUT" has "$RC" "$OUT" "Reset complete \(halted"
check "5.3 memory reads the same after the resets" "now '$(first16)' before '$M1'" \
  bash -c '[ -n "$2" ] && [ "$1" = "$2" ]' _ "$(first16)" "$M1"
dbg disconnect >/dev/null

# ============================================================
# SECTION 6: ERASE AND REFLASH
# ============================================================
if [ "$ALLOW_ERASE" = "1" ]; then
  start_section "Erase and reflash"

  IMG=$(image16 "$HEXFILE")
  check "6.1 the image has a vector table at 0x0" "$IMG" bash -c '[ -n "$1" ] && [ "$1" != "$2" ]' _ "$IMG" "$BLANK16"
  NOW=$(first16)
  check "6.2 the target holds that image" "target '$NOW' image '$IMG'" [ "$NOW" = "$IMG" ]

  if [ "$NOW" = "$IMG" ] && [ "$IMG" != "$BLANK16" ]; then
    # A flash with no image must not reach the target. An older CLI erased
    # first; the reflash below puts the image back either way.
    ERASED=1
    OUT=$(dbg flash); RC=$?
    check "6.3 flash with no image is refused" "$OUT" refused "$RC" "$OUT" "Provide --hex, --elf, or --bin"
    check "6.4 ...without erasing anything" "now '$(first16)'" [ "$(first16)" = "$IMG" ]

    OUT=$(dbg erase --yes); RC=$?
    check "6.5 erase succeeds" "$OUT" has "$RC" "$OUT" "Erase complete|Erased"
    check "6.6 the target reads blank" "now '$(first16)'" [ "$(first16)" = "$BLANK16" ]

    OUT=$(dbg flash --hex "$HEXFILE"); RC=$?
    check "6.7 flash --hex succeeds" "$(printf '%s' "$OUT" | tail -3)" has "$RC" "$OUT" "Flashed!"
    NOW=$(first16)
    check "6.8 the target holds the image again" "target '$NOW' image '$IMG'" [ "$NOW" = "$IMG" ]
    [ "$NOW" = "$IMG" ] && ERASED=0
    OUT=$(dbg reset); RC=$?
    check "6.9 it resets and runs" "$OUT" has "$RC" "$OUT" "Reset complete \(running"
  else
    echo "The target does not hold $HEXFILE; not erasing a target this suite cannot restore."
  fi
fi

# ============================================================
# SECTION 7: END STATE
# ============================================================
start_section "End state"

dbg disconnect >/dev/null
check "7.1 GDB server stopped" "$(dbg status)" [ "$(status_field "GDB server running")" = False ]

print_summary
exit_with_status
