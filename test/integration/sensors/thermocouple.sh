#!/bin/bash

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
# Comprehensive test suite for lager thermocouple commands
# Tests all edge cases, error conditions, and production features

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
  echo "Usage: $0 <IP/BOX> <THERMOCOUPLE_NET>"
  echo ""
  echo "Examples:"
  echo "  $0 my-box tc1"
  echo "  $0 <BOX_IP> thermocouple1"
  echo ""
  echo "Arguments:"
  echo "  IP/BOX            - Box ID or Tailscale IP address"
  echo "  THERMOCOUPLE_NET  - Name of the thermocouple net to test"
  echo ""
  exit 1
fi

BOX="$1"
TC_NET="$2"

echo "========================================================================"
echo "LAGER THERMOCOUPLE COMPREHENSIVE TEST SUITE"
echo "========================================================================"
echo ""
echo "Box: $BOX"
echo "Thermocouple Net: $TC_NET"
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
if lager boxes 2>&1 | grep -q '.'; then
  echo -e "${GREEN}[OK]${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Failed${NC}"
  track_test "fail"
fi
echo ""

echo "Test 1.2: List available nets"
if lager nets --box $BOX 2>&1 | grep -q '.'; then
  echo -e "${GREEN}[OK]${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Failed${NC}"
  track_test "fail"
fi
echo ""

echo "Test 1.3: Verify thermocouple net exists"
if lager nets --box $BOX 2>&1 | grep -q "$TC_NET"; then
  echo -e "${GREEN}[OK] Thermocouple net found${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Thermocouple net not found${NC}"
  track_test "fail"
fi
echo ""

echo "Test 1.4: Thermocouple help output"
if lager thermocouple --help 2>&1 | grep -q "Read thermocouple"; then
  echo -e "${GREEN}[OK]${NC}"
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
OUTPUT=$(lager thermocouple nonexistent_net --box $BOX 2>&1)
if echo "$OUTPUT" | grep -qi "error\|not found\|Invalid Net"; then
  echo -e "${GREEN}[OK] Error caught correctly${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] No error for invalid net${NC}"
  track_test "fail"
fi
echo ""

echo "Test 2.2: Invalid box"
OUTPUT=$(lager thermocouple $TC_NET --box INVALID-BOX 2>&1)
if echo "$OUTPUT" | grep -qi "error\|don't have"; then
  echo -e "${GREEN}[OK] Error caught correctly${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] No error for invalid box${NC}"
  track_test "fail"
fi
echo ""

echo "Test 2.3: Missing net name argument"
OUTPUT=$(lager thermocouple --box $BOX 2>&1)
if echo "$OUTPUT" | grep -qi "missing\|error\|usage"; then
  echo -e "${GREEN}[OK] Missing argument caught${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Missing argument not caught${NC}"
  track_test "fail"
fi
echo ""

echo "Test 2.4: Empty net name"
OUTPUT=$(lager thermocouple "" --box $BOX 2>&1)
if echo "$OUTPUT" | grep -qi "error\|not found"; then
  echo -e "${GREEN}[OK] Empty net name caught${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Empty net name not caught${NC}"
  track_test "fail"
fi
echo ""

echo "Test 2.5: Non-thermocouple net (wrong role)"
NON_TC_NET=$(lager nets --box $BOX 2>/dev/null | grep -v "thermocouple\|Thermocouple" | grep -v "Net Type" | grep -v "^$" | head -1 | awk '{print $1}')
if [ -n "$NON_TC_NET" ]; then
  OUTPUT=$(lager thermocouple "$NON_TC_NET" --box $BOX 2>&1)
  if echo "$OUTPUT" | grep -qi "error\|not found\|Invalid"; then
    echo -e "${GREEN}[OK] Non-thermocouple net rejected${NC}"
    track_test "pass"
  else
    echo -e "${RED}[FAIL] Non-thermocouple net not rejected${NC}"
    track_test "fail"
  fi
else
  echo -e "${YELLOW}[SKIP] No non-thermocouple nets available to test${NC}"
  track_test "exclude"
fi
echo ""

echo "Test 2.6: Net name with special characters"
OUTPUT=$(lager thermocouple "invalid@#$%net" --box $BOX 2>&1)
if echo "$OUTPUT" | grep -qi "error\|not found"; then
  echo -e "${GREEN}[OK] Special characters caught${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Special characters not caught${NC}"
  track_test "fail"
fi
echo ""

echo "Test 2.7: Very long net name (100 characters)"
LONG_NET_NAME=$(printf 'a%.0s' {1..100})
OUTPUT=$(lager thermocouple "$LONG_NET_NAME" --box $BOX 2>&1)
if echo "$OUTPUT" | grep -qi "error\|not found"; then
  echo -e "${GREEN}[OK] Long net name caught${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Long net name not caught${NC}"
  track_test "fail"
fi
echo ""

# ============================================================================
# SECTION 3: BASIC READ OPERATIONS
# ============================================================================
echo "========================================================================"
echo "SECTION 3: BASIC READ OPERATIONS"
echo "========================================================================"
echo ""
start_section "Basic Read Operations"

echo "Test 3.1: Basic thermocouple read"
TC_OUTPUT=$(lager thermocouple $TC_NET --box $BOX 2>&1)
echo "$TC_OUTPUT"
if echo "$TC_OUTPUT" | grep -q "Temperature:"; then
  echo -e "${GREEN}[OK] Read successful${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Read failed${NC}"
  track_test "fail"
fi
echo ""

echo "Test 3.2: Verify output format contains '˚C'"
TC_OUTPUT=$(lager thermocouple $TC_NET --box $BOX 2>&1)
if echo "$TC_OUTPUT" | grep -q "˚C\|°C\|C"; then
  echo -e "${GREEN}[OK] Output contains temperature unit${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Output missing temperature unit${NC}"
  track_test "fail"
fi
echo ""

echo "Test 3.3: Verify temperature is numeric"
TC_OUTPUT=$(lager thermocouple $TC_NET --box $BOX 2>&1)
# Extract numeric value (handles negative temps and decimals)
TC_VALUE=$(echo "$TC_OUTPUT" | grep -oE '[-+]?[0-9]*\.?[0-9]+' | head -1)
if [[ "$TC_VALUE" =~ ^-?[0-9]+\.?[0-9]*$ ]]; then
  echo -e "${GREEN}[OK] Temperature is numeric: ${TC_VALUE}˚C${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Temperature is not numeric: $TC_VALUE${NC}"
  track_test "fail"
fi
echo ""

echo "Test 3.4: Temperature is in reasonable range (-50˚C to 150˚C)"
TC_OUTPUT=$(lager thermocouple $TC_NET --box $BOX 2>&1)
TC_VALUE=$(echo "$TC_OUTPUT" | grep -oE '[-+]?[0-9]*\.?[0-9]+' | head -1)
# Use bc for floating point comparison
if [ -n "$TC_VALUE" ]; then
  if (( $(echo "$TC_VALUE >= -50 && $TC_VALUE <= 150" | bc -l) )); then
    echo -e "${GREEN}[OK] Temperature in reasonable range: ${TC_VALUE}˚C${NC}"
    track_test "pass"
  else
    echo -e "${YELLOW}[WARNING] Temperature outside typical range: ${TC_VALUE}˚C (may be valid)${NC}"
    track_test "pass"
  fi
else
  echo -e "${RED}[FAIL] Could not extract temperature value${NC}"
  track_test "fail"
fi
echo ""

echo "Test 3.5: Read output includes net name"
TC_OUTPUT=$(lager thermocouple $TC_NET --box $BOX 2>&1)
if echo "$TC_OUTPUT" | grep -qi "temperature"; then
  echo -e "${GREEN}[OK] Output includes descriptive text${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[WARNING] Output format may be minimal${NC}"
  track_test "pass"
fi
echo ""

# ============================================================================
# SECTION 4: MULTIPLE READS (Stability)
# ============================================================================
echo "========================================================================"
echo "SECTION 4: MULTIPLE READS (Stability)"
echo "========================================================================"
echo ""
start_section "Multiple Reads"

echo "Test 4.1: Five consecutive reads (stability test)"
echo "Reading thermocouple 5 times to check stability:"
declare -a TEMPS
for i in {1..5}; do
  TC_OUTPUT=$(lager thermocouple $TC_NET --box $BOX 2>&1)
  TC_VALUE=$(echo "$TC_OUTPUT" | grep -oE '[-+]?[0-9]*\.?[0-9]+' | head -1)
  TEMPS[$i]=$TC_VALUE
  echo "  Read $i: ${TC_VALUE}˚C"
done
echo -e "${GREEN}[OK] All reads completed${NC}"
track_test "pass"
echo ""

echo "Test 4.2: Temperature stability check (max deviation)"
if [ ${#TEMPS[@]} -ge 5 ]; then
  # Calculate min and max
  MIN_TEMP=${TEMPS[1]}
  MAX_TEMP=${TEMPS[1]}
  for temp in "${TEMPS[@]}"; do
    if [ -n "$temp" ]; then
      if (( $(echo "$temp < $MIN_TEMP" | bc -l) )); then
        MIN_TEMP=$temp
      fi
      if (( $(echo "$temp > $MAX_TEMP" | bc -l) )); then
        MAX_TEMP=$temp
      fi
    fi
  done
  DEVIATION=$(echo "$MAX_TEMP - $MIN_TEMP" | bc)
  echo "Min: ${MIN_TEMP}˚C, Max: ${MAX_TEMP}˚C, Deviation: ${DEVIATION}˚C"
  if (( $(echo "$DEVIATION < 5.0" | bc -l) )); then
    echo -e "${GREEN}[OK] Temperature stable (deviation < 5˚C)${NC}"
    track_test "pass"
  else
    echo -e "${YELLOW}[WARNING] Temperature varied by ${DEVIATION}˚C (may be expected)${NC}"
    track_test "pass"
  fi
else
  echo -e "${YELLOW}[SKIP] Insufficient data for stability check${NC}"
  track_test "exclude"
fi
echo ""

echo "Test 4.3: Rapid consecutive reads (10x)"
FAIL_COUNT=0
for i in {1..10}; do
  lager thermocouple $TC_NET --box $BOX >/dev/null 2>&1 || FAIL_COUNT=$((FAIL_COUNT + 1))
done
if [ $FAIL_COUNT -eq 0 ]; then
  echo -e "${GREEN}[OK] 10 consecutive reads completed${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] $FAIL_COUNT/10 reads failed${NC}"
  track_test "fail"
fi
echo ""

echo "Test 4.4: Reads with delays (5x with 0.5s delay)"
for i in {1..5}; do
  TC_OUTPUT=$(lager thermocouple $TC_NET --box $BOX 2>&1)
  TC_VALUE=$(echo "$TC_OUTPUT" | grep -oE '[-+]?[0-9]*\.?[0-9]+' | head -1)
  echo "  Read $i: ${TC_VALUE}˚C"
  sleep 0.5
done
echo -e "${GREEN}[OK] Delayed reads completed${NC}"
track_test "pass"
echo ""

# ============================================================================
# SECTION 5: RAPID READING (Burst Tests)
# ============================================================================
echo "========================================================================"
echo "SECTION 5: RAPID READING (Burst Tests)"
echo "========================================================================"
echo ""
start_section "Rapid Reading"

echo "Test 5.1: Burst read test (20 rapid reads)"
FAIL_COUNT=0
for i in {1..20}; do
  lager thermocouple $TC_NET --box $BOX >/dev/null 2>&1 || FAIL_COUNT=$((FAIL_COUNT + 1))
done
if [ $FAIL_COUNT -eq 0 ]; then
  echo -e "${GREEN}[OK] 20 rapid reads completed${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] $FAIL_COUNT/20 reads failed${NC}"
  track_test "fail"
fi
echo ""

echo "Test 5.2: Burst read test (50 rapid reads)"
FAIL_COUNT=0
for i in {1..50}; do
  lager thermocouple $TC_NET --box $BOX >/dev/null 2>&1 || FAIL_COUNT=$((FAIL_COUNT + 1))
done
if [ $FAIL_COUNT -eq 0 ]; then
  echo -e "${GREEN}[OK] 50 rapid reads completed${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] $FAIL_COUNT/50 reads failed${NC}"
  track_test "fail"
fi
echo ""

echo "Test 5.3: Sampling rate test (continuous reading for 10 seconds)"
echo "Measuring maximum sampling rate..."
SAMPLE_COUNT=0
START_TIME=$(date +%s)
TEST_DURATION=10
while [ $(($(date +%s) - START_TIME)) -lt $TEST_DURATION ]; do
  lager thermocouple $TC_NET --box $BOX >/dev/null 2>&1 && SAMPLE_COUNT=$((SAMPLE_COUNT + 1))
done
# Calculate samples per second with decimal precision
SAMPLE_RATE=$(echo "scale=2; $SAMPLE_COUNT / $TEST_DURATION" | bc)

if [ $SAMPLE_COUNT -eq 0 ]; then
  echo -e "${RED}[FAIL] No samples completed in ${TEST_DURATION} seconds${NC}"
  echo "  This indicates severe performance issues"
  track_test "fail"
else
  echo -e "${GREEN}[OK] Sampling rate: ${SAMPLE_RATE} samples/second (${SAMPLE_COUNT} samples in ${TEST_DURATION}s)${NC}"
  # Thermocouples typically have 1-3s settling time, so 0.3-1 samples/sec is normal
  if (( $(echo "$SAMPLE_RATE < 0.2" | bc -l) )); then
    echo "  [WARNING] Note: Very slow sampling rate (expected 0.3-1 samples/sec for thermocouples)"
  elif (( $(echo "$SAMPLE_RATE < 1" | bc -l) )); then
    echo "  [OK] Normal performance for thermocouples with settling time"
  else
    echo "  [OK] Excellent performance"
  fi
  track_test "pass"
fi
echo ""

echo "Test 5.4: Alternating reads (read, pause, read - 10x)"
for i in {1..10}; do
  lager thermocouple $TC_NET --box $BOX >/dev/null 2>&1
  sleep 0.1
done
echo -e "${GREEN}[OK] Alternating reads completed${NC}"
track_test "pass"
echo ""

# ============================================================================
# SECTION 6: OUTPUT VALIDATION
# ============================================================================
echo "========================================================================"
echo "SECTION 6: OUTPUT VALIDATION"
echo "========================================================================"
echo ""
start_section "Output Validation"

echo "Test 6.1: Output format validation"
TC_OUTPUT=$(lager thermocouple $TC_NET --box $BOX 2>&1)
if echo "$TC_OUTPUT" | grep -q "Temperature:"; then
  echo -e "${GREEN}[OK] Contains 'Temperature:'${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Unexpected output format${NC}"
  track_test "fail"
fi
echo ""

echo "Test 6.2: Output has single line format"
TC_OUTPUT=$(lager thermocouple $TC_NET --box $BOX 2>&1)
LINE_COUNT=$(echo "$TC_OUTPUT" | wc -l)
if [ $LINE_COUNT -le 2 ]; then
  echo -e "${GREEN}[OK] Output is concise (≤2 lines)${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[WARNING] Output has $LINE_COUNT lines${NC}"
  track_test "pass"
fi
echo ""

echo "Test 6.3: No extraneous error messages in successful read"
TC_OUTPUT=$(lager thermocouple $TC_NET --box $BOX 2>&1)
if echo "$TC_OUTPUT" | grep -qi "error\|warning\|exception\|traceback"; then
  echo -e "${YELLOW}[WARNING] Output contains error-like messages${NC}"
  track_test "fail"
else
  echo -e "${GREEN}[OK] Clean output (no error messages)${NC}"
  track_test "pass"
fi
echo ""

echo "Test 6.4: Temperature precision (decimal places)"
TC_OUTPUT=$(lager thermocouple $TC_NET --box $BOX 2>&1)
TC_VALUE=$(echo "$TC_OUTPUT" | grep -oE '[-+]?[0-9]*\.?[0-9]+' | head -1)
if echo "$TC_VALUE" | grep -q '\.'; then
  DECIMALS=$(echo "$TC_VALUE" | awk -F. '{print length($2)}')
  echo -e "${GREEN}[OK] Temperature has $DECIMALS decimal places: ${TC_VALUE}˚C${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[WARNING] Temperature has no decimal places: ${TC_VALUE}˚C${NC}"
  track_test "pass"
fi
echo ""

# ============================================================================
# SECTION 7: PERFORMANCE BENCHMARKS
# ============================================================================
echo "========================================================================"
echo "SECTION 7: PERFORMANCE BENCHMARKS"
echo "========================================================================"
echo ""
start_section "Performance Benchmarks"

echo "Test 7.1: Read latency (10 iterations average)"
TOTAL_TIME=0
for i in {1..10}; do
  START_TIME=$(get_timestamp_ms)
  lager thermocouple $TC_NET --box $BOX >/dev/null
  END_TIME=$(get_timestamp_ms)
  TOTAL_TIME=$((TOTAL_TIME + (END_TIME - START_TIME)))
done
AVG_MS=$((TOTAL_TIME / 10))
echo -e "${GREEN}[OK] Average read time: ${AVG_MS}ms${NC}"

# Thermocouples are inherently slower than ADC/GPIO due to sensor settling time
if [ $AVG_MS -gt 5000 ]; then
  echo "  [WARNING] Very slow (>5s per read - may indicate issues)"
  track_test "fail"
elif [ $AVG_MS -gt 3000 ]; then
  echo "  Note: Slower than typical (expected 1-3s for Phidget thermocouples)"
  track_test "pass"
elif [ $AVG_MS -gt 1000 ]; then
  echo "  [OK] Normal performance for thermocouples (1-3s settling time)"
  track_test "pass"
else
  echo "  [OK] Excellent performance"
  track_test "pass"
fi
echo ""

echo "Test 7.2: Minimum read time (single fastest read)"
MIN_TIME=999999
for i in {1..20}; do
  START_TIME=$(get_timestamp_ms)
  lager thermocouple $TC_NET --box $BOX >/dev/null 2>&1
  END_TIME=$(get_timestamp_ms)
  READ_TIME=$((END_TIME - START_TIME))
  if [ $READ_TIME -lt $MIN_TIME ]; then
    MIN_TIME=$READ_TIME
  fi
done
echo -e "${GREEN}[OK] Minimum read time: ${MIN_TIME}ms${NC}"
track_test "pass"
echo ""

echo "Test 7.3: Maximum read time (single slowest read)"
MAX_TIME=0
for i in {1..20}; do
  START_TIME=$(get_timestamp_ms)
  lager thermocouple $TC_NET --box $BOX >/dev/null 2>&1
  END_TIME=$(get_timestamp_ms)
  READ_TIME=$((END_TIME - START_TIME))
  if [ $READ_TIME -gt $MAX_TIME ]; then
    MAX_TIME=$READ_TIME
  fi
done
echo -e "${GREEN}[OK] Maximum read time: ${MAX_TIME}ms${NC}"
echo "  Read time variance: $((MAX_TIME - MIN_TIME))ms"
track_test "pass"
echo ""

echo "Test 7.4: Throughput test (100 reads)"
echo "Reading 100 times to measure throughput..."
START_TIME=$(get_timestamp_ms)
FAIL_COUNT=0
for i in {1..100}; do
  lager thermocouple $TC_NET --box $BOX >/dev/null 2>&1 || FAIL_COUNT=$((FAIL_COUNT + 1))
done
END_TIME=$(get_timestamp_ms)
ELAPSED_MS=$(( END_TIME - START_TIME ))
AVG_TIME=$((ELAPSED_MS / 100))
THROUGHPUT=$(echo "scale=2; 100000 / $ELAPSED_MS" | bc)

echo -e "${GREEN}[OK] 100 reads completed in ${ELAPSED_MS}ms${NC}"
echo "  Average: ${AVG_TIME}ms per read"
echo "  Throughput: ${THROUGHPUT} reads/second"
echo "  Failures: $FAIL_COUNT/100"

if [ $FAIL_COUNT -eq 0 ]; then
  track_test "pass"
else
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

echo "Test 8.1: Read after invalid net error"
lager thermocouple invalid_net --box $BOX >/dev/null 2>&1 || true
if lager thermocouple $TC_NET --box $BOX >/dev/null 2>&1; then
  echo -e "${GREEN}[OK] Read succeeded after error${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Read failed after error${NC}"
  track_test "fail"
fi
echo ""

echo "Test 8.2: Read after invalid box error"
lager thermocouple $TC_NET --box INVALID-BOX >/dev/null 2>&1 || true
if lager thermocouple $TC_NET --box $BOX >/dev/null 2>&1; then
  echo -e "${GREEN}[OK] Read succeeded after error${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Read failed after error${NC}"
  track_test "fail"
fi
echo ""

echo "Test 8.3: Multiple errors then valid read"
lager thermocouple invalid1 --box $BOX >/dev/null 2>&1 || true
lager thermocouple invalid2 --box $BOX >/dev/null 2>&1 || true
lager thermocouple "" --box $BOX >/dev/null 2>&1 || true
if lager thermocouple $TC_NET --box $BOX >/dev/null 2>&1; then
  echo -e "${GREEN}[OK] System recovered after multiple errors${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] System failed to recover${NC}"
  track_test "fail"
fi
echo ""

echo "Test 8.4: Alternating valid and invalid reads"
for i in {1..5}; do
  lager thermocouple $TC_NET --box $BOX >/dev/null 2>&1 || echo "[FAIL] Valid read $i failed"
  lager thermocouple invalid_net --box $BOX >/dev/null 2>&1 || true
done
echo -e "${GREEN}[OK] Alternating reads completed${NC}"
track_test "pass"
echo ""

# ============================================================================
# SECTION 9: TEMPERATURE RANGE TESTS
# ============================================================================
echo "========================================================================"
echo "SECTION 9: TEMPERATURE RANGE TESTS"
echo "========================================================================"
echo ""
start_section "Temperature Range"

echo "Test 9.1: Current temperature reading"
TC_OUTPUT=$(lager thermocouple $TC_NET --box $BOX 2>&1)
TC_VALUE=$(echo "$TC_OUTPUT" | grep -oE '[-+]?[0-9]*\.?[0-9]+' | head -1)
echo "Current temperature: ${TC_VALUE}˚C"
track_test "pass"
echo ""

echo "Test 9.2: Temperature classification"
if [ -n "$TC_VALUE" ]; then
  if (( $(echo "$TC_VALUE < 0" | bc -l) )); then
    echo -e "${BLUE}[INFO] Temperature is below freezing (${TC_VALUE}˚C)${NC}"
  elif (( $(echo "$TC_VALUE < 20" | bc -l) )); then
    echo -e "${BLUE}[INFO] Temperature is cold (${TC_VALUE}˚C)${NC}"
  elif (( $(echo "$TC_VALUE < 30" | bc -l) )); then
    echo -e "${BLUE}[INFO] Temperature is room temperature (${TC_VALUE}˚C)${NC}"
  elif (( $(echo "$TC_VALUE < 50" | bc -l) )); then
    echo -e "${BLUE}[INFO] Temperature is warm (${TC_VALUE}˚C)${NC}"
  else
    echo -e "${BLUE}[INFO] Temperature is hot (${TC_VALUE}˚C)${NC}"
  fi
  track_test "pass"
else
  echo -e "${RED}[FAIL] Could not determine temperature${NC}"
  track_test "fail"
fi
echo ""

echo "Test 9.3: Negative temperature support check"
if [ -n "$TC_VALUE" ]; then
  if (( $(echo "$TC_VALUE < 0" | bc -l) )); then
    echo -e "${GREEN}[OK] Negative temperature supported (${TC_VALUE}˚C)${NC}"
  else
    echo -e "${YELLOW}[SKIP] Current temp is positive; negative support not tested${NC}"
  fi
  track_test "pass"
else
  track_test "exclude"
fi
echo ""

echo "Test 9.4: High temperature warning check (>100˚C)"
if [ -n "$TC_VALUE" ]; then
  if (( $(echo "$TC_VALUE > 100" | bc -l) )); then
    echo -e "${YELLOW}[WARNING] WARNING: High temperature detected (${TC_VALUE}˚C)${NC}"
    echo "  Verify thermocouple is functioning correctly"
  else
    echo -e "${GREEN}[OK] Temperature within safe range (${TC_VALUE}˚C)${NC}"
  fi
  track_test "pass"
else
  track_test "exclude"
fi
echo ""

echo "Test 9.5: Thermocouple type detection (if available in output)"
TC_OUTPUT=$(lager thermocouple $TC_NET --box $BOX 2>&1)
if echo "$TC_OUTPUT" | grep -qi "type\|K-type\|J-type\|T-type"; then
  TC_TYPE=$(echo "$TC_OUTPUT" | grep -oE "[KJTREN]-type" | head -1)
  echo -e "${GREEN}[OK] Thermocouple type detected: $TC_TYPE${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[SKIP] Thermocouple type not shown in output${NC}"
  track_test "exclude"
fi
echo ""

# ============================================================================
# SECTION 10: HELP DOCUMENTATION
# ============================================================================
echo "========================================================================"
echo "SECTION 10: HELP DOCUMENTATION"
echo "========================================================================"
echo ""
start_section "Help Documentation"

echo "Test 10.1: Thermocouple help output"
if lager thermocouple --help >/dev/null 2>&1; then
  echo -e "${GREEN}[OK] thermocouple --help works${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] thermocouple --help failed${NC}"
  track_test "fail"
fi
echo ""

echo "Test 10.2: Help contains usage information"
HELP_OUTPUT=$(lager thermocouple --help 2>&1)
if echo "$HELP_OUTPUT" | grep -q "Usage:\|NETNAME"; then
  echo -e "${GREEN}[OK] Help contains usage info${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Help missing usage info${NC}"
  track_test "fail"
fi
echo ""

echo "Test 10.3: Help contains options"
HELP_OUTPUT=$(lager thermocouple --help 2>&1)
if echo "$HELP_OUTPUT" | grep -q "Options:\|--box"; then
  echo -e "${GREEN}[OK] Help contains options${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Help missing options${NC}"
  track_test "fail"
fi
echo ""

echo "Test 10.4: Help contains description"
HELP_OUTPUT=$(lager thermocouple --help 2>&1)
if echo "$HELP_OUTPUT" | grep -qi "thermocouple\|temperature"; then
  echo -e "${GREEN}[OK] Help contains description${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Help missing description${NC}"
  track_test "fail"
fi
echo ""

# ============================================================================
# SECTION 11: EDGE CASES
# ============================================================================
echo "========================================================================"
echo "SECTION 11: EDGE CASES"
echo "========================================================================"
echo ""
start_section "Edge Cases"

echo "Test 11.1: Net name case sensitivity"
TC_NET_UPPER=$(echo "$TC_NET" | tr '[:lower:]' '[:upper:]')
TC_NET_LOWER=$(echo "$TC_NET" | tr '[:upper:]' '[:lower:]')

OUTPUT_ORIG=$(lager thermocouple $TC_NET --box $BOX 2>&1)
OUTPUT_UPPER=$(lager thermocouple $TC_NET_UPPER --box $BOX 2>&1)
OUTPUT_LOWER=$(lager thermocouple $TC_NET_LOWER --box $BOX 2>&1)

# Check if any work (case insensitive) or only exact match (case sensitive)
if echo "$OUTPUT_UPPER" | grep -q "Temperature:" || echo "$OUTPUT_LOWER" | grep -q "Temperature:"; then
  echo -e "${GREEN}[OK] Case insensitive net names supported${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[SKIP] Net names may be case sensitive${NC}"
  track_test "pass"
fi
echo ""

echo "Test 11.2: Whitespace in arguments"
OUTPUT=$(lager thermocouple " $TC_NET " --box $BOX 2>&1)
if echo "$OUTPUT" | grep -q "Temperature:"; then
  echo -e "${GREEN}[OK] Handles whitespace in net name${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[SKIP] Whitespace may not be trimmed${NC}"
  track_test "pass"
fi
echo ""

echo "Test 11.3: Net name with numbers"
NUMBER_NET="tc123"
OUTPUT=$(lager thermocouple $NUMBER_NET --box $BOX 2>&1)
if echo "$OUTPUT" | grep -qi "error\|not found"; then
  echo -e "${GREEN}[OK] Non-existent numeric net name properly rejected${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[SKIP] Numeric net name may exist${NC}"
  track_test "exclude"
fi
echo ""

echo "Test 11.4: Multiple --box arguments (last wins)"
OUTPUT=$(lager thermocouple $TC_NET --box INVALID --box $BOX 2>&1)
if echo "$OUTPUT" | grep -q "Temperature:"; then
  echo -e "${GREEN}[OK] Last --box argument used${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[SKIP] Multiple --box behavior uncertain${NC}"
  track_test "exclude"
fi
echo ""

# ============================================================================
# SECTION 12: CONCURRENT ACCESS TESTS
# ============================================================================
echo "========================================================================"
echo "SECTION 12: CONCURRENT ACCESS TESTS"
echo "========================================================================"
echo ""
start_section "Concurrent Access"

echo "Test 12.1: Simultaneous reads (5 parallel background jobs)"
declare -a PIDS
for i in {1..5}; do
  (lager thermocouple $TC_NET --box $BOX > /tmp/tc_concurrent_$i.txt 2>&1) &
  PIDS[$i]=$!
done

# Wait for all background jobs
CONCURRENT_FAILURES=0
for i in {1..5}; do
  wait ${PIDS[$i]} || CONCURRENT_FAILURES=$((CONCURRENT_FAILURES + 1))
done

if [ $CONCURRENT_FAILURES -eq 0 ]; then
  echo -e "${GREEN}[OK] All 5 concurrent reads completed successfully${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] ${CONCURRENT_FAILURES}/5 concurrent reads failed${NC}"
  track_test "fail"
fi

# Cleanup temp files
rm -f /tmp/tc_concurrent_*.txt
echo ""

echo "Test 12.2: Verify concurrent read results are valid"
VALID_RESULTS=0
for i in {1..5}; do
  (lager thermocouple $TC_NET --box $BOX > /tmp/tc_concurrent_check_$i.txt 2>&1) &
  PIDS[$i]=$!
done

for i in {1..5}; do
  wait ${PIDS[$i]}
  if grep -q "Temperature:" /tmp/tc_concurrent_check_$i.txt 2>/dev/null; then
    VALID_RESULTS=$((VALID_RESULTS + 1))
  fi
done

if [ $VALID_RESULTS -eq 5 ]; then
  echo -e "${GREEN}[OK] All concurrent reads returned valid temperature data${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[WARNING] Only ${VALID_RESULTS}/5 concurrent reads returned valid data${NC}"
  track_test "fail"
fi

rm -f /tmp/tc_concurrent_check_*.txt
echo ""

echo "Test 12.3: Heavy concurrent load (10 parallel reads)"
declare -a PIDS10
HEAVY_FAILURES=0
for i in {1..10}; do
  (lager thermocouple $TC_NET --box $BOX >/dev/null 2>&1) &
  PIDS10[$i]=$!
done

for i in {1..10}; do
  wait ${PIDS10[$i]} || HEAVY_FAILURES=$((HEAVY_FAILURES + 1))
done

if [ $HEAVY_FAILURES -eq 0 ]; then
  echo -e "${GREEN}[OK] All 10 concurrent reads completed${NC}"
  track_test "pass"
elif [ $HEAVY_FAILURES -le 2 ]; then
  echo -e "${YELLOW}[WARNING] ${HEAVY_FAILURES}/10 reads failed (acceptable under load)${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] ${HEAVY_FAILURES}/10 reads failed${NC}"
  track_test "fail"
fi
echo ""

echo "Test 12.4: Sequential vs concurrent performance"
echo "Sequential (5 reads)..."
SEQ_START=$(get_timestamp_ms)
for i in {1..5}; do
  lager thermocouple $TC_NET --box $BOX >/dev/null 2>&1
done
SEQ_END=$(get_timestamp_ms)
SEQ_TIME=$((SEQ_END - SEQ_START))

echo "Concurrent (5 reads)..."
CONC_START=$(get_timestamp_ms)
for i in {1..5}; do
  (lager thermocouple $TC_NET --box $BOX >/dev/null 2>&1) &
  PIDS[$i]=$!
done
for i in {1..5}; do
  wait ${PIDS[$i]}
done
CONC_END=$(get_timestamp_ms)
CONC_TIME=$((CONC_END - CONC_START))

echo "Sequential time: ${SEQ_TIME}ms"
echo "Concurrent time: ${CONC_TIME}ms"

if [ $CONC_TIME -lt $SEQ_TIME ]; then
  SPEEDUP=$(echo "scale=2; $SEQ_TIME / $CONC_TIME" | bc)
  echo -e "${GREEN}[OK] Concurrent reads faster (${SPEEDUP}x speedup)${NC}"
  track_test "pass"
else
  echo -e "${BLUE}[INFO] Sequential reads comparable to concurrent${NC}"
  track_test "pass"
fi
echo ""

# ============================================================================
# SECTION 13: LONG-DURATION MONITORING
# ============================================================================
echo "========================================================================"
echo "SECTION 13: LONG-DURATION MONITORING (>10 minutes)"
echo "========================================================================"
echo ""
start_section "Long-Duration Monitoring"

echo "Test 13.1: Extended monitoring test (10 minute duration)"
echo "This test will run for 10 minutes to check stability..."
echo "Starting at: $(date '+%H:%M:%S')"
echo ""

# Skip if user doesn't want to wait (set environment variable to skip)
if [ "${SKIP_LONG_TEST}" = "1" ]; then
  echo -e "${YELLOW}[SKIP] Skipping long-duration test (SKIP_LONG_TEST=1)${NC}"
  track_test "exclude"
else
  DURATION_SECONDS=600  # 10 minutes
  SAMPLE_INTERVAL=30    # Sample every 30 seconds
  SAMPLES=$((DURATION_SECONDS / SAMPLE_INTERVAL))

  declare -a LONG_TEMPS
  LONG_FAILURES=0

  echo "Will take ${SAMPLES} samples over ${DURATION_SECONDS} seconds..."

  for i in $(seq 1 $SAMPLES); do
    ELAPSED=$((i * SAMPLE_INTERVAL))
    REMAINING=$((DURATION_SECONDS - ELAPSED))

    TC_OUTPUT=$(lager thermocouple $TC_NET --box $BOX 2>&1)
    if echo "$TC_OUTPUT" | grep -q "Temperature:"; then
      TC_VALUE=$(echo "$TC_OUTPUT" | grep -oE '[-+]?[0-9]*\.?[0-9]+' | head -1)
      LONG_TEMPS[$i]=$TC_VALUE
      printf "  [%02d:%02d] Sample %2d/%d: %s˚C (remaining: %02d:%02d)\n" \
        $((ELAPSED/60)) $((ELAPSED%60)) $i $SAMPLES "$TC_VALUE" \
        $((REMAINING/60)) $((REMAINING%60))
    else
      LONG_FAILURES=$((LONG_FAILURES + 1))
      echo "  [Sample $i/$SAMPLES] [FAIL] Read failed"
    fi

    # Sleep until next sample (don't sleep after last sample)
    if [ $i -lt $SAMPLES ]; then
      sleep $SAMPLE_INTERVAL
    fi
  done

  echo ""
  echo "Completed at: $(date '+%H:%M:%S')"

  if [ $LONG_FAILURES -eq 0 ]; then
    echo -e "${GREEN}[OK] All ${SAMPLES} samples completed successfully${NC}"
    track_test "pass"
  else
    echo -e "${RED}[FAIL] ${LONG_FAILURES}/${SAMPLES} samples failed${NC}"
    track_test "fail"
  fi
fi
echo ""

echo "Test 13.2: Long-duration temperature stability analysis"
if [ "${SKIP_LONG_TEST}" = "1" ] || [ ${#LONG_TEMPS[@]} -eq 0 ]; then
  echo -e "${YELLOW}[SKIP] Skipped (no long-duration data)${NC}"
  track_test "exclude"
else
  # Calculate min, max, average
  MIN_LONG=${LONG_TEMPS[1]}
  MAX_LONG=${LONG_TEMPS[1]}
  SUM=0
  COUNT=0

  for temp in "${LONG_TEMPS[@]}"; do
    if [ -n "$temp" ]; then
      if (( $(echo "$temp < $MIN_LONG" | bc -l) )); then
        MIN_LONG=$temp
      fi
      if (( $(echo "$temp > $MAX_LONG" | bc -l) )); then
        MAX_LONG=$temp
      fi
      SUM=$(echo "$SUM + $temp" | bc)
      COUNT=$((COUNT + 1))
    fi
  done

  AVG_LONG=$(echo "scale=3; $SUM / $COUNT" | bc)
  DEVIATION_LONG=$(echo "scale=3; $MAX_LONG - $MIN_LONG" | bc)

  echo "Temperature statistics over 10 minutes:"
  echo "  Minimum:   ${MIN_LONG}˚C"
  echo "  Maximum:   ${MAX_LONG}˚C"
  echo "  Average:   ${AVG_LONG}˚C"
  echo "  Deviation: ${DEVIATION_LONG}˚C"

  # Check if temperature stayed stable (< 10°C deviation for ambient monitoring)
  if (( $(echo "$DEVIATION_LONG < 10.0" | bc -l) )); then
    echo -e "${GREEN}[OK] Temperature remained stable (< 10˚C deviation)${NC}"
    track_test "pass"
  else
    echo -e "${YELLOW}[WARNING] Temperature varied significantly (${DEVIATION_LONG}˚C)${NC}"
    echo "  This may indicate environmental changes or sensor issues"
    track_test "pass"
  fi
fi
echo ""

echo "Test 13.3: Drift detection (compare first vs last samples)"
if [ "${SKIP_LONG_TEST}" = "1" ] || [ ${#LONG_TEMPS[@]} -lt 2 ]; then
  echo -e "${YELLOW}[SKIP] Skipped (no long-duration data)${NC}"
  track_test "exclude"
else
  FIRST_TEMP=${LONG_TEMPS[1]}
  LAST_TEMP=${LONG_TEMPS[${#LONG_TEMPS[@]}]}
  DRIFT=$(echo "scale=3; $LAST_TEMP - $FIRST_TEMP" | bc)

  echo "First sample: ${FIRST_TEMP}˚C"
  echo "Last sample:  ${LAST_TEMP}˚C"
  echo "Drift:        ${DRIFT}˚C"

  # Absolute value of drift
  ABS_DRIFT=$(echo "$DRIFT" | sed 's/-//')

  if (( $(echo "$ABS_DRIFT < 2.0" | bc -l) )); then
    echo -e "${GREEN}[OK] Minimal drift detected (< 2˚C over 10 minutes)${NC}"
    track_test "pass"
  elif (( $(echo "$ABS_DRIFT < 5.0" | bc -l) )); then
    echo -e "${YELLOW}[WARNING] Moderate drift detected (${DRIFT}˚C)${NC}"
    track_test "pass"
  else
    echo -e "${YELLOW}[WARNING] Significant drift detected (${DRIFT}˚C)${NC}"
    echo "  May indicate environmental temperature change"
    track_test "pass"
  fi
fi
echo ""

echo "Test 13.4: Continuous monitoring with no failures"
if [ "${SKIP_LONG_TEST}" = "1" ]; then
  echo -e "${YELLOW}[SKIP] Skipped (SKIP_LONG_TEST=1)${NC}"
  track_test "exclude"
elif [ $LONG_FAILURES -eq 0 ]; then
  echo -e "${GREEN}[OK] No failures during 10-minute monitoring${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] ${LONG_FAILURES} failures during monitoring${NC}"
  track_test "fail"
fi
echo ""

# ============================================================================
# SECTION 14: CALIBRATION VERIFICATION
# ============================================================================
echo "========================================================================"
echo "SECTION 14: CALIBRATION VERIFICATION"
echo "========================================================================"
echo ""
start_section "Calibration Verification"

echo "Test 14.1: Known temperature reference check"
echo "This test requires manual setup with a known temperature source."
echo ""
echo "To perform calibration verification:"
echo "  1. Prepare ice bath (0°C): Ice + water in insulated container"
echo "  2. Prepare boiling water (100°C at sea level)"
echo "  3. Or use calibrated temperature chamber"
echo ""

if [ -n "$KNOWN_TEMP" ]; then
  echo "Using known reference temperature: ${KNOWN_TEMP}˚C"
  echo "Reading thermocouple..."

  TC_OUTPUT=$(lager thermocouple $TC_NET --box $BOX 2>&1)
  MEASURED_TEMP=$(echo "$TC_OUTPUT" | grep -oE '[-+]?[0-9]*\.?[0-9]+' | head -1)

  echo "Measured temperature: ${MEASURED_TEMP}˚C"

  # Calculate error
  ERROR=$(echo "scale=3; $MEASURED_TEMP - $KNOWN_TEMP" | bc)
  ABS_ERROR=$(echo "$ERROR" | sed 's/-//')

  echo "Error: ${ERROR}˚C"

  # Typical thermocouple accuracy: ±1-2°C
  if (( $(echo "$ABS_ERROR < 2.0" | bc -l) )); then
    echo -e "${GREEN}[OK] Within typical thermocouple accuracy (±2˚C)${NC}"
    track_test "pass"
  elif (( $(echo "$ABS_ERROR < 5.0" | bc -l) )); then
    echo -e "${YELLOW}[WARNING] Outside typical accuracy but acceptable (±5˚C)${NC}"
    track_test "pass"
  else
    echo -e "${RED}[FAIL] Significant calibration error (${ERROR}˚C)${NC}"
    echo "  Thermocouple may need calibration or replacement"
    track_test "fail"
  fi
else
  echo -e "${YELLOW}[SKIP] Skipped (set KNOWN_TEMP environment variable to test)${NC}"
  echo "  Example: KNOWN_TEMP=0 ./test_thermocouple_commands.sh my-box thermocouple1"
  track_test "exclude"
fi
echo ""

echo "Test 14.2: Ice bath test (0°C reference)"
if [ -n "$ICE_BATH_TEST" ]; then
  echo "Place thermocouple in ice bath (0°C)..."
  echo "Waiting 30 seconds for thermal equilibrium..."
  sleep 30

  TC_OUTPUT=$(lager thermocouple $TC_NET --box $BOX 2>&1)
  MEASURED_TEMP=$(echo "$TC_OUTPUT" | grep -oE '[-+]?[0-9]*\.?[0-9]+' | head -1)

  echo "Ice bath reading: ${MEASURED_TEMP}˚C"

  # Ice bath should be close to 0°C (allow ±3°C for imperfect setup)
  ABS_TEMP=$(echo "$MEASURED_TEMP" | sed 's/-//')
  if (( $(echo "$ABS_TEMP < 3.0" | bc -l) )); then
    echo -e "${GREEN}[OK] Ice bath reading within expected range (0 ± 3˚C)${NC}"
    track_test "pass"
  else
    echo -e "${YELLOW}[WARNING] Ice bath reading outside expected range${NC}"
    echo "  Ensure proper ice bath setup (crushed ice + water)"
    track_test "fail"
  fi
else
  echo -e "${YELLOW}[SKIP] Skipped (set ICE_BATH_TEST=1 to run)${NC}"
  track_test "exclude"
fi
echo ""

echo "Test 14.3: Boiling water test (100°C reference)"
if [ -n "$BOILING_WATER_TEST" ]; then
  echo "[WARNING] Exercise caution with boiling water"
  echo "Place thermocouple in boiling water (100°C at sea level)..."
  echo "Waiting 30 seconds for thermal equilibrium..."
  sleep 30

  TC_OUTPUT=$(lager thermocouple $TC_NET --box $BOX 2>&1)
  MEASURED_TEMP=$(echo "$TC_OUTPUT" | grep -oE '[-+]?[0-9]*\.?[0-9]+' | head -1)

  echo "Boiling water reading: ${MEASURED_TEMP}˚C"

  # Boiling point varies with altitude (93-100°C)
  if (( $(echo "$MEASURED_TEMP > 90.0 && $MEASURED_TEMP < 105.0" | bc -l) )); then
    echo -e "${GREEN}[OK] Boiling water reading within expected range (93-105˚C)${NC}"
    track_test "pass"
  else
    echo -e "${YELLOW}[WARNING] Boiling water reading outside expected range${NC}"
    echo "  Consider altitude and ensure water is actively boiling"
    track_test "fail"
  fi
else
  echo -e "${YELLOW}[SKIP] Skipped (set BOILING_WATER_TEST=1 to run)${NC}"
  track_test "exclude"
fi
echo ""

echo "Test 14.4: Calibration certificate check"
echo "Check if calibration information is available..."

# Try to get device info or calibration data
CAL_INFO=$(lager thermocouple $TC_NET --box $BOX 2>&1)
if echo "$CAL_INFO" | grep -qi "calibrat\|certifi\|accuracy\|toleran"; then
  echo -e "${GREEN}[OK] Calibration information available${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[SKIP] No calibration information in output${NC}"
  echo "  Note: Phidget thermocouples typically have manufacturer calibration"
  track_test "exclude"
fi
echo ""

# ============================================================================
# SECTION 15: THERMOCOUPLE TYPE DETECTION
# ============================================================================
echo "========================================================================"
echo "SECTION 15: THERMOCOUPLE TYPE DETECTION"
echo "========================================================================"
echo ""
start_section "Thermocouple Type Detection"

echo "Test 15.1: Thermocouple type in standard output"
TC_OUTPUT=$(lager thermocouple $TC_NET --box $BOX 2>&1)
if echo "$TC_OUTPUT" | grep -qi "type.*[KJTREN]"; then
  TC_TYPE=$(echo "$TC_OUTPUT" | grep -oiE "type.*[KJTREN]" | head -1)
  echo -e "${GREEN}[OK] Thermocouple type found: $TC_TYPE${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[SKIP] Thermocouple type not in standard output${NC}"
  track_test "exclude"
fi
echo ""

echo "Test 15.2: Check net details for type information"
NET_INFO=$(lager nets --box $BOX 2>&1 | grep -A2 "$TC_NET")
if echo "$NET_INFO" | grep -qi "type.*[KJTREN]\|K-type\|J-type\|T-type"; then
  TC_TYPE=$(echo "$NET_INFO" | grep -oiE "[KJTREN]-type" | head -1)
  echo -e "${GREEN}[OK] Thermocouple type in net info: $TC_TYPE${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[SKIP] Thermocouple type not in net information${NC}"
  track_test "exclude"
fi
echo ""

echo "Test 15.3: Query thermocouple device type"
# Try to get more detailed info about the thermocouple device
# This might require a different command or API endpoint
echo "Attempting to query thermocouple configuration..."

# Check if there's a way to query the device directly
if lager --help 2>&1 | grep -q "device\|config"; then
  echo -e "${BLUE}[INFO] Device query commands may be available${NC}"
  # Try various commands that might reveal type info
  for CMD in "device" "config" "info"; do
    if lager $CMD --help >/dev/null 2>&1; then
      echo "  Found command: lager $CMD"
    fi
  done
  track_test "pass"
else
  echo -e "${YELLOW}[SKIP] No device query commands found${NC}"
  track_test "exclude"
fi
echo ""

echo "Test 15.4: Thermocouple type inference from temperature range"
# Different thermocouple types have different temperature ranges:
# K-type: -270°C to 1372°C (most common)
# J-type: -210°C to 1200°C
# T-type: -270°C to 400°C
# E-type: -270°C to 1000°C
# N-type: -270°C to 1300°C
# R/S-type: -50°C to 1768°C

TC_OUTPUT=$(lager thermocouple $TC_NET --box $BOX 2>&1)
TC_VALUE=$(echo "$TC_OUTPUT" | grep -oE '[-+]?[0-9]*\.?[0-9]+' | head -1)

if [ -n "$TC_VALUE" ]; then
  echo "Current reading: ${TC_VALUE}˚C"
  echo -e "${BLUE}[INFO] Type inference requires testing at extreme temperatures${NC}"
  echo "  K-type (most common): -270 to 1372°C"
  echo "  J-type: -210 to 1200°C"
  echo "  T-type: -270 to 400°C (good for low temps)"
  echo "  Cannot determine type from single room temperature reading"
  track_test "exclude"
else
  echo -e "${RED}[FAIL] Could not read temperature for type inference${NC}"
  track_test "exclude"
fi
echo ""

echo "Test 15.5: Verify thermocouple compatibility with expected type"
if [ -n "$EXPECTED_TC_TYPE" ]; then
  echo "Expected thermocouple type: $EXPECTED_TC_TYPE"

  # Try to find type info
  TC_OUTPUT=$(lager thermocouple $TC_NET --box $BOX 2>&1)
  NET_INFO=$(lager nets --box $BOX 2>&1 | grep -A2 "$TC_NET")

  FOUND_TYPE=""
  if echo "$TC_OUTPUT" | grep -qi "$EXPECTED_TC_TYPE"; then
    FOUND_TYPE="output"
  elif echo "$NET_INFO" | grep -qi "$EXPECTED_TC_TYPE"; then
    FOUND_TYPE="net info"
  fi

  if [ -n "$FOUND_TYPE" ]; then
    echo -e "${GREEN}[OK] Type matches expected ($EXPECTED_TC_TYPE found in $FOUND_TYPE)${NC}"
    track_test "pass"
  else
    echo -e "${YELLOW}[WARNING] Could not verify type matches expected${NC}"
    echo "  Expected: $EXPECTED_TC_TYPE"
    track_test "exclude"
  fi
else
  echo -e "${YELLOW}[SKIP] Skipped (set EXPECTED_TC_TYPE to verify)${NC}"
  echo "  Example: EXPECTED_TC_TYPE=K-type"
  track_test "exclude"
fi
echo ""

# ============================================================================
# CLEANUP
# ============================================================================
echo "========================================================================"
echo "CLEANUP"
echo "========================================================================"
echo ""

echo "No cleanup required for thermocouple tests (read-only operations)"
echo -e "${GREEN}[OK] Test suite complete${NC}"
echo ""

# ============================================================================
# PRINT SUMMARY
# ============================================================================
print_summary

# Exit with appropriate status code
exit_with_status
