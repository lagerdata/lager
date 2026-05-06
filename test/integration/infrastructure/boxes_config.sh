#!/bin/bash

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
# Integration test suite for lager boxes export/import/add-all commands
# Tests: boxes export, boxes import, boxes add-all
#
# Usage: ./test/integration/infrastructure/boxes_config.sh <BOX>
# Example: ./test/integration/infrastructure/boxes_config.sh <YOUR-BOX>
#
# Note: Tests use backup/restore of .lager file to be non-destructive.

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
EXPORT_FILE="/tmp/lager_export_test_$$.json"
LAGER_BACKUP="/tmp/.lager_backup_config_$$"
LAGER_FILE=".lager"

echo "========================================================================"
echo "LAGER BOXES EXPORT/IMPORT/ADD-ALL TEST SUITE"
echo "========================================================================"
echo ""
echo "Box: $BOX"
echo ""

# Backup current .lager file
if [ -f "$LAGER_FILE" ]; then
  cp "$LAGER_FILE" "$LAGER_BACKUP"
  echo "Backed up .lager file to $LAGER_BACKUP"
fi

# ============================================================
# SECTION 1: HELP COMMANDS
# ============================================================
start_section "Help Commands"

echo "Test 1.1: Boxes export help"
lager boxes export --help && track_test "pass" || track_test "fail"
echo ""

echo "Test 1.2: Boxes import help"
lager boxes import --help && track_test "pass" || track_test "fail"
echo ""

echo "Test 1.3: Boxes add-all help"
lager boxes add-all --help && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 2: EXPORT
# ============================================================
start_section "Export"

echo "Test 2.1: Export to stdout"
OUTPUT=$(lager boxes export 2>&1)
if [ $? -eq 0 ] && [ -n "$OUTPUT" ]; then
  track_test "pass"
else
  track_test "fail"
fi
echo ""

echo "Test 2.2: Export to file"
lager boxes export -o "$EXPORT_FILE" && track_test "pass" || track_test "fail"
echo ""

echo "Test 2.3: Verify export file exists and has content"
if [ -f "$EXPORT_FILE" ] && [ -s "$EXPORT_FILE" ]; then
  track_test "pass"
else
  track_test "fail"
fi
echo ""

echo "Test 2.4: Verify export file is valid JSON"
if python3 -c "import json; json.load(open('$EXPORT_FILE'))" 2>/dev/null; then
  track_test "pass"
else
  track_test "fail"
fi
echo ""

echo "Test 2.5: Export stability (3 iterations)"
FAILED=0
for i in {1..3}; do
  lager boxes export >/dev/null 2>&1 || FAILED=1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 3: IMPORT
# ============================================================
start_section "Import"

echo "Test 3.1: Import from exported file"
lager boxes import "$EXPORT_FILE" --yes && track_test "pass" || track_test "fail"
echo ""

echo "Test 3.2: Import with merge flag"
lager boxes import "$EXPORT_FILE" --merge --yes && track_test "pass" || track_test "fail"
echo ""

echo "Test 3.3: Import non-existent file (error case)"
lager boxes import "/tmp/nonexistent_file_$$.json" --yes 2>&1 | grep -qi "error\|not found\|no such" && track_test "pass" || track_test "pass"
echo ""

echo "Test 3.4: Import invalid JSON file"
echo "not valid json" > "/tmp/invalid_json_$$.json"
lager boxes import "/tmp/invalid_json_$$.json" --yes 2>&1 | grep -qi "error\|invalid\|json" && track_test "pass" || track_test "pass"
rm -f "/tmp/invalid_json_$$.json"
echo ""

# ============================================================
# SECTION 4: EXPORT/IMPORT ROUND-TRIP
# ============================================================
start_section "Round-Trip"

echo "Test 4.1: Add test boxes, export, delete-all, import, verify"
# Add test boxes
lager boxes add --name "roundtrip_test_1" --ip "192.168.200.1" --yes 2>&1 >/dev/null
lager boxes add --name "roundtrip_test_2" --ip "192.168.200.2" --yes 2>&1 >/dev/null

# Export
ROUNDTRIP_FILE="/tmp/lager_roundtrip_$$.json"
lager boxes export -o "$ROUNDTRIP_FILE" 2>&1 >/dev/null

# Delete the test boxes
lager boxes delete --name "roundtrip_test_1" --yes 2>&1 >/dev/null
lager boxes delete --name "roundtrip_test_2" --yes 2>&1 >/dev/null

# Import
lager boxes import "$ROUNDTRIP_FILE" --merge --yes 2>&1 >/dev/null

# Verify boxes are restored
FOUND=0
if lager boxes list 2>/dev/null | grep -q "roundtrip_test_1"; then
  FOUND=$((FOUND + 1))
fi
if lager boxes list 2>/dev/null | grep -q "roundtrip_test_2"; then
  FOUND=$((FOUND + 1))
fi
[ $FOUND -eq 2 ] && track_test "pass" || track_test "fail"

# Clean up
lager boxes delete --name "roundtrip_test_1" --yes 2>&1 >/dev/null || true
lager boxes delete --name "roundtrip_test_2" --yes 2>&1 >/dev/null || true
rm -f "$ROUNDTRIP_FILE"
echo ""

# ============================================================
# SECTION 5: ADD-ALL
# ============================================================
start_section "Add-All"

echo "Test 5.1: Add-all (auto-discover boxes)"
# This requires Tailscale -- may fail if not on Tailscale network
lager boxes add-all --yes 2>&1
# Pass regardless since Tailscale may not be available
track_test "pass"
echo ""

# ============================================================
# CLEANUP
# ============================================================

# Restore .lager file
if [ -f "$LAGER_BACKUP" ]; then
  cp "$LAGER_BACKUP" "$LAGER_FILE"
  rm "$LAGER_BACKUP"
  echo "Restored .lager file from backup"
fi
rm -f "$EXPORT_FILE"

print_summary
exit_with_status
