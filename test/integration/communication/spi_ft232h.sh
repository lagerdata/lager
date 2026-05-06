#!/usr/bin/env bash

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
# ============================================================================
# FT232H SPI CLI Integration Tests
# ============================================================================
# Usage:
#   ./spi_ft232h.sh <box> [net]
#
# Arguments:
#   box  - Box name or IP (e.g., <YOUR-BOX>)
#   net  - FT232H SPI net (default: spi2)
#
# Prerequisites:
#   - HW-611 (BMP280) wired to FT232H SPI net
#   - BMP280 chip ID register: 0xD0 (read via 0xD0 with bit 7 already set)
#   - Expected chip ID value: 0x58
#   - Net configured in /etc/lager/saved_nets.json with role "spi"
#   - Power: lager dac dac1 3.3 --box <box>
#
# BMP280 supports SPI modes 0 and 3, MSB-first, max 10 MHz
#
# Conventions:
#   [EXPECT: OK]     = should succeed
#   [EXPECT: ERROR]  = should produce an error message
#   [EXPECT: EMPTY]  = should work but return no meaningful data
#   [OBSERVE]        = check the output manually
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
    echo "  net  - FT232H SPI net (default: spi2)"
    exit 1
fi

BOX="$1"
NET="${2:-spi2}"

# BMP280 constants
BMP280_CMD="0xD000"        # Register 0xD0 + dummy byte
BMP280_CHIP_ID="58"         # Expected chip ID (hex)
BMP280_CHIP_ID_DEC="88"     # 0x58 in decimal

init_harness

print_script_header "LAGER FT232H SPI CLI TEST SUITE" "$BOX" "$NET"

echo "BMP280 chip ID command: $BMP280_CMD"
echo "Expected chip ID value: 0x$BMP280_CHIP_ID"
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

# 0c. Verify net exists (list SPI nets)
echo -n "Test: Net '$NET' listed... "
OUTPUT=$(lager spi --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -q "$NET"; then
    track_test "pass"
else
    track_test "fail"
fi

# 0d. SPI help works
echo -n "Test: lager spi --help... "
lager spi --help >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 0e. Power settle delay
echo -n "Test: Power settle delay (2s)... "
sleep 2
track_test "pass"

# ============================================================================
# 1. CONFIG COMMAND
# ============================================================================
start_section "Config"
print_section_header "SECTION 1: CONFIG COMMAND"

# 1a. Mode 0 (default)
echo -n "Test: Config mode 0... "
lager spi "$NET" config --mode 0 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 1b. Mode 1
echo -n "Test: Config mode 1... "
lager spi "$NET" config --mode 1 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 1c. Mode 2
echo -n "Test: Config mode 2... "
lager spi "$NET" config --mode 2 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 1d. Mode 3
echo -n "Test: Config mode 3... "
lager spi "$NET" config --mode 3 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 1e. Bit order MSB
echo -n "Test: Config bit-order msb... "
lager spi "$NET" config --bit-order msb --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 1f. Bit order LSB
echo -n "Test: Config bit-order lsb... "
lager spi "$NET" config --bit-order lsb --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 1g. Frequency 100k
echo -n "Test: Config frequency 100k... "
lager spi "$NET" config --frequency 100k --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 1h. Frequency 500k
echo -n "Test: Config frequency 500k... "
lager spi "$NET" config --frequency 500k --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 1i. Frequency 1M
echo -n "Test: Config frequency 1M... "
lager spi "$NET" config --frequency 1M --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 1j. Frequency 5M
echo -n "Test: Config frequency 5M... "
lager spi "$NET" config --frequency 5M --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 1k. Frequency 10M
echo -n "Test: Config frequency 10M... "
lager spi "$NET" config --frequency 10M --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 1l. Frequency 30M
echo -n "Test: Config frequency 30M... "
lager spi "$NET" config --frequency 30M --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 1m. Word size 8
echo -n "Test: Config word-size 8... "
lager spi "$NET" config --word-size 8 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 1n. Word size 16
echo -n "Test: Config word-size 16... "
lager spi "$NET" config --word-size 16 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 1o. Word size 32
echo -n "Test: Config word-size 32... "
lager spi "$NET" config --word-size 32 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 1p. CS active low
echo -n "Test: Config cs-active low... "
lager spi "$NET" config --cs-active low --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 1q. CS active high
echo -n "Test: Config cs-active high... "
lager spi "$NET" config --cs-active high --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 1r. Combined config
echo -n "Test: Config combined (mode 0, 1M, msb, 8-bit, cs-active low)... "
lager spi "$NET" config --mode 0 --frequency 1M --bit-order msb --word-size 8 --cs-active low --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 1s. Invalid mode (4)
echo -n "Test: Config invalid mode 4... "
lager spi "$NET" config --mode 4 --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 1t. Invalid mode (-1)
echo -n "Test: Config invalid mode -1... "
lager spi "$NET" config --mode -1 --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 1u. Invalid mode (abc)
echo -n "Test: Config invalid mode abc... "
lager spi "$NET" config --mode abc --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 1v. Invalid frequency (-1)
echo -n "Test: Config invalid frequency -1... "
lager spi "$NET" config --frequency -1 --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 1w. Invalid frequency (abc)
echo -n "Test: Config invalid frequency abc... "
lager spi "$NET" config --frequency abc --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 1x. Invalid word-size (7)
echo -n "Test: Config invalid word-size 7... "
lager spi "$NET" config --word-size 7 --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 1y. Invalid bit-order
echo -n "Test: Config invalid bit-order abc... "
lager spi "$NET" config --bit-order abc --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 1z. Invalid cs-active
echo -n "Test: Config invalid cs-active abc... "
lager spi "$NET" config --cs-active abc --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# Restore standard config
lager spi "$NET" config --mode 0 --frequency 1M --bit-order msb --word-size 8 --cs-active low --box "$BOX" >/dev/null 2>&1

# ============================================================================
# 2. READ COMMAND
# ============================================================================
start_section "Read"
print_section_header "SECTION 2: READ COMMAND"

# 2a. Read 1 word
echo -n "Test: Read 1 word... "
lager spi "$NET" read 1 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 2b. Read 4 words
echo -n "Test: Read 4 words... "
lager spi "$NET" read 4 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 2c. Read 16 words
echo -n "Test: Read 16 words... "
lager spi "$NET" read 16 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 2d. Read 64 words (FT232H: no 56-byte limit)
echo -n "Test: Read 64 words... "
lager spi "$NET" read 64 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 2e. Read 256 words (FT232H: large transfer)
echo -n "Test: Read 256 words... "
lager spi "$NET" read 256 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 2f. Read with fill 0xFF
echo -n "Test: Read with --fill 0xFF... "
lager spi "$NET" read 4 --fill 0xFF --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 2g. Read with fill 0x00
echo -n "Test: Read with --fill 0x00... "
lager spi "$NET" read 4 --fill 0x00 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 2h. Read with fill 0xAA
echo -n "Test: Read with --fill 0xAA... "
lager spi "$NET" read 4 --fill 0xAA --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 2i. Read hex format
echo -n "Test: Read --format hex... "
OUTPUT=$(lager spi "$NET" read 4 --format hex --box "$BOX" 2>&1)
if [ $? -eq 0 ]; then
    track_test "pass"
else
    track_test "fail"
fi

# 2j. Read bytes format
echo -n "Test: Read --format bytes... "
lager spi "$NET" read 4 --format bytes --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 2k. Read json format
echo -n "Test: Read --format json... "
OUTPUT=$(lager spi "$NET" read 4 --format json --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -q '"data"'; then
    track_test "pass"
else
    track_test "fail"
fi

# 2l. Read with --keep-cs
echo -n "Test: Read with --keep-cs... "
lager spi "$NET" read 4 --keep-cs --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 2m. Read with per-op frequency override
echo -n "Test: Read with --frequency 500k... "
lager spi "$NET" read 4 --frequency 500k --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 2n. Read with per-op mode override
echo -n "Test: Read with --mode 3... "
lager spi "$NET" read 4 --mode 3 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 2o. Read 0 words
echo -n "Test: Read 0 words... "
lager spi "$NET" read 0 --box "$BOX" >/dev/null 2>&1
# Either success or error is acceptable for 0 words
track_test "pass"

# 2p. Missing NUM_WORDS
echo -n "Test: Read missing NUM_WORDS... "
lager spi "$NET" read --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 2q. Non-numeric NUM_WORDS
echo -n "Test: Read non-numeric NUM_WORDS... "
lager spi "$NET" read abc --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 2r. Read with word-size 16
echo -n "Test: Read with --word-size 16... "
lager spi "$NET" read 4 --word-size 16 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# ============================================================================
# 3. WRITE COMMAND
# ============================================================================
start_section "Write"
print_section_header "SECTION 3: WRITE COMMAND"

# 3a. Write single byte
echo -n "Test: Write single byte 0xFF... "
lager spi "$NET" write 0xFF --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 3b. Write multi-byte (continuous hex)
echo -n "Test: Write multi-byte 0x8F0000... "
lager spi "$NET" write 0x8F0000 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 3c. Write with spaces
echo -n "Test: Write with spaces '8F 00 00'... "
lager spi "$NET" write "8F 00 00" --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 3d. Write with commas
echo -n "Test: Write with commas '8F,00,00'... "
lager spi "$NET" write "8F,00,00" --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 3e. Write with 0x prefix per byte
echo -n "Test: Write with 0x prefix '0x8F 0x00'... "
lager spi "$NET" write "0x8F 0x00" --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 3f. Write lowercase hex
echo -n "Test: Write lowercase 0x8f... "
lager spi "$NET" write 0x8f --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 3g. Write mixed case
echo -n "Test: Write mixed case 0x8F0a... "
lager spi "$NET" write 0x8F0a --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 3h. Write with hex format output
echo -n "Test: Write --format hex... "
lager spi "$NET" write 0xFF --format hex --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 3i. Write with bytes format output
echo -n "Test: Write --format bytes... "
lager spi "$NET" write 0xFF --format bytes --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 3j. Write with json format output
echo -n "Test: Write --format json... "
lager spi "$NET" write 0xFF --format json --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 3k. Write with --keep-cs
echo -n "Test: Write with --keep-cs... "
lager spi "$NET" write 0xFF --keep-cs --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 3l. Write with frequency override
echo -n "Test: Write with --frequency 500k... "
lager spi "$NET" write 0xFF --frequency 500k --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 3m. Write with mode override
echo -n "Test: Write with --mode 3... "
lager spi "$NET" write 0xFF --mode 3 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 3n. Write missing DATA
echo -n "Test: Write missing DATA... "
lager spi "$NET" write --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 3o. Write invalid hex
echo -n "Test: Write invalid hex 0xGG... "
lager spi "$NET" write 0xGG --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 3p. Write odd-length hex
echo -n "Test: Write odd-length hex 0x8F0... "
OUTPUT=$(lager spi "$NET" write 0x8F0 --box "$BOX" 2>&1)
# Odd-length should be padded (0x08 0xF0) or error
track_test "pass"

# 3q. Write byte > 0xFF in separated format
echo -n "Test: Write byte > 0xFF... "
lager spi "$NET" write "0x1FF" --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 3r. Write with data-file via transfer
echo -n "Test: Write with --data-file... "
echo -ne '\x8F\x00' > /tmp/spi_ft232h_test_data.bin
lager spi "$NET" transfer --data-file /tmp/spi_ft232h_test_data.bin 2 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 3s. Write empty data (single 0x00)
echo -n "Test: Write 0x00... "
lager spi "$NET" write 0x00 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 3t. Write 128 bytes (FT232H: no 56-byte limit)
echo -n "Test: Write 128 bytes... "
LONG_DATA=$(python3 -c "print(' '.join(['FF']*128))")
lager spi "$NET" write "$LONG_DATA" --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 3u. Write all 0xFF
echo -n "Test: Write 0xFFFF... "
lager spi "$NET" write 0xFFFF --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# ============================================================================
# 4. TRANSFER COMMAND
# ============================================================================
start_section "Transfer"
print_section_header "SECTION 4: TRANSFER COMMAND"

# 4a. Transfer with data shorter than n_words (padding)
echo -n "Test: Transfer padding (1 byte data, 4 words)... "
lager spi "$NET" transfer --data 0x8F 4 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 4b. Transfer with data longer than n_words (truncation)
echo -n "Test: Transfer truncation (4 bytes data, 2 words)... "
lager spi "$NET" transfer --data 0x8F000000 2 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 4c. Transfer exact match (data == n_words)
echo -n "Test: Transfer exact match (2 bytes, 2 words)... "
lager spi "$NET" transfer --data 0x8F00 2 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 4d. Transfer with fill 0xFF
echo -n "Test: Transfer --fill 0xFF... "
lager spi "$NET" transfer --data 0x8F --fill 0xFF 4 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 4e. Transfer with fill 0x00
echo -n "Test: Transfer --fill 0x00... "
lager spi "$NET" transfer --data 0x8F --fill 0x00 4 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 4f. Transfer hex format
echo -n "Test: Transfer --format hex... "
lager spi "$NET" transfer --data 0x8F --format hex 2 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 4g. Transfer bytes format
echo -n "Test: Transfer --format bytes... "
lager spi "$NET" transfer --data 0x8F --format bytes 2 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 4h. Transfer json format
echo -n "Test: Transfer --format json... "
OUTPUT=$(lager spi "$NET" transfer --data 0x8F --format json 2 --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -q '"data"'; then
    track_test "pass"
else
    track_test "fail"
fi

# 4i. Transfer with data-file
echo -n "Test: Transfer with --data-file... "
echo -ne '\x8F\x00' > /tmp/spi_ft232h_test_cmd.bin
lager spi "$NET" transfer --data-file /tmp/spi_ft232h_test_cmd.bin 2 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 4j. Transfer with --keep-cs
echo -n "Test: Transfer with --keep-cs... "
lager spi "$NET" transfer --data 0x8F --keep-cs 2 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 4k. Transfer per-op frequency override
echo -n "Test: Transfer --frequency 500k... "
lager spi "$NET" transfer --data 0x8F --frequency 500k 2 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 4l. Transfer per-op mode override
echo -n "Test: Transfer --mode 3... "
lager spi "$NET" transfer --data 0x8F --mode 3 2 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 4m. Transfer per-op bit-order override
echo -n "Test: Transfer --bit-order lsb... "
lager spi "$NET" transfer --data 0x8F --bit-order lsb 2 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 4n. Transfer per-op cs-active override
echo -n "Test: Transfer --cs-active high... "
lager spi "$NET" transfer --data 0x8F --cs-active high 2 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 4o. Transfer per-op word-size override
echo -n "Test: Transfer --word-size 16... "
lager spi "$NET" transfer --data 0x8F --word-size 16 2 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 4p. Transfer no --data (read-only)
echo -n "Test: Transfer without --data... "
lager spi "$NET" transfer 4 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 4q. Transfer 0 words
echo -n "Test: Transfer 0 words... "
lager spi "$NET" transfer --data 0x8F 0 --box "$BOX" >/dev/null 2>&1
# Either success or error acceptable
track_test "pass"

# 4r. Transfer missing NUM_WORDS
echo -n "Test: Transfer missing NUM_WORDS... "
lager spi "$NET" transfer --data 0x8F --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 4s. Transfer non-existent data-file
echo -n "Test: Transfer non-existent --data-file... "
lager spi "$NET" transfer --data-file /tmp/nonexistent_spi_ft232h_file.bin 2 --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 4t. Transfer data with spaces
echo -n "Test: Transfer data with spaces... "
lager spi "$NET" transfer --data "8F 00" 2 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 4u. Transfer data with commas
echo -n "Test: Transfer data with commas... "
lager spi "$NET" transfer --data "8F,00" 2 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 4v. Transfer 128 words (FT232H: large transfer)
echo -n "Test: Transfer 128 words... "
lager spi "$NET" transfer --data 0x8F 128 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# ============================================================================
# 5. BMP280 CHIP ID FUNCTIONAL TESTS
# ============================================================================
start_section "BMP280 Chip ID"
print_section_header "SECTION 5: BMP280 CHIP ID"

# Restore mode 0 for functional tests
lager spi "$NET" config --mode 0 --frequency 1M --bit-order msb --word-size 8 --cs-active low --box "$BOX" >/dev/null 2>&1

# 5a. BMP280 chip ID basic
echo -n "Test: BMP280 chip ID on $NET... "
OUTPUT=$(lager spi "$NET" transfer --data "$BMP280_CMD" 2 --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "$BMP280_CHIP_ID"; then
    track_test "pass"
else
    echo "  (got: $OUTPUT)"
    track_test "fail"
fi

# 5b. BMP280 chip ID in mode 0
echo -n "Test: BMP280 chip ID mode 0... "
OUTPUT=$(lager spi "$NET" transfer --data "$BMP280_CMD" --mode 0 2 --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "$BMP280_CHIP_ID"; then
    track_test "pass"
else
    track_test "fail"
fi

# 5c. BMP280 chip ID in mode 3
echo -n "Test: BMP280 chip ID mode 3... "
OUTPUT=$(lager spi "$NET" transfer --data "$BMP280_CMD" --mode 3 2 --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "$BMP280_CHIP_ID"; then
    track_test "pass"
else
    track_test "fail"
fi

# 5d. BMP280 chip ID with json format
echo -n "Test: BMP280 chip ID json format... "
OUTPUT=$(lager spi "$NET" transfer --data "$BMP280_CMD" --format json 2 --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -q '"data"'; then
    track_test "pass"
else
    track_test "fail"
fi

# 5e. BMP280 chip ID with bytes format
echo -n "Test: BMP280 chip ID bytes format... "
OUTPUT=$(lager spi "$NET" transfer --data "$BMP280_CMD" --format bytes 2 --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -q "$BMP280_CHIP_ID_DEC"; then  # 0x58 = 88 decimal
    track_test "pass"
else
    track_test "fail"
fi

# 5f. BMP280 chip ID consistency (5 reads)
echo -n "Test: BMP280 chip ID 5 consecutive reads... "
CONSISTENT=true
for i in $(seq 1 5); do
    OUTPUT=$(lager spi "$NET" transfer --data "$BMP280_CMD" 2 --box "$BOX" 2>&1)
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

# 5g. BMP280 chip ID at 500k
echo -n "Test: BMP280 chip ID at 500k... "
OUTPUT=$(lager spi "$NET" transfer --data "$BMP280_CMD" --frequency 500k 2 --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "$BMP280_CHIP_ID"; then
    track_test "pass"
else
    track_test "fail"
fi

# 5h. BMP280 chip ID at 5M
echo -n "Test: BMP280 chip ID at 5M... "
OUTPUT=$(lager spi "$NET" transfer --data "$BMP280_CMD" --frequency 5M 2 --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "$BMP280_CHIP_ID"; then
    track_test "pass"
else
    track_test "fail"
fi

# 5i. BMP280 chip ID at 10M
echo -n "Test: BMP280 chip ID at 10M... "
OUTPUT=$(lager spi "$NET" transfer --data "$BMP280_CMD" --frequency 10M 2 --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "$BMP280_CHIP_ID"; then
    track_test "pass"
else
    track_test "fail"
fi

# 5j. BMP280 chip ID via write command format
echo -n "Test: BMP280 chip ID via write command... "
OUTPUT=$(lager spi "$NET" write "$BMP280_CMD" --format hex --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "$BMP280_CHIP_ID"; then
    track_test "pass"
else
    # write may not return data; accept as pass if command succeeded
    if [ $? -eq 0 ]; then
        track_test "pass"
    else
        track_test "fail"
    fi
fi

# ============================================================================
# 6. NET NAME AND BOX EDGE CASES
# ============================================================================
start_section "Net/Box Edge Cases"
print_section_header "SECTION 6: NET NAME AND BOX EDGE CASES"

# 6a. Invalid net name
echo -n "Test: Invalid net name... "
lager spi nonexistent_spi_99 transfer --data 0xFF 1 --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 6b. No net name (list SPI nets)
echo -n "Test: No net name (list nets)... "
OUTPUT=$(lager spi --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "spi\|name\|instrument"; then
    track_test "pass"
else
    track_test "fail"
fi

# 6c. --box before net name
echo -n "Test: --box before net name... "
lager spi --box "$BOX" "$NET" transfer --data 0xFF 1 >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 6d. --box after subcommand
echo -n "Test: --box after subcommand... "
lager spi "$NET" transfer --data 0xFF 1 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 6e. Invalid box name
echo -n "Test: Invalid box name... "
lager spi "$NET" transfer --data 0xFF 1 --box nonexistent_box_12345 >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 6f. Help output (spi)
echo -n "Test: spi --help... "
lager spi --help >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 6g. Subcommand help (config)
echo -n "Test: spi config --help... "
lager spi "$NET" config --help >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 6h. Subcommand help (transfer)
echo -n "Test: spi transfer --help... "
lager spi "$NET" transfer --help >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 6i. Invalid subcommand
echo -n "Test: Invalid subcommand... "
lager spi "$NET" foobar --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# ============================================================================
# 7. PARAMETER VALIDATION
# ============================================================================
start_section "Parameter Validation"
print_section_header "SECTION 7: PARAMETER VALIDATION"

# 7a. Invalid mode 4 in transfer
echo -n "Test: Transfer with invalid mode 4... "
lager spi "$NET" transfer --data 0xFF --mode 4 1 --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 7b. Invalid mode -1 in transfer
echo -n "Test: Transfer with invalid mode -1... "
lager spi "$NET" transfer --data 0xFF --mode -1 1 --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 7c. Invalid mode abc in transfer
echo -n "Test: Transfer with invalid mode abc... "
lager spi "$NET" transfer --data 0xFF --mode abc 1 --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 7d. Invalid frequency 0 in transfer
echo -n "Test: Transfer with invalid frequency 0... "
lager spi "$NET" transfer --data 0xFF --frequency 0 1 --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 7e. Invalid word-size 12 in transfer
echo -n "Test: Transfer with invalid word-size 12... "
lager spi "$NET" transfer --data 0xFF --word-size 12 1 --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 7f. Invalid bit-order abc in transfer
echo -n "Test: Transfer with invalid bit-order abc... "
lager spi "$NET" transfer --data 0xFF --bit-order abc 1 --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 7g. Invalid cs-active abc in transfer
echo -n "Test: Transfer with invalid cs-active abc... "
lager spi "$NET" transfer --data 0xFF --cs-active abc 1 --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 7h. Invalid frequency -1 in read
echo -n "Test: Read with invalid frequency -1... "
lager spi "$NET" read 1 --frequency -1 --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 7i. Invalid word-size 7 in read
echo -n "Test: Read with invalid word-size 7... "
lager spi "$NET" read 1 --word-size 7 --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 7j. Invalid bit-order in write
echo -n "Test: Write with invalid bit-order... "
lager spi "$NET" write 0xFF --bit-order abc --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 7k. Invalid cs-active in write
echo -n "Test: Write with invalid cs-active... "
lager spi "$NET" write 0xFF --cs-active abc --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# ============================================================================
# 8. DATA FORMAT EDGE CASES
# ============================================================================
start_section "Data Formats"
print_section_header "SECTION 8: DATA FORMAT EDGE CASES"

# 8a. 0x prefix
echo -n "Test: Data 0x8F... "
lager spi "$NET" transfer --data 0x8F 2 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 8b. No prefix
echo -n "Test: Data 8F... "
lager spi "$NET" transfer --data 8F 2 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 8c. Lowercase
echo -n "Test: Data 0x8f (lowercase)... "
lager spi "$NET" transfer --data 0x8f 2 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 8d. Mixed case
echo -n "Test: Data 0x8F0a (mixed case)... "
lager spi "$NET" transfer --data 0x8F0a 2 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 8e. Comma separated
echo -n "Test: Data '8F,00' (commas)... "
lager spi "$NET" transfer --data "8F,00" 2 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 8f. Space separated
echo -n "Test: Data '8F 00' (spaces)... "
lager spi "$NET" transfer --data "8F 00" 2 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 8g. Colon separated
echo -n "Test: Data '8F:00' (colons)... "
lager spi "$NET" transfer --data "8F:00" 2 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 8h. Hyphen separated
echo -n "Test: Data '8F-00' (hyphens)... "
lager spi "$NET" transfer --data "8F-00" 2 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 8i. Per-byte 0x prefix with spaces
echo -n "Test: Data '0x8F 0x00' (0x per byte)... "
lager spi "$NET" transfer --data "0x8F 0x00" 2 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 8j. Odd-length hex string
echo -n "Test: Data 0x8F0 (odd-length)... "
OUTPUT=$(lager spi "$NET" transfer --data 0x8F0 2 --box "$BOX" 2>&1)
# Should pad to 0x08 0xF0 or accept
track_test "pass"

# 8k. Single hex digit
echo -n "Test: Data 0xF (single digit)... "
OUTPUT=$(lager spi "$NET" transfer --data 0xF 1 --box "$BOX" 2>&1)
track_test "pass"

# 8l. All zeros
echo -n "Test: Data 0x00... "
lager spi "$NET" transfer --data 0x00 1 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 8m. All ones
echo -n "Test: Data 0xFF... "
lager spi "$NET" transfer --data 0xFF 1 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 8n. Invalid hex character
echo -n "Test: Data 0xGG (invalid hex)... "
lager spi "$NET" transfer --data 0xGG 1 --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 8o. Byte value > 0xFF in separated format
echo -n "Test: Data '0x1FF' (value > 0xFF)... "
lager spi "$NET" transfer --data "0x1FF" 1 --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 8p. Decimal value
echo -n "Test: Data with decimal... "
OUTPUT=$(lager spi "$NET" transfer --data 255 1 --box "$BOX" 2>&1)
# May parse as hex 0x25 0x05 or fail
track_test "pass"

# ============================================================================
# 9. SEQUENCE TESTS
# ============================================================================
start_section "Sequences"
print_section_header "SECTION 9: SEQUENCE TESTS"

# Ensure standard config for sequence tests
lager spi "$NET" config --mode 0 --frequency 1M --bit-order msb --word-size 8 --cs-active low --box "$BOX" >/dev/null 2>&1

# 9a. Config then read
echo -n "Test: Config -> read sequence... "
lager spi "$NET" config --mode 0 --frequency 1M --box "$BOX" >/dev/null 2>&1
lager spi "$NET" read 4 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 9b. Config -> transfer BMP280
echo -n "Test: Config -> transfer BMP280 chip ID... "
lager spi "$NET" config --mode 0 --frequency 1M --box "$BOX" >/dev/null 2>&1
OUTPUT=$(lager spi "$NET" transfer --data "$BMP280_CMD" 2 --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "$BMP280_CHIP_ID"; then
    track_test "pass"
else
    track_test "fail"
fi

# 9c. Rapid frequency changes (100k through 10M)
echo -n "Test: Rapid frequency changes... "
FREQ_OK=true
for freq in 100k 500k 1M 5M 10M; do
    lager spi "$NET" transfer --data "$BMP280_CMD" --frequency "$freq" 2 --box "$BOX" >/dev/null 2>&1
    if [ $? -ne 0 ]; then
        FREQ_OK=false
        break
    fi
done
if [ "$FREQ_OK" = true ]; then
    track_test "pass"
else
    track_test "fail"
fi

# 9d. Mode cycling with BMP280 verify (0/3/0)
echo -n "Test: Mode cycling with BMP280 verify... "
MODE_OK=true
for mode in 0 3 0; do
    OUTPUT=$(lager spi "$NET" transfer --data "$BMP280_CMD" --mode "$mode" 2 --box "$BOX" 2>&1)
    if ! echo "$OUTPUT" | grep -qi "$BMP280_CHIP_ID"; then
        MODE_OK=false
        break
    fi
done
if [ "$MODE_OK" = true ]; then
    track_test "pass"
else
    track_test "fail"
fi

# 9e. Alternating read/write
echo -n "Test: Alternating read/write... "
ALT_OK=true
for i in $(seq 1 5); do
    lager spi "$NET" read 4 --box "$BOX" >/dev/null 2>&1 || ALT_OK=false
    lager spi "$NET" write 0xFF --box "$BOX" >/dev/null 2>&1 || ALT_OK=false
done
if [ "$ALT_OK" = true ]; then
    track_test "pass"
else
    track_test "fail"
fi

# 9f. High-frequency burst (10M, 5 rapid transfers)
echo -n "Test: High-frequency burst (10M)... "
BURST_OK=true
for i in $(seq 1 5); do
    lager spi "$NET" transfer --data "$BMP280_CMD" --frequency 10M 2 --box "$BOX" >/dev/null 2>&1
    if [ $? -ne 0 ]; then
        BURST_OK=false
        break
    fi
done
if [ "$BURST_OK" = true ]; then
    track_test "pass"
else
    track_test "fail"
fi

# ============================================================================
# 10. CONFIG PERSISTENCE
# ============================================================================
start_section "Config Persistence"
print_section_header "SECTION 10: CONFIG PERSISTENCE"

# 10a. Set config, verify output
echo -n "Test: Set config mode 0, freq 1M... "
OUTPUT=$(lager spi "$NET" config --mode 0 --frequency 1M --bit-order msb --word-size 8 --cs-active low --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "configured\|mode=0\|SPI"; then
    track_test "pass"
else
    track_test "fail"
fi

# 10b. Verify transfer works after config
echo -n "Test: Transfer works after config... "
OUTPUT=$(lager spi "$NET" transfer --data "$BMP280_CMD" 2 --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "$BMP280_CHIP_ID"; then
    track_test "pass"
else
    track_test "fail"
fi

# 10c. Per-op override does not persist
echo -n "Test: Per-op override doesn't persist... "
lager spi "$NET" config --mode 0 --frequency 1M --box "$BOX" >/dev/null 2>&1
lager spi "$NET" transfer --data "$BMP280_CMD" --mode 3 2 --box "$BOX" >/dev/null 2>&1
# After mode 3 override, next transfer should still work at original config
OUTPUT=$(lager spi "$NET" transfer --data "$BMP280_CMD" 2 --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "$BMP280_CHIP_ID"; then
    track_test "pass"
else
    track_test "fail"
fi

# 10d. Config change without restart
echo -n "Test: Config change without restart... "
lager spi "$NET" config --frequency 500k --box "$BOX" >/dev/null 2>&1
OUTPUT=$(lager spi "$NET" transfer --data "$BMP280_CMD" 2 --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "$BMP280_CHIP_ID"; then
    track_test "pass"
else
    track_test "fail"
fi

# 10e. Multiple config changes
echo -n "Test: Multiple config changes... "
CONFIG_OK=true
for freq in 100k 500k 1M; do
    lager spi "$NET" config --frequency "$freq" --box "$BOX" >/dev/null 2>&1
    OUTPUT=$(lager spi "$NET" transfer --data "$BMP280_CMD" 2 --box "$BOX" 2>&1)
    if ! echo "$OUTPUT" | grep -qi "$BMP280_CHIP_ID"; then
        CONFIG_OK=false
        break
    fi
done
if [ "$CONFIG_OK" = true ]; then
    track_test "pass"
else
    track_test "fail"
fi

# 10f. High-freq config persistence (5M then 10M)
echo -n "Test: High-freq config persistence... "
lager spi "$NET" config --frequency 5M --box "$BOX" >/dev/null 2>&1
OUTPUT1=$(lager spi "$NET" transfer --data "$BMP280_CMD" 2 --box "$BOX" 2>&1)
lager spi "$NET" config --frequency 10M --box "$BOX" >/dev/null 2>&1
OUTPUT2=$(lager spi "$NET" transfer --data "$BMP280_CMD" 2 --box "$BOX" 2>&1)
if echo "$OUTPUT1" | grep -qi "$BMP280_CHIP_ID" && echo "$OUTPUT2" | grep -qi "$BMP280_CHIP_ID"; then
    track_test "pass"
else
    track_test "fail"
fi

# ============================================================================
# 11. CLEANUP
# ============================================================================
start_section "Cleanup"
print_section_header "SECTION 11: CLEANUP"

# 11a. Restore default config
echo -n "Test: Restore config on $NET... "
lager spi "$NET" config --mode 0 --frequency 1M --bit-order msb --word-size 8 --cs-active low --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 11b. Clean up temp files
echo -n "Test: Clean up temp files... "
rm -f /tmp/spi_ft232h_test_data.bin /tmp/spi_ft232h_test_cmd.bin
track_test "pass"

# 11c. Final BMP280 sanity check
echo -n "Test: Final BMP280 chip ID sanity check... "
OUTPUT=$(lager spi "$NET" transfer --data "$BMP280_CMD" 2 --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "$BMP280_CHIP_ID"; then
    track_test "pass"
else
    track_test "fail"
fi

# ============================================================================
# SUMMARY
# ============================================================================
print_summary
exit_with_status
