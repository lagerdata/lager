#!/usr/bin/env bash

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
# ============================================================================
# I2C CLI Integration Tests - LabJack T7
# ============================================================================
# Usage:
#   ./i2c_labjack.sh <box> [net]
#
# Arguments:
#   box  - Box name or IP (e.g., <YOUR-BOX>)
#   net  - I2C net name (default: i2c2)
#
# Prerequisites:
#   - BMP280 connected to LabJack T7 I2C on box at address 0x76
#   - BMP280 powered: lager dac dac1 3.3 --box <YOUR-BOX>
#   - Net 'i2c2' configured (LabJack T7 I2C, FIO4=SDA, FIO5=SCL)
#
# LabJack T7 I2C constraints:
#   - Maximum 56 bytes per transaction (hardware buffer limit)
#   - No internal pull-ups (--pull-ups accepted but silently ignored)
#   - Frequency range ~25 Hz to ~450 kHz via throttle register
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
    echo "  net  - I2C net name (default: i2c2)"
    echo ""
    echo "Prerequisites:"
    echo "  - BMP280 wired to LabJack T7 at address 0x76"
    echo "  - lager dac dac1 3.3 --box <box>   (power the BMP280)"
    exit 1
fi

BOX="$1"
NET="${2:-i2c2}"
ADDR="0x76"           # BMP280 address
CHIP_ID_REG="0xD0"    # Chip ID register
CHIP_ID_VAL="58"      # Expected chip ID value (hex)
MAX_BYTES=56          # LabJack T7 I2C buffer limit

init_harness

print_script_header "LAGER I2C CLI TEST SUITE - LABJACK T7" "$BOX" "$NET"

echo "Device: BMP280 at $ADDR"
echo "LabJack max bytes per transaction: $MAX_BYTES"
echo ""

# ============================================================================
# 0. PREREQUISITES
# ============================================================================
start_section "Prerequisites"
print_section_header "SECTION 0: PREREQUISITES"

# 0a. Box connectivity
echo -n "Test: Box connectivity... "
lager hello --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 0b. Power BMP280 via DAC
echo -n "Test: Power BMP280 via DAC... "
lager dac dac1 3.3 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 0c. I2C net exists (list I2C nets)
echo -n "Test: Net '$NET' accessible... "
OUTPUT=$(lager i2c --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -q "$NET"; then
    track_test "pass"
else
    track_test "fail"
fi

# 0d. I2C help works
echo -n "Test: lager i2c --help... "
lager i2c --help >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# ============================================================================
# 1. CONFIG COMMAND
# ============================================================================
start_section "Config"
print_section_header "SECTION 1: CONFIG COMMAND"

# 1a. Standard 100kHz
echo -n "Test: Config 100kHz... "
lager i2c "$NET" config --frequency 100k --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 1b. 400kHz fast mode
echo -n "Test: Config 400kHz... "
lager i2c "$NET" config --frequency 400k --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 1c. Frequency with Hz suffix
echo -n "Test: Config 100000hz... "
lager i2c "$NET" config --frequency 100000hz --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 1d. 1MHz (LabJack caps at ~450kHz via throttle=0)
echo -n "Test: Config 1M (caps at ~450kHz)... "
lager i2c "$NET" config --frequency 1M --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 1e. 10kHz
echo -n "Test: Config 10kHz... "
lager i2c "$NET" config --frequency 10k --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 1f. Decimal frequency (3.5k)
echo -n "Test: Config 3.5k... "
lager i2c "$NET" config --frequency 3.5k --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 1g. Plain number frequency
echo -n "Test: Config 200000... "
lager i2c "$NET" config --frequency 200000 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 1h. Pull-ups on (LabJack ignores, should still accept)
echo -n "Test: Config --pull-ups on (silently ignored)... "
lager i2c "$NET" config --pull-ups on --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 1i. Pull-ups off
echo -n "Test: Config --pull-ups off... "
lager i2c "$NET" config --pull-ups off --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 1j. Frequency only (no pull-ups flag)
echo -n "Test: Config frequency only... "
lager i2c "$NET" config --frequency 100k --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 1k. Pull-ups only (no frequency)
echo -n "Test: Config pull-ups only... "
lager i2c "$NET" config --pull-ups on --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 1l. Combined frequency + pull-ups
echo -n "Test: Config 100k + pull-ups on... "
lager i2c "$NET" config --frequency 100k --pull-ups on --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 1m. No options (show current config)
echo -n "Test: Config no options (show stored)... "
OUTPUT=$(lager i2c "$NET" config --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "freq=\|configured\|I2C"; then
    track_test "pass"
else
    track_test "fail"
fi

# 1n. Invalid frequency string
echo -n "Test: Config invalid frequency abc... "
lager i2c "$NET" config --frequency abc --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# Restore standard config
lager i2c "$NET" config --frequency 100k --pull-ups on --box "$BOX" >/dev/null 2>&1

# ============================================================================
# 2. SCAN COMMAND
# ============================================================================
start_section "Scan"
print_section_header "SECTION 2: SCAN COMMAND"

# 2a. Default scan (0x08-0x77)
echo -n "Test: Default scan (finds BMP280 at 0x76)... "
OUTPUT=$(lager i2c "$NET" scan --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -q "76"; then
    track_test "pass"
else
    track_test "fail"
fi

# 2b. Narrow range (just BMP280)
echo -n "Test: Narrow scan --start 0x76 --end 0x76... "
OUTPUT=$(lager i2c "$NET" scan --start 0x76 --end 0x76 --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -q "76"; then
    track_test "pass"
else
    track_test "fail"
fi

# 2c. Range excluding BMP280
echo -n "Test: Scan excluding device (0x08-0x75)... "
OUTPUT=$(lager i2c "$NET" scan --start 0x08 --end 0x75 --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "0 device\|no device"; then
    track_test "pass"
else
    track_test "fail"
fi

# 2d. Full range (0x00-0x7F)
echo -n "Test: Full range scan (0x00-0x7F)... "
lager i2c "$NET" scan --start 0x00 --end 0x7F --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 2e. Reserved low addresses (0x00-0x07)
echo -n "Test: Reserved low range (0x00-0x07)... "
lager i2c "$NET" scan --start 0x00 --end 0x07 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 2f. Reserved high addresses (0x78-0x7F)
echo -n "Test: Reserved high range (0x78-0x7F)... "
lager i2c "$NET" scan --start 0x78 --end 0x7F --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 2g. Single empty address
echo -n "Test: Single empty address (0x10)... "
lager i2c "$NET" scan --start 0x10 --end 0x10 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 2h. Reversed range (start > end)
echo -n "Test: Reversed range (0x77-0x08)... "
lager i2c "$NET" scan --start 0x77 --end 0x08 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 2i. Address without 0x prefix
echo -n "Test: Address without 0x prefix (08-77)... "
lager i2c "$NET" scan --start 08 --end 77 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 2j. Decimal addresses
echo -n "Test: Decimal addresses (8-119)... "
lager i2c "$NET" scan --start 8 --end 119 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 2k. Invalid start address
echo -n "Test: Invalid start address (0xZZ)... "
lager i2c "$NET" scan --start 0xZZ --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 2l. Address above 0x7F
echo -n "Test: Address above 0x7F (0x80)... "
lager i2c "$NET" scan --start 0x80 --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# ============================================================================
# 3. READ COMMAND
# ============================================================================
start_section "Read"
print_section_header "SECTION 3: READ COMMAND"

# 3a. Read 1 byte
echo -n "Test: Read 1 byte... "
lager i2c "$NET" read 1 --address "$ADDR" --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 3b. Read 8 bytes
echo -n "Test: Read 8 bytes... "
lager i2c "$NET" read 8 --address "$ADDR" --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 3c. Read 56 bytes (LabJack max)
echo -n "Test: Read 56 bytes (LabJack max OK)... "
lager i2c "$NET" read 56 --address "$ADDR" --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 3d. Read 57 bytes (LabJack: MUST FAIL)
echo -n "Test: Read 57 bytes (LabJack: must fail)... "
lager i2c "$NET" read 57 --address "$ADDR" --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 3e. Read hex format
echo -n "Test: Read --format hex... "
lager i2c "$NET" read 4 --address "$ADDR" --format hex --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 3f. Read bytes format
echo -n "Test: Read --format bytes... "
lager i2c "$NET" read 4 --address "$ADDR" --format bytes --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 3g. Read json format
echo -n "Test: Read --format json... "
OUTPUT=$(lager i2c "$NET" read 4 --address "$ADDR" --format json --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -q '"data"'; then
    track_test "pass"
else
    track_test "fail"
fi

# 3h. Read with frequency override
echo -n "Test: Read --frequency 400k... "
lager i2c "$NET" read 1 --address "$ADDR" --frequency 400k --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 3i. Read from non-existent device
echo -n "Test: Read from empty address 0x10 (error)... "
lager i2c "$NET" read 1 --address 0x10 --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 3j. Read 0 bytes
echo -n "Test: Read 0 bytes... "
lager i2c "$NET" read 0 --address "$ADDR" --box "$BOX" >/dev/null 2>&1
# Either success or error acceptable
track_test "pass"

# 3k. Missing --address
echo -n "Test: Read missing --address... "
lager i2c "$NET" read 1 --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 3l. Missing NUM_BYTES
echo -n "Test: Read missing NUM_BYTES... "
lager i2c "$NET" read --address "$ADDR" --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 3m. Address in decimal
echo -n "Test: Read address in decimal (118 = 0x76)... "
lager i2c "$NET" read 1 --address 118 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 3n. Address 0x80 (out of range)
echo -n "Test: Read address 0x80 (invalid)... "
lager i2c "$NET" read 1 --address 0x80 --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 3o. Non-numeric num_bytes
echo -n "Test: Read non-numeric num_bytes... "
lager i2c "$NET" read abc --address "$ADDR" --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 3p. Negative num_bytes
echo -n "Test: Read negative num_bytes... "
lager i2c "$NET" read -1 --address "$ADDR" --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# ============================================================================
# 4. WRITE COMMAND
# ============================================================================
start_section "Write"
print_section_header "SECTION 4: WRITE COMMAND"

# 4a. Write single byte (register pointer)
echo -n "Test: Write single byte 0xD0... "
lager i2c "$NET" write 0xD0 --address "$ADDR" --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 4b. Write two bytes (register + value)
echo -n "Test: Write two bytes 0xF400... "
lager i2c "$NET" write 0xF400 --address "$ADDR" --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 4c. Write with spaces
echo -n "Test: Write with spaces 'F4 00'... "
lager i2c "$NET" write "F4 00" --address "$ADDR" --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 4d. Write with commas
echo -n "Test: Write with commas 'F4,00'... "
lager i2c "$NET" write "F4,00" --address "$ADDR" --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 4e. Write with colons
echo -n "Test: Write with colons 'F4:00'... "
lager i2c "$NET" write "F4:00" --address "$ADDR" --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 4f. Write with 0x prefix per byte
echo -n "Test: Write with 0x per byte '0xF4 0x00'... "
lager i2c "$NET" write "0xF4 0x00" --address "$ADDR" --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 4g. Write with data-file
echo -n "Test: Write with --data-file... "
echo -ne '\xF4\x00' > /tmp/i2c_lj_test.bin
lager i2c "$NET" write --data-file /tmp/i2c_lj_test.bin --address "$ADDR" --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 4h. Write to non-existent device
echo -n "Test: Write to empty address 0x10... "
lager i2c "$NET" write 0x00 --address 0x10 --box "$BOX" >/dev/null 2>&1
# May error with NACK or succeed silently - both acceptable
track_test "pass"

# 4i. Write with no data
echo -n "Test: Write with no data... "
lager i2c "$NET" write --address "$ADDR" --box "$BOX" >/dev/null 2>&1
# May error or send 0-byte write
track_test "pass"

# 4j. Write with frequency override
echo -n "Test: Write --frequency 400k... "
lager i2c "$NET" write 0xD0 --address "$ADDR" --frequency 400k --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 4k. Missing --address
echo -n "Test: Write missing --address... "
lager i2c "$NET" write 0xD0 --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 4l. Invalid hex data
echo -n "Test: Write invalid hex 0xGG... "
lager i2c "$NET" write 0xGG --address "$ADDR" --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 4m. Non-existent data-file
echo -n "Test: Write non-existent --data-file... "
lager i2c "$NET" write --data-file /tmp/nonexistent_i2c_file.bin --address "$ADDR" --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 4n. Write 56 bytes (LabJack max OK)
echo -n "Test: Write 56 bytes (LabJack max)... "
LONG_DATA=$(python3 -c "print('F4' + '00'*55)")
lager i2c "$NET" write "$LONG_DATA" --address "$ADDR" --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 4o. Write 57 bytes (LabJack: MUST FAIL)
echo -n "Test: Write 57 bytes (LabJack: must fail)... "
LONG_DATA=$(python3 -c "print('F4' + '00'*56)")
lager i2c "$NET" write "$LONG_DATA" --address "$ADDR" --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 4p. BMP280 soft reset
echo -n "Test: BMP280 soft reset (0xE0B6)... "
lager i2c "$NET" write 0xE0B6 --address "$ADDR" --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"
sleep 0.01

# 4q. Write 0xFF
echo -n "Test: Write 0xF4FF... "
lager i2c "$NET" write 0xF4FF --address "$ADDR" --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 4r. Write 0x00
echo -n "Test: Write 0xF400... "
lager i2c "$NET" write 0xF400 --address "$ADDR" --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# ============================================================================
# 5. TRANSFER (WRITE_READ) COMMAND
# ============================================================================
start_section "Transfer"
print_section_header "SECTION 5: TRANSFER (WRITE_READ) COMMAND"

# 5a. Read chip ID
echo -n "Test: Transfer chip ID (expect 58)... "
OUTPUT=$(lager i2c "$NET" transfer 1 --address "$ADDR" --data "$CHIP_ID_REG" --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -q "$CHIP_ID_VAL"; then
    track_test "pass"
else
    echo "  (got: $OUTPUT)"
    track_test "fail"
fi

# 5b. Read calibration data (26 bytes, within 56-byte limit)
echo -n "Test: Transfer 26 bytes (calibration)... "
lager i2c "$NET" transfer 26 --address "$ADDR" --data 0x88 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 5c. Read status register
echo -n "Test: Transfer status register... "
lager i2c "$NET" transfer 1 --address "$ADDR" --data 0xF3 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 5d. Read ctrl_meas register
echo -n "Test: Transfer ctrl_meas register... "
lager i2c "$NET" transfer 1 --address "$ADDR" --data 0xF4 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 5e. Transfer 56 bytes (LabJack max OK)
echo -n "Test: Transfer 56 bytes (LabJack max)... "
lager i2c "$NET" transfer 56 --address "$ADDR" --data 0x80 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 5f. Transfer 57 bytes (LabJack: MUST FAIL)
echo -n "Test: Transfer 57 bytes (LabJack: must fail)... "
lager i2c "$NET" transfer 57 --address "$ADDR" --data 0x80 --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 5g. Hex output format
echo -n "Test: Transfer --format hex... "
OUTPUT=$(lager i2c "$NET" transfer 1 --address "$ADDR" --data "$CHIP_ID_REG" --format hex --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -q "$CHIP_ID_VAL"; then
    track_test "pass"
else
    track_test "fail"
fi

# 5h. Bytes output format
echo -n "Test: Transfer --format bytes... "
OUTPUT=$(lager i2c "$NET" transfer 1 --address "$ADDR" --data "$CHIP_ID_REG" --format bytes --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -q "88"; then  # 0x58 = 88 decimal
    track_test "pass"
else
    track_test "fail"
fi

# 5i. JSON output format
echo -n "Test: Transfer --format json... "
OUTPUT=$(lager i2c "$NET" transfer 1 --address "$ADDR" --data "$CHIP_ID_REG" --format json --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -q '"data"'; then
    track_test "pass"
else
    track_test "fail"
fi

# 5j. Transfer with frequency override
echo -n "Test: Transfer --frequency 400k... "
OUTPUT=$(lager i2c "$NET" transfer 1 --address "$ADDR" --data "$CHIP_ID_REG" --frequency 400k --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -q "$CHIP_ID_VAL"; then
    track_test "pass"
else
    track_test "fail"
fi

# 5k. Transfer to non-existent device
echo -n "Test: Transfer to empty address 0x10 (error)... "
lager i2c "$NET" transfer 1 --address 0x10 --data 0x00 --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 5l. Transfer without --data (just read)
echo -n "Test: Transfer without --data... "
lager i2c "$NET" transfer 1 --address "$ADDR" --box "$BOX" >/dev/null 2>&1
# Should work or error gracefully
track_test "pass"

# 5m. Transfer 0 bytes read
echo -n "Test: Transfer 0 bytes... "
lager i2c "$NET" transfer 0 --address "$ADDR" --data "$CHIP_ID_REG" --box "$BOX" >/dev/null 2>&1
# Either OK or error is acceptable
track_test "pass"

# 5n. Transfer with multi-byte write data
echo -n "Test: Transfer with 2-byte write (0xF400)... "
lager i2c "$NET" transfer 1 --address "$ADDR" --data 0xF400 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 5o. Transfer with data-file
echo -n "Test: Transfer with --data-file... "
echo -ne '\xD0' > /tmp/i2c_lj_reg.bin
lager i2c "$NET" transfer 1 --address "$ADDR" --data-file /tmp/i2c_lj_reg.bin --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 5p. Missing --address
echo -n "Test: Transfer missing --address... "
lager i2c "$NET" transfer 1 --data "$CHIP_ID_REG" --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 5q. Missing NUM_BYTES
echo -n "Test: Transfer missing NUM_BYTES... "
lager i2c "$NET" transfer --address "$ADDR" --data "$CHIP_ID_REG" --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 5r. Data without 0x prefix
echo -n "Test: Transfer data without 0x prefix... "
OUTPUT=$(lager i2c "$NET" transfer 1 --address "$ADDR" --data D0 --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -q "$CHIP_ID_VAL"; then
    track_test "pass"
else
    track_test "fail"
fi

# 5s. Non-existent data-file
echo -n "Test: Transfer non-existent --data-file... "
lager i2c "$NET" transfer 1 --address "$ADDR" --data-file /tmp/nonexistent_i2c_reg.bin --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# ============================================================================
# 6. NET NAME AND BOX EDGE CASES
# ============================================================================
start_section "Net/Box Edge Cases"
print_section_header "SECTION 6: NET NAME AND BOX EDGE CASES"

# 6a. Invalid net name
echo -n "Test: Invalid net name... "
lager i2c nonexistent_i2c_99 scan --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 6b. No net name (list I2C nets)
echo -n "Test: No net name (list nets)... "
OUTPUT=$(lager i2c --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "i2c\|name\|instrument"; then
    track_test "pass"
else
    track_test "fail"
fi

# 6c. --box before net name
echo -n "Test: --box before net name... "
lager i2c --box "$BOX" "$NET" scan >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 6d. --box after subcommand
echo -n "Test: --box after subcommand... "
lager i2c "$NET" scan --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 6e. Invalid box name
echo -n "Test: Invalid box name... "
lager i2c "$NET" scan --box nonexistent_box_12345 >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 6f. Help output (i2c)
echo -n "Test: i2c --help... "
lager i2c --help >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 6g. Subcommand help (config)
echo -n "Test: i2c config --help... "
lager i2c "$NET" config --help >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 6h. Subcommand help (scan/read/write/transfer)
echo -n "Test: i2c subcommand help... "
ALL_OK=true
for cmd in scan read write transfer; do
    lager i2c "$NET" $cmd --help >/dev/null 2>&1 || ALL_OK=false
done
if [ "$ALL_OK" = true ]; then
    track_test "pass"
else
    track_test "fail"
fi

# 6i. Invalid subcommand
echo -n "Test: Invalid subcommand... "
lager i2c "$NET" foobar --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# ============================================================================
# 7. ADDRESS FORMAT EDGE CASES
# ============================================================================
start_section "Address Formats"
print_section_header "SECTION 7: ADDRESS FORMAT EDGE CASES"

# 7a. Hex with 0x prefix
echo -n "Test: Address 0x76... "
OUTPUT=$(lager i2c "$NET" transfer 1 --address 0x76 --data "$CHIP_ID_REG" --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -q "$CHIP_ID_VAL"; then
    track_test "pass"
else
    track_test "fail"
fi

# 7b. Decimal address
echo -n "Test: Address 118 (decimal)... "
OUTPUT=$(lager i2c "$NET" transfer 1 --address 118 --data "$CHIP_ID_REG" --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -q "$CHIP_ID_VAL"; then
    track_test "pass"
else
    track_test "fail"
fi

# 7c. Address 0x00
echo -n "Test: Address 0x00... "
lager i2c "$NET" transfer 1 --address 0x00 --data 0x00 --box "$BOX" >/dev/null 2>&1
track_test "pass"

# 7d. Address 0x7F
echo -n "Test: Address 0x7F... "
lager i2c "$NET" transfer 1 --address 0x7F --data 0x00 --box "$BOX" >/dev/null 2>&1
track_test "pass"

# 7e. Address 0x80 (out of range)
echo -n "Test: Address 0x80 (invalid)... "
lager i2c "$NET" transfer 1 --address 0x80 --data 0x00 --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 7f. Address 0xFF (out of range)
echo -n "Test: Address 0xFF (invalid)... "
lager i2c "$NET" transfer 1 --address 0xFF --data 0x00 --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 7g. Negative address
echo -n "Test: Address -1 (invalid)... "
lager i2c "$NET" transfer 1 --address -1 --data 0x00 --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 7h. Non-numeric address
echo -n "Test: Address xyz (invalid)... "
lager i2c "$NET" transfer 1 --address xyz --data 0x00 --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# ============================================================================
# 8. DATA FORMAT EDGE CASES
# ============================================================================
start_section "Data Formats"
print_section_header "SECTION 8: DATA FORMAT EDGE CASES"

# 8a. 0x prefix
echo -n "Test: Data 0xD0... "
OUTPUT=$(lager i2c "$NET" transfer 1 --address "$ADDR" --data 0xD0 --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -q "$CHIP_ID_VAL"; then
    track_test "pass"
else
    track_test "fail"
fi

# 8b. No prefix
echo -n "Test: Data D0 (no 0x)... "
OUTPUT=$(lager i2c "$NET" transfer 1 --address "$ADDR" --data D0 --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -q "$CHIP_ID_VAL"; then
    track_test "pass"
else
    track_test "fail"
fi

# 8c. Lowercase hex
echo -n "Test: Data 0xd0 (lowercase)... "
OUTPUT=$(lager i2c "$NET" transfer 1 --address "$ADDR" --data 0xd0 --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -q "$CHIP_ID_VAL"; then
    track_test "pass"
else
    track_test "fail"
fi

# 8d. Multi-byte continuous
echo -n "Test: Data 0xD0F3 (multi-byte)... "
lager i2c "$NET" transfer 1 --address "$ADDR" --data 0xD0F3 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 8e. Space separated
echo -n "Test: Data 'D0 F3' (spaces)... "
lager i2c "$NET" transfer 1 --address "$ADDR" --data "D0 F3" --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 8f. Comma separated
echo -n "Test: Data 'D0,F3' (commas)... "
lager i2c "$NET" transfer 1 --address "$ADDR" --data "D0,F3" --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 8g. Colon separated
echo -n "Test: Data 'D0:F3' (colons)... "
lager i2c "$NET" transfer 1 --address "$ADDR" --data "D0:F3" --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 8h. Hyphen separated
echo -n "Test: Data 'D0-F3' (hyphens)... "
lager i2c "$NET" transfer 1 --address "$ADDR" --data "D0-F3" --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 8i. Per-byte 0x prefix with spaces
echo -n "Test: Data '0xD0 0xF3' (0x per byte)... "
lager i2c "$NET" transfer 1 --address "$ADDR" --data "0xD0 0xF3" --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 8j. Odd-length hex
echo -n "Test: Data 0xD0F (odd-length)... "
lager i2c "$NET" transfer 1 --address "$ADDR" --data 0xD0F --box "$BOX" >/dev/null 2>&1
# Should pad or error
track_test "pass"

# 8k. All zeros
echo -n "Test: Data 0x00... "
lager i2c "$NET" transfer 1 --address "$ADDR" --data 0x00 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 8l. All ones
echo -n "Test: Data 0xFF... "
lager i2c "$NET" transfer 1 --address "$ADDR" --data 0xFF --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 8m. Invalid hex character
echo -n "Test: Data 0xGG (invalid)... "
lager i2c "$NET" transfer 1 --address "$ADDR" --data 0xGG --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 8n. Byte value > 0xFF
echo -n "Test: Data '0x1FF' (value > 0xFF)... "
lager i2c "$NET" transfer 1 --address "$ADDR" --data "0x1FF" --box "$BOX" >/dev/null 2>&1
# May truncate or error
track_test "pass"

# ============================================================================
# 9. SEQUENCE TESTS
# ============================================================================
start_section "Sequences"
print_section_header "SECTION 9: SEQUENCE TESTS"

# Restore standard config
lager i2c "$NET" config --frequency 100k --pull-ups on --box "$BOX" >/dev/null 2>&1

# 9a. Config -> scan -> transfer
echo -n "Test: Config -> scan -> transfer... "
lager i2c "$NET" config --frequency 100k --box "$BOX" >/dev/null 2>&1
lager i2c "$NET" scan --box "$BOX" >/dev/null 2>&1
OUTPUT=$(lager i2c "$NET" transfer 1 --address "$ADDR" --data "$CHIP_ID_REG" --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -q "$CHIP_ID_VAL"; then
    track_test "pass"
else
    track_test "fail"
fi

# 9b. Write register, then transfer readback
echo -n "Test: Write then transfer readback... "
lager i2c "$NET" write 0xF400 --address "$ADDR" --box "$BOX" >/dev/null 2>&1
OUTPUT=$(lager i2c "$NET" transfer 1 --address "$ADDR" --data 0xF4 --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -q "00"; then
    track_test "pass"
else
    track_test "fail"
fi

# 9c. Forced mode measurement
echo -n "Test: Forced measurement (write -> wait -> read)... "
lager i2c "$NET" write 0xF425 --address "$ADDR" --box "$BOX" >/dev/null 2>&1
sleep 0.1
lager i2c "$NET" transfer 6 --address "$ADDR" --data 0xF7 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 9d. Soft reset then chip ID
echo -n "Test: Soft reset -> chip ID... "
lager i2c "$NET" write 0xE0B6 --address "$ADDR" --box "$BOX" >/dev/null 2>&1
sleep 0.01
OUTPUT=$(lager i2c "$NET" transfer 1 --address "$ADDR" --data "$CHIP_ID_REG" --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -q "$CHIP_ID_VAL"; then
    track_test "pass"
else
    track_test "fail"
fi

# 9e. Rapid transfers (10 chip ID reads)
echo -n "Test: 10 rapid chip ID reads... "
RAPID_OK=true
for i in $(seq 1 10); do
    OUTPUT=$(lager i2c "$NET" transfer 1 --address "$ADDR" --data "$CHIP_ID_REG" --box "$BOX" 2>&1)
    if ! echo "$OUTPUT" | grep -q "$CHIP_ID_VAL"; then
        RAPID_OK=false
        break
    fi
done
if [ "$RAPID_OK" = true ]; then
    track_test "pass"
else
    track_test "fail"
fi

# 9f. Different frequencies between commands
echo -n "Test: Different frequencies... "
FREQ_OK=true
for freq in 100k 400k 10k; do
    OUTPUT=$(lager i2c "$NET" transfer 1 --address "$ADDR" --data "$CHIP_ID_REG" --frequency "$freq" --box "$BOX" 2>&1)
    if ! echo "$OUTPUT" | grep -q "$CHIP_ID_VAL"; then
        FREQ_OK=false
        break
    fi
done
if [ "$FREQ_OK" = true ]; then
    track_test "pass"
else
    track_test "fail"
fi

# ============================================================================
# 10. CONFIG PERSISTENCE
# ============================================================================
start_section "Config Persistence"
print_section_header "SECTION 10: CONFIG PERSISTENCE"

# 10a. Set freq 400k
echo -n "Test: Set freq 400k... "
OUTPUT=$(lager i2c "$NET" config --frequency 400k --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -q "400000"; then
    track_test "pass"
else
    track_test "fail"
fi

# 10b. Pull-ups only, freq should stay 400k
echo -n "Test: Pull-ups only, freq stays 400k... "
OUTPUT=$(lager i2c "$NET" config --pull-ups on --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -q "400000"; then
    track_test "pass"
else
    track_test "fail"
fi

# 10c. Freq only, pull-ups should remain
echo -n "Test: Freq only, pull-ups preserved... "
OUTPUT=$(lager i2c "$NET" config --frequency 100k --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -q "100000" && echo "$OUTPUT" | grep -qi "pull_ups=on"; then
    track_test "pass"
else
    track_test "fail"
fi

# 10d. No options shows stored values
echo -n "Test: No options shows stored... "
OUTPUT=$(lager i2c "$NET" config --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -q "100000"; then
    track_test "pass"
else
    track_test "fail"
fi

# 10e. Output has both freq= and pull_ups=
echo -n "Test: Output has both freq= and pull_ups=... "
OUTPUT=$(lager i2c "$NET" config --frequency 100k --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -q "freq=" && echo "$OUTPUT" | grep -q "pull_ups="; then
    track_test "pass"
else
    track_test "fail"
fi

# 10f. Scan works after persisted config
echo -n "Test: Scan works after persisted config... "
OUTPUT=$(lager i2c "$NET" scan --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -q "76"; then
    track_test "pass"
else
    track_test "fail"
fi

# 10g. Transfer works after persisted config
echo -n "Test: Transfer works after persisted config... "
OUTPUT=$(lager i2c "$NET" transfer 1 --address "$ADDR" --data "$CHIP_ID_REG" --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -q "$CHIP_ID_VAL"; then
    track_test "pass"
else
    track_test "fail"
fi

# 10h. Per-op override does not persist
echo -n "Test: Per-op freq override doesn't persist... "
lager i2c "$NET" config --frequency 100k --box "$BOX" >/dev/null 2>&1
lager i2c "$NET" transfer 1 --address "$ADDR" --data "$CHIP_ID_REG" --frequency 400k --box "$BOX" >/dev/null 2>&1
OUTPUT=$(lager i2c "$NET" config --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -q "100000"; then
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
echo -n "Test: Restore BMP280 sleep mode... "
lager i2c "$NET" write 0xF400 --address "$ADDR" --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 11b. Restore default config
echo -n "Test: Restore default config... "
lager i2c "$NET" config --frequency 100k --pull-ups on --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 11c. Clean up temp files
echo -n "Test: Clean up temp files... "
rm -f /tmp/i2c_lj_test.bin /tmp/i2c_lj_reg.bin
track_test "pass"

# ============================================================================
# SUMMARY
# ============================================================================
print_summary
exit_with_status
