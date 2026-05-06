#!/usr/bin/env bash

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
# ============================================================================
# I2C CLI Edge Case Test Commands
# ============================================================================
# Usage: Review and run commands individually or source this file.
#
# Prerequisites:
#   - BMP280 connected to Aardvark on your box at address 0x76
#   - BMP280 powered: lager dac dac1 3.3 --box <YOUR-BOX>
#   - Net 'i2c1' configured (Aardvark I2C)
#
# Conventions:
#   [EXPECT: OK]     = should succeed
#   [EXPECT: ERROR]  = should produce an error message
#   [EXPECT: EMPTY]  = should work but return no data / no devices
#   [OBSERVE]        = check the output manually
# ============================================================================

BOX="${1:?Usage: $0 <BOX> [NET]}"
NET="${2:-i2c1}"
ADDR="0x76"  # BMP280 address

echo "============================================"
echo "I2C CLI Edge Case Tests"
echo "Box: $BOX  Net: $NET  Device: $ADDR"
echo "============================================"

# --------------------------------------------------------------------------
# 0. PREREQUISITES
# --------------------------------------------------------------------------
echo ""
echo "=== 0. Prerequisites ==="

# Power the BMP280
echo "--- 0a. Power BMP280 via DAC ---"
lager dac dac1 3.3 --box "$BOX"
# [EXPECT: OK]

# --------------------------------------------------------------------------
# 1. CONFIG COMMAND EDGE CASES
# --------------------------------------------------------------------------
echo ""
echo "=== 1. Config Command ==="

# 1a. Standard 100kHz with pull-ups
echo "--- 1a. Standard config (100k, pull-ups on) ---"
lager i2c "$NET" config --frequency 100k --pull-ups on --box "$BOX"
# [EXPECT: OK] "I2C configured: freq=100000Hz, pull_ups=on"

# 1b. 400kHz fast mode
echo "--- 1b. 400kHz config ---"
lager i2c "$NET" config --frequency 400k --box "$BOX"
# [EXPECT: OK] freq=400000Hz

# 1c. Frequency with Hz suffix
echo "--- 1c. Frequency with Hz suffix ---"
lager i2c "$NET" config --frequency 100000hz --box "$BOX"
# [EXPECT: OK] freq=100000Hz

# 1d. Frequency with M suffix (1MHz)
echo "--- 1d. 1MHz config ---"
lager i2c "$NET" config --frequency 1M --box "$BOX"
# [EXPECT: OK] Aardvark may cap at 800kHz

# 1e. Low frequency (10kHz)
echo "--- 1e. 10kHz config ---"
lager i2c "$NET" config --frequency 10k --box "$BOX"
# [EXPECT: OK]

# 1f. Pull-ups off
echo "--- 1f. Pull-ups off ---"
lager i2c "$NET" config --pull-ups off --box "$BOX"
# [EXPECT: OK] pull_ups=off

# 1g. Pull-ups on
echo "--- 1g. Pull-ups on ---"
lager i2c "$NET" config --pull-ups on --box "$BOX"
# [EXPECT: OK] pull_ups=on

# 1h. Config with only frequency (no pull-ups flag)
echo "--- 1h. Frequency only (no pull-ups) ---"
lager i2c "$NET" config --frequency 100k --box "$BOX"
# [EXPECT: OK] Should not mention pull_ups

# 1i. Config with only pull-ups (no frequency)
echo "--- 1i. Pull-ups only (no frequency) ---"
lager i2c "$NET" config --pull-ups on --box "$BOX"
# [EXPECT: OK]

# 1j. Config with no options at all
echo "--- 1j. Config with no options ---"
lager i2c "$NET" config --box "$BOX"
# [OBSERVE] May show current config or apply defaults

# 1k. Invalid frequency string
echo "--- 1k. Invalid frequency ---"
lager i2c "$NET" config --frequency abc --box "$BOX"
# [EXPECT: ERROR] Bad parameter

# 1l. Decimal frequency
echo "--- 1l. Decimal frequency (3.5k) ---"
lager i2c "$NET" config --frequency 3.5k --box "$BOX"
# [EXPECT: OK] freq=3500Hz

# 1m. Frequency as plain number
echo "--- 1m. Plain number frequency ---"
lager i2c "$NET" config --frequency 200000 --box "$BOX"
# [EXPECT: OK] freq=200000Hz

# Restore standard config
lager i2c "$NET" config --frequency 100k --pull-ups on --box "$BOX"

# --------------------------------------------------------------------------
# 2. SCAN COMMAND EDGE CASES
# --------------------------------------------------------------------------
echo ""
echo "=== 2. Scan Command ==="

# 2a. Default scan (0x08-0x77)
echo "--- 2a. Default scan ---"
lager i2c "$NET" scan --box "$BOX"
# [EXPECT: OK] i2cdetect grid showing 0x76

# 2b. Narrow range (just BMP280)
echo "--- 2b. Narrow range scan ---"
lager i2c "$NET" scan --start 0x76 --end 0x76 --box "$BOX"
# [EXPECT: OK] Shows only 0x76 row

# 2c. Range excluding BMP280
echo "--- 2c. Range excluding device ---"
lager i2c "$NET" scan --start 0x08 --end 0x75 --box "$BOX"
# [EXPECT: EMPTY] "Found 0 device(s)"

# 2d. Full range (0x00-0x7F)
echo "--- 2d. Full range scan ---"
lager i2c "$NET" scan --start 0x00 --end 0x7F --box "$BOX"
# [EXPECT: OK] Includes reserved addresses

# 2e. Reserved low addresses only
echo "--- 2e. Reserved low range ---"
lager i2c "$NET" scan --start 0x00 --end 0x07 --box "$BOX"
# [EXPECT: EMPTY or shows reserved devices]

# 2f. Reserved high addresses only
echo "--- 2f. Reserved high range ---"
lager i2c "$NET" scan --start 0x78 --end 0x7F --box "$BOX"
# [EXPECT: EMPTY or shows reserved devices]

# 2g. Single address scan - empty
echo "--- 2g. Single address (empty) ---"
lager i2c "$NET" scan --start 0x10 --end 0x10 --box "$BOX"
# [EXPECT: EMPTY]

# 2h. Reversed range (start > end)
echo "--- 2h. Reversed range ---"
lager i2c "$NET" scan --start 0x77 --end 0x08 --box "$BOX"
# [OBSERVE] Should return empty or handle gracefully

# 2i. Start address without 0x prefix
echo "--- 2i. Address without 0x prefix ---"
lager i2c "$NET" scan --start 08 --end 77 --box "$BOX"
# [OBSERVE] Should it work? Depends on parsing

# 2j. Decimal addresses
echo "--- 2j. Decimal addresses ---"
lager i2c "$NET" scan --start 8 --end 119 --box "$BOX"
# [OBSERVE] Decimal support

# 2k. Invalid start address
echo "--- 2k. Invalid start address ---"
lager i2c "$NET" scan --start 0xZZ --box "$BOX"
# [EXPECT: ERROR]

# 2l. Address above 0x7F
echo "--- 2l. Start address > 0x7F ---"
lager i2c "$NET" scan --start 0x80 --box "$BOX"
# [EXPECT: ERROR] Out of 7-bit range

# --------------------------------------------------------------------------
# 3. READ COMMAND EDGE CASES
# --------------------------------------------------------------------------
echo ""
echo "=== 3. Read Command ==="

# 3a. Read 1 byte
echo "--- 3a. Read 1 byte ---"
lager i2c "$NET" read 1 --address 0x76 --box "$BOX"
# [EXPECT: OK] Single hex byte

# 3b. Read multiple bytes
echo "--- 3b. Read 8 bytes ---"
lager i2c "$NET" read 8 --address 0x76 --box "$BOX"
# [EXPECT: OK] 8 hex bytes

# 3c. Read with hex format (explicit)
echo "--- 3c. Read hex format ---"
lager i2c "$NET" read 4 --address 0x76 --format hex --box "$BOX"
# [EXPECT: OK] Space-separated hex

# 3d. Read with bytes format
echo "--- 3d. Read bytes format ---"
lager i2c "$NET" read 4 --address 0x76 --format bytes --box "$BOX"
# [EXPECT: OK] Space-separated decimal

# 3e. Read with json format
echo "--- 3e. Read json format ---"
lager i2c "$NET" read 4 --address 0x76 --format json --box "$BOX"
# [EXPECT: OK] {"data": [...]}

# 3f. Read with frequency override
echo "--- 3f. Read with frequency override ---"
lager i2c "$NET" read 1 --address 0x76 --frequency 400k --box "$BOX"
# [EXPECT: OK]

# 3g. Read from non-existent device
echo "--- 3g. Read from empty address ---"
lager i2c "$NET" read 1 --address 0x10 --box "$BOX"
# [EXPECT: ERROR] no ACK received (device not responding)

# 3h. Read 0 bytes
echo "--- 3h. Read 0 bytes ---"
lager i2c "$NET" read 0 --address 0x76 --box "$BOX"
# [OBSERVE] Edge case - may error or return empty

# 3i. Read large number of bytes
echo "--- 3i. Read 256 bytes ---"
lager i2c "$NET" read 256 --address 0x76 --box "$BOX"
# [EXPECT: OK] 256 hex bytes

# 3j. Missing --address (required)
echo "--- 3j. Missing --address ---"
lager i2c "$NET" read 1 --box "$BOX"
# [EXPECT: ERROR] "Missing option '--address'"

# 3k. Missing NUM_BYTES argument
echo "--- 3k. Missing NUM_BYTES ---"
lager i2c "$NET" read --address 0x76 --box "$BOX"
# [EXPECT: ERROR] Missing argument

# 3l. Address in decimal
echo "--- 3l. Address in decimal ---"
lager i2c "$NET" read 1 --address 118 --box "$BOX"
# [OBSERVE] 118 = 0x76, should work same as hex

# 3m. Address 0x00 (general call)
echo "--- 3m. Address 0x00 ---"
lager i2c "$NET" read 1 --address 0x00 --box "$BOX"
# [OBSERVE] General call address behavior

# 3n. Address 0x7F (max)
echo "--- 3n. Address 0x7F ---"
lager i2c "$NET" read 1 --address 0x7F --box "$BOX"
# [OBSERVE]

# 3o. Address 0x80 (out of range)
echo "--- 3o. Address 0x80 (invalid) ---"
lager i2c "$NET" read 1 --address 0x80 --box "$BOX"
# [EXPECT: ERROR] Out of 7-bit range

# 3p. Address 0xFF (out of range)
echo "--- 3p. Address 0xFF (invalid) ---"
lager i2c "$NET" read 1 --address 0xFF --box "$BOX"
# [EXPECT: ERROR]

# 3q. Negative num_bytes
echo "--- 3q. Negative num_bytes ---"
lager i2c "$NET" read -1 --address 0x76 --box "$BOX"
# [EXPECT: ERROR]

# 3r. Non-numeric num_bytes
echo "--- 3r. Non-numeric num_bytes ---"
lager i2c "$NET" read abc --address 0x76 --box "$BOX"
# [EXPECT: ERROR]

# --------------------------------------------------------------------------
# 4. WRITE COMMAND EDGE CASES
# --------------------------------------------------------------------------
echo ""
echo "=== 4. Write Command ==="

# 4a. Write single byte (register pointer)
echo "--- 4a. Write single byte ---"
lager i2c "$NET" write 0xD0 --address 0x76 --box "$BOX"
# [EXPECT: OK] "Wrote 1 byte(s) to 0x76"

# 4b. Write two bytes (register + value)
echo "--- 4b. Write two bytes ---"
lager i2c "$NET" write 0xF400 --address 0x76 --box "$BOX"
# [EXPECT: OK] "Wrote 2 byte(s) to 0x76"

# 4c. Write with spaces separator
echo "--- 4c. Write with spaces ---"
lager i2c "$NET" write "F4 00" --address 0x76 --box "$BOX"
# [EXPECT: OK]

# 4d. Write with comma separator
echo "--- 4d. Write with commas ---"
lager i2c "$NET" write "F4,00" --address 0x76 --box "$BOX"
# [EXPECT: OK]

# 4e. Write with colon separator
echo "--- 4e. Write with colons ---"
lager i2c "$NET" write "F4:00" --address 0x76 --box "$BOX"
# [EXPECT: OK]

# 4f. Write with 0x prefix on each byte
echo "--- 4f. Write with 0x prefix per byte ---"
lager i2c "$NET" write "0xF4 0x00" --address 0x76 --box "$BOX"
# [EXPECT: OK]

# 4g. Write with data-file
echo "--- 4g. Write with data-file ---"
echo -ne '\xF4\x00' > /tmp/i2c_test_data.bin
lager i2c "$NET" write --data-file /tmp/i2c_test_data.bin --address 0x76 --box "$BOX"
# [EXPECT: OK]

# 4h. Write to non-existent device
echo "--- 4h. Write to empty address ---"
lager i2c "$NET" write 0x00 --address 0x10 --box "$BOX"
# [OBSERVE] May report NACK or succeed silently

# 4i. Write empty data (no DATA argument)
echo "--- 4i. Write with no data ---"
lager i2c "$NET" write --address 0x76 --box "$BOX"
# [OBSERVE] May error or send 0-byte write

# 4j. Write with hex format output
echo "--- 4j. Write with --format hex ---"
lager i2c "$NET" write 0xD0 --address 0x76 --format hex --box "$BOX"
# [EXPECT: OK]

# 4k. Write with json format output
echo "--- 4k. Write with --format json ---"
lager i2c "$NET" write 0xD0 --address 0x76 --format json --box "$BOX"
# [EXPECT: OK]

# 4l. Write with frequency override
echo "--- 4l. Write with frequency override ---"
lager i2c "$NET" write 0xD0 --address 0x76 --frequency 400k --box "$BOX"
# [EXPECT: OK]

# 4m. Missing --address
echo "--- 4m. Missing --address ---"
lager i2c "$NET" write 0xD0 --box "$BOX"
# [EXPECT: ERROR]

# 4n. Invalid hex data
echo "--- 4n. Invalid hex data ---"
lager i2c "$NET" write 0xGG --address 0x76 --box "$BOX"
# [EXPECT: ERROR]

# 4o. Odd-length hex string (e.g., 3 chars)
echo "--- 4o. Odd-length hex string ---"
lager i2c "$NET" write 0xD0F --address 0x76 --box "$BOX"
# [OBSERVE] Should pad or error? Implementation pads leading zero -> 0D 0F

# 4p. Data-file that doesn't exist
echo "--- 4p. Non-existent data-file ---"
lager i2c "$NET" write --data-file /tmp/nonexistent_file.bin --address 0x76 --box "$BOX"
# [EXPECT: ERROR]

# 4q. Both DATA and --data-file
echo "--- 4q. Both DATA and --data-file ---"
echo -ne '\xD0' > /tmp/i2c_test_data.bin
lager i2c "$NET" write 0xD0 --data-file /tmp/i2c_test_data.bin --address 0x76 --box "$BOX"
# [OBSERVE] Which takes precedence?

# 4r. Write BMP280 soft reset (0xE0 = 0xB6)
echo "--- 4r. BMP280 soft reset ---"
lager i2c "$NET" write 0xE0B6 --address 0x76 --box "$BOX"
# [EXPECT: OK] Device resets

# 4s. Write all 0xFF
echo "--- 4s. Write 0xFF ---"
lager i2c "$NET" write 0xF4FF --address 0x76 --box "$BOX"
# [EXPECT: OK]

# 4t. Write all 0x00
echo "--- 4t. Write 0x00 ---"
lager i2c "$NET" write 0xF400 --address 0x76 --box "$BOX"
# [EXPECT: OK]

# --------------------------------------------------------------------------
# 5. TRANSFER (WRITE_READ) COMMAND EDGE CASES
# --------------------------------------------------------------------------
echo ""
echo "=== 5. Transfer Command ==="

# 5a. Read chip ID (standard use case)
echo "--- 5a. Read chip ID ---"
lager i2c "$NET" transfer 1 --address 0x76 --data 0xD0 --box "$BOX"
# [EXPECT: OK] "58"

# 5b. Read calibration data (26 bytes)
echo "--- 5b. Read calibration data ---"
lager i2c "$NET" transfer 26 --address 0x76 --data 0x88 --box "$BOX"
# [EXPECT: OK] 26 hex bytes, mostly non-zero

# 5c. Read status register
echo "--- 5c. Read status register ---"
lager i2c "$NET" transfer 1 --address 0x76 --data 0xF3 --box "$BOX"
# [EXPECT: OK]

# 5d. Read ctrl_meas register
echo "--- 5d. Read ctrl_meas register ---"
lager i2c "$NET" transfer 1 --address 0x76 --data 0xF4 --box "$BOX"
# [EXPECT: OK] Default 0x00

# 5e. Read config register
echo "--- 5e. Read config register ---"
lager i2c "$NET" transfer 1 --address 0x76 --data 0xF5 --box "$BOX"
# [EXPECT: OK] Default 0x00

# 5f. Hex output format
echo "--- 5f. Transfer hex format ---"
lager i2c "$NET" transfer 1 --address 0x76 --data 0xD0 --format hex --box "$BOX"
# [EXPECT: OK] "58"

# 5g. Bytes output format
echo "--- 5g. Transfer bytes format ---"
lager i2c "$NET" transfer 1 --address 0x76 --data 0xD0 --format bytes --box "$BOX"
# [EXPECT: OK] "88"

# 5h. JSON output format
echo "--- 5h. Transfer json format ---"
lager i2c "$NET" transfer 1 --address 0x76 --data 0xD0 --format json --box "$BOX"
# [EXPECT: OK] {"data": [88]}

# 5i. Transfer with frequency override
echo "--- 5i. Transfer with frequency override ---"
lager i2c "$NET" transfer 1 --address 0x76 --data 0xD0 --frequency 400k --box "$BOX"
# [EXPECT: OK] "58"

# 5j. Transfer to non-existent device
echo "--- 5j. Transfer to empty address ---"
lager i2c "$NET" transfer 1 --address 0x10 --data 0x00 --box "$BOX"
# [EXPECT: ERROR] no ACK received (device not responding)

# 5k. Transfer with no --data (just read)
echo "--- 5k. Transfer without --data ---"
lager i2c "$NET" transfer 1 --address 0x76 --box "$BOX"
# [OBSERVE] Should this work? data defaults to None -> [] in dispatcher

# 5l. Transfer 0 bytes read
echo "--- 5l. Transfer 0 read bytes ---"
lager i2c "$NET" transfer 0 --address 0x76 --data 0xD0 --box "$BOX"
# [OBSERVE] Edge case

# 5m. Transfer large read (128 bytes)
echo "--- 5m. Transfer 128 bytes ---"
lager i2c "$NET" transfer 128 --address 0x76 --data 0x80 --box "$BOX"
# [EXPECT: OK] 128 hex bytes

# 5n. Transfer with multi-byte write data
echo "--- 5n. Transfer with 2-byte write ---"
lager i2c "$NET" transfer 1 --address 0x76 --data 0xF400 --box "$BOX"
# [OBSERVE] Writes 0xF4 0x00 then reads 1 byte

# 5o. Transfer with data-file
echo "--- 5o. Transfer with data-file ---"
echo -ne '\xD0' > /tmp/i2c_test_reg.bin
lager i2c "$NET" transfer 1 --address 0x76 --data-file /tmp/i2c_test_reg.bin --box "$BOX"
# [EXPECT: OK] "58"

# 5p. Transfer with both --data and --data-file
echo "--- 5p. Both --data and --data-file ---"
echo -ne '\xD0' > /tmp/i2c_test_reg.bin
lager i2c "$NET" transfer 1 --address 0x76 --data 0xF3 --data-file /tmp/i2c_test_reg.bin --box "$BOX"
# [OBSERVE] Which takes precedence?

# 5q. Missing --address
echo "--- 5q. Missing --address ---"
lager i2c "$NET" transfer 1 --data 0xD0 --box "$BOX"
# [EXPECT: ERROR]

# 5r. Missing NUM_BYTES
echo "--- 5r. Missing NUM_BYTES ---"
lager i2c "$NET" transfer --address 0x76 --data 0xD0 --box "$BOX"
# [EXPECT: ERROR]

# 5s. Data with space separators
echo "--- 5s. Data with spaces ---"
lager i2c "$NET" transfer 1 --address 0x76 --data "D0" --box "$BOX"
# [EXPECT: OK]

# 5t. Data with various hex formats
echo "--- 5t. Data without 0x prefix ---"
lager i2c "$NET" transfer 1 --address 0x76 --data D0 --box "$BOX"
# [EXPECT: OK]

# 5u. Transfer 256 bytes
echo "--- 5u. Transfer 256 bytes ---"
lager i2c "$NET" transfer 256 --address 0x76 --data 0x80 --box "$BOX"
# [EXPECT: OK]

# 5v. Non-existent data-file
echo "--- 5v. Non-existent data-file ---"
lager i2c "$NET" transfer 1 --address 0x76 --data-file /tmp/nope.bin --box "$BOX"
# [EXPECT: ERROR]

# --------------------------------------------------------------------------
# 6. NET NAME AND BOX EDGE CASES
# --------------------------------------------------------------------------
echo ""
echo "=== 6. Net Name and Box Edge Cases ==="

# 6a. Invalid net name
echo "--- 6a. Invalid net name ---"
lager i2c nonexistent scan --box "$BOX"
# [EXPECT: ERROR]

# 6b. No net name (should list I2C nets)
echo "--- 6b. No net name (list nets) ---"
lager i2c --box "$BOX"
# [EXPECT: OK] Table of I2C nets

# 6c. Box flag before net name
echo "--- 6c. --box before net name ---"
lager i2c --box "$BOX" "$NET" scan
# [EXPECT: OK] Should work via I2CGroup reordering

# 6d. Box flag after subcommand
echo "--- 6d. --box after subcommand ---"
lager i2c "$NET" scan --box "$BOX"
# [EXPECT: OK]

# 6e. Invalid box name
echo "--- 6e. Invalid box name ---"
lager i2c "$NET" scan --box nonexistent-box-12345
# [EXPECT: ERROR]

# 6f. No --box flag (uses default if set)
echo "--- 6f. No --box flag ---"
lager i2c "$NET" scan
# [OBSERVE] Uses default box if configured, otherwise error

# 6g. Help output
echo "--- 6g. Help output ---"
lager i2c --help
# [EXPECT: OK] Shows usage

# 6h. Subcommand help
echo "--- 6h. Subcommand help ---"
lager i2c "$NET" config --help
lager i2c "$NET" scan --help
lager i2c "$NET" read --help
lager i2c "$NET" write --help
lager i2c "$NET" transfer --help
# [EXPECT: OK] Shows usage for each

# 6i. Invalid subcommand
echo "--- 6i. Invalid subcommand ---"
lager i2c "$NET" foobar --box "$BOX"
# [EXPECT: ERROR]

# --------------------------------------------------------------------------
# 7. ADDRESS FORMAT EDGE CASES
# --------------------------------------------------------------------------
echo ""
echo "=== 7. Address Format Edge Cases ==="

# 7a. Hex with 0x prefix (standard)
echo "--- 7a. 0x76 ---"
lager i2c "$NET" transfer 1 --address 0x76 --data 0xD0 --box "$BOX"
# [EXPECT: OK]

# 7b. Hex without 0x prefix
echo "--- 7b. 76 (no prefix) ---"
lager i2c "$NET" transfer 1 --address 76 --data 0xD0 --box "$BOX"
# [OBSERVE] Should parse as hex (76 = 0x76) if 2 chars

# 7c. Decimal address
echo "--- 7c. Decimal 118 ---"
lager i2c "$NET" transfer 1 --address 118 --data 0xD0 --box "$BOX"
# [OBSERVE] 118 decimal = 0x76

# 7d. Uppercase hex
echo "--- 7d. 0X76 uppercase ---"
lager i2c "$NET" transfer 1 --address 0X76 --data 0xD0 --box "$BOX"
# [OBSERVE]

# 7e. Address 0x00
echo "--- 7e. Address 0x00 ---"
lager i2c "$NET" transfer 1 --address 0x00 --data 0x00 --box "$BOX"
# [OBSERVE]

# 7f. Address 0x7F (max valid)
echo "--- 7f. Address 0x7F ---"
lager i2c "$NET" transfer 1 --address 0x7F --data 0x00 --box "$BOX"
# [OBSERVE]

# 7g. Address 0x80 (out of range)
echo "--- 7g. Address 0x80 (invalid) ---"
lager i2c "$NET" transfer 1 --address 0x80 --data 0x00 --box "$BOX"
# [EXPECT: ERROR]

# 7h. Address 0xFF (out of range)
echo "--- 7h. Address 0xFF (invalid) ---"
lager i2c "$NET" transfer 1 --address 0xFF --data 0x00 --box "$BOX"
# [EXPECT: ERROR]

# 7i. Negative address
echo "--- 7i. Negative address ---"
lager i2c "$NET" transfer 1 --address -1 --data 0x00 --box "$BOX"
# [EXPECT: ERROR]

# 7j. Non-numeric address
echo "--- 7j. Non-numeric address ---"
lager i2c "$NET" transfer 1 --address xyz --data 0x00 --box "$BOX"
# [EXPECT: ERROR]

# 7k. Empty address
echo "--- 7k. Empty address ---"
lager i2c "$NET" transfer 1 --address "" --data 0x00 --box "$BOX"
# [EXPECT: ERROR]

# --------------------------------------------------------------------------
# 8. DATA FORMAT EDGE CASES
# --------------------------------------------------------------------------
echo ""
echo "=== 8. Data Format Edge Cases ==="

# 8a. Single byte with 0x
echo "--- 8a. 0xD0 ---"
lager i2c "$NET" transfer 1 --address 0x76 --data 0xD0 --box "$BOX"
# [EXPECT: OK]

# 8b. Single byte without 0x
echo "--- 8b. D0 ---"
lager i2c "$NET" transfer 1 --address 0x76 --data D0 --box "$BOX"
# [EXPECT: OK]

# 8c. Multi-byte continuous
echo "--- 8c. 0xD0F3 ---"
lager i2c "$NET" transfer 1 --address 0x76 --data 0xD0F3 --box "$BOX"
# [EXPECT: OK] Writes 0xD0, 0xF3

# 8d. Multi-byte with spaces
echo "--- 8d. Space separated ---"
lager i2c "$NET" transfer 1 --address 0x76 --data "D0 F3" --box "$BOX"
# [EXPECT: OK]

# 8e. Multi-byte with commas
echo "--- 8e. Comma separated ---"
lager i2c "$NET" transfer 1 --address 0x76 --data "D0,F3" --box "$BOX"
# [EXPECT: OK]

# 8f. Multi-byte with colons
echo "--- 8f. Colon separated ---"
lager i2c "$NET" transfer 1 --address 0x76 --data "D0:F3" --box "$BOX"
# [EXPECT: OK]

# 8g. Multi-byte with hyphens
echo "--- 8g. Hyphen separated ---"
lager i2c "$NET" transfer 1 --address 0x76 --data "D0-F3" --box "$BOX"
# [EXPECT: OK]

# 8h. Lowercase hex
echo "--- 8h. Lowercase hex ---"
lager i2c "$NET" transfer 1 --address 0x76 --data 0xd0 --box "$BOX"
# [EXPECT: OK]

# 8i. Mixed case hex
echo "--- 8i. Mixed case ---"
lager i2c "$NET" transfer 1 --address 0x76 --data 0xD0f3 --box "$BOX"
# [EXPECT: OK]

# 8j. Odd-length hex string
echo "--- 8j. Odd-length hex (3 chars) ---"
lager i2c "$NET" transfer 1 --address 0x76 --data 0xD0F --box "$BOX"
# [OBSERVE] Should pad to 0x0D 0x0F or error?

# 8k. Single hex digit
echo "--- 8k. Single hex digit ---"
lager i2c "$NET" transfer 1 --address 0x76 --data 0xD --box "$BOX"
# [OBSERVE] Pads to 0x0D?

# 8l. All zeros
echo "--- 8l. Data 0x00 ---"
lager i2c "$NET" transfer 1 --address 0x76 --data 0x00 --box "$BOX"
# [EXPECT: OK]

# 8m. All ones
echo "--- 8m. Data 0xFF ---"
lager i2c "$NET" transfer 1 --address 0x76 --data 0xFF --box "$BOX"
# [EXPECT: OK]

# 8n. Invalid hex character
echo "--- 8n. Invalid hex ---"
lager i2c "$NET" transfer 1 --address 0x76 --data 0xGG --box "$BOX"
# [EXPECT: ERROR]

# 8o. Per-byte 0x prefix with spaces
echo "--- 8o. 0x prefix per byte ---"
lager i2c "$NET" transfer 1 --address 0x76 --data "0xD0 0xF3" --box "$BOX"
# [EXPECT: OK]

# 8p. Byte value > 0xFF in separated format
echo "--- 8p. Byte > 0xFF ---"
lager i2c "$NET" transfer 1 --address 0x76 --data "0x1FF" --box "$BOX"
# [EXPECT: ERROR or truncate]

# --------------------------------------------------------------------------
# 9. SEQUENCE TESTS (command combinations)
# --------------------------------------------------------------------------
echo ""
echo "=== 9. Sequence Tests ==="

# 9a. Config -> Scan -> Transfer
echo "--- 9a. Config, scan, transfer sequence ---"
lager i2c "$NET" config --frequency 100k --pull-ups on --box "$BOX"
lager i2c "$NET" scan --box "$BOX"
lager i2c "$NET" transfer 1 --address 0x76 --data 0xD0 --box "$BOX"
# [EXPECT: OK] All three work independently

# 9b. Write register, then read it back with separate transfer
echo "--- 9b. Write then transfer readback ---"
lager i2c "$NET" write 0xF400 --address 0x76 --box "$BOX"
lager i2c "$NET" transfer 1 --address 0x76 --data 0xF4 --box "$BOX"
# [EXPECT: OK] Should read 0x00 (sleep mode)

# 9c. Set forced mode, wait, read raw data
echo "--- 9c. Forced mode measurement ---"
lager i2c "$NET" write 0xF425 --address 0x76 --box "$BOX"
sleep 0.1
lager i2c "$NET" transfer 6 --address 0x76 --data 0xF7 --box "$BOX"
# [EXPECT: OK] 6 bytes of raw pressure+temperature

# 9d. Soft reset then verify chip ID
echo "--- 9d. Reset then chip ID ---"
lager i2c "$NET" write 0xE0B6 --address 0x76 --box "$BOX"
sleep 0.01
lager i2c "$NET" transfer 1 --address 0x76 --data 0xD0 --box "$BOX"
# [EXPECT: OK] "58"

# 9e. Multiple rapid transfers
echo "--- 9e. Rapid transfers ---"
for i in $(seq 1 10); do
    lager i2c "$NET" transfer 1 --address 0x76 --data 0xD0 --box "$BOX"
done
# [EXPECT: OK] All 10 should return "58"

# 9f. Different frequencies between commands
echo "--- 9f. Different frequencies ---"
lager i2c "$NET" transfer 1 --address 0x76 --data 0xD0 --frequency 100k --box "$BOX"
lager i2c "$NET" transfer 1 --address 0x76 --data 0xD0 --frequency 400k --box "$BOX"
lager i2c "$NET" transfer 1 --address 0x76 --data 0xD0 --frequency 10k --box "$BOX"
# [EXPECT: OK] All return "58"

# --------------------------------------------------------------------------
# 10. CONFIG PERSISTENCE ACROSS COMMANDS
# --------------------------------------------------------------------------
echo ""
echo "=== 10. Config Persistence ==="

# 10a. Set freq 400k
echo "--- 10a. Set freq 400k ---"
lager i2c "$NET" config --frequency 400k --box "$BOX"
# [EXPECT: OK] "freq=400000Hz"

# 10b. Pull-ups only, freq should stay 400k
echo "--- 10b. Pull-ups only, freq stays 400k ---"
lager i2c "$NET" config --pull-ups on --box "$BOX"
# [EXPECT: OK] "freq=400000Hz, pull_ups=on"

# 10c. Freq only, pull-ups should stay on
echo "--- 10c. Freq only, pull-ups stays on ---"
lager i2c "$NET" config --frequency 100k --box "$BOX"
# [EXPECT: OK] "freq=100000Hz, pull_ups=on"

# 10d. No options shows stored values
echo "--- 10d. No options shows stored ---"
lager i2c "$NET" config --box "$BOX"
# [EXPECT: OK] "freq=100000Hz, pull_ups=on"

# 10e. Scan works after persisted config
echo "--- 10e. Scan after persisted config ---"
lager i2c "$NET" scan --box "$BOX"
# [EXPECT: OK] BMP280 at 0x76

# 10f. Transfer works after persisted config
echo "--- 10f. Transfer after persisted config ---"
lager i2c "$NET" transfer 1 --address 0x76 --data 0xD0 --box "$BOX"
# [EXPECT: OK] "58"

# 10g. Pull-ups off persists through freq change
echo "--- 10g. Pull-ups off, then freq change ---"
lager i2c "$NET" config --pull-ups off --box "$BOX"
lager i2c "$NET" config --frequency 400k --box "$BOX"
# [EXPECT: OK] Second output has "pull_ups=off"

# 10h. Output always has both fields
echo "--- 10h. Output has both freq= and pull_ups= ---"
lager i2c "$NET" config --frequency 100k --box "$BOX"
# [EXPECT: OK] Contains both "freq=" and "pull_ups="

# 10i. Rapid toggling preserves independence
echo "--- 10i. Rapid toggle sequence ---"
lager i2c "$NET" config --frequency 400k --box "$BOX"
lager i2c "$NET" config --pull-ups on --box "$BOX"
lager i2c "$NET" config --frequency 100k --box "$BOX"
lager i2c "$NET" config --pull-ups off --box "$BOX"
lager i2c "$NET" config --pull-ups on --box "$BOX"
# [EXPECT: OK] Final: "freq=100000Hz, pull_ups=on"

# 10j. Restore clean state
echo "--- 10j. Restore ---"
lager i2c "$NET" config --frequency 100k --pull-ups on --box "$BOX"
# [EXPECT: OK]

# --------------------------------------------------------------------------
# 11. CLEANUP
# --------------------------------------------------------------------------
echo ""
echo "=== 11. Cleanup ==="

# Restore BMP280 to sleep mode
lager i2c "$NET" write 0xF400 --address 0x76 --box "$BOX"
# Restore config
lager i2c "$NET" config --frequency 100k --pull-ups on --box "$BOX"
# Clean up temp files
rm -f /tmp/i2c_test_data.bin /tmp/i2c_test_reg.bin

echo ""
echo "=== Test commands complete ==="
echo "Review output above for PASS/FAIL/OBSERVE results."
