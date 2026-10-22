#!/bin/bash

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
#
# The box's radios, read-only: a BLE scan, its filters and its limits, and the
# Wi-Fi checks the CLI makes before it contacts the box.
#
# Nothing here changes radio state. It never connects, pairs or disconnects a
# BLE device, and never runs `lager wifi connect` or `delete-connection`. On
# a bench whose only uplink is Wi-Fi, either one can take the box off the
# network.
#
# The scans' own output is never printed, not even on failure. It names every
# advertiser near the box, and bench logs are public. Only the scan line, the
# device count, a "No ..." line and any error line are, through summary().
#
# `lager wifi status` and `access-points` are not run against the box: on a
# box whose container uses the default network they cannot see its Wi-Fi
# (status reports "Interface detection failed", the scan needs nmcli), so a
# check could only fail.
#
# Usage: radios.sh <BOX>
#
# Run by hand: bash test/integration/communication/radios.sh MASTER

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/../../framework/colors.sh"
source "${SCRIPT_DIR}/../../framework/harness.sh"

set +e

if [ $# -lt 1 ]; then
  echo "Usage: $0 <BOX>"
  exit 1
fi

BOX="$1"
export LAGER_NO_UPDATE_CHECK=1
# Matches no advertiser, so a filtered scan must find nothing.
NO_SUCH_NAME="lager-ci-no-such-device-7f3a"

init_harness

# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------

# The lines of a scan's output that name no device.
summary() {
  printf '%s\n' "$1" | grep -E '^(Scanning|Found [0-9]+ |No |Error)' | head -4
}

# Run one check. $1 = description, $2 = detail printed on failure (already
# summarised), the rest is the condition, run as a command: exit 0 passes.
check() {
  local desc="$1" detail="$2"
  shift 2
  echo -n "Test: $desc... "
  if "$@"; then
    track_test "pass"
  else
    track_test "fail"
    [ -n "$detail" ] && echo "    detail: $(printf '%s' "$detail" | head -4 | tr '\n' ' ')"
  fi
}

# $1 = exit status, $2 = output, $3 = pattern the output must contain.
succeeded() { [ "$1" -eq 0 ] && printf '%s' "$2" | grep -qE -- "$3"; }
refused() { [ "$1" -ne 0 ] && printf '%s' "$2" | grep -qE -- "$3"; }

# Run lager into OUT and RC, and print only the summary.
run() {
  OUT=$(lager "$@" 2>&1); RC=$?
  SUM=$(summary "$OUT")
  echo "    $ lager $* -> rc=$RC: $(printf '%s' "$SUM" | tr '\n' ' ')"
}

print_script_header "Radios (read-only)" "$BOX"

# ------------------------------------------------------------
start_section "BLE scan"
# ------------------------------------------------------------

run ble scan --box "$BOX" --timeout 3
check "a 3 s scan succeeds" "$SUM" succeeded "$RC" "$OUT" "^Scanning for BLE devices for 3.0 seconds"
check "it reports a device count" "$SUM" succeeded "$RC" "$OUT" "^Found [0-9]+ device"
# A second scan straight after: the box releases the adapter between scans.
run ble scan --box "$BOX" --timeout 2
check "a second scan straight after succeeds" "$SUM" succeeded "$RC" "$OUT" "^Found [0-9]+ device"

run ble scan --box "$BOX" --timeout 2 --name-exact "$NO_SUCH_NAME"
check "an exact-name filter that matches nothing finds 0" "$SUM" succeeded "$RC" "$OUT" "^Found 0 device"
check "and says no device matched" "$SUM" succeeded "$RC" "$OUT" "No devices found matching filter criteria"

run ble scan --box "$BOX" --timeout 2 --name-contains "$NO_SUCH_NAME"
check "a name-contains filter that matches nothing finds 0" "$SUM" succeeded "$RC" "$OUT" "^Found 0 device"

# ------------------------------------------------------------
start_section "BLE scan limits (refused before the box)"
# ------------------------------------------------------------

run ble scan --box "$BOX" --timeout 0.01
check "a timeout under 0.1 s is refused" "$SUM" refused "$RC" "$OUT" "Timeout must be between 0.1 and 300"
run ble scan --box "$BOX" --timeout 301
check "a timeout over 300 s is refused" "$SUM" refused "$RC" "$OUT" "Timeout must be between 0.1 and 300"

# ------------------------------------------------------------
start_section "Wi-Fi checks (refused before the box)"
# ------------------------------------------------------------

run wifi --help
check "lager wifi --help" "" succeeded "$RC" "$OUT" "access-points"
run wifi access-points --box "$BOX" --interface wlan0-name-too-long
check "an interface name over 15 characters is refused" "$SUM" \
  refused "$RC" "$OUT" "Interface name too long"

print_summary
exit_with_status
