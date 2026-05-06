#!/usr/bin/env bash

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
# ============================================================================
# Aardvark GPIO CLI Integration Tests -- Comprehensive Edge Case Suite
# ============================================================================
# Usage:
#   ./gpio_aardvark.sh <box> [out_net] [in_net] [adc_net]
#
# Arguments:
#   box      - Box name or IP (e.g., <YOUR-BOX>)
#   out_net  - Aardvark GPIO output net (default: gpio4, pin 7 / SCK)
#   in_net   - Aardvark GPIO input net  (default: gpio5, pin 8 / MOSI)
#   adc_net  - LabJack ADC net for voltage verification (default: adc1)
#
# Prerequisites:
#   - Total Phase Aardvark I2C/SPI Host Adapter connected to box
#   - Nets configured in /etc/lager/saved_nets.json with role "gpio"
#     and instrument matching "aardvark" or "totalphase"
#   - Optional: Loopback wire from out_net pin to in_net pin
#   - Optional: out_net pin wired to LabJack AIN0 for ADC verification
#
# Wiring (Config C -- full test):
#   Aardvark Pin 7  (SCK/gpio4)  ---+---> LabJack AIN0 (adc1)
#                                   |
#   Aardvark Pin 8  (MOSI/gpio5) <--+     (loopback Y-splice)
#   Aardvark Pin 10 (GND)        -------> LabJack GND
#
# Notes:
#   - Loopback and ADC tests auto-detect and skip gracefully.
#   - Tests strip ANSI escape codes before validating output strings.
#   - Hold tests use background process + kill pattern.
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
    echo "Usage: $0 <box> [out_net] [in_net] [adc_net]"
    echo ""
    echo "  box      - Box name or IP (e.g., <YOUR-BOX>)"
    echo "  out_net  - Aardvark GPIO output net (default: gpio4, pin 7 / SCK)"
    echo "  in_net   - Aardvark GPIO input net  (default: gpio5, pin 8 / MOSI)"
    echo "  adc_net  - LabJack ADC net for voltage verification (default: adc1)"
    echo ""
    echo "Examples:"
    echo "  $0 <YOUR-BOX>                          # Single pin only"
    echo "  $0 <YOUR-BOX> gpio4 gpio5              # With loopback"
    echo "  $0 <YOUR-BOX> gpio4 gpio5 adc1         # Full test (loopback + ADC)"
    exit 1
fi

BOX="$1"
OUT_NET="${2:-gpio4}"
IN_NET="${3:-gpio5}"
ADC_NET="${4:-adc1}"

# Feature detection flags (set during auto-detection sections)
LOOPBACK_AVAILABLE=false
ADC_AVAILABLE=false

init_harness

print_script_header "LAGER AARDVARK GPIO CLI TEST SUITE (Comprehensive)" "$BOX" "$OUT_NET"

echo "Output net: $OUT_NET"
echo "Input net:  $IN_NET"
echo "ADC net:    $ADC_NET"
echo ""

# ============================================================================
# Helpers
# ============================================================================

# Strip ANSI escape codes from a string
strip_ansi() {
    echo "$1" | sed $'s/\033\[[0-9;]*m//g'
}

# Extract numeric voltage from ADC output (e.g., "3.312 V" -> "3.312")
extract_voltage() {
    echo "$1" | grep -oE '[0-9]+\.[0-9]+' | head -1
}

# Compare voltage: returns 0 if $1 >= $2 (using awk for float comparison)
voltage_gte() {
    awk "BEGIN { exit !($1 >= $2) }"
}

# Compare voltage: returns 0 if $1 <= $2
voltage_lte() {
    awk "BEGIN { exit !($1 <= $2) }"
}

# ============================================================================
# 0. PREREQUISITES
# ============================================================================
start_section "Prerequisites"
print_section_header "SECTION 0: PREREQUISITES"

# 0a. Verify box connectivity
echo -n "Test: Box connectivity... "
lager hello --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 0b. GPO help works
echo -n "Test: lager gpo --help... "
lager gpo --help >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 0c. GPI help works
echo -n "Test: lager gpi --help... "
lager gpi --help >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 0d. GPO net help works
echo -n "Test: lager gpo $OUT_NET --help... "
lager gpo "$OUT_NET" --help --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 0e. GPI net help works
echo -n "Test: lager gpi $OUT_NET --help... "
lager gpi "$OUT_NET" --help --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

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
# 5. LOOPBACK ROUND-TRIP (out_net -> in_net with jumper)
# ============================================================================
start_section "Loopback Round-Trip"
print_section_header "SECTION 5: LOOPBACK ROUND-TRIP"

echo "  (Requires ${OUT_NET} -> ${IN_NET} jumper wire for loopback)"
echo ""

# 5a. Detect loopback: set high, read input -- if HIGH, loopback exists
echo -n "Test: Loopback detection (set high, read input)... "
lager gpo "$OUT_NET" high --box "$BOX" >/dev/null 2>&1
sleep 0.3
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
# 6. ADC VOLTAGE VERIFICATION (out_net -> LabJack AIN0)
# ============================================================================
start_section "ADC Voltage Verify"
print_section_header "SECTION 6: ADC VOLTAGE VERIFICATION"

echo "  (Requires ${OUT_NET} wired to LabJack AIN0 / ${ADC_NET})"
echo ""

# ADC tests use --hold mode to keep the Aardvark driving the GPIO pin while
# the LabJack ADC reads the voltage.  Without --hold, the gpo process exits
# and closes the Aardvark handle, causing the GPIO pin to float before the
# ADC read happens (the adc command is a separate process using the LabJack).

# Helper: set GPIO with hold, read ADC, kill hold, return voltage
# Usage: adc_with_hold <level>    sets VOLTS_RESULT
adc_with_hold() {
    local level="$1"
    lager gpo "$OUT_NET" "$level" --hold --box "$BOX" >/dev/null 2>&1 &
    local hold_pid=$!
    sleep 3
    local raw
    raw=$(lager adc "$ADC_NET" --box "$BOX" 2>&1)
    local clean
    clean=$(strip_ansi "$raw")
    VOLTS_RESULT=$(extract_voltage "$clean")
    kill "$hold_pid" 2>/dev/null
    wait "$hold_pid" 2>/dev/null
    sleep 3
}

# 6a. Detect ADC: two-phase check using --hold mode.
#      The ADC is only "connected to GPIO" if HIGH reads 2.5-5.0V AND LOW reads < 0.5V.
echo -n "Test: ADC detection (hold HIGH then hold LOW)... "
adc_with_hold "high"
VOLTS_HIGH="$VOLTS_RESULT"
adc_with_hold "low"
VOLTS_LOW="$VOLTS_RESULT"
if [ -n "$VOLTS_HIGH" ] && [ -n "$VOLTS_LOW" ] \
   && voltage_gte "$VOLTS_HIGH" "2.5" && voltage_lte "$VOLTS_HIGH" "5.0" \
   && voltage_lte "$VOLTS_LOW" "0.5"; then
    ADC_AVAILABLE=true
    track_test "pass"
    echo "  ADC detected (HIGH=${VOLTS_HIGH}V, LOW=${VOLTS_LOW}V) -- remaining ADC tests will run"
else
    ADC_AVAILABLE=false
    track_test "skip"
    if [ -n "$VOLTS_HIGH" ]; then
        echo "  ADC not tracking GPIO (HIGH=${VOLTS_HIGH}V, LOW=${VOLTS_LOW}V) -- check wiring"
    else
        echo "  ADC not available -- remaining ADC tests will skip"
    fi
fi

# Helper: skip ADC test if not available
run_adc_test() {
    local test_name="$1"
    if [ "$ADC_AVAILABLE" = false ]; then
        echo -n "Test: $test_name... "
        track_test "skip"
        return 1
    fi
    return 0
}

# 6b. HIGH = ~3.3V (expect 3.0 - 3.5V)
if run_adc_test "Hold HIGH -> ADC 3.0-3.5V"; then
    echo -n "Test: Hold HIGH -> ADC 3.0-3.5V... "
    adc_with_hold "high"
    VOLTS="$VOLTS_RESULT"
    if [ -n "$VOLTS" ] && voltage_gte "$VOLTS" "3.0" && voltage_lte "$VOLTS" "3.5"; then
        track_test "pass"
    else
        echo -n "(got: ${VOLTS}V) "
        track_test "fail"
    fi
fi

# 6c. LOW = ~0V (expect < 0.2V)
if run_adc_test "Hold LOW -> ADC < 0.2V"; then
    echo -n "Test: Hold LOW -> ADC < 0.2V... "
    adc_with_hold "low"
    VOLTS="$VOLTS_RESULT"
    if [ -n "$VOLTS" ] && voltage_lte "$VOLTS" "0.2"; then
        track_test "pass"
    else
        echo -n "(got: ${VOLTS}V) "
        track_test "fail"
    fi
fi

# 6d. Toggle HIGH -> ADC > 3.0V (set low first, toggle to high, hold)
if run_adc_test "Toggle HIGH -> hold -> ADC > 3.0V"; then
    echo -n "Test: Toggle HIGH -> hold -> ADC > 3.0V... "
    # Set low first (non-hold to update cache), then hold high
    lager gpo "$OUT_NET" low --box "$BOX" >/dev/null 2>&1
    sleep 1
    adc_with_hold "high"
    VOLTS="$VOLTS_RESULT"
    if [ -n "$VOLTS" ] && voltage_gte "$VOLTS" "3.0"; then
        track_test "pass"
    else
        echo -n "(got: ${VOLTS}V) "
        track_test "fail"
    fi
fi

# 6e. Toggle LOW -> ADC < 0.2V
if run_adc_test "Toggle LOW -> hold -> ADC < 0.2V"; then
    echo -n "Test: Toggle LOW -> hold -> ADC < 0.2V... "
    lager gpo "$OUT_NET" high --box "$BOX" >/dev/null 2>&1
    sleep 1
    adc_with_hold "low"
    VOLTS="$VOLTS_RESULT"
    if [ -n "$VOLTS" ] && voltage_lte "$VOLTS" "0.2"; then
        track_test "pass"
    else
        echo -n "(got: ${VOLTS}V) "
        track_test "fail"
    fi
fi

# 6f. 3x HIGH/LOW cycle verified with ADC (using hold)
if run_adc_test "3x HIGH/LOW ADC cycle"; then
    echo -n "Test: 3x HIGH/LOW ADC cycle... "
    CYCLE_OK=true
    for i in 1 2 3; do
        adc_with_hold "high"
        VOLTS="$VOLTS_RESULT"
        if [ -z "$VOLTS" ] || ! voltage_gte "$VOLTS" "3.0"; then
            echo -n "(cycle $i HIGH: got ${VOLTS}V) "
            CYCLE_OK=false
            break
        fi
        adc_with_hold "low"
        VOLTS="$VOLTS_RESULT"
        if [ -z "$VOLTS" ] || ! voltage_lte "$VOLTS" "0.2"; then
            echo -n "(cycle $i LOW: got ${VOLTS}V) "
            CYCLE_OK=false
            break
        fi
    done
    if [ "$CYCLE_OK" = true ]; then
        track_test "pass"
    else
        track_test "fail"
    fi
fi

# Wait for Aardvark to become available (retry up to 5 times)
wait_for_aardvark() {
    local attempts=5
    for i in $(seq 1 $attempts); do
        if lager gpo "$OUT_NET" low --box "$BOX" >/dev/null 2>&1; then
            return 0
        fi
        sleep 2
    done
    echo "  WARNING: Aardvark may still be locked after $attempts attempts"
    return 1
}

# Ensure Aardvark is available after hold-mode ADC tests
wait_for_aardvark

# ============================================================================
# 7. MULTI-PIN TESTS
# ============================================================================
start_section "Multi-Pin Tests"
print_section_header "SECTION 7: MULTI-PIN TESTS"

# 7a. OUT_NET high + cached readback
echo -n "Test: $OUT_NET high + readback... "
lager gpo "$OUT_NET" high --box "$BOX" >/dev/null 2>&1
RAW=$(lager gpi "$OUT_NET" --box "$BOX" 2>&1)
CLEAN=$(strip_ansi "$RAW")
if echo "$CLEAN" | grep -q "HIGH (1)"; then
    track_test "pass"
else
    echo -n "(got: '$CLEAN') "
    track_test "fail"
fi

# 7b. OUT_NET low + cached readback
echo -n "Test: $OUT_NET low + readback... "
lager gpo "$OUT_NET" low --box "$BOX" >/dev/null 2>&1
RAW=$(lager gpi "$OUT_NET" --box "$BOX" 2>&1)
CLEAN=$(strip_ansi "$RAW")
if echo "$CLEAN" | grep -q "LOW (0)"; then
    track_test "pass"
else
    echo -n "(got: '$CLEAN') "
    track_test "fail"
fi

# 7c-7f. Test IN_NET if it is different from OUT_NET
if [ "$IN_NET" != "$OUT_NET" ]; then
    # 7c. IN_NET high + cached readback
    echo -n "Test: $IN_NET high + readback... "
    lager gpo "$IN_NET" high --box "$BOX" >/dev/null 2>&1
    RAW=$(lager gpi "$IN_NET" --box "$BOX" 2>&1)
    CLEAN=$(strip_ansi "$RAW")
    if echo "$CLEAN" | grep -q "HIGH (1)"; then
        track_test "pass"
    else
        echo -n "(got: '$CLEAN') "
        track_test "fail"
    fi

    # 7d. IN_NET low + cached readback
    echo -n "Test: $IN_NET low + readback... "
    lager gpo "$IN_NET" low --box "$BOX" >/dev/null 2>&1
    RAW=$(lager gpi "$IN_NET" --box "$BOX" 2>&1)
    CLEAN=$(strip_ansi "$RAW")
    if echo "$CLEAN" | grep -q "LOW (0)"; then
        track_test "pass"
    else
        echo -n "(got: '$CLEAN') "
        track_test "fail"
    fi
else
    skip_test "$IN_NET high + readback" "IN_NET same as OUT_NET"
    skip_test "$IN_NET low + readback" "IN_NET same as OUT_NET"
fi

# 7e-7f. Test gpio6 if available (auto-detect)
echo -n "Test: gpio6 availability check... "
lager gpo gpio6 high --box "$BOX" >/dev/null 2>&1
if [ $? -eq 0 ]; then
    track_test "pass"
    GPIO6_AVAILABLE=true
else
    track_test "skip"
    GPIO6_AVAILABLE=false
    echo "  gpio6 not configured -- skipping gpio6 tests"
fi

if [ "$GPIO6_AVAILABLE" = true ]; then
    echo -n "Test: gpio6 low + readback... "
    lager gpo gpio6 low --box "$BOX" >/dev/null 2>&1
    RAW=$(lager gpi gpio6 --box "$BOX" 2>&1)
    CLEAN=$(strip_ansi "$RAW")
    if echo "$CLEAN" | grep -q "LOW (0)"; then
        track_test "pass"
    else
        echo -n "(got: '$CLEAN') "
        track_test "fail"
    fi
else
    skip_test "gpio6 low + readback" "gpio6 not available"
fi

# ============================================================================
# 8. CLI ARGUMENT PLACEMENT
# ============================================================================
start_section "CLI Arg Placement"
print_section_header "SECTION 8: CLI ARGUMENT PLACEMENT"

# 8a. --box before netname (GPO)
echo -n "Test: GPO --box before netname... "
lager gpo --box "$BOX" "$OUT_NET" low >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 8b. --box before netname (GPI)
echo -n "Test: GPI --box before netname... "
lager gpi --box "$BOX" "$OUT_NET" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 8c. --box after level
echo -n "Test: GPO --box after level... "
lager gpo "$OUT_NET" low --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 8d. --box after netname (GPI)
echo -n "Test: GPI netname --box after... "
lager gpi "$OUT_NET" --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 8e. Mixed case level On
echo -n "Test: GPO mixed case 'On'... "
lager gpo "$OUT_NET" On --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# ============================================================================
# 9. ERROR CASES
# ============================================================================
start_section "Error Cases"
print_section_header "SECTION 9: ERROR CASES"

# 9a. GPO invalid net -> nonzero exit
echo -n "Test: GPO invalid net NONEXISTENT -> nonzero exit... "
lager gpo NONEXISTENT high --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 9b. GPO invalid net -> stderr contains error text
echo -n "Test: GPO invalid net -> stderr has error msg... "
STDERR=$(lager gpo NONEXISTENT high --box "$BOX" 2>&1)
if echo "$STDERR" | grep -qiE "(not found|error|invalid|no .* net)"; then
    track_test "pass"
else
    echo -n "(got: '$STDERR') "
    track_test "fail"
fi

# 9c. GPI invalid net -> nonzero exit
echo -n "Test: GPI invalid net NONEXISTENT -> nonzero exit... "
lager gpi NONEXISTENT --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 9d. GPI invalid net -> stderr contains error text
echo -n "Test: GPI invalid net -> stderr has error msg... "
STDERR=$(lager gpi NONEXISTENT --box "$BOX" 2>&1)
if echo "$STDERR" | grep -qiE "(not found|error|invalid|no .* net)"; then
    track_test "pass"
else
    echo -n "(got: '$STDERR') "
    track_test "fail"
fi

# 9e. GPO missing level -> nonzero exit
echo -n "Test: GPO missing level -> nonzero exit... "
lager gpo "$OUT_NET" --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 9f. GPO missing level -> stderr has error message
echo -n "Test: GPO missing level -> stderr msg... "
STDERR=$(lager gpo "$OUT_NET" --box "$BOX" 2>&1)
if echo "$STDERR" | grep -qiE "(level|required|usage|error)"; then
    track_test "pass"
else
    echo -n "(got: '$STDERR') "
    track_test "fail"
fi

# 9g. GPO invalid level 'banana' -> nonzero exit
echo -n "Test: GPO invalid level 'banana' -> nonzero exit... "
lager gpo "$OUT_NET" banana --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 9h. GPO invalid box -> nonzero exit
echo -n "Test: GPO invalid box FAKEBOX_999 -> nonzero exit... "
lager gpo "$OUT_NET" high --box FAKEBOX_999 >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# ============================================================================
# 10. STRESS / SEQUENTIAL RELIABILITY
# ============================================================================
start_section "Stress Tests"
print_section_header "SECTION 10: STRESS / SEQUENTIAL RELIABILITY"

# 10a. 20 sequential GPO commands (high/low cycle)
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

# 10b. 10 sequential GPI reads
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

# 10c. 10 interleaved GPO/GPI commands
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
# 11. TOGGLE STATE PERSISTENCE (cross-process cache)
# ============================================================================
start_section "Toggle Persistence"
print_section_header "SECTION 11: TOGGLE STATE PERSISTENCE"

# 11a. off -> toggle -> HIGH (cross-process)
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

# 11b. toggle again -> LOW (state persisted from 11a)
echo -n "Test: toggle again -> LOW... "
RAW=$(lager gpo "$OUT_NET" toggle --box "$BOX" 2>&1)
CLEAN=$(strip_ansi "$RAW")
if echo "$CLEAN" | grep -q "toggled to LOW"; then
    track_test "pass"
else
    echo -n "(got: '$CLEAN') "
    track_test "fail"
fi

# 11c. Explicit high -> toggle -> LOW
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

# 11d. Explicit low -> toggle -> HIGH
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

# 11e. 10 toggles alternating from known state (off)
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
# 12. HOLD MODE
# ============================================================================
start_section "Hold Mode"
print_section_header "SECTION 12: HOLD MODE"

# Helper: kill hold process and wait for remote Aardvark release
kill_hold() {
    local pid="$1"
    kill "$pid" 2>/dev/null
    wait "$pid" 2>/dev/null
    # Wait for the remote box-side process to release the Aardvark USB device.
    # Killing the local CLI process disconnects the stream, but the remote
    # process may take a moment to exit and close the Aardvark handle.
    sleep 3
}

# 12a. Hold high: start in background, verify pin, kill
echo -n "Test: Hold high (background + verify + kill)... "
lager gpo "$OUT_NET" high --hold --box "$BOX" >/dev/null 2>&1 &
HOLD_PID=$!
sleep 3
# Verify pin is held high (cached readback)
RAW=$(lager gpi "$OUT_NET" --box "$BOX" 2>&1)
CLEAN=$(strip_ansi "$RAW")
# Kill the hold process and wait for remote cleanup
kill_hold $HOLD_PID
if echo "$CLEAN" | grep -q "HIGH (1)"; then
    track_test "pass"
else
    echo -n "(got: '$CLEAN') "
    track_test "fail"
fi

# Reset state between hold tests: non-hold GPO to flush cache
lager gpo "$OUT_NET" low --box "$BOX" >/dev/null 2>&1
sleep 1

# 12b. Hold low: start in background, verify pin, kill
echo -n "Test: Hold low (background + verify + kill)... "
lager gpo "$OUT_NET" low --hold --box "$BOX" >/dev/null 2>&1 &
HOLD_PID=$!
sleep 3
# Verify pin is held low (cached readback)
RAW=$(lager gpi "$OUT_NET" --box "$BOX" 2>&1)
CLEAN=$(strip_ansi "$RAW")
# Kill the hold process and wait for remote cleanup
kill_hold $HOLD_PID
if echo "$CLEAN" | grep -q "LOW (0)"; then
    track_test "pass"
else
    echo -n "(got: '$CLEAN') "
    track_test "fail"
fi

# Reset state between hold tests
lager gpo "$OUT_NET" low --box "$BOX" >/dev/null 2>&1
sleep 1

# 12c. Hold with loopback verification (if available)
if [ "$LOOPBACK_AVAILABLE" = true ]; then
    echo -n "Test: Hold high + loopback read... "
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
else
    skip_test "Hold high + loopback read" "no loopback"
fi

# ============================================================================
# 13. CLEANUP
# ============================================================================
start_section "Cleanup"
print_section_header "SECTION 13: CLEANUP"

# 13a. Set output low
echo -n "Test: Set output low... "
lager gpo "$OUT_NET" low --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 13b. Reset IN_NET if different
if [ "$IN_NET" != "$OUT_NET" ]; then
    echo -n "Test: Set $IN_NET low... "
    lager gpo "$IN_NET" low --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"
else
    skip_test "Set $IN_NET low" "IN_NET same as OUT_NET"
fi

# 13c. Final connectivity check
echo -n "Test: Final connectivity check... "
lager hello --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# ============================================================================
# SUMMARY
# ============================================================================
print_summary
exit_with_status
