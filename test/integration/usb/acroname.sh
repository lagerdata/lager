#!/bin/bash

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
#
# Acroname hub: detection and port inventory. READ-ONLY.
#
# This suite never switches a port. On a bench the other ports of the hub
# power probes and peers that later steps depend on, so switching is tested
# by usb.sh on the one port it is given. Here every check reads: the hub is
# detected, its ports are saved as usb nets numbered 0..N-1 with no gaps or
# repeats, and every port's state can be read.
#
# Usage: acroname.sh <BOX>

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/../../framework/colors.sh"
source "${SCRIPT_DIR}/../../framework/harness.sh"

set +e

if [ $# -lt 1 ]; then
  echo "Usage: $0 <BOX>"
  exit 1
fi

BOX="$1"

init_harness

# "0<sep>1<sep>...<sep>N-1". Not `seq -s`: BSD seq appends a trailing separator.
port_list() {
  local sep="$1" n="$2" i out=""
  for (( i=0; i<n; i++ )); do out="${out:+$out$sep}$i"; done
  printf '%s' "$out"
}

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

echo "========================================================================"
echo "ACRONAME HUB INVENTORY SUITE (read-only)"
echo "========================================================================"
echo "Box: $BOX"
echo ""

# ============================================================
# SECTION 1: DETECTION
# ============================================================
start_section "Detection"

INSTRUMENTS=$(lager instruments --box "$BOX" 2>&1); RC=$?
check "1.1 instruments listing exits 0" "$INSTRUMENTS" [ "$RC" -eq 0 ]

# The first Acroname hub row: its model and the channel list it reports.
HUB_ROW=$(printf '%s\n' "$INSTRUMENTS" | grep -E '^Acroname_(4|8)Port ' | head -1)
HUB_MODEL=$(printf '%s' "$HUB_ROW" | awk '{print $1}')
check "1.2 an Acroname hub is detected" "$INSTRUMENTS" [ -n "$HUB_MODEL" ]

case "$HUB_MODEL" in
  Acroname_4Port) PORTS=4 ;;
  Acroname_8Port) PORTS=8 ;;
  *) PORTS=0 ;;
esac
EXPECTED_CHANNELS=$(port_list ', ' "$PORTS")
check "1.3 the hub reports channels 0..$((PORTS - 1))" "$HUB_ROW" \
  bash -c 'printf "%s" "$1" | grep -qF "usb: $2"' _ "$HUB_ROW" "$EXPECTED_CHANNELS"

# ============================================================
# SECTION 2: SAVED PORT NETS
# ============================================================
start_section "Saved Port Nets"

NETS=$(lager nets --box "$BOX" 2>&1)
# "<name> <channel>" for every usb net under the first Acroname hub heading.
PORT_NETS=$(printf '%s\n' "$NETS" | awk -v model="${HUB_MODEL#Acroname_}" '
  /^Acroname / { inhub = (index($0, "Acroname " model) == 1); next }
  /^[^ ├└]/    { inhub = 0 }
  inhub && /^(├|└)── / { if ($3 == "usb") print $2, $4 }')
echo "  port nets:"
printf '%s\n' "$PORT_NETS" | sed 's/^/    /'

COUNT=$(printf '%s\n' "$PORT_NETS" | grep -c .)
check "2.1 one usb net per port ($PORTS)" "$COUNT nets" \
  bash -c '[ "$1" -gt 0 ] && [ "$2" -eq "$1" ]' _ "$PORTS" "$COUNT"

CHANNELS=$(printf '%s\n' "$PORT_NETS" | awk '{print $2}' | sort -n | tr '\n' ' ' | sed 's/ $//')
check "2.2 port channels are 0..$((PORTS - 1)), no gaps or repeats" "$CHANNELS" \
  bash -c '[ "$1" -gt 0 ] && [ "$2" = "$3" ]' _ "$PORTS" "$CHANNELS" "$(port_list ' ' "$PORTS")"

USB_LIST=$(lager usb --box "$BOX" 2>&1); RC=$?
MISSING=""
for n in $(printf '%s\n' "$PORT_NETS" | awk '{print $1}'); do
  printf '%s\n' "$USB_LIST" | grep -qE "^${n}[[:space:]]" || MISSING="$MISSING $n"
done
check "2.3 'lager usb' lists every port net" "missing:$MISSING" \
  bash -c '[ "$1" -eq 0 ] && [ "$3" -gt 0 ] && [ -z "$2" ]' _ "$RC" "$MISSING" "$COUNT"

# ============================================================
# SECTION 3: PORT STATE (read-only)
# ============================================================
start_section "Port State"

BAD=""
for n in $(printf '%s\n' "$PORT_NETS" | awk '{print $1}'); do
  OUT=$(lager usb "$n" state --box "$BOX" 2>&1)
  echo "    $OUT"
  printf '%s' "$OUT" | grep -qE "USB port '${n}' is (enabled|disabled)" || BAD="$BAD $n"
done
check "3.1 every port's state reads enabled or disabled" "unreadable:$BAD" \
  bash -c '[ "$1" -gt 0 ] && [ -z "$2" ]' _ "$COUNT" "$BAD"

print_summary
exit_with_status
