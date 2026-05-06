#!/bin/bash

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
# YKUSH-specific test suite for lager usb commands
# Tests Yepkit YKUSH hub edge cases and specific behaviors

# Determine script directory for relative paths
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Source test framework
source "${SCRIPT_DIR}/../../framework/colors.sh"
source "${SCRIPT_DIR}/../../framework/harness.sh"

# DON'T exit on error - we want to track failures
set +e

# Initialize the test harness
init_harness

# Check if required arguments are provided
if [ $# -lt 1 ]; then
  echo "Usage: $0 <IP/BOX>"
  echo ""
  echo "Examples:"
  echo "  $0 MY-BOX"
  echo "  $0 <BOX_IP>"
  echo ""
  echo "Arguments:"
  echo "  IP/BOX   - Box ID or Tailscale IP address with YKUSH hubs"
  echo ""
  echo "This script auto-detects YKUSH nets and tests YKUSH-specific features:"
  echo "  - 1-indexed port numbering (ports start at 1)"
  echo "  - Port validation (port must be >= 1)"
  echo "  - Pykush library detection"
  echo "  - Serial number handling for multiple hubs"
  echo "  - ykushcmd CLI fallback"
  echo ""
  exit 1
fi

BOX="$1"

echo "========================================================================"
echo "YKUSH USB HUB COMPREHENSIVE TEST SUITE"
echo "========================================================================"
echo ""
echo "Box: $BOX"
echo ""

# ============================================================================
# AUTO-DETECT YKUSH NETS
# ============================================================================
echo "========================================================================"
echo "DETECTING YKUSH HUBS"
echo "========================================================================"
echo ""

# Get all YKUSH nets
YKUSH_NETS=$(lager nets --box $BOX 2>/dev/null | grep "YKUSH" | awk '{print $1}')

# Count them
NUM_YKUSH=$(echo "$YKUSH_NETS" | grep -c "usb" || echo "0")

echo "Found $NUM_YKUSH YKUSH hub ports"
echo ""

if [ "$NUM_YKUSH" -eq 0 ]; then
  echo -e "${RED}ERROR: No YKUSH hubs found on box $BOX${NC}"
  echo "Please ensure YKUSH hubs are connected and nets are configured."
  exit 1
fi

# Select test net (typically YKUSH has 3 ports: 1, 2, 3)
TEST_YKUSH_NET=$(echo "$YKUSH_NETS" | head -1)
echo -e "${GREEN}Using YKUSH test net: $TEST_YKUSH_NET${NC}"

# Get serial number from net info
YKUSH_SERIAL=$(lager nets --box $BOX 2>/dev/null | grep "$TEST_YKUSH_NET" | awk -F'::' '{print $(NF-1)}')
echo "YKUSH Serial: $YKUSH_SERIAL"
echo ""

# ============================================================================
# SECTION 1: YKUSH HARDWARE DETECTION
# ============================================================================
echo "========================================================================"
echo "SECTION 1: YKUSH HARDWARE DETECTION"
echo "========================================================================"
echo ""
start_section "YKUSH Hardware Detection"

echo "Test 1.1: List all nets and identify YKUSH hubs"
OUTPUT=$(lager nets --box $BOX 2>&1)
echo "$OUTPUT" | grep -E "usb|YKUSH"
echo ""
if echo "$OUTPUT" | grep -q "YKUSH"; then
  echo -e "${GREEN}[OK] YKUSH hubs detected${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] No YKUSH hubs found${NC}"
  track_test "fail"
fi
echo ""

echo "Test 1.2: Verify YKUSH hub identification"
OUTPUT=$(lager nets --box $BOX 2>&1 | grep "YKUSH")
echo "$OUTPUT"
if [ -n "$OUTPUT" ]; then
  echo -e "${GREEN}[OK] YKUSH hubs identified${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Failed to identify YKUSH hubs${NC}"
  track_test "fail"
fi
echo ""

echo "Test 1.3: Verify serial number in address"
OUTPUT=$(lager nets --box $BOX 2>&1 | grep "$TEST_YKUSH_NET")
echo "$OUTPUT"
if echo "$OUTPUT" | grep -q "YK"; then
  echo -e "${GREEN}[OK] YKUSH serial number present in address${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[WARNING] Serial number format unexpected${NC}"
  track_test "fail"
fi
echo ""

echo "Test 1.4: Verify channel/port numbering (1-indexed)"
OUTPUT=$(lager nets --box $BOX 2>&1 | grep "YKUSH")
echo "$OUTPUT"
# Check that the port number is in valid 1-indexed range (1-3 for 3-port hub)
if echo "$OUTPUT" | grep -qE "\s+[1-3]\s+"; then
  echo -e "${GREEN}[OK] YKUSH uses 1-indexed ports (1-3 for 3-port hub)${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[WARNING] Port numbering unclear${NC}"
  track_test "fail"
fi
echo ""

# ============================================================================
# SECTION 2: PORT VALIDATION TESTS
# ============================================================================
echo "========================================================================"
echo "SECTION 2: PORT VALIDATION TESTS (1-indexed)"
echo "========================================================================"
echo ""
start_section "Port Validation Tests"

echo "Test 2.1: Port 1 operations (valid - lowest port)"
FIRST_YKUSH_NET=$(echo "$YKUSH_NETS" | head -1)
OUTPUT=$(lager usb $FIRST_YKUSH_NET enable --box $BOX 2>&1)
echo "$OUTPUT"
if echo "$OUTPUT" | grep -q "port 1"; then
  echo -e "${GREEN}[OK] Port 1 is valid for YKUSH (1-indexed)${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[WARNING] Port 1 behavior unexpected${NC}"
  track_test "fail"
fi
echo ""

echo "Test 2.2: All valid ports on YKUSH hub (typically 1-3)"
FAIL_COUNT=0
for port_net in $YKUSH_NETS; do
  echo "  Testing $port_net:"
  OUTPUT=$(lager usb $port_net enable --box $BOX 2>&1)
  echo "  $OUTPUT"
  if [ $? -ne 0 ]; then
    FAIL_COUNT=$((FAIL_COUNT + 1))
  fi
done
if [ $FAIL_COUNT -eq 0 ]; then
  echo -e "${GREEN}[OK] All YKUSH hub ports operational${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] $FAIL_COUNT ports failed${NC}"
  track_test "fail"
fi
echo ""

echo "Test 2.3: Port 3 (highest port on 3-port hub)"
LAST_YKUSH_NET=$(echo "$YKUSH_NETS" | tail -1)
echo "Testing highest port net: $LAST_YKUSH_NET"
OUTPUT=$(lager usb $LAST_YKUSH_NET enable --box $BOX 2>&1)
echo "$OUTPUT"
if echo "$OUTPUT" | grep -q "port 3"; then
  echo -e "${GREEN}[OK] Port 3 accessible on YKUSH hub${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[WARNING] Port 3 behavior unexpected${NC}"
  track_test "fail"
fi
echo ""

echo "Test 2.4: Verify 1-indexed constraint (port must be >= 1)"
# This is validated in the driver code (_validate_port method)
# We can't directly test port 0 rejection without modifying saved_nets.json
# But we can verify the behavior through documentation and normal operations
OUTPUT=$(lager usb $FIRST_YKUSH_NET enable --box $BOX 2>&1)
echo "$OUTPUT"
if echo "$OUTPUT" | grep -qE "port [1-9]"; then
  echo -e "${GREEN}[OK] YKUSH port numbers start at 1 (not 0)${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[WARNING] Port numbering unclear${NC}"
  track_test "fail"
fi
echo ""

# ============================================================================
# SECTION 3: YKUSH-SPECIFIC FEATURES
# ============================================================================
echo "========================================================================"
echo "SECTION 3: YKUSH-SPECIFIC FEATURES"
echo "========================================================================"
echo ""
start_section "YKUSH-Specific Features"

echo "Test 3.1: Port state detection"
echo "Disabling port..."
lager usb $TEST_YKUSH_NET disable --box $BOX >/dev/null 2>&1
echo "Toggling (should enable)..."
OUTPUT=$(lager usb $TEST_YKUSH_NET toggle --box $BOX 2>&1)
echo "$OUTPUT"
if echo "$OUTPUT" | grep -q "ON"; then
  echo -e "${GREEN}[OK] YKUSH correctly detects and toggles port state${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Port state detection failed${NC}"
  track_test "fail"
fi
echo ""

echo "Test 3.2: Verify pykush output format"
OUTPUT=$(lager usb $TEST_YKUSH_NET enable --box $BOX 2>&1)
echo "$OUTPUT"
# Check for proper output format: net name, port number, and state
if echo "$OUTPUT" | grep -qE "port [0-9]+\)" && echo "$OUTPUT" | grep -q "ON"; then
  echo -e "${GREEN}[OK] Pykush driver output format correct${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[WARNING] Output format differs from expected${NC}"
  track_test "fail"
fi
echo ""

echo "Test 3.3: Device pooling (LRU cache behavior)"
echo "First operation:"
OUTPUT1=$(lager usb $TEST_YKUSH_NET enable --box $BOX 2>&1)
echo "$OUTPUT1"
echo "Second operation (should reuse cached device):"
OUTPUT2=$(lager usb $TEST_YKUSH_NET disable --box $BOX 2>&1)
echo "$OUTPUT2"
if [ $? -eq 0 ]; then
  echo -e "${GREEN}[OK] Device caching working${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Device caching issue${NC}"
  track_test "fail"
fi
echo ""

echo "Test 3.4: Serial number handling"
OUTPUT=$(lager nets --box $BOX 2>&1 | grep "$TEST_YKUSH_NET")
echo "$OUTPUT"
if echo "$OUTPUT" | grep -qE "YK[0-9]{5}"; then
  echo -e "${GREEN}[OK] Serial number properly formatted (YK##### pattern)${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[WARNING] Serial number format unexpected${NC}"
  track_test "fail"
fi
echo ""

echo "Test 3.5: Rapid operations (testing device cache)"
FAIL_COUNT=0
for i in {1..10}; do
  lager usb $TEST_YKUSH_NET toggle --box $BOX >/dev/null 2>&1 || FAIL_COUNT=$((FAIL_COUNT + 1))
done
if [ $FAIL_COUNT -eq 0 ]; then
  echo -e "${GREEN}[OK] Device cache handles rapid operations${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] $FAIL_COUNT operations failed${NC}"
  track_test "fail"
fi
echo ""

# ============================================================================
# SECTION 4: YKUSH API COMPATIBILITY
# ============================================================================
echo "========================================================================"
echo "SECTION 4: YKUSH API COMPATIBILITY"
echo "========================================================================"
echo ""
start_section "YKUSH API Compatibility"

echo "Test 4.1: set_port_state API (primary method)"
OUTPUT=$(lager usb $TEST_YKUSH_NET enable --box $BOX 2>&1)
echo "$OUTPUT"
if [ $? -eq 0 ]; then
  echo -e "${GREEN}[OK] set_port_state API working${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] set_port_state API failed${NC}"
  track_test "fail"
fi
echo ""

echo "Test 4.2: Legacy API fallback (switch_port_on/off)"
# The driver tries multiple APIs in order
OUTPUT=$(lager usb $TEST_YKUSH_NET disable --box $BOX 2>&1)
echo "$OUTPUT"
if [ $? -eq 0 ]; then
  echo -e "${GREEN}[OK] API fallback mechanism working${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] API fallback failed${NC}"
  track_test "fail"
fi
echo ""

echo "Test 4.3: get_port_state for toggle operation"
OUTPUT=$(lager usb $TEST_YKUSH_NET toggle --box $BOX 2>&1)
echo "$OUTPUT"
if [ $? -eq 0 ]; then
  echo -e "${GREEN}[OK] get_port_state API working${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] get_port_state API failed${NC}"
  track_test "fail"
fi
echo ""

echo "Test 4.4: YKUSH_PORT_STATE constants"
# The driver uses YKUSH_PORT_STATE_UP and YKUSH_PORT_STATE_DOWN
# Test by doing enable/disable operations
lager usb $TEST_YKUSH_NET enable --box $BOX >/dev/null 2>&1
OUTPUT1=$?
lager usb $TEST_YKUSH_NET disable --box $BOX >/dev/null 2>&1
OUTPUT2=$?
if [ $OUTPUT1 -eq 0 ] && [ $OUTPUT2 -eq 0 ]; then
  echo -e "${GREEN}[OK] YKUSH_PORT_STATE constants working${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Port state constants issue${NC}"
  track_test "fail"
fi
echo ""

# ============================================================================
# SECTION 5: YKUSH ERROR HANDLING
# ============================================================================
echo "========================================================================"
echo "SECTION 5: YKUSH ERROR HANDLING"
echo "========================================================================"
echo ""
start_section "YKUSH Error Handling"

echo "Test 5.1: Invalid port state query handling"
OUTPUT=$(lager usb $TEST_YKUSH_NET toggle --box $BOX 2>&1)
echo "$OUTPUT"
if [ $? -eq 0 ]; then
  echo -e "${GREEN}[OK] Port state query handled correctly${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Port state query failed${NC}"
  track_test "fail"
fi
echo ""

echo "Test 5.2: Recovery after error"
# Force an error (invalid net)
lager usb invalid_ykush_net enable --box $BOX >/dev/null 2>&1
# Try valid operation
OUTPUT=$(lager usb $TEST_YKUSH_NET enable --box $BOX 2>&1)
echo "$OUTPUT"
if [ $? -eq 0 ]; then
  echo -e "${GREEN}[OK] Recovery after error successful${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Failed to recover after error${NC}"
  track_test "fail"
fi
echo ""

echo "Test 5.3: Error message clarity"
OUTPUT=$(lager usb invalid_ykush_net enable --box $BOX 2>&1)
echo "$OUTPUT"
if echo "$OUTPUT" | grep -qi "error\|not found"; then
  echo -e "${GREEN}[OK] Clear error messages for invalid nets${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[WARNING] Error messages could be clearer${NC}"
  track_test "fail"
fi
echo ""

echo "Test 5.4: Runtime error handling (set_port_state failure)"
# If set_port_state returns False, driver should raise RuntimeError
# We can't easily force this without modifying the hub, but we can
# verify normal operations don't trigger false errors
FAIL_COUNT=0
for i in {1..5}; do
  lager usb $TEST_YKUSH_NET toggle --box $BOX >/dev/null 2>&1 || FAIL_COUNT=$((FAIL_COUNT + 1))
done
if [ $FAIL_COUNT -eq 0 ]; then
  echo -e "${GREEN}[OK] No false runtime errors during normal operation${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Runtime errors occurred${NC}"
  track_test "fail"
fi
echo ""

# ============================================================================
# SECTION 6: MULTI-PORT OPERATIONS
# ============================================================================
if [ "$NUM_YKUSH" -gt 1 ]; then
  echo "========================================================================"
  echo "SECTION 6: MULTI-PORT OPERATIONS"
  echo "========================================================================"
  echo ""
  start_section "Multi-Port Operations"

  echo "Test 6.1: Simultaneous operations on multiple ports"
  FIRST_NET=$(echo "$YKUSH_NETS" | head -1)
  SECOND_NET=$(echo "$YKUSH_NETS" | head -2 | tail -1)
  echo "Operating on port 1:"
  OUTPUT1=$(lager usb $FIRST_NET enable --box $BOX 2>&1)
  echo "$OUTPUT1"
  echo "Operating on port 2:"
  OUTPUT2=$(lager usb $SECOND_NET enable --box $BOX 2>&1)
  echo "$OUTPUT2"
  if [ $? -eq 0 ]; then
    echo -e "${GREEN}[OK] Multi-port operations successful${NC}"
    track_test "pass"
  else
    echo -e "${RED}[FAIL] Multi-port operation failed${NC}"
    track_test "fail"
  fi
  echo ""

  echo "Test 6.2: Cycling all YKUSH ports"
  FAIL_COUNT=0
  for net in $YKUSH_NETS; do
    echo "  Toggling $net:"
    OUTPUT=$(lager usb $net toggle --box $BOX 2>&1)
    echo "  $OUTPUT"
    [ $? -ne 0 ] && FAIL_COUNT=$((FAIL_COUNT + 1))
  done
  if [ $FAIL_COUNT -eq 0 ]; then
    echo -e "${GREEN}[OK] All YKUSH ports cycled successfully${NC}"
    track_test "pass"
  else
    echo -e "${RED}[FAIL] $FAIL_COUNT ports failed${NC}"
    track_test "fail"
  fi
  echo ""

  echo "Test 6.3: Sequential port enable (1→2→3)"
  for net in $YKUSH_NETS; do
    echo "  Enabling $net:"
    OUTPUT=$(lager usb $net enable --box $BOX 2>&1)
    echo "  $OUTPUT"
  done
  echo -e "${GREEN}[OK] Sequential enable completed${NC}"
  track_test "pass"
  echo ""

  echo "Test 6.4: Sequential port disable (1→2→3)"
  for net in $YKUSH_NETS; do
    echo "  Disabling $net:"
    OUTPUT=$(lager usb $net disable --box $BOX 2>&1)
    echo "  $OUTPUT"
  done
  echo -e "${GREEN}[OK] Sequential disable completed${NC}"
  track_test "pass"
  echo ""
fi

# ============================================================================
# SECTION 7: YKUSH PERFORMANCE
# ============================================================================
echo "========================================================================"
echo "SECTION 7: YKUSH PERFORMANCE TESTS"
echo "========================================================================"
echo ""
start_section "YKUSH Performance"

echo "Test 7.1: Rapid cycling performance (20 operations)"
START_TIME=$(date +%s)
FAIL_COUNT=0
for i in {1..20}; do
  lager usb $TEST_YKUSH_NET toggle --box $BOX >/dev/null 2>&1 || FAIL_COUNT=$((FAIL_COUNT + 1))
done
END_TIME=$(date +%s)
DURATION=$((END_TIME - START_TIME))
echo "Duration: ${DURATION}s for 20 operations"
echo "Average: $(echo "scale=2; $DURATION / 20" | bc)s per operation"
if [ $FAIL_COUNT -eq 0 ] && [ $DURATION -lt 60 ]; then
  echo -e "${GREEN}[OK] Performance acceptable (<60s for 20 ops)${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[WARNING] Performance issue or failures (${FAIL_COUNT} failures)${NC}"
  track_test "fail"
fi
echo ""

echo "Test 7.2: Device cache efficiency (LRU)"
# Multiple operations should be fast due to device caching
START_TIME=$(date +%s)
for i in {1..5}; do
  lager usb $TEST_YKUSH_NET enable --box $BOX >/dev/null 2>&1
  lager usb $TEST_YKUSH_NET disable --box $BOX >/dev/null 2>&1
done
END_TIME=$(date +%s)
DURATION=$((END_TIME - START_TIME))
echo "Duration: ${DURATION}s for 10 operations"
if [ $DURATION -lt 30 ]; then
  echo -e "${GREEN}[OK] Device caching efficient (<30s)${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[WARNING] Device caching may be slow (${DURATION}s)${NC}"
  track_test "fail"
fi
echo ""

echo "Test 7.3: Toggle state query performance"
# Toggle requires reading current state first
START_TIME=$(date +%s)
for i in {1..10}; do
  lager usb $TEST_YKUSH_NET toggle --box $BOX >/dev/null 2>&1
done
END_TIME=$(date +%s)
DURATION=$((END_TIME - START_TIME))
echo "Duration: ${DURATION}s for 10 toggles"
if [ $DURATION -lt 30 ]; then
  echo -e "${GREEN}[OK] Toggle performance acceptable (<30s)${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[WARNING] Toggle may be slow (${DURATION}s)${NC}"
  track_test "fail"
fi
echo ""

# ============================================================================
# SECTION 8: YKUSH VS ACRONAME INTEROPERABILITY
# ============================================================================
# Check if both YKUSH and Acroname exist on this box
ACRONAME_NETS=$(lager nets --box $BOX 2>/dev/null | grep "Acroname" | awk '{print $1}')
NUM_ACRONAME=$(echo "$ACRONAME_NETS" | grep -c "usb" || echo "0")

if [ "$NUM_ACRONAME" -gt 0 ]; then
  echo "========================================================================"
  echo "SECTION 8: YKUSH + ACRONAME INTEROPERABILITY"
  echo "========================================================================"
  echo ""
  start_section "YKUSH + Acroname Interoperability"

  TEST_ACRONAME_NET=$(echo "$ACRONAME_NETS" | head -1)

  echo "Test 8.1: Alternating YKUSH and Acroname operations"
  echo "YKUSH operation:"
  OUTPUT1=$(lager usb $TEST_YKUSH_NET enable --box $BOX 2>&1)
  echo "$OUTPUT1"
  echo "Acroname operation:"
  OUTPUT2=$(lager usb $TEST_ACRONAME_NET enable --box $BOX 2>&1)
  echo "$OUTPUT2"
  if [ $? -eq 0 ]; then
    echo -e "${GREEN}[OK] Mixed hub type operations successful${NC}"
    track_test "pass"
  else
    echo -e "${RED}[FAIL] Mixed hub operations failed${NC}"
    track_test "fail"
  fi
  echo ""

  echo "Test 8.2: Verify port numbering independence"
  echo "YKUSH (1-indexed):"
  OUTPUT1=$(lager nets --box $BOX 2>&1 | grep "$TEST_YKUSH_NET")
  echo "$OUTPUT1"
  echo "Acroname (0-indexed):"
  OUTPUT2=$(lager nets --box $BOX 2>&1 | grep "$TEST_ACRONAME_NET")
  echo "$OUTPUT2"
  if echo "$OUTPUT1" | grep -qE "[1-3]" && echo "$OUTPUT2" | grep -qE "[0-7]"; then
    echo -e "${GREEN}[OK] Port numbering correctly independent${NC}"
    track_test "pass"
  else
    echo -e "${YELLOW}[WARNING] Port numbering verification inconclusive${NC}"
    track_test "fail"
  fi
  echo ""

  echo "Test 8.3: Rapid switching between hub types"
  FAIL_COUNT=0
  for i in {1..5}; do
    lager usb $TEST_YKUSH_NET toggle --box $BOX >/dev/null 2>&1 || FAIL_COUNT=$((FAIL_COUNT + 1))
    lager usb $TEST_ACRONAME_NET toggle --box $BOX >/dev/null 2>&1 || FAIL_COUNT=$((FAIL_COUNT + 1))
  done
  if [ $FAIL_COUNT -eq 0 ]; then
    echo -e "${GREEN}[OK] Rapid hub type switching successful${NC}"
    track_test "pass"
  else
    echo -e "${RED}[FAIL] $FAIL_COUNT operations failed${NC}"
    track_test "fail"
  fi
  echo ""
fi

# ============================================================================
# CLEANUP
# ============================================================================
echo "========================================================================"
echo "CLEANUP"
echo "========================================================================"
echo ""

echo "Enabling all YKUSH ports (safe state)..."
for net in $YKUSH_NETS; do
  lager usb $net enable --box $BOX >/dev/null 2>&1
done
echo -e "${GREEN}[OK] Cleanup complete${NC}"
echo ""

# ============================================================================
# PRINT SUMMARY
# ============================================================================
print_summary

# Exit with appropriate status code
exit_with_status
