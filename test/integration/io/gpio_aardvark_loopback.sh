#!/usr/bin/env bash

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
# ============================================================================
# Aardvark GPIO Loopback Tests -- Standalone
# ============================================================================
# Usage:
#   ./gpio_aardvark_loopback.sh <box> [out_net] [in_net]
#
# Arguments:
#   box      - Box name or IP (e.g., <YOUR-BOX>)
#   out_net  - Aardvark GPIO output net (default: gpio4, pin 7 / SCK)
#   in_net   - Aardvark GPIO input net  (default: gpio5, pin 8 / MOSI)
#
# Wiring required:
#   Aardvark Pin 7  (SCK / gpio4)  ------> Aardvark Pin 8  (MOSI / gpio5)
#
#   This script REQUIRES the loopback wire to be connected. Unlike the
#   main gpio_aardvark.sh which auto-detects and skips, this script will
#   FAIL if loopback is not wired.
#
# Run this separately from the main test to allow changing wiring configs
# between test runs.
# ============================================================================

# Resolve script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Load test framework
source "${SCRIPT_DIR}/../../framework/colors.sh"
source "${SCRIPT_DIR}/../../framework/harness.sh"

# ============================================================================
# Arguments
# ============================================================================
if [ -z "$1" ]; then
    echo "Usage: $0 <box> [out_net] [in_net]"
    echo ""
    echo "  box      - Box name or IP (e.g., <YOUR-BOX>)"
    echo "  out_net  - Aardvark GPIO output net (default: gpio4, pin 7 / SCK)"
    echo "  in_net   - Aardvark GPIO input net  (default: gpio5, pin 8 / MOSI)"
    echo ""
    echo "Wiring: Connect ${2:-gpio4} pin to ${3:-gpio5} pin with a jumper wire"
    exit 1
fi

BOX="$1"
OUT_NET="${2:-gpio4}"
IN_NET="${3:-gpio5}"

init_harness

print_script_header "LAGER AARDVARK GPIO LOOPBACK TEST" "$BOX" "$OUT_NET"

echo "Output net: $OUT_NET (drive side)"
echo "Input net:  $IN_NET (sense side)"
echo ""

# ============================================================================
# Helpers
# ============================================================================

# Strip ANSI escape codes from a string
strip_ansi() {
    echo "$1" | sed $'s/\033\[[0-9;]*m//g'
}

# ============================================================================
# 0. PREREQUISITES
# ============================================================================
start_section "Prerequisites"
print_section_header "SECTION 0: PREREQUISITES"

# 0a. Verify box connectivity
echo -n "Test: Box connectivity... "
lager hello --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 0b. GPO works on output net
echo -n "Test: GPO $OUT_NET succeeds... "
lager gpo "$OUT_NET" low --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 0c. GPI works on input net
echo -n "Test: GPI $IN_NET succeeds... "
lager gpi "$IN_NET" --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# ============================================================================
# 1. LOOPBACK DETECTION
# ============================================================================
start_section "Loopback Detection"
print_section_header "SECTION 1: LOOPBACK DETECTION"

echo "  Verifying loopback wire between $OUT_NET and $IN_NET..."
echo ""

# 1a. Set output HIGH, read input -- must see HIGH
echo -n "Test: Set $OUT_NET HIGH, read $IN_NET -> HIGH... "
lager gpo "$OUT_NET" high --box "$BOX" >/dev/null 2>&1
sleep 1
RAW=$(lager gpi "$IN_NET" --box "$BOX" 2>&1)
CLEAN=$(strip_ansi "$RAW")
if echo "$CLEAN" | grep -q "HIGH (1)"; then
    track_test "pass"
else
    echo -n "(got: '$CLEAN') "
    track_test "fail"
    echo ""
    echo -e "${RED}LOOPBACK NOT DETECTED!${NC}"
    echo "Ensure $OUT_NET and $IN_NET are connected with a jumper wire."
    echo "Aborting remaining tests."
    print_summary
    exit_with_status
fi

# 1b. Set output LOW, read input -- must see LOW (confirms bidirectional tracking)
echo -n "Test: Set $OUT_NET LOW, read $IN_NET -> LOW... "
lager gpo "$OUT_NET" low --box "$BOX" >/dev/null 2>&1
sleep 1
RAW=$(lager gpi "$IN_NET" --box "$BOX" 2>&1)
CLEAN=$(strip_ansi "$RAW")
if echo "$CLEAN" | grep -q "LOW (0)"; then
    track_test "pass"
else
    echo -n "(got: '$CLEAN') "
    track_test "fail"
    echo ""
    echo -e "${RED}LOOPBACK INCONSISTENT!${NC}"
    echo "HIGH tracked but LOW did not. Check wiring and ground."
    echo "Aborting remaining tests."
    print_summary
    exit_with_status
fi

echo ""
echo "  Loopback confirmed -- proceeding with tests"
echo ""

# ============================================================================
# 2. BASIC LOOPBACK ROUND-TRIP
# ============================================================================
start_section "Basic Round-Trip"
print_section_header "SECTION 2: BASIC LOOPBACK ROUND-TRIP"

# 2a. HIGH -> read HIGH
echo -n "Test: HIGH -> read HIGH (1)... "
lager gpo "$OUT_NET" high --box "$BOX" >/dev/null 2>&1
sleep 0.2
RAW=$(lager gpi "$IN_NET" --box "$BOX" 2>&1)
CLEAN=$(strip_ansi "$RAW")
if echo "$CLEAN" | grep -q "HIGH (1)"; then
    track_test "pass"
else
    echo -n "(got: '$CLEAN') "
    track_test "fail"
fi

# 2b. LOW -> read LOW
echo -n "Test: LOW -> read LOW (0)... "
lager gpo "$OUT_NET" low --box "$BOX" >/dev/null 2>&1
sleep 0.2
RAW=$(lager gpi "$IN_NET" --box "$BOX" 2>&1)
CLEAN=$(strip_ansi "$RAW")
if echo "$CLEAN" | grep -q "LOW (0)"; then
    track_test "pass"
else
    echo -n "(got: '$CLEAN') "
    track_test "fail"
fi

# 2c. '1' -> read HIGH
echo -n "Test: '1' -> read HIGH... "
lager gpo "$OUT_NET" 1 --box "$BOX" >/dev/null 2>&1
sleep 0.2
RAW=$(lager gpi "$IN_NET" --box "$BOX" 2>&1)
CLEAN=$(strip_ansi "$RAW")
if echo "$CLEAN" | grep -q "HIGH (1)"; then
    track_test "pass"
else
    echo -n "(got: '$CLEAN') "
    track_test "fail"
fi

# 2d. '0' -> read LOW
echo -n "Test: '0' -> read LOW... "
lager gpo "$OUT_NET" 0 --box "$BOX" >/dev/null 2>&1
sleep 0.2
RAW=$(lager gpi "$IN_NET" --box "$BOX" 2>&1)
CLEAN=$(strip_ansi "$RAW")
if echo "$CLEAN" | grep -q "LOW (0)"; then
    track_test "pass"
else
    echo -n "(got: '$CLEAN') "
    track_test "fail"
fi

# 2e. 'on' -> read HIGH
echo -n "Test: 'on' -> read HIGH... "
lager gpo "$OUT_NET" on --box "$BOX" >/dev/null 2>&1
sleep 0.2
RAW=$(lager gpi "$IN_NET" --box "$BOX" 2>&1)
CLEAN=$(strip_ansi "$RAW")
if echo "$CLEAN" | grep -q "HIGH (1)"; then
    track_test "pass"
else
    echo -n "(got: '$CLEAN') "
    track_test "fail"
fi

# 2f. 'off' -> read LOW
echo -n "Test: 'off' -> read LOW... "
lager gpo "$OUT_NET" off --box "$BOX" >/dev/null 2>&1
sleep 0.2
RAW=$(lager gpi "$IN_NET" --box "$BOX" 2>&1)
CLEAN=$(strip_ansi "$RAW")
if echo "$CLEAN" | grep -q "LOW (0)"; then
    track_test "pass"
else
    echo -n "(got: '$CLEAN') "
    track_test "fail"
fi

# ============================================================================
# 3. TOGGLE VIA LOOPBACK
# ============================================================================
start_section "Toggle Loopback"
print_section_header "SECTION 3: TOGGLE VIA LOOPBACK"

# 3a. off -> toggle -> input reads HIGH
echo -n "Test: off -> toggle -> input HIGH... "
lager gpo "$OUT_NET" off --box "$BOX" >/dev/null 2>&1
lager gpo "$OUT_NET" toggle --box "$BOX" >/dev/null 2>&1
sleep 0.2
RAW=$(lager gpi "$IN_NET" --box "$BOX" 2>&1)
CLEAN=$(strip_ansi "$RAW")
if echo "$CLEAN" | grep -q "HIGH (1)"; then
    track_test "pass"
else
    echo -n "(got: '$CLEAN') "
    track_test "fail"
fi

# 3b. toggle again -> input reads LOW
echo -n "Test: toggle again -> input LOW... "
lager gpo "$OUT_NET" toggle --box "$BOX" >/dev/null 2>&1
sleep 0.2
RAW=$(lager gpi "$IN_NET" --box "$BOX" 2>&1)
CLEAN=$(strip_ansi "$RAW")
if echo "$CLEAN" | grep -q "LOW (0)"; then
    track_test "pass"
else
    echo -n "(got: '$CLEAN') "
    track_test "fail"
fi

# 3c. Double toggle from low -> input reads LOW
echo -n "Test: double toggle from low -> input LOW... "
lager gpo "$OUT_NET" low --box "$BOX" >/dev/null 2>&1
lager gpo "$OUT_NET" toggle --box "$BOX" >/dev/null 2>&1
lager gpo "$OUT_NET" toggle --box "$BOX" >/dev/null 2>&1
sleep 0.2
RAW=$(lager gpi "$IN_NET" --box "$BOX" 2>&1)
CLEAN=$(strip_ansi "$RAW")
if echo "$CLEAN" | grep -q "LOW (0)"; then
    track_test "pass"
else
    echo -n "(got: '$CLEAN') "
    track_test "fail"
fi

# 3d. 10 toggles from off, verify each via input
echo -n "Test: 10 toggles from off, verify each via input... "
lager gpo "$OUT_NET" off --box "$BOX" >/dev/null 2>&1
TOGGLE_OK=true
EXPECTED_LEVELS=("HIGH (1)" "LOW (0)" "HIGH (1)" "LOW (0)" "HIGH (1)" "LOW (0)" "HIGH (1)" "LOW (0)" "HIGH (1)" "LOW (0)")
for i in $(seq 0 9); do
    lager gpo "$OUT_NET" toggle --box "$BOX" >/dev/null 2>&1
    sleep 0.1
    RAW=$(lager gpi "$IN_NET" --box "$BOX" 2>&1)
    CLEAN=$(strip_ansi "$RAW")
    EXPECTED="${EXPECTED_LEVELS[$i]}"
    if ! echo "$CLEAN" | grep -q "$EXPECTED"; then
        echo -n "(toggle $((i+1)): expected '$EXPECTED', got '$CLEAN') "
        TOGGLE_OK=false
        break
    fi
done
if [ "$TOGGLE_OK" = true ]; then
    track_test "pass"
else
    track_test "fail"
fi

# ============================================================================
# 4. CONSISTENCY
# ============================================================================
start_section "Consistency"
print_section_header "SECTION 4: CONSISTENCY"

# 4a. 3x consistent reads for HIGH
echo -n "Test: HIGH, 3x consistent reads... "
lager gpo "$OUT_NET" high --box "$BOX" >/dev/null 2>&1
sleep 0.2
READ3_OK=true
for i in 1 2 3; do
    RAW=$(lager gpi "$IN_NET" --box "$BOX" 2>&1)
    CLEAN=$(strip_ansi "$RAW")
    if ! echo "$CLEAN" | grep -q "HIGH (1)"; then
        READ3_OK=false
        echo -n "(read $i: '$CLEAN') "
        break
    fi
done
if [ "$READ3_OK" = true ]; then
    track_test "pass"
else
    track_test "fail"
fi

# 4b. 3x consistent reads for LOW
echo -n "Test: LOW, 3x consistent reads... "
lager gpo "$OUT_NET" low --box "$BOX" >/dev/null 2>&1
sleep 0.2
READ3L_OK=true
for i in 1 2 3; do
    RAW=$(lager gpi "$IN_NET" --box "$BOX" 2>&1)
    CLEAN=$(strip_ansi "$RAW")
    if ! echo "$CLEAN" | grep -q "LOW (0)"; then
        READ3L_OK=false
        echo -n "(read $i: '$CLEAN') "
        break
    fi
done
if [ "$READ3L_OK" = true ]; then
    track_test "pass"
else
    track_test "fail"
fi

# 4c. 5x rapid toggle with read after each
echo -n "Test: 5x rapid toggle with read... "
lager gpo "$OUT_NET" low --box "$BOX" >/dev/null 2>&1
RAPID_OK=true
for i in 1 2 3 4 5; do
    lager gpo "$OUT_NET" toggle --box "$BOX" >/dev/null 2>&1
    sleep 0.1
    RAW=$(lager gpi "$IN_NET" --box "$BOX" 2>&1)
    if [ $? -ne 0 ]; then
        RAPID_OK=false
        break
    fi
done
if [ "$RAPID_OK" = true ]; then
    track_test "pass"
else
    track_test "fail"
fi

# 4d. 10x interleaved set+read
echo -n "Test: 10x interleaved set+read... "
INTERLEAVE_OK=true
for i in $(seq 1 5); do
    lager gpo "$OUT_NET" high --box "$BOX" >/dev/null 2>&1
    sleep 0.1
    RAW=$(lager gpi "$IN_NET" --box "$BOX" 2>&1)
    CLEAN=$(strip_ansi "$RAW")
    if ! echo "$CLEAN" | grep -q "HIGH (1)"; then
        echo -n "(cycle $i HIGH: '$CLEAN') "
        INTERLEAVE_OK=false
        break
    fi
    lager gpo "$OUT_NET" low --box "$BOX" >/dev/null 2>&1
    sleep 0.1
    RAW=$(lager gpi "$IN_NET" --box "$BOX" 2>&1)
    CLEAN=$(strip_ansi "$RAW")
    if ! echo "$CLEAN" | grep -q "LOW (0)"; then
        echo -n "(cycle $i LOW: '$CLEAN') "
        INTERLEAVE_OK=false
        break
    fi
done
if [ "$INTERLEAVE_OK" = true ]; then
    track_test "pass"
else
    track_test "fail"
fi

# ============================================================================
# 5. HOLD MODE VIA LOOPBACK
# ============================================================================
start_section "Hold + Loopback"
print_section_header "SECTION 5: HOLD MODE VIA LOOPBACK"

# Helper: kill hold process and wait for remote Aardvark release
kill_hold() {
    local pid="$1"
    kill "$pid" 2>/dev/null
    wait "$pid" 2>/dev/null
    sleep 3
}

# 5a. Hold HIGH, verify via input net
echo -n "Test: Hold HIGH, loopback read -> HIGH... "
lager gpo "$OUT_NET" high --hold --box "$BOX" >/dev/null 2>&1 &
HOLD_PID=$!
sleep 3
RAW=$(lager gpi "$IN_NET" --box "$BOX" 2>&1)
CLEAN=$(strip_ansi "$RAW")
kill_hold $HOLD_PID
if echo "$CLEAN" | grep -q "HIGH (1)"; then
    track_test "pass"
else
    echo -n "(got: '$CLEAN') "
    track_test "fail"
fi

# Reset between hold tests
lager gpo "$OUT_NET" low --box "$BOX" >/dev/null 2>&1
sleep 1

# 5b. Hold LOW, verify via input net
echo -n "Test: Hold LOW, loopback read -> LOW... "
lager gpo "$OUT_NET" low --hold --box "$BOX" >/dev/null 2>&1 &
HOLD_PID=$!
sleep 3
RAW=$(lager gpi "$IN_NET" --box "$BOX" 2>&1)
CLEAN=$(strip_ansi "$RAW")
kill_hold $HOLD_PID
if echo "$CLEAN" | grep -q "LOW (0)"; then
    track_test "pass"
else
    echo -n "(got: '$CLEAN') "
    track_test "fail"
fi

# ============================================================================
# 6. STRESS
# ============================================================================
start_section "Stress"
print_section_header "SECTION 6: STRESS"

# 6a. 20x high/low cycle with loopback verification
echo -n "Test: 20x high/low cycle verified via loopback... "
STRESS_OK=true
for i in $(seq 1 10); do
    lager gpo "$OUT_NET" high --box "$BOX" >/dev/null 2>&1
    sleep 0.1
    RAW=$(lager gpi "$IN_NET" --box "$BOX" 2>&1)
    CLEAN=$(strip_ansi "$RAW")
    if ! echo "$CLEAN" | grep -q "HIGH (1)"; then
        echo -n "(cycle $i HIGH failed) "
        STRESS_OK=false
        break
    fi
    lager gpo "$OUT_NET" low --box "$BOX" >/dev/null 2>&1
    sleep 0.1
    RAW=$(lager gpi "$IN_NET" --box "$BOX" 2>&1)
    CLEAN=$(strip_ansi "$RAW")
    if ! echo "$CLEAN" | grep -q "LOW (0)"; then
        echo -n "(cycle $i LOW failed) "
        STRESS_OK=false
        break
    fi
done
if [ "$STRESS_OK" = true ]; then
    track_test "pass"
else
    track_test "fail"
fi

# ============================================================================
# 7. CLEANUP
# ============================================================================
start_section "Cleanup"
print_section_header "SECTION 7: CLEANUP"

# 7a. Set output low
echo -n "Test: Set $OUT_NET low... "
lager gpo "$OUT_NET" low --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 7b. Final connectivity check
echo -n "Test: Final connectivity check... "
lager hello --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# ============================================================================
# SUMMARY
# ============================================================================
print_summary
exit_with_status
