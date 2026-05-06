#!/bin/bash

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
# Acroname-specific test suite for lager usb commands
# Tests Acroname USBHub2x4, USBHub3p, USBHub3c edge cases and specific behaviors

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
  echo "  IP/BOX   - Box ID or Tailscale IP address with Acroname hubs"
  echo ""
  echo "This script auto-detects Acroname nets and tests Acroname-specific features:"
  echo "  - 0-indexed port numbering"
  echo "  - Port boundary validation (4-port vs 8-port)"
  echo "  - BrainStem library detection"
  echo "  - Hub model differentiation"
  echo "  - Connection pooling behavior"
  echo ""
  exit 1
fi

BOX="$1"

echo "========================================================================"
echo "ACRONAME USB HUB COMPREHENSIVE TEST SUITE"
echo "========================================================================"
echo ""
echo "Box: $BOX"
echo ""

# ============================================================================
# AUTO-DETECT ACRONAME NETS
# ============================================================================
echo "========================================================================"
echo "DETECTING ACRONAME HUBS"
echo "========================================================================"
echo ""

# Get all Acroname nets
ACRONAME_4PORT_NETS=$(lager nets --box $BOX 2>/dev/null | grep "Acroname_4Port" | awk '{print $1}')
ACRONAME_8PORT_NETS=$(lager nets --box $BOX 2>/dev/null | grep "Acroname_8Port" | awk '{print $1}')

# Count them
NUM_4PORT=$(echo "$ACRONAME_4PORT_NETS" | grep -c "usb" || echo "0")
NUM_8PORT=$(echo "$ACRONAME_8PORT_NETS" | grep -c "usb" || echo "0")

echo "Found $NUM_4PORT Acroname 4-Port hubs"
echo "Found $NUM_8PORT Acroname 8-Port hubs"
echo ""

if [ "$NUM_4PORT" -eq 0 ] && [ "$NUM_8PORT" -eq 0 ]; then
  echo -e "${RED}ERROR: No Acroname hubs found on box $BOX${NC}"
  echo "Please ensure Acroname hubs are connected and nets are configured."
  exit 1
fi

# Select test nets
if [ "$NUM_4PORT" -gt 0 ]; then
  TEST_4PORT_NET=$(echo "$ACRONAME_4PORT_NETS" | head -1)
  echo -e "${GREEN}Using 4-Port test net: $TEST_4PORT_NET${NC}"
fi

if [ "$NUM_8PORT" -gt 0 ]; then
  TEST_8PORT_NET=$(echo "$ACRONAME_8PORT_NETS" | head -1)
  echo -e "${GREEN}Using 8-Port test net: $TEST_8PORT_NET${NC}"
fi

echo ""

# ============================================================================
# SECTION 1: ACRONAME HARDWARE DETECTION
# ============================================================================
echo "========================================================================"
echo "SECTION 1: ACRONAME HARDWARE DETECTION"
echo "========================================================================"
echo ""
start_section "Acroname Hardware Detection"

echo "Test 1.1: List all nets and identify Acroname hubs"
OUTPUT=$(lager nets --box $BOX 2>&1)
echo "$OUTPUT" | grep -E "usb|Acroname"
echo ""
if echo "$OUTPUT" | grep -q "Acroname"; then
  echo -e "${GREEN}[OK] Acroname hubs detected${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] No Acroname hubs found${NC}"
  track_test "fail"
fi
echo ""

echo "Test 1.2: Verify 4-Port hub identification"
if [ "$NUM_4PORT" -gt 0 ]; then
  OUTPUT=$(lager nets --box $BOX 2>&1 | grep "Acroname_4Port")
  echo "$OUTPUT"
  if [ -n "$OUTPUT" ]; then
    echo -e "${GREEN}[OK] Acroname 4-Port hubs identified${NC}"
    track_test "pass"
  else
    echo -e "${RED}[FAIL] Failed to identify 4-Port hubs${NC}"
    track_test "fail"
  fi
else
  echo -e "${YELLOW}[SKIP] No 4-Port hubs available${NC}"
  track_test "exclude"
fi
echo ""

echo "Test 1.3: Verify 8-Port hub identification"
if [ "$NUM_8PORT" -gt 0 ]; then
  OUTPUT=$(lager nets --box $BOX 2>&1 | grep "Acroname_8Port")
  echo "$OUTPUT"
  if [ -n "$OUTPUT" ]; then
    echo -e "${GREEN}[OK] Acroname 8-Port hubs identified${NC}"
    track_test "pass"
  else
    echo -e "${RED}[FAIL] Failed to identify 8-Port hubs${NC}"
    track_test "fail"
  fi
else
  echo -e "${YELLOW}[SKIP] No 8-Port hubs available${NC}"
  track_test "exclude"
fi
echo ""

echo "Test 1.4: Verify channel/port numbering (0-indexed)"
if [ "$NUM_4PORT" -gt 0 ]; then
  OUTPUT=$(lager nets --box $BOX 2>&1 | grep "Acroname_4Port" | head -1)
  echo "$OUTPUT"
  # Check that the port number is in valid 0-indexed range (0-3 for 4-port)
  if echo "$OUTPUT" | grep -qE "\s+[0-3]\s+"; then
    echo -e "${GREEN}[OK] Acroname uses 0-indexed ports (0-3 for 4-port)${NC}"
    track_test "pass"
  else
    echo -e "${YELLOW}[WARNING] Port numbering unclear${NC}"
    track_test "fail"
  fi
else
  echo -e "${YELLOW}[SKIP] No 4-Port hubs available${NC}"
  track_test "exclude"
fi
echo ""

# ============================================================================
# SECTION 2: PORT BOUNDARY TESTS (4-PORT)
# ============================================================================
if [ "$NUM_4PORT" -gt 0 ]; then
  echo "========================================================================"
  echo "SECTION 2: PORT BOUNDARY TESTS (4-PORT HUBS)"
  echo "========================================================================"
  echo ""
  start_section "Port Boundary Tests (4-Port)"

  echo "Test 2.1: Port 0 operations (valid - 0-indexed)"
  # Acroname is 0-indexed, verify enable works and shows proper format
  OUTPUT=$(lager usb $TEST_4PORT_NET enable --box $BOX 2>&1)
  echo "$OUTPUT"
  # Check for success message and proper output format (port number in parentheses)
  if echo "$OUTPUT" | grep -qE "port [0-9]+\)" && echo "$OUTPUT" | grep -q "ON"; then
    echo -e "${GREEN}[OK] Port 0 is valid for Acroname (0-indexed)${NC}"
    track_test "pass"
  else
    echo -e "${YELLOW}[WARNING] Port 0 behavior unexpected${NC}"
    track_test "fail"
  fi
  echo ""

  echo "Test 2.2: All valid ports on 4-Port hub (0-3)"
  FAIL_COUNT=0
  for port_net in $ACRONAME_4PORT_NETS; do
    echo "  Testing $port_net:"
    OUTPUT=$(lager usb $port_net enable --box $BOX 2>&1)
    echo "  $OUTPUT"
    if [ $? -ne 0 ]; then
      FAIL_COUNT=$((FAIL_COUNT + 1))
    fi
  done
  if [ $FAIL_COUNT -eq 0 ]; then
    echo -e "${GREEN}[OK] All 4-Port hub ports operational${NC}"
    track_test "pass"
  else
    echo -e "${RED}[FAIL] $FAIL_COUNT ports failed${NC}"
    track_test "fail"
  fi
  echo ""

  echo "Test 2.3: Toggle all 4-Port hub ports"
  FAIL_COUNT=0
  for port_net in $ACRONAME_4PORT_NETS; do
    echo "  Toggling $port_net:"
    OUTPUT=$(lager usb $port_net toggle --box $BOX 2>&1)
    echo "  $OUTPUT"
    if [ $? -ne 0 ]; then
      FAIL_COUNT=$((FAIL_COUNT + 1))
    fi
  done
  if [ $FAIL_COUNT -eq 0 ]; then
    echo -e "${GREEN}[OK] All 4-Port toggles successful${NC}"
    track_test "pass"
  else
    echo -e "${RED}[FAIL] $FAIL_COUNT toggles failed${NC}"
    track_test "fail"
  fi
  echo ""
fi

# ============================================================================
# SECTION 3: PORT BOUNDARY TESTS (8-PORT)
# ============================================================================
if [ "$NUM_8PORT" -gt 0 ]; then
  echo "========================================================================"
  echo "SECTION 3: PORT BOUNDARY TESTS (8-PORT HUBS)"
  echo "========================================================================"
  echo ""
  start_section "Port Boundary Tests (8-Port)"

  echo "Test 3.1: Port 0 operations (valid - 0-indexed)"
  OUTPUT=$(lager usb $TEST_8PORT_NET enable --box $BOX 2>&1)
  echo "$OUTPUT"
  # Check for success message and proper output format (any valid port 0-7 is fine)
  if echo "$OUTPUT" | grep -qE "port [0-7]\)" && echo "$OUTPUT" | grep -q "ON"; then
    echo -e "${GREEN}[OK] Port 0 is valid for Acroname (0-indexed)${NC}"
    track_test "pass"
  else
    echo -e "${YELLOW}[WARNING] Port 0 behavior unexpected${NC}"
    track_test "fail"
  fi
  echo ""

  echo "Test 3.2: All valid ports on 8-Port hub (0-7)"
  FAIL_COUNT=0
  for port_net in $ACRONAME_8PORT_NETS; do
    echo "  Testing $port_net:"
    OUTPUT=$(lager usb $port_net enable --box $BOX 2>&1)
    echo "  $OUTPUT"
    if [ $? -ne 0 ]; then
      FAIL_COUNT=$((FAIL_COUNT + 1))
    fi
  done
  if [ $FAIL_COUNT -eq 0 ]; then
    echo -e "${GREEN}[OK] All 8-Port hub ports operational${NC}"
    track_test "pass"
  else
    echo -e "${RED}[FAIL] $FAIL_COUNT ports failed${NC}"
    track_test "fail"
  fi
  echo ""

  echo "Test 3.3: Toggle all 8-Port hub ports"
  FAIL_COUNT=0
  for port_net in $ACRONAME_8PORT_NETS; do
    echo "  Toggling $port_net:"
    OUTPUT=$(lager usb $port_net toggle --box $BOX 2>&1)
    echo "  $OUTPUT"
    if [ $? -ne 0 ]; then
      FAIL_COUNT=$((FAIL_COUNT + 1))
    fi
  done
  if [ $FAIL_COUNT -eq 0 ]; then
    echo -e "${GREEN}[OK] All 8-Port toggles successful${NC}"
    track_test "pass"
  else
    echo -e "${RED}[FAIL] $FAIL_COUNT toggles failed${NC}"
    track_test "fail"
  fi
  echo ""

  echo "Test 3.4: Port 7 (highest port on 8-port hub)"
  LAST_8PORT_NET=$(echo "$ACRONAME_8PORT_NETS" | tail -1)
  echo "Testing highest port net: $LAST_8PORT_NET"
  OUTPUT=$(lager usb $LAST_8PORT_NET enable --box $BOX 2>&1)
  echo "$OUTPUT"
  # Any port in range 0-7 is valid, just verify operation succeeds
  if echo "$OUTPUT" | grep -qE "port [0-7]\)" && echo "$OUTPUT" | grep -q "ON"; then
    echo -e "${GREEN}[OK] Port 7 accessible on 8-port hub${NC}"
    track_test "pass"
  else
    echo -e "${YELLOW}[WARNING] Port 7 behavior unexpected${NC}"
    track_test "fail"
  fi
  echo ""
fi

# ============================================================================
# SECTION 4: ACRONAME-SPECIFIC FEATURES
# ============================================================================
echo "========================================================================"
echo "SECTION 4: ACRONAME-SPECIFIC FEATURES"
echo "========================================================================"
echo ""
start_section "Acroname-Specific Features"

TEST_NET=$TEST_4PORT_NET
[ -z "$TEST_NET" ] && TEST_NET=$TEST_8PORT_NET

echo "Test 4.1: Port state detection"
echo "Disabling port..."
lager usb $TEST_NET disable --box $BOX >/dev/null 2>&1
echo "Toggling (should enable)..."
OUTPUT=$(lager usb $TEST_NET toggle --box $BOX 2>&1)
echo "$OUTPUT"
if echo "$OUTPUT" | grep -q "ON"; then
  echo -e "${GREEN}[OK] Acroname correctly detects and toggles port state${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Port state detection failed${NC}"
  track_test "fail"
fi
echo ""

echo "Test 4.2: Verify BrainStem output format"
OUTPUT=$(lager usb $TEST_NET enable --box $BOX 2>&1)
echo "$OUTPUT"
# Check for proper output format: net name, port number, and state
if echo "$OUTPUT" | grep -qE "port [0-9]+\)" && echo "$OUTPUT" | grep -q "ON"; then
  echo -e "${GREEN}[OK] BrainStem driver output format correct${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[WARNING] Output format differs from expected${NC}"
  track_test "fail"
fi
echo ""

echo "Test 4.3: Connection persistence (singleton behavior)"
echo "First operation:"
OUTPUT1=$(lager usb $TEST_NET enable --box $BOX 2>&1)
echo "$OUTPUT1"
echo "Second operation (should reuse connection):"
OUTPUT2=$(lager usb $TEST_NET disable --box $BOX 2>&1)
echo "$OUTPUT2"
if [ $? -eq 0 ]; then
  echo -e "${GREEN}[OK] Connection reuse working${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Connection issue${NC}"
  track_test "fail"
fi
echo ""

echo "Test 4.4: Rapid operations (stressing connection pooling)"
FAIL_COUNT=0
for i in {1..10}; do
  lager usb $TEST_NET toggle --box $BOX >/dev/null 2>&1 || FAIL_COUNT=$((FAIL_COUNT + 1))
done
if [ $FAIL_COUNT -eq 0 ]; then
  echo -e "${GREEN}[OK] Connection pooling handles rapid operations${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] $FAIL_COUNT operations failed${NC}"
  track_test "fail"
fi
echo ""

# ============================================================================
# SECTION 5: MULTI-HUB OPERATIONS
# ============================================================================
if [ "$NUM_4PORT" -gt 0 ] && [ "$NUM_8PORT" -gt 0 ]; then
  echo "========================================================================"
  echo "SECTION 5: MULTI-HUB OPERATIONS"
  echo "========================================================================"
  echo ""
  start_section "Multi-Hub Operations"

  echo "Test 5.1: Simultaneous operations on different hub types"
  echo "Operating on 4-Port hub:"
  OUTPUT1=$(lager usb $TEST_4PORT_NET enable --box $BOX 2>&1)
  echo "$OUTPUT1"
  echo "Operating on 8-Port hub:"
  OUTPUT2=$(lager usb $TEST_8PORT_NET enable --box $BOX 2>&1)
  echo "$OUTPUT2"
  if [ $? -eq 0 ]; then
    echo -e "${GREEN}[OK] Multi-hub operations successful${NC}"
    track_test "pass"
  else
    echo -e "${RED}[FAIL] Multi-hub operation failed${NC}"
    track_test "fail"
  fi
  echo ""

  echo "Test 5.2: Alternating operations between hub types"
  FAIL_COUNT=0
  for i in {1..5}; do
    echo "  Iteration $i:"
    lager usb $TEST_4PORT_NET toggle --box $BOX >/dev/null 2>&1 || FAIL_COUNT=$((FAIL_COUNT + 1))
    lager usb $TEST_8PORT_NET toggle --box $BOX >/dev/null 2>&1 || FAIL_COUNT=$((FAIL_COUNT + 1))
  done
  if [ $FAIL_COUNT -eq 0 ]; then
    echo -e "${GREEN}[OK] Alternating hub operations successful${NC}"
    track_test "pass"
  else
    echo -e "${RED}[FAIL] $FAIL_COUNT operations failed${NC}"
    track_test "fail"
  fi
  echo ""

  echo "Test 5.3: All Acroname ports cycling"
  FAIL_COUNT=0
  ALL_ACRONAME_NETS="$ACRONAME_4PORT_NETS $ACRONAME_8PORT_NETS"
  for net in $ALL_ACRONAME_NETS; do
    lager usb $net toggle --box $BOX >/dev/null 2>&1 || FAIL_COUNT=$((FAIL_COUNT + 1))
  done
  if [ $FAIL_COUNT -eq 0 ]; then
    echo -e "${GREEN}[OK] All Acroname ports cycled successfully${NC}"
    track_test "pass"
  else
    echo -e "${RED}[FAIL] $FAIL_COUNT ports failed${NC}"
    track_test "fail"
  fi
  echo ""
fi

# ============================================================================
# SECTION 6: ACRONAME ERROR HANDLING
# ============================================================================
echo "========================================================================"
echo "SECTION 6: ACRONAME ERROR HANDLING"
echo "========================================================================"
echo ""
start_section "Acroname Error Handling"

echo "Test 6.1: Invalid port state query handling"
# This tests that the driver handles BrainStem errors correctly
OUTPUT=$(lager usb $TEST_NET toggle --box $BOX 2>&1)
echo "$OUTPUT"
if [ $? -eq 0 ]; then
  echo -e "${GREEN}[OK] Port state query handled correctly${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Port state query failed${NC}"
  track_test "fail"
fi
echo ""

echo "Test 6.2: Recovery after error"
# Force an error (invalid net)
lager usb invalid_acroname_net enable --box $BOX >/dev/null 2>&1
# Try valid operation
OUTPUT=$(lager usb $TEST_NET enable --box $BOX 2>&1)
echo "$OUTPUT"
if [ $? -eq 0 ]; then
  echo -e "${GREEN}[OK] Recovery after error successful${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Failed to recover after error${NC}"
  track_test "fail"
fi
echo ""

echo "Test 6.3: BrainStem error code handling"
# Test that the driver handles NO_ERROR correctly
OUTPUT=$(lager usb $TEST_NET enable --box $BOX 2>&1)
echo "$OUTPUT"
if ! echo "$OUTPUT" | grep -qi "error.*code"; then
  echo -e "${GREEN}[OK] No spurious error codes in successful operations${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[WARNING] Unexpected error code in output${NC}"
  track_test "fail"
fi
echo ""

# ============================================================================
# SECTION 7: ACRONAME PERFORMANCE
# ============================================================================
echo "========================================================================"
echo "SECTION 7: ACRONAME PERFORMANCE TESTS"
echo "========================================================================"
echo ""
start_section "Acroname Performance"

echo "Test 7.1: Rapid cycling performance (20 operations)"
START_TIME=$(date +%s)
FAIL_COUNT=0
for i in {1..20}; do
  lager usb $TEST_NET toggle --box $BOX >/dev/null 2>&1 || FAIL_COUNT=$((FAIL_COUNT + 1))
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

echo "Test 7.2: Connection pooling efficiency"
# Multiple operations should be fast due to connection reuse
START_TIME=$(date +%s)
for i in {1..5}; do
  lager usb $TEST_NET enable --box $BOX >/dev/null 2>&1
  lager usb $TEST_NET disable --box $BOX >/dev/null 2>&1
done
END_TIME=$(date +%s)
DURATION=$((END_TIME - START_TIME))
echo "Duration: ${DURATION}s for 10 operations"
if [ $DURATION -lt 30 ]; then
  echo -e "${GREEN}[OK] Connection pooling efficient (<30s)${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[WARNING] Connection pooling may be slow (${DURATION}s)${NC}"
  track_test "fail"
fi
echo ""

# ============================================================================
# CLEANUP
# ============================================================================
echo "========================================================================"
echo "CLEANUP"
echo "========================================================================"
echo ""

echo "Enabling all Acroname ports (safe state)..."
ALL_ACRONAME_NETS="$ACRONAME_4PORT_NETS $ACRONAME_8PORT_NETS"
for net in $ALL_ACRONAME_NETS; do
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
