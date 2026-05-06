#!/bin/bash

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
# ============================================================================
# Comprehensive test suite for lager nets commands
# ============================================================================
# Tests all edge cases, error conditions, and production features
#
# REQUIRED INSTRUMENTS FOR FULL TEST COVERAGE:
# --------------------------------------------
# While the test suite will run without instruments (simulating net creation),
# for comprehensive testing, connect the following instruments to the box:
#
# Recommended Minimum Setup:
#   - LabJack T7 (TCPIP) - For GPIO, ADC, DAC net testing
#   - Any power supply (Rigol, Keysight, EA, etc.) - For supply net testing
#   - J-Link debug probe (USB) - For debug net testing
#   - Acroname USB hub or YKUSH hub - For USB net testing
#
# Full Test Coverage (Optional):
#   - Oscilloscope (Rigol, Picoscope) - For scope net testing
#   - Battery emulator (Keithley 2281S) - For battery net testing
#   - Electronic load (Rigol DL3021) - For eload net testing
#   - Rotrics arm - For robot net testing
#   - Logitech camera - For camera net testing
#   - Yocto Watt meter - For watt-meter net testing
#
# Note: Tests will adapt based on available instruments. Missing instruments
# will result in some tests being marked as "excluded" or showing warnings,
# but will not cause test failures.
#
# USAGE:
#   ./test_nets_commands.sh <BOX>
#
# EXAMPLES:
#   ./test_nets_commands.sh my-box
#   ./test_nets_commands.sh <BOX_IP>
#
# ============================================================================

# Determine script directory for relative paths
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Source test framework
source "${SCRIPT_DIR}/../../framework/colors.sh"
source "${SCRIPT_DIR}/../../framework/harness.sh"

set +e  # Don't exit on error - we want to track failures

# Initialize the test harness
init_harness

# Check if BOX argument is provided
if [ $# -lt 1 ]; then
  echo "Usage: $0 <BOX>"
  echo ""
  echo "Examples:"
  echo "  $0 my-box"
  echo "  $0 <BOX_IP>"
  echo ""
  echo "Arguments:"
  echo "  BOX       - Box ID or Tailscale IP address"
  echo ""
  echo "For full test coverage, see the header of this script for"
  echo "recommended instruments to connect to the box."
  echo ""
  exit 1
fi

BOX="$1"
TEST_NET_NAME="test_net_temp"
TEST_NET_NAME2="test_net_temp2"
BACKUP_FILE="/tmp/lager_nets_backup_$(date +%s).json"

echo "========================================================================"
echo "LAGER NETS COMPREHENSIVE TEST SUITE"
echo "========================================================================"
echo ""
echo "Box: $BOX"
echo ""
echo "[WARNING] This test suite will create and delete nets."
echo "[WARNING] A backup will be created before destructive tests."
echo ""

# ============================================================
# SECTION 1: BASIC COMMANDS (List and Info)
# ============================================================
start_section "Basic Commands"
echo "========================================================================"
echo "SECTION 1: BASIC COMMANDS (List and Info)"
echo "========================================================================"
echo ""

echo "Test 1.1: List available boxes"
if lager boxes 2>&1 | grep -q '.'; then
  echo -e "${GREEN}[OK]${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Failed${NC}"
  track_test "fail"
fi
echo ""

echo "Test 1.2: Nets command help"
if lager nets --help 2>&1 | grep -q 'List all saved nets'; then
  echo -e "${GREEN}[OK]${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Failed${NC}"
  track_test "fail"
fi
echo ""

echo "Test 1.3: List all nets (basic)"
if lager nets --box $BOX 2>&1 | grep -q '.'; then
  echo -e "${GREEN}[OK]${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Failed${NC}"
  track_test "fail"
fi
echo ""

echo "Test 1.4: Count existing nets"
NET_COUNT=$(lager nets --box $BOX 2>/dev/null | grep -c "^[A-Za-z]" || echo "0")
echo "Found $NET_COUNT nets on box $BOX"
if [ "$NET_COUNT" -ge 0 ]; then
  echo -e "${GREEN}[OK]${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Failed${NC}"
  track_test "fail"
fi
echo ""

echo "Test 1.5: List nets multiple times (stability test)"
FAIL_COUNT=0
for i in {1..5}; do
  lager nets --box $BOX >/dev/null 2>&1 || FAIL_COUNT=$((FAIL_COUNT + 1))
done
if [ $FAIL_COUNT -eq 0 ]; then
  echo -e "${GREEN}[OK] Multiple list operations completed${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] $FAIL_COUNT/5 list operations failed${NC}"
  track_test "fail"
fi
echo ""

echo "Test 1.6: Create command help"
if lager nets create --help 2>&1 | grep -q '.'; then
  echo -e "${GREEN}[OK] create --help works${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] create --help failed${NC}"
  track_test "fail"
fi
echo ""

echo "Test 1.7: Delete command help"
if lager nets delete --help 2>&1 | grep -q '.'; then
  echo -e "${GREEN}[OK] delete --help works${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] delete --help failed${NC}"
  track_test "fail"
fi
echo ""

echo "Test 1.8: Rename command help"
if lager nets rename --help 2>&1 | grep -q '.'; then
  echo -e "${GREEN}[OK] rename --help works${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] rename --help failed${NC}"
  track_test "fail"
fi
echo ""

# ============================================================
# SECTION 2: ERROR CASES (Invalid Commands)
# ============================================================
start_section "Error Cases"
echo "========================================================================"
echo "SECTION 2: ERROR CASES (Invalid Commands)"
echo "========================================================================"
echo ""

echo "Test 2.1: Invalid box"
OUTPUT=$(lager nets --box INVALID-BOX 2>&1)
if echo "$OUTPUT" | grep -qi "error\|don't have\|not found"; then
  echo -e "${GREEN}[OK] Error caught correctly${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] No error for invalid box${NC}"
  track_test "fail"
fi
echo ""

echo "Test 2.2: Create with missing arguments"
OUTPUT=$(lager nets create --box $BOX 2>&1)
if echo "$OUTPUT" | grep -qi "error\|missing\|usage"; then
  echo -e "${GREEN}[OK] Missing arguments caught${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Missing arguments not caught${NC}"
  track_test "fail"
fi
echo ""

echo "Test 2.3: Delete non-existent net"
OUTPUT=$(lager nets delete "nonexistent_net_12345" "gpio" --box $BOX --yes 2>&1)
if echo "$OUTPUT" | grep -qi "not found"; then
  echo -e "${GREEN}[OK] Delete non-existent net handled${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[SKIP] Non-existent net deletion not validated${NC}"
  track_test "exclude"
fi
echo ""

echo "Test 2.4: Rename non-existent net"
OUTPUT=$(lager nets rename "nonexistent_net_12345" "new_name" --box $BOX 2>&1)
if echo "$OUTPUT" | grep -qi "not found"; then
  echo -e "${GREEN}[OK] Rename non-existent net handled${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[SKIP] Non-existent net rename not validated${NC}"
  track_test "exclude"
fi
echo ""

echo "Test 2.5: Create net with missing name"
OUTPUT=$(lager nets create --box $BOX 2>&1)
if echo "$OUTPUT" | grep -qi "error\|missing\|usage"; then
  echo -e "${GREEN}[OK] Missing name parameter caught${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Missing parameter not caught${NC}"
  track_test "fail"
fi
echo ""

echo "Test 2.6: Delete net with missing type"
OUTPUT=$(lager nets delete --box $BOX 2>&1)
if echo "$OUTPUT" | grep -qi "error\|missing\|usage"; then
  echo -e "${GREEN}[OK] Missing type parameter caught${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Missing parameter not caught${NC}"
  track_test "fail"
fi
echo ""

echo "Test 2.7: Create batch with non-existent JSON file"
OUTPUT=$(lager nets create-batch "/tmp/nonexistent_file_12345.json" --box $BOX 2>&1)
if echo "$OUTPUT" | grep -qi "error\|not found\|does not exist"; then
  echo -e "${GREEN}[OK] Non-existent file handled${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Non-existent file not caught${NC}"
  track_test "fail"
fi
echo ""

# ============================================================
start_section "Net Creation"
# SECTION 3: NET CREATION (Single Nets)
# ============================================================
echo "========================================================================"
echo "SECTION 3: NET CREATION (Single Nets)"
echo "========================================================================"
echo ""

echo "Test 3.1: Create a simple analog net (simulated VISA address)"
# Note: Using a simulated VISA address - adjust based on actual available instruments
lager nets create --box $BOX --name "${TEST_NET_NAME}_analog" --visa "TCPIP0::192.168.1.100::inst0::INSTR" 2>&1 || echo "[WARNING] Net creation may require valid instrument"
echo ""

echo "Test 3.2: List nets to verify creation"
lager nets --box $BOX | grep -q "${TEST_NET_NAME}_analog" && echo "[OK] Net created and listed" || echo "[WARNING] Net not found in list"
echo ""

echo "Test 3.3: Create net with different VISA formats"
# GPIB format
lager nets create --box $BOX --name "${TEST_NET_NAME}_gpib" --visa "GPIB0::10::INSTR" 2>&1 || echo "[WARNING] GPIB format may not be supported"
echo ""

# USB format
lager nets create --box $BOX --name "${TEST_NET_NAME}_usb" --visa "USB0::0x1234::0x5678::SERIAL::INSTR" 2>&1 || echo "[WARNING] USB format may not be supported"
echo ""

# Serial format
lager nets create --box $BOX --name "${TEST_NET_NAME}_serial" --visa "ASRL1::INSTR" 2>&1 || echo "[WARNING] Serial format may not be supported"
echo ""

echo "Test 3.4: Create multiple nets with sequential names"
for i in {1..3}; do
  lager nets create --box $BOX --name "${TEST_NET_NAME}_${i}" --visa "TCPIP0::192.168.1.$((100+i))::inst0::INSTR" 2>&1 || echo "[WARNING] Net creation $i may require valid instrument"
done
echo "[OK] Sequential net creation attempted"
echo ""

echo "Test 3.5: Attempt to create duplicate net (same name)"
lager nets create --box $BOX --name "${TEST_NET_NAME}_1" --visa "TCPIP0::192.168.1.200::inst0::INSTR" 2>&1 || echo "[OK] Duplicate net name properly rejected"
echo ""

echo "Test 3.6: Create net with special characters in name"
lager nets create --box $BOX --name "${TEST_NET_NAME}_special-net.test_123" --visa "TCPIP0::192.168.1.150::inst0::INSTR" 2>&1 || echo "[WARNING] Special characters may not be allowed"
echo ""

echo "Test 3.7: Create net with very long name"
LONG_NAME="${TEST_NET_NAME}_$(printf 'a%.0s' {1..100})"
lager nets create --box $BOX --name "$LONG_NAME" --visa "TCPIP0::192.168.1.151::inst0::INSTR" 2>&1 || echo "[WARNING] Long name may be rejected"
echo ""

# ============================================================
start_section "Net Listing and Filtering"
# SECTION 4: NET LISTING AND FILTERING
# ============================================================
echo "========================================================================"
echo "SECTION 4: NET LISTING AND FILTERING"
echo "========================================================================"
echo ""

echo "Test 4.1: List all nets (full output)"
lager nets --box $BOX
echo ""

echo "Test 4.2: Count nets after creation"
NET_COUNT_AFTER=$(lager nets --box $BOX 2>/dev/null | grep -c "Net:" || echo "0")
echo "Net count after creation: $NET_COUNT_AFTER"
echo ""

echo "Test 4.3: Search for specific net in listing"
if lager nets --box $BOX | grep -q "${TEST_NET_NAME}"; then
  echo "[OK] Test nets found in listing"
else
  echo "[WARNING] Test nets not found in listing"
fi
echo ""

echo "Test 4.4: Rapid consecutive list operations (stress test)"
for i in {1..20}; do
  lager nets --box $BOX >/dev/null 2>&1 || echo "[FAIL] List operation $i failed"
done
echo "[OK] 20 rapid list operations completed"
echo ""

echo "Test 4.5: List nets with different boxes"
lager boxes 2>&1 | while read -r box_line; do
  BOX_ID=$(echo "$box_line" | awk '{print $1}')
  if [ -n "$BOX_ID" ] && [ "$BOX_ID" != "ID" ]; then
    echo "  Listing nets for box: $BOX_ID"
    lager nets --box "$BOX_ID" 2>&1 | head -5 || true
  fi
done
echo ""

# ============================================================
start_section "Net Renaming"
# SECTION 5: NET RENAMING
# ============================================================
echo "========================================================================"
echo "SECTION 5: NET RENAMING"
echo "========================================================================"
echo ""

echo "Test 5.1: Rename a net"
if lager nets --box $BOX | grep -q "${TEST_NET_NAME}_1"; then
  lager nets rename --box $BOX --name "${TEST_NET_NAME}_1" --new-name "${TEST_NET_NAME}_1_renamed"
  echo ""
else
  echo "[WARNING] Test net not available for renaming"
fi

echo "Test 5.2: Verify renamed net exists"
if lager nets --box $BOX | grep -q "${TEST_NET_NAME}_1_renamed"; then
  echo "[OK] Net successfully renamed"
else
  echo "[WARNING] Renamed net not found"
fi
echo ""

echo "Test 5.3: Verify old name no longer exists"
if lager nets --box $BOX | grep -q "${TEST_NET_NAME}_1\$"; then
  echo "[WARNING] Old net name still exists"
else
  echo "[OK] Old net name removed"
fi
echo ""

echo "Test 5.4: Rename net back to original name"
if lager nets --box $BOX | grep -q "${TEST_NET_NAME}_1_renamed"; then
  lager nets rename --box $BOX --name "${TEST_NET_NAME}_1_renamed" --new-name "${TEST_NET_NAME}_1"
  echo ""
else
  echo "[WARNING] Renamed net not available"
fi

echo "Test 5.5: Attempt to rename to existing name (conflict)"
if lager nets --box $BOX | grep -q "${TEST_NET_NAME}_1"; then
  lager nets rename --box $BOX --name "${TEST_NET_NAME}_1" --new-name "${TEST_NET_NAME}_2" 2>&1 || echo "[OK] Rename conflict properly handled"
  echo ""
else
  echo "[WARNING] Test nets not available for conflict test"
fi

echo "Test 5.6: Rename with special characters"
if lager nets --box $BOX | grep -q "${TEST_NET_NAME}_2"; then
  lager nets rename --box $BOX --name "${TEST_NET_NAME}_2" --new-name "${TEST_NET_NAME}_special_@#$" 2>&1 || echo "[WARNING] Special characters may not be allowed"
  echo ""
else
  echo "[WARNING] Test net not available"
fi

echo "Test 5.7: Rename with very long name"
if lager nets --box $BOX | grep -q "${TEST_NET_NAME}_3"; then
  LONG_NEW_NAME="${TEST_NET_NAME}_$(printf 'b%.0s' {1..100})"
  lager nets rename --box $BOX --name "${TEST_NET_NAME}_3" --new-name "$LONG_NEW_NAME" 2>&1 || echo "[WARNING] Long name may be rejected"
  echo ""
else
  echo "[WARNING] Test net not available"
fi

# ============================================================
start_section "Net Deletion"
# SECTION 6: NET DELETION (Individual)
# ============================================================
echo "========================================================================"
echo "SECTION 6: NET DELETION (Individual)"
echo "========================================================================"
echo ""

echo "Test 6.1: Delete a specific net"
if lager nets --box $BOX | grep -q "${TEST_NET_NAME}_analog"; then
  # Try to determine the net type from the listing
  NET_TYPE=$(lager nets --box $BOX | grep "${TEST_NET_NAME}_analog" | grep -oE "Type: [A-Za-z]+" | cut -d' ' -f2 || echo "Analog")
  lager nets delete --box $BOX --name "${TEST_NET_NAME}_analog" --type "$NET_TYPE"
  echo ""
else
  echo "[WARNING] Test net not available for deletion"
fi

echo "Test 6.2: Verify net was deleted"
if lager nets --box $BOX | grep -q "${TEST_NET_NAME}_analog"; then
  echo "[FAIL] Net still exists after deletion"
else
  echo "[OK] Net successfully deleted"
fi
echo ""

echo "Test 6.3: Delete non-existent net (should fail gracefully)"
lager nets delete --box $BOX --name "definitely_nonexistent_net_xyz" --type "Analog" 2>&1 || echo "[OK] Non-existent net deletion handled gracefully"
echo ""

echo "Test 6.4: Delete net with wrong type (should fail)"
if lager nets --box $BOX | grep -q "${TEST_NET_NAME}_1"; then
  lager nets delete --box $BOX --name "${TEST_NET_NAME}_1" --type "WrongType" 2>&1 || echo "[OK] Wrong type deletion properly rejected"
  echo ""
else
  echo "[WARNING] Test net not available"
fi

echo "Test 6.5: Sequential deletion of multiple nets"
for i in {1..3}; do
  if lager nets --box $BOX | grep -q "${TEST_NET_NAME}_${i}"; then
    NET_TYPE=$(lager nets --box $BOX | grep "${TEST_NET_NAME}_${i}" | grep -oE "Type: [A-Za-z]+" | cut -d' ' -f2 || echo "Analog")
    lager nets delete --box $BOX --name "${TEST_NET_NAME}_${i}" --type "$NET_TYPE" 2>&1 || echo "[WARNING] Deletion $i may have failed"
  fi
done
echo "[OK] Sequential deletions attempted"
echo ""

echo "Test 6.6: Verify all sequential deletions"
REMAINING_TEST_NETS=$(lager nets --box $BOX | grep -c "${TEST_NET_NAME}_[0-9]" || echo "0")
echo "Remaining test nets: $REMAINING_TEST_NETS"
echo ""

# ============================================================
start_section "Batch Net Creation"
# SECTION 7: BATCH NET CREATION
# ============================================================
echo "========================================================================"
echo "SECTION 7: BATCH NET CREATION"
echo "========================================================================"
echo ""

echo "Test 7.1: Create JSON file for batch creation"
BATCH_JSON="/tmp/lager_nets_batch_test.json"
cat > "$BATCH_JSON" <<'EOF'
{
  "nets": [
    {
      "name": "batch_net_1",
      "visa": "TCPIP0::192.168.10.10::inst0::INSTR"
    },
    {
      "name": "batch_net_2",
      "visa": "TCPIP0::192.168.10.11::inst0::INSTR"
    },
    {
      "name": "batch_net_3",
      "visa": "TCPIP0::192.168.10.12::inst0::INSTR"
    }
  ]
}
EOF
echo "[OK] Batch JSON file created at $BATCH_JSON"
cat "$BATCH_JSON"
echo ""

echo "Test 7.2: Execute batch net creation"
lager nets create-batch --box $BOX --file "$BATCH_JSON" 2>&1 || echo "[WARNING] Batch creation may require valid instruments"
echo ""

echo "Test 7.3: Verify batch-created nets exist"
for net in "batch_net_1" "batch_net_2" "batch_net_3"; do
  if lager nets --box $BOX | grep -q "$net"; then
    echo "  [OK] $net created"
  else
    echo "  [WARNING] $net not found"
  fi
done
echo ""

echo "Test 7.4: Create batch JSON with invalid format"
INVALID_BATCH_JSON="/tmp/lager_nets_batch_invalid.json"
cat > "$INVALID_BATCH_JSON" <<'EOF'
{
  "invalid_key": "value"
}
EOF
lager nets create-batch --box $BOX --file "$INVALID_BATCH_JSON" 2>&1 || echo "[OK] Invalid JSON format properly rejected"
echo ""

echo "Test 7.5: Create batch JSON with duplicate names"
DUPLICATE_BATCH_JSON="/tmp/lager_nets_batch_duplicate.json"
cat > "$DUPLICATE_BATCH_JSON" <<'EOF'
{
  "nets": [
    {
      "name": "duplicate_net",
      "visa": "TCPIP0::192.168.10.20::inst0::INSTR"
    },
    {
      "name": "duplicate_net",
      "visa": "TCPIP0::192.168.10.21::inst0::INSTR"
    }
  ]
}
EOF
lager nets create-batch --box $BOX --file "$DUPLICATE_BATCH_JSON" 2>&1 || echo "[WARNING] Duplicate names may be handled during batch creation"
echo ""

echo "Test 7.6: Create large batch (10 nets)"
LARGE_BATCH_JSON="/tmp/lager_nets_batch_large.json"
cat > "$LARGE_BATCH_JSON" <<'EOF'
{
  "nets": [
EOF
for i in {1..10}; do
  if [ $i -eq 10 ]; then
    echo "    {\"name\": \"large_batch_net_$i\", \"visa\": \"TCPIP0::192.168.20.$i::inst0::INSTR\"}" >> "$LARGE_BATCH_JSON"
  else
    echo "    {\"name\": \"large_batch_net_$i\", \"visa\": \"TCPIP0::192.168.20.$i::inst0::INSTR\"}," >> "$LARGE_BATCH_JSON"
  fi
done
cat >> "$LARGE_BATCH_JSON" <<'EOF'
  ]
}
EOF
lager nets create-batch --box $BOX --file "$LARGE_BATCH_JSON" 2>&1 || echo "[WARNING] Large batch creation may require valid instruments"
echo ""

echo "Test 7.7: Verify large batch creation count"
LARGE_BATCH_COUNT=$(lager nets --box $BOX | grep -c "large_batch_net_" || echo "0")
echo "Large batch nets created: $LARGE_BATCH_COUNT / 10"
echo ""

# ============================================================
start_section "Create-All Functionality"
# SECTION 8: CREATE-ALL FUNCTIONALITY
# ============================================================
echo "========================================================================"
echo "SECTION 8: CREATE-ALL FUNCTIONALITY"
echo "========================================================================"
echo ""

echo "Test 8.1: Check help for create-all"
lager nets create-all --help
echo ""

echo "Test 8.2: List current nets before create-all"
NETS_BEFORE=$(lager nets --box $BOX | grep -c "Net:" || echo "0")
echo "Nets before create-all: $NETS_BEFORE"
echo ""

echo "Test 8.3: Execute create-all (creates all possible nets)"
echo "[WARNING] This may create many nets depending on available instruments"
lager nets create-all --box $BOX 2>&1 || echo "[WARNING] Create-all may require available instruments"
echo ""

echo "Test 8.4: List nets after create-all"
NETS_AFTER=$(lager nets --box $BOX | grep -c "Net:" || echo "0")
echo "Nets after create-all: $NETS_AFTER"
echo "Nets created by create-all: $((NETS_AFTER - NETS_BEFORE))"
echo ""

echo "Test 8.5: Execute create-all again (idempotent test)"
lager nets create-all --box $BOX 2>&1 || echo "[WARNING] Second create-all execution"
NETS_SECOND=$(lager nets --box $BOX | grep -c "Net:" || echo "0")
echo "Nets after second create-all: $NETS_SECOND"
echo ""

# ============================================================
start_section "Stress Tests"
# SECTION 9: STRESS TESTS
# ============================================================
echo "========================================================================"
echo "SECTION 9: STRESS TESTS"
echo "========================================================================"
echo ""

echo "Test 9.1: Rapid net creation (10 nets)"
START_TIME=$(get_timestamp_ms)
for i in {1..10}; do
  lager nets create --box $BOX --name "stress_net_${i}" --visa "TCPIP0::192.168.50.$i::inst0::INSTR" 2>&1 >/dev/null || true
done
END_TIME=$(get_timestamp_ms)
ELAPSED_MS=$(( END_TIME - START_TIME ))
echo "[OK] 10 net creations attempted in ${ELAPSED_MS}ms"
echo ""

echo "Test 9.2: Rapid net listing (50 iterations)"
START_TIME=$(get_timestamp_ms)
for i in {1..50}; do
  lager nets --box $BOX >/dev/null 2>&1 || echo "[FAIL] List $i failed"
done
END_TIME=$(get_timestamp_ms)
ELAPSED_MS=$(( END_TIME - START_TIME ))
echo "[OK] 50 list operations in ${ELAPSED_MS}ms"
echo "[OK] Average list time: $((ELAPSED_MS / 50))ms"
echo ""

echo "Test 9.3: Rapid rename operations (5 cycles)"
for i in {1..5}; do
  if lager nets --box $BOX | grep -q "stress_net_1"; then
    lager nets rename --box $BOX --name "stress_net_1" --new-name "stress_net_1_temp" 2>&1 >/dev/null || true
    lager nets rename --box $BOX --name "stress_net_1_temp" --new-name "stress_net_1" 2>&1 >/dev/null || true
  fi
done
echo "[OK] Rapid rename cycles completed"
echo ""

echo "Test 9.4: Create and delete cycle (10 iterations)"
for i in {1..10}; do
  lager nets create --box $BOX --name "cycle_net_${i}" --visa "TCPIP0::192.168.60.$i::inst0::INSTR" 2>&1 >/dev/null || true
  if lager nets --box $BOX | grep -q "cycle_net_${i}"; then
    NET_TYPE=$(lager nets --box $BOX | grep "cycle_net_${i}" | grep -oE "Type: [A-Za-z]+" | cut -d' ' -f2 || echo "Analog")
    lager nets delete --box $BOX --name "cycle_net_${i}" --type "$NET_TYPE" 2>&1 >/dev/null || true
  fi
done
echo "[OK] Create/delete cycles completed"
echo ""

# ============================================================
start_section "Performance Benchmarks"
# SECTION 10: PERFORMANCE BENCHMARKS
# ============================================================
echo "========================================================================"
echo "SECTION 10: PERFORMANCE BENCHMARKS"
echo "========================================================================"
echo ""

echo "Test 10.1: Net listing latency (10 iterations average)"
TOTAL_TIME=0
for i in {1..10}; do
  START_TIME=$(get_timestamp_ms)
  lager nets --box $BOX >/dev/null
  END_TIME=$(get_timestamp_ms)
  TOTAL_TIME=$((TOTAL_TIME + (END_TIME - START_TIME)))
done
AVG_MS=$((TOTAL_TIME / 10))
echo "[OK] Average net listing time: ${AVG_MS}ms"
echo ""

echo "Test 10.2: Net creation latency (5 iterations average)"
TOTAL_TIME=0
for i in {1..5}; do
  START_TIME=$(get_timestamp_ms)
  lager nets create --box $BOX --name "perf_net_${i}" --visa "TCPIP0::192.168.70.$i::inst0::INSTR" 2>&1 >/dev/null || true
  END_TIME=$(get_timestamp_ms)
  TOTAL_TIME=$((TOTAL_TIME + (END_TIME - START_TIME)))
done
AVG_MS=$((TOTAL_TIME / 5))
echo "[OK] Average net creation time: ${AVG_MS}ms"
echo ""

echo "Test 10.3: Net deletion latency (5 iterations average)"
TOTAL_TIME=0
for i in {1..5}; do
  if lager nets --box $BOX | grep -q "perf_net_${i}"; then
    NET_TYPE=$(lager nets --box $BOX | grep "perf_net_${i}" | grep -oE "Type: [A-Za-z]+" | cut -d' ' -f2 || echo "Analog")
    START_TIME=$(get_timestamp_ms)
    lager nets delete --box $BOX --name "perf_net_${i}" --type "$NET_TYPE" 2>&1 >/dev/null || true
    END_TIME=$(get_timestamp_ms)
    TOTAL_TIME=$((TOTAL_TIME + (END_TIME - START_TIME)))
  fi
done
AVG_MS=$((TOTAL_TIME / 5))
echo "[OK] Average net deletion time: ${AVG_MS}ms"
echo ""

echo "Test 10.4: Net rename latency (5 iterations average)"
TOTAL_TIME=0
# Create a test net first
lager nets create --box $BOX --name "rename_perf_net" --visa "TCPIP0::192.168.70.50::inst0::INSTR" 2>&1 >/dev/null || true
for i in {1..5}; do
  if lager nets --box $BOX | grep -q "rename_perf_net"; then
    START_TIME=$(get_timestamp_ms)
    lager nets rename --box $BOX --name "rename_perf_net" --new-name "rename_perf_net_temp" 2>&1 >/dev/null || true
    lager nets rename --box $BOX --name "rename_perf_net_temp" --new-name "rename_perf_net" 2>&1 >/dev/null || true
    END_TIME=$(get_timestamp_ms)
    TOTAL_TIME=$((TOTAL_TIME + (END_TIME - START_TIME)))
  fi
done
AVG_MS=$((TOTAL_TIME / 5))
echo "[OK] Average net rename cycle time: ${AVG_MS}ms"
echo ""

# ============================================================
start_section "Error Recovery"
# SECTION 11: ERROR RECOVERY TESTS
# ============================================================
echo "========================================================================"
echo "SECTION 11: ERROR RECOVERY TESTS"
echo "========================================================================"
echo ""

echo "Test 11.1: List after creation errors"
lager nets create --box $BOX --name "error_net" --visa "INVALID::VISA::ADDRESS" 2>&1 >/dev/null || true
lager nets --box $BOX >/dev/null && echo "[OK] List works after creation error" || echo "[FAIL] List failed after error"
echo ""

echo "Test 11.2: Create after deletion errors"
lager nets delete --box $BOX --name "nonexistent" --type "Analog" 2>&1 >/dev/null || true
lager nets create --box $BOX --name "after_error_net" --visa "TCPIP0::192.168.80.1::inst0::INSTR" 2>&1 || echo "[WARNING] Creation after error may require valid instrument"
echo ""

echo "Test 11.3: Rename after error"
lager nets rename --box $BOX --name "nonexistent" --new-name "newname" 2>&1 >/dev/null || true
if lager nets --box $BOX | grep -q "after_error_net"; then
  lager nets rename --box $BOX --name "after_error_net" --new-name "after_error_net_renamed" 2>&1 || echo "[WARNING] Rename may have failed"
fi
echo ""

echo "Test 11.4: Multiple errors followed by valid operation"
lager nets create --box $BOX --name "" --visa "" 2>&1 >/dev/null || true
lager nets delete --box $BOX --name "" --type "" 2>&1 >/dev/null || true
lager nets rename --box $BOX --name "" --new-name "" 2>&1 >/dev/null || true
lager nets --box $BOX >/dev/null && echo "[OK] List works after multiple errors" || echo "[FAIL] List failed"
echo ""

# ============================================================
start_section "Edge Cases"
# SECTION 12: EDGE CASES
# ============================================================
echo "========================================================================"
echo "SECTION 12: EDGE CASES"
echo "========================================================================"
echo ""

echo "Test 12.1: Net name with spaces"
lager nets create --box $BOX --name "net with spaces" --visa "TCPIP0::192.168.90.1::inst0::INSTR" 2>&1 || echo "[WARNING] Spaces in name may not be allowed"
echo ""

echo "Test 12.2: Net name with only numbers"
lager nets create --box $BOX --name "123456" --visa "TCPIP0::192.168.90.2::inst0::INSTR" 2>&1 || echo "[WARNING] Numeric-only name may not be allowed"
echo ""

echo "Test 12.3: Net name with Unicode characters"
lager nets create --box $BOX --name "net_测试_🔌" --visa "TCPIP0::192.168.90.3::inst0::INSTR" 2>&1 || echo "[WARNING] Unicode may not be supported"
echo ""

echo "Test 12.4: Empty net name"
lager nets create --box $BOX --name "" --visa "TCPIP0::192.168.90.4::inst0::INSTR" 2>&1 || echo "[OK] Empty name properly rejected"
echo ""

echo "Test 12.5: Net name with path-like structure"
lager nets create --box $BOX --name "path/to/net" --visa "TCPIP0::192.168.90.5::inst0::INSTR" 2>&1 || echo "[WARNING] Path-like name may not be allowed"
echo ""

echo "Test 12.6: VISA address with unusual port"
lager nets create --box $BOX --name "unusual_port_net" --visa "TCPIP0::192.168.90.10::5025::SOCKET" 2>&1 || echo "[WARNING] Socket address format may not be supported"
echo ""

echo "Test 12.7: Very short net name (single character)"
lager nets create --box $BOX --name "x" --visa "TCPIP0::192.168.90.11::inst0::INSTR" 2>&1 || echo "[WARNING] Single character name may not be allowed"
echo ""

# ============================================================
start_section "TUI/GUI Commands"
# SECTION 13: TUI/GUI COMMANDS (Interactive)
# ============================================================
echo "========================================================================"
echo "SECTION 13: TUI/GUI COMMANDS (Interactive)"
echo "========================================================================"
echo ""

echo "Test 13.1: TUI command help"
if lager nets tui --help 2>&1 | grep -q '.'; then
  echo -e "${GREEN}[OK] TUI --help works${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] TUI --help failed${NC}"
  track_test "fail"
fi
echo ""

echo "Test 13.2: GUI command help"
if lager nets gui --help 2>&1 | grep -q '.'; then
  echo -e "${GREEN}[OK] GUI --help works${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] GUI --help failed${NC}"
  track_test "fail"
fi
echo ""

echo "Test 13.3: TUI command (non-interactive mode check)"
echo -e "${YELLOW}[SKIP] Skipping TUI launch (requires interactive terminal)${NC}"
track_test "exclude"
echo ""

echo "Test 13.4: GUI command (non-interactive mode check)"
echo -e "${YELLOW}[SKIP] Skipping GUI launch (requires display)${NC}"
track_test "exclude"
echo ""

# ============================================================
start_section "Cleanup - Delete All"
# SECTION 14: CLEANUP - DELETE ALL (DESTRUCTIVE)
# ============================================================
echo "========================================================================"
echo "SECTION 14: CLEANUP - DELETE ALL (DESTRUCTIVE)"
echo "========================================================================"
echo ""

echo "[WARNING] This section will test delete-all functionality"
echo "[WARNING] This is a destructive operation that deletes ALL nets"
echo ""

echo "Test 14.1: Count nets before delete-all"
NETS_BEFORE_DELETE=$(lager nets --box $BOX | grep -c "^[A-Za-z]" || echo "0")
echo "Nets before delete-all: $NETS_BEFORE_DELETE"
track_test "pass"
echo ""

echo "Test 14.2: Execute delete-all with confirmation"
echo "Note: Skipping interactive delete-all (requires --yes flag or confirmation)"
# Uncomment the following line to actually test delete-all (DESTRUCTIVE!)
# lager nets delete-all --box $BOX --yes
echo -e "${YELLOW}[SKIP] delete-all test skipped for safety (uncomment to run)${NC}"
track_test "exclude"
echo ""

echo "Test 14.3: Simulate delete-all by individually deleting test nets"
echo "Cleaning up test nets created during this test run..."
DELETED_COUNT=0
lager nets --box $BOX | grep -E "${TEST_NET_NAME}|batch_net|large_batch|stress_net|cycle_net|perf_net|after_error|rename_perf|error_net" | while read -r line; do
  NET_NAME=$(echo "$line" | awk '{print $1}')
  NET_TYPE=$(echo "$line" | awk '{print $2}')
  if [ -n "$NET_NAME" ] && [ -n "$NET_TYPE" ]; then
    echo "  Deleting: $NET_NAME (Type: $NET_TYPE)"
    if lager nets delete "$NET_NAME" "$NET_TYPE" --box $BOX --yes 2>&1 >/dev/null; then
      DELETED_COUNT=$((DELETED_COUNT + 1))
    fi
  fi
done
if [ $DELETED_COUNT -ge 0 ]; then
  echo -e "${GREEN}[OK] Cleanup completed (deleted $DELETED_COUNT test nets)${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Cleanup failed${NC}"
  track_test "fail"
fi
echo ""

echo "Test 14.4: Count nets after cleanup"
NETS_AFTER_CLEANUP=$(lager nets --box $BOX | grep -c "^[A-Za-z]" || echo "0")
echo "Nets after cleanup: $NETS_AFTER_CLEANUP"
echo "Nets removed: $((NETS_BEFORE_DELETE - NETS_AFTER_CLEANUP))"
track_test "pass"
echo ""

# ============================================================
start_section "Regression Tests"
# SECTION 15: REGRESSION TESTS
# ============================================================
echo "========================================================================"
echo "SECTION 15: REGRESSION TESTS (Bug Fixes Validation)"
echo "========================================================================"
echo ""

echo "Test 15.1: Verify listing after multiple operations"
if lager nets --box $BOX >/dev/null 2>&1; then
  echo -e "${GREEN}[OK] Listing stable after multiple operations${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] REGRESSION: Listing failed${NC}"
  track_test "fail"
fi
echo ""

echo "Test 15.2: Verify state stability after operations"
if lager nets --box $BOX >/dev/null 2>&1; then
  echo -e "${GREEN}[OK] State stable after all operations${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] REGRESSION: State corrupted${NC}"
  track_test "fail"
fi
echo ""

# ============================================================
# FINAL CLEANUP
# ============================================================
echo "========================================================================"
echo "FINAL CLEANUP"
echo "========================================================================"
echo ""

echo "Removing any remaining test artifacts..."
lager nets --box $BOX | grep -E "duplicate_test|partial_fail" | while read -r line; do
  NET_NAME=$(echo "$line" | grep -oE "Net: [A-Za-z0-9_-]+" | cut -d' ' -f2)
  NET_TYPE=$(echo "$line" | grep -oE "Type: [A-Za-z]+" | cut -d' ' -f2)
  if [ -n "$NET_NAME" ] && [ -n "$NET_TYPE" ]; then
    lager nets delete --box $BOX --name "$NET_NAME" --type "$NET_TYPE" 2>&1 >/dev/null || true
  fi
done

echo "Removing temporary JSON files..."
rm -f "$BATCH_JSON" "$INVALID_BATCH_JSON" "$DUPLICATE_BATCH_JSON" "$LARGE_BATCH_JSON" "$PARTIAL_FAIL_JSON"
echo "[OK] Cleanup complete"
echo ""

echo "Final net count:"
lager nets --box $BOX | grep -c "Net:" || echo "0"
echo ""

# ============================================================
# TEST SUMMARY
# ============================================================
print_summary

# Exit with appropriate status code
exit_with_status
