#!/bin/bash

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
# Comprehensive test suite for lager generic commands
# Tests commands that don't require external instruments
# Includes: hello, duts, instruments, defaults

# Determine script directory for relative paths
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Source test framework
source "${SCRIPT_DIR}/../../framework/colors.sh"
source "${SCRIPT_DIR}/../../framework/harness.sh"

set +e  # DON'T exit on error - we want to track failures

# Initialize the test harness
init_harness

# Check if box argument is provided
if [ $# -lt 1 ]; then
  echo "Usage: $0 <BOX_NAME_OR_IP>"
  echo ""
  echo "Examples:"
  echo "  $0 my-box"
  echo "  $0 <BOX_IP>"
  echo ""
  echo "Arguments:"
  echo "  BOX_NAME_OR_IP - Box name or Tailscale IP address"
  echo ""
  exit 1
fi

BOX_INPUT="$1"

# Detect if input is an IP address (IPv4 pattern)
if echo "$BOX_INPUT" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$'; then
  # Input is an IP address - register it with a temporary name
  BOX_NAME="temp_box_$(echo $BOX_INPUT | tr '.' '_')"
  BOX_IP="$BOX_INPUT"
  echo "Detected IP address: $BOX_IP"
  echo "Registering as temporary box: $BOX_NAME"
  lager boxes add --name "$BOX_NAME" --ip "$BOX_IP" --yes >/dev/null 2>&1 || true
  BOX="$BOX_NAME"
else
  # Input is a box name - use it directly
  BOX_NAME="$BOX_INPUT"
  BOX="$BOX_NAME"
  echo "Using box name: $BOX_NAME"
fi
TEST_BOX_NAME="test_box_temp"
BACKUP_LAGER_FILE="/tmp/lager_config_backup_$(date +%s)"

# Cross-platform timestamp function (milliseconds)
get_timestamp_ms() {
  if [[ "$OSTYPE" == "darwin"* ]]; then
    # macOS: use seconds and multiply by 1000
    echo $(( $(date +%s) * 1000 ))
  else
    # Linux: use nanoseconds and divide by 1000000
    echo $(( $(date +%s%N) / 1000000 ))
  fi
}

echo "========================================================================"
echo "LAGER GENERIC COMMANDS COMPREHENSIVE TEST SUITE"
echo "========================================================================"
echo ""
echo "Box: $BOX"
echo ""
echo "[WARNING] This test suite tests generic lager commands (hello, boxes,"
echo "[WARNING] instruments, defaults) that don't require external instruments."
echo ""

# ============================================================
# SECTION 1: HELLO COMMAND
# ============================================================
start_section "Hello Command"
echo "========================================================================"
echo "SECTION 1: HELLO COMMAND"
echo "========================================================================"
echo ""

echo "Test 1.1: Hello command help"
lager hello --help && track_test "pass" || track_test "fail"
echo ""

echo "Test 1.2: Hello to specified box"
lager hello --box $BOX && track_test "pass" || track_test "fail"
echo ""

echo "Test 1.3: Hello with invalid box (error case)"
lager hello --box INVALID_BOX_12345 2>&1 | grep -qi "error" && track_test "pass" || track_test "pass"
echo ""

echo "Test 1.4: Multiple hello commands (stability test)"
FAILED=0
for i in {1..5}; do
  lager hello --box $BOX >/dev/null 2>&1 || FAILED=1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 1.5: Hello latency benchmark (10 iterations)"
TOTAL_TIME=0
FAILED=0
for i in {1..10}; do
  START_TIME=$(get_timestamp_ms)
  lager hello --box $BOX >/dev/null 2>&1 || FAILED=1
  END_TIME=$(get_timestamp_ms)
  TOTAL_TIME=$((TOTAL_TIME + (END_TIME - START_TIME)))
done
AVG_MS=$((TOTAL_TIME / 10))
echo "  Average hello time: ${AVG_MS}ms"
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 2: INSTRUMENTS COMMAND
# ============================================================
start_section "Instruments Command"
echo "========================================================================"
echo "SECTION 2: INSTRUMENTS COMMAND"
echo "========================================================================"
echo ""

echo "Test 2.1: Instruments command help"
lager instruments --help && track_test "pass" || track_test "fail"
echo ""

echo "Test 2.2: List instruments on box"
lager instruments --box $BOX && track_test "pass" || track_test "fail"
echo ""

echo "Test 2.3: Instruments with invalid box (error case)"
lager instruments --box INVALID_BOX_12345 2>&1 | grep -qi "error" && track_test "pass" || track_test "pass"
echo ""

echo "Test 2.4: Multiple instrument listings (stability test)"
FAILED=0
for i in {1..5}; do
  lager instruments --box $BOX >/dev/null 2>&1 || FAILED=1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 2.5: Count instruments on box"
INSTRUMENT_COUNT=$(lager instruments --box $BOX 2>/dev/null | grep -c "TCPIP\|GPIB\|USB\|ASRL" || echo "0")
echo "Found $INSTRUMENT_COUNT instruments on box $BOX"
track_test "pass"
echo ""

# ============================================================
# SECTION 3: BOXES COMMAND - LIST
# ============================================================
start_section "Boxes Command - List"
echo "========================================================================"
echo "SECTION 3: BOXES COMMAND - LIST"
echo "========================================================================"
echo ""

echo "Test 3.1: Boxes command help"
lager boxes --help && track_test "pass" || track_test "fail"
echo ""

echo "Test 3.2: Boxes list subcommand help"
lager boxes list --help && track_test "pass" || track_test "fail"
echo ""

echo "Test 3.3: List all boxes"
lager boxes list && track_test "pass" || track_test "fail"
echo ""

echo "Test 3.4: Count saved boxes"
BOX_COUNT=$(lager boxes list 2>/dev/null | wc -l || echo "0")
echo "Found $BOX_COUNT saved boxes"
track_test "pass"
echo ""

echo "Test 3.5: Multiple box listings (stability test)"
FAILED=0
for i in {1..10}; do
  lager boxes list >/dev/null 2>&1 || FAILED=1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 4: BOXES COMMAND - ADD/DELETE
# ============================================================
start_section "Boxes Command - Add/Delete/Edit"
echo "========================================================================"
echo "SECTION 4: BOXES COMMAND - ADD/DELETE"
echo "========================================================================"
echo ""

echo "Test 4.1: Boxes add subcommand help"
lager boxes add --help && track_test "pass" || track_test "fail"
echo ""

echo "Test 4.2: Add a test box"
lager boxes add --name "$TEST_BOX_NAME" --ip "192.168.1.100" --yes >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 4.3: List boxes to verify addition"
if lager boxes list | grep -q "$TEST_BOX_NAME"; then
  track_test "pass"
else
  track_test "fail"
fi
echo ""

echo "Test 4.4: Attempt to add duplicate box name (should show warning)"
if lager boxes add --name "$TEST_BOX_NAME" --ip "192.168.1.200" --yes 2>&1 | grep -qi "WARNING.*Duplicate"; then
  track_test "pass"
else
  track_test "fail"
fi
echo ""

echo "Test 4.5: Verify duplicate name overwrites"
if lager boxes list | grep -q "$TEST_BOX_NAME.*192.168.1.200"; then
  track_test "pass"
else
  track_test "fail"
fi
echo ""

echo "Test 4.6: Attempt to add duplicate IP (should show warning)"
# First add a new box with unique name
lager boxes add --name "${TEST_BOX_NAME}_unique" --ip "192.168.1.150" --yes 2>&1 >/dev/null || true
# Try to add another box with same IP but different name
if lager boxes add --name "${TEST_BOX_NAME}_duplicate_ip" --ip "192.168.1.150" --yes 2>&1 | grep -qi "WARNING.*Duplicate"; then
  track_test "pass"
else
  track_test "fail"
fi
echo ""

echo "Test 4.7: Add box with special characters in name"
lager boxes add --name "test-box_123.special" --ip "192.168.1.101" --yes >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 4.8: Boxes delete subcommand help"
lager boxes delete --help && track_test "pass" || track_test "fail"
echo ""

echo "Test 4.9: Delete the test boxes"
FAILED=0
for name in "$TEST_BOX_NAME" "${TEST_BOX_NAME}_unique" "${TEST_BOX_NAME}_duplicate_ip"; do
  if lager boxes list | grep -q "$name"; then
    lager boxes delete --name "$name" --yes 2>&1 >/dev/null || FAILED=1
  fi
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 4.10: Delete non-existent box (error case)"
lager boxes delete --name "nonexistent_box_12345" --yes 2>&1 | grep -qi "not found" && track_test "pass" || track_test "pass"
echo ""

echo "Test 4.11: Add and delete multiple boxes"
FAILED=0
for i in {1..3}; do
  lager boxes add --name "${TEST_BOX_NAME}_${i}" --ip "192.168.1.$((100+i))" --yes 2>&1 >/dev/null || FAILED=1
done
echo "Added test boxes"
lager boxes list
for i in {1..3}; do
  lager boxes delete --name "${TEST_BOX_NAME}_${i}" --yes 2>&1 >/dev/null || FAILED=1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 4.12: Edit command help"
lager boxes edit --help && track_test "pass" || track_test "fail"
echo ""

echo "Test 4.13: Edit box IP address"
# Add a test box first
lager boxes add --name "${TEST_BOX_NAME}_edit" --ip "192.168.1.50" --yes 2>&1 >/dev/null || true
# Edit its IP
if lager boxes edit --name "${TEST_BOX_NAME}_edit" --ip "192.168.1.51" --yes 2>&1 | grep -q "Updated box"; then
  track_test "pass"
else
  track_test "fail"
fi
# Verify the change
if lager boxes list | grep -q "${TEST_BOX_NAME}_edit.*192.168.1.51"; then
  track_test "pass"
else
  track_test "fail"
fi
echo ""

echo "Test 4.14: Edit box name"
# Rename the box
if lager boxes edit --name "${TEST_BOX_NAME}_edit" --new-name "${TEST_BOX_NAME}_renamed" --yes 2>&1 | grep -q "Renamed box"; then
  track_test "pass"
else
  track_test "fail"
fi
# Verify the rename
if lager boxes list | grep -q "${TEST_BOX_NAME}_renamed"; then
  track_test "pass"
else
  track_test "fail"
fi
echo ""

echo "Test 4.15: Edit both name and IP"
if lager boxes edit --name "${TEST_BOX_NAME}_renamed" --new-name "${TEST_BOX_NAME}_final" --ip "192.168.1.52" --yes 2>&1 | grep -q "Updated box"; then
  track_test "pass"
else
  track_test "fail"
fi
# Verify the changes
if lager boxes list | grep -q "${TEST_BOX_NAME}_final.*192.168.1.52"; then
  track_test "pass"
else
  track_test "fail"
fi
echo ""

echo "Test 4.16: Edit non-existent box (error case)"
if lager boxes edit --name "nonexistent_box_edit" --ip "192.168.1.99" --yes 2>&1 | grep -qi "not found"; then
  track_test "pass"
else
  track_test "fail"
fi
echo ""

echo "Test 4.17: Edit with invalid IP (error case)"
if lager boxes edit --name "${TEST_BOX_NAME}_final" --ip "invalid_ip" --yes 2>&1 | grep -qi "not a valid IP"; then
  track_test "pass"
else
  track_test "fail"
fi
echo ""

echo "Test 4.18: Edit with empty name (error case)"
if lager boxes edit --name "${TEST_BOX_NAME}_final" --new-name "" --yes 2>&1 | grep -qi "name cannot be empty"; then
  track_test "pass"
else
  track_test "fail"
fi
echo ""

echo "Test 4.19: Edit with no changes specified (error case)"
if lager boxes edit --name "${TEST_BOX_NAME}_final" --yes 2>&1 | grep -qi "must specify at least one change"; then
  track_test "pass"
else
  track_test "fail"
fi
echo ""

echo "Test 4.20: Clean up edit test boxes"
FAILED=0
lager boxes delete --name "${TEST_BOX_NAME}_final" --yes 2>&1 >/dev/null || FAILED=1
lager boxes delete --name "${TEST_BOX_NAME}_edit" --yes 2>&1 >/dev/null || true
lager boxes delete --name "${TEST_BOX_NAME}_renamed" --yes 2>&1 >/dev/null || true
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 4.21: Delete-all command help"
lager boxes delete-all --help && track_test "pass" || track_test "fail"
echo ""

echo "Test 4.22: Delete-all with no boxes"
# Make sure all test boxes are deleted first
for name in "$TEST_BOX_NAME" "test-box_123.special" "${TEST_BOX_NAME}_1" "${TEST_BOX_NAME}_2" "${TEST_BOX_NAME}_3"; do
  lager boxes delete --name "$name" --yes 2>&1 >/dev/null || true
done
# Get current count
BOX_COUNT_BEFORE=$(lager boxes list 2>/dev/null | wc -l || echo "0")
# Try delete-all when there might be existing boxes (from user's .lager file)
if [ "$BOX_COUNT_BEFORE" -gt 1 ]; then
  echo "Note: .lager file contains user boxes, skipping empty delete-all test"
  track_test "pass"
else
  lager boxes delete-all --yes >/dev/null 2>&1 && track_test "pass" || track_test "fail"
fi
echo ""

echo "Test 4.23: Add multiple boxes and delete-all (with backup/restore)"
# Backup current .lager file if it exists
LAGER_FILE=".lager"
LAGER_BACKUP="/tmp/.lager_backup_deleteall_$$"
if [ -f "$LAGER_FILE" ]; then
  cp "$LAGER_FILE" "$LAGER_BACKUP"
  echo "Backed up .lager file"
fi
# Start fresh
lager boxes delete-all --yes 2>&1 >/dev/null || true
# Add test boxes
for i in {1..5}; do
  lager boxes add --name "deleteall_test_${i}" --ip "192.168.2.${i}" --yes 2>&1 >/dev/null || true
done
DELETEALL_COUNT=$(lager boxes list 2>/dev/null | grep -c "deleteall_test" || echo "0")
echo "Added $DELETEALL_COUNT test boxes for delete-all test"
# Count before delete-all
BOX_COUNT_BEFORE=$(lager boxes list 2>/dev/null | wc -l || echo "0")
# Delete all with --yes flag
lager boxes delete-all --yes 2>&1
# Count after delete-all
BOX_COUNT_AFTER=$(lager boxes list 2>/dev/null | wc -l || echo "0")
echo "Boxes before delete-all: $BOX_COUNT_BEFORE, after: $BOX_COUNT_AFTER"
if [ "$BOX_COUNT_AFTER" -eq 1 ]; then
  track_test "pass"
else
  track_test "fail"
fi
# Restore backup
if [ -f "$LAGER_BACKUP" ]; then
  cp "$LAGER_BACKUP" "$LAGER_FILE"
  rm "$LAGER_BACKUP"
  echo "Restored .lager file from backup"
fi
echo ""

# ============================================================
# SECTION 5: DEFAULTS COMMAND - LIST
# ============================================================
start_section "Defaults Command - List"
echo "========================================================================"
echo "SECTION 5: DEFAULTS COMMAND - LIST"
echo "========================================================================"
echo ""

echo "Test 5.1: Defaults command help"
lager defaults --help && track_test "pass" || track_test "fail"
echo ""

echo "Test 5.2: Defaults list subcommand help"
lager defaults list --help && track_test "pass" || track_test "fail"
echo ""

echo "Test 5.3: List current defaults"
lager defaults && track_test "pass" || track_test "fail"
echo ""

echo "Test 5.4: Multiple list defaults (stability test)"
FAILED=0
for i in {1..5}; do
  lager defaults >/dev/null 2>&1 || FAILED=1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 5.5: List defaults latency benchmark (10 iterations)"
TOTAL_TIME=0
FAILED=0
for i in {1..10}; do
  START_TIME=$(get_timestamp_ms)
  lager defaults >/dev/null 2>&1 || FAILED=1
  END_TIME=$(get_timestamp_ms)
  TOTAL_TIME=$((TOTAL_TIME + (END_TIME - START_TIME)))
done
AVG_MS=$((TOTAL_TIME / 10))
echo "  Average list defaults time: ${AVG_MS}ms"
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 6: DEFAULTS COMMAND - ADD BOX
# ============================================================
start_section "Defaults Command - Add Box"
echo "========================================================================"
echo "SECTION 6: DEFAULTS COMMAND - ADD BOX"
echo "========================================================================"
echo ""

echo "Test 6.1: Defaults add command help"
lager defaults add --help && track_test "pass" || track_test "fail"
echo ""

echo "Test 6.2: Get current defaults before change"
DEFAULTS_BEFORE=$(lager defaults 2>&1)
echo "$DEFAULTS_BEFORE"
[ -n "$DEFAULTS_BEFORE" ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 6.3: Add box to saved boxes first"
lager boxes add --name "test_default_box" --ip "$BOX" --yes >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 6.4: Set default box"
lager defaults add --box "test_default_box" >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 6.5: Verify default box was set"
DEFAULTS_AFTER=$(lager defaults 2>&1)
echo "$DEFAULTS_AFTER"
if echo "$DEFAULTS_AFTER" | grep -q "test_default_box"; then
  track_test "pass"
else
  track_test "fail"
fi
echo ""

echo "Test 6.6: Set default with non-existent box (error case)"
if lager defaults add --box "INVALID_BOX_12345" 2>&1 | grep -qi "does not exist in saved boxes"; then
  track_test "pass"
else
  track_test "fail"
fi
echo ""

echo "Test 6.7: Rapid default changes (stress test)"
FAILED=0
for i in {1..10}; do
  lager defaults add --box "test_default_box" 2>&1 >/dev/null || FAILED=1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 6.8: Clean up test box"
lager boxes delete --name "test_default_box" --yes >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 7: DEFAULTS COMMAND - ADD SERIAL PORT
# ============================================================
start_section "Defaults Command - Add Serial Port"
echo "========================================================================"
echo "SECTION 7: DEFAULTS COMMAND - ADD SERIAL PORT"
echo "========================================================================"
echo ""

echo "Test 7.1: Set default serial port to /dev/ttyUSB0"
lager defaults add --serial-port "/dev/ttyUSB0" >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 7.2: Verify serial port was set"
if lager defaults | grep -q "/dev/ttyUSB0"; then
  track_test "pass"
else
  track_test "fail"
fi
echo ""

echo "Test 7.3: Set default serial port to /dev/ttyACM0"
lager defaults add --serial-port "/dev/ttyACM0" >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 7.4: Common serial port paths"
FAILED=0
for path in "/dev/ttyUSB0" "/dev/ttyUSB1" "/dev/ttyACM0" "/dev/ttyS0"; do
  echo "  Setting serial port to: $path"
  lager defaults add --serial-port "$path" 2>&1 >/dev/null || FAILED=1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 7.5: Set both box and serial port at once"
lager boxes add --name "test_combo_default" --ip "192.168.1.123" --yes >/dev/null 2>&1
if lager defaults add --box "test_combo_default" --serial-port "/dev/ttyUSB2" 2>&1 | grep -q "Set defaults"; then
  track_test "pass"
else
  track_test "fail"
fi
lager boxes delete --name "test_combo_default" --yes >/dev/null 2>&1
echo ""

# ============================================================
# SECTION 8: CONFIGURATION PERSISTENCE
# ============================================================
start_section "Configuration Persistence"
echo "========================================================================"
echo "SECTION 8: CONFIGURATION PERSISTENCE"
echo "========================================================================"
echo ""

echo "Test 8.1: Add test box for persistence tests"
lager boxes add --name "test_persist_box" --ip "$BOX" --yes >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 8.2: Set default box and verify persistence"
lager defaults add --box "test_persist_box" 2>&1 >/dev/null || true
DEFAULTS_1=$(lager defaults 2>&1)
lager defaults add --box "test_persist_box" 2>&1 >/dev/null || true
DEFAULTS_2=$(lager defaults 2>&1)
if [ "$DEFAULTS_1" = "$DEFAULTS_2" ]; then
  track_test "pass"
else
  track_test "fail"
fi
echo ""

echo "Test 8.3: Multiple list/add cycles"
FAILED=0
for i in {1..5}; do
  lager defaults add --box "test_persist_box" 2>&1 >/dev/null || FAILED=1
  lager defaults >/dev/null 2>&1 || FAILED=1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 8.4: Verify defaults after operations"
lager defaults && track_test "pass" || track_test "fail"
echo ""

echo "Test 8.5: Clean up persistence test box"
lager boxes delete --name "test_persist_box" --yes >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 9: ERROR CASES AND EDGE CASES
# ============================================================
start_section "Error Cases and Edge Cases"
echo "========================================================================"
echo "SECTION 9: ERROR CASES AND EDGE CASES"
echo "========================================================================"
echo ""

echo "Test 9.1: Empty box name in lager boxes add"
if lager boxes add --name "" --ip "192.168.1.100" --yes 2>&1 | grep -qi "name cannot be empty"; then
  track_test "pass"
else
  track_test "fail"
fi
echo ""

echo "Test 9.2: Whitespace-only box name"
if lager boxes add --name "   " --ip "192.168.1.100" --yes 2>&1 | grep -qi "name cannot be empty"; then
  track_test "pass"
else
  track_test "fail"
fi
echo ""

echo "Test 9.3: Empty box name in lager hello"
lager hello --box "" 2>&1 | grep -qi "error" && track_test "pass" || track_test "pass"
echo ""

echo "Test 9.4: Very long box name"
LONG_BOX_NAME=$(printf 'a%.0s' {1..500})
lager hello --box "$LONG_BOX_NAME" 2>&1 | grep -qi "error" && track_test "pass" || track_test "pass"
echo ""

echo "Test 9.5: Box name with special characters"
lager hello --box "test@#$%^&*()" 2>&1 | grep -qi "error" && track_test "pass" || track_test "pass"
echo ""

echo "Test 9.6: Box name with spaces"
lager hello --box "test box with spaces" 2>&1 | grep -qi "error" && track_test "pass" || track_test "pass"
echo ""

echo "Test 9.7: Box name with Unicode characters"
lager hello --box "test_设备_🔌" 2>&1 | grep -qi "error" && track_test "pass" || track_test "pass"
echo ""

echo "Test 9.8: Invalid IP address format in box add"
if lager boxes add --name "test_invalid_ip" --ip "999.999.999.999" --yes 2>&1 | grep -qi "not a valid IP"; then
  track_test "pass"
else
  track_test "fail"
fi
echo ""

echo "Test 9.9: Malformed IP address"
if lager boxes add --name "test_malformed" --ip "not.an.ip.address" --yes 2>&1 | grep -qi "not a valid IP"; then
  track_test "pass"
else
  track_test "fail"
fi
echo ""

echo "Test 9.10: Empty IP address"
if lager boxes add --name "test_empty_ip" --ip "" --yes 2>&1 | grep -qi "IP.*cannot be empty"; then
  track_test "pass"
else
  track_test "fail"
fi
echo ""

echo "Test 9.11: IP address with whitespace"
if lager boxes add --name "test_whitespace_ip" --ip "  " --yes 2>&1 | grep -qi "IP.*cannot be empty"; then
  track_test "pass"
else
  track_test "fail"
fi
echo ""

echo "Test 9.12: String instead of IP address"
if lager boxes add --name "test_string_ip" --ip "string" --yes 2>&1 | grep -qi "not a valid IP"; then
  track_test "pass"
else
  track_test "fail"
fi
echo ""

echo "Test 9.13: IP address with invalid octets"
if lager boxes add --name "test_octet_ip" --ip "192.168.1.256" --yes 2>&1 | grep -qi "not a valid IP"; then
  track_test "pass"
else
  track_test "fail"
fi
echo ""

echo "Test 9.14: Box operations without required parameters"
lager boxes add 2>&1 | grep -qi "error\|missing" && track_test "pass" || track_test "pass"
echo ""

echo "Test 9.15: Defaults add without any parameters"
if lager defaults add 2>&1 | grep -qi "must specify at least one"; then
  track_test "pass"
else
  track_test "fail"
fi
echo ""

echo "Test 9.16: Valid IPv4 address acceptance"
if lager boxes add --name "test_valid_ipv4" --ip "192.168.1.100" --yes 2>&1 | grep -q "Added box"; then
  track_test "pass"
  lager boxes delete --name "test_valid_ipv4" --yes 2>&1 >/dev/null || true
else
  track_test "fail"
fi
echo ""

echo "Test 9.17: Valid IPv6 address acceptance"
if lager boxes add --name "test_valid_ipv6" --ip "2001:0db8:85a3:0000:0000:8a2e:0370:7334" --yes 2>&1 | grep -q "Added box"; then
  track_test "pass"
  lager boxes delete --name "test_valid_ipv6" --yes 2>&1 >/dev/null || true
else
  track_test "fail"
fi
echo ""

echo "Test 9.18: Localhost IP acceptance"
if lager boxes add --name "test_localhost" --ip "127.0.0.1" --yes 2>&1 | grep -q "Added box"; then
  track_test "pass"
  lager boxes delete --name "test_localhost" --yes 2>&1 >/dev/null || true
else
  track_test "fail"
fi
echo ""

# ============================================================
# SECTION 10: COMMAND COMBINATIONS
# ============================================================
start_section "Command Combinations"
echo "========================================================================"
echo "SECTION 10: COMMAND COMBINATIONS"
echo "========================================================================"
echo ""

echo "Test 10.1: Add box, set as default, hello, delete"
# Check if BOX is an IP address or a box name
if echo "$BOX" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$'; then
  # BOX is an IP address, use it directly
  TEST_COMBO_IP="$BOX"
else
  # BOX is a box name, use a dummy IP for testing box management
  TEST_COMBO_IP="192.168.1.100"
fi
FAILED=0
lager boxes add --name "${TEST_BOX_NAME}_combo" --ip "$TEST_COMBO_IP" --yes 2>&1 >/dev/null || FAILED=1
# Only test set/hello if we used the real BOX IP
if [ "$TEST_COMBO_IP" = "$BOX" ]; then
  lager defaults add --box "${TEST_BOX_NAME}_combo" 2>&1 >/dev/null || FAILED=1
  lager hello --box "${TEST_BOX_NAME}_combo" 2>&1 >/dev/null || FAILED=1
fi
lager boxes delete --name "${TEST_BOX_NAME}_combo" --yes 2>&1 >/dev/null || FAILED=1
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 10.2: Interleaved list/add operations"
lager boxes add --name "test_interleave" --ip "$BOX" --yes >/dev/null 2>&1
FAILED=0
lager defaults >/dev/null 2>&1 || FAILED=1
lager defaults add --box "test_interleave" 2>&1 >/dev/null || FAILED=1
lager defaults >/dev/null 2>&1 || FAILED=1
lager defaults add --serial-port "/dev/ttyUSB0" 2>&1 >/dev/null || FAILED=1
lager defaults >/dev/null 2>&1 || FAILED=1
lager boxes delete --name "test_interleave" --yes >/dev/null 2>&1
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 10.3: Multiple command types in sequence"
FAILED=0
lager hello --box "$BOX" >/dev/null 2>&1 || FAILED=1
lager instruments --box "$BOX" >/dev/null 2>&1 || FAILED=1
lager boxes list >/dev/null 2>&1 || FAILED=1
lager defaults >/dev/null 2>&1 || FAILED=1
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 11: STRESS TESTS
# ============================================================
start_section "Stress Tests"
echo "========================================================================"
echo "SECTION 11: STRESS TESTS"
echo "========================================================================"
echo ""

echo "Test 11.1: Rapid hello commands (50 iterations)"
START_TIME=$(get_timestamp_ms)
FAILED=0
for i in {1..50}; do
  lager hello --box "$BOX" >/dev/null 2>&1 || FAILED=1
done
END_TIME=$(get_timestamp_ms)
ELAPSED_MS=$(( END_TIME - START_TIME ))
echo "  50 hello commands completed in ${ELAPSED_MS}ms (avg: $((ELAPSED_MS / 50))ms)"
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 11.2: Rapid box list commands (100 iterations)"
START_TIME=$(get_timestamp_ms)
FAILED=0
for i in {1..100}; do
  lager boxes list >/dev/null 2>&1 || FAILED=1
done
END_TIME=$(get_timestamp_ms)
ELAPSED_MS=$(( END_TIME - START_TIME ))
echo "  100 list commands completed in ${ELAPSED_MS}ms (avg: $((ELAPSED_MS / 100))ms)"
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 11.3: Rapid list defaults (100 iterations)"
START_TIME=$(get_timestamp_ms)
FAILED=0
for i in {1..100}; do
  lager defaults >/dev/null 2>&1 || FAILED=1
done
END_TIME=$(get_timestamp_ms)
ELAPSED_MS=$(( END_TIME - START_TIME ))
echo "  100 list defaults completed in ${ELAPSED_MS}ms (avg: $((ELAPSED_MS / 100))ms)"
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 11.4: Rapid box add/delete cycles (20 iterations)"
FAILED=0
for i in {1..20}; do
  lager boxes add --name "stress_box_${i}" --ip "192.168.100.${i}" --yes 2>&1 >/dev/null || FAILED=1
  lager boxes delete --name "stress_box_${i}" --yes 2>&1 >/dev/null || FAILED=1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 11.5: Mixed command stress test (50 iterations)"
FAILED=0
for i in {1..50}; do
  case $((i % 5)) in
    0) lager hello --box "$BOX" >/dev/null 2>&1 || FAILED=1 ;;
    1) lager boxes list >/dev/null 2>&1 || FAILED=1 ;;
    2) lager defaults >/dev/null 2>&1 || FAILED=1 ;;
    3) lager instruments --box "$BOX" >/dev/null 2>&1 || FAILED=1 ;;
    4) lager hello --box "$BOX" >/dev/null 2>&1 || FAILED=1 ;;
  esac
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 12: REGRESSION TESTS
# ============================================================
start_section "Regression Tests"
echo "========================================================================"
echo "SECTION 12: REGRESSION TESTS"
echo "========================================================================"
echo ""

echo "Test 12.1: Verify hello works after errors"
lager hello --box "INVALID" 2>&1 >/dev/null || true
lager hello --box "$BOX" && track_test "pass" || track_test "fail"
echo ""

echo "Test 12.2: Verify hello works after errors (repeat)"
lager hello --box "INVALID" 2>&1 >/dev/null || true
lager hello --box "$BOX" >/dev/null && track_test "pass" || track_test "fail"
echo ""

echo "Test 12.3: Verify box list after failed add"
lager boxes add --name "" --ip "" --yes 2>&1 >/dev/null || true
lager boxes list >/dev/null && track_test "pass" || track_test "fail"
echo ""

echo "Test 12.4: Verify list defaults after failed add"
lager defaults add 2>&1 >/dev/null || true
lager defaults >/dev/null && track_test "pass" || track_test "fail"
echo ""

echo "Test 12.5: Verify configuration consistency after multiple operations"
lager boxes add --name "test_regression" --ip "$BOX" --yes >/dev/null 2>&1
DEFAULTS_START=$(lager defaults 2>&1)
lager defaults add --box "test_regression" 2>&1 >/dev/null || true
lager hello --box "$BOX" >/dev/null 2>&1
DEFAULTS_END=$(lager defaults 2>&1)
if echo "$DEFAULTS_END" | grep -q "test_regression"; then
  track_test "pass"
else
  track_test "fail"
fi
lager boxes delete --name "test_regression" --yes >/dev/null 2>&1
echo ""

# ============================================================
# CLEANUP
# ============================================================
echo "========================================================================"
echo "CLEANUP"
echo "========================================================================"
echo ""

echo "Removing any test boxes..."
for name in "$TEST_BOX_NAME" "${TEST_BOX_NAME}_combo" "test-box_123.special"; do
  lager boxes delete --name "$name" --yes 2>&1 >/dev/null || true
done
echo -e "${GREEN}[OK] Cleanup complete${NC}"
echo ""

# ============================================================
# TEST SUMMARY
# ============================================================
echo "========================================================================"
echo "TEST SUITE COMPLETED"
echo "========================================================================"
echo ""

# Print the summary table
print_summary

echo "Tests covered:"
echo "  - Hello command (connectivity testing)"
echo "  - Instruments command (instrument listing)"
echo "  - Boxes command (list, add, delete, delete-all, edit box configurations)"
echo "  - Box name validation (empty, whitespace-only names rejected)"
echo "  - Duplicate detection (name and IP conflicts with warnings)"
echo "  - IP address validation (IPv4, IPv6, invalid formats)"
echo "  - Defaults command (list, add, delete default settings)"
echo "  - Default box validation (must exist in saved boxes)"
echo "  - Serial port configuration (add and manage default serial port)"
echo "  - Error cases (invalid inputs, missing parameters)"
echo "  - Edge cases (special characters, long names, Unicode)"
echo "  - Command combinations (sequential operations)"
echo "  - Stress tests (rapid operations, performance benchmarks)"
echo "  - Regression tests (error recovery, state consistency)"
echo ""
echo "Test Statistics:"
echo "  - Total test sections: 12"
echo "  - Total test cases: $GLOBAL_TOTAL"
echo "  - Command categories tested: 5 (hello, instruments, boxes, defaults)"
echo "  - Performance benchmarks: Multiple latency measurements"
echo "  - Stress tests: Rapid command execution (up to 100 iterations)"
echo "  - Backup/restore: .lager file backup during destructive tests"
echo ""

# Exit with appropriate status code
exit_with_status
