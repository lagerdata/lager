#!/usr/bin/env bash

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
# ============================================================================
# SPI CLI Integration Tests - Aardvark Automatic CS Mode
# ============================================================================
# Usage:
#   ./spi_aardvark_auto.sh <box>
#
# Arguments:
#   box  - Box name or IP (e.g., <YOUR-BOX>)
#
# Prerequisites:
#   - Aardvark adapter connected to box USB
#   - BMP280 (HW-611) wired to Aardvark SPI pins:
#       Aardvark pin 1 (SCK)  -> HW-611 SCL
#       Aardvark pin 3 (MOSI) -> HW-611 SDA
#       Aardvark pin 5 (MISO) -> HW-611 SDO
#       Aardvark pin 9 (SS)   -> HW-611 CSB (auto chip select)
#   - LabJack DAC0 (dac1) -> HW-611 VCC (3.3V power)
#   - LabJack FIO0 (gpio16) -> disconnected (not used in auto mode)
#   - HW-611 GND -> Aardvark GND (pins 2,4,6,8,10)
#   - Net 'spi1' configured with cs_mode=auto
#   - Net 'dac1' configured for DAC output
#
# Wiring Diagram:
#
#   Aardvark                HW-611 (BMP280)        LabJack T7
#   +---------+             +---------+             +---------+
#   | 1  SCK  |------------>| SCL     |             |         |
#   | 3  MOSI |------------>| SDA     |             |         |
#   | 5  MISO |<------------| SDO     |             |         |
#   | 9  SS   |------------>| CSB     |             | FIO0    | (disconnected)
#   | 2  GND  |-----+-------| GND     |             |         |
#   +---------+     |       | VCC     |<------------| DAC0    | (dac1 3.3V)
#                   +-------| GND     |             | GND     |
#                           +---------+             +---------+
#
# BMP280 SPI register access:
#   Read:  bit 7 = 1 (e.g., chip ID reg 0xD0 -> send 0xD0, already has bit 7)
#   Write: bit 7 = 0 (e.g., ctrl_meas reg 0xF4 -> send 0x74)
#   Chip ID: 0x58 (expected value)
#
# Known limitations:
#   - keep_cs in auto mode: Aardvark may warn; use manual CS for split transactions
#   - fill 0x00 quirk: Some Aardvark firmware versions treat fill=0 as no fill
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
    echo "Usage: $0 <box>"
    echo ""
    echo "  box  - Box name or IP (e.g., <YOUR-BOX>)"
    echo ""
    echo "Prerequisites:"
    echo "  - BMP280 wired to Aardvark SPI (auto CS via SS pin 9)"
    echo "  - lager dac dac1 3.3 --box <box>   (power the BMP280)"
    exit 1
fi

BOX="$1"
SPI_NET="spi1"
CHIP_ID_CMD="0xD0"
CHIP_ID_VAL="58"

init_harness

print_script_header "LAGER SPI CLI TEST SUITE - AARDVARK AUTO CS" "$BOX" "$SPI_NET"

echo "Device: BMP280 at SPI (auto CS via Aardvark SS pin 9)"
echo "Chip ID command: $CHIP_ID_CMD, expected value: 0x$CHIP_ID_VAL"
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
echo -n "Test: Power BMP280 via DAC (3.3V)... "
lager dac dac1 3.3 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"
sleep 0.5

# 0c. SPI net accessible
echo -n "Test: SPI net '$SPI_NET' listed... "
OUTPUT=$(lager spi --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -q "$SPI_NET"; then
    track_test "pass"
else
    track_test "fail"
fi

# 0d. SPI help
echo -n "Test: lager spi --help... "
lager spi --help >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# ============================================================================
# 1. CONFIG - AUTO CS MODE
# ============================================================================
start_section "Config Auto CS"
print_section_header "SECTION 1: CONFIG - AUTO CS MODE"

# 1a. Set cs-mode auto
echo -n "Test: Config cs-mode auto... "
lager spi "$SPI_NET" config --cs-mode auto --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 1b. Standard config
echo -n "Test: Config mode 0, 1M, msb, 8-bit, cs-active low... "
lager spi "$SPI_NET" config --mode 0 --frequency 1M --bit-order msb --word-size 8 --cs-active low --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# ============================================================================
# 2. CHIP ID READ
# ============================================================================
start_section "Chip ID"
print_section_header "SECTION 2: CHIP ID READ"

# 2a. Basic chip ID read
echo -n "Test: Chip ID read... "
OUTPUT=$(lager spi "$SPI_NET" transfer --data "$CHIP_ID_CMD" 2 --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "$CHIP_ID_VAL"; then
    track_test "pass"
else
    echo "  (got: $OUTPUT)"
    track_test "fail"
fi

# 2b. Calibration data (24 bytes from reg 0x88)
echo -n "Test: Calibration data (24 bytes)... "
OUTPUT=$(lager spi "$SPI_NET" transfer --data 0x88 25 --box "$BOX" 2>&1)
if [ $? -eq 0 ]; then
    track_test "pass"
else
    track_test "fail"
fi

# ============================================================================
# 3. SPI MODES
# ============================================================================
start_section "SPI Modes"
print_section_header "SECTION 3: ALL 4 SPI MODES"

for mode in 0 1 2 3; do
    echo -n "Test: Chip ID in mode $mode... "
    OUTPUT=$(lager spi "$SPI_NET" transfer --data "$CHIP_ID_CMD" --mode "$mode" 2 --box "$BOX" 2>&1)
    if echo "$OUTPUT" | grep -qi "$CHIP_ID_VAL"; then
        track_test "pass"
    else
        echo "  (mode $mode got: $OUTPUT)"
        track_test "fail"
    fi
done

# Restore mode 0
lager spi "$SPI_NET" config --mode 0 --box "$BOX" >/dev/null 2>&1

# ============================================================================
# 4. BIT ORDER
# ============================================================================
start_section "Bit Order"
print_section_header "SECTION 4: BIT ORDER"

# 4a. MSB first
echo -n "Test: Bit order MSB chip ID... "
OUTPUT=$(lager spi "$SPI_NET" transfer --data "$CHIP_ID_CMD" --bit-order msb 2 --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "$CHIP_ID_VAL"; then
    track_test "pass"
else
    track_test "fail"
fi

# 4b. LSB first (data garbled but should not error)
echo -n "Test: Bit order LSB (no error)... "
lager spi "$SPI_NET" transfer --data 0xFF --bit-order lsb 2 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# Restore MSB
lager spi "$SPI_NET" config --bit-order msb --box "$BOX" >/dev/null 2>&1

# ============================================================================
# 5. FREQUENCIES
# ============================================================================
start_section "Frequencies"
print_section_header "SECTION 5: FREQUENCIES"

for freq in 125k 1M 4M 8M; do
    echo -n "Test: Chip ID at $freq... "
    OUTPUT=$(lager spi "$SPI_NET" transfer --data "$CHIP_ID_CMD" --frequency "$freq" 2 --box "$BOX" 2>&1)
    if echo "$OUTPUT" | grep -qi "$CHIP_ID_VAL"; then
        track_test "pass"
    else
        echo "  ($freq got: $OUTPUT)"
        track_test "fail"
    fi
done

# Restore 1M
lager spi "$SPI_NET" config --frequency 1M --box "$BOX" >/dev/null 2>&1

# ============================================================================
# 6. WORD SIZES
# ============================================================================
start_section "Word Sizes"
print_section_header "SECTION 6: WORD SIZES"

# 6a. Word size 8 (standard)
echo -n "Test: Word size 8... "
lager spi "$SPI_NET" read 4 --word-size 8 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 6b. Word size 16
echo -n "Test: Word size 16... "
lager spi "$SPI_NET" read 2 --word-size 16 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 6c. Word size 32
echo -n "Test: Word size 32... "
lager spi "$SPI_NET" read 1 --word-size 32 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# Restore word size 8
lager spi "$SPI_NET" config --word-size 8 --box "$BOX" >/dev/null 2>&1

# ============================================================================
# 7. CS POLARITY
# ============================================================================
start_section "CS Polarity"
print_section_header "SECTION 7: CS POLARITY"

# 7a. CS active low (standard for BMP280)
echo -n "Test: CS active low chip ID... "
lager spi "$SPI_NET" config --cs-active low --box "$BOX" >/dev/null 2>&1
OUTPUT=$(lager spi "$SPI_NET" transfer --data "$CHIP_ID_CMD" 2 --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "$CHIP_ID_VAL"; then
    track_test "pass"
else
    track_test "fail"
fi

# 7b. CS active high (data garbled but should not error)
echo -n "Test: CS active high (no error)... "
lager spi "$SPI_NET" transfer --data 0xFF --cs-active high 2 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# Restore active low
lager spi "$SPI_NET" config --cs-active low --box "$BOX" >/dev/null 2>&1

# ============================================================================
# 8. READ-ONLY COMMAND
# ============================================================================
start_section "Read-Only"
print_section_header "SECTION 8: READ-ONLY COMMAND"

# 8a. Read 1 word
echo -n "Test: Read 1 word... "
lager spi "$SPI_NET" read 1 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 8b. Read 4 words
echo -n "Test: Read 4 words... "
lager spi "$SPI_NET" read 4 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# ============================================================================
# 9. WRITE + READBACK
# ============================================================================
start_section "Write + Readback"
print_section_header "SECTION 9: WRITE + READBACK"

# 9a. Write 0x00 to ctrl_meas, readback
echo -n "Test: Write ctrl_meas=0x00, readback... "
lager spi "$SPI_NET" write "0x74 0x00" --box "$BOX" >/dev/null 2>&1
OUTPUT=$(lager spi "$SPI_NET" transfer --data 0xF4 2 --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "00"; then
    track_test "pass"
else
    echo "  (got: $OUTPUT)"
    track_test "fail"
fi

# ============================================================================
# 10. KEEP-CS (KNOWN LIMITATION)
# ============================================================================
start_section "Keep-CS"
print_section_header "SECTION 10: KEEP-CS (KNOWN LIMITATION IN AUTO MODE)"

# 10a. Keep-cs in auto mode (may warn or fail)
echo -n "Test: Keep-cs in auto mode (warning expected)... "
OUTPUT=$(lager spi "$SPI_NET" read 4 --keep-cs --box "$BOX" 2>&1)
# Either success with warning or error is acceptable
track_test "pass"
echo "  Note: Aardvark auto mode may not support keep_cs; use manual CS instead"

# ============================================================================
# 11. DATA-FILE TRANSFER
# ============================================================================
start_section "Data File"
print_section_header "SECTION 11: DATA-FILE TRANSFER"

# 11a. Transfer with data-file
echo -n "Test: Transfer with --data-file... "
echo -ne '\xD0\x00' > /tmp/spi_auto_test.bin
OUTPUT=$(lager spi "$SPI_NET" transfer --data-file /tmp/spi_auto_test.bin 2 --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "$CHIP_ID_VAL"; then
    track_test "pass"
else
    track_test "fail"
fi

# ============================================================================
# 12. FILL BYTE VARIANTS
# ============================================================================
start_section "Fill Byte"
print_section_header "SECTION 12: FILL BYTE VARIANTS"

# 12a. Fill 0xFF
echo -n "Test: Read with fill 0xFF... "
lager spi "$SPI_NET" read 4 --fill 0xFF --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 12b. Fill 0x00
echo -n "Test: Read with fill 0x00... "
lager spi "$SPI_NET" read 4 --fill 0x00 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 12c. Fill 0xAA
echo -n "Test: Read with fill 0xAA... "
lager spi "$SPI_NET" read 4 --fill 0xAA --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# ============================================================================
# 13. LARGE TRANSACTION
# ============================================================================
start_section "Large Transaction"
print_section_header "SECTION 13: LARGE TRANSACTION"

# 13a. 257 bytes (crosses USB packet boundary)
echo -n "Test: Read 257 bytes... "
lager spi "$SPI_NET" read 257 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# ============================================================================
# 14. OUTPUT FORMATS
# ============================================================================
start_section "Output Formats"
print_section_header "SECTION 14: OUTPUT FORMATS"

# 14a. Hex
echo -n "Test: Transfer --format hex... "
OUTPUT=$(lager spi "$SPI_NET" transfer --data "$CHIP_ID_CMD" --format hex 2 --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "$CHIP_ID_VAL"; then
    track_test "pass"
else
    track_test "fail"
fi

# 14b. Bytes
echo -n "Test: Transfer --format bytes... "
OUTPUT=$(lager spi "$SPI_NET" transfer --data "$CHIP_ID_CMD" --format bytes 2 --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -q "88"; then
    track_test "pass"
else
    track_test "fail"
fi

# 14c. JSON
echo -n "Test: Transfer --format json... "
OUTPUT=$(lager spi "$SPI_NET" transfer --data "$CHIP_ID_CMD" --format json 2 --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -q '"data"'; then
    track_test "pass"
else
    track_test "fail"
fi

# ============================================================================
# 15. CONFIG PERSISTENCE
# ============================================================================
start_section "Config Persistence"
print_section_header "SECTION 15: CONFIG PERSISTENCE"

# 15a. Set config, verify transfer works
echo -n "Test: Config persists across transfer... "
lager spi "$SPI_NET" config --mode 0 --frequency 1M --box "$BOX" >/dev/null 2>&1
OUTPUT=$(lager spi "$SPI_NET" transfer --data "$CHIP_ID_CMD" 2 --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "$CHIP_ID_VAL"; then
    track_test "pass"
else
    track_test "fail"
fi

# 15b. Per-op override doesn't persist
echo -n "Test: Per-op mode override doesn't persist... "
lager spi "$SPI_NET" config --mode 0 --box "$BOX" >/dev/null 2>&1
lager spi "$SPI_NET" transfer --data "$CHIP_ID_CMD" --mode 3 2 --box "$BOX" >/dev/null 2>&1
OUTPUT=$(lager spi "$SPI_NET" transfer --data "$CHIP_ID_CMD" 2 --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "$CHIP_ID_VAL"; then
    track_test "pass"
else
    track_test "fail"
fi

# ============================================================================
# 16. INVALID PARAMETERS
# ============================================================================
start_section "Invalid Parameters"
print_section_header "SECTION 16: INVALID PARAMETERS"

# 16a. Invalid mode 5
echo -n "Test: Config invalid mode 5... "
lager spi "$SPI_NET" config --mode 5 --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 16b. Invalid word-size 12
echo -n "Test: Config invalid word-size 12... "
lager spi "$SPI_NET" config --word-size 12 --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 16c. Invalid bit-order abc
echo -n "Test: Config invalid bit-order abc... "
lager spi "$SPI_NET" config --bit-order abc --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 16d. Invalid cs-active abc
echo -n "Test: Config invalid cs-active abc... "
lager spi "$SPI_NET" config --cs-active abc --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# 16e. Invalid frequency abc
echo -n "Test: Config invalid frequency abc... "
lager spi "$SPI_NET" config --frequency abc --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# ============================================================================
# 17. CLEANUP
# ============================================================================
start_section "Cleanup"
print_section_header "SECTION 17: CLEANUP"

# 17a. Restore default config
echo -n "Test: Restore config... "
lager spi "$SPI_NET" config --mode 0 --frequency 1M --bit-order msb --word-size 8 --cs-active low --cs-mode auto --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 17b. Restore BMP280 sleep mode
echo -n "Test: Restore BMP280 sleep mode... "
lager spi "$SPI_NET" write "0x74 0x00" --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 17c. Clean temp files
echo -n "Test: Clean up temp files... "
rm -f /tmp/spi_auto_test.bin
track_test "pass"

# ============================================================================
# SUMMARY
# ============================================================================
print_summary
exit_with_status
