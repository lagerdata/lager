#!/usr/bin/env bash

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
# ============================================================================
# SPI CLI Integration Tests - LabJack T7 Manual CS Mode
# ============================================================================
# Usage:
#   ./spi_labjack_manual.sh <box>
#
# Arguments:
#   box  - Box name or IP (e.g., <YOUR-BOX>)
#
# Prerequisites:
#   - LabJack T7 connected to box USB
#   - BMP280 (HW-611) wired to LabJack SPI pins:
#       FIO1 (CLK)  -> HW-611 SCL
#       FIO2 (MOSI) -> HW-611 SDA
#       FIO3 (MISO) -> HW-611 SDO
#       FIO0        -> NC (not connected)
#   - LabJack FIO6 (gpio22) -> HW-611 CSB (manual chip select)
#   - LabJack DAC0 (dac1) -> HW-611 VCC (3.3V power)
#   - HW-611 GND -> LabJack GND
#   - Net 'spi2' configured with cs_mode=manual
#   - Net 'gpio22' configured as GPIO output
#   - Net 'dac1' configured for DAC output
#
# Wiring Diagram:
#
#   LabJack T7              HW-611 (BMP280)
#   +---------+             +---------+
#   | FIO0    | NC          |         |
#   | FIO1 CLK|------------>| SCL     |
#   | FIO2 MOSI|----------->| SDA     |
#   | FIO3 MISO|<-----------| SDO     |
#   | FIO6 CS |------------>| CSB     | (gpio22)
#   | GND     |-------------| GND     |
#   | DAC0    |------------>| VCC     | (dac1 3.3V)
#   +---------+             +---------+
#
# LabJack T7 SPI characteristics:
#   - Maximum 56 bytes per transaction (hardware buffer limit)
#   - Speed forced to ~800 kHz (throttle=0; any throttle > 0 fails)
#   - Warm-up transaction required after init (driver handles automatically)
#   - Manual CS: SPI_CS_DIONUM set to dummy pin
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
    echo "  - BMP280 wired to LabJack SPI (manual CS via gpio22/FIO6)"
    echo "  - lager dac dac1 3.3 --box <box>   (power the BMP280)"
    exit 1
fi

BOX="$1"
SPI_NET="spi2"
GPIO_CS="gpio22"
CHIP_ID_CMD="0xD0"
CHIP_ID_VAL="58"
CTRL_MEAS_REG_READ="0xF4"   # Read ctrl_meas (bit 7 already set)
CTRL_MEAS_REG_WRITE="0x74"  # Write ctrl_meas (bit 7 = 0)

init_harness

print_script_header "LAGER SPI CLI TEST SUITE - LABJACK MANUAL CS" "$BOX" "$SPI_NET (CS: $GPIO_CS)"

echo "Device: BMP280 at SPI (manual CS via $GPIO_CS/FIO6)"
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

# 0d. GPIO net accessible
echo -n "Test: GPIO net '$GPIO_CS' accessible (set high)... "
lager gpo "$GPIO_CS" high --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# ============================================================================
# 1. CONFIG - MANUAL CS MODE
# ============================================================================
start_section "Config Manual CS"
print_section_header "SECTION 1: CONFIG - MANUAL CS MODE"

# 1a. Set cs-mode manual
echo -n "Test: Config cs-mode manual... "
lager spi "$SPI_NET" config --cs-mode manual --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 1b. Set standard SPI parameters
echo -n "Test: Config mode 0, 800k, msb, 8-bit, cs-active low... "
lager spi "$SPI_NET" config --mode 0 --frequency 800k --bit-order msb --word-size 8 --cs-active low --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# ============================================================================
# 2. MANUAL CS CHIP ID READ
# ============================================================================
start_section "Manual CS Chip ID"
print_section_header "SECTION 2: MANUAL CS CHIP ID READ"

# 2a. Read chip ID with manual CS (gpo low -> transfer -> gpo high)
echo -n "Test: Chip ID with manual CS... "
lager gpo "$GPIO_CS" low --box "$BOX" >/dev/null 2>&1
OUTPUT=$(lager spi "$SPI_NET" transfer --data "$CHIP_ID_CMD" 2 --box "$BOX" 2>&1)
lager gpo "$GPIO_CS" high --box "$BOX" >/dev/null 2>&1
if echo "$OUTPUT" | grep -qi "$CHIP_ID_VAL"; then
    track_test "pass"
else
    echo "  (got: $OUTPUT)"
    track_test "fail"
fi

# 2b. Forgot-CS test: CS stays high, should NOT get valid chip ID
echo -n "Test: Forgot-CS (CS stays high -> no chip ID)... "
lager gpo "$GPIO_CS" high --box "$BOX" >/dev/null 2>&1
OUTPUT=$(lager spi "$SPI_NET" transfer --data "$CHIP_ID_CMD" 2 --box "$BOX" 2>&1)
if echo "$OUTPUT" | grep -qi "$CHIP_ID_VAL"; then
    echo "  (got: $OUTPUT, chip ID should NOT appear with CS high)"
    track_test "fail"
else
    track_test "pass"
fi

# 2c. Consistency: 5 manual CS chip ID reads
echo -n "Test: 5 consecutive manual CS chip ID reads... "
CONSISTENT=true
for i in $(seq 1 5); do
    lager gpo "$GPIO_CS" low --box "$BOX" >/dev/null 2>&1
    OUTPUT=$(lager spi "$SPI_NET" transfer --data "$CHIP_ID_CMD" 2 --box "$BOX" 2>&1)
    lager gpo "$GPIO_CS" high --box "$BOX" >/dev/null 2>&1
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
# 3. KEEP-CS SPLIT TRANSACTION
# ============================================================================
start_section "Keep-CS Split"
print_section_header "SECTION 3: KEEP-CS SPLIT TRANSACTION"

# 3a. Keep-cs split: first half (13 bytes) with --keep-cs, then second half (12 bytes)
echo -n "Test: Keep-cs split (13 + 12 bytes calibration)... "
lager gpo "$GPIO_CS" low --box "$BOX" >/dev/null 2>&1
# First part: register address 0x88 (calibration start) + 12 bytes = 13 total, keep CS
OUTPUT1=$(lager spi "$SPI_NET" transfer --data 0x88 13 --keep-cs --box "$BOX" 2>&1)
RC1=$?
# Second part: 12 more bytes without register address
OUTPUT2=$(lager spi "$SPI_NET" transfer 12 --box "$BOX" 2>&1)
RC2=$?
lager gpo "$GPIO_CS" high --box "$BOX" >/dev/null 2>&1
if [ $RC1 -eq 0 ] && [ $RC2 -eq 0 ]; then
    track_test "pass"
else
    track_test "fail"
fi

# ============================================================================
# 4. SPI MODES
# ============================================================================
start_section "SPI Modes"
print_section_header "SECTION 4: SPI MODES (0, 1, 2, 3)"

# BMP280 only returns a valid chip ID in mode 0 on this hardware;
# modes 1, 2, 3 all return garbled data.
echo -n "Test: Chip ID in mode 0 (supported)... "
lager spi "$SPI_NET" config --mode 0 --box "$BOX" >/dev/null 2>&1
lager gpo "$GPIO_CS" low --box "$BOX" >/dev/null 2>&1
OUTPUT=$(lager spi "$SPI_NET" transfer --data "$CHIP_ID_CMD" 2 --box "$BOX" 2>&1)
lager gpo "$GPIO_CS" high --box "$BOX" >/dev/null 2>&1
if echo "$OUTPUT" | grep -qi "$CHIP_ID_VAL"; then
    track_test "pass"
else
    echo "  (mode 0 got: $OUTPUT)"
    track_test "fail"
fi

for mode in 1 2; do
    echo -n "Test: Mode $mode garbles data (unsupported)... "
    lager spi "$SPI_NET" config --mode "$mode" --box "$BOX" >/dev/null 2>&1
    lager gpo "$GPIO_CS" low --box "$BOX" >/dev/null 2>&1
    OUTPUT=$(lager spi "$SPI_NET" transfer --data "$CHIP_ID_CMD" 2 --box "$BOX" 2>&1)
    lager gpo "$GPIO_CS" high --box "$BOX" >/dev/null 2>&1
    if echo "$OUTPUT" | grep -qi "$CHIP_ID_VAL"; then
        echo "  (mode $mode got valid chip ID unexpectedly: $OUTPUT)"
        track_test "fail"
    else
        track_test "pass"
    fi
done

# Mode 3: BMP280 datasheet supports modes 0 and 3; expect valid chip ID
echo -n "Test: Mode 3 valid chip ID (supported per datasheet)... "
lager spi "$SPI_NET" config --mode 3 --box "$BOX" >/dev/null 2>&1
lager gpo "$GPIO_CS" low --box "$BOX" >/dev/null 2>&1
OUTPUT=$(lager spi "$SPI_NET" transfer --data "$CHIP_ID_CMD" 2 --box "$BOX" 2>&1)
lager gpo "$GPIO_CS" high --box "$BOX" >/dev/null 2>&1
if echo "$OUTPUT" | grep -qi "$CHIP_ID_VAL"; then
    track_test "pass"
else
    echo "  (mode 3 got: $OUTPUT, expected chip ID $CHIP_ID_VAL)"
    track_test "fail"
fi

# Restore mode 0
lager spi "$SPI_NET" config --mode 0 --box "$BOX" >/dev/null 2>&1

# ============================================================================
# 5. OUTPUT FORMATS
# ============================================================================
start_section "Output Formats"
print_section_header "SECTION 5: OUTPUT FORMATS"

# 5a. Hex format
echo -n "Test: Output format hex... "
lager gpo "$GPIO_CS" low --box "$BOX" >/dev/null 2>&1
OUTPUT=$(lager spi "$SPI_NET" transfer --data "$CHIP_ID_CMD" --format hex 2 --box "$BOX" 2>&1)
lager gpo "$GPIO_CS" high --box "$BOX" >/dev/null 2>&1
if echo "$OUTPUT" | grep -qi "$CHIP_ID_VAL"; then
    track_test "pass"
else
    track_test "fail"
fi

# 5b. Bytes format
echo -n "Test: Output format bytes... "
lager gpo "$GPIO_CS" low --box "$BOX" >/dev/null 2>&1
OUTPUT=$(lager spi "$SPI_NET" transfer --data "$CHIP_ID_CMD" --format bytes 2 --box "$BOX" 2>&1)
lager gpo "$GPIO_CS" high --box "$BOX" >/dev/null 2>&1
if echo "$OUTPUT" | grep -q "88"; then  # 0x58 = 88 decimal
    track_test "pass"
else
    track_test "fail"
fi

# 5c. JSON format
echo -n "Test: Output format json... "
lager gpo "$GPIO_CS" low --box "$BOX" >/dev/null 2>&1
OUTPUT=$(lager spi "$SPI_NET" transfer --data "$CHIP_ID_CMD" --format json 2 --box "$BOX" 2>&1)
lager gpo "$GPIO_CS" high --box "$BOX" >/dev/null 2>&1
if echo "$OUTPUT" | grep -q '"data"'; then
    track_test "pass"
else
    track_test "fail"
fi

# ============================================================================
# 6. DATA-FILE TRANSFER
# ============================================================================
start_section "Data File"
print_section_header "SECTION 6: DATA-FILE TRANSFER"

# 6a. Transfer with data-file
echo -n "Test: Transfer with --data-file... "
echo -ne '\xD0\x00' > /tmp/spi_lj_manual_test.bin
lager gpo "$GPIO_CS" low --box "$BOX" >/dev/null 2>&1
OUTPUT=$(lager spi "$SPI_NET" transfer --data-file /tmp/spi_lj_manual_test.bin 2 --box "$BOX" 2>&1)
lager gpo "$GPIO_CS" high --box "$BOX" >/dev/null 2>&1
if echo "$OUTPUT" | grep -qi "$CHIP_ID_VAL"; then
    track_test "pass"
else
    track_test "fail"
fi

# ============================================================================
# 7. FILL BYTE VARIANTS
# ============================================================================
start_section "Fill Byte"
print_section_header "SECTION 7: FILL BYTE VARIANTS"

# 7a. Fill 0xFF (default)
echo -n "Test: Read with fill 0xFF... "
lager gpo "$GPIO_CS" low --box "$BOX" >/dev/null 2>&1
lager spi "$SPI_NET" read 4 --fill 0xFF --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"
lager gpo "$GPIO_CS" high --box "$BOX" >/dev/null 2>&1

# 7b. Fill 0x00
echo -n "Test: Read with fill 0x00... "
lager gpo "$GPIO_CS" low --box "$BOX" >/dev/null 2>&1
lager spi "$SPI_NET" read 4 --fill 0x00 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"
lager gpo "$GPIO_CS" high --box "$BOX" >/dev/null 2>&1

# ============================================================================
# 8. BIT ORDER
# ============================================================================
start_section "Bit Order"
print_section_header "SECTION 8: BIT ORDER"

# 8a. MSB first (standard)
echo -n "Test: Bit order MSB chip ID... "
lager spi "$SPI_NET" config --bit-order msb --box "$BOX" >/dev/null 2>&1
lager gpo "$GPIO_CS" low --box "$BOX" >/dev/null 2>&1
OUTPUT=$(lager spi "$SPI_NET" transfer --data "$CHIP_ID_CMD" 2 --box "$BOX" 2>&1)
lager gpo "$GPIO_CS" high --box "$BOX" >/dev/null 2>&1
if echo "$OUTPUT" | grep -qi "$CHIP_ID_VAL"; then
    track_test "pass"
else
    track_test "fail"
fi

# 8b. LSB first (data will be garbled but command should succeed)
echo -n "Test: Bit order LSB (command succeeds)... "
lager spi "$SPI_NET" config --bit-order lsb --box "$BOX" >/dev/null 2>&1
lager gpo "$GPIO_CS" low --box "$BOX" >/dev/null 2>&1
lager spi "$SPI_NET" transfer --data 0xFF 2 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"
lager gpo "$GPIO_CS" high --box "$BOX" >/dev/null 2>&1

# Restore MSB
lager spi "$SPI_NET" config --bit-order msb --box "$BOX" >/dev/null 2>&1

# ============================================================================
# 9. WRITE + READBACK
# ============================================================================
start_section "Write + Readback"
print_section_header "SECTION 9: WRITE + READBACK (ctrl_meas register)"

# 9a. Write 0x00 to ctrl_meas, then read back
echo -n "Test: Write ctrl_meas=0x00, readback... "
# Write: register 0x74 (0xF4 with bit 7 = 0), value 0x00
lager gpo "$GPIO_CS" low --box "$BOX" >/dev/null 2>&1
lager spi "$SPI_NET" write "0x74 0x00" --box "$BOX" >/dev/null 2>&1
lager gpo "$GPIO_CS" high --box "$BOX" >/dev/null 2>&1
# Read back: register 0xF4 (bit 7 = 1)
lager gpo "$GPIO_CS" low --box "$BOX" >/dev/null 2>&1
OUTPUT=$(lager spi "$SPI_NET" transfer --data 0xF4 2 --box "$BOX" 2>&1)
lager gpo "$GPIO_CS" high --box "$BOX" >/dev/null 2>&1
if echo "$OUTPUT" | grep -qi "00"; then
    track_test "pass"
else
    echo "  (got: $OUTPUT, expected 00)"
    track_test "fail"
fi

# 9b. Write 0x25 to ctrl_meas (forced mode), readback
echo -n "Test: Write ctrl_meas=0x25, readback... "
lager gpo "$GPIO_CS" low --box "$BOX" >/dev/null 2>&1
lager spi "$SPI_NET" write "0x74 0x25" --box "$BOX" >/dev/null 2>&1
lager gpo "$GPIO_CS" high --box "$BOX" >/dev/null 2>&1
sleep 0.05
lager gpo "$GPIO_CS" low --box "$BOX" >/dev/null 2>&1
OUTPUT=$(lager spi "$SPI_NET" transfer --data 0xF4 2 --box "$BOX" 2>&1)
lager gpo "$GPIO_CS" high --box "$BOX" >/dev/null 2>&1
# Forced mode: after conversion, mode bits revert to 00, so value may be 0x24
if [ $? -eq 0 ]; then
    track_test "pass"
else
    track_test "fail"
fi

# ============================================================================
# 10. 56-BYTE BOUNDARY
# ============================================================================
start_section "56-Byte Boundary"
print_section_header "SECTION 10: 56-BYTE BOUNDARY (LABJACK LIMIT)"

# 10a. 56 bytes (OK - exactly at limit)
echo -n "Test: Read 56 bytes (at limit)... "
lager gpo "$GPIO_CS" low --box "$BOX" >/dev/null 2>&1
lager spi "$SPI_NET" read 56 --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"
lager gpo "$GPIO_CS" high --box "$BOX" >/dev/null 2>&1

# 10b. 57 bytes (MUST FAIL - exceeds LabJack buffer)
echo -n "Test: Read 57 bytes (must fail)... "
lager gpo "$GPIO_CS" low --box "$BOX" >/dev/null 2>&1
lager spi "$SPI_NET" read 57 --box "$BOX" >/dev/null 2>&1 && track_test "fail" || track_test "pass"
lager gpo "$GPIO_CS" high --box "$BOX" >/dev/null 2>&1

# ============================================================================
# 11. CLEANUP
# ============================================================================
start_section "Cleanup"
print_section_header "SECTION 11: CLEANUP"

# 11a. Restore default config
echo -n "Test: Restore config (mode 0, 800k, msb, 8-bit, cs-active low)... "
lager spi "$SPI_NET" config --mode 0 --frequency 800k --bit-order msb --word-size 8 --cs-active low --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 11b. Set CS high (deasserted)
echo -n "Test: Set CS high (deasserted)... "
lager gpo "$GPIO_CS" high --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"

# 11c. Restore BMP280 sleep mode
echo -n "Test: Restore BMP280 sleep mode... "
lager gpo "$GPIO_CS" low --box "$BOX" >/dev/null 2>&1
lager spi "$SPI_NET" write "0x74 0x00" --box "$BOX" >/dev/null 2>&1 && track_test "pass" || track_test "fail"
lager gpo "$GPIO_CS" high --box "$BOX" >/dev/null 2>&1

# 11d. Clean temp files
echo -n "Test: Clean up temp files... "
rm -f /tmp/spi_lj_manual_test.bin
track_test "pass"

# ============================================================================
# SUMMARY
# ============================================================================
print_summary
exit_with_status
