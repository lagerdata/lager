"""
Hardware verification for the LabJack U3 SPI and I2C drivers.

SPI is checked by LOOPBACK: jumper MOSI to MISO and every byte written must
come back. That needs no target device and proves clocking, framing, byte order
and the odd-length trim in one run. It does NOT prove mode (CPOL/CPHA) --
loopback returns the same bytes in all four modes -- so mode selection stays
unit-tested only.

I2C needs a real target plus external pull-ups; the U3 has none. Pass the
target address to check a read against it. With no address the run does a scan
only, which proves the bus is clocked but is weak evidence: with no pull-ups
fitted every address NAKs and an empty scan is indistinguishable from an empty
bus.

Wiring, using the advertised default spans:

    SPI net on FIO4-FIO7    CS=FIO4  CLK=FIO5  MISO=FIO6  MOSI=FIO7
                            jumper FIO7 to FIO6 for loopback
    I2C net on FIO6-FIO7    SDA=FIO6  SCL=FIO7
                            4.7k from each to VS, plus the target

The two spans overlap, so re-jumper between the SPI and I2C sections rather
than expecting both to pass in one pass with one wiring.

Run via:
    lager python test/api/communication/test_spi_i2c_labjack_u3.py \
        --box <BOX> -- --spi-net u3spi --i2c-net u3i2c [--i2c-addr 0x48]
"""

import sys

from lager import Net, NetType

SPI_PATTERNS = [
    [0x00],                                # 1 byte: odd, the trim case
    [0xFF],
    [0xA5, 0x5A],                          # 2 bytes: even
    [0x01, 0x02, 0x03],                    # 3 bytes: odd
    [0xDE, 0xAD, 0xBE, 0xEF],
    [0x00, 0xFF, 0x55, 0xAA, 0x0F],        # 5 bytes: odd
    list(range(50)),                       # the maximum transfer
]


def _arg(name, default=None):
    if name in sys.argv:
        return sys.argv[sys.argv.index(name) + 1]
    return default


def check_spi(netname):
    print(f"=== SPI loopback on '{netname}' (jumper MOSI to MISO) ===\n")
    spi = Net.get(netname, type=NetType.SPI)
    spi.config(mode=0, bit_order="msb", word_size=8)
    failures = 0

    for pattern in SPI_PATTERNS:
        label = f"{len(pattern)} byte(s)"
        got = spi.transfer(pattern)
        if list(got) == list(pattern):
            print(f"  PASS: {label} echoed intact")
        else:
            failures += 1
            print(f"  FAIL: {label}")
            print(f"        sent {pattern}")
            print(f"        got  {list(got)}")
            if len(got) != len(pattern):
                print(f"        LENGTH DIFFERS: sent {len(pattern)}, "
                      f"got {len(got)} -- the odd-packet trim is wrong")

    # Word sizes above 8 bits are split in software; loopback proves the split
    # and the reassembly agree.
    for word_size, words in ((16, [0x1234, 0xABCD]), (32, [0xDEADBEEF])):
        spi.config(word_size=word_size)
        got = spi.transfer(words)
        if list(got) == words:
            print(f"  PASS: {word_size}-bit words echoed intact")
        else:
            failures += 1
            print(f"  FAIL: {word_size}-bit words: sent {words}, got {list(got)}")
    spi.config(word_size=8)

    # LSB-first is a software reversal on both sides, so a loopback still
    # returns the original words.
    spi.config(bit_order="lsb")
    got = spi.transfer([0x01, 0x80])
    if list(got) == [0x01, 0x80]:
        print("  PASS: lsb-first echoed intact")
    else:
        failures += 1
        print(f"  FAIL: lsb-first: got {list(got)}")
    spi.config(bit_order="msb")

    print(f"\n  SPI: {failures} failure(s)\n")
    return failures


def check_i2c(netname, address):
    print(f"=== I2C on '{netname}' ===\n")
    i2c = Net.get(netname, type=NetType.I2C)
    i2c.config(frequency_hz=100_000)
    failures = 0

    found = i2c.scan()
    print(f"  scan found: {[hex(a) for a in found]}")
    if not found:
        print("  WEAK: nothing acknowledged. With no pull-ups fitted this is "
              "what an empty bus looks like, so it proves only that the scan "
              "completed without erroring.")

    if address is None:
        print("  SKIP: no --i2c-addr given, so no read was attempted.\n")
        return failures

    if address in found:
        print(f"  PASS: scan saw the target at {hex(address)}")
    else:
        failures += 1
        print(f"  FAIL: scan did not see the target at {hex(address)}")

    try:
        data = i2c.read(address=address, num_bytes=1)
        print(f"  PASS: read 1 byte from {hex(address)}: {[hex(b) for b in data]}")
    except Exception as exc:
        failures += 1
        print(f"  FAIL: read from {hex(address)} raised: {exc}")

    # An odd read is the case where the library trims its own response; a byte
    # count that comes back short would mean the driver trimmed it twice.
    for n in (1, 3, 5):
        try:
            data = i2c.read(address=address, num_bytes=n)
            if len(data) == n:
                print(f"  PASS: {n}-byte read returned {n} bytes")
            else:
                failures += 1
                print(f"  FAIL: {n}-byte read returned {len(data)} bytes")
        except Exception as exc:
            failures += 1
            print(f"  FAIL: {n}-byte read raised: {exc}")

    # Nothing should live at 0x7F; a write there must be refused, not silently
    # succeed. This is the check a ported "acks == 0" test would pass anyway,
    # so treat it as a floor rather than proof.
    try:
        i2c.write(address=0x7F, data=[0x00])
        failures += 1
        print("  FAIL: a write to 0x7F reported success with nothing there")
    except Exception:
        print("  PASS: a write to an empty address was refused")

    print(f"\n  I2C: {failures} failure(s)\n")
    return failures


def main():
    spi_net = _arg("--spi-net")
    i2c_net = _arg("--i2c-net")
    raw_addr = _arg("--i2c-addr")
    address = int(raw_addr, 0) if raw_addr else None

    print("=== LabJack U3 SPI/I2C hardware verification ===\n")
    failures = 0
    if spi_net:
        failures += check_spi(spi_net)
    else:
        print("SKIP: no --spi-net given\n")
    if i2c_net:
        failures += check_i2c(i2c_net, address)
    else:
        print("SKIP: no --i2c-net given\n")

    if failures:
        print(f"=== {failures} FAILURE(S) ===")
        sys.exit(1)
    print("=== All checks passed ===")


if __name__ == "__main__":
    main()
