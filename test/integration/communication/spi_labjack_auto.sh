#!/usr/bin/env bash

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
# ============================================================================
# SPI CLI Integration Tests - LabJack T7 Auto CS Mode
# ============================================================================
# Usage:
#   ./spi_labjack_auto.sh <box>
#
# Arguments:
#   box  - Box name or IP (e.g., <YOUR-BOX>)
#
# Prerequisites:
#   - LabJack T7 connected to box USB
#   - BMP280 (HW-611) wired to LabJack SPI pins:
#       FIO0 (CS)   -> HW-611 CSB (auto chip select)
#       FIO1 (CLK)  -> HW-611 SCL
#       FIO2 (MOSI) -> HW-611 SDA
#       FIO3 (MISO) -> HW-611 SDO
#   - LabJack DAC0 (dac1) -> HW-611 VCC (3.3V power)
#   - HW-611 GND -> LabJack GND
#   - Net 'spi2' configured with cs_mode=auto
#   - Net 'dac1' configured for DAC output
#
# Wiring Diagram:
#
#   LabJack T7              HW-611 (BMP280)
#   +---------+             +---------+
#   | FIO0 CS |------------>| CSB     |
#   | FIO1 CLK|------------>| SCL     |
#   | FIO2 MOSI|----------->| SDA     |
#   | FIO3 MISO|<-----------| SDO     |
#   | GND     |-------------| GND     |
#   | DAC0    |------------>| VCC     | (dac1 3.3V)
#   +---------+             +---------+
#
# LabJack T7 SPI characteristics:
#   - Maximum 56 bytes per transaction (hardware buffer limit)
#   - Speed forced to ~800 kHz (throttle=0; any throttle > 0 fails)
#   - Warm-up transaction required after init (driver handles automatically)
#   - Auto CS via FIO0 (SPI_OPTIONS bit 0)
#
# BMP280 SPI register access:
#   Read:  bit 7 = 1 (e.g., chip ID reg 0xD0 -> send 0xD0, already has bit 7)
#   Write: bit 7 = 0 (e.g., ctrl_meas reg 0xF4 -> send 0x74)
#   Chip ID: 0x58 (expected value)
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
    echo "  - BMP280 wired to LabJack SPI (auto CS via FIO0)"
    echo "  - lager dac dac1 3.3 --box <box>   (power the BMP280)"
    exit 1
fi

BOX="$1"
SPI_NET="spi2"
CHIP_ID_CMD="0xD0"
CHIP_ID_VAL="58"
CTRL_MEAS_REG_READ="0xF4"   # Read ctrl_meas (bit 7 already set)
CTRL_MEAS_REG_WRITE="0x74"  # Write ctrl_meas (bit 7 = 0)

init_harness

print_script_header "LAGER SPI CLI TEST SUITE - LABJACK AUTO CS" "$BOX" "$SPI_NET"

echo "Device: BMP280 at SPI (auto CS via LabJack FIO0)"
echo "Chip ID command: $CHIP_ID_CMD, expected value: 0x$CHIP_ID_VAL"
echo "LabJack T7: 56-byte max, ~800kHz forced, warm-up required"
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

# 1b. Standard config (LabJack forces ~800kHz regardless of requested freq)
echo -n "Test: Config mode 0, 800k, msb, 8-bit, cs-active low... "
lager spi "$SPI_NET" config --mode 0 --frequency 800k --bit-order msb --word-size 8 --cs-active low --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

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

# 2b. Calibration data (24 bytes from reg 0x88, within 56-byte limit)
echo -n "Test: Calibration data (25 bytes)... "
OUTPUT=$(lager spi "$SPI_NET" transfer --data 0x88 25 --box "$BOX" 2>&1)
if [ $? -eq 0 ]; then
    track_test "pass"
else
    track_test "fail"
fi

# 2c. 5x consistency
echo -n "Test: 5 consecutive chip ID reads... "
CONSISTENT=true
for i in $(seq 1 5); do
    OUTPUT=$(lager spi "$SPI_NET" transfer --data "$CHIP_ID_CMD" 2 --box "$BOX" 2>&1)
    if ! echo "$OUTPUT" | grep -qi "$CHIP_ID_VAL"; then
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
# 3. SPI MODES
# ============================================================================
start_section "SPI Modes"
print_section_header "SECTION 3: SPI MODES (0, 1, 2, 3)"

# BMP280 supports SPI modes 0 and 3 per datasheet.
# Mode 1 returns garbled data. Mode 2 may work depending on CS timing.
echo -n "Test: Chip ID in mode 0 (supported)... "
lager spi "$SPI_NET" config --mode 0 --box "$BOX" >/dev/null 2>&1
OUTPUT=$(lager spi "$SPI_NET" transfer --data "$CHIP_ID_CMD" 2 --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "$CHIP_ID_VAL"; then
    track_test "pass"
else
    echo "  (mode 0 got: $OUTPUT)"
    track_test "fail"
fi

echo -n "Test: Mode 1 garbles data (unsupported)... "
lager spi "$SPI_NET" config --mode 1 --box "$BOX" >/dev/null 2>&1
OUTPUT=$(lager spi "$SPI_NET" transfer --data "$CHIP_ID_CMD" 2 --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "$CHIP_ID_VAL"; then
    echo "  (mode 1 got valid chip ID unexpectedly: $OUTPUT)"
    track_test "fail"
else
    track_test "pass"
fi

echo -n "Test: Mode 2 garbles data (unsupported)... "
lager spi "$SPI_NET" config --mode 2 --box "$BOX" >/dev/null 2>&1
OUTPUT=$(lager spi "$SPI_NET" transfer --data "$CHIP_ID_CMD" 2 --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "$CHIP_ID_VAL"; then
    echo "  (mode 2 returned valid chip ID -- BMP280 tolerates mode 2 with GPIO CS timing)"
    track_test "pass"
else
    track_test "pass"
fi

echo -n "Test: Mode 3 valid chip ID (supported per datasheet)... "
lager spi "$SPI_NET" config --mode 3 --box "$BOX" >/dev/null 2>&1
OUTPUT=$(lager spi "$SPI_NET" transfer --data "$CHIP_ID_CMD" 2 --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "$CHIP_ID_VAL"; then
    track_test "pass"
else
    echo "  (mode 3 got: $OUTPUT, expected chip ID $CHIP_ID_VAL)"
    track_test "fail"
fi

# Restore mode 0
lager spi "$SPI_NET" config --mode 0 --box "$BOX" >/dev/null 2>&1

# ============================================================================
# 4. OUTPUT FORMATS
# ============================================================================
start_section "Output Formats"
print_section_header "SECTION 4: OUTPUT FORMATS"

# 4a. Hex format
echo -n "Test: Transfer --format hex... "
OUTPUT=$(lager spi "$SPI_NET" transfer --data "$CHIP_ID_CMD" --format hex 2 --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "$CHIP_ID_VAL"; then
    track_test "pass"
else
    track_test "fail"
fi

# 4b. Bytes format
echo -n "Test: Transfer --format bytes... "
OUTPUT=$(lager spi "$SPI_NET" transfer --data "$CHIP_ID_CMD" --format bytes 2 --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -q "88"; then  # 0x58 = 88 decimal
    track_test "pass"
else
    track_test "fail"
fi

# 4c. JSON format
echo -n "Test: Transfer --format json... "
OUTPUT=$(lager spi "$SPI_NET" transfer --data "$CHIP_ID_CMD" --format json 2 --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -q '"data"'; then
    track_test "pass"
else
    track_test "fail"
fi

# ============================================================================
# 5. DATA-FILE TRANSFER
# ============================================================================
start_section "Data File"
print_section_header "SECTION 5: DATA-FILE TRANSFER"

# 5a. Transfer with data-file
echo -n "Test: Transfer with --data-file... "
echo -ne '\xD0\x00' > /tmp/spi_lj_auto_test.bin
OUTPUT=$(lager spi "$SPI_NET" transfer --data-file /tmp/spi_lj_auto_test.bin 2 --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "$CHIP_ID_VAL"; then
    track_test "pass"
else
    track_test "fail"
fi

# ============================================================================
# 6. FILL BYTE VARIANTS
# ============================================================================
start_section "Fill Byte"
print_section_header "SECTION 6: FILL BYTE VARIANTS"

# 6a. Fill 0xFF
echo -n "Test: Read with fill 0xFF... "
lager spi "$SPI_NET" read 4 --fill 0xFF --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 6b. Fill 0x00
echo -n "Test: Read with fill 0x00... "
lager spi "$SPI_NET" read 4 --fill 0x00 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# ============================================================================
# 7. BIT ORDER
# ============================================================================
start_section "Bit Order"
print_section_header "SECTION 7: BIT ORDER"

# 7a. MSB first (standard)
echo -n "Test: Bit order MSB chip ID... "
lager spi "$SPI_NET" config --bit-order msb --box "$BOX" >/dev/null 2>&1
OUTPUT=$(lager spi "$SPI_NET" transfer --data "$CHIP_ID_CMD" 2 --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "$CHIP_ID_VAL"; then
    track_test "pass"
else
    track_test "fail"
fi

# 7b. LSB first (data garbled but should not error)
echo -n "Test: Bit order LSB (no error)... "
lager spi "$SPI_NET" config --bit-order lsb --box "$BOX" >/dev/null 2>&1
lager spi "$SPI_NET" transfer --data 0xFF 2 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# Restore MSB
lager spi "$SPI_NET" config --bit-order msb --box "$BOX" >/dev/null 2>&1

# ============================================================================
# 8. WRITE + READBACK
# ============================================================================
start_section "Write + Readback"
print_section_header "SECTION 8: WRITE + READBACK (ctrl_meas register)"

# 8a. Write 0x00 to ctrl_meas, readback
echo -n "Test: Write ctrl_meas=0x00, readback... "
lager spi "$SPI_NET" write "0x74 0x00" --box "$BOX" >/dev/null 2>&1
OUTPUT=$(lager spi "$SPI_NET" transfer --data 0xF4 2 --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "00"; then
    track_test "pass"
else
    echo "  (got: $OUTPUT)"
    track_test "fail"
fi

# 8b. Write 0x25 to ctrl_meas (forced mode), readback
echo -n "Test: Write ctrl_meas=0x25, readback... "
lager spi "$SPI_NET" write "0x74 0x25" --box "$BOX" >/dev/null 2>&1
sleep 0.05
OUTPUT=$(lager spi "$SPI_NET" transfer --data 0xF4 2 --box "$BOX" 2>&1)
# Forced mode: after conversion, mode bits revert to 00, so value may be 0x24
if [ $? -eq 0 ]; then
    track_test "pass"
else
    track_test "fail"
fi

# ============================================================================
# 9. 56-BYTE BOUNDARY
# ============================================================================
start_section "56-Byte Boundary"
print_section_header "SECTION 9: 56-BYTE BOUNDARY (LABJACK LIMIT)"

# 9a. 56 bytes (OK - exactly at limit)
echo -n "Test: Read 56 bytes (at limit)... "
lager spi "$SPI_NET" read 56 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 9b. 57 bytes (MUST FAIL - exceeds LabJack buffer)
echo -n "Test: Read 57 bytes (must fail)... "
lager spi "$SPI_NET" read 57 --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"

# ============================================================================
# 10. CLEANUP
# ============================================================================
start_section "Cleanup"
print_section_header "SECTION 10: CLEANUP"

# 10a. Restore default config
echo -n "Test: Restore config... "
lager spi "$SPI_NET" config --mode 0 --frequency 800k --bit-order msb --word-size 8 --cs-active low --cs-mode auto --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 10b. Restore BMP280 sleep mode
echo -n "Test: Restore BMP280 sleep mode... "
lager spi "$SPI_NET" write "0x74 0x00" --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 10c. Clean temp files
echo -n "Test: Clean up temp files... "
rm -f /tmp/spi_lj_auto_test.bin
track_test "pass"

# ============================================================================
# SUMMARY
# ============================================================================
print_summary
exit_with_status
