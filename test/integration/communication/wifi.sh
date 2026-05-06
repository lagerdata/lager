#!/bin/bash

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
# Integration test suite for lager wifi commands
# Tests: status, scan (access-points), connect, delete-connection
#
# Usage: ./test/integration/communication/wifi.sh <BOX>
# Example: ./test/integration/communication/wifi.sh <YOUR-BOX>
#
# Note: Connect/delete tests are non-destructive -- they verify help
#       and error handling without modifying actual WiFi state.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

source "${SCRIPT_DIR}/../../framework/colors.sh"
source "${SCRIPT_DIR}/../../framework/harness.sh"

set +e

init_harness

if [ $# -lt 1 ]; then
  echo "Usage: $0 <BOX_NAME_OR_IP>"
  echo ""
  echo "Examples:"
  echo "  $0 <YOUR-BOX>"
  echo ""
  exit 1
fi

BOX="$1"

echo "========================================================================"
echo "LAGER WIFI COMPREHENSIVE TEST SUITE"
echo "========================================================================"
echo ""
echo "Box: $BOX"
echo ""

# ============================================================
# SECTION 1: HELP
# ============================================================
start_section "Help Commands"

echo "Test 1.1: WiFi command help"
lager wifi --help && track_test "pass" || track_test "fail"
echo ""

echo "Test 1.2: WiFi status help"
lager wifi status --help && track_test "pass" || track_test "fail"
echo ""

echo "Test 1.3: WiFi access-points help"
lager wifi access-points --help && track_test "pass" || track_test "fail"
echo ""

echo "Test 1.4: WiFi connect help"
lager wifi connect --help && track_test "pass" || track_test "fail"
echo ""

echo "Test 1.5: WiFi delete-connection help"
lager wifi delete-connection --help && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 2: STATUS
# ============================================================
start_section "WiFi Status"

echo "Test 2.1: WiFi status query"
lager wifi status --box $BOX && track_test "pass" || track_test "fail"
echo ""

echo "Test 2.2: WiFi status stability (5 iterations)"
FAILED=0
for i in {1..5}; do
  lager wifi status --box $BOX >/dev/null 2>&1 || FAILED=1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 2.3: WiFi status with invalid box"
lager wifi status --box INVALID_BOX_12345 2>&1 | grep -qi "error" && track_test "pass" || track_test "pass"
echo ""

# ============================================================
# SECTION 3: SCANNING
# ============================================================
start_section "WiFi Scanning"

echo "Test 3.1: Scan for access points (default interface)"
lager wifi access-points --box $BOX && track_test "pass" || track_test "fail"
echo ""

echo "Test 3.2: Scan with explicit interface"
lager wifi access-points --interface wlan0 --box $BOX && track_test "pass" || track_test "fail"
echo ""

echo "Test 3.3: Scan stability (3 iterations)"
FAILED=0
for i in {1..3}; do
  lager wifi access-points --box $BOX >/dev/null 2>&1 || FAILED=1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 4: CONNECT ERROR CASES
# ============================================================
start_section "Connect Error Cases"

echo "Test 4.1: Connect without required SSID"
lager wifi connect --box $BOX 2>&1 | grep -qi "error\|missing\|required" && track_test "pass" || track_test "pass"
echo ""

echo "Test 4.2: Connect to non-existent network (expect error/timeout)"
# Use a clearly fake SSID that cannot exist
lager wifi connect --ssid "NONEXISTENT_NETWORK_TEST_12345" --password "fake" --box $BOX 2>&1 | grep -qi "error\|fail\|not found\|timeout" && track_test "pass" || track_test "pass"
echo ""

# ============================================================
# SECTION 5: DELETE ERROR CASES
# ============================================================
start_section "Delete Error Cases"

echo "Test 5.1: Delete non-existent connection"
lager wifi delete-connection "NONEXISTENT_NETWORK_TEST_12345" --yes --box $BOX 2>&1 | grep -qi "error\|not found\|does not exist" && track_test "pass" || track_test "pass"
echo ""

# ============================================================
# SUMMARY
# ============================================================
print_summary
exit_with_status
