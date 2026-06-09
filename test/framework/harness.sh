#!/bin/bash

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
# harness.sh - Test harness infrastructure for Lager integration tests
#
# Provides:
#   - Test tracking (sections, pass/fail counts)
#   - Summary table printing
#   - Cross-platform timestamp function
#   - Common helper functions
#
# Usage:
#   source "${SCRIPT_DIR}/../framework/colors.sh"
#   source "${SCRIPT_DIR}/../framework/harness.sh"
#
#   init_harness  # Initialize tracking arrays
#   start_section "My Section"
#   my_command && track_test "pass" || track_test "fail"
#   print_summary
#   exit_with_status

# ============================================================
# Test Tracking Variables
# ============================================================

# Section tracking arrays
declare -a SECTION_NAMES
declare -a SECTION_TOTAL
declare -a SECTION_PASSED
declare -a SECTION_FAILED
declare -a SECTION_EXCLUDED

# Current section index (starts at -1, incremented by start_section)
CURRENT_SECTION=-1

# Global counters
GLOBAL_TOTAL=0
GLOBAL_PASSED=0
GLOBAL_FAILED=0
GLOBAL_EXCLUDED=0

# ============================================================
# Initialization
# ============================================================

# Initialize the test harness
# Call this at the beginning of your test script.
#
# Opt-in session-scoped box locking:
#   LAGER_TEST_LOCK=1   - acquire `lager boxes lock --box "$BOX"` at start
#                         and release on EXIT/INT/TERM. `$BOX` must be set
#                         before init_harness when this is enabled.
#   LAGER_LOCK_HOLDER   - forwarded as --user to make the lock holder
#                         match the CLI's auto-lock identity (so the
#                         per-script `lager python` calls inside the suite
#                         see "already ours" rather than colliding).
init_harness() {
    SECTION_NAMES=()
    SECTION_TOTAL=()
    SECTION_PASSED=()
    SECTION_FAILED=()
    SECTION_EXCLUDED=()
    CURRENT_SECTION=-1
    GLOBAL_TOTAL=0
    GLOBAL_PASSED=0
    GLOBAL_FAILED=0
    GLOBAL_EXCLUDED=0

    if [ "${LAGER_TEST_LOCK:-0}" = "1" ]; then
        if [ -z "${BOX:-}" ]; then
            echo "init_harness: LAGER_TEST_LOCK=1 but \$BOX is not set yet; skipping session lock" >&2
        else
            acquire_box_lock_harness "$BOX"
            # shellcheck disable=SC2064  # we want $BOX expanded at trap-time, not later
            trap "release_box_lock_harness '$BOX'" EXIT INT TERM
        fi
    fi
}

# Acquire a `lager boxes lock` for the duration of a bash test suite.
# Used by init_harness when LAGER_TEST_LOCK=1; can also be called directly
# from tests that want to manage the lock themselves.
#
# Usage: acquire_box_lock_harness "$BOX"
acquire_box_lock_harness() {
    local box="$1"
    local args=(boxes lock --box "$box")
    if [ -n "${LAGER_LOCK_HOLDER:-}" ]; then
        args+=(--user "$LAGER_LOCK_HOLDER")
    fi
    if ! lager "${args[@]}" >&2; then
        echo "acquire_box_lock_harness: failed to lock '$box'" >&2
        return 1
    fi
}

# Release the harness-acquired lock. Idempotent and best-effort: a failure
# here must not mask the test exit code.
#
# Usage: release_box_lock_harness "$BOX"
release_box_lock_harness() {
    local box="$1"
    [ -n "$box" ] || return 0
    lager boxes unlock --box "$box" >/dev/null 2>&1 || true
}

# ============================================================
# Section Management
# ============================================================

# Start a new test section
# Usage: start_section "Section Name"
start_section() {
    local name="$1"
    CURRENT_SECTION=$((CURRENT_SECTION + 1))
    SECTION_NAMES[$CURRENT_SECTION]="$name"
    SECTION_TOTAL[$CURRENT_SECTION]=0
    SECTION_PASSED[$CURRENT_SECTION]=0
    SECTION_FAILED[$CURRENT_SECTION]=0
    SECTION_EXCLUDED[$CURRENT_SECTION]=0
}

# ============================================================
# Test Result Tracking
# ============================================================

# Track a test result
# Usage: my_command && track_test "pass" || track_test "fail"
track_test() {
    local passed="$1"  # "pass", "fail", or "exclude"/"skip"

    SECTION_TOTAL[$CURRENT_SECTION]=$((SECTION_TOTAL[$CURRENT_SECTION] + 1))
    GLOBAL_TOTAL=$((GLOBAL_TOTAL + 1))

    case "$passed" in
        pass)
            SECTION_PASSED[$CURRENT_SECTION]=$((SECTION_PASSED[$CURRENT_SECTION] + 1))
            GLOBAL_PASSED=$((GLOBAL_PASSED + 1))
            echo -e "${GREEN}[PASS]${NC}"
            ;;
        fail)
            SECTION_FAILED[$CURRENT_SECTION]=$((SECTION_FAILED[$CURRENT_SECTION] + 1))
            GLOBAL_FAILED=$((GLOBAL_FAILED + 1))
            echo -e "${RED}[FAIL]${NC}"
            ;;
        exclude|skip)
            SECTION_EXCLUDED[$CURRENT_SECTION]=$((SECTION_EXCLUDED[$CURRENT_SECTION] + 1))
            GLOBAL_EXCLUDED=$((GLOBAL_EXCLUDED + 1))
            echo -e "${YELLOW}[SKIP]${NC}"
            ;;
    esac
}

# Skip a test with a reason
# Usage: skip_test "Test name" "reason"
skip_test() {
    local test_name="$1"
    local reason="$2"

    SECTION_TOTAL[$CURRENT_SECTION]=$((SECTION_TOTAL[$CURRENT_SECTION] + 1))
    GLOBAL_TOTAL=$((GLOBAL_TOTAL + 1))
    SECTION_EXCLUDED[$CURRENT_SECTION]=$((SECTION_EXCLUDED[$CURRENT_SECTION] + 1))
    GLOBAL_EXCLUDED=$((GLOBAL_EXCLUDED + 1))

    echo -e "Testing: ${test_name}... ${YELLOW}[SKIP]${NC} ($reason)"
}

# Track test result with custom message
# Usage: track_test_msg "pass" "Test description"
track_test_msg() {
    local passed="$1"
    local msg="$2"

    SECTION_TOTAL[$CURRENT_SECTION]=$((SECTION_TOTAL[$CURRENT_SECTION] + 1))
    GLOBAL_TOTAL=$((GLOBAL_TOTAL + 1))

    case "$passed" in
        pass)
            SECTION_PASSED[$CURRENT_SECTION]=$((SECTION_PASSED[$CURRENT_SECTION] + 1))
            GLOBAL_PASSED=$((GLOBAL_PASSED + 1))
            echo -e "${GREEN}[PASS]${NC} - $msg"
            ;;
        fail)
            SECTION_FAILED[$CURRENT_SECTION]=$((SECTION_FAILED[$CURRENT_SECTION] + 1))
            GLOBAL_FAILED=$((GLOBAL_FAILED + 1))
            echo -e "${RED}[FAIL]${NC} - $msg"
            ;;
    esac
}

# ============================================================
# Summary Printing
# ============================================================

# Print the test summary table
print_summary() {
    echo ""
    echo "========================================================================"
    echo "TEST SUMMARY"
    echo "========================================================================"
    echo ""

    # Determine if we need the excluded column
    local show_excluded=0
    if [ "$GLOBAL_EXCLUDED" -gt 0 ]; then
        show_excluded=1
    fi

    # Print header
    if [ "$show_excluded" -eq 1 ]; then
        printf "%-8s %-40s %5s %6s %6s %8s\n" "Section" "Description" "Total" "Passed" "Failed" "Excluded"
    else
        printf "%-8s %-45s %5s %6s %6s\n" "Section" "Description" "Total" "Passed" "Failed"
    fi
    echo "--------------------------------------------------------------------------------"

    for i in "${!SECTION_NAMES[@]}"; do
        local section_num=$((i + 1))
        if [ "$show_excluded" -eq 1 ]; then
            printf "%-8s %-40s %5s %6s %6s %8s\n" \
                "$section_num" \
                "${SECTION_NAMES[$i]}" \
                "${SECTION_TOTAL[$i]}" \
                "${SECTION_PASSED[$i]}" \
                "${SECTION_FAILED[$i]}" \
                "${SECTION_EXCLUDED[$i]}"
        else
            printf "%-8s %-45s %5s %6s %6s\n" \
                "$section_num" \
                "${SECTION_NAMES[$i]}" \
                "${SECTION_TOTAL[$i]}" \
                "${SECTION_PASSED[$i]}" \
                "${SECTION_FAILED[$i]}"
        fi
    done

    echo "--------------------------------------------------------------------------------"
    if [ "$show_excluded" -eq 1 ]; then
        printf "%-8s %-40s %5s %6s %6s %8s\n" \
            "TOTAL" "" \
            "$GLOBAL_TOTAL" \
            "$GLOBAL_PASSED" \
            "$GLOBAL_FAILED" \
            "$GLOBAL_EXCLUDED"
    else
        printf "%-8s %-45s %5s %6s %6s\n" \
            "TOTAL" "" \
            "$GLOBAL_TOTAL" \
            "$GLOBAL_PASSED" \
            "$GLOBAL_FAILED"
    fi

    echo ""

    if [ $GLOBAL_FAILED -eq 0 ]; then
        echo -e "${GREEN}[PASS] ALL TESTS PASSED${NC}"
        echo ""
        echo -e "Production readiness: ${GREEN}READY${NC}"
    else
        echo -e "${RED}[FAIL] ${GLOBAL_FAILED} TEST(S) FAILED${NC}"
        echo ""
        echo -e "Production readiness: ${RED}NEEDS ATTENTION${NC}"
    fi

    echo ""
}

# Exit with appropriate status code based on test results
exit_with_status() {
    if [ $GLOBAL_FAILED -gt 0 ]; then
        exit 1
    fi
    exit 0
}

# ============================================================
# Utility Functions
# ============================================================

# Cross-platform timestamp function (milliseconds)
# macOS date doesn't support %N (nanoseconds), so we handle both platforms
get_timestamp_ms() {
    if [[ "$OSTYPE" == "darwin"* ]]; then
        # macOS: use seconds and multiply by 1000
        echo $(( $(date +%s) * 1000 ))
    else
        # Linux: use nanoseconds and divide by 1000000
        echo $(( $(date +%s%N) / 1000000 ))
    fi
}

# Register a box from IP address if needed
# Usage: register_box_from_ip "<BOX_IP>" "my_box_name"
# Returns: Sets BOX variable to the box name
register_box_from_ip() {
    local input="$1"
    local prefix="${2:-temp_box}"

    if echo "$input" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$'; then
        # Input is an IP address - register it with a temporary name
        BOX_NAME="${prefix}_$(echo $input | tr '.' '_')"
        BOX_IP="$input"
        echo "Detected IP address: $BOX_IP"
        echo "Registering as temporary box: $BOX_NAME"
        lager boxes add --name "$BOX_NAME" --ip "$BOX_IP" --yes >/dev/null 2>&1 || true
        BOX="$BOX_NAME"
    else
        # Input is a box name - use it directly
        BOX_NAME="$input"
        BOX="$BOX_NAME"
        echo "Using box name: $BOX_NAME"
    fi
}

# Print a section header (visual separator)
# Usage: print_section_header "SECTION 1: MY TESTS"
print_section_header() {
    local title="$1"
    echo "========================================================================"
    echo "$title"
    echo "========================================================================"
    echo ""
}

# Print test script header
# Usage: print_script_header "LAGER SUPPLY TEST SUITE" "$BOX" "$NET"
print_script_header() {
    local title="$1"
    local box="$2"
    local net="${3:-}"

    echo "========================================================================"
    echo "$title"
    echo "========================================================================"
    echo ""
    echo "Box: $box"
    if [ -n "$net" ]; then
        echo "Net: $net"
    fi
    echo ""
}

# Run a test command and track result
# Usage: run_and_track "Test description" "command to run"
run_and_track() {
    local description="$1"
    local cmd="$2"

    echo -n "Test: $description... "
    if eval "$cmd" >/dev/null 2>&1; then
        track_test "pass"
        return 0
    else
        track_test "fail"
        return 1
    fi
}

# Run a test and expect failure (for error validation tests)
# Usage: run_expect_fail "Test invalid input" "command that should fail" "error pattern"
run_expect_fail() {
    local description="$1"
    local cmd="$2"
    local pattern="${3:-error}"

    echo -n "Test: $description... "
    if eval "$cmd" 2>&1 | grep -qi "$pattern"; then
        track_test "pass"
        return 0
    else
        track_test "fail"
        return 1
    fi
}
