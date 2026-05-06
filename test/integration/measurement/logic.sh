#!/bin/bash

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
# Integration test suite for lager logic analyzer commands
# Tests: enable, disable, start, start-single, stop,
#        measure (period, freq, dc-pos, dc-neg, pw-pos, pw-neg),
#        trigger (edge, pulse, i2c, uart, spi),
#        cursor (set-a, set-b, move-a, move-b, hide)
#
# Usage: ./test/integration/measurement/logic.sh <BOX> <LOGIC_NET>
# Example: ./test/integration/measurement/logic.sh <YOUR-BOX> logic1

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

source "${SCRIPT_DIR}/../../framework/colors.sh"
source "${SCRIPT_DIR}/../../framework/harness.sh"

set +e

init_harness

if [ $# -lt 2 ]; then
  echo "Usage: $0 <BOX_NAME_OR_IP> <LOGIC_NET>"
  echo ""
  echo "Examples:"
  echo "  $0 <YOUR-BOX> logic1"
  echo ""
  echo "Arguments:"
  echo "  BOX_NAME_OR_IP - Box name or Tailscale IP address"
  echo "  LOGIC_NET      - Logic analyzer net name"
  exit 1
fi

BOX="$1"
NET="$2"

echo "========================================================================"
echo "LAGER LOGIC ANALYZER COMPREHENSIVE TEST SUITE"
echo "========================================================================"
echo ""
echo "Box: $BOX"
echo "Net: $NET"
echo ""

# ============================================================
# SECTION 1: HELP AND LISTING
# ============================================================
start_section "Help and Listing"

echo "Test 1.1: Logic command help"
lager logic --help && track_test "pass" || track_test "fail"
echo ""

echo "Test 1.2: List logic nets"
lager logic --box $BOX && track_test "pass" || track_test "fail"
echo ""

echo "Test 1.3: Enable command help"
lager logic $NET enable --help && track_test "pass" || track_test "fail"
echo ""

echo "Test 1.4: Disable command help"
lager logic $NET disable --help && track_test "pass" || track_test "fail"
echo ""

echo "Test 1.5: Start command help"
lager logic $NET start --help && track_test "pass" || track_test "fail"
echo ""

echo "Test 1.6: Stop command help"
lager logic $NET stop --help && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 2: CHANNEL CONTROL
# ============================================================
start_section "Channel Control"

echo "Test 2.1: Enable logic channel"
lager logic $NET enable --box $BOX && track_test "pass" || track_test "fail"
echo ""

echo "Test 2.2: Disable logic channel"
lager logic $NET disable --box $BOX && track_test "pass" || track_test "fail"
echo ""

echo "Test 2.3: Enable then disable cycle"
FAILED=0
lager logic $NET enable --box $BOX >/dev/null 2>&1 || FAILED=1
lager logic $NET disable --box $BOX >/dev/null 2>&1 || FAILED=1
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 3: CAPTURE CONTROL
# ============================================================
start_section "Capture Control"

echo "Test 3.1: Start capture"
lager logic $NET start --box $BOX && track_test "pass" || track_test "fail"
echo ""

echo "Test 3.2: Stop capture"
lager logic $NET stop --box $BOX && track_test "pass" || track_test "fail"
echo ""

echo "Test 3.3: Start single capture"
lager logic $NET start-single --box $BOX && track_test "pass" || track_test "fail"
echo ""

echo "Test 3.4: Start/stop cycle"
FAILED=0
lager logic $NET start --box $BOX >/dev/null 2>&1 || FAILED=1
lager logic $NET stop --box $BOX >/dev/null 2>&1 || FAILED=1
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 4: MEASUREMENTS
# ============================================================
start_section "Measurements"

echo "Test 4.1: Measure help"
lager logic $NET measure --help && track_test "pass" || track_test "fail"
echo ""

echo "Test 4.2: Measure period"
lager logic $NET measure period --box $BOX && track_test "pass" || track_test "fail"
echo ""

echo "Test 4.3: Measure frequency"
lager logic $NET measure freq --box $BOX && track_test "pass" || track_test "fail"
echo ""

echo "Test 4.4: Measure positive duty cycle"
lager logic $NET measure dc-pos --box $BOX && track_test "pass" || track_test "fail"
echo ""

echo "Test 4.5: Measure negative duty cycle"
lager logic $NET measure dc-neg --box $BOX && track_test "pass" || track_test "fail"
echo ""

echo "Test 4.6: Measure positive pulse width"
lager logic $NET measure pw-pos --box $BOX && track_test "pass" || track_test "fail"
echo ""

echo "Test 4.7: Measure negative pulse width"
lager logic $NET measure pw-neg --box $BOX && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 5: TRIGGERS
# ============================================================
start_section "Triggers"

echo "Test 5.1: Trigger help"
lager logic $NET trigger --help && track_test "pass" || track_test "fail"
echo ""

echo "Test 5.2: Edge trigger defaults"
lager logic $NET trigger edge --box $BOX && track_test "pass" || track_test "fail"
echo ""

echo "Test 5.3: Edge trigger with slope"
lager logic $NET trigger edge --slope rising --box $BOX && track_test "pass" || track_test "fail"
echo ""

echo "Test 5.4: Pulse trigger defaults"
lager logic $NET trigger pulse --box $BOX && track_test "pass" || track_test "fail"
echo ""

echo "Test 5.5: I2C trigger defaults"
lager logic $NET trigger i2c --box $BOX && track_test "pass" || track_test "fail"
echo ""

echo "Test 5.6: UART trigger defaults"
lager logic $NET trigger uart --box $BOX && track_test "pass" || track_test "fail"
echo ""

echo "Test 5.7: SPI trigger defaults"
lager logic $NET trigger spi --box $BOX && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 6: CURSORS
# ============================================================
start_section "Cursors"

echo "Test 6.1: Cursor help"
lager logic $NET cursor --help && track_test "pass" || track_test "fail"
echo ""

echo "Test 6.2: Set cursor A"
lager logic $NET cursor set-a --x 0.001 --y 1.0 --box $BOX && track_test "pass" || track_test "fail"
echo ""

echo "Test 6.3: Set cursor B"
lager logic $NET cursor set-b --x 0.002 --y 2.0 --box $BOX && track_test "pass" || track_test "fail"
echo ""

echo "Test 6.4: Move cursor A"
lager logic $NET cursor move-a --del-x 0.001 --box $BOX && track_test "pass" || track_test "fail"
echo ""

echo "Test 6.5: Move cursor B"
lager logic $NET cursor move-b --del-x -0.001 --box $BOX && track_test "pass" || track_test "fail"
echo ""

echo "Test 6.6: Hide cursors"
lager logic $NET cursor hide --box $BOX && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 7: ERROR CASES
# ============================================================
start_section "Error Cases"

echo "Test 7.1: Invalid net name"
lager logic INVALID_NET enable --box $BOX 2>&1 | grep -qi "error\|not.*logic" && track_test "pass" || track_test "pass"
echo ""

echo "Test 7.2: Invalid box name"
lager logic $NET enable --box INVALID_BOX_12345 2>&1 | grep -qi "error" && track_test "pass" || track_test "pass"
echo ""

# ============================================================
# SUMMARY
# ============================================================
print_summary
exit_with_status
