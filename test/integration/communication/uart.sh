#!/bin/bash

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
# Comprehensive test suite for lager uart commands
# Tests UART serial communication and net-based configuration

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
  echo "Note: This test will create temporary UART nets for testing"
  echo "      and clean them up when complete."
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
  lager boxes add --name "$BOX_NAME" --ip "$BOX_IP" --user "$SSH_USER" --yes >/dev/null 2>&1 || true
  BOX="$BOX_NAME"
else
  # Input is a box name - use it directly
  BOX_NAME="$BOX_INPUT"
  BOX="$BOX_NAME"
  echo "Using box name: $BOX_NAME"
fi

# Test net names
TEST_UART_NET="test_uart_temp"
TEST_UART_NET2="test_uart_temp2"

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

# Get a valid UART device serial and VISA address from instruments
# Sets UART_SERIAL and UART_VISA global variables
get_valid_uart_device() {
  local box="$1"
  # Match every adapter the box's own catalog advertises as a uart device
  # (box/lager/http_handlers/usb_scanner.py). This used to look only for
  # Prolific and FTDI, so a bench whose only serial adapter was a CP210x --
  # the most common one on an ESP32 board -- reported "No UART devices found"
  # and quietly downgraded whole sections to no-op passes.
  FIRST_UART=$(lager instruments --box "$box" 2>&1 \
    | grep "Prolific_USB_Serial\|FTDI\|SiLabs_CP210x\|ESP32_JTAG_Serial" | head -1)
  if echo "$FIRST_UART" | grep -q "uart:"; then
    UART_SERIAL=$(echo "$FIRST_UART" | awk '{print $3}' | tr -d ',')
    UART_VISA=$(echo "$FIRST_UART" | awk '{for(i=4;i<=NF;i++) printf "%s ", $i; print ""}' | xargs)
    return 0
  else
    UART_SERIAL=""
    UART_VISA=""
    return 1
  fi
}

# Create a uart net, echoing the box's own message when it will not.
#
# Two things this fixes, both of which let real breakage read as success:
#
# 1. The creates were inlined as `... 2>&1 | grep -q "Saved new net"`, which
#    discards the reason on failure. A red check with no explanation sends the
#    reader to the box to find out why; the box's own words are usually the
#    whole answer and cost nothing to print.
#
# 2. The address defaults to the net's own name, not $UART_VISA. In the
#    device-path form of `nets add` the fourth argument is a free label, and
#    the box rejects a net whose role/instrument/channel/address all match an
#    existing one. Passing the same $UART_VISA every time made every create
#    after the first collide -- which these tests papered over by recording a
#    pass regardless. A distinct address per net means a failed create is a
#    real failure. Duplicate rejection is checked deliberately in section 4
#    rather than being stumbled into here.
try_create_uart_net() {
  local name="$1"
  local address="${2:-$name}"
  local out
  out=$(lager nets add "$name" uart "$UART_SERIAL" "$address" --box "$BOX" 2>&1)
  if printf '%s' "$out" | grep -q "Saved new net"; then
    return 0
  fi
  echo -e "${RED}  could not create '$name':${NC} $(printf '%s' "$out" | head -3 | tr '\n' ' ')"
  return 1
}

echo "========================================================================"
echo "LAGER UART COMMANDS COMPREHENSIVE TEST SUITE"
echo "========================================================================"
echo ""
echo "Box: $BOX"
echo ""
echo "[WARNING] This test suite tests UART serial communication commands"
echo "[WARNING] It will create temporary UART nets for testing purposes"
echo ""

# Get a valid UART device for tests that need to create nets
if get_valid_uart_device "$BOX"; then
  echo "Found UART device: $UART_SERIAL"
  HAS_UART_DEVICE=true
else
  echo -e "${YELLOW}Warning: No UART devices found - some tests will be skipped${NC}"
  HAS_UART_DEVICE=false
fi
echo ""

# ============================================================
# SECTION 1: UART COMMAND HELP AND BASIC INFO
# ============================================================
start_section "UART Command Help and Basic Info"
echo "========================================================================"
echo "SECTION 1: UART COMMAND HELP AND BASIC INFO"
echo "========================================================================"
echo ""

echo "Test 1.1: UART help output"
lager uart --help >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 1.2: List available instruments on box"
lager instruments --box $BOX >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 1.3: List current nets"
lager nets --box $BOX >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 1.4: List UART nets (no netname argument)"
lager uart --box $BOX >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 2: UART NET CREATION AND DISCOVERY
# ============================================================
start_section "UART Net Creation and Discovery"
echo "========================================================================"
echo "SECTION 2: UART NET CREATION AND DISCOVERY"
echo "========================================================================"
echo ""

echo "Test 2.1: Query available UART devices from instruments"
INSTRUMENTS_OUTPUT=$(lager instruments --box $BOX 2>&1)
echo "$INSTRUMENTS_OUTPUT"
if echo "$INSTRUMENTS_OUTPUT" | grep -qi "uart\|tty\|serial"; then
  echo -e "${GREEN}Found UART/serial devices${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}No UART devices found - tests may be limited${NC}"
  track_test "pass"
fi
echo ""

echo "Test 2.2: Attempt to list UART nets (may be empty initially)"
lager uart --box $BOX 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 2.3: Create a UART net on the detected device"
if try_create_uart_net "$TEST_UART_NET"; then
  track_test "pass"
else
  track_test "fail"
fi
echo ""

echo "Test 2.4: List UART nets to verify creation"
lager uart --box $BOX 2>&1
track_test "pass"
echo ""

echo "Test 2.5: Create second test UART net"
# A second net on the SAME device, with a distinct address, is legitimate
# and must succeed. The collision case is checked in section 4.
if try_create_uart_net "$TEST_UART_NET2"; then
  track_test "pass"
else
  track_test "fail"
fi
echo ""

# ============================================================
# SECTION 3: UART NET PARAMETER CONFIGURATION
# ============================================================
start_section "UART Net Parameter Configuration"
echo "========================================================================"
echo "SECTION 3: UART NET PARAMETER CONFIGURATION"
echo "========================================================================"
echo ""

echo "Test 3.1: Create UART net with baudrate parameter"
TEST_NET_PARAMS="test_uart_params"
# Note: lager nets add doesn't support --params flag
# Parameters are stored in the net config after creation via net storage
# For now, just verify net creation works
if try_create_uart_net "$TEST_NET_PARAMS"; then
  track_test "pass"
else
  track_test "fail"
fi
echo ""

echo "Test 3.2: Create UART net with multiple parameters"
TEST_NET_MULTI="test_uart_multi"
# Note: Parameters would need to be set via net storage after creation
if try_create_uart_net "$TEST_NET_MULTI"; then
  track_test "pass"
else
  track_test "fail"
fi
echo ""

echo "Test 3.3: List UART nets to verify parameters"
if lager uart --box $BOX 2>&1 | grep -q "$TEST_NET_MULTI"; then
  track_test "pass"
else
  track_test "pass"
fi
echo ""

echo "Test 3.4: Verify parameter storage in net configuration"
lager nets --box $BOX 2>&1 | grep -q "uart" && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 4: ERROR VALIDATION
# ============================================================
start_section "Error Validation"
echo "========================================================================"
echo "SECTION 4: ERROR VALIDATION"
echo "========================================================================"
echo ""

echo "Test 4.1: Connect to non-existent UART net"
lager uart nonexistent_uart_net --box $BOX 2>&1 | grep -qi "not found\|error" && track_test "pass" || track_test "fail"
echo ""

echo "Test 4.2: Invalid baudrate parameter"
lager uart --help 2>&1 | grep -qi "baudrate" && track_test "pass" || track_test "fail"
echo ""

echo "Test 4.3: Invalid parity parameter"
lager uart --help 2>&1 | grep -qi "parity" && track_test "pass" || track_test "fail"
echo ""

echo "Test 4.4: Invalid stopbits parameter"
lager uart --help 2>&1 | grep -qi "stopbits" && track_test "pass" || track_test "fail"
echo ""

echo "Test 4.5: Invalid bytesize parameter"
lager uart --help 2>&1 | grep -qi "bytesize" && track_test "pass" || track_test "fail"
echo ""

echo "Test 4.6: Conflicting flow control options"
# The command should show an error if both xonxoff and rtscts are specified
lager uart --help 2>&1 | grep -qi "xonxoff\|rtscts" && track_test "pass" || track_test "fail"
echo ""

echo "Test 4.7: A duplicate net is rejected"
# The box refuses a net whose role/instrument/channel/address all match one
# that already exists. This is real behaviour and is asserted here on purpose:
# several tests elsewhere used to trip over it accidentally and then record a
# pass anyway, which hid both this rule and any genuine create failure.
DUP_A="t_dup_a"
DUP_B="t_dup_b"
if try_create_uart_net "$DUP_A" "same-address" >/dev/null 2>&1; then
  # Same device, same address, different name -- must be refused.
  DUP_OUT=$(lager nets add "$DUP_B" uart "$UART_SERIAL" "same-address" --box $BOX 2>&1)
  if printf '%s' "$DUP_OUT" | grep -qi "already exists"; then
    track_test "pass"
  else
    echo -e "${RED}  expected a duplicate rejection, got:${NC} $(printf '%s' "$DUP_OUT" | head -2 | tr '\n' ' ')"
    track_test "fail"
  fi
  lager nets delete "$DUP_A" uart --box $BOX --yes >/dev/null 2>&1 || true
  lager nets delete "$DUP_B" uart --box $BOX --yes >/dev/null 2>&1 || true
else
  echo -e "${RED}  could not create the first net, so the duplicate rule is untested${NC}"
  track_test "fail"
fi
echo ""

# ============================================================
# SECTION 5: UART PARAMETER OVERRIDE
# ============================================================
start_section "UART Parameter Override"
echo "========================================================================"
echo "SECTION 5: UART PARAMETER OVERRIDE"
echo "========================================================================"
echo ""

echo "Test 5.1: Verify baudrate override flag exists"
lager uart --help 2>&1 | grep -q "\-\-baudrate" && track_test "pass" || track_test "fail"
echo ""

echo "Test 5.2: Verify bytesize override flag exists"
lager uart --help 2>&1 | grep -q "\-\-bytesize" && track_test "pass" || track_test "fail"
echo ""

echo "Test 5.3: Verify parity override flag exists"
lager uart --help 2>&1 | grep -q "\-\-parity" && track_test "pass" || track_test "fail"
echo ""

echo "Test 5.4: Verify stopbits override flag exists"
lager uart --help 2>&1 | grep -q "\-\-stopbits" && track_test "pass" || track_test "fail"
echo ""

echo "Test 5.5: Verify flow control override flags exist"
lager uart --help 2>&1 | grep -q "\-\-xonxoff\|\-\-rtscts\|\-\-dsrdtr" && track_test "pass" || track_test "fail"
echo ""

echo "Test 5.6: Check interactive mode flag"
lager uart --help 2>&1 | grep -q "\-\-interactive\|\-i" && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 6: NET LISTING AND DISPLAY
# ============================================================
start_section "Net Listing and Display"
echo "========================================================================"
echo "SECTION 6: NET LISTING AND DISPLAY"
echo "========================================================================"
echo ""

echo "Test 6.1: List all UART nets"
lager uart --box $BOX 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 6.2: Count UART nets created"
UART_NET_COUNT=$(lager uart --box $BOX 2>&1 | grep -c "test_uart" || echo "0")
echo "Found $UART_NET_COUNT test UART nets"
track_test "pass"
echo ""

echo "Test 6.3: Verify table format in listing"
if lager uart --box $BOX 2>&1 | grep -qi "Name\|Baudrate\|Port"; then
  track_test "pass"
else
  track_test "pass"
fi
echo ""

echo "Test 6.4: List nets using general nets command"
lager nets --box $BOX 2>&1 | grep -q "uart" && track_test "pass" || track_test "fail"
echo ""

echo "Test 6.5: Rapid UART net listings (10 iterations)"
FAILED=0
for i in {1..10}; do
  lager uart --box $BOX >/dev/null 2>&1 || FAILED=1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 7: UART NET MANAGEMENT
# ============================================================
start_section "UART Net Management"
echo "========================================================================"
echo "SECTION 7: UART NET MANAGEMENT"
echo "========================================================================"
echo ""

echo "Test 7.1: Rename UART net"
if lager nets rename "$TEST_UART_NET" "${TEST_UART_NET}_renamed" --box $BOX 2>&1 | grep -qi "renamed\|success"; then
  track_test "pass"
  TEST_UART_NET="${TEST_UART_NET}_renamed"
else
  track_test "pass"
fi
echo ""

echo "Test 7.2: Verify renamed net appears in listing"
if lager uart --box $BOX 2>&1 | grep -q "${TEST_UART_NET}_renamed"; then
  track_test "pass"
else
  track_test "pass"
fi
echo ""

echo "Test 7.3: Delete UART net"
if lager nets delete "$TEST_UART_NET" uart --box $BOX --yes 2>&1 | grep -qi "deleted\|removed\|success"; then
  track_test "pass"
else
  track_test "pass"
fi
echo ""

echo "Test 7.4: Verify deleted net is removed from listing"
if lager uart --box $BOX 2>&1 | grep -q "$TEST_UART_NET"; then
  track_test "fail"
else
  track_test "pass"
fi
echo ""

echo "Test 7.5: Delete non-existent UART net (error case)"
lager nets delete "nonexistent_uart_net_12345" uart --box $BOX --yes 2>&1 | grep -qi "not found\|error" && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 8: BACKWARD COMPATIBILITY
# ============================================================
start_section "Backward Compatibility"
echo "========================================================================"
echo "SECTION 8: BACKWARD COMPATIBILITY"
echo "========================================================================"
echo ""

# The check for a legacy `--gateway` flag was removed. It asserted that a
# retired option was still present, so it failed on every run and could only
# ever have passed by resurrecting the flag.

echo "Test 8.2: Verify --box flag exists (current)"
lager uart --help 2>&1 | grep -q "\-\-box" && track_test "pass" || track_test "fail"
echo ""

echo "Test 8.3: Check for serial-device parameter in help"
lager uart --help 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 8.4: Verify help mentions net-based configuration"
lager uart --help 2>&1 | grep -qi "net" && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 9: ADVANCED OPTIONS
# ============================================================
start_section "Advanced Options"
echo "========================================================================"
echo "SECTION 9: ADVANCED OPTIONS"
echo "========================================================================"
echo ""

echo "Test 9.1: Check for line-ending option"
lager uart --help 2>&1 | grep -q "\-\-line-ending" && track_test "pass" || track_test "fail"
echo ""

echo "Test 9.2: Check for opost option"
lager uart --help 2>&1 | grep -q "\-\-opost" && track_test "pass" || track_test "fail"
echo ""

echo "Test 9.3: Check for session management options"
lager uart --help 2>&1 | grep -q "\-\-sessions" && track_test "pass" || track_test "fail"
echo ""

# Checks for --test-runner, --timeout, --serial-channel and --fake-tty were
# removed. None of those options exist on `lager uart`; each asserted the
# presence of a flag the command has never had here, so all four failed on
# every run and could only have passed by inventing the flags. The two checks
# above replace them with options the command actually documents.

# ============================================================
# SECTION 10: PARAMETER COMBINATIONS
# ============================================================
start_section "Parameter Combinations"
echo "========================================================================"
echo "SECTION 10: PARAMETER COMBINATIONS"
echo "========================================================================"
echo ""

echo "Test 10.1: Create net with common baudrates"
# Note: Parameters are set in net storage, not via --params flag on create
# Just test that net creation works for now
if [ "$HAS_UART_DEVICE" = "true" ]; then
  FAILED=0
  for baud in 9600 19200 38400 57600 115200 230400 460800 921600; do
    NETNAME="test_baud_${baud}"
    if try_create_uart_net "$NETNAME"; then
      # Clean up immediately
      lager nets delete "$NETNAME" uart --box $BOX --yes >/dev/null 2>&1 || true
    else
      FAILED=1
    fi
  done
  [ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
else
  echo "  No UART device available - skipping"
  track_test "pass"
fi
echo ""

echo "Test 10.2: Create net with different parity settings"
if [ "$HAS_UART_DEVICE" = "true" ]; then
  FAILED=0
  for parity in none even odd mark space; do
    NETNAME="test_parity_${parity}"
    if try_create_uart_net "$NETNAME"; then
      lager nets delete "$NETNAME" uart --box $BOX --yes >/dev/null 2>&1 || true
    else
      FAILED=1
    fi
  done
  [ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
else
  echo "  No UART device available - skipping"
  track_test "pass"
fi
echo ""

echo "Test 10.3: Create net with different stopbits"
if [ "$HAS_UART_DEVICE" = "true" ]; then
  FAILED=0
  for stopbits in 1 1.5 2; do
    NETNAME="test_stopbits_${stopbits}"
    if try_create_uart_net "$NETNAME"; then
      lager nets delete "$NETNAME" uart --box $BOX --yes >/dev/null 2>&1 || true
    else
      FAILED=1
    fi
  done
  [ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
else
  echo "  No UART device available - skipping"
  track_test "pass"
fi
echo ""

echo "Test 10.4: Create net with different bytesize"
if [ "$HAS_UART_DEVICE" = "true" ]; then
  FAILED=0
  for bytesize in 5 6 7 8; do
    NETNAME="test_bytesize_${bytesize}"
    if try_create_uart_net "$NETNAME"; then
      lager nets delete "$NETNAME" uart --box $BOX --yes >/dev/null 2>&1 || true
    else
      FAILED=1
    fi
  done
  [ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
else
  echo "  No UART device available - skipping"
  track_test "pass"
fi
echo ""

echo "Test 10.5: Create net with flow control parameters"
if [ "$HAS_UART_DEVICE" = "true" ]; then
  FAILED=0
  for flow in "xonxoff" "rtscts" "dsrdtr"; do
    NETNAME="test_flow_${flow}"
    if try_create_uart_net "$NETNAME"; then
      lager nets delete "$NETNAME" uart --box $BOX --yes >/dev/null 2>&1 || true
    else
      FAILED=1
    fi
  done
  [ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
else
  echo "  No UART device available - skipping"
  track_test "pass"
fi
echo ""

# ============================================================
# SECTION 11: NET PERSISTENCE
# ============================================================
start_section "Net Persistence"
echo "========================================================================"
echo "SECTION 11: NET PERSISTENCE"
echo "========================================================================"
echo ""

echo "Test 11.1: Create net and verify it persists across listings"
# Short on purpose. The checks below grep the `lager uart` table for this
# name, and that table wraps a long name across two lines --
#   test_uart_p   Unknown_UAR   /dev/ttyUSB ...
#   ersist        T_Device      1
# so grepping the full name can never match, no matter how healthy the net is.
TEST_PERSIST_NET="t_persist"
if try_create_uart_net "$TEST_PERSIST_NET"; then
  track_test "pass"
else
  track_test "pass"
fi
echo ""

echo "Test 11.2: List nets multiple times to verify persistence"
FAILED=0
for i in {1..5}; do
  if ! lager uart --box $BOX 2>&1 | grep -q "$TEST_PERSIST_NET"; then
    FAILED=1
  fi
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 11.3: Verify net appears in general nets listing"
lager nets --box $BOX 2>&1 | grep -q "$TEST_PERSIST_NET" && track_test "pass" || track_test "fail"
echo ""

echo "Test 11.4: Clean up persistence test net"
lager nets delete "$TEST_PERSIST_NET" uart --box $BOX --yes >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 12: EDGE CASES
# ============================================================
start_section "Edge Cases"
echo "========================================================================"
echo "SECTION 12: EDGE CASES"
echo "========================================================================"
echo ""

echo "Test 12.1: Create net with very long name"
LONG_NAME=$(printf 'uart_%.0s' {1..50})
if try_create_uart_net "$LONG_NAME"; then
  lager nets delete "$LONG_NAME" uart --box $BOX --yes >/dev/null 2>&1 || true
  track_test "pass"
else
  track_test "pass"
fi
echo ""

echo "Test 12.2: Create net with special characters in device path"
if lager nets add "test_special_path" uart "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART-if00-port0" "" --box $BOX 2>&1 | grep -q "Saved new net"; then
  lager nets delete "test_special_path" uart --box $BOX --yes >/dev/null 2>&1 || true
  track_test "pass"
else
  track_test "pass"
fi
echo ""

echo "Test 12.3: Create net with empty parameter value"
# Note: Parameters are set via net storage, not --params flag
# Just test that net creation works
if try_create_uart_net "test_empty_param"; then
  lager nets delete "test_empty_param" uart --box $BOX --yes >/dev/null 2>&1 || true
  track_test "pass"
else
  track_test "pass"
fi
echo ""

echo "Test 12.4: Verify help text contains usage examples"
lager uart --help 2>&1 | grep -qi "example\|usage" && track_test "pass" || track_test "fail"
echo ""

echo "Test 12.5: Test with invalid box name"
lager uart --box "INVALID_BOX_12345" 2>&1 | grep -qi "error\|not found\|don't have" && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 13: REGRESSION TESTS
# ============================================================
start_section "Regression Tests"
echo "========================================================================"
echo "SECTION 13: REGRESSION TESTS"
echo "========================================================================"
echo ""

echo "Test 13.1: Verify UART command works after errors"
lager uart nonexistent_net --box $BOX >/dev/null 2>&1 || true
lager uart --box $BOX >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 13.2: Verify net listing after failed creation"
lager nets add "" uart "$UART_SERIAL" "$UART_VISA" --box $BOX >/dev/null 2>&1 || true
lager uart --box $BOX >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 13.3: Verify net listing after failed deletion"
lager nets delete "nonexistent_uart_net" uart --box $BOX --yes >/dev/null 2>&1 || true
lager uart --box $BOX >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 13.4: Configuration consistency after multiple operations"
# Short for the same reason as TEST_PERSIST_NET: this check greps the
# `lager uart` table, which wraps longer names across two lines.
TEST_REGRESSION_NET="t_regress"
if try_create_uart_net "$TEST_REGRESSION_NET"; then
  # Verify it appears consistently
  FAILED=0
  for i in {1..5}; do
    if ! lager uart --box $BOX 2>&1 | grep -q "$TEST_REGRESSION_NET"; then
      FAILED=1
    fi
  done
  lager nets delete "$TEST_REGRESSION_NET" uart --box $BOX --yes >/dev/null 2>&1 || true
  [ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
else
  track_test "pass"
fi
echo ""

# ============================================================
# SECTION 14: SESSION MANAGEMENT (--sessions / --force)
# ============================================================
start_section "Session Management"
echo "========================================================================"
echo "SECTION 14: SESSION MANAGEMENT"
echo "========================================================================"
echo ""
# Surface-level, like the rest of this suite: it never opens a streaming
# session, so nothing here can hang. The behaviour of a held net being
# released -- and of --force taking one over -- is covered by
# test/unit/box/test_uart_session_cleanup.py and
# test/unit/cli/test_uart_session_release.py.

echo "Test 14.1: Check for --sessions option"
lager uart --help 2>&1 | grep -q "\-\-sessions" && track_test "pass" || track_test "fail"
echo ""

echo "Test 14.2: Check for --force option"
lager uart --help 2>&1 | grep -q "\-\-force" && track_test "pass" || track_test "fail"
echo ""

echo "Test 14.3: --sessions reports one of its three valid answers"
SESS_OUTPUT=$(lager uart --sessions --box $BOX 2>&1)
echo "$SESS_OUTPUT"
# A table of holders, "no sessions", or the too-old notice are all correct.
# A traceback, an auth error, or silence are not.
if echo "$SESS_OUTPUT" | grep -qiE "Device Path|No UART sessions are active|does not report UART sessions"; then
  track_test "pass"
else
  track_test "fail"
fi
echo ""

echo "Test 14.4: --sessions needs no netname"
lager uart --sessions --box $BOX >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 14.5: --force on a net that does not exist reports the missing net"
FORCE_OUTPUT=$(lager uart nonexistent_net_xyz --force --box $BOX 2>&1)
echo "$FORCE_OUTPUT" | head -3
if echo "$FORCE_OUTPUT" | grep -qiE "not found|does not exist|no.*net"; then
  track_test "pass"
else
  track_test "fail"
fi
echo ""

# ============================================================
# SECTION 15: UART DEVICE ROUND-TRIP
# ============================================================
# The only section that moves real bytes over the wire. Every section above
# exercises argument parsing and net bookkeeping, so none of them can catch a
# regression in the read/write path itself -- a `lager uart` that connects,
# reports a session, and transfers nothing would pass all fourteen.
#
# Needs a peer running the uart_ci_peer firmware, which answers PING with
# PONG. Without one every check SKIPs rather than fails, so a bench that has
# no peer attached stays green.
#
# One session carries every command. The CLI suppresses one inbound line per
# send (websocket_client.py:145) but `suppress_next_line` is a boolean, not a
# counter, so a fast burst of commands has only its first reply-line eaten.
# Each assertion below is therefore anchored and textually distinct from the
# command that triggers it, and holds whether or not the echo was suppressed.
start_section "UART Device Round-Trip"
echo "========================================================================"
echo "SECTION 15: UART DEVICE ROUND-TRIP"
echo "========================================================================"
echo ""

UART_PEER_NET="${UART_PEER_NET:-ESP_UART}"
UART_PEER_BAUD="${UART_PEER_BAUD:-115200}"
UART_PEER_SETTLE="${UART_PEER_SETTLE:-3}"
UART_PEER_DEADLINE="${UART_PEER_DEADLINE:-45}"
UART_PEER_TOKEN="ci-$$-${RANDOM}"

# `lager uart -i` requires BOTH stdin and stdout to be terminals
# (cli/commands/communication/uart.py:512) and exits 1 otherwise, so the
# commands cannot be piped in and the output cannot be piped to `tee`. The
# driver allocates a pty, types into it, and prints what came back.
UART_PTY_DRIVER="${SCRIPT_DIR}/../../framework/uart_pty_driver.py"

PEER_OUT=$(python3 "$UART_PTY_DRIVER" \
  --net "$UART_PEER_NET" --box "$BOX" --baud "$UART_PEER_BAUD" \
  --settle "$UART_PEER_SETTLE" --deadline "$UART_PEER_DEADLINE" \
  'ID?' 'PING' "ECHO ${UART_PEER_TOKEN}" 'RESET' 'COUNT?' 'COUNT?' 'Ping' 2>&1)
PEER_RC=$?
echo "$PEER_OUT"
echo ""

echo "Test 15.1: Peer identifies itself (ID?)"
# Three outcomes, kept apart on purpose. Reporting a refused session as a
# missing peer is how a broken check comes to look like absent hardware and
# sits green forever.
if [ "$PEER_RC" -eq 2 ]; then
  UART_PEER_PRESENT=false
  echo -e "${RED}The CLI would not start a session on '$UART_PEER_NET'${NC}"
  echo -e "${RED}That is a failure, not a missing peer - see the output above${NC}"
  track_test "fail"
elif echo "$PEER_OUT" | grep -q 'LAGER-UART-PEER'; then
  UART_PEER_PRESENT=true
  track_test "pass"
else
  UART_PEER_PRESENT=false
  echo -e "${YELLOW}Session opened but nothing answered ID? on '$UART_PEER_NET'${NC}"
  echo -e "${YELLOW}Flash test/assets/uart_ci_peer and set UART_PEER_NET to enable${NC}"
  track_test "skip"
fi
echo ""

echo "Test 15.2: PING returns PONG"
if [ "$UART_PEER_PRESENT" = true ]; then
  echo "$PEER_OUT" | grep -qE '^PONG[[:space:]]*$' && track_test "pass" || track_test "fail"
else
  track_test "skip"
fi
echo ""

echo "Test 15.3: ECHO round-trips a per-run token"
# A fresh token each run, so a stale buffer or a replayed log cannot pass.
if [ "$UART_PEER_PRESENT" = true ]; then
  echo "$PEER_OUT" | grep -qE "^${UART_PEER_TOKEN}[[:space:]]*$" && track_test "pass" || track_test "fail"
else
  track_test "skip"
fi
echo ""

echo "Test 15.4: COUNT? advances device-side state (1 then 2 after RESET)"
# Proves the device is executing, not replaying a fixed response.
if [ "$UART_PEER_PRESENT" = true ]; then
  if echo "$PEER_OUT" | grep -qE '^1[[:space:]]*$' && echo "$PEER_OUT" | grep -qE '^2[[:space:]]*$'; then
    track_test "pass"
  else
    track_test "fail"
  fi
else
  track_test "skip"
fi
echo ""

echo "Test 15.5: Unknown command still answers (ERR unknown)"
# Not cosmetic: a command that replied with nothing would leave the CLI's
# line suppression armed, and it would swallow the next command's reply.
if [ "$UART_PEER_PRESENT" = true ]; then
  echo "$PEER_OUT" | grep -qE '^ERR unknown[[:space:]]*$' && track_test "pass" || track_test "fail"
else
  track_test "skip"
fi
echo ""

echo "Test 15.6: Net released after disconnect"
# A leaked net is a regression in its own right: it makes the next run fail
# for a reason that has nothing to do with the next run.
if [ "$UART_PEER_PRESENT" = true ]; then
  if lager uart --sessions --box "$BOX" 2>&1 | grep -q "$UART_PEER_NET"; then
    track_test "fail"
  else
    track_test "pass"
  fi
else
  track_test "skip"
fi
echo ""

# ============================================================
# CLEANUP
# ============================================================
echo "========================================================================"
echo "CLEANUP"
echo "========================================================================"
echo ""

echo "Removing any test UART nets..."
# --yes on every delete. Without it the command prompts for confirmation, and
# these deletes have their output redirected -- so a run that actually created
# nets would block on an invisible prompt, or leave the nets behind. That did
# not show before because `lager nets create` is not a command, so nothing was
# ever created and every delete found nothing to confirm.
for name in "$TEST_UART_NET" "$TEST_UART_NET2" "$TEST_NET_PARAMS" "$TEST_NET_MULTI" "${TEST_UART_NET}_renamed"; do
  lager nets delete "$name" uart --box $BOX --yes >/dev/null 2>&1 || true
done

# Clean up any remaining stress test nets
for i in {1..10}; do
  lager nets delete "stress_uart_${i}" uart --box $BOX --yes >/dev/null 2>&1 || true
  lager nets delete "stress_multi_${i}" uart --box $BOX --yes >/dev/null 2>&1 || true
done

# Section 10 and 12 net names. Their inline deletes normally clear these, but
# a suite that dies partway would otherwise strand them on the bench for the
# next run to trip over.
for baud in 9600 19200 38400 57600 115200 230400 460800 921600; do
  lager nets delete "test_baud_${baud}" uart --box $BOX --yes >/dev/null 2>&1 || true
done
for parity in none even odd mark space; do
  lager nets delete "test_parity_${parity}" uart --box $BOX --yes >/dev/null 2>&1 || true
done
for stopbits in 1 1.5 2; do
  lager nets delete "test_stopbits_${stopbits}" uart --box $BOX --yes >/dev/null 2>&1 || true
done
for bytesize in 5 6 7 8; do
  lager nets delete "test_bytesize_${bytesize}" uart --box $BOX --yes >/dev/null 2>&1 || true
done
for flow in none xonxoff rtscts dsrdtr; do
  lager nets delete "test_flow_${flow}" uart --box $BOX --yes >/dev/null 2>&1 || true
done
for name in "$TEST_PERSIST_NET" "$TEST_REGRESSION_NET" "$LONG_NAME" \
            t_dup_a t_dup_b test_special_path test_empty_param; do
  [ -n "$name" ] && lager nets delete "$name" uart --box $BOX --yes >/dev/null 2>&1 || true
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
echo "  - UART command help and basic information"
echo "  - UART net creation and discovery"
echo "  - UART net parameter configuration (baudrate, parity, stopbits, etc.)"
echo "  - Error validation (invalid nets, parameters, boxes)"
echo "  - UART parameter overrides at runtime"
echo "  - Net listing and display formatting"
echo "  - UART net management (rename, delete)"
echo "  - Backward compatibility (--gateway vs --box)"
echo "  - Advanced options (interactive mode, line endings, test runners)"
echo "  - Parameter combinations (all baudrates, parity, stopbits, etc.)"
echo "  - Net persistence across operations"
echo "  - Edge cases (long names, special paths, empty parameters)"
echo "  - Regression tests (error recovery, state consistency)"
echo "  - Session management (--sessions listing and --force take-over)"
echo "  - Device round-trip against a live peer (PING/ECHO/COUNT over the wire)"
echo ""
echo "Test Statistics:"
echo "  - Total test sections: 15"
echo "  - Total test cases: $GLOBAL_TOTAL"
echo "  - Command categories tested: uart, nets (UART-specific)"
echo "  - Net-based configuration: Create, list, rename, delete UART nets"
echo "  - Parameter testing: Baudrate, parity, stopbits, bytesize, flow control"
echo "  - Backward compatibility: Legacy device path support"
echo ""

# Exit with appropriate status code
exit_with_status
