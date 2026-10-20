#!/bin/bash

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
#
# lager usb: one hub port, end to end.
#
# Every action is checked twice: its exit status and message, and the port
# state read back with `lager usb <net> state`. The suite drives ONLY the net
# it is given -- never another port on the same hub, because on a bench the
# other ports power probes and peers that later steps depend on.
#
# The port is put back in the state it started in, and when that is enabled
# the suite finishes with a `cycle`, which waits for the device to
# re-enumerate, so a later step does not find it missing.
#
# Usage: usb.sh <BOX> <USB_NET>
#
# Environment:
#   USB_EXPECT_DEVICE=1   a device is attached to USB_NET; every cycle must
#                         then report that it re-enumerated.
#   USB_SETTLE_S          seconds to let a just-powered device enumerate before
#                         a cycle (default 4). A cycle started sooner sees
#                         nothing on the port (#730).
#   USB_TOGGLE_NETS       ports this suite may switch (comma or space
#                         separated). USB_NET must be one of them, so a typo
#                         or a changed default cannot cut power to a probe.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/../../framework/colors.sh"
source "${SCRIPT_DIR}/../../framework/harness.sh"

set +e

if [ $# -lt 2 ]; then
  echo "Usage: $0 <BOX> <USB_NET>"
  echo "  USB_NET is the ONLY port this suite switches."
  exit 1
fi

BOX="$1"
USB_NET="$2"
EXPECT_DEVICE="${USB_EXPECT_DEVICE:-0}"
SETTLE="${USB_SETTLE_S:-4}"

case " ${USB_TOGGLE_NETS//,/ } " in
  *" $USB_NET "*) ;;
  *)
    echo "Refusing to switch '$USB_NET': it is not in USB_TOGGLE_NETS ('${USB_TOGGLE_NETS:-}')." >&2
    exit 2 ;;
esac

init_harness

# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------

# Print "enabled" or "disabled" for $USB_NET, or nothing when unreadable.
port_state() {
  lager usb "$USB_NET" state --box "$BOX" 2>/dev/null \
    | sed -nE "s/.*USB port '[^']+' is (enabled|disabled).*/\1/p"
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

# $1 = exit status, $2 = output, $3 = pattern the output must contain.
succeeded() { [ "$1" -eq 0 ] && printf '%s' "$2" | grep -qE "$3"; }
# The message a cycle must print: re-enumerated when a device is expected,
# any of the four outcomes otherwise.
cycle_ok() {
  if [ "$EXPECT_DEVICE" = "1" ]; then
    succeeded "$1" "$2" "power-cycled; device re-enumerated"
  else
    succeeded "$1" "$2" "power-cycled"
  fi
}
refused() { [ "$1" -ne 0 ] && printf '%s' "$2" | grep -qiE "$3"; }
# An empty expectation never matches: an unreadable state is not a pass.
state_is() { [ -n "$1" ] && [ "$(port_state)" = "$1" ]; }

# Run `lager usb $USB_NET <action>` into OUT/RC.
act() {
  OUT=$(lager usb "$USB_NET" "$@" --box "$BOX" 2>&1); RC=$?
  echo "    $ lager usb $USB_NET $* -> rc=$RC: $(printf '%s' "$OUT" | head -1)"
}

INITIAL_STATE=$(port_state)

restore_port() {
  if [ "$INITIAL_STATE" = "disabled" ]; then
    lager usb "$USB_NET" disable --box "$BOX" >/dev/null 2>&1
  else
    lager usb "$USB_NET" enable --box "$BOX" >/dev/null 2>&1
  fi
}
harness_add_cleanup restore_port

echo "========================================================================"
echo "LAGER USB SUITE"
echo "========================================================================"
echo "Box:           $BOX"
echo "USB net:       $USB_NET"
echo "Initial state: ${INITIAL_STATE:-unreadable}"
echo ""

# ============================================================
# SECTION 1: LISTING AND READ-ONLY STATE
# ============================================================
start_section "Listing and State"

OUT=$(lager usb --box "$BOX" 2>&1); RC=$?
check "1.1 'lager usb' lists the net" "$OUT" \
  succeeded "$RC" "$OUT" "(^|[[:space:]])${USB_NET}[[:space:]]"

OUT=$(lager usb --help 2>&1); RC=$?
check "1.2 help names every subcommand" "$OUT" \
  bash -c 'for c in enable disable toggle state cycle recover; do
             printf "%s" "$1" | grep -qE "^  $c " || exit 1; done' _ "$OUT"

check "1.3 state reads enabled or disabled" "${INITIAL_STATE:-empty}" \
  bash -c '[ "$1" = enabled ] || [ "$1" = disabled ]' _ "$INITIAL_STATE"

act state
check "1.4 state names the net" "$OUT" succeeded "$RC" "$OUT" "USB port '${USB_NET}' is "

# ============================================================
# SECTION 2: REFUSALS (nothing reaches the hub)
# ============================================================
start_section "Refusals"

OUT=$(lager usb ci_no_such_usb_net enable --box "$BOX" 2>&1); RC=$?
check "2.1 unknown net is refused" "$OUT" refused "$RC" "$OUT" "not found|no .*net"

OUT=$(lager usb "$USB_NET" no_such_action --box "$BOX" 2>&1); RC=$?
check "2.2 unknown action is refused" "$OUT" refused "$RC" "$OUT" "no such command|usage"

# Subcommands are case-sensitive; ENABLE is not an alias.
OUT=$(lager usb "$USB_NET" ENABLE --box "$BOX" 2>&1); RC=$?
check "2.3 ENABLE (wrong case) is refused" "$OUT" refused "$RC" "$OUT" "no such command|usage"

# A saved net of another role. The role check is client-side, so nothing is
# sent to that net's instrument.
WRONG_ROLE_NET=$(lager nets --box "$BOX" 2>/dev/null \
  | sed -nE 's/^(├|└)── +([^ ]+) +([^ ]+) .*/\2 \3/p' | awk '$2 != "usb" {print $1; exit}')
OUT=$(lager usb "$WRONG_ROLE_NET" enable --box "$BOX" 2>&1); RC=$?
check "2.4 a ${WRONG_ROLE_NET:-non-usb} net is refused as a USB net" "$OUT" \
  refused "$RC" "$OUT" "not found|not a usb|role|no .*net"

OUT=$(lager usb "$USB_NET" enable --box CI-NO-SUCH-BOX 2>&1); RC=$?
check "2.5 unknown box is refused" "$OUT" refused "$RC" "$OUT" "box|not found|unknown|error"

check "2.6 the port state is unchanged by the refusals" "$(port_state)" state_is "$INITIAL_STATE"

# ============================================================
# SECTION 3: ENABLE / DISABLE
# ============================================================
start_section "Enable and Disable"

act enable
check "3.1 enable succeeds and says enabled" "$OUT" succeeded "$RC" "$OUT" "USB port '${USB_NET}' enabled"
check "3.2 state reads enabled" "$(port_state)" state_is enabled

act enable
check "3.3 enable again is idempotent" "$OUT" succeeded "$RC" "$OUT" "enabled"
check "3.4 state still enabled" "$(port_state)" state_is enabled

act disable
check "3.5 disable succeeds and says disabled" "$OUT" succeeded "$RC" "$OUT" "USB port '${USB_NET}' disabled"
check "3.6 state reads disabled" "$(port_state)" state_is disabled

act disable
check "3.7 disable again is idempotent" "$OUT" succeeded "$RC" "$OUT" "disabled"
check "3.8 state still disabled" "$(port_state)" state_is disabled

# ============================================================
# SECTION 4: TOGGLE
# ============================================================
start_section "Toggle"

act toggle
check "4.1 toggle from disabled reports enabled" "$OUT" succeeded "$RC" "$OUT" "toggled .* enabled"
check "4.2 state reads enabled" "$(port_state)" state_is enabled

act toggle
check "4.3 toggle from enabled reports disabled" "$OUT" succeeded "$RC" "$OUT" "toggled .* disabled"
check "4.4 state reads disabled" "$(port_state)" state_is disabled

# Six toggles from disabled: each report must alternate and agree with state.
BAD=0
EXPECT=enabled
for _ in 1 2 3 4 5 6; do
  act toggle
  if ! succeeded "$RC" "$OUT" "toggled .* ${EXPECT}" || ! state_is "$EXPECT"; then
    BAD=$((BAD + 1))
  fi
  [ "$EXPECT" = enabled ] && EXPECT=disabled || EXPECT=enabled
done
check "4.5 six toggles alternate and match state" "$BAD bad" [ "$BAD" -eq 0 ]

# ============================================================
# SECTION 5: CYCLE AND RECOVER
# ============================================================
start_section "Cycle and Recover"

lager usb "$USB_NET" enable --box "$BOX" >/dev/null 2>&1
sleep "$SETTLE"
act cycle --off-time 1
check "5.1 cycle succeeds$([ "$EXPECT_DEVICE" = 1 ] && echo ' and the device re-enumerates')" "$OUT" cycle_ok "$RC" "$OUT"
check "5.2 state reads enabled after a cycle" "$(port_state)" state_is enabled

lager usb "$USB_NET" disable --box "$BOX" >/dev/null 2>&1
act recover
check "5.3 recover succeeds" "$OUT" succeeded "$RC" "$OUT" "power restored"
check "5.4 state reads enabled after recover" "$(port_state)" state_is enabled

OUT=$(lager usb "$USB_NET" cycle --off-time -1 --box "$BOX" 2>&1); RC=$?
check "5.5 a negative --off-time is refused" "$OUT" refused "$RC" "$OUT" "off.time|invalid|must|error"
check "5.6 state unchanged by the refused cycle" "$(port_state)" state_is enabled

# ============================================================
# SECTION 6: REPEATED SWITCHING
# ============================================================
start_section "Repeated Switching"

BAD=0
for _ in $(seq 1 10); do
  lager usb "$USB_NET" disable --box "$BOX" >/dev/null 2>&1 || BAD=$((BAD + 1))
  lager usb "$USB_NET" enable --box "$BOX" >/dev/null 2>&1 || BAD=$((BAD + 1))
done
check "6.1 ten disable/enable pairs all exit 0" "$BAD failed" [ "$BAD" -eq 0 ]
check "6.2 state reads enabled after the pairs" "$(port_state)" state_is enabled

# ============================================================
# SECTION 7: RESTORE
# ============================================================
start_section "Restore"

if [ "$INITIAL_STATE" = "disabled" ]; then
  act disable
  check "7.1 port restored to disabled" "$OUT" state_is disabled
else
  # Ends powered, and waits for the device to come back for later steps.
  sleep "$SETTLE"
  act cycle --off-time 1
  check "7.1 port restored to enabled, device back" "$OUT" cycle_ok "$RC" "$OUT"
fi

print_summary
exit_with_status
