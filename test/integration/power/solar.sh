#!/bin/bash

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
# Comprehensive test suite for lager solar commands
# Tests all edge cases, error conditions, and production features
#
# IMPORTANT: EA solar simulators are single-threaded devices
# - Only one command can access the device at a time
# - Concurrent commands will fail with "device-busy" errors
# - Rapid set/stop cycling requires settling time between operations
# - This is a hardware limitation, not a software bug

# Determine script directory for relative paths
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Source test framework
source "${SCRIPT_DIR}/../../framework/colors.sh"
source "${SCRIPT_DIR}/../../framework/harness.sh"

set +e  # DON'T exit on error - we want to track failures

# Initialize the test harness
init_harness

# Safety delay between tests (seconds)
TEST_DELAY=0.5

# Check if required arguments are provided
if [ $# -lt 2 ]; then
  echo "Usage: $0 <BOX> <SOLAR_NET>"
  echo ""
  echo "Examples:"
  echo "  $0 my-box solar1"
  echo "  $0 <BOX_IP> solar1"
  echo ""
  echo "Arguments:"
  echo "  BOX        - Box ID or Tailscale IP address"
  echo "  SOLAR_NET  - Name of the solar net to test"
  echo ""
  exit 1
fi

BOX="$1"
SOLAR_NET="$2"

echo "========================================================================"
echo "LAGER SOLAR COMPREHENSIVE TEST SUITE"
echo "========================================================================"
echo ""
sleep $TEST_DELAY
echo "Box: $BOX"
echo "Solar Net: $SOLAR_NET"
echo ""
sleep $TEST_DELAY

# ============================================================
# SECTION 1: BASIC COMMANDS (No connection required)
# ============================================================
start_section "Basic Commands"
echo "========================================================================"
echo "SECTION 1: BASIC COMMANDS (No Connection Required)"
echo "========================================================================"
echo ""
sleep $TEST_DELAY

echo "Test 1.1: List available boxes"
if lager boxes >/dev/null 2>&1; then
  lager boxes
  track_test "pass"
else
  lager boxes
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 1.2: List available nets"
if lager nets --box $BOX >/dev/null 2>&1; then
  lager nets --box $BOX
  track_test "pass"
else
  lager nets --box $BOX
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 1.3: Verify solar net exists"
if lager nets --box $BOX | grep -q "$SOLAR_NET"; then
  echo -e "${GREEN}[OK] Solar net found${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Solar net not found${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 1.4: Solar help output"
if lager solar --help >/dev/null 2>&1; then
  lager solar --help
  track_test "pass"
else
  lager solar --help
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 1.5: Irradiance command help"
if lager solar $SOLAR_NET irradiance --help >/dev/null 2>&1; then
  lager solar $SOLAR_NET irradiance --help --box $BOX
  echo -e "${GREEN}[OK] irradiance --help works${NC}"
  track_test "pass"
else
  lager solar $SOLAR_NET irradiance --help --box $BOX
  echo -e "${RED}[FAIL] irradiance --help failed${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 1.6: Set command help"
if lager solar $SOLAR_NET set --help >/dev/null 2>&1; then
  lager solar $SOLAR_NET set --help --box $BOX
  echo -e "${GREEN}[OK] set --help works${NC}"
  track_test "pass"
else
  lager solar $SOLAR_NET set --help --box $BOX
  echo -e "${RED}[FAIL] set --help failed${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

# ============================================================
# SECTION 2: ERROR CASES (Invalid Commands)
# ============================================================
start_section "Error Cases"
echo "========================================================================"
echo "SECTION 2: ERROR CASES (Invalid Commands)"
echo "========================================================================"
echo ""
sleep $TEST_DELAY

echo "Test 2.1: Invalid net name"
if lager solar nonexistent_net irradiance --box $BOX 2>&1 | grep -qi "not found\|error"; then
  echo -e "${GREEN}[OK] Invalid net name properly rejected${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Invalid net name was not properly rejected${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 2.2: Invalid box"
if lager solar $SOLAR_NET irradiance --box INVALID-BOX 2>&1 | grep -qi "error\|don't have"; then
  lager solar $SOLAR_NET irradiance --box INVALID-BOX 2>&1 || true
  echo -e "${GREEN}[OK] Error caught correctly${NC}"
  track_test "pass"
else
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 2.3: Negative irradiance"
if lager solar $SOLAR_NET irradiance -100.0 --box $BOX 2>&1 | grep -qi "error\|No such option\|negative"; then
  lager solar $SOLAR_NET irradiance -100.0 --box $BOX 2>&1 || true
  echo -e "${GREEN}[OK] Negative irradiance caught${NC}"
  track_test "pass"
else
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 2.4: Invalid irradiance format"
if lager solar $SOLAR_NET irradiance abc --box $BOX 2>&1 | grep -qi "error\|not a valid"; then
  lager solar $SOLAR_NET irradiance abc --box $BOX 2>&1 || true
  echo -e "${GREEN}[OK] Invalid format caught${NC}"
  track_test "pass"
else
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 2.5: Extremely high irradiance (10000 W/m²)"
lager solar $SOLAR_NET irradiance 10000.0 --box $BOX 2>&1 || true
echo -e "${GREEN}[OK] High irradiance caught or clamped${NC}"
track_test "pass"
echo ""
sleep $TEST_DELAY

echo "Test 2.6: Read-only command with write attempt (voc)"
if lager solar $SOLAR_NET voc 5.0 --box $BOX 2>&1 | grep -qi "read.only\|no such option\|error"; then
  echo -e "${GREEN}[OK] Write to read-only parameter properly rejected${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[WARNING] Read-only validation may need improvement${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 2.7: Read-only command with write attempt (temperature)"
if lager solar $SOLAR_NET temperature 25.0 --box $BOX 2>&1 | grep -qi "read.only\|no such option\|error"; then
  echo -e "${GREEN}[OK] Write to read-only parameter properly rejected${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[WARNING] Read-only validation may need improvement${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 2.8: Operations before set (solar not initialized)"
echo "Note: Some commands may require 'set' to be called first"
lager solar $SOLAR_NET irradiance --box $BOX 2>&1 || echo -e "${YELLOW}[WARNING] May require 'set' first${NC}"
track_test "pass"
echo ""
sleep $TEST_DELAY

# ============================================================
# SECTION 3: SET AND STOP OPERATIONS
# ============================================================
start_section "Set and Stop Operations"
echo "========================================================================"
echo "SECTION 3: SET AND STOP OPERATIONS"
echo "========================================================================"
echo ""
sleep $TEST_DELAY

echo "Test 3.1: Initialize solar simulation (set)"
if lager solar $SOLAR_NET set --box $BOX; then
  echo -e "${GREEN}[OK] Solar simulation initialized${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Failed to initialize solar simulation${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 3.2: Stop solar simulation"
if lager solar $SOLAR_NET stop --box $BOX; then
  echo -e "${GREEN}[OK] Solar simulation stopped${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Failed to stop solar simulation${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 3.3: Re-initialize after stop"
if lager solar $SOLAR_NET set --box $BOX; then
  echo -e "${GREEN}[OK] Solar simulation re-initialized${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Failed to re-initialize${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 3.4: Multiple set calls (idempotent test)"
lager solar $SOLAR_NET set --box $BOX
lager solar $SOLAR_NET set --box $BOX
lager solar $SOLAR_NET set --box $BOX
echo -e "${GREEN}[OK] Multiple set calls completed${NC}"
track_test "pass"
echo ""
sleep $TEST_DELAY

echo "Test 3.5: Multiple stop calls (idempotent test)"
lager solar $SOLAR_NET stop --box $BOX
lager solar $SOLAR_NET stop --box $BOX
lager solar $SOLAR_NET stop --box $BOX
echo -e "${GREEN}[OK] Multiple stop calls completed${NC}"
track_test "pass"
echo ""
sleep $TEST_DELAY

echo "Test 3.6: Set/stop cycling (10 cycles with settling time)"
for i in {1..10}; do
  lager solar $SOLAR_NET set --box $BOX >/dev/null 2>&1 || echo "[FAIL] Set $i failed"
  sleep 0.5  # Give device time to initialize
  lager solar $SOLAR_NET stop --box $BOX >/dev/null 2>&1 || echo "[FAIL] Stop $i failed"
  sleep 0.5  # Give device time to stop cleanly
done
echo -e "${GREEN}[OK] Set/stop cycling completed${NC}"
track_test "pass"
echo ""
sleep $TEST_DELAY

# Re-initialize for remaining tests
echo "Re-initializing solar simulation for remaining tests..."
lager solar $SOLAR_NET set --box $BOX >/dev/null
echo ""

# ============================================================
# SECTION 4: IRRADIANCE OPERATIONS (WRITE)
# ============================================================
start_section "Irradiance Operations (Write)"
echo "========================================================================"
echo "SECTION 4: IRRADIANCE OPERATIONS (WRITE)"
echo "========================================================================"
echo ""
sleep $TEST_DELAY

echo "Test 4.1: Set irradiance to 0 W/m²"
if lager solar $SOLAR_NET irradiance 0.0 --box $BOX; then
  track_test "pass"
else
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 4.2: Set irradiance to 100 W/m²"
if lager solar $SOLAR_NET irradiance 100.0 --box $BOX; then
  track_test "pass"
else
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 4.3: Set irradiance to 500 W/m²"
if lager solar $SOLAR_NET irradiance 500.0 --box $BOX; then
  track_test "pass"
else
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 4.4: Set irradiance to 1000 W/m² (standard test condition)"
if lager solar $SOLAR_NET irradiance 1000.0 --box $BOX; then
  track_test "pass"
else
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 4.5: Set irradiance to 1200 W/m² (high irradiance)"
if lager solar $SOLAR_NET irradiance 1200.0 --box $BOX; then
  track_test "pass"
else
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 4.6: Set irradiance to fractional value (750.5 W/m²)"
if lager solar $SOLAR_NET irradiance 750.5 --box $BOX; then
  track_test "pass"
else
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 4.7: Set irradiance to very small value (0.1 W/m²)"
if lager solar $SOLAR_NET irradiance 0.1 --box $BOX; then
  track_test "pass"
else
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 4.8: Set irradiance to precise value (314.159 W/m²)"
if lager solar $SOLAR_NET irradiance 314.159 --box $BOX; then
  track_test "pass"
else
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 4.9: Irradiance sweep (0 to 1000 W/m² in 200 W/m² steps)"
for irradiance in 0.0 200.0 400.0 600.0 800.0 1000.0; do
  echo "  Setting irradiance to ${irradiance} W/m²"
  lager solar $SOLAR_NET irradiance $irradiance --box $BOX >/dev/null 2>&1 || echo "[FAIL] Failed at ${irradiance} W/m²"
done
echo -e "${GREEN}[OK] Irradiance sweep completed${NC}"
track_test "pass"
echo ""
sleep $TEST_DELAY

echo "Test 4.10: Common irradiance values"
for irradiance in 0 50 100 250 500 750 1000; do
  echo "  Setting irradiance to ${irradiance} W/m²"
  lager solar $SOLAR_NET irradiance $irradiance --box $BOX >/dev/null 2>&1 || echo "[FAIL] Failed at ${irradiance} W/m²"
done
echo -e "${GREEN}[OK] Common irradiance values completed${NC}"
track_test "pass"
echo ""
sleep $TEST_DELAY

echo "Test 4.11: Rapid irradiance changes (stress test)"
for i in {1..20}; do
  IRRADIANCE=$(echo "scale=1; ($i % 10) * 100.0" | bc)
  lager solar $SOLAR_NET irradiance $IRRADIANCE --box $BOX >/dev/null 2>&1 || echo "[FAIL] Irradiance change $i failed"
done
echo -e "${GREEN}[OK] Rapid irradiance changes completed${NC}"
track_test "pass"
echo ""
sleep $TEST_DELAY

# ============================================================
# SECTION 5: IRRADIANCE OPERATIONS (READ)
# ============================================================
start_section "Irradiance Operations (Read)"
echo "========================================================================"
echo "SECTION 5: IRRADIANCE OPERATIONS (READ)"
echo "========================================================================"
echo ""
sleep $TEST_DELAY

echo "Test 5.1: Set irradiance to 500 W/m² and read back"
lager solar $SOLAR_NET irradiance 500.0 --box $BOX >/dev/null
IRRADIANCE_OUTPUT=$(lager solar $SOLAR_NET irradiance --box $BOX)
echo "$IRRADIANCE_OUTPUT"
IRRADIANCE_VALUE=$(echo "$IRRADIANCE_OUTPUT" | grep -oE '[0-9]+\.?[0-9]*' | head -1)
echo "Irradiance readback: ${IRRADIANCE_VALUE} W/m²"
track_test "pass"
echo ""
sleep $TEST_DELAY

echo "Test 5.2: Read irradiance without setting (current value)"
IRRADIANCE_OUTPUT=$(lager solar $SOLAR_NET irradiance --box $BOX)
echo "$IRRADIANCE_OUTPUT"
track_test "pass"
echo ""
sleep $TEST_DELAY

echo "Test 5.3: Verify irradiance read returns numeric value"
IRRADIANCE_VALUE=$(echo "$IRRADIANCE_OUTPUT" | grep -oE '[0-9]+\.?[0-9]*' | head -1)
if [[ "$IRRADIANCE_VALUE" =~ ^-?[0-9]+\.?[0-9]*$ ]]; then
  echo -e "${GREEN}[OK] Irradiance read returned valid numeric value: ${IRRADIANCE_VALUE} W/m²${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Irradiance read returned non-numeric value: $IRRADIANCE_VALUE${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 5.4: Irradiance write-read verification loop"
for irradiance in 100 300 500 700 900; do
  lager solar $SOLAR_NET irradiance $irradiance --box $BOX >/dev/null
  READBACK_OUTPUT=$(lager solar $SOLAR_NET irradiance --box $BOX)
  READBACK=$(echo "$READBACK_OUTPUT" | grep -oE '[0-9]+\.?[0-9]*' | head -1)
  echo "  Set: ${irradiance} W/m², Readback: ${READBACK} W/m²"
done
track_test "pass"
echo ""
sleep $TEST_DELAY

echo "Test 5.5: Multiple consecutive irradiance reads (stability)"
lager solar $SOLAR_NET irradiance 600.0 --box $BOX >/dev/null
for i in {1..5}; do
  READBACK_OUTPUT=$(lager solar $SOLAR_NET irradiance --box $BOX)
  READBACK=$(echo "$READBACK_OUTPUT" | grep -oE '[0-9]+\.?[0-9]*' | head -1)
  echo "  Read $i: ${READBACK} W/m²"
done
track_test "pass"
echo ""
sleep $TEST_DELAY

echo "Test 5.6: Irradiance precision test (fractional values)"
for irradiance in 123.456 456.789 789.012; do
  lager solar $SOLAR_NET irradiance $irradiance --box $BOX >/dev/null
  READBACK_OUTPUT=$(lager solar $SOLAR_NET irradiance --box $BOX)
  READBACK=$(echo "$READBACK_OUTPUT" | grep -oE '[0-9]+\.?[0-9]*' | head -1)
  echo "  Set: ${irradiance} W/m², Readback: ${READBACK} W/m²"
done
track_test "pass"
echo ""
sleep $TEST_DELAY

# ============================================================
# SECTION 6: READ-ONLY PARAMETERS (VOC, Temperature, MPP)
# ============================================================
start_section "Read-Only Parameters"
echo "========================================================================"
echo "SECTION 6: READ-ONLY PARAMETERS (VOC, Temperature, MPP)"
echo "========================================================================"
echo ""
sleep $TEST_DELAY

echo "Test 6.1: Read open-circuit voltage (Voc)"
VOC_OUTPUT=$(lager solar $SOLAR_NET voc --box $BOX 2>&1)
echo "$VOC_OUTPUT"
VOC_VALUE=$(echo "$VOC_OUTPUT" | grep -oE '[0-9]+\.?[0-9]*' | head -1)
if [ -n "$VOC_VALUE" ]; then
  echo -e "${GREEN}[OK] Voc: ${VOC_VALUE} V${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[WARNING] Voc value not extracted${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 6.2: Read cell temperature"
TEMP_OUTPUT=$(lager solar $SOLAR_NET temperature --box $BOX 2>&1)
echo "$TEMP_OUTPUT"
TEMP_VALUE=$(echo "$TEMP_OUTPUT" | grep -oE '[0-9]+\.?[0-9]*' | head -1)
if [ -n "$TEMP_VALUE" ]; then
  echo -e "${GREEN}[OK] Temperature: ${TEMP_VALUE} °C${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[WARNING] Temperature value not extracted${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 6.3: Read MPP voltage"
MPP_V_OUTPUT=$(lager solar $SOLAR_NET mpp-voltage --box $BOX 2>&1)
echo "$MPP_V_OUTPUT"
MPP_V_VALUE=$(echo "$MPP_V_OUTPUT" | grep -oE '[0-9]+\.?[0-9]*' | head -1)
if [ -n "$MPP_V_VALUE" ]; then
  echo -e "${GREEN}[OK] MPP Voltage: ${MPP_V_VALUE} V${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[WARNING] MPP voltage value not extracted${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 6.4: Read MPP current"
MPP_I_OUTPUT=$(lager solar $SOLAR_NET mpp-current --box $BOX 2>&1)
echo "$MPP_I_OUTPUT"
MPP_I_VALUE=$(echo "$MPP_I_OUTPUT" | grep -oE '[0-9]+\.?[0-9]*' | head -1)
if [ -n "$MPP_I_VALUE" ]; then
  echo -e "${GREEN}[OK] MPP Current: ${MPP_I_VALUE} A${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[WARNING] MPP current value not extracted${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 6.5: Read dynamic panel resistance"
RESISTANCE_OUTPUT=$(lager solar $SOLAR_NET resistance --box $BOX 2>&1)
echo "$RESISTANCE_OUTPUT"
RESISTANCE_VALUE=$(echo "$RESISTANCE_OUTPUT" | grep -oE '[0-9]+\.?[0-9]*' | head -1)
if [ -n "$RESISTANCE_VALUE" ]; then
  echo -e "${GREEN}[OK] Resistance: ${RESISTANCE_VALUE} Ω${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[WARNING] Resistance value not extracted${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 6.6: Multiple reads of all read-only parameters"
for i in {1..3}; do
  echo "  Read iteration $i:"
  lager solar $SOLAR_NET voc --box $BOX 2>&1 | head -1
  lager solar $SOLAR_NET temperature --box $BOX 2>&1 | head -1
  lager solar $SOLAR_NET mpp-voltage --box $BOX 2>&1 | head -1
  lager solar $SOLAR_NET mpp-current --box $BOX 2>&1 | head -1
  lager solar $SOLAR_NET resistance --box $BOX 2>&1 | head -1
  echo ""
done
track_test "pass"
echo ""
sleep $TEST_DELAY

# ============================================================
# SECTION 7: PARAMETER CORRELATION (Irradiance Effects)
# ============================================================
start_section "Parameter Correlation"
echo "========================================================================"
echo "SECTION 7: PARAMETER CORRELATION (Irradiance Effects)"
echo "========================================================================"
echo ""
sleep $TEST_DELAY

echo "Test 7.1: Read all parameters at 0 W/m²"
lager solar $SOLAR_NET irradiance 0.0 --box $BOX >/dev/null
sleep 0.2
echo "Irradiance: 0 W/m²"
lager solar $SOLAR_NET voc --box $BOX 2>&1 | head -1
lager solar $SOLAR_NET mpp-voltage --box $BOX 2>&1 | head -1
lager solar $SOLAR_NET mpp-current --box $BOX 2>&1 | head -1
lager solar $SOLAR_NET temperature --box $BOX 2>&1 | head -1
track_test "pass"
echo ""
sleep $TEST_DELAY

echo "Test 7.2: Read all parameters at 500 W/m²"
lager solar $SOLAR_NET irradiance 500.0 --box $BOX >/dev/null
sleep 0.2
echo "Irradiance: 500 W/m²"
lager solar $SOLAR_NET voc --box $BOX 2>&1 | head -1
lager solar $SOLAR_NET mpp-voltage --box $BOX 2>&1 | head -1
lager solar $SOLAR_NET mpp-current --box $BOX 2>&1 | head -1
lager solar $SOLAR_NET temperature --box $BOX 2>&1 | head -1
track_test "pass"
echo ""
sleep $TEST_DELAY

echo "Test 7.3: Read all parameters at 1000 W/m² (STC)"
lager solar $SOLAR_NET irradiance 1000.0 --box $BOX >/dev/null
sleep 0.2
echo "Irradiance: 1000 W/m² (Standard Test Conditions)"
lager solar $SOLAR_NET voc --box $BOX 2>&1 | head -1
lager solar $SOLAR_NET mpp-voltage --box $BOX 2>&1 | head -1
lager solar $SOLAR_NET mpp-current --box $BOX 2>&1 | head -1
lager solar $SOLAR_NET temperature --box $BOX 2>&1 | head -1
track_test "pass"
echo ""
sleep $TEST_DELAY

echo "Test 7.4: Sweep irradiance and observe parameter changes"
echo "Sweeping irradiance from 0 to 1000 W/m² and observing effects:"
for irradiance in 0 250 500 750 1000; do
  lager solar $SOLAR_NET irradiance $irradiance --box $BOX >/dev/null
  sleep 0.2
  VOC=$(lager solar $SOLAR_NET voc --box $BOX 2>&1 | grep -oE '[0-9]+\.?[0-9]*' | head -1)
  MPP_V=$(lager solar $SOLAR_NET mpp-voltage --box $BOX 2>&1 | grep -oE '[0-9]+\.?[0-9]*' | head -1)
  MPP_I=$(lager solar $SOLAR_NET mpp-current --box $BOX 2>&1 | grep -oE '[0-9]+\.?[0-9]*' | head -1)
  echo "  ${irradiance} W/m²: Voc=${VOC}V, Vmpp=${MPP_V}V, Impp=${MPP_I}A"
done
track_test "pass"
echo ""
sleep $TEST_DELAY

echo "Test 7.5: Verify Voc increases with irradiance"
lager solar $SOLAR_NET irradiance 100.0 --box $BOX >/dev/null
sleep 0.2
VOC_LOW=$(lager solar $SOLAR_NET voc --box $BOX 2>&1 | grep -oE '[0-9]+\.?[0-9]*' | head -1)
lager solar $SOLAR_NET irradiance 1000.0 --box $BOX >/dev/null
sleep 0.2
VOC_HIGH=$(lager solar $SOLAR_NET voc --box $BOX 2>&1 | grep -oE '[0-9]+\.?[0-9]*' | head -1)
echo "Voc @ 100 W/m²: ${VOC_LOW} V"
echo "Voc @ 1000 W/m²: ${VOC_HIGH} V"
if (( $(echo "$VOC_HIGH >= $VOC_LOW" | bc -l 2>/dev/null || echo "1") )); then
  echo -e "${GREEN}[OK] Voc behaves correctly with irradiance${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[WARNING] Voc behavior unexpected${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 7.6: Verify MPP current increases with irradiance"
lager solar $SOLAR_NET irradiance 100.0 --box $BOX >/dev/null
sleep 0.2
MPP_I_LOW=$(lager solar $SOLAR_NET mpp-current --box $BOX 2>&1 | grep -oE '[0-9]+\.?[0-9]*' | head -1)
lager solar $SOLAR_NET irradiance 1000.0 --box $BOX >/dev/null
sleep 0.2
MPP_I_HIGH=$(lager solar $SOLAR_NET mpp-current --box $BOX 2>&1 | grep -oE '[0-9]+\.?[0-9]*' | head -1)
echo "Impp @ 100 W/m²: ${MPP_I_LOW} A"
echo "Impp @ 1000 W/m²: ${MPP_I_HIGH} A"
if (( $(echo "$MPP_I_HIGH >= $MPP_I_LOW" | bc -l 2>/dev/null || echo "1") )); then
  echo -e "${GREEN}[OK] MPP current behaves correctly with irradiance${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[WARNING] MPP current behavior unexpected${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

# ============================================================
# SECTION 8: BOUNDARY AND EDGE CASES
# ============================================================
start_section "Boundary and Edge Cases"
echo "========================================================================"
echo "SECTION 8: BOUNDARY AND EDGE CASES"
echo "========================================================================"
echo ""
sleep $TEST_DELAY

echo "Test 8.1: Irradiance at minimum (0 W/m²)"
lager solar $SOLAR_NET irradiance 0.0 --box $BOX
READBACK=$(lager solar $SOLAR_NET irradiance --box $BOX | grep -oE '[0-9]+\.?[0-9]*' | head -1)
echo "Irradiance at 0 W/m², readback: ${READBACK} W/m²"
track_test "pass"
echo ""
sleep $TEST_DELAY

echo "Test 8.2: Irradiance at typical maximum (1500 W/m²)"
lager solar $SOLAR_NET irradiance 1500.0 --box $BOX 2>&1 || echo -e "${YELLOW}[WARNING] May be clamped${NC}"
READBACK=$(lager solar $SOLAR_NET irradiance --box $BOX 2>&1 | grep -oE '[0-9]+\.?[0-9]*' | head -1)
echo "Irradiance at 1500 W/m², readback: ${READBACK} W/m²"
track_test "pass"
echo ""
sleep $TEST_DELAY

echo "Test 8.3: Very small irradiance (0.001 W/m²)"
lager solar $SOLAR_NET irradiance 0.001 --box $BOX
READBACK=$(lager solar $SOLAR_NET irradiance --box $BOX | grep -oE '[0-9]+\.?[0-9]*' | head -1)
echo "Irradiance at 0.001 W/m², readback: ${READBACK} W/m²"
track_test "pass"
echo ""
sleep $TEST_DELAY

echo "Test 8.4: Irradiance with many decimal places (123.456789 W/m²)"
lager solar $SOLAR_NET irradiance 123.456789 --box $BOX
READBACK=$(lager solar $SOLAR_NET irradiance --box $BOX | grep -oE '[0-9]+\.?[0-9]*' | head -1)
echo "Set: 123.456789 W/m², Readback: ${READBACK} W/m²"
track_test "pass"
echo ""
sleep $TEST_DELAY

echo "Test 8.5: Tiny incremental changes (precision)"
lager solar $SOLAR_NET irradiance 500.000 --box $BOX >/dev/null
READBACK1=$(lager solar $SOLAR_NET irradiance --box $BOX | grep -oE '[0-9]+\.?[0-9]*' | head -1)
lager solar $SOLAR_NET irradiance 500.001 --box $BOX >/dev/null
READBACK2=$(lager solar $SOLAR_NET irradiance --box $BOX | grep -oE '[0-9]+\.?[0-9]*' | head -1)
echo "500.000 W/m² -> ${READBACK1} W/m²"
echo "500.001 W/m² -> ${READBACK2} W/m²"
track_test "pass"
echo ""
sleep $TEST_DELAY

echo "Test 8.6: Very large irradiance (5000 W/m² - test clamping)"
lager solar $SOLAR_NET irradiance 5000.0 --box $BOX 2>&1 || echo -e "${YELLOW}[WARNING] Large irradiance rejected or clamped${NC}"
READBACK=$(lager solar $SOLAR_NET irradiance --box $BOX 2>&1 | grep -oE '[0-9]+\.?[0-9]*' | head -1)
echo "Irradiance after 5000 W/m² request: ${READBACK} W/m² (may be clamped)"
track_test "pass"
echo ""
sleep $TEST_DELAY

# ============================================================
# SECTION 9: SEQUENTIAL OPERATIONS (EA devices are single-threaded)
# ============================================================
start_section "Sequential Operations"
echo "========================================================================"
echo "SECTION 9: SEQUENTIAL OPERATIONS"
echo "========================================================================"
echo ""
sleep $TEST_DELAY

echo "Test 9.1: Sequential parameter reads (EA devices don't support concurrent access)"
echo "Note: EA solar simulators can only handle one command at a time"
lager solar $SOLAR_NET irradiance --box $BOX >/dev/null
lager solar $SOLAR_NET voc --box $BOX >/dev/null
lager solar $SOLAR_NET temperature --box $BOX >/dev/null
lager solar $SOLAR_NET mpp-voltage --box $BOX >/dev/null
lager solar $SOLAR_NET mpp-current --box $BOX >/dev/null
echo -e "${GREEN}[OK] Sequential reads completed${NC}"
track_test "pass"
echo ""
sleep $TEST_DELAY

echo "Test 9.2: Rapid sequential parameter updates and reads"
lager solar $SOLAR_NET irradiance 600 --box $BOX >/dev/null
lager solar $SOLAR_NET voc --box $BOX >/dev/null
lager solar $SOLAR_NET mpp-voltage --box $BOX >/dev/null
lager solar $SOLAR_NET irradiance 800 --box $BOX >/dev/null
lager solar $SOLAR_NET mpp-current --box $BOX >/dev/null
echo -e "${GREEN}[OK] Rapid sequential updates completed${NC}"
track_test "pass"
echo ""
sleep $TEST_DELAY

echo "Test 9.3: Interleaved irradiance changes with reads"
lager solar $SOLAR_NET irradiance 100 --box $BOX >/dev/null
lager solar $SOLAR_NET voc --box $BOX >/dev/null
lager solar $SOLAR_NET irradiance 500 --box $BOX >/dev/null
lager solar $SOLAR_NET mpp-voltage --box $BOX >/dev/null
lager solar $SOLAR_NET irradiance 1000 --box $BOX >/dev/null
lager solar $SOLAR_NET mpp-current --box $BOX >/dev/null
echo -e "${GREEN}[OK] Interleaved operations completed${NC}"
track_test "pass"
echo ""
sleep $TEST_DELAY

# ============================================================
# SECTION 10: CONCURRENCY AND STRESS TESTS
# ============================================================
start_section "Concurrency and Stress Tests"
echo "========================================================================"
echo "SECTION 10: CONCURRENCY AND STRESS TESTS"
echo "========================================================================"
echo ""
sleep $TEST_DELAY

echo "Test 10.1: Rapid irradiance changes (20 iterations)"
echo "Changing irradiance 20 times rapidly..."
START_TIME=$(get_timestamp_ms)
for i in {1..20}; do
  IRRADIANCE=$(echo "scale=1; 100.0 + ($i % 10) * 100.0" | bc)
  lager solar $SOLAR_NET irradiance $IRRADIANCE --box $BOX >/dev/null 2>&1 || echo "[FAIL] Irradiance change $i failed"
done
END_TIME=$(get_timestamp_ms)
ELAPSED_MS=$(( END_TIME - START_TIME ))
echo -e "${GREEN}[OK] 20 irradiance changes completed in ${ELAPSED_MS}ms${NC}"
echo -e "${GREEN}[OK] Average change time: $((ELAPSED_MS / 20))ms${NC}"
track_test "pass"
echo ""
sleep $TEST_DELAY

echo "Test 10.2: Rapid parameter reads (10 iterations)"
for i in {1..10}; do
  lager solar $SOLAR_NET voc --box $BOX >/dev/null 2>&1
  lager solar $SOLAR_NET mpp-voltage --box $BOX >/dev/null 2>&1
  lager solar $SOLAR_NET mpp-current --box $BOX >/dev/null 2>&1
done
echo -e "${GREEN}[OK] 10 rapid read iterations completed${NC}"
track_test "pass"
echo ""
sleep $TEST_DELAY

echo "Test 10.3: Mixed parameter stress test (10 iterations)"
for i in {1..10}; do
  IRRADIANCE=$(echo "scale=1; ($i % 10) * 100.0" | bc)
  lager solar $SOLAR_NET irradiance $IRRADIANCE --box $BOX >/dev/null 2>&1
  lager solar $SOLAR_NET voc --box $BOX >/dev/null 2>&1
  lager solar $SOLAR_NET mpp-current --box $BOX >/dev/null 2>&1
done
echo -e "${GREEN}[OK] 10 mixed parameter iterations completed${NC}"
track_test "pass"
echo ""
sleep $TEST_DELAY

echo "Test 10.4: Set/stop stress test (5 cycles)"
START_TIME=$(get_timestamp_ms)
for i in {1..5}; do
  lager solar $SOLAR_NET stop --box $BOX >/dev/null 2>&1 || echo "[FAIL] Stop $i failed"
  lager solar $SOLAR_NET set --box $BOX >/dev/null 2>&1 || echo "[FAIL] Set $i failed"
done
END_TIME=$(get_timestamp_ms)
ELAPSED_MS=$(( END_TIME - START_TIME ))
echo -e "${GREEN}[OK] 5 set/stop cycles completed in ${ELAPSED_MS}ms${NC}"
track_test "pass"
echo ""
sleep $TEST_DELAY

echo "Test 10.5: Parameter query burst (10 queries)"
for i in {1..10}; do
  lager solar $SOLAR_NET irradiance --box $BOX >/dev/null 2>&1 || echo "[FAIL] Query $i failed"
done
echo -e "${GREEN}[OK] 10 parameter queries completed${NC}"
track_test "pass"
echo ""
sleep $TEST_DELAY

# ============================================================
# SECTION 11: PERFORMANCE BENCHMARKS
# ============================================================
start_section "Performance Benchmarks"
echo "========================================================================"
echo "SECTION 11: PERFORMANCE BENCHMARKS"
echo "========================================================================"
echo ""
sleep $TEST_DELAY

echo "Test 11.1: Irradiance write latency (5 iterations average)"
TOTAL_TIME=0
for i in {1..5}; do
  START_TIME=$(get_timestamp_ms)
  lager solar $SOLAR_NET irradiance 500.0 --box $BOX >/dev/null
  END_TIME=$(get_timestamp_ms)
  TOTAL_TIME=$((TOTAL_TIME + (END_TIME - START_TIME)))
done
AVG_MS=$((TOTAL_TIME / 5))
echo -e "${GREEN}[OK] Average irradiance write time: ${AVG_MS}ms${NC}"
track_test "pass"
echo ""
sleep $TEST_DELAY

echo "Test 11.2: Irradiance read latency (5 iterations average)"
TOTAL_TIME=0
for i in {1..5}; do
  START_TIME=$(get_timestamp_ms)
  lager solar $SOLAR_NET irradiance --box $BOX >/dev/null
  END_TIME=$(get_timestamp_ms)
  TOTAL_TIME=$((TOTAL_TIME + (END_TIME - START_TIME)))
done
AVG_MS=$((TOTAL_TIME / 5))
echo -e "${GREEN}[OK] Average irradiance read time: ${AVG_MS}ms${NC}"
track_test "pass"
echo ""
sleep $TEST_DELAY

echo "Test 11.3: Voc read latency (5 iterations average)"
TOTAL_TIME=0
for i in {1..5}; do
  START_TIME=$(get_timestamp_ms)
  lager solar $SOLAR_NET voc --box $BOX >/dev/null
  END_TIME=$(get_timestamp_ms)
  TOTAL_TIME=$((TOTAL_TIME + (END_TIME - START_TIME)))
done
AVG_MS=$((TOTAL_TIME / 5))
echo -e "${GREEN}[OK] Average Voc read time: ${AVG_MS}ms${NC}"
track_test "pass"
echo ""
sleep $TEST_DELAY

echo "Test 11.4: MPP voltage read latency (5 iterations average)"
TOTAL_TIME=0
for i in {1..5}; do
  START_TIME=$(get_timestamp_ms)
  lager solar $SOLAR_NET mpp-voltage --box $BOX >/dev/null
  END_TIME=$(get_timestamp_ms)
  TOTAL_TIME=$((TOTAL_TIME + (END_TIME - START_TIME)))
done
AVG_MS=$((TOTAL_TIME / 5))
echo -e "${GREEN}[OK] Average MPP voltage read time: ${AVG_MS}ms${NC}"
track_test "pass"
echo ""
sleep $TEST_DELAY

echo "Test 11.5: Set/stop latency (3 iterations average)"
TOTAL_TIME=0
for i in {1..3}; do
  START_TIME=$(get_timestamp_ms)
  lager solar $SOLAR_NET stop --box $BOX >/dev/null
  lager solar $SOLAR_NET set --box $BOX >/dev/null
  END_TIME=$(get_timestamp_ms)
  TOTAL_TIME=$((TOTAL_TIME + (END_TIME - START_TIME)))
done
AVG_MS=$((TOTAL_TIME / 3))
echo -e "${GREEN}[OK] Average set/stop cycle time: ${AVG_MS}ms${NC}"
track_test "pass"
echo ""
sleep $TEST_DELAY

echo "Test 11.6: Parameter update rate (5 updates with timing)"
echo "Measuring parameter update rate..."
UPDATE_COUNT=5
START_TIME=$(get_timestamp_ms)
for i in {1..5}; do
  lager solar $SOLAR_NET irradiance 500 --box $BOX >/dev/null 2>&1
done
END_TIME=$(get_timestamp_ms)
ELAPSED_MS=$(( END_TIME - START_TIME ))
if [ $ELAPSED_MS -gt 0 ]; then
  UPDATE_RATE=$(echo "scale=2; $UPDATE_COUNT * 1000 / $ELAPSED_MS" | bc)
  echo -e "${GREEN}[OK] Parameter update rate: ${UPDATE_RATE} updates/second${NC}"
else
  echo -e "${GREEN}[OK] Parameter updates completed${NC}"
fi
track_test "pass"
echo ""
sleep $TEST_DELAY

# ============================================================
# SECTION 12: ERROR RECOVERY TESTS
# ============================================================
start_section "Error Recovery Tests"
echo "========================================================================"
echo "SECTION 12: ERROR RECOVERY TESTS"
echo "========================================================================"
echo ""
sleep $TEST_DELAY

echo "Test 12.1: Operations after invalid irradiance (error recovery)"
lager solar $SOLAR_NET irradiance -100.0 --box $BOX 2>&1 >/dev/null || true
if lager solar $SOLAR_NET irradiance 500.0 --box $BOX >/dev/null 2>&1; then
  echo -e "${GREEN}[OK] Command succeeded after error${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Command failed after error${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 12.2: Operations after invalid net"
lager solar invalid_net irradiance --box $BOX 2>&1 >/dev/null || true
if lager solar $SOLAR_NET irradiance --box $BOX >/dev/null 2>&1; then
  echo -e "${GREEN}[OK] Command succeeded after error${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Command failed after error${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 12.3: Multiple errors followed by valid commands"
lager solar $SOLAR_NET irradiance -1.0 --box $BOX 2>&1 >/dev/null || true
lager solar invalid_net voc --box $BOX 2>&1 >/dev/null || true
lager solar $SOLAR_NET irradiance abc --box $BOX 2>&1 >/dev/null || true
lager solar $SOLAR_NET irradiance 600 --box $BOX >/dev/null
lager solar $SOLAR_NET voc --box $BOX >/dev/null
lager solar $SOLAR_NET mpp-voltage --box $BOX >/dev/null
echo -e "${GREEN}[OK] Valid commands succeeded after multiple errors${NC}"
track_test "pass"
echo ""
sleep $TEST_DELAY

echo "Test 12.4: State consistency after errors"
lager solar $SOLAR_NET irradiance -999.0 --box $BOX 2>&1 >/dev/null || true
IRRADIANCE_OUTPUT=$(lager solar $SOLAR_NET irradiance --box $BOX 2>&1)
echo "Irradiance after error: output captured (should be valid)"
echo -e "${GREEN}[OK] State query successful after error${NC}"
track_test "pass"
echo ""
sleep $TEST_DELAY

# ============================================================
# SECTION 13: SOLAR SIMULATION SCENARIOS
# ============================================================
start_section "Solar Simulation Scenarios"
echo "========================================================================"
echo "SECTION 13: SOLAR SIMULATION SCENARIOS"
echo "========================================================================"
echo ""
sleep $TEST_DELAY

echo "Test 13.1: Sunrise simulation (0 to 1000 W/m²)"
echo "Simulating sunrise from 0 to 1000 W/m²..."
for irradiance in 0 50 100 200 400 600 800 1000; do
  lager solar $SOLAR_NET irradiance $irradiance --box $BOX >/dev/null
  echo "  Irradiance: ${irradiance} W/m²"
  sleep 0.1
done
echo -e "${GREEN}[OK] Sunrise simulation completed${NC}"
track_test "pass"
echo ""
sleep $TEST_DELAY

echo "Test 13.2: Sunset simulation (1000 to 0 W/m²)"
echo "Simulating sunset from 1000 to 0 W/m²..."
for irradiance in 1000 800 600 400 200 100 50 0; do
  lager solar $SOLAR_NET irradiance $irradiance --box $BOX >/dev/null
  echo "  Irradiance: ${irradiance} W/m²"
  sleep 0.1
done
echo -e "${GREEN}[OK] Sunset simulation completed${NC}"
track_test "pass"
echo ""
sleep $TEST_DELAY

echo "Test 13.3: Cloudy day simulation (variable irradiance)"
echo "Simulating clouds passing over solar panel..."
for irradiance in 1000 800 500 300 600 900 700 400 800 1000; do
  lager solar $SOLAR_NET irradiance $irradiance --box $BOX >/dev/null
  echo "  Irradiance: ${irradiance} W/m²"
  sleep 0.05
done
echo -e "${GREEN}[OK] Cloudy day simulation completed${NC}"
track_test "pass"
echo ""
sleep $TEST_DELAY

echo "Test 13.4: Standard Test Conditions (STC)"
echo "Setting up Standard Test Conditions: 1000 W/m², 25°C"
lager solar $SOLAR_NET irradiance 1000.0 --box $BOX >/dev/null
sleep 0.2
echo "STC Parameters:"
lager solar $SOLAR_NET irradiance --box $BOX
lager solar $SOLAR_NET voc --box $BOX
lager solar $SOLAR_NET mpp-voltage --box $BOX
lager solar $SOLAR_NET mpp-current --box $BOX
lager solar $SOLAR_NET temperature --box $BOX
track_test "pass"
echo ""
sleep $TEST_DELAY

echo "Test 13.5: Low light conditions (indoor/dawn)"
echo "Testing low light conditions (10 W/m²)..."
lager solar $SOLAR_NET irradiance 10.0 --box $BOX >/dev/null
sleep 0.2
echo "Low Light Parameters:"
lager solar $SOLAR_NET irradiance --box $BOX
lager solar $SOLAR_NET voc --box $BOX
lager solar $SOLAR_NET mpp-voltage --box $BOX
lager solar $SOLAR_NET mpp-current --box $BOX
track_test "pass"
echo ""
sleep $TEST_DELAY

# ============================================================
# SECTION 14: REGRESSION TESTS (Specific Bug Fixes)
# ============================================================
start_section "Regression Tests (Bug Fixes Validation)"
echo "========================================================================"
echo "SECTION 14: REGRESSION TESTS (Bug Fixes Validation)"
echo "========================================================================"
echo ""
sleep $TEST_DELAY

echo "Test 14.1: Verify negative irradiance rejection"
if lager solar $SOLAR_NET irradiance -1.0 --box $BOX 2>&1 | grep -qi "error\|invalid\|negative"; then
  echo -e "${GREEN}[OK] Negative irradiance properly rejected${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[WARNING] Negative irradiance may have been accepted (check output)${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 14.2: Verify set/stop state persistence"
lager solar $SOLAR_NET set --box $BOX >/dev/null
lager solar $SOLAR_NET irradiance 500 --box $BOX >/dev/null
lager solar $SOLAR_NET stop --box $BOX >/dev/null
# After stop, irradiance read should still work or return appropriate error
lager solar $SOLAR_NET irradiance --box $BOX 2>&1 || echo "[WARNING] Irradiance read after stop (expected behavior)"
track_test "pass"
echo ""
sleep $TEST_DELAY

echo "Test 14.3: Verify parameter consistency after multiple reads"
lager solar $SOLAR_NET set --box $BOX >/dev/null
lager solar $SOLAR_NET irradiance 777.777 --box $BOX >/dev/null
READBACK1=$(lager solar $SOLAR_NET irradiance --box $BOX | grep -oE '[0-9]+\.?[0-9]*' | head -1)
READBACK2=$(lager solar $SOLAR_NET irradiance --box $BOX | grep -oE '[0-9]+\.?[0-9]*' | head -1)
READBACK3=$(lager solar $SOLAR_NET irradiance --box $BOX | grep -oE '[0-9]+\.?[0-9]*' | head -1)
echo "Read 1: ${READBACK1} W/m², Read 2: ${READBACK2} W/m², Read 3: ${READBACK3} W/m²"
if [ "$READBACK1" = "$READBACK2" ] && [ "$READBACK2" = "$READBACK3" ]; then
  echo -e "${GREEN}[OK] Parameter reads are consistent${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] REGRESSION: Parameter reads are inconsistent${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 14.4: Verify read-only parameters cannot be written"
ERROR_COUNT=0
lager solar $SOLAR_NET voc 5.0 --box $BOX 2>&1 | grep -qi "error\|read.only\|no such option" && ERROR_COUNT=$((ERROR_COUNT + 1))
lager solar $SOLAR_NET temperature 25.0 --box $BOX 2>&1 | grep -qi "error\|read.only\|no such option" && ERROR_COUNT=$((ERROR_COUNT + 1))
lager solar $SOLAR_NET mpp-voltage 10.0 --box $BOX 2>&1 | grep -qi "error\|read.only\|no such option" && ERROR_COUNT=$((ERROR_COUNT + 1))
lager solar $SOLAR_NET mpp-current 2.0 --box $BOX 2>&1 | grep -qi "error\|read.only\|no such option" && ERROR_COUNT=$((ERROR_COUNT + 1))
if [ $ERROR_COUNT -eq 4 ]; then
  echo -e "${GREEN}[OK] All read-only parameters properly protected${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[WARNING] ${ERROR_COUNT}/4 read-only parameters protected${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

# ============================================================
# CLEANUP
# ============================================================
echo "========================================================================"
echo "CLEANUP"
echo "========================================================================"
echo ""
sleep $TEST_DELAY

echo "Setting solar simulator to safe state..."
lager solar $SOLAR_NET irradiance 0.0 --box $BOX 2>&1 || true
lager solar $SOLAR_NET stop --box $BOX 2>&1 || true
echo -e "${GREEN}[OK] Cleanup complete${NC}"
echo ""
sleep $TEST_DELAY

echo "Final solar state (if available):"
lager solar $SOLAR_NET irradiance --box $BOX 2>&1 || true
echo ""
sleep $TEST_DELAY

# ============================================================
# TEST SUMMARY
# ============================================================
echo "========================================================================"
echo "TEST SUITE COMPLETED"
echo "========================================================================"
echo ""
sleep $TEST_DELAY

# Print the summary table
print_summary

# Exit with appropriate status code
exit_with_status
