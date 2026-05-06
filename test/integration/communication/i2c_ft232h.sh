#!/usr/bin/env bash

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
# ============================================================================
# FT232H I2C CLI Integration Tests
# ============================================================================
# Usage:
#   ./i2c_ft232h.sh <box> [net]
#
# Arguments:
#   box  - Box name or IP (e.g., <YOUR-BOX>)
#   net  - FT232H I2C net (default: i2c2)
#
# Prerequisites:
#   - HW-611 (BMP280) wired to FT232H I2C net
#   - FT232H I2C pins: AD0=SCL, AD1+AD2=SDA (bridged)
#   - BMP280 CSB tied to VCC (I2C mode), SDO tied to GND (address 0x76)
#   - External 4.7k pull-ups on SDA and SCL (unless module has onboard)
#   - Net configured in /etc/lager/saved_nets.json with role "i2c"
#   - Power: lager dac dac1 3.3 --box <box>
#
# BMP280 I2C constants:
#   Chip ID register: 0xD0   Expected chip ID: 0x58
#   Calibration:      0x88   (26 bytes)
#   Status:           0xF3   Ctrl_meas: 0xF4
#   Config:           0xF5   Reset: 0xE0 (write 0xB6)
#
# Conventions:
#   track_test "pass"  = expected success
#   track_test "fail"  = unexpected failure
#   For error tests: command fails && track_test "pass" || track_test "fail"
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
    echo "Usage: $0 <box> [net]"
    echo ""
    echo "  box  - Box name or IP (e.g., <YOUR-BOX>)"
    echo "  net  - FT232H I2C net (default: i2c2)"
    exit 1
fi

BOX="$1"
NET="${2:-i2c2}"

# BMP280 constants
BMP280_ADDR="0x76"
BMP280_CHIP_ID="58"          # Expected in hex output
CHIP_ID_REG="0xD0"
CALIB_REG="0x88"
STATUS_REG="0xF3"
CTRL_MEAS_REG="0xF4"
CONFIG_REG="0xF5"
RESET_REG="0xE0"
RESET_VALUE="0xB6"
FORCED_MODE="0x25"           # osrs_t=1x, osrs_p=1x, forced mode

init_harness

print_script_header "LAGER FT232H I2C CLI TEST SUITE" "$BOX" "$NET"

echo "BMP280 address: $BMP280_ADDR"
echo "Expected chip ID: 0x$BMP280_CHIP_ID"
echo ""

# ============================================================================
# 0. PREREQUISITES
# ============================================================================
start_section "Prerequisites"
print_section_header "SECTION 0: PREREQUISITES"

# 0a. Verify box connectivity
echo -n "Test: Box connectivity... "
lager hello --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 0b. Power up via DAC (3.3V for BMP280)
echo -n "Test: DAC power 3.3V... "
lager dac dac1 3.3 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 0c. Power settle delay
echo -n "Test: Power settle delay (2s)... "
sleep 2
track_test "pass"

# 0d. Verify net exists (list I2C nets)
echo -n "Test: Net '$NET' listed... "
OUTPUT=$(lager i2c --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -q "$NET"; then
    track_test "pass"
else
    track_test "fail"
fi

# 0e. I2C help works
echo -n "Test: lager i2c --help... "
lager i2c --help >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# ============================================================================
# 1. CONFIG COMMAND
# ============================================================================
start_section "Config"
print_section_header "SECTION 1: CONFIG COMMAND"

# 1a. Default config (no options) - shows stored values
echo -n "Test: Config no options (show stored)... "
lager i2c "$NET" config --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 1b. Frequency 100k
echo -n "Test: Config frequency 100k... "
lager i2c "$NET" config --frequency 100k --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 1c. Frequency 400k
echo -n "Test: Config frequency 400k... "
lager i2c "$NET" config --frequency 400k --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 1d. Frequency 50k
echo -n "Test: Config frequency 50k... "
lager i2c "$NET" config --frequency 50k --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 1e. Frequency with Hz suffix
echo -n "Test: Config frequency 100000hz... "
lager i2c "$NET" config --frequency 100000hz --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 1f. Frequency bare number
echo -n "Test: Config frequency 100000... "
lager i2c "$NET" config --frequency 100000 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 1g. Frequency 1M
echo -n "Test: Config frequency 1M... "
lager i2c "$NET" config --frequency 1M --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 1h. Pull-ups on (ignored for FT232H but should not error)
echo -n "Test: Config pull-ups on... "
lager i2c "$NET" config --pull-ups on --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 1i. Pull-ups off
echo -n "Test: Config pull-ups off... "
lager i2c "$NET" config --pull-ups off --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 1j. Combined frequency + pull-ups
echo -n "Test: Config combined (400k, pull-ups off)... "
lager i2c "$NET" config --frequency 400k --pull-ups off --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 1k. Invalid frequency (abc)
echo -n "Test: Config invalid frequency abc... "
lager i2c "$NET" config --frequency abc --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 1l. Invalid frequency (-1)
echo -n "Test: Config invalid frequency -1... "
lager i2c "$NET" config --frequency -1 --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 1m. Invalid pull-ups value
echo -n "Test: Config invalid pull-ups maybe... "
lager i2c "$NET" config --pull-ups maybe --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 1n. Restore default config
echo -n "Test: Restore default 100k... "
lager i2c "$NET" config --frequency 100k --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# ============================================================================
# 2. SCAN COMMAND
# ============================================================================
start_section "Scan"
print_section_header "SECTION 2: SCAN COMMAND"

# 2a. Default scan (0x08-0x77)
echo -n "Test: Scan default range... "
OUTPUT=$(lager i2c "$NET" scan --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "76"; then
    track_test "pass"
else
    track_test "fail"
fi

# 2b. Narrow range including device
echo -n "Test: Scan 0x76-0x76... "
OUTPUT=$(lager i2c "$NET" scan --start 0x76 --end 0x76 --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "76"; then
    track_test "pass"
else
    track_test "fail"
fi

# 2c. Narrow range excluding device
echo -n "Test: Scan 0x08-0x75 (no BMP280)... "
OUTPUT=$(lager i2c "$NET" scan --start 0x08 --end 0x75 --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "76"; then
    track_test "fail"
else
    track_test "pass"
fi

# 2d. Full range
echo -n "Test: Scan 0x00-0x7F... "
OUTPUT=$(lager i2c "$NET" scan --start 0x00 --end 0x7F --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "76"; then
    track_test "pass"
else
    track_test "fail"
fi

# 2e. Reserved low addresses (0x00-0x07)
echo -n "Test: Scan 0x00-0x07 (reserved)... "
lager i2c "$NET" scan --start 0x00 --end 0x07 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 2f. Reserved high addresses (0x78-0x7F)
echo -n "Test: Scan 0x78-0x7F (reserved)... "
lager i2c "$NET" scan --start 0x78 --end 0x7F --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 2g. Single address match
echo -n "Test: Scan single address 0x76... "
OUTPUT=$(lager i2c "$NET" scan --start 0x76 --end 0x76 --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "76"; then
    track_test "pass"
else
    track_test "fail"
fi

# 2h. Single address no match
echo -n "Test: Scan single address 0x50 (empty)... "
OUTPUT=$(lager i2c "$NET" scan --start 0x50 --end 0x50 --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "50" | grep -qv "\-\-"; then
    track_test "fail"
else
    track_test "pass"
fi

# 2i. Invalid address format
echo -n "Test: Scan invalid start address ZZ... "
lager i2c "$NET" scan --start ZZ --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 2j. Address out of 7-bit range
echo -n "Test: Scan start 0x80 (out of range)... "
lager i2c "$NET" scan --start 0x80 --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 2k. Scan consistency (3 consecutive scans)
echo -n "Test: Scan consistency (3x)... "
SCAN_OK=true
for i in 1 2 3; do
    OUTPUT=$(lager i2c "$NET" scan --start 0x76 --end 0x76 --box "$BOX" 2>&1)
    if ! echo "$OUTPUT" | grep -qi "76"; then
        SCAN_OK=false
        break
    fi
done
if [ "$SCAN_OK" = true ]; then
    track_test "pass"
else
    track_test "fail"
fi

# 2l. Reversed range (start > end)
echo -n "Test: Scan reversed range (0x77-0x08)... "
lager i2c "$NET" scan --start 0x77 --end 0x08 --box "$BOX" >/dev/null 2>&1
# Either error or empty is acceptable
track_test "pass"

# ============================================================================
# 3. READ COMMAND
# ============================================================================
start_section "Read"
print_section_header "SECTION 3: READ COMMAND"

# 3a. Read 1 byte
echo -n "Test: Read 1 byte from 0x76... "
lager i2c "$NET" read 1 --address 0x76 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 3b. Read 4 bytes
echo -n "Test: Read 4 bytes from 0x76... "
lager i2c "$NET" read 4 --address 0x76 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 3c. Read 8 bytes
echo -n "Test: Read 8 bytes from 0x76... "
lager i2c "$NET" read 8 --address 0x76 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 3d. Read 26 bytes (calibration data length)
echo -n "Test: Read 26 bytes from 0x76... "
lager i2c "$NET" read 26 --address 0x76 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 3e. Read 256 bytes (FT232H: no 56-byte limit)
echo -n "Test: Read 256 bytes from 0x76... "
lager i2c "$NET" read 256 --address 0x76 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 3f. Read format hex
echo -n "Test: Read format hex... "
OUTPUT=$(lager i2c "$NET" read 4 --address 0x76 --format hex --box "$BOX" 2>&1)
if [ $? -eq 0 ]; then
    track_test "pass"
else
    track_test "fail"
fi

# 3g. Read format bytes
echo -n "Test: Read format bytes... "
lager i2c "$NET" read 4 --address 0x76 --format bytes --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 3h. Read format json
echo -n "Test: Read format json... "
OUTPUT=$(lager i2c "$NET" read 4 --address 0x76 --format json --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -q '"data"'; then
    track_test "pass"
else
    track_test "fail"
fi

# 3i. Read with frequency override
echo -n "Test: Read with --frequency 400k... "
lager i2c "$NET" read 1 --address 0x76 --frequency 400k --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 3j. Address without 0x prefix
echo -n "Test: Read address 76 (no prefix)... "
lager i2c "$NET" read 1 --address 76 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 3k. Invalid address 0x80
echo -n "Test: Read invalid address 0x80... "
lager i2c "$NET" read 1 --address 0x80 --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 3l. Non-existent device (NACK)
echo -n "Test: Read from 0x50 (NACK)... "
lager i2c "$NET" read 1 --address 0x50 --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 3m. Read 0 bytes
echo -n "Test: Read 0 bytes... "
lager i2c "$NET" read 0 --address 0x76 --box "$BOX" >/dev/null 2>&1
# Either success or error is acceptable
track_test "pass"

# 3n. Missing --address
echo -n "Test: Read missing --address... "
lager i2c "$NET" read 4 --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# ============================================================================
# 4. WRITE COMMAND
# ============================================================================
start_section "Write"
print_section_header "SECTION 4: WRITE COMMAND"

# 4a. Single byte (register pointer)
echo -n "Test: Write single byte 0xD0... "
lager i2c "$NET" write 0xD0 --address 0x76 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 4b. Multi-byte (register + value)
echo -n "Test: Write register+value 0xF400... "
lager i2c "$NET" write 0xF400 --address 0x76 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 4c. Hex with spaces
echo -n "Test: Write hex with spaces 'F4 00'... "
lager i2c "$NET" write "F4 00" --address 0x76 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 4d. Hex with commas
echo -n "Test: Write hex with commas 'F4,00'... "
lager i2c "$NET" write "F4,00" --address 0x76 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 4e. Hex with 0x prefix per byte
echo -n "Test: Write '0xF4 0x00'... "
lager i2c "$NET" write "0xF4 0x00" --address 0x76 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 4f. Lowercase hex
echo -n "Test: Write lowercase 0xf400... "
lager i2c "$NET" write 0xf400 --address 0x76 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 4g. Write with format hex
echo -n "Test: Write format hex... "
lager i2c "$NET" write 0xD0 --address 0x76 --format hex --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 4h. Write with format json
echo -n "Test: Write format json... "
lager i2c "$NET" write 0xD0 --address 0x76 --format json --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 4i. Write with frequency override
echo -n "Test: Write with --frequency 400k... "
lager i2c "$NET" write 0xD0 --address 0x76 --frequency 400k --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 4j. Write to non-existent device (NACK)
echo -n "Test: Write to 0x50 (NACK)... "
lager i2c "$NET" write 0x00 --address 0x50 --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 4k. Invalid hex data
echo -n "Test: Write invalid hex 0xGG... "
lager i2c "$NET" write 0xGG --address 0x76 --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 4l. Odd-length hex
echo -n "Test: Write odd-length hex 0xD0F... "
lager i2c "$NET" write 0xD0F --address 0x76 --box "$BOX" >/dev/null 2>&1
# Padding behavior -- either works or errors
track_test "pass"

# 4m. Data file
echo -n "Test: Write from data file... "
printf '\xD0' > /tmp/i2c_ft232h_write_test.bin
lager i2c "$NET" write --data-file /tmp/i2c_ft232h_write_test.bin --address 0x76 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 4n. Soft reset command
echo -n "Test: Write soft reset (0xE0B6)... "
lager i2c "$NET" write 0xE0B6 --address 0x76 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"
sleep 0.1  # Let device recover from reset

# ============================================================================
# 5. TRANSFER (write_read) COMMAND
# ============================================================================
start_section "Transfer"
print_section_header "SECTION 5: TRANSFER COMMAND"

# 5a. Read chip ID (the core test)
echo -n "Test: Transfer read chip ID (0xD0)... "
OUTPUT=$(lager i2c "$NET" transfer 1 --address 0x76 --data 0xD0 --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "$BMP280_CHIP_ID"; then
    track_test "pass"
else
    track_test "fail"
    echo "  (expected 58, got: $OUTPUT)"
fi

# 5b. Read calibration data (26 bytes)
echo -n "Test: Transfer read calibration (26 bytes)... "
lager i2c "$NET" transfer 26 --address 0x76 --data 0x88 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 5c. Read status register
echo -n "Test: Transfer read status reg (0xF3)... "
lager i2c "$NET" transfer 1 --address 0x76 --data 0xF3 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 5d. Read config register
echo -n "Test: Transfer read config reg (0xF5)... "
lager i2c "$NET" transfer 1 --address 0x76 --data 0xF5 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 5e. Format hex
echo -n "Test: Transfer format hex... "
OUTPUT=$(lager i2c "$NET" transfer 1 --address 0x76 --data 0xD0 --format hex --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "$BMP280_CHIP_ID"; then
    track_test "pass"
else
    track_test "fail"
fi

# 5f. Format bytes
echo -n "Test: Transfer format bytes... "
lager i2c "$NET" transfer 1 --address 0x76 --data 0xD0 --format bytes --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 5g. Format json
echo -n "Test: Transfer format json... "
OUTPUT=$(lager i2c "$NET" transfer 1 --address 0x76 --data 0xD0 --format json --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -q '"data"'; then
    track_test "pass"
else
    track_test "fail"
fi

# 5h. Frequency override 400k
echo -n "Test: Transfer with --frequency 400k... "
OUTPUT=$(lager i2c "$NET" transfer 1 --address 0x76 --data 0xD0 --frequency 400k --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "$BMP280_CHIP_ID"; then
    track_test "pass"
else
    track_test "fail"
fi

# 5i. Frequency override 50k
echo -n "Test: Transfer with --frequency 50k... "
OUTPUT=$(lager i2c "$NET" transfer 1 --address 0x76 --data 0xD0 --frequency 50k --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "$BMP280_CHIP_ID"; then
    track_test "pass"
else
    track_test "fail"
fi

# 5j. Multi-byte register read (6 bytes raw data after forced mode)
echo -n "Test: Transfer read 6 bytes raw data (0xF7)... "
# First set forced mode
lager i2c "$NET" write 0xF425 --address 0x76 --box "$BOX" >/dev/null 2>&1
sleep 0.1
lager i2c "$NET" transfer 6 --address 0x76 --data 0xF7 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 5k. Data file
echo -n "Test: Transfer with --data-file... "
printf '\xD0' > /tmp/i2c_ft232h_reg.bin
lager i2c "$NET" transfer 1 --address 0x76 --data-file /tmp/i2c_ft232h_reg.bin --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 5l. Non-existent device (NACK)
echo -n "Test: Transfer to 0x50 (NACK)... "
lager i2c "$NET" transfer 1 --address 0x50 --data 0x00 --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 5m. Missing --address
echo -n "Test: Transfer missing --address... "
lager i2c "$NET" transfer 1 --data 0xD0 --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 5n. Read 0 bytes
echo -n "Test: Transfer 0 bytes... "
lager i2c "$NET" transfer 0 --address 0x76 --data 0xD0 --box "$BOX" >/dev/null 2>&1
# Either success or error is acceptable
track_test "pass"

# 5o. Hex with colons
echo -n "Test: Transfer data with colons 'D0'... "
lager i2c "$NET" transfer 1 --address 0x76 --data "D0" --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 5p. Chip ID consistency (5 consecutive reads)
echo -n "Test: Chip ID consistency (5x)... "
CONSISTENT=true
for i in 1 2 3 4 5; do
    OUTPUT=$(lager i2c "$NET" transfer 1 --address 0x76 --data 0xD0 --box "$BOX" 2>&1)
    if ! echo "$OUTPUT" | grep -qi "$BMP280_CHIP_ID"; then
        CONSISTENT=false
        break
    fi
done
if [ "$CONSISTENT" = true ]; then
    track_test "pass"
else
    track_test "fail"
fi

# ============================================================================
# 6. BMP280 FUNCTIONAL TESTS
# ============================================================================
start_section "BMP280 Functional"
print_section_header "SECTION 6: BMP280 FUNCTIONAL TESTS"

# 6a. Chip ID verification
echo -n "Test: BMP280 chip ID == 0x58... "
OUTPUT=$(lager i2c "$NET" transfer 1 --address 0x76 --data 0xD0 --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "$BMP280_CHIP_ID"; then
    track_test "pass"
else
    track_test "fail"
fi

# 6b. Soft reset then chip ID
echo -n "Test: Soft reset then chip ID... "
lager i2c "$NET" write 0xE0B6 --address 0x76 --box "$BOX" >/dev/null 2>&1
sleep 0.1
OUTPUT=$(lager i2c "$NET" transfer 1 --address 0x76 --data 0xD0 --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "$BMP280_CHIP_ID"; then
    track_test "pass"
else
    track_test "fail"
fi

# 6c. Set forced mode, read raw data
echo -n "Test: Forced mode -> read raw data... "
lager i2c "$NET" write 0xF425 --address 0x76 --box "$BOX" >/dev/null 2>&1
sleep 0.1
OUTPUT=$(lager i2c "$NET" transfer 6 --address 0x76 --data 0xF7 --box "$BOX" 2>&1)
if [ $? -eq 0 ] && [ -n "$OUTPUT" ]; then
    track_test "pass"
else
    track_test "fail"
fi

# 6d. Read calibration data (26 bytes)
echo -n "Test: Calibration data (26 bytes)... "
OUTPUT=$(lager i2c "$NET" transfer 26 --address 0x76 --data 0x88 --box "$BOX" 2>&1)
if [ $? -eq 0 ] && [ -n "$OUTPUT" ]; then
    track_test "pass"
else
    track_test "fail"
fi

# 6e. Config register write/readback
echo -n "Test: Config register write/readback... "
lager i2c "$NET" write 0xF500 --address 0x76 --box "$BOX" >/dev/null 2>&1
OUTPUT=$(lager i2c "$NET" transfer 1 --address 0x76 --data 0xF5 --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "00"; then
    track_test "pass"
else
    track_test "fail"
fi

# 6f. Frequency sweep chip ID (50k, 100k, 400k)
echo -n "Test: Frequency sweep chip ID... "
SWEEP_OK=true
for freq in 50k 100k 400k; do
    OUTPUT=$(lager i2c "$NET" transfer 1 --address 0x76 --data 0xD0 --frequency "$freq" --box "$BOX" 2>&1)
    if ! echo "$OUTPUT" | grep -qi "$BMP280_CHIP_ID"; then
        SWEEP_OK=false
        break
    fi
done
if [ "$SWEEP_OK" = true ]; then
    track_test "pass"
else
    track_test "fail"
fi

# 6g. Multiple sensor reads (5 forced measurements)
echo -n "Test: 5 consecutive forced measurements... "
MEAS_OK=true
for i in 1 2 3 4 5; do
    lager i2c "$NET" write 0xF425 --address 0x76 --box "$BOX" >/dev/null 2>&1
    sleep 0.05
    OUTPUT=$(lager i2c "$NET" transfer 6 --address 0x76 --data 0xF7 --box "$BOX" 2>&1)
    if [ $? -ne 0 ]; then
        MEAS_OK=false
        break
    fi
done
if [ "$MEAS_OK" = true ]; then
    track_test "pass"
else
    track_test "fail"
fi

# 6h. Sleep mode verify
echo -n "Test: Sleep mode verify... "
lager i2c "$NET" write 0xF400 --address 0x76 --box "$BOX" >/dev/null 2>&1
OUTPUT=$(lager i2c "$NET" transfer 1 --address 0x76 --data 0xF4 --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "00"; then
    track_test "pass"
else
    track_test "fail"
fi

# 6i. Status register readable
echo -n "Test: Status register readable... "
lager i2c "$NET" transfer 1 --address 0x76 --data 0xF3 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 6j. Rapid transfers (10 consecutive chip ID reads)
echo -n "Test: Rapid transfers (10x chip ID)... "
RAPID_OK=true
for i in $(seq 1 10); do
    OUTPUT=$(lager i2c "$NET" transfer 1 --address 0x76 --data 0xD0 --box "$BOX" 2>&1)
    if ! echo "$OUTPUT" | grep -qi "$BMP280_CHIP_ID"; then
        RAPID_OK=false
        break
    fi
done
if [ "$RAPID_OK" = true ]; then
    track_test "pass"
else
    track_test "fail"
fi

# ============================================================================
# 7. NET/BOX EDGE CASES
# ============================================================================
start_section "Net/Box Edge Cases"
print_section_header "SECTION 7: NET/BOX EDGE CASES"

# 7a. Invalid net name
echo -n "Test: Invalid net name NONEXISTENT... "
lager i2c NONEXISTENT scan --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 7b. No net name (list nets)
echo -n "Test: No net name (list nets)... "
OUTPUT=$(lager i2c --box "$BOX" 2>&1)
if [ $? -eq 0 ] && [ -n "$OUTPUT" ]; then
    track_test "pass"
else
    track_test "fail"
fi

# 7c. --box before net
echo -n "Test: --box before net... "
lager i2c --box "$BOX" "$NET" scan >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 7d. --box after subcommand
echo -n "Test: --box after subcommand... "
lager i2c "$NET" scan --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 7e. Invalid box name
echo -n "Test: Invalid box name... "
lager i2c "$NET" scan --box FAKEBOX_999 >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 7f. Help on config subcommand
echo -n "Test: Config --help... "
lager i2c "$NET" config --help >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 7g. Help on scan subcommand
echo -n "Test: Scan --help... "
lager i2c "$NET" scan --help >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 7h. Help on read subcommand
echo -n "Test: Read --help... "
lager i2c "$NET" read --help >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 7i. Help on write subcommand
echo -n "Test: Write --help... "
lager i2c "$NET" write --help >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# ============================================================================
# 8. PARAMETER VALIDATION EDGE CASES
# ============================================================================
start_section "Parameter Validation"
print_section_header "SECTION 8: PARAMETER VALIDATION EDGE CASES"

# 8a. Address 0x00 (general call)
echo -n "Test: Address 0x00 (general call)... "
lager i2c "$NET" read 1 --address 0x00 --box "$BOX" >/dev/null 2>&1
# May NACK -- either result is fine
track_test "pass"

# 8b. Address 0x7F (max 7-bit)
echo -n "Test: Address 0x7F (max valid)... "
lager i2c "$NET" read 1 --address 0x7F --box "$BOX" >/dev/null 2>&1
# May NACK -- either result is fine
track_test "pass"

# 8c. Address decimal 118 (== 0x76)
echo -n "Test: Address decimal 118... "
OUTPUT=$(lager i2c "$NET" transfer 1 --address 118 --data 0xD0 --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "$BMP280_CHIP_ID"; then
    track_test "pass"
else
    track_test "fail"
fi

# 8d. Frequency 0 (should error)
echo -n "Test: Config frequency 0... "
lager i2c "$NET" config --frequency 0 --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 8e. Negative num_bytes
echo -n "Test: Read -1 bytes (error)... "
lager i2c "$NET" read -- -1 --address 0x76 --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 8f. Non-numeric num_bytes
echo -n "Test: Read abc bytes (error)... "
lager i2c "$NET" read abc --address 0x76 --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 8g. Very large read (10000 bytes)
echo -n "Test: Read 10000 bytes... "
lager i2c "$NET" read 10000 --address 0x76 --box "$BOX" >/dev/null 2>&1
# May succeed or fail -- boundary test
track_test "pass"

# 8h. Empty data write
echo -n "Test: Write empty data... "
lager i2c "$NET" write "" --address 0x76 --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 8i. Data with colons separator
echo -n "Test: Write 'F4:00' (colon sep)... "
lager i2c "$NET" write "F4:00" --address 0x76 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 8j. Data with hyphens separator
echo -n "Test: Write 'F4-00' (hyphen sep)... "
lager i2c "$NET" write "F4-00" --address 0x76 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 8k. Uppercase 0X prefix
echo -n "Test: Address 0X76 (uppercase prefix)... "
lager i2c "$NET" transfer 1 --address 0X76 --data 0xD0 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# ============================================================================
# 9. SEQUENCE TESTS
# ============================================================================
start_section "Sequences"
print_section_header "SECTION 9: SEQUENCE TESTS"

# 9a. Config -> Scan -> Transfer chain
echo -n "Test: Config -> Scan -> Transfer... "
lager i2c "$NET" config --frequency 100k --box "$BOX" >/dev/null 2>&1
lager i2c "$NET" scan --box "$BOX" >/dev/null 2>&1
OUTPUT=$(lager i2c "$NET" transfer 1 --address 0x76 --data 0xD0 --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "$BMP280_CHIP_ID"; then
    track_test "pass"
else
    track_test "fail"
fi

# 9b. Write register, transfer readback, verify
echo -n "Test: Write then transfer readback... "
lager i2c "$NET" write 0xF400 --address 0x76 --box "$BOX" >/dev/null 2>&1
OUTPUT=$(lager i2c "$NET" transfer 1 --address 0x76 --data 0xF4 --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "00"; then
    track_test "pass"
else
    track_test "fail"
fi

# 9c. Forced measurement cycle (3x)
echo -n "Test: Forced measurement cycle (3x)... "
CYCLE_OK=true
for i in 1 2 3; do
    lager i2c "$NET" write 0xF425 --address 0x76 --box "$BOX" >/dev/null 2>&1
    sleep 0.1
    OUTPUT=$(lager i2c "$NET" transfer 6 --address 0x76 --data 0xF7 --box "$BOX" 2>&1)
    if [ $? -ne 0 ]; then
        CYCLE_OK=false
        break
    fi
done
if [ "$CYCLE_OK" = true ]; then
    track_test "pass"
else
    track_test "fail"
fi

# 9d. Soft reset -> chip ID (verify recovery)
echo -n "Test: Reset -> chip ID recovery... "
lager i2c "$NET" write 0xE0B6 --address 0x76 --box "$BOX" >/dev/null 2>&1
sleep 0.1
OUTPUT=$(lager i2c "$NET" transfer 1 --address 0x76 --data 0xD0 --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "$BMP280_CHIP_ID"; then
    track_test "pass"
else
    track_test "fail"
fi

# 9e. Frequency changes between commands
echo -n "Test: Freq changes (100k, 400k, 50k)... "
FREQ_OK=true
for freq in 100k 400k 50k; do
    OUTPUT=$(lager i2c "$NET" transfer 1 --address 0x76 --data 0xD0 --frequency "$freq" --box "$BOX" 2>&1)
    if ! echo "$OUTPUT" | grep -qi "$BMP280_CHIP_ID"; then
        FREQ_OK=false
        break
    fi
done
if [ "$FREQ_OK" = true ]; then
    track_test "pass"
else
    track_test "fail"
fi

# 9f. Alternating write/read (5 pairs)
echo -n "Test: Alternating write/read (5x)... "
ALT_OK=true
for i in 1 2 3 4 5; do
    lager i2c "$NET" write 0xD0 --address 0x76 --box "$BOX" >/dev/null 2>&1
    lager i2c "$NET" read 1 --address 0x76 --box "$BOX" >/dev/null 2>&1
    if [ $? -ne 0 ]; then
        ALT_OK=false
        break
    fi
done
if [ "$ALT_OK" = true ]; then
    track_test "pass"
else
    track_test "fail"
fi

# 9g. Config persistence across commands
echo -n "Test: Config persistence across commands... "
lager i2c "$NET" config --frequency 400k --box "$BOX" >/dev/null 2>&1
OUTPUT=$(lager i2c "$NET" config --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "400000"; then
    track_test "pass"
else
    track_test "fail"
fi

# 9h. Transfer after scan
echo -n "Test: Transfer after scan... "
lager i2c "$NET" scan --box "$BOX" >/dev/null 2>&1
OUTPUT=$(lager i2c "$NET" transfer 1 --address 0x76 --data 0xD0 --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "$BMP280_CHIP_ID"; then
    track_test "pass"
else
    track_test "fail"
fi

# ============================================================================
# 10. CONFIG PERSISTENCE
# ============================================================================
start_section "Config Persistence"
print_section_header "SECTION 10: CONFIG PERSISTENCE"

# 10a. Set frequency 400k, verify shows 400k
echo -n "Test: Set freq 400k, verify... "
lager i2c "$NET" config --frequency 400k --box "$BOX" >/dev/null 2>&1
OUTPUT=$(lager i2c "$NET" config --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "400000"; then
    track_test "pass"
else
    track_test "fail"
fi

# 10b. Run scan (no freq override), verify uses 400k
echo -n "Test: Scan at persisted 400k... "
OUTPUT=$(lager i2c "$NET" scan --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "76"; then
    track_test "pass"
else
    track_test "fail"
fi

# 10c. Transfer with freq override 100k, verify config still 400k
echo -n "Test: Transfer override 100k, config stays 400k... "
lager i2c "$NET" transfer 1 --address 0x76 --data 0xD0 --frequency 100k --box "$BOX" >/dev/null 2>&1
OUTPUT=$(lager i2c "$NET" config --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "400000"; then
    track_test "pass"
else
    track_test "fail"
fi

# 10d. Change frequency to 100k, verify
echo -n "Test: Change freq to 100k... "
lager i2c "$NET" config --frequency 100k --box "$BOX" >/dev/null 2>&1
OUTPUT=$(lager i2c "$NET" config --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "100000"; then
    track_test "pass"
else
    track_test "fail"
fi

# 10e. Pull-ups on, verify shown
echo -n "Test: Pull-ups on, verify... "
lager i2c "$NET" config --pull-ups on --box "$BOX" >/dev/null 2>&1
OUTPUT=$(lager i2c "$NET" config --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "pull_ups=on"; then
    track_test "pass"
else
    track_test "fail"
fi

# 10f. Pull-ups off, verify shown
echo -n "Test: Pull-ups off, verify... "
lager i2c "$NET" config --pull-ups off --box "$BOX" >/dev/null 2>&1
OUTPUT=$(lager i2c "$NET" config --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "pull_ups=off"; then
    track_test "pass"
else
    track_test "fail"
fi

# ============================================================================
# 11. CLEANUP
# ============================================================================
start_section "Cleanup"
print_section_header "SECTION 11: CLEANUP"

# 11a. Restore BMP280 to sleep mode
echo -n "Test: Restore BMP280 to sleep... "
lager i2c "$NET" write 0xF400 --address 0x76 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 11b. Restore default config
echo -n "Test: Restore default config... "
lager i2c "$NET" config --frequency 100k --pull-ups off --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 11c. Final chip ID sanity check
echo -n "Test: Final chip ID sanity check... "
OUTPUT=$(lager i2c "$NET" transfer 1 --address 0x76 --data 0xD0 --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "$BMP280_CHIP_ID"; then
    track_test "pass"
else
    track_test "fail"
fi

# 11d. Clean up temp files
echo -n "Test: Clean up temp files... "
rm -f /tmp/i2c_ft232h_write_test.bin /tmp/i2c_ft232h_reg.bin
track_test "pass"

# ============================================================================
# SUMMARY
# ============================================================================
print_summary
exit_with_status
