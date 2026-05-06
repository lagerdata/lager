#!/usr/bin/env bash

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
# ============================================================================
# FT232H GPIO CLI Integration Tests -- Comprehensive Edge Case Suite
# ============================================================================
# Usage:
#   ./gpio_ft232h.sh <box> [out_net] [in_net]
#
# Arguments:
#   box      - Box name or IP (e.g., <YOUR-BOX>)
#   out_net  - FT232H GPIO output net (default: gpio1, pin AD4)
#   in_net   - FT232H GPIO input net  (default: gpio2, pin AD5)
#
# Prerequisites:
#   - QYF-740 vibrating motor module wired as follows:
#       VCC  -> Rigol DP821 CH1 positive (supply1 net)
#       GND  -> Rigol DP821 CH1 negative
#       IN   -> FT232H AD4 grey wire (gpio1 net)
#   - FT232H GND (black wire) tied to Rigol CH1 negative (common ground)
#   - Optional: AD5 wired for input testing (gpio2 net)
#   - Optional: AD4->AD5 jumper for loopback testing
#   - Nets configured in /etc/lager/saved_nets.json with role "gpio"
#   - Power supply net "supply1" configured for the Rigol DP821
#
# Notes:
#   - I2C and GPIO cannot use FT232H simultaneously (each claims the FTDI
#     interface). Since each CLI command is a separate process, sequential
#     commands work fine -- just don't run them in parallel.
#   - Motor should physically vibrate on HIGH, stop on LOW.
#   - Loopback tests auto-detect and skip gracefully if no jumper.
#   - Tests strip ANSI escape codes before validating output strings.
#
# Conventions:
#   track_test "pass"  = expected success
#   track_test "fail"  = unexpected failure
#   track_test "skip"  = test skipped (e.g., no loopback wire)
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
    echo "  out_net  - FT232H GPIO output net (default: gpio1, pin AD4)"
    echo "  in_net   - FT232H GPIO input net  (default: gpio2, pin AD5)"
    exit 1
fi

BOX="$1"
OUT_NET="${2:-gpio1}"
IN_NET="${3:-gpio2}"

# Loopback detection flag (set in Section 5)
LOOPBACK_AVAILABLE=false

init_harness

print_script_header "LAGER FT232H GPIO CLI TEST SUITE (Comprehensive)" "$BOX" "$OUT_NET"

echo "Output net: $OUT_NET"
echo "Input net:  $IN_NET"
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

# 0b. Set supply voltage to 3.3V
echo -n "Test: Supply voltage 3.3V... "
lager supply supply1 voltage 3.3 --yes --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 0c. Enable supply output
echo -n "Test: Supply enable... "
lager supply supply1 enable --yes --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 0d. Power settle delay
echo -n "Test: Power settle delay (2s)... "
sleep 2
track_test "pass"

# 0d. GPO help works
echo -n "Test: lager gpo --help... "
lager gpo --help >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 0e. GPI help works
echo -n "Test: lager gpi --help... "
lager gpi --help >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# ============================================================================
# 1. GPO BASIC OUTPUT -- EXIT CODES
# ============================================================================
start_section "GPO Exit Codes"
print_section_header "SECTION 1: GPO BASIC OUTPUT -- EXIT CODES"

# Every valid level keyword must succeed (exit 0)
for LVL in high low 1 0 on off; do
    echo -n "Test: GPO '$LVL' exit 0... "
    lager gpo "$OUT_NET" "$LVL" --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"
done

# ============================================================================
# 2. GPO OUTPUT MESSAGE FORMAT
# ============================================================================
start_section "GPO Message Format"
print_section_header "SECTION 2: GPO OUTPUT MESSAGE FORMAT"

# 2a. high -> "GPIO '<net>' set to HIGH"
echo -n "Test: GPO 'high' message... "
RAW=$(lager gpo "$OUT_NET" high --box "$BOX" 2>&1)
CLEAN=$(strip_ansi "$RAW")
if [ "$CLEAN" = "GPIO '$OUT_NET' set to HIGH" ]; then
    track_test "pass"
else
    echo -n "(got: '$CLEAN') "
    track_test "fail"
fi

# 2b. low -> "GPIO '<net>' set to LOW"
echo -n "Test: GPO 'low' message... "
RAW=$(lager gpo "$OUT_NET" low --box "$BOX" 2>&1)
CLEAN=$(strip_ansi "$RAW")
if [ "$CLEAN" = "GPIO '$OUT_NET' set to LOW" ]; then
    track_test "pass"
else
    echo -n "(got: '$CLEAN') "
    track_test "fail"
fi

# 2c. 1 -> "set to HIGH"
echo -n "Test: GPO '1' message contains 'set to HIGH'... "
RAW=$(lager gpo "$OUT_NET" 1 --box "$BOX" 2>&1)
CLEAN=$(strip_ansi "$RAW")
if echo "$CLEAN" | grep -q "set to HIGH"; then
    track_test "pass"
else
    echo -n "(got: '$CLEAN') "
    track_test "fail"
fi

# 2d. 0 -> "set to LOW"
echo -n "Test: GPO '0' message contains 'set to LOW'... "
RAW=$(lager gpo "$OUT_NET" 0 --box "$BOX" 2>&1)
CLEAN=$(strip_ansi "$RAW")
if echo "$CLEAN" | grep -q "set to LOW"; then
    track_test "pass"
else
    echo -n "(got: '$CLEAN') "
    track_test "fail"
fi

# 2e. on -> "set to HIGH"
echo -n "Test: GPO 'on' message contains 'set to HIGH'... "
RAW=$(lager gpo "$OUT_NET" on --box "$BOX" 2>&1)
CLEAN=$(strip_ansi "$RAW")
if echo "$CLEAN" | grep -q "set to HIGH"; then
    track_test "pass"
else
    echo -n "(got: '$CLEAN') "
    track_test "fail"
fi

# 2f. off -> "set to LOW"
echo -n "Test: GPO 'off' message contains 'set to LOW'... "
RAW=$(lager gpo "$OUT_NET" off --box "$BOX" 2>&1)
CLEAN=$(strip_ansi "$RAW")
if echo "$CLEAN" | grep -q "set to LOW"; then
    track_test "pass"
else
    echo -n "(got: '$CLEAN') "
    track_test "fail"
fi

# 2g. Case insensitive HIGH -> accepted, "set to HIGH"
# Click case_sensitive=False lowercases input, so "HIGH" -> "high" at impl level
echo -n "Test: GPO 'HIGH' (uppercase) message... "
RAW=$(lager gpo "$OUT_NET" HIGH --box "$BOX" 2>&1)
CLEAN=$(strip_ansi "$RAW")
if echo "$CLEAN" | grep -q "set to HIGH"; then
    track_test "pass"
else
    echo -n "(got: '$CLEAN') "
    track_test "fail"
fi

# ============================================================================
# 3. GPO TOGGLE BEHAVIOR & MESSAGE FORMAT
# ============================================================================
start_section "GPO Toggle"
print_section_header "SECTION 3: GPO TOGGLE BEHAVIOR & MESSAGE FORMAT"

# 3a. off -> toggle produces "toggled to HIGH"
echo -n "Test: off -> toggle -> 'toggled to HIGH'... "
lager gpo "$OUT_NET" off --box "$BOX" >/dev/null 2>&1
RAW=$(lager gpo "$OUT_NET" toggle --box "$BOX" 2>&1)
CLEAN=$(strip_ansi "$RAW")
if echo "$CLEAN" | grep -q "toggled to HIGH"; then
    track_test "pass"
else
    echo -n "(got: '$CLEAN') "
    track_test "fail"
fi

# 3b. high -> toggle produces "toggled to LOW"
echo -n "Test: high -> toggle -> 'toggled to LOW'... "
lager gpo "$OUT_NET" high --box "$BOX" >/dev/null 2>&1
RAW=$(lager gpo "$OUT_NET" toggle --box "$BOX" 2>&1)
CLEAN=$(strip_ansi "$RAW")
if echo "$CLEAN" | grep -q "toggled to LOW"; then
    track_test "pass"
else
    echo -n "(got: '$CLEAN') "
    track_test "fail"
fi

# 3c. Double toggle returns to original state
echo -n "Test: Double toggle (off -> toggle -> toggle -> LOW)... "
lager gpo "$OUT_NET" off --box "$BOX" >/dev/null 2>&1
lager gpo "$OUT_NET" toggle --box "$BOX" >/dev/null 2>&1
RAW=$(lager gpo "$OUT_NET" toggle --box "$BOX" 2>&1)
CLEAN=$(strip_ansi "$RAW")
if echo "$CLEAN" | grep -q "toggled to LOW"; then
    track_test "pass"
else
    echo -n "(got: '$CLEAN') "
    track_test "fail"
fi

# 3d. Toggle message includes net name
echo -n "Test: Toggle message includes net name '$OUT_NET'... "
lager gpo "$OUT_NET" off --box "$BOX" >/dev/null 2>&1
RAW=$(lager gpo "$OUT_NET" toggle --box "$BOX" 2>&1)
CLEAN=$(strip_ansi "$RAW")
if echo "$CLEAN" | grep -q "GPIO '$OUT_NET' toggled to"; then
    track_test "pass"
else
    echo -n "(got: '$CLEAN') "
    track_test "fail"
fi

# 3e. 10 toggles from off: even count -> final output "toggled to LOW"
echo -n "Test: 10 toggles from off (final = LOW)... "
lager gpo "$OUT_NET" off --box "$BOX" >/dev/null 2>&1
TOGGLE_OK=true
for i in $(seq 1 9); do
    lager gpo "$OUT_NET" toggle --box "$BOX" >/dev/null 2>&1
    if [ $? -ne 0 ]; then
        TOGGLE_OK=false
        break
    fi
done
if [ "$TOGGLE_OK" = true ]; then
    RAW=$(lager gpo "$OUT_NET" toggle --box "$BOX" 2>&1)
    CLEAN=$(strip_ansi "$RAW")
    if echo "$CLEAN" | grep -q "toggled to LOW"; then
        track_test "pass"
    else
        echo -n "(got: '$CLEAN') "
        track_test "fail"
    fi
else
    track_test "fail"
fi

# 3f. Toggle after explicit 1
echo -n "Test: '1' -> toggle -> 'toggled to LOW'... "
lager gpo "$OUT_NET" 1 --box "$BOX" >/dev/null 2>&1
RAW=$(lager gpo "$OUT_NET" toggle --box "$BOX" 2>&1)
CLEAN=$(strip_ansi "$RAW")
if echo "$CLEAN" | grep -q "toggled to LOW"; then
    track_test "pass"
else
    echo -n "(got: '$CLEAN') "
    track_test "fail"
fi

# ============================================================================
# 4. GPI BASIC INPUT
# ============================================================================
start_section "GPI Basic Input"
print_section_header "SECTION 4: GPI BASIC INPUT"

# NOTE: Basic GPI tests use OUT_NET (the output pin) since a separate input
# net may not be configured. The output pin can be read back after being set.

# 4a. Read returns a value matching format
echo -n "Test: GPI read format 'GPIO ...: (HIGH|LOW) (0|1)'... "
RAW=$(lager gpi "$OUT_NET" --box "$BOX" 2>&1)
CLEAN=$(strip_ansi "$RAW")
if echo "$CLEAN" | grep -qE "GPIO '$OUT_NET': (HIGH|LOW) \([01]\)"; then
    track_test "pass"
else
    echo -n "(got: '$CLEAN') "
    track_test "fail"
fi

# 4b. Exact format validation after ANSI strip
echo -n "Test: GPI exact format match... "
RAW=$(lager gpi "$OUT_NET" --box "$BOX" 2>&1)
CLEAN=$(strip_ansi "$RAW")
if [ "$CLEAN" = "GPIO '$OUT_NET': HIGH (1)" ] || [ "$CLEAN" = "GPIO '$OUT_NET': LOW (0)" ]; then
    track_test "pass"
else
    echo -n "(got: '$CLEAN') "
    track_test "fail"
fi

# 4c. 3x consecutive read consistency (all succeed)
echo -n "Test: GPI 3x consecutive reads succeed... "
READ_OK=true
for i in 1 2 3; do
    lager gpi "$OUT_NET" --box "$BOX" >/dev/null 2>&1
    if [ $? -ne 0 ]; then
        READ_OK=false
        break
    fi
done
if [ "$READ_OK" = true ]; then
    track_test "pass"
else
    track_test "fail"
fi

# 4d. Read output net after setting high -> "HIGH (1)"
# The cache-restore in _ensure_open() should make the pin read back HIGH
echo -n "Test: GPI read output net after high -> HIGH (1)... "
lager gpo "$OUT_NET" high --box "$BOX" >/dev/null 2>&1
RAW=$(lager gpi "$OUT_NET" --box "$BOX" 2>&1)
CLEAN=$(strip_ansi "$RAW")
if echo "$CLEAN" | grep -q "HIGH (1)"; then
    track_test "pass"
else
    echo -n "(got: '$CLEAN') "
    track_test "fail"
fi

# 4e. Read output net after setting low -> "LOW (0)"
echo -n "Test: GPI read output net after low -> LOW (0)... "
lager gpo "$OUT_NET" low --box "$BOX" >/dev/null 2>&1
RAW=$(lager gpi "$OUT_NET" --box "$BOX" 2>&1)
CLEAN=$(strip_ansi "$RAW")
if echo "$CLEAN" | grep -q "LOW (0)"; then
    track_test "pass"
else
    echo -n "(got: '$CLEAN') "
    track_test "fail"
fi

# ============================================================================
# 5. LOOPBACK ROUND-TRIP (AD4 output -> AD5 input with jumper)
# ============================================================================
start_section "Loopback Round-Trip"
print_section_header "SECTION 5: LOOPBACK ROUND-TRIP"

echo "  (Requires AD4->AD5 jumper wire for loopback)"
echo ""

# 5a. Detect loopback: set high, read input -- if HIGH, loopback exists
echo -n "Test: Loopback detection (set high, read input)... "
lager gpo "$OUT_NET" high --box "$BOX" >/dev/null 2>&1
sleep 0.2
RAW=$(lager gpi "$IN_NET" --box "$BOX" 2>&1)
CLEAN=$(strip_ansi "$RAW")
if echo "$CLEAN" | grep -q "HIGH (1)"; then
    LOOPBACK_AVAILABLE=true
    track_test "pass"
    echo "  Loopback detected -- remaining loopback tests will run"
else
    LOOPBACK_AVAILABLE=false
    track_test "skip"
    echo "  No loopback detected -- remaining loopback tests will skip"
fi

# Helper: skip loopback test if not available
run_loopback_test() {
    local test_name="$1"
    if [ "$LOOPBACK_AVAILABLE" = false ]; then
        echo -n "Test: $test_name... "
        track_test "skip"
        return 1
    fi
    return 0
}

# 5b. Set high, read input -> HIGH (1)
if run_loopback_test "Loopback high -> read HIGH (1)"; then
    echo -n "Test: Loopback high -> read HIGH (1)... "
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
fi

# 5c. Set low, read input -> LOW (0)
if run_loopback_test "Loopback low -> read LOW (0)"; then
    echo -n "Test: Loopback low -> read LOW (0)... "
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
fi

# 5d. Toggle then verify via input
if run_loopback_test "Loopback toggle -> read"; then
    echo -n "Test: Loopback toggle -> read HIGH (1)... "
    lager gpo "$OUT_NET" low --box "$BOX" >/dev/null 2>&1
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
fi

# 5e. 3x consistent reads for high
if run_loopback_test "Loopback high, 3x consistent"; then
    echo -n "Test: Loopback high, 3x consistent... "
    lager gpo "$OUT_NET" high --box "$BOX" >/dev/null 2>&1
    sleep 0.2
    READ3_OK=true
    for i in 1 2 3; do
        RAW=$(lager gpi "$IN_NET" --box "$BOX" 2>&1)
        CLEAN=$(strip_ansi "$RAW")
        if ! echo "$CLEAN" | grep -q "HIGH (1)"; then
            READ3_OK=false
            break
        fi
    done
    if [ "$READ3_OK" = true ]; then
        track_test "pass"
    else
        echo -n "(got: '$CLEAN') "
        track_test "fail"
    fi
fi

# 5f. 3x consistent reads for low
if run_loopback_test "Loopback low, 3x consistent"; then
    echo -n "Test: Loopback low, 3x consistent... "
    lager gpo "$OUT_NET" low --box "$BOX" >/dev/null 2>&1
    sleep 0.2
    READ3L_OK=true
    for i in 1 2 3; do
        RAW=$(lager gpi "$IN_NET" --box "$BOX" 2>&1)
        CLEAN=$(strip_ansi "$RAW")
        if ! echo "$CLEAN" | grep -q "LOW (0)"; then
            READ3L_OK=false
            break
        fi
    done
    if [ "$READ3L_OK" = true ]; then
        track_test "pass"
    else
        echo -n "(got: '$CLEAN') "
        track_test "fail"
    fi
fi

# 5g. Exact value (1) validation on high
if run_loopback_test "Loopback exact value (1)"; then
    echo -n "Test: Loopback exact value (1)... "
    lager gpo "$OUT_NET" high --box "$BOX" >/dev/null 2>&1
    sleep 0.2
    RAW=$(lager gpi "$IN_NET" --box "$BOX" 2>&1)
    CLEAN=$(strip_ansi "$RAW")
    if echo "$CLEAN" | grep -q "(1)"; then
        track_test "pass"
    else
        echo -n "(got: '$CLEAN') "
        track_test "fail"
    fi
fi

# 5h. Rapid toggle 5x with read after each
if run_loopback_test "Loopback rapid toggle 5x"; then
    echo -n "Test: Loopback rapid toggle 5x with read... "
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
fi

# ============================================================================
# 6. CLI ARGUMENT PLACEMENT
# ============================================================================
start_section "CLI Arg Placement"
print_section_header "SECTION 6: CLI ARGUMENT PLACEMENT"

# 6a. --box before netname (GPO)
echo -n "Test: GPO --box before netname... "
lager gpo --box "$BOX" "$OUT_NET" low >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 6b. --box before netname (GPI)
echo -n "Test: GPI --box before netname... "
lager gpi --box "$BOX" "$OUT_NET" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 6c. --box after level
echo -n "Test: GPO --box after level... "
lager gpo "$OUT_NET" low --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 6d. --box after netname (GPI)
echo -n "Test: GPI netname --box after... "
lager gpi "$OUT_NET" --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 6e. Mixed case level On
echo -n "Test: GPO mixed case 'On'... "
lager gpo "$OUT_NET" On --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# ============================================================================
# 7. ERROR CASES
# ============================================================================
start_section "Error Cases"
print_section_header "SECTION 7: ERROR CASES"

# 7a. GPO invalid net -> nonzero exit
echo -n "Test: GPO invalid net NONEXISTENT -> nonzero exit... "
lager gpo NONEXISTENT high --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 7b. GPO invalid net -> stderr contains error text
echo -n "Test: GPO invalid net -> stderr has error msg... "
STDERR=$(lager gpo NONEXISTENT high --box "$BOX" 2>&1)
if echo "$STDERR" | grep -qiE "(not found|error|invalid|no .* net)"; then
    track_test "pass"
else
    echo -n "(got: '$STDERR') "
    track_test "fail"
fi

# 7c. GPI invalid net -> nonzero exit
echo -n "Test: GPI invalid net NONEXISTENT -> nonzero exit... "
lager gpi NONEXISTENT --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 7d. GPI invalid net -> stderr contains error text
echo -n "Test: GPI invalid net -> stderr has error msg... "
STDERR=$(lager gpi NONEXISTENT --box "$BOX" 2>&1)
if echo "$STDERR" | grep -qiE "(not found|error|invalid|no .* net)"; then
    track_test "pass"
else
    echo -n "(got: '$STDERR') "
    track_test "fail"
fi

# 7e. GPO missing level -> nonzero exit
echo -n "Test: GPO missing level -> nonzero exit... "
lager gpo "$OUT_NET" --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 7f. GPO missing level -> stderr has error message
echo -n "Test: GPO missing level -> stderr msg... "
STDERR=$(lager gpo "$OUT_NET" --box "$BOX" 2>&1)
if echo "$STDERR" | grep -qiE "(level|required|usage|error)"; then
    track_test "pass"
else
    echo -n "(got: '$STDERR') "
    track_test "fail"
fi

# 7g. GPO invalid level 'banana' -> nonzero exit (Click rejects)
echo -n "Test: GPO invalid level 'banana' -> nonzero exit... "
lager gpo "$OUT_NET" banana --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 7h. GPO invalid box -> nonzero exit
echo -n "Test: GPO invalid box FAKEBOX_999 -> nonzero exit... "
lager gpo "$OUT_NET" high --box FAKEBOX_999 >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# ============================================================================
# 8. STRESS / SEQUENTIAL RELIABILITY
# ============================================================================
start_section "Stress Tests"
print_section_header "SECTION 8: STRESS / SEQUENTIAL RELIABILITY"

# 8a. 20 sequential GPO commands (high/low cycle)
echo -n "Test: 20 sequential GPO high/low cycles... "
STRESS_OK=true
for i in $(seq 1 10); do
    lager gpo "$OUT_NET" high --box "$BOX" >/dev/null 2>&1
    if [ $? -ne 0 ]; then STRESS_OK=false; break; fi
    lager gpo "$OUT_NET" low --box "$BOX" >/dev/null 2>&1
    if [ $? -ne 0 ]; then STRESS_OK=false; break; fi
done
if [ "$STRESS_OK" = true ]; then
    track_test "pass"
else
    track_test "fail"
fi

# 8b. 10 sequential GPI reads
echo -n "Test: 10 sequential GPI reads... "
READ_STRESS_OK=true
for i in $(seq 1 10); do
    lager gpi "$OUT_NET" --box "$BOX" >/dev/null 2>&1
    if [ $? -ne 0 ]; then
        READ_STRESS_OK=false
        break
    fi
done
if [ "$READ_STRESS_OK" = true ]; then
    track_test "pass"
else
    track_test "fail"
fi

# 8c. 10 interleaved GPO/GPI commands
echo -n "Test: 10 interleaved GPO/GPI commands... "
INTERLEAVE_OK=true
for i in $(seq 1 5); do
    lager gpo "$OUT_NET" high --box "$BOX" >/dev/null 2>&1
    if [ $? -ne 0 ]; then INTERLEAVE_OK=false; break; fi
    lager gpi "$OUT_NET" --box "$BOX" >/dev/null 2>&1
    if [ $? -ne 0 ]; then INTERLEAVE_OK=false; break; fi
    lager gpo "$OUT_NET" low --box "$BOX" >/dev/null 2>&1
    if [ $? -ne 0 ]; then INTERLEAVE_OK=false; break; fi
    lager gpi "$OUT_NET" --box "$BOX" >/dev/null 2>&1
    if [ $? -ne 0 ]; then INTERLEAVE_OK=false; break; fi
done
if [ "$INTERLEAVE_OK" = true ]; then
    track_test "pass"
else
    track_test "fail"
fi

# ============================================================================
# 9. TOGGLE STATE PERSISTENCE (cross-process cache)
# ============================================================================
start_section "Toggle Persistence"
print_section_header "SECTION 9: TOGGLE STATE PERSISTENCE"

# 9a. off -> toggle -> HIGH (cross-process)
echo -n "Test: off -> toggle -> HIGH... "
lager gpo "$OUT_NET" off --box "$BOX" >/dev/null 2>&1
RAW=$(lager gpo "$OUT_NET" toggle --box "$BOX" 2>&1)
CLEAN=$(strip_ansi "$RAW")
if echo "$CLEAN" | grep -q "toggled to HIGH"; then
    track_test "pass"
else
    echo -n "(got: '$CLEAN') "
    track_test "fail"
fi

# 9b. toggle again -> LOW (state persisted from 9a)
echo -n "Test: toggle again -> LOW... "
RAW=$(lager gpo "$OUT_NET" toggle --box "$BOX" 2>&1)
CLEAN=$(strip_ansi "$RAW")
if echo "$CLEAN" | grep -q "toggled to LOW"; then
    track_test "pass"
else
    echo -n "(got: '$CLEAN') "
    track_test "fail"
fi

# 9c. Explicit high -> toggle -> LOW
echo -n "Test: high -> toggle -> LOW... "
lager gpo "$OUT_NET" high --box "$BOX" >/dev/null 2>&1
RAW=$(lager gpo "$OUT_NET" toggle --box "$BOX" 2>&1)
CLEAN=$(strip_ansi "$RAW")
if echo "$CLEAN" | grep -q "toggled to LOW"; then
    track_test "pass"
else
    echo -n "(got: '$CLEAN') "
    track_test "fail"
fi

# 9d. Explicit low -> toggle -> HIGH
echo -n "Test: low -> toggle -> HIGH... "
lager gpo "$OUT_NET" low --box "$BOX" >/dev/null 2>&1
RAW=$(lager gpo "$OUT_NET" toggle --box "$BOX" 2>&1)
CLEAN=$(strip_ansi "$RAW")
if echo "$CLEAN" | grep -q "toggled to HIGH"; then
    track_test "pass"
else
    echo -n "(got: '$CLEAN') "
    track_test "fail"
fi

# 9e. 10 toggles alternating from known state (off)
echo -n "Test: 10 toggles alternating from off... "
lager gpo "$OUT_NET" off --box "$BOX" >/dev/null 2>&1
TOGGLE10_OK=true
EXPECTED_STATES=("HIGH" "LOW" "HIGH" "LOW" "HIGH" "LOW" "HIGH" "LOW" "HIGH" "LOW")
for i in $(seq 0 9); do
    RAW=$(lager gpo "$OUT_NET" toggle --box "$BOX" 2>&1)
    CLEAN=$(strip_ansi "$RAW")
    EXPECTED="${EXPECTED_STATES[$i]}"
    if ! echo "$CLEAN" | grep -q "toggled to $EXPECTED"; then
        echo -n "(toggle $((i+1)): expected $EXPECTED, got '$CLEAN') "
        TOGGLE10_OK=false
        break
    fi
done
if [ "$TOGGLE10_OK" = true ]; then
    track_test "pass"
else
    track_test "fail"
fi

# ============================================================================
# 10. CLEANUP
# ============================================================================
start_section "Cleanup"
print_section_header "SECTION 10: CLEANUP"

# 10a. Set output low (motor off)
echo -n "Test: Set output low (motor off)... "
lager gpo "$OUT_NET" low --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 10b. Disable supply (power off motor)
echo -n "Test: Supply disable... "
lager supply supply1 disable --yes --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 10c. Final connectivity check
echo -n "Test: Final connectivity check... "
lager hello --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# ============================================================================
# SUMMARY
# ============================================================================
print_summary
exit_with_status
