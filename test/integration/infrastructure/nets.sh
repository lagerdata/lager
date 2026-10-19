#!/bin/bash

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
# ============================================================================
# Saved-net lifecycle suite for `lager nets`
# ============================================================================
# Exercises the saved-net registry end to end against a real box: add,
# add-batch, show, describe, rename and delete, plus the rejections each one
# is supposed to make. Every check asserts an exit status AND a result read
# back from the box -- a command that only prints something does not pass.
#
# Scratch nets only. Every net this suite creates is a UART net in the
# device-path form (`nets add NAME uart /dev/... LABEL`): the box accepts it
# without any matching hardware, so the suite needs no instrument and never
# shares a channel with a real net. All of them carry a per-run prefix and a
# cleanup trap deletes whatever is left on exit.
#
# Deliberately NOT exercised here:
#   nets add-all     -- writes a net for every instrument on the box into the
#                       shared registry; on a bench box that renumbers and
#                       duplicates nets other suites depend on.
#   nets delete-all  -- wipes the registry, relay nets included.
#   nets tui         -- interactive; covered by the unit tests.
#
# USAGE:
#   ./nets.sh <BOX>
# ============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

source "${SCRIPT_DIR}/../../framework/colors.sh"
source "${SCRIPT_DIR}/../../framework/harness.sh"

set +e  # Don't exit on error - we want to track failures

init_harness

if [ $# -lt 1 ]; then
  echo "Usage: $0 <BOX>"
  exit 1
fi

BOX="$1"

# Per-run prefix: two runs (or a leftover from a killed run) never collide.
PFX="ci_nets_$$"
SCRATCH_ADDR="ci-nets-scratch-$$"
TMPDIR_NETS="$(mktemp -d)"

# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------

# Rows of the `lager nets` tree listing. Every saved net is one row.
count_nets() {
  lager nets --box "$BOX" 2>/dev/null | grep -cE '^(├|└)── '
}

# Exit 0 when the box has a saved net named $1.
net_exists() {
  lager nets show "$1" --box "$BOX" --json >/dev/null 2>&1
}

# Print field $2 of saved net $1 (JSON from `nets show --json`). Lists are
# printed comma-joined. Prints nothing when the net or field is missing.
net_field() {
  lager nets show "$1" --box "$BOX" --json 2>/dev/null | python3 -c '
import json, sys
try:
    rec = json.load(sys.stdin)
except ValueError:
    sys.exit(0)
val = rec.get(sys.argv[1], "")
print(",".join(map(str, val)) if isinstance(val, list) else val)
' "$2"
}

# Add a scratch UART net. $1 = name, $2 = device path, $3 = address label.
add_scratch() {
  lager nets add "$1" uart "$2" "${3:-$SCRATCH_ADDR-$1}" --box "$BOX" 2>&1
}

# Run one check. $1 = description, $2 = detail printed on failure, the rest
# is the condition, run as a command: exit 0 passes.
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

# Conditions for check.
absent() { ! net_exists "$1"; }
# $1 = exit status, $2 = output, $3 = pattern the output must contain.
refused() { [ "$1" -ne 0 ] && printf '%s' "$2" | grep -qi "$3"; }
succeeded() { [ "$1" -eq 0 ] && printf '%s' "$2" | grep -q "$3"; }
equals() { [ "$1" = "$2" ]; }
is_zero() { [ "$1" -eq 0 ]; }
# Exit 0 when net $1 is a row of the `lager nets` listing.
listed() { lager nets --box "$BOX" 2>/dev/null | grep -qE "^(├|└)── +$1 "; }

cleanup_scratch_nets() {
  local name
  lager nets --box "$BOX" 2>/dev/null \
    | sed -nE 's/^(├|└)── +([^ ]+) +uart .*/\2/p' \
    | grep "^${PFX}" \
    | while read -r name; do
        lager nets delete "$name" uart --box "$BOX" --yes >/dev/null 2>&1
      done
  rm -rf "$TMPDIR_NETS"
}
harness_add_cleanup cleanup_scratch_nets

echo "========================================================================"
echo "LAGER NETS LIFECYCLE SUITE"
echo "========================================================================"
echo "Box:    $BOX"
echo "Prefix: $PFX"
echo ""

# ============================================================
# SECTION 1: LISTING AND COMMAND SURFACE
# ============================================================
start_section "Listing and Command Surface"

OUT=$(lager nets --box "$BOX" 2>&1); RC=$?
check "1.1 nets listing exits 0" "$OUT" is_zero "$RC"

BASELINE_COUNT=$(count_nets)
echo "  saved nets before the suite: $BASELINE_COUNT"
check "1.2 listing shows at least one saved net" "" [ "$BASELINE_COUNT" -ge 1 ]

# The subcommands this suite drives must be the ones the CLI exposes. A
# rename on the CLI side (create -> add) used to leave this suite calling
# commands that no longer existed while still reporting green.
HELP=$(lager nets --help 2>&1)
MISSING=""
for sub in add add-batch delete rename show describe state; do
  printf '%s\n' "$HELP" | grep -qE "^  ${sub} " || MISSING="$MISSING $sub"
done
check "1.3 nets --help lists every subcommand this suite drives" "missing:$MISSING" equals "$MISSING" ""

OUT=$(lager nets state --box "$BOX" --json 2>&1); RC=$?
printf '%s' "$OUT" | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if isinstance(d,(list,dict)) else 1)' 2>/dev/null
PARSE_RC=$?
check "1.4 nets state --json exits 0 with parseable JSON" "$OUT" is_zero $((RC + PARSE_RC))

# ============================================================
# SECTION 2: REJECTIONS
# ============================================================
start_section "Rejections"

OUT=$(lager nets add --box "$BOX" 2>&1); RC=$?
check "2.1 add with no arguments is refused" "$OUT" refused "$RC" "$OUT" "missing argument"

OUT=$(lager nets add "${PFX}_noinst" gpio 1 "TCPIP0::192.0.2.1::inst0::INSTR" --box "$BOX" 2>&1); RC=$?
check "2.2 add naming an absent instrument is refused" "$OUT" refused "$RC" "$OUT" "no instrument"
check "2.3 the refused net was not saved" "" absent "${PFX}_noinst"

OUT=$(lager nets delete "${PFX}_missing" uart --box "$BOX" --yes 2>&1); RC=$?
check "2.4 delete of an unknown net is refused with 'not found'" "$OUT" refused "$RC" "$OUT" "not found"

OUT=$(lager nets rename "${PFX}_missing" "${PFX}_other" --box "$BOX" 2>&1); RC=$?
check "2.5 rename of an unknown net is refused with 'not found'" "$OUT" refused "$RC" "$OUT" "not found"

OUT=$(lager nets show "${PFX}_missing" --box "$BOX" 2>&1); RC=$?
check "2.6 show of an unknown net is refused with 'not found'" "$OUT" refused "$RC" "$OUT" "not found"

OUT=$(lager nets add-batch "$TMPDIR_NETS/does-not-exist.json" --box "$BOX" 2>&1); RC=$?
check "2.7 add-batch with a missing file is refused" "$OUT" refused "$RC" "$OUT" "no such file"

# ============================================================
# SECTION 3: ADD AND SHOW
# ============================================================
start_section "Add and Show"

NET_A="${PFX}_a"
OUT=$(add_scratch "$NET_A" "/dev/ci-nets-$$-a"); RC=$?
check "3.1 add a scratch net" "$OUT" succeeded "$RC" "$OUT" "Saved new net"

V=$(net_field "$NET_A" role);    check "3.2 show reads back role=uart" "$V" equals "$V" "uart"
V=$(net_field "$NET_A" pin);     check "3.3 show reads back the device path" "$V" equals "$V" "/dev/ci-nets-$$-a"
V=$(net_field "$NET_A" address); check "3.4 show reads back the address" "$V" equals "$V" "$SCRATCH_ADDR-$NET_A"

AFTER=$(count_nets)
check "3.5 listing grew by exactly one ($BASELINE_COUNT -> $AFTER)" "" [ "$AFTER" -eq $((BASELINE_COUNT + 1)) ]
check "3.6 the new net appears in the listing" "" listed "$NET_A"

OUT=$(add_scratch "$NET_A" "/dev/ci-nets-$$-a2"); RC=$?
check "3.7 a second net with the same name is refused" "$OUT" refused "$RC" "$OUT" "already exists"

OUT=$(add_scratch "${PFX}_dup" "/dev/ci-nets-$$-a" "$SCRATCH_ADDR-$NET_A"); RC=$?
check "3.8 a net duplicating role/channel/address is refused" "$OUT" refused "$RC" "$OUT" "same role"
check "3.9 the refused duplicate was not saved" "" absent "${PFX}_dup"

# BMP (CJK) plus non-BMP (U+1D400, 4-byte UTF-8) on purpose: different
# encoding paths, and a CJK-only name would not catch a surrogate-pair bug.
NET_U="${PFX}_测试_𝐀"
OUT=$(add_scratch "$NET_U" "/dev/ci-nets-$$-u"); RC=$?
check "3.10 a Unicode net name is saved" "$OUT" succeeded "$RC" "$OUT" "Saved new net"
check "3.11 the Unicode net reads back by name" "" net_exists "$NET_U"

# ============================================================
# SECTION 4: DESCRIBE
# ============================================================
start_section "Describe"

OUT=$(lager nets describe "$NET_A" -p "bench lifecycle scratch" -t ci -t scratch --dut-connection "J9 pin 1" --box "$BOX" 2>&1); RC=$?
check "4.1 describe sets metadata" "$OUT" is_zero "$RC"
V=$(net_field "$NET_A" purpose); check "4.2 purpose reads back" "$V" equals "$V" "bench lifecycle scratch"
V=$(net_field "$NET_A" tags);    check "4.3 tags read back in order" "$V" equals "$V" "ci,scratch"

OUT=$(lager nets describe "$NET_A" --clear-tags -t only --box "$BOX" 2>&1)
V=$(net_field "$NET_A" tags);    check "4.4 --clear-tags replaces the tag list" "$V / $OUT" equals "$V" "only"
V=$(net_field "$NET_A" pin);     check "4.5 describe leaves the channel untouched" "$V" equals "$V" "/dev/ci-nets-$$-a"

# ============================================================
# SECTION 5: RENAME
# ============================================================
start_section "Rename"

NET_R="${PFX}_renamed"
OUT=$(lager nets rename "$NET_A" "$NET_R" --box "$BOX" 2>&1); RC=$?
check "5.1 rename exits 0" "$OUT" is_zero "$RC"
check "5.2 the old name is gone" "" absent "$NET_A"
V=$(net_field "$NET_R" pin);     check "5.3 the renamed net keeps its channel" "$V" equals "$V" "/dev/ci-nets-$$-a"
V=$(net_field "$NET_R" purpose); check "5.4 the renamed net keeps its metadata" "$V" equals "$V" "bench lifecycle scratch"
N=$(count_nets);                 check "5.5 rename did not change the net count" "$N" [ "$N" -eq $((BASELINE_COUNT + 2)) ]

OUT=$(lager nets rename "$NET_R" "$NET_U" --box "$BOX" 2>&1); RC=$?
check "5.6 renaming onto an existing name is refused" "$OUT" refused "$RC" "$OUT" "already exists"
check "5.7 the refused rename left the source in place" "" net_exists "$NET_R"

OUT=$(lager nets rename "$NET_R" "$NET_A" --box "$BOX" 2>&1); RC=$?
check "5.8 rename back to the original name" "$OUT" is_zero "$RC"
check "5.9 the original name is back" "" net_exists "$NET_A"

# ============================================================
# SECTION 6: ADD-BATCH
# ============================================================
start_section "Add-Batch"

BATCH="$TMPDIR_NETS/batch.json"
cat > "$BATCH" <<EOF
[
  {"name": "${PFX}_b1", "role": "uart", "channel": "/dev/ci-nets-$$-b1", "address": "$SCRATCH_ADDR-b1"},
  {"name": "${PFX}_b2", "role": "uart", "channel": "/dev/ci-nets-$$-b2", "address": "$SCRATCH_ADDR-b2"},
  {"name": "${PFX}_b3", "role": "uart", "channel": "/dev/ci-nets-$$-b3", "address": "$SCRATCH_ADDR-b3"}
]
EOF
BEFORE=$(count_nets)
OUT=$(lager nets add-batch "$BATCH" --box "$BOX" 2>&1); RC=$?
check "6.1 add-batch of three nets exits 0" "$OUT" is_zero "$RC"
MISSING=""
for n in b1 b2 b3; do
  [ "$(net_field "${PFX}_$n" pin)" = "/dev/ci-nets-$$-$n" ] || MISSING="$MISSING $n"
done
check "6.2 every batch net reads back with its channel" "missing:$MISSING" equals "$MISSING" ""
N=$(count_nets); check "6.3 listing grew by exactly three ($BEFORE -> $N)" "" [ "$N" -eq $((BEFORE + 3)) ]

printf '[{"name": "%s_bad1",' "$PFX" > "$TMPDIR_NETS/invalid.json"
OUT=$(lager nets add-batch "$TMPDIR_NETS/invalid.json" --box "$BOX" 2>&1); RC=$?
check "6.4 malformed JSON is refused" "$OUT" refused "$RC" "$OUT" "invalid json"

printf '{"name": "%s_bad2"}' "$PFX" > "$TMPDIR_NETS/object.json"
OUT=$(lager nets add-batch "$TMPDIR_NETS/object.json" --box "$BOX" 2>&1); RC=$?
check "6.5 a non-array document is refused" "$OUT" refused "$RC" "$OUT" "array"

printf '[{"name": "%s_bad3", "role": "uart", "channel": "/dev/ci-nets-%s-bad3"}]' "$PFX" "$$" > "$TMPDIR_NETS/missing.json"
OUT=$(lager nets add-batch "$TMPDIR_NETS/missing.json" --box "$BOX" 2>&1); RC=$?
check "6.6 a record missing 'address' is refused" "$OUT" refused "$RC" "$OUT" "missing required field"

# One good record plus one with an unknown key: the whole batch must be
# refused, the good record included.
cat > "$TMPDIR_NETS/unknown.json" <<EOF
[
  {"name": "${PFX}_ok", "role": "uart", "channel": "/dev/ci-nets-$$-ok", "address": "$SCRATCH_ADDR-ok"},
  {"name": "${PFX}_bad4", "role": "uart", "channel": "/dev/ci-nets-$$-bad4", "address": "$SCRATCH_ADDR-bad4", "colour": "red"}
]
EOF
OUT=$(lager nets add-batch "$TMPDIR_NETS/unknown.json" --box "$BOX" 2>&1); RC=$?
check "6.7 a record with an unknown key is refused" "$OUT" refused "$RC" "$OUT" "unknown key"
check "6.8 a refused batch saves none of its records" "" absent "${PFX}_ok"

# ============================================================
# SECTION 7: DELETE
# ============================================================
start_section "Delete"

OUT=$(lager nets delete "$NET_A" gpio --box "$BOX" --yes 2>&1); RC=$?
check "7.1 delete with the wrong net type is refused" "$OUT" refused "$RC" "$OUT" "not found"
check "7.2 the wrong-type delete kept the net" "" net_exists "$NET_A"

OUT=$(lager nets delete "$NET_A" uart --box "$BOX" --yes 2>&1); RC=$?
check "7.3 delete exits 0 and reports the deletion" "$OUT" succeeded "$RC" "$OUT" "Deleted"
check "7.4 the deleted net is gone" "" absent "$NET_A"

FAILED_DELETES=""
for n in "$NET_U" "${PFX}_b1" "${PFX}_b2" "${PFX}_b3"; do
  lager nets delete "$n" uart --box "$BOX" --yes >/dev/null 2>&1 || FAILED_DELETES="$FAILED_DELETES $n"
  net_exists "$n" && FAILED_DELETES="$FAILED_DELETES $n(still-present)"
done
check "7.5 every remaining scratch net deletes" "$FAILED_DELETES" equals "$FAILED_DELETES" ""

FINAL=$(count_nets)
check "7.6 net count is back to the baseline ($BASELINE_COUNT -> $FINAL)" "" [ "$FINAL" -eq "$BASELINE_COUNT" ]

# ============================================================
# SECTION 8: REPEATED LIFECYCLE
# ============================================================
start_section "Repeated Lifecycle"

# Ten add -> verify -> delete -> verify cycles. A registry write that is
# acknowledged but not persisted, or a delete that leaves a stale entry,
# shows up here as a specific cycle number.
BAD_CYCLES=""
for i in $(seq 1 10); do
  n="${PFX}_cycle_$i"
  add_scratch "$n" "/dev/ci-nets-$$-c$i" >/dev/null || { BAD_CYCLES="$BAD_CYCLES $i(add)"; continue; }
  net_exists "$n" || BAD_CYCLES="$BAD_CYCLES $i(missing-after-add)"
  lager nets delete "$n" uart --box "$BOX" --yes >/dev/null 2>&1 || BAD_CYCLES="$BAD_CYCLES $i(delete)"
  net_exists "$n" && BAD_CYCLES="$BAD_CYCLES $i(present-after-delete)"
done
check "8.1 ten add/delete cycles each read back correctly" "$BAD_CYCLES" equals "$BAD_CYCLES" ""

LIST_FAILS=0
for _ in $(seq 1 20); do
  lager nets --box "$BOX" >/dev/null 2>&1 || LIST_FAILS=$((LIST_FAILS + 1))
done
check "8.2 twenty consecutive listings all succeed ($LIST_FAILS failed)" "" is_zero "$LIST_FAILS"

N=$(count_nets)
check "8.3 net count is still the baseline ($N)" "" [ "$N" -eq "$BASELINE_COUNT" ]

print_summary
exit_with_status
