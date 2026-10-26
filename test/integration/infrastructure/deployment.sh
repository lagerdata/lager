#!/bin/bash

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

# test_deployment.sh
# Post-deployment test script for Lager boxes
#
# This script tests all non-net commands to verify box functionality
# without requiring any instruments to be connected.
#
# Usage: ./test_deployment.sh <box-ip-or-name>
# Example: ./test_deployment.sh <BOX_IP>
# Example: ./test_deployment.sh my-box

# DON'T exit on error - we want to track failures, matching every other suite
# in this directory. Under `set -e` the first failing run_test aborted the
# script, so the summary block at the end never ran and a single early failure
# hid every later result.
set +e

SSH_USER="${SSH_USER:-lager}"

# Color definitions
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BOLD='\033[1m'
NC='\033[0m' # No Color

# Test counters
TOTAL_TESTS=0
PASSED_TESTS=0
FAILED_TESTS=0
SKIPPED_TESTS=0

# DUT identifier
DUT=""

# Parse arguments
if [ -z "$1" ]; then
    echo -e "${RED}Error: No DUT name or IP address provided${NC}"
    echo "Usage: $0 <dut-name-or-ip>"
    echo "Example: $0 my-gateway"
    echo "Example: $0 <BOX_IP>"
    echo ""
    echo "Note: For best results, add the DUT first using:"
    echo "  lager boxes add --name <name> --ip <ip> --user <username> --yes"
    exit 1
fi

DUT="$1"

# Check if the box exists in the saved registry (by name or IP). This used
# to read `lager duts`, a registry that no longer exists in the CLI -- every
# run took the not-found path -- and then blocked on an interactive prompt,
# which in a non-TTY context reads EOF and exits. The registry is
# `lager boxes` now, and the not-found case warns and continues, which was
# the stated intent all along ("don't fail - allow testing with raw IPs").
DUT_CHECK=$(lager boxes 2>/dev/null | grep "^${DUT}" | head -1)
if [ -z "$DUT_CHECK" ]; then
    # Try to find by IP
    DUT_CHECK=$(lager boxes 2>/dev/null | awk -v ip="${DUT}" '$2 == ip {print; exit}')
fi

if [ -z "$DUT_CHECK" ]; then
    echo -e "${YELLOW}Warning: box '${DUT}' not found in saved boxes${NC}"
    echo -e "${YELLOW}Tests may fail if SSH authentication is not configured${NC}"
    echo ""
    echo "To register it:"
    echo "  lager boxes add --name ${DUT} --ip <IP_ADDRESS> --user <username> --yes"
    echo ""
fi

# Print header
echo ""
echo -e "${BOLD}=========================================${NC}"
echo -e "${BOLD}  Lager Box Post-Deployment Tests${NC}"
echo -e "${BOLD}=========================================${NC}"
echo ""
echo -e "${BLUE}Box:${NC} ${DUT}"
echo -e "${BLUE}Time:${NC}        $(date '+%Y-%m-%d %H:%M:%S')"
echo ""

# Initialize debug log
echo "========================================" > /tmp/deployment_test_debug.log
echo "Deployment Test Debug Log" >> /tmp/deployment_test_debug.log
echo "Started: $(date '+%Y-%m-%d %H:%M:%S')" >> /tmp/deployment_test_debug.log
echo "DUT: ${DUT}" >> /tmp/deployment_test_debug.log
echo "========================================" >> /tmp/deployment_test_debug.log
echo "" >> /tmp/deployment_test_debug.log

echo -e "${BLUE}Debug log:${NC} /tmp/deployment_test_debug.log"
echo ""

# Check if lager CLI is available
if ! command -v lager &> /dev/null; then
    echo -e "${RED}[ERROR] lager CLI not found${NC}"
    echo "Please install the lager CLI first:"
    echo "  cd cli"
    echo "  pip install -e ."
    exit 1
fi

# Detect DUT username early for test skipping logic. Same registry rename as
# the check above: `lager boxes` columns are name / ip / user / version /
# status, so the $3 read below still lands on the user.
DETECTED_USER=""
DUT_INFO_EARLY=$(lager boxes 2>/dev/null | grep "^${DUT}" | head -1)
if [ -z "$DUT_INFO_EARLY" ]; then
    DUT_INFO_EARLY=$(lager boxes 2>/dev/null | awk -v ip="${DUT}" '$2 == ip {print; exit}')
fi
if [ -n "$DUT_INFO_EARLY" ]; then
    DETECTED_USER=$(echo "$DUT_INFO_EARLY" | awk '{print $3}')
    if [ "$DETECTED_USER" = "-" ] || [ -z "$DETECTED_USER" ]; then
        DETECTED_USER="${SSH_USER}"
    fi
else
    DETECTED_USER="${SSH_USER}"
fi

echo "[$(date '+%H:%M:%S')] Detected DUT user: $DETECTED_USER" >> /tmp/deployment_test_debug.log

# Helper functions
print_test_header() {
    echo ""
    echo -e "${BOLD}${BLUE}$1${NC}"
    echo "----------------------------------------"
}

run_test() {
    local test_name="$1"
    local test_command="$2"
    local expect_success="${3:-true}"

    TOTAL_TESTS=$((TOTAL_TESTS + 1))

    echo -n "Testing: ${test_name}... "

    # Log the command being run
    echo "[$(date '+%H:%M:%S')] Running: $test_command" >> /tmp/deployment_test_debug.log

    # Run command once and capture output. A failure prints this output, not
    # a second run's: re-running a flaky command (a BLE scan on a controller
    # that resets itself) printed a clean retry under [FAIL] and lost the error.
    local output
    local start_time=$(date +%s)
    output=$(eval "$test_command" 2>&1)
    local exit_code=$?
    if [ $exit_code -eq 0 ]; then
        local end_time=$(date +%s)
        local duration=$((end_time - start_time))
        echo "[$(date '+%H:%M:%S')] Completed in ${duration}s (success)" >> /tmp/deployment_test_debug.log

        if [ "$expect_success" = "true" ]; then
            echo -e "${GREEN}[PASS]${NC}"
            PASSED_TESTS=$((PASSED_TESTS + 1))
            return 0
        else
            echo -e "${RED}[FAIL]${NC} (expected failure but succeeded)"
            FAILED_TESTS=$((FAILED_TESTS + 1))
            return 1
        fi
    else
        local end_time=$(date +%s)
        local duration=$((end_time - start_time))
        echo "[$(date '+%H:%M:%S')] Completed in ${duration}s (failed)" >> /tmp/deployment_test_debug.log

        if [ "$expect_success" = "false" ]; then
            echo -e "${GREEN}[PASS]${NC} (expected failure)"
            PASSED_TESTS=$((PASSED_TESTS + 1))
            return 0
        else
            echo -e "${RED}[FAIL]${NC}"
            FAILED_TESTS=$((FAILED_TESTS + 1))
            # Show error output for debugging
            echo -e "${YELLOW}  Command output:${NC}"
            echo "$output" | sed 's/^/    /'
            echo "[$(date '+%H:%M:%S')] ERROR: $output" >> /tmp/deployment_test_debug.log
            return 1
        fi
    fi
}

run_test_with_output() {
    local test_name="$1"
    local test_command="$2"

    TOTAL_TESTS=$((TOTAL_TESTS + 1))

    echo -n "Testing: ${test_name}... "

    # Log the command being run
    echo "[$(date '+%H:%M:%S')] Running: $test_command" >> /tmp/deployment_test_debug.log

    # Run command and capture output
    local output
    local start_time=$(date +%s)
    output=$(eval "$test_command" 2>&1)
    local exit_code=$?
    local end_time=$(date +%s)
    local duration=$((end_time - start_time))

    echo "[$(date '+%H:%M:%S')] Completed in ${duration}s with exit code $exit_code" >> /tmp/deployment_test_debug.log

    if [ $exit_code -eq 0 ]; then
        echo -e "${GREEN}[PASS]${NC} (${duration}s)"
        PASSED_TESTS=$((PASSED_TESTS + 1))
        # Show first line of output
        echo -e "${BLUE}  Output:${NC} $(echo "$output" | head -1)"
        return 0
    else
        echo -e "${RED}[FAIL]${NC} (${duration}s)"
        FAILED_TESTS=$((FAILED_TESTS + 1))
        echo -e "${YELLOW}  Error:${NC}"
        echo "$output" | sed 's/^/    /'
        echo "[$(date '+%H:%M:%S')] ERROR: $output" >> /tmp/deployment_test_debug.log
        return 1
    fi
}

skip_test() {
    local test_name="$1"
    local reason="$2"

    TOTAL_TESTS=$((TOTAL_TESTS + 1))
    SKIPPED_TESTS=$((SKIPPED_TESTS + 1))

    echo -e "Testing: ${test_name}... ${YELLOW}[SKIP]${NC} ($reason)"
}

# =============================================================================
# Test Section 1: Basic Connectivity
# =============================================================================
print_test_header "1. Basic Connectivity Tests"

run_test "lager --version" "lager --version"
run_test "lager --help" "lager --help"
run_test_with_output "lager hello --box ${DUT}" "timeout 30 lager hello --box ${DUT}"
run_test_with_output "lager hello --box ${DUT}" "timeout 30 lager hello --box ${DUT}"

# =============================================================================
# Test Section 2: Configuration Management
# =============================================================================
print_test_header "2. Configuration Management Tests"

run_test "lager defaults --help" "lager defaults --help"
run_test "lager dut --help" "lager dut --help"

# Note: Some lager commands (like instruments, nets) may not work with custom usernames
# They try to connect as the default SSH user regardless of box configuration
# Skip these tests if using a custom username box
if [ "$DETECTED_USER" != "${SSH_USER}" ]; then
    echo -e "${YELLOW}Note: Skipping tests that require '${SSH_USER}' user (current user: ${DETECTED_USER})${NC}"
    skip_test "lager instruments --box ${DUT}" "requires ${SSH_USER} user"
    skip_test "lager nets --box ${DUT}" "requires ${SSH_USER} user"
else
    run_test_with_output "lager instruments --box ${DUT}" "timeout 30 lager instruments --box ${DUT}"
    run_test_with_output "lager nets --box ${DUT}" "timeout 30 lager nets --box ${DUT}"
fi

# =============================================================================
# Test Section 3: Python Execution
# =============================================================================
print_test_header "3. Python Execution Tests"

run_test "lager python --help" "lager python --help"

# Test simple Python execution
TOTAL_TESTS=$((TOTAL_TESTS + 1))
echo -n "Testing: lager python (print test)... "
echo "[$(date '+%H:%M:%S')] Starting Python print test" >> /tmp/deployment_test_debug.log

TEMP_PY=$(mktemp)
mv "$TEMP_PY" "${TEMP_PY}.py"
TEMP_PY="${TEMP_PY}.py"
echo 'print("hello from box")' > "$TEMP_PY"
echo "[$(date '+%H:%M:%S')] Created temp file: $TEMP_PY" >> /tmp/deployment_test_debug.log
echo "[$(date '+%H:%M:%S')] Running: timeout 30 lager python --box ${DUT} $TEMP_PY" >> /tmp/deployment_test_debug.log

start_time=$(date +%s)
output=$(timeout 30 lager python --box ${DUT} "$TEMP_PY" 2>&1)
exit_code=$?
end_time=$(date +%s)
duration=$((end_time - start_time))

echo "[$(date '+%H:%M:%S')] Python test completed in ${duration}s with exit code $exit_code" >> /tmp/deployment_test_debug.log

rm -f "$TEMP_PY"
if [ $exit_code -eq 0 ]; then
    echo -e "${GREEN}[PASS]${NC} (${duration}s)"
    PASSED_TESTS=$((PASSED_TESTS + 1))
    echo -e "${BLUE}  Output:${NC} $(echo "$output" | head -1)"
else
    echo -e "${RED}[FAIL]${NC} (${duration}s)"
    FAILED_TESTS=$((FAILED_TESTS + 1))
    echo -e "${YELLOW}  Error:${NC}"
    echo "$output" | sed 's/^/    /'
    echo "[$(date '+%H:%M:%S')] Python test ERROR: $output" >> /tmp/deployment_test_debug.log
fi

# Test Python with imports
TOTAL_TESTS=$((TOTAL_TESTS + 1))
echo -n "Testing: lager python (import test)... "
echo "[$(date '+%H:%M:%S')] Starting Python import test" >> /tmp/deployment_test_debug.log

TEMP_PY=$(mktemp)
mv "$TEMP_PY" "${TEMP_PY}.py"
TEMP_PY="${TEMP_PY}.py"
echo 'import sys; print(sys.version)' > "$TEMP_PY"
echo "[$(date '+%H:%M:%S')] Running: timeout 30 lager python --box ${DUT} $TEMP_PY" >> /tmp/deployment_test_debug.log

start_time=$(date +%s)
output=$(timeout 30 lager python --box ${DUT} "$TEMP_PY" 2>&1)
exit_code=$?
end_time=$(date +%s)
duration=$((end_time - start_time))

echo "[$(date '+%H:%M:%S')] Python import test completed in ${duration}s with exit code $exit_code" >> /tmp/deployment_test_debug.log

rm -f "$TEMP_PY"
if [ $exit_code -eq 0 ]; then
    echo -e "${GREEN}[PASS]${NC} (${duration}s)"
    PASSED_TESTS=$((PASSED_TESTS + 1))
    echo -e "${BLUE}  Output:${NC} $(echo "$output" | head -1)"
else
    echo -e "${RED}[FAIL]${NC} (${duration}s)"
    FAILED_TESTS=$((FAILED_TESTS + 1))
    echo -e "${YELLOW}  Error:${NC}"
    echo "$output" | sed 's/^/    /'
    echo "[$(date '+%H:%M:%S')] Python import test ERROR: $output" >> /tmp/deployment_test_debug.log
fi

# =============================================================================
# Test Section 4: Development Environment
# =============================================================================
print_test_header "4. Development Environment Tests"

run_test "lager devenv --help" "lager devenv --help"
run_test "lager exec --help" "lager exec --help"

# =============================================================================
# Test Section 5: SSH Connectivity
# =============================================================================
print_test_header "5. SSH Connectivity Tests"

run_test "lager ssh --help" "lager ssh --help"

# Test SSH command execution (non-interactive) - use direct SSH since lager ssh doesn't support --command
# First resolve DUT to IP and username for SSH
# Handle both old format (just IP) and new format (with user field)
# lager boxes output format:
#   name          ip                user
#   <YOUR-BOX>    <BOX_IP>     -
#   test-deploy   <BOX_IP>    test-deploy

# Try to find the box by name first (`lager boxes`; see the registry note
# at the top of the script)
DUT_INFO=$(lager boxes 2>/dev/null | grep "^${DUT}" | head -1)

# If not found by name, try to find by IP address (2nd column match)
if [ -z "$DUT_INFO" ]; then
    DUT_INFO=$(lager boxes 2>/dev/null | awk -v ip="${DUT}" '$2 == ip {print; exit}')
fi

if [ -n "$DUT_INFO" ]; then
    # DUT found - parse the table format
    # Extract IP (2nd column) and user (3rd column)
    DUT_IP=$(echo "$DUT_INFO" | awk '{print $2}')
    DUT_USER=$(echo "$DUT_INFO" | awk '{print $3}')

    # If user is "-" (old format), use default SSH_USER
    if [ "$DUT_USER" = "-" ] || [ -z "$DUT_USER" ]; then
        DUT_USER="${SSH_USER}"
    fi
else
    # DUT not found in saved list - assume it's a direct IP
    DUT_IP="${DUT}"
    DUT_USER="${SSH_USER}"
fi

echo -e "${BLUE}Resolved DUT:${NC} ${DUT_USER}@${DUT_IP}"
echo ""

run_test_with_output "SSH echo test" "timeout 30 ssh ${DUT_USER}@${DUT_IP} 'echo test'"
run_test_with_output "SSH uname" "timeout 30 ssh ${DUT_USER}@${DUT_IP} 'uname -a'"
run_test_with_output "SSH docker ps" "timeout 30 ssh ${DUT_USER}@${DUT_IP} 'docker ps --format \"{{.Names}}\"'"

# =============================================================================
# Test Section 6: USB Hub Control
# =============================================================================
print_test_header "6. USB Hub Control Tests"

run_test "lager usb --help" "lager usb --help"

# `lager usb --box` with no net lists the box's USB nets. It reads saved nets
# only, so it must succeed whether or not a hub is attached. (This used to
# run `lager usb --box X list`, which takes "list" as a net name, behind an
# `|| true` that hid the error.)
if [ "$DETECTED_USER" != "${SSH_USER}" ]; then
    skip_test "lager usb (list USB nets)" "requires ${SSH_USER} user"
else
    run_test "lager usb (list USB nets)" "timeout 60 lager usb --box ${DUT}"
fi

# =============================================================================
# Test Section 7: BLE Scanning
# =============================================================================
print_test_header "7. BLE Scanning Tests"

run_test "lager ble --help" "lager ble --help"

# A short scan on a box with a Bluetooth adapter must succeed; finding no
# advertisers is still success. A box with no adapter fails here, and that is
# a real finding about the box. (The old check put --box before the
# subcommand, which `lager ble` rejects as an unknown option, inside an
# `|| true`: it had failed on every run without anyone seeing it.)
#
# The scan's own output is never printed, not even on failure: it lists the
# names and addresses of every advertiser near the box, and CI logs are
# public. Only the device count, any error line and the exit status are.
# run_test prints this summary from the attempt that failed and never re-runs
# the scan, so a controller fault shows its own error rather than the clean
# scan that followed the adapter's reset.
ble_scan_summary() {
    local out rc
    out=$(timeout 60 lager ble scan --box "$DUT" --timeout 2 2>&1)
    rc=$?
    printf '%s\n' "$out" | grep -E '^(Found [0-9]+ device|Error)' | head -3
    echo "exit status $rc"
    return "$rc"
}
if [ "$DETECTED_USER" != "${SSH_USER}" ]; then
    skip_test "lager ble scan" "requires ${SSH_USER} user"
else
    run_test "lager ble scan" "ble_scan_summary"
fi

# =============================================================================
# Test Section 8: Webcam Management
# =============================================================================
print_test_header "8. Webcam Management Tests"

run_test "lager webcam --help" "lager webcam --help"

# `webcam url` lists active streams and is read-only, so it must succeed with
# or without a camera. (This used to run `lager webcam --box X list`, which
# takes "list" as a net name, behind an `|| true` that hid the error.)
if [ "$DETECTED_USER" != "${SSH_USER}" ]; then
    skip_test "lager webcam url" "requires ${SSH_USER} user"
else
    run_test "lager webcam url" "timeout 60 lager webcam url --box ${DUT}"
fi

# =============================================================================
# Test Section 9: Package Management
# =============================================================================
print_test_header "9. Package Management Tests"

run_test "lager box config pip --help" "lager box config pip --help"

if [ "$DETECTED_USER" != "${SSH_USER}" ]; then
    skip_test "lager box config pip list" "requires ${SSH_USER} user"
else
    run_test_with_output "lager box config pip list" "timeout 60 lager box config pip list --box ${DUT}"
fi

# =============================================================================
# Test Section 10: Container Health
# =============================================================================
print_test_header "10. Container Health Tests"

# One container, named `lager` (box/start_box.sh). These used to filter for
# `controller` and `python`, which no longer exist: `docker ps` printed
# nothing and exited 0, so the check passed on every box whatever ran. Both
# checks now compare the container's state, not just the command's status.
LAGER_STATE=$(timeout 30 ssh "${DUT_USER}@${DUT_IP}" 'docker inspect lager --format "{{.State.Status}}"' 2>&1)
run_test "Lager container is running (state: ${LAGER_STATE})" "[ \"${LAGER_STATE}\" = running ]"

LAGER_RESTARTING=$(timeout 30 ssh "${DUT_USER}@${DUT_IP}" 'docker inspect lager --format "{{.State.Restarting}}"' 2>&1)
run_test "Lager container is not in a restart loop" "[ \"${LAGER_RESTARTING}\" = false ]"

# =============================================================================
# Test Section 11: Box Services
# =============================================================================
print_test_header "11. Box Services Tests"

# Test if Python container has lager module available
TOTAL_TESTS=$((TOTAL_TESTS + 1))
echo -n "Testing: Lager Python module available... "
echo "[$(date '+%H:%M:%S')] Starting lager module test" >> /tmp/deployment_test_debug.log

TEMP_PY=$(mktemp)
mv "$TEMP_PY" "${TEMP_PY}.py"
TEMP_PY="${TEMP_PY}.py"
echo 'import lager; print("lager module found")' > "$TEMP_PY"
echo "[$(date '+%H:%M:%S')] Running: timeout 30 lager python --box ${DUT} $TEMP_PY" >> /tmp/deployment_test_debug.log

start_time=$(date +%s)
output=$(timeout 30 lager python --box ${DUT} "$TEMP_PY" 2>&1)
exit_code=$?
end_time=$(date +%s)
duration=$((end_time - start_time))

echo "[$(date '+%H:%M:%S')] Lager module test completed in ${duration}s with exit code $exit_code" >> /tmp/deployment_test_debug.log

rm -f "$TEMP_PY"
if [ $exit_code -eq 0 ]; then
    echo -e "${GREEN}[PASS]${NC} (${duration}s)"
    PASSED_TESTS=$((PASSED_TESTS + 1))
    echo -e "${BLUE}  Output:${NC} $(echo "$output" | head -1)"
else
    echo -e "${RED}[FAIL]${NC} (${duration}s)"
    FAILED_TESTS=$((FAILED_TESTS + 1))
    echo -e "${YELLOW}  Error:${NC}"
    echo "$output" | sed 's/^/    /'
    echo "[$(date '+%H:%M:%S')] Lager module test ERROR: $output" >> /tmp/deployment_test_debug.log
fi

# Check if saved_nets.json exists (use direct SSH)
run_test_with_output "Saved nets configuration exists" "timeout 30 ssh ${DUT_USER}@${DUT_IP} 'test -f /etc/lager/saved_nets.json && echo \"saved_nets.json exists\"'"

# =============================================================================
# Test Section 12: Command Help Tests (verify all commands exist)
# =============================================================================
print_test_header "12. Command Existence Tests"

# Test that all major commands have help
run_test "lager adc --help" "lager adc --help"
run_test "lager arm --help" "lager arm --help"
run_test "lager battery --help" "lager battery --help"
run_test "lager dac --help" "lager dac --help"
run_test "lager debug --help" "lager debug --help"
run_test "lager eload --help" "lager eload --help"
run_test "lager gpi --help" "lager gpi --help"
run_test "lager gpo --help" "lager gpo --help"
run_test "lager logic --help" "lager logic --help"
run_test "lager scope --help" "lager scope --help"
run_test "lager solar --help" "lager solar --help"
run_test "lager supply --help" "lager supply --help"
run_test "lager thermocouple --help" "lager thermocouple --help"
run_test "lager uart --help" "lager uart --help"

# =============================================================================
# Test Results Summary
# =============================================================================
echo ""
echo -e "${BOLD}=========================================${NC}"
echo -e "${BOLD}  Test Results Summary${NC}"
echo -e "${BOLD}=========================================${NC}"
echo ""
echo -e "Total Tests:   ${BOLD}${TOTAL_TESTS}${NC}"
echo -e "Passed:        ${GREEN}${PASSED_TESTS}${NC}"
echo -e "Failed:        ${RED}${FAILED_TESTS}${NC}"
echo -e "Skipped:       ${YELLOW}${SKIPPED_TESTS}${NC}"
echo ""

# Calculate pass rate
if [ $TOTAL_TESTS -gt 0 ]; then
    PASS_RATE=$((PASSED_TESTS * 100 / TOTAL_TESTS))
    echo -e "Pass Rate:     ${BOLD}${PASS_RATE}%${NC}"
    echo ""
fi

# Show debug log location
echo -e "${BLUE}Debug log:${NC} /tmp/deployment_test_debug.log"
echo ""

# Final verdict
if [ $FAILED_TESTS -eq 0 ]; then
    echo -e "${BOLD}${GREEN}=========================================${NC}"
    echo -e "${BOLD}${GREEN}  All Tests Passed!${NC}"
    echo -e "${BOLD}${GREEN}=========================================${NC}"
    echo ""
    echo -e "${GREEN}Box is ready for use!${NC}"
    echo ""
    echo "Next steps:"
    echo "  1. Connect your test instruments"
    echo "  2. Create nets: lager nets create <name> <type> <channel> <address> --box ${DUT}"
    echo "  3. Test instrument commands with your hardware"
    echo ""
    exit 0
else
    echo -e "${BOLD}${RED}=========================================${NC}"
    echo -e "${BOLD}${RED}  Some Tests Failed${NC}"
    echo -e "${BOLD}${RED}=========================================${NC}"
    echo ""
    echo -e "${YELLOW}Review the failures above and:${NC}"
    echo "  1. Check that all Docker containers are running"
    echo "  2. Verify network connectivity to box"
    echo "  3. Check box logs for errors"
    echo "  4. Re-run deployment if needed:"
    echo "     lager install --ip ${DUT}"
    echo ""
    exit 1
fi
