# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Word/byte conversion shared by every SPI backend.

These are characterization tests. Every expected value here was captured from
``LabJackSPI`` BEFORE the conversion helpers moved to ``SPIBase``, so the suite
fails if the move changed any observable output. That is the whole point: the
helpers were lifted so the LabJack UD (U3) driver could reuse them instead of
carrying a second copy of the bit reversal, and a second copy that disagrees
with the first is exactly the defect the move exists to prevent.

Nothing here touches LJM or a device. ``LabJackSPI.__init__`` only validates
and registers pin claims, so it constructs fine with no hardware present.
"""
import unittest

from lager.exceptions import SPIBackendError
from lager.protocols.spi.labjack_spi import LabJackSPI
from lager.protocols.spi.spi_base import SPIBase


def _driver(word_size, bit_order):
    return LabJackSPI(cs_pin=0, clk_pin=1, mosi_pin=2, miso_pin=3,
                      word_size=word_size, bit_order=bit_order)


# Captured from the T7 driver before the helpers moved to SPIBase.
WORDS = {
    8:  [0x00, 0x01, 0x80, 0xA5, 0xFF],
    16: [0x0000, 0x0001, 0x1234, 0xABCD, 0xFFFF],
    32: [0x00000000, 0x00000001, 0x12345678, 0xDEADBEEF, 0xFFFFFFFF],
}
WORDS_TO_BYTES = {
    (8, "msb"):  [0, 1, 128, 165, 255],
    (8, "lsb"):  [0, 128, 1, 165, 255],
    (16, "msb"): [0, 0, 0, 1, 18, 52, 171, 205, 255, 255],
    (16, "lsb"): [0, 0, 128, 0, 44, 72, 179, 213, 255, 255],
    (32, "msb"): [0, 0, 0, 0, 0, 0, 0, 1, 18, 52, 86, 120,
                  222, 173, 190, 239, 255, 255, 255, 255],
    (32, "lsb"): [0, 0, 0, 0, 128, 0, 0, 0, 30, 106, 44, 72,
                  247, 125, 181, 123, 255, 255, 255, 255],
}
RAW_BYTES = {
    8:  [0, 1, 2, 3],
    16: [0, 1, 2, 3, 4, 5, 6, 7],
    32: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15],
}
BYTES_TO_WORDS = {
    (8, "msb"):  [0, 1, 2, 3],
    (8, "lsb"):  [0, 128, 64, 192],
    (16, "msb"): [1, 515, 1029, 1543],
    (16, "lsb"): [32768, 49216, 40992, 57440],
    (32, "msb"): [66051, 67438087, 134810123, 202182159],
    (32, "lsb"): [3225452544, 3764428832, 3494940688, 4033916976],
}

_CASES = [(ws, bo) for ws in (8, 16, 32) for bo in ("msb", "lsb")]


class T7BehaviourIsUnchangedTests(unittest.TestCase):
    """The T7's observable output, byte for byte, across the whole matrix."""

    def test_words_to_bytes(self):
        for word_size, bit_order in _CASES:
            with self.subTest(word_size=word_size, bit_order=bit_order):
                got = _driver(word_size, bit_order)._words_to_bytes(
                    WORDS[word_size])
                self.assertEqual(got, WORDS_TO_BYTES[(word_size, bit_order)])

    def test_bytes_to_words(self):
        for word_size, bit_order in _CASES:
            with self.subTest(word_size=word_size, bit_order=bit_order):
                got = _driver(word_size, bit_order)._bytes_to_words(
                    RAW_BYTES[word_size])
                self.assertEqual(got, BYTES_TO_WORDS[(word_size, bit_order)])

    def test_round_trip_is_the_identity(self):
        """Every matrix cell survives a there-and-back trip."""
        for word_size, bit_order in _CASES:
            with self.subTest(word_size=word_size, bit_order=bit_order):
                drv = _driver(word_size, bit_order)
                words = WORDS[word_size]
                self.assertEqual(
                    drv._bytes_to_words(drv._words_to_bytes(words)), words)

    def test_a_ragged_tail_keeps_its_short_word(self):
        """Bytes that do not fill a whole word are not silently dropped.

        Preserved deliberately rather than asserted as correct: a truncated SPI
        read is a real thing to hand back, and changing it is a behaviour
        change, not a cleanup.
        """
        self.assertEqual(
            _driver(16, "msb")._bytes_to_words([0xAA, 0xBB, 0xCC]),
            [0xAABB, 0x00CC])
        self.assertEqual(
            _driver(32, "msb")._bytes_to_words([1, 2, 3, 4, 5]),
            [0x01020304, 0x05])

    def test_an_oversized_word_is_refused_with_the_same_message(self):
        for word_size, bad in ((8, 0x1FF), (16, 0x10000), (32, 0x100000000)):
            with self.subTest(word_size=word_size):
                with self.assertRaises(SPIBackendError) as ctx:
                    _driver(word_size, "msb")._words_to_bytes([bad])
                self.assertIn(f"exceeds {word_size}-bit word size", str(ctx.exception))


class SharedOnTheBaseTests(unittest.TestCase):
    """The helpers are reachable from the base, so a second driver can reuse them."""

    def test_base_staticmethods_exist_and_take_explicit_parameters(self):
        self.assertEqual(
            SPIBase.words_to_bytes([0x1234], word_size=16, bit_order="msb"),
            [0x12, 0x34])
        self.assertEqual(
            SPIBase.bytes_to_words([0x12, 0x34], word_size=16, bit_order="msb"),
            [0x1234])

    def test_the_t7_delegates_rather_than_carrying_its_own_copy(self):
        """The T7's private helpers must agree with the base exactly.

        A second implementation of the bit reversal is the failure this move
        exists to prevent, so agreement is asserted rather than assumed.
        """
        for word_size, bit_order in _CASES:
            with self.subTest(word_size=word_size, bit_order=bit_order):
                drv = _driver(word_size, bit_order)
                self.assertEqual(
                    drv._words_to_bytes(WORDS[word_size]),
                    SPIBase.words_to_bytes(WORDS[word_size],
                                           word_size=word_size,
                                           bit_order=bit_order))
                self.assertEqual(
                    drv._bytes_to_words(RAW_BYTES[word_size]),
                    SPIBase.bytes_to_words(RAW_BYTES[word_size],
                                           word_size=word_size,
                                           bit_order=bit_order))

    def test_defaults_are_the_eight_bit_msb_first_case(self):
        self.assertEqual(SPIBase.words_to_bytes([0xA5]), [0xA5])
        self.assertEqual(SPIBase.bytes_to_words([0xA5]), [0xA5])
