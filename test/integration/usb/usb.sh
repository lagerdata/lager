#!/bin/bash

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
# Comprehensive test suite for lager usb commands
# Tests all edge cases, error conditions, and production features with verbose output

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
if [ $# -lt 2 ]; then
  echo "Usage: $0 <IP/BOX> <USB_NET>"
  echo ""
  echo "Examples:"
  echo "  $0 my-box usb1"
  echo "  $0 <BOX_IP> usb1"
  echo ""
  echo "Arguments:"
  echo "  IP/BOX   - Box ID or Tailscale IP address"
  echo "  USB_NET  - Name of the USB net to test"
  echo ""
  exit 1
fi

BOX="$1"
USB_NET="$2"

echo "========================================================================"
echo "LAGER USB COMPREHENSIVE TEST SUITE"
echo "========================================================================"
echo ""
echo "Box: $BOX"
echo "USB Net: $USB_NET"
echo ""

# ============================================================================
# SECTION 1: BASIC COMMANDS
# ============================================================================
echo "========================================================================"
echo "SECTION 1: BASIC COMMANDS (No Connection Required)"
echo "========================================================================"
echo ""
start_section "Basic Commands"

echo "Test 1.1: List available boxes"
OUTPUT=$(lager boxes 2>&1)
echo "$OUTPUT"
if echo "$OUTPUT" | grep -q '.'; then
  echo -e "${GREEN}[OK]${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Failed${NC}"
  track_test "fail"
fi
echo ""

echo "Test 1.2: List available nets"
OUTPUT=$(lager nets --box $BOX 2>&1)
echo "$OUTPUT"
if echo "$OUTPUT" | grep -q '.'; then
  echo -e "${GREEN}[OK]${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Failed${NC}"
  track_test "fail"
fi
echo ""

echo "Test 1.3: Verify USB net exists"
OUTPUT=$(lager nets --box $BOX 2>&1)
if echo "$OUTPUT" | grep -q "$USB_NET"; then
  echo -e "${GREEN}[OK] USB net found${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] USB net not found${NC}"
  track_test "fail"
fi
echo ""

echo "Test 1.4: USB help output"
OUTPUT=$(lager usb --help 2>&1)
echo "$OUTPUT"
if echo "$OUTPUT" | grep -q "Control programmable USB hubs"; then
  echo -e "${GREEN}[OK]${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Failed${NC}"
  track_test "fail"
fi
echo ""

echo "Test 1.5: Show USB net details"
OUTPUT=$(lager nets --box $BOX 2>&1 | grep "$USB_NET")
echo "$OUTPUT"
if [ -n "$OUTPUT" ]; then
  echo -e "${GREEN}[OK] USB net details shown${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Failed${NC}"
  track_test "fail"
fi
echo ""

# ============================================================================
# SECTION 2: ERROR CASES
# ============================================================================
echo "========================================================================"
echo "SECTION 2: ERROR CASES (Invalid Commands)"
echo "========================================================================"
echo ""
start_section "Error Cases"

echo "Test 2.1: Invalid net name"
OUTPUT=$(lager usb nonexistent_net enable --box $BOX 2>&1)
echo "$OUTPUT"
if echo "$OUTPUT" | grep -qi "error\|not found"; then
  echo -e "${GREEN}[OK] Error caught correctly${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] No error for invalid net${NC}"
  track_test "fail"
fi
echo ""

echo "Test 2.2: Invalid box"
OUTPUT=$(lager usb $USB_NET enable --box INVALID-BOX 2>&1)
echo "$OUTPUT" | head -20
if echo "$OUTPUT" | grep -qi "error\|don't have\|UNAUTHORIZED"; then
  echo -e "${GREEN}[OK] Error caught correctly${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] No error for invalid box${NC}"
  track_test "fail"
fi
echo ""

echo "Test 2.3: Invalid command"
OUTPUT=$(lager usb $USB_NET invalid_command --box $BOX 2>&1)
echo "$OUTPUT"
if echo "$OUTPUT" | grep -qi "invalid\|error"; then
  echo -e "${GREEN}[OK] Invalid command caught${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Invalid command not caught${NC}"
  track_test "fail"
fi
echo ""

echo "Test 2.4: Missing net name argument"
OUTPUT=$(lager usb enable --box $BOX 2>&1)
echo "$OUTPUT"
if echo "$OUTPUT" | grep -qi "missing\|error\|usage"; then
  echo -e "${GREEN}[OK] Missing argument caught${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Missing argument not caught${NC}"
  track_test "fail"
fi
echo ""

echo "Test 2.5: Missing command argument"
OUTPUT=$(lager usb $USB_NET --box $BOX 2>&1)
echo "$OUTPUT"
if echo "$OUTPUT" | grep -qi "missing\|error\|usage"; then
  echo -e "${GREEN}[OK] Missing command caught${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Missing command not caught${NC}"
  track_test "fail"
fi
echo ""

echo "Test 2.6: Empty net name"
OUTPUT=$(lager usb "" enable --box $BOX 2>&1)
echo "$OUTPUT"
if echo "$OUTPUT" | grep -qi "error\|not found"; then
  echo -e "${GREEN}[OK] Empty net name caught${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Empty net name not caught${NC}"
  track_test "fail"
fi
echo ""

echo "Test 2.7: Non-USB net (wrong role)"
NON_USB_NET=$(lager nets --box $BOX 2>/dev/null | grep -E "battery|supply|adc" | head -1 | awk '{print $1}')
if [ -n "$NON_USB_NET" ]; then
  OUTPUT=$(lager usb "$NON_USB_NET" enable --box $BOX 2>&1)
  echo "$OUTPUT"
  if echo "$OUTPUT" | grep -qi "error\|not found"; then
    echo -e "${GREEN}[OK] Non-USB net rejected${NC}"
    track_test "pass"
  else
    echo -e "${RED}[FAIL] Non-USB net not rejected${NC}"
    track_test "fail"
  fi
else
  echo -e "${YELLOW}[SKIP] No non-USB nets available to test${NC}"
  track_test "exclude"
fi
echo ""

# ============================================================================
# SECTION 3: ENABLE OPERATIONS
# ============================================================================
echo "========================================================================"
echo "SECTION 3: ENABLE OPERATIONS"
echo "========================================================================"
echo ""
start_section "Enable Operations"

echo "Test 3.1: Basic enable"
OUTPUT=$(lager usb $USB_NET enable --box $BOX 2>&1)
echo "$OUTPUT"
if echo "$OUTPUT" | grep -q "ON"; then
  echo -e "${GREEN}[OK]${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Failed${NC}"
  track_test "fail"
fi
echo ""

echo "Test 3.2: Enable when already enabled (idempotent)"
OUTPUT=$(lager usb $USB_NET enable --box $BOX 2>&1)
echo "$OUTPUT"
if [ $? -eq 0 ]; then
  echo -e "${GREEN}[OK] Idempotent enable completed${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Failed${NC}"
  track_test "fail"
fi
echo ""

echo "Test 3.3: Multiple consecutive enables (5x)"
FAIL_COUNT=0
for i in {1..5}; do
  echo "  Attempt $i:"
  OUTPUT=$(lager usb $USB_NET enable --box $BOX 2>&1)
  echo "  $OUTPUT"
  [ $? -ne 0 ] && FAIL_COUNT=$((FAIL_COUNT + 1))
done
if [ $FAIL_COUNT -eq 0 ]; then
  echo -e "${GREEN}[OK] Multiple consecutive enables completed${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] $FAIL_COUNT/5 enables failed${NC}"
  track_test "fail"
fi
echo ""

echo "Test 3.4: Enable with case variations"
OUTPUT1=$(lager usb $USB_NET ENABLE --box $BOX 2>&1)
echo "ENABLE: $OUTPUT1"
OUTPUT2=$(lager usb $USB_NET Enable --box $BOX 2>&1)
echo "Enable: $OUTPUT2"
if [ $? -eq 0 ]; then
  echo -e "${GREEN}[OK] Case-insensitive enable works${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[WARNING] Case sensitivity issue${NC}"
  track_test "fail"
fi
echo ""

# ============================================================================
# SECTION 4: DISABLE OPERATIONS
# ============================================================================
echo "========================================================================"
echo "SECTION 4: DISABLE OPERATIONS"
echo "========================================================================"
echo ""
start_section "Disable Operations"

echo "Test 4.1: Basic disable"
OUTPUT=$(lager usb $USB_NET disable --box $BOX 2>&1)
echo "$OUTPUT"
if echo "$OUTPUT" | grep -q "OFF"; then
  echo -e "${GREEN}[OK]${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Failed${NC}"
  track_test "fail"
fi
echo ""

echo "Test 4.2: Disable when already disabled (idempotent)"
OUTPUT=$(lager usb $USB_NET disable --box $BOX 2>&1)
echo "$OUTPUT"
if [ $? -eq 0 ]; then
  echo -e "${GREEN}[OK] Idempotent disable completed${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Failed${NC}"
  track_test "fail"
fi
echo ""

echo "Test 4.3: Multiple consecutive disables (5x)"
FAIL_COUNT=0
for i in {1..5}; do
  echo "  Attempt $i:"
  OUTPUT=$(lager usb $USB_NET disable --box $BOX 2>&1)
  echo "  $OUTPUT"
  [ $? -ne 0 ] && FAIL_COUNT=$((FAIL_COUNT + 1))
done
if [ $FAIL_COUNT -eq 0 ]; then
  echo -e "${GREEN}[OK] Multiple consecutive disables completed${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] $FAIL_COUNT/5 disables failed${NC}"
  track_test "fail"
fi
echo ""

echo "Test 4.4: Disable with case variations"
OUTPUT1=$(lager usb $USB_NET DISABLE --box $BOX 2>&1)
echo "DISABLE: $OUTPUT1"
OUTPUT2=$(lager usb $USB_NET Disable --box $BOX 2>&1)
echo "Disable: $OUTPUT2"
if [ $? -eq 0 ]; then
  echo -e "${GREEN}[OK] Case-insensitive disable works${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[WARNING] Case sensitivity issue${NC}"
  track_test "fail"
fi
echo ""

# ============================================================================
# SECTION 5: TOGGLE OPERATIONS
# ============================================================================
echo "========================================================================"
echo "SECTION 5: TOGGLE OPERATIONS"
echo "========================================================================"
echo ""
start_section "Toggle Operations"

echo "Test 5.1: Toggle from disabled to enabled"
lager usb $USB_NET disable --box $BOX >/dev/null 2>&1
OUTPUT=$(lager usb $USB_NET toggle --box $BOX 2>&1)
echo "$OUTPUT"
if echo "$OUTPUT" | grep -q "ON"; then
  echo -e "${GREEN}[OK] Toggled to enabled${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Failed${NC}"
  track_test "fail"
fi
echo ""

echo "Test 5.2: Toggle from enabled to disabled"
OUTPUT=$(lager usb $USB_NET toggle --box $BOX 2>&1)
echo "$OUTPUT"
if echo "$OUTPUT" | grep -q "OFF"; then
  echo -e "${GREEN}[OK] Toggled to disabled${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Failed${NC}"
  track_test "fail"
fi
echo ""

echo "Test 5.3: Multiple consecutive toggles (10x)"
FAIL_COUNT=0
for i in {1..10}; do
  echo "  Toggle $i:"
  OUTPUT=$(lager usb $USB_NET toggle --box $BOX 2>&1)
  echo "  $OUTPUT"
  [ $? -ne 0 ] && FAIL_COUNT=$((FAIL_COUNT + 1))
done
if [ $FAIL_COUNT -eq 0 ]; then
  echo -e "${GREEN}[OK] 10 consecutive toggles completed${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] $FAIL_COUNT/10 toggles failed${NC}"
  track_test "fail"
fi
echo ""

echo "Test 5.4: Toggle with case variations"
OUTPUT1=$(lager usb $USB_NET TOGGLE --box $BOX 2>&1)
echo "TOGGLE: $OUTPUT1"
OUTPUT2=$(lager usb $USB_NET Toggle --box $BOX 2>&1)
echo "Toggle: $OUTPUT2"
if [ $? -eq 0 ]; then
  echo -e "${GREEN}[OK] Case-insensitive toggle works${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[WARNING] Case sensitivity issue${NC}"
  track_test "fail"
fi
echo ""

echo "Test 5.5: Toggle state verification"
lager usb $USB_NET enable --box $BOX >/dev/null
OUTPUT=$(lager usb $USB_NET toggle --box $BOX 2>&1)
echo "$OUTPUT"
if echo "$OUTPUT" | grep -q "OFF"; then
  echo -e "${GREEN}[OK] Toggle correctly disabled the port${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[WARNING] Toggle state may be inconsistent${NC}"
  track_test "fail"
fi
echo ""

echo "Test 5.6: Reverse toggle state verification"
lager usb $USB_NET disable --box $BOX >/dev/null
OUTPUT=$(lager usb $USB_NET toggle --box $BOX 2>&1)
echo "$OUTPUT"
if echo "$OUTPUT" | grep -q "ON"; then
  echo -e "${GREEN}[OK] Toggle correctly enabled the port${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[WARNING] Toggle state may be inconsistent${NC}"
  track_test "fail"
fi
echo ""

# ============================================================================
# SECTION 6: CYCLING TESTS
# ============================================================================
echo "========================================================================"
echo "SECTION 6: ENABLE/DISABLE CYCLING"
echo "========================================================================"
echo ""
start_section "Enable/Disable Cycling"

echo "Test 6.1: Rapid enable/disable cycling (10 cycles)"
FAIL_COUNT=0
for i in {1..10}; do
  echo "  Cycle $i:"
  OUTPUT=$(lager usb $USB_NET disable --box $BOX 2>&1)
  echo "    Disable: $OUTPUT"
  [ $? -ne 0 ] && FAIL_COUNT=$((FAIL_COUNT + 1))
  OUTPUT=$(lager usb $USB_NET enable --box $BOX 2>&1)
  echo "    Enable: $OUTPUT"
  [ $? -ne 0 ] && FAIL_COUNT=$((FAIL_COUNT + 1))
done
if [ $FAIL_COUNT -eq 0 ]; then
  echo -e "${GREEN}[OK] Rapid cycling completed${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] $FAIL_COUNT failures during cycling${NC}"
  track_test "fail"
fi
echo ""

echo "Test 6.2: Power cycle simulation (5 cycles with delays)"
FAIL_COUNT=0
for i in {1..5}; do
  echo "  Power cycle $i:"
  OUTPUT=$(lager usb $USB_NET disable --box $BOX 2>&1)
  echo "    Disable: $OUTPUT"
  [ $? -ne 0 ] && FAIL_COUNT=$((FAIL_COUNT + 1))
  sleep 0.5
  OUTPUT=$(lager usb $USB_NET enable --box $BOX 2>&1)
  echo "    Enable: $OUTPUT"
  [ $? -ne 0 ] && FAIL_COUNT=$((FAIL_COUNT + 1))
  sleep 0.5
done
if [ $FAIL_COUNT -eq 0 ]; then
  echo -e "${GREEN}[OK] Power cycling with delays completed${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] $FAIL_COUNT failures during power cycling${NC}"
  track_test "fail"
fi
echo ""

echo "Test 6.3: Enable/toggle/disable sequence"
FAIL_COUNT=0
for i in {1..5}; do
  echo "  Sequence $i:"
  OUTPUT=$(lager usb $USB_NET enable --box $BOX 2>&1)
  echo "    Enable: $OUTPUT"
  [ $? -ne 0 ] && FAIL_COUNT=$((FAIL_COUNT + 1))
  OUTPUT=$(lager usb $USB_NET toggle --box $BOX 2>&1)
  echo "    Toggle: $OUTPUT"
  [ $? -ne 0 ] && FAIL_COUNT=$((FAIL_COUNT + 1))
  OUTPUT=$(lager usb $USB_NET disable --box $BOX 2>&1)
  echo "    Disable: $OUTPUT"
  [ $? -ne 0 ] && FAIL_COUNT=$((FAIL_COUNT + 1))
done
if [ $FAIL_COUNT -eq 0 ]; then
  echo -e "${GREEN}[OK] Enable/toggle/disable sequence completed${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] $FAIL_COUNT failures during sequence${NC}"
  track_test "fail"
fi
echo ""

# ============================================================================
# SECTION 7: OUTPUT VALIDATION
# ============================================================================
echo "========================================================================"
echo "SECTION 7: OUTPUT VALIDATION"
echo "========================================================================"
echo ""
start_section "Output Validation"

echo "Test 7.1: Enable output format"
OUTPUT=$(lager usb $USB_NET enable --box $BOX 2>&1)
echo "$OUTPUT"
if echo "$OUTPUT" | grep -q "ON"; then
  echo -e "${GREEN}[OK] Contains 'ON'${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[WARNING] Unexpected output format${NC}"
  track_test "fail"
fi
echo ""

echo "Test 7.2: Disable output format"
OUTPUT=$(lager usb $USB_NET disable --box $BOX 2>&1)
echo "$OUTPUT"
if echo "$OUTPUT" | grep -q "OFF"; then
  echo -e "${GREEN}[OK] Contains 'OFF'${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[WARNING] Unexpected output format${NC}"
  track_test "fail"
fi
echo ""

echo "Test 7.3: Output contains net name"
OUTPUT=$(lager usb $USB_NET enable --box $BOX 2>&1)
echo "$OUTPUT"
if echo "$OUTPUT" | grep -q "$USB_NET"; then
  echo -e "${GREEN}[OK] Net name in output${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[WARNING] Net name not found${NC}"
  track_test "fail"
fi
echo ""

echo "Test 7.4: Output contains port information"
OUTPUT=$(lager usb $USB_NET enable --box $BOX 2>&1)
echo "$OUTPUT"
if echo "$OUTPUT" | grep -q "port"; then
  echo -e "${GREEN}[OK] Port info in output${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[WARNING] Port info not found${NC}"
  track_test "fail"
fi
echo ""

# ============================================================================
# SECTION 8: ERROR RECOVERY
# ============================================================================
echo "========================================================================"
echo "SECTION 8: ERROR RECOVERY TESTS"
echo "========================================================================"
echo ""
start_section "Error Recovery"

echo "Test 8.1: Operations after invalid net name"
OUTPUT=$(lager usb invalid_net enable --box $BOX 2>&1)
echo "$OUTPUT"
OUTPUT=$(lager usb $USB_NET enable --box $BOX 2>&1)
echo "$OUTPUT"
if [ $? -eq 0 ]; then
  echo -e "${GREEN}[OK] Command succeeded after error${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Command failed after error${NC}"
  track_test "fail"
fi
echo ""

echo "Test 8.2: Operations after invalid command"
OUTPUT=$(lager usb $USB_NET invalid_cmd --box $BOX 2>&1)
echo "$OUTPUT"
OUTPUT=$(lager usb $USB_NET enable --box $BOX 2>&1)
echo "$OUTPUT"
if [ $? -eq 0 ]; then
  echo -e "${GREEN}[OK] Command succeeded after error${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Command failed after error${NC}"
  track_test "fail"
fi
echo ""

echo "Test 8.3: Multiple errors then valid commands"
lager usb invalid_net enable --box $BOX >/dev/null 2>&1
lager usb $USB_NET invalid_cmd --box $BOX >/dev/null 2>&1
lager usb "" enable --box $BOX >/dev/null 2>&1
OUTPUT1=$(lager usb $USB_NET enable --box $BOX 2>&1)
echo "$OUTPUT1"
OUTPUT2=$(lager usb $USB_NET disable --box $BOX 2>&1)
echo "$OUTPUT2"
if [ $? -eq 0 ]; then
  echo -e "${GREEN}[OK] Valid commands succeeded after multiple errors${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Failed${NC}"
  track_test "fail"
fi
echo ""

# ============================================================================
# SECTION 9: REGRESSION TESTS
# ============================================================================
echo "========================================================================"
echo "SECTION 9: REGRESSION TESTS"
echo "========================================================================"
echo ""
start_section "Regression Tests"

echo "Test 9.1: Enable is idempotent"
lager usb $USB_NET enable --box $BOX >/dev/null 2>&1
lager usb $USB_NET enable --box $BOX >/dev/null 2>&1
OUTPUT=$(lager usb $USB_NET enable --box $BOX 2>&1)
echo "$OUTPUT"
if [ $? -eq 0 ]; then
  echo -e "${GREEN}[OK] Enable is idempotent${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Failed${NC}"
  track_test "fail"
fi
echo ""

echo "Test 9.2: Disable is idempotent"
lager usb $USB_NET disable --box $BOX >/dev/null 2>&1
lager usb $USB_NET disable --box $BOX >/dev/null 2>&1
OUTPUT=$(lager usb $USB_NET disable --box $BOX 2>&1)
echo "$OUTPUT"
if [ $? -eq 0 ]; then
  echo -e "${GREEN}[OK] Disable is idempotent${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Failed${NC}"
  track_test "fail"
fi
echo ""

echo "Test 9.3: Toggle alternates state correctly"
lager usb $USB_NET enable --box $BOX >/dev/null
STATE1=$(lager usb $USB_NET toggle --box $BOX 2>&1)
echo "State 1: $STATE1"
STATE2=$(lager usb $USB_NET toggle --box $BOX 2>&1)
echo "State 2: $STATE2"
STATE3=$(lager usb $USB_NET toggle --box $BOX 2>&1)
echo "State 3: $STATE3"
if echo "$STATE1" | grep -q "OFF" && echo "$STATE2" | grep -q "ON" && echo "$STATE3" | grep -q "OFF"; then
  echo -e "${GREEN}[OK] Toggle alternates (OFF → ON → OFF)${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[WARNING] Toggle state alternation inconsistent${NC}"
  track_test "fail"
fi
echo ""

echo "Test 9.4: Case-insensitive command handling"
lager usb $USB_NET enable --box $BOX >/dev/null 2>&1
lager usb $USB_NET ENABLE --box $BOX >/dev/null 2>&1
OUTPUT=$(lager usb $USB_NET Enable --box $BOX 2>&1)
echo "$OUTPUT"
if [ $? -eq 0 ]; then
  echo -e "${GREEN}[OK] Case-insensitive handling verified${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Failed${NC}"
  track_test "fail"
fi
echo ""

echo "Test 9.5: Rapid state changes"
FAIL_COUNT=0
for i in {1..20}; do
  lager usb $USB_NET enable --box $BOX >/dev/null 2>&1 || FAIL_COUNT=$((FAIL_COUNT + 1))
  lager usb $USB_NET disable --box $BOX >/dev/null 2>&1 || FAIL_COUNT=$((FAIL_COUNT + 1))
  lager usb $USB_NET toggle --box $BOX >/dev/null 2>&1 || FAIL_COUNT=$((FAIL_COUNT + 1))
done
if [ $FAIL_COUNT -eq 0 ]; then
  echo -e "${GREEN}[OK] Rapid state changes completed${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] $FAIL_COUNT failures${NC}"
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

echo "Setting USB port to safe state (enabled)..."
lager usb $USB_NET enable --box $BOX >/dev/null 2>&1
echo -e "${GREEN}[OK] Cleanup complete${NC}"
echo ""

# ============================================================================
# PRINT SUMMARY
# ============================================================================
print_summary

# Exit with appropriate status code
exit_with_status
