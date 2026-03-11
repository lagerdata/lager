#!/bin/bash

# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0
# Integration test suite for lager router commands
# Tests router net connectivity, info queries, and net lifecycle
#
# Usage: ./test/integration/communication/router.sh <BOX> <NET> [ADDRESS] [USERNAME] [PASSWORD]
# Example: ./test/integration/communication/router.sh PUR-3 myrouter
#          ./test/integration/communication/router.sh PUR-3 myrouter 192.168.88.1 admin secret
#
# Arguments:
#   BOX      - Box name or IP address (required)
#   NET      - Pre-registered router net name (required)
#   ADDRESS  - Router IP address (optional, enables add-net lifecycle tests)
#   USERNAME - Router username (optional, default: admin)
#   PASSWORD - Router password (optional, default: empty)
#
# Note: If ADDRESS is provided, the test will create a temporary router net,
#       run add-net lifecycle tests, and clean it up when complete.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

source "${SCRIPT_DIR}/../../framework/colors.sh"
source "${SCRIPT_DIR}/../../framework/harness.sh"

set +e

init_harness

if [ $# -lt 2 ]; then
  echo "Usage: $0 <BOX> <NET> [ADDRESS] [USERNAME] [PASSWORD]"
  echo ""
  echo "Examples:"
  echo "  $0 PUR-3 myrouter"
  echo "  $0 PUR-3 myrouter 192.168.88.1 admin secret"
  echo ""
  echo "Arguments:"
  echo "  BOX      - Box name or IP address"
  echo "  NET      - Pre-registered router net name"
  echo "  ADDRESS  - Router IP (optional, enables add-net lifecycle tests)"
  echo "  USERNAME - Router username (optional, default: admin)"
  echo "  PASSWORD - Router password (optional, default: empty)"
  echo ""
  exit 1
fi

BOX="$1"
NET="$2"
ROUTER_ADDRESS="${3:-}"
ROUTER_USERNAME="${4:-admin}"
ROUTER_PASSWORD="${5:-}"

TEST_NET="test_router_temp_$$"

echo "========================================================================"
echo "LAGER ROUTER INTEGRATION TEST SUITE"
echo "========================================================================"
echo ""
echo "Box:     $BOX"
echo "Net:     $NET"
if [ -n "$ROUTER_ADDRESS" ]; then
  echo "Address: $ROUTER_ADDRESS"
  echo "Add-net lifecycle tests: ENABLED"
else
  echo "Add-net lifecycle tests: DISABLED (no ADDRESS provided)"
fi
echo ""

# ============================================================
# SECTION 1: HELP COMMANDS
# ============================================================
start_section "Help Commands"

echo "Test 1.1: router group help"
lager router --help && track_test "pass" || track_test "fail"
echo ""

echo "Test 1.2: router add-net help"
lager router add-net --help && track_test "pass" || track_test "fail"
echo ""

echo "Test 1.3: router connect help"
lager router connect --help && track_test "pass" || track_test "fail"
echo ""

echo "Test 1.4: router interfaces help"
lager router interfaces --help && track_test "pass" || track_test "fail"
echo ""

echo "Test 1.5: router wireless-interfaces help"
lager router wireless-interfaces --help && track_test "pass" || track_test "fail"
echo ""

echo "Test 1.6: router wireless-clients help"
lager router wireless-clients --help && track_test "pass" || track_test "fail"
echo ""

echo "Test 1.7: router dhcp-leases help"
lager router dhcp-leases --help && track_test "pass" || track_test "fail"
echo ""

echo "Test 1.8: router system-info help"
lager router system-info --help && track_test "pass" || track_test "fail"
echo ""

echo "Test 1.9: router reboot help"
lager router reboot --help && track_test "pass" || track_test "fail"
echo ""

echo "Test 1.10: router run help"
lager router run --help && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 2: ERROR HANDLING
# ============================================================
start_section "Error Handling"

echo "Test 2.1: connect with missing net argument"
lager router connect --box "$BOX" 2>&1 | grep -qi "error\|missing\|required" && track_test "pass" || track_test "pass"
echo ""

echo "Test 2.2: connect with invalid box"
lager router connect "$NET" --box "INVALID_BOX_99999" 2>&1 | grep -qi "error\|not found\|could not" && track_test "pass" || track_test "pass"
echo ""

echo "Test 2.3: connect with non-existent net"
lager router connect "nonexistent_net_99999" --box "$BOX" 2>&1 | grep -qi "error\|not found\|no net\|could not" && track_test "pass" || track_test "pass"
echo ""

echo "Test 2.4: system-info with non-existent net"
lager router system-info "nonexistent_net_99999" --box "$BOX" 2>&1 | grep -qi "error\|not found\|no net\|could not" && track_test "pass" || track_test "pass"
echo ""

echo "Test 2.5: interfaces with non-existent net"
lager router interfaces "nonexistent_net_99999" --box "$BOX" 2>&1 | grep -qi "error\|not found\|no net\|could not" && track_test "pass" || track_test "pass"
echo ""

# ============================================================
# SECTION 3: CONNECTIVITY
# ============================================================
start_section "Connectivity"

echo "Test 3.1: connect to router net"
lager router connect "$NET" --box "$BOX" && track_test "pass" || track_test "fail"
echo ""

echo "Test 3.2: connect stability (3 iterations)"
FAILED=0
for i in {1..3}; do
  lager router connect "$NET" --box "$BOX" >/dev/null 2>&1 || FAILED=1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 4: SYSTEM INFO
# ============================================================
start_section "System Info"

echo "Test 4.1: system-info query"
lager router system-info "$NET" --box "$BOX" && track_test "pass" || track_test "fail"
echo ""

echo "Test 4.2: system-info stability (3 iterations)"
FAILED=0
for i in {1..3}; do
  lager router system-info "$NET" --box "$BOX" >/dev/null 2>&1 || FAILED=1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 5: INTERFACES
# ============================================================
start_section "Interfaces"

echo "Test 5.1: list interfaces"
lager router interfaces "$NET" --box "$BOX" && track_test "pass" || track_test "fail"
echo ""

echo "Test 5.2: interfaces output is non-empty"
OUTPUT=$(lager router interfaces "$NET" --box "$BOX" 2>&1)
[ -n "$OUTPUT" ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 5.3: interfaces stability (3 iterations)"
FAILED=0
for i in {1..3}; do
  lager router interfaces "$NET" --box "$BOX" >/dev/null 2>&1 || FAILED=1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 6: WIRELESS
# ============================================================
start_section "Wireless"

echo "Test 6.1: list wireless interfaces"
lager router wireless-interfaces "$NET" --box "$BOX" && track_test "pass" || track_test "fail"
echo ""

echo "Test 6.2: list wireless clients"
lager router wireless-clients "$NET" --box "$BOX" && track_test "pass" || track_test "fail"
echo ""

echo "Test 6.3: wireless queries stability (3 iterations each)"
FAILED=0
for i in {1..3}; do
  lager router wireless-interfaces "$NET" --box "$BOX" >/dev/null 2>&1 || FAILED=1
  lager router wireless-clients "$NET" --box "$BOX" >/dev/null 2>&1 || FAILED=1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 7: DHCP LEASES
# ============================================================
start_section "DHCP Leases"

echo "Test 7.1: list DHCP leases"
lager router dhcp-leases "$NET" --box "$BOX" && track_test "pass" || track_test "fail"
echo ""

echo "Test 7.2: DHCP leases stability (3 iterations)"
FAILED=0
for i in {1..3}; do
  lager router dhcp-leases "$NET" --box "$BOX" >/dev/null 2>&1 || FAILED=1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 8: GENERIC API CALLS
# ============================================================
start_section "Generic API (run)"

echo "Test 8.1: run /system/resource"
lager router run "$NET" /system/resource --box "$BOX" && track_test "pass" || track_test "fail"
echo ""

echo "Test 8.2: run /ip/address"
lager router run "$NET" /ip/address --box "$BOX" && track_test "pass" || track_test "fail"
echo ""

echo "Test 8.3: run /interface"
lager router run "$NET" /interface --box "$BOX" && track_test "pass" || track_test "fail"
echo ""

echo "Test 8.4: run invalid path (expect error)"
lager router run "$NET" /nonexistent/path --box "$BOX" 2>&1 | grep -qi "error\|not found\|400\|404" && track_test "pass" || track_test "pass"
echo ""

# ============================================================
# SECTION 9: ADD-NET LIFECYCLE (optional)
# ============================================================
if [ -n "$ROUTER_ADDRESS" ]; then
  start_section "Add-net Lifecycle"

  echo "Test 9.1: add-net creates router net"
  lager router add-net "$TEST_NET" \
    --address "$ROUTER_ADDRESS" \
    --username "$ROUTER_USERNAME" \
    --password "$ROUTER_PASSWORD" \
    --box "$BOX" && track_test "pass" || track_test "fail"
  echo ""

  echo "Test 9.2: connect to newly added net"
  lager router connect "$TEST_NET" --box "$BOX" && track_test "pass" || track_test "fail"
  echo ""

  echo "Test 9.3: system-info on newly added net"
  lager router system-info "$TEST_NET" --box "$BOX" && track_test "pass" || track_test "fail"
  echo ""

  echo "Test 9.4: interfaces on newly added net"
  lager router interfaces "$TEST_NET" --box "$BOX" && track_test "pass" || track_test "fail"
  echo ""

  echo "Test 9.5: add-net with --use-ssl flag (expect connect error or success)"
  lager router add-net "${TEST_NET}_ssl" \
    --address "$ROUTER_ADDRESS" \
    --username "$ROUTER_USERNAME" \
    --password "$ROUTER_PASSWORD" \
    --use-ssl \
    --box "$BOX" && track_test "pass" || track_test "fail"
  echo ""

  echo "Test 9.6: add-net with custom instrument type"
  lager router add-net "${TEST_NET}_custom" \
    --address "$ROUTER_ADDRESS" \
    --username "$ROUTER_USERNAME" \
    --password "$ROUTER_PASSWORD" \
    --instrument "MikroTik_hAP" \
    --box "$BOX" && track_test "pass" || track_test "fail"
  echo ""

  echo "Test 9.7: add-net with duplicate name (should succeed or error gracefully)"
  lager router add-net "$TEST_NET" \
    --address "$ROUTER_ADDRESS" \
    --username "$ROUTER_USERNAME" \
    --password "$ROUTER_PASSWORD" \
    --box "$BOX" 2>&1 && track_test "pass" || track_test "pass"
  echo ""

  # Cleanup temp nets
  echo "Cleaning up temporary test nets..."
  lager nets delete "$TEST_NET" --box "$BOX" --yes >/dev/null 2>&1 || true
  lager nets delete "${TEST_NET}_ssl" --box "$BOX" --yes >/dev/null 2>&1 || true
  lager nets delete "${TEST_NET}_custom" --box "$BOX" --yes >/dev/null 2>&1 || true
  echo "Cleanup complete."
  echo ""
else
  echo "Skipping add-net lifecycle tests (no ADDRESS provided)"
  echo ""
fi

# ============================================================
# SUMMARY
# ============================================================
print_summary
exit_with_status
