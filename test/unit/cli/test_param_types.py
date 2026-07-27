#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for ``cli/core/param_types.py`` -- the custom click ParamTypes that
every command's argument validation funnels through.

Five of these are on live user-facing paths and had no coverage at all:

    EnvVarType         `lager devenv --env`, `lager devenv env`, `lager python --env`
    PortForwardType    `lager python --port`
    MemoryAddressType  `lager debug memrd <start_addr> <length>`
    HexArrayType       `lager debug`
    BinfileType        `lager debug --bin` (constructed with exists=True)

The rest (``CanFrameType``, ``CanFilterType``, ``CanbusRange``,
``ADCChannelType``, ``VarAssignmentType``) are re-exported from
``cli/core/__init__.py`` but wired to no command -- there is no ``lager canbus``
group registered in ``cli/main.py``. They are covered here anyway because
export-only today is not export-only forever, and two of them are broken; see
``CanFdIsBrokenTests``.

``ParamType.fail()`` raises ``click.BadParameter`` (a ``UsageError`` subclass),
which is what click renders as a clean usage message rather than a traceback.
Tests assert that type specifically: a raw ``ValueError`` escaping ``convert``
is a bug, because click does not catch it and the user gets a stack trace.
"""

import os
import tempfile
import unittest

import click
import pytest

from cli.core.param_types import (
    ADCChannelType,
    Binfile,
    BinfileType,
    CanbusRange,
    CanFilter,
    CanFilterType,
    CanFrame,
    CanFrameType,
    EnvVarType,
    HexArrayType,
    HexParamType,
    MemoryAddressType,
    PortForwardSpecifier,
    PortForwardType,
    VarAssignmentType,
    grouper,
    parse_can_data,
    parse_can2,
    parse_canfd,
)


def convert(param_type, value):
    """Call convert() the way click does, with no param/ctx."""
    return param_type.convert(value, None, None)


class MemoryAddressTypeTests(unittest.TestCase):
    """`lager debug memrd 0x20000000 256` -- both args go through this."""

    def test_hex_with_prefix(self):
        self.assertEqual(convert(MemoryAddressType(), '0x20000000'), 0x20000000)

    def test_hex_prefix_is_case_insensitive(self):
        self.assertEqual(convert(MemoryAddressType(), '0X1A'), 0x1A)

    def test_decimal_without_prefix(self):
        self.assertEqual(convert(MemoryAddressType(), '256'), 256)

    def test_surrounding_whitespace_is_stripped(self):
        self.assertEqual(convert(MemoryAddressType(), '  0x10  '), 0x10)

    def test_bare_hex_digits_are_decimal_not_hex(self):
        """No 0x prefix means base 10, so '10' is ten, not sixteen.

        Pinned because it is a plausible place to "helpfully" auto-detect hex,
        which would silently change every existing decimal invocation.
        """
        self.assertEqual(convert(MemoryAddressType(), '10'), 10)

    def test_bare_hex_letters_are_rejected(self):
        with self.assertRaises(click.BadParameter):
            convert(MemoryAddressType(), 'deadbeef')

    def test_malformed_hex_is_rejected(self):
        with self.assertRaises(click.BadParameter):
            convert(MemoryAddressType(), '0xZZ')

    def test_empty_is_rejected(self):
        with self.assertRaises(click.BadParameter):
            convert(MemoryAddressType(), '')

    def test_repr_is_the_metavar(self):
        self.assertEqual(repr(MemoryAddressType()), 'ADDR')


class HexParamTypeTests(unittest.TestCase):

    def test_plain_hex(self):
        self.assertEqual(convert(HexParamType(), '1a'), 0x1A)

    def test_prefixed_hex(self):
        self.assertEqual(convert(HexParamType(), '0x1a'), 0x1A)

    def test_invalid_is_rejected(self):
        with self.assertRaises(click.BadParameter):
            convert(HexParamType(), 'nope')

    def test_repr(self):
        self.assertEqual(repr(HexParamType()), 'HEX')


class GrouperTests(unittest.TestCase):

    def test_chunks_evenly(self):
        self.assertEqual(list(grouper(iter('aabbcc'), 2)),
                         [['a', 'a'], ['b', 'b'], ['c', 'c']])

    def test_final_chunk_may_be_short(self):
        self.assertEqual(list(grouper(iter('abc'), 2)), [['a', 'b'], ['c']])

    def test_empty_input(self):
        self.assertEqual(list(grouper(iter(''), 2)), [])


class HexArrayTypeTests(unittest.TestCase):

    def test_bytes_are_split_pairwise(self):
        self.assertEqual(convert(HexArrayType(), 'deadbeef'), [0xDE, 0xAD, 0xBE, 0xEF])

    def test_single_byte(self):
        self.assertEqual(convert(HexArrayType(), 'ff'), [0xFF])

    def test_uppercase(self):
        self.assertEqual(convert(HexArrayType(), 'DEAD'), [0xDE, 0xAD])

    def test_empty_is_an_empty_array(self):
        """Zero is even, so '' converts rather than failing."""
        self.assertEqual(convert(HexArrayType(), ''), [])

    def test_odd_digit_count_is_rejected(self):
        with self.assertRaises(click.BadParameter):
            convert(HexArrayType(), 'abc')

    def test_non_hex_is_rejected(self):
        with self.assertRaises(click.BadParameter):
            convert(HexArrayType(), 'zzzz')

    def test_repr(self):
        self.assertEqual(repr(HexArrayType()), 'HEXARRAY')


class VarAssignmentTypeTests(unittest.TestCase):

    def test_splits_into_pair(self):
        self.assertEqual(convert(VarAssignmentType(), 'FOO=BAR'), ['FOO', 'BAR'])

    def test_missing_equals_is_rejected(self):
        with self.assertRaises(click.BadParameter):
            convert(VarAssignmentType(), 'FOO')

    def test_two_equals_is_rejected(self):
        """Unlike EnvVarType, this one does NOT split once.

        A value containing '=' is refused rather than kept whole. Pinned
        because the two types look interchangeable and are not.
        """
        with self.assertRaises(click.BadParameter):
            convert(VarAssignmentType(), 'FOO=BAR=BAZ')


class EnvVarTypeTests(unittest.TestCase):
    """`lager python --env FOO=BAR`, `lager devenv env FOO=BAR`."""

    def test_returns_the_whole_assignment_not_a_pair(self):
        """Returns the original string, unlike VarAssignmentType."""
        self.assertEqual(convert(EnvVarType(), 'FOO=BAR'), 'FOO=BAR')

    def test_value_may_contain_equals(self):
        """maxsplit=1, so a base64 or key=value payload survives intact."""
        self.assertEqual(convert(EnvVarType(), 'FOO=a=b=c'), 'FOO=a=b=c')

    def test_empty_value_is_allowed(self):
        self.assertEqual(convert(EnvVarType(), 'FOO='), 'FOO=')

    def test_underscore_prefix_is_allowed(self):
        self.assertEqual(convert(EnvVarType(), '_PRIVATE=1'), '_PRIVATE=1')

    def test_digits_allowed_after_first_character(self):
        self.assertEqual(convert(EnvVarType(), 'PY3PATH=/x'), 'PY3PATH=/x')

    def test_missing_equals_is_rejected(self):
        with self.assertRaises(click.BadParameter):
            convert(EnvVarType(), 'FOO')

    def test_leading_digit_is_rejected(self):
        with self.assertRaises(click.BadParameter):
            convert(EnvVarType(), '1FOO=BAR')

    def test_hyphen_in_name_is_rejected(self):
        with self.assertRaises(click.BadParameter):
            convert(EnvVarType(), 'FOO-BAR=1')

    def test_empty_name_is_rejected(self):
        with self.assertRaises(click.BadParameter):
            convert(EnvVarType(), '=VALUE')

    def test_shell_metacharacters_in_name_are_rejected(self):
        for bad in ('FOO;rm=1', 'FOO$X=1', 'FOO BAR=1'):
            with self.subTest(value=bad):
                with self.assertRaises(click.BadParameter):
                    convert(EnvVarType(), bad)


class BinfileTypeTests(unittest.TestCase):
    """`lager debug flash --bin firmware.bin,0x8000` (exists=True there)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.binpath = os.path.join(self.tmp, 'firmware.bin')
        with open(self.binpath, 'wb') as f:
            f.write(b'\x00')
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_path_and_address(self):
        result = convert(BinfileType(), f'{self.binpath},0x8000')
        self.assertEqual(result, Binfile(path=self.binpath, address=0x8000))

    def test_address_may_be_bare_hex(self):
        self.assertEqual(convert(BinfileType(), f'{self.binpath},8000').address, 0x8000)

    def test_rsplit_means_a_comma_in_the_path_is_kept(self):
        """Split is rsplit(',', 1), so only the LAST comma separates.

        A path containing a comma still works, which is the reason rsplit is
        used rather than split.
        """
        odd = os.path.join(self.tmp, 'fw,v2.bin')
        with open(odd, 'wb') as f:
            f.write(b'\x00')
        self.assertEqual(convert(BinfileType(), f'{odd},0x10').path, odd)

    def test_missing_comma_is_rejected(self):
        with self.assertRaises(click.BadParameter):
            convert(BinfileType(), self.binpath)

    def test_bad_address_is_rejected(self):
        with self.assertRaises(click.BadParameter):
            convert(BinfileType(), f'{self.binpath},nothex')

    def test_exists_true_rejects_a_missing_file(self):
        missing = os.path.join(self.tmp, 'not-here.bin')
        with self.assertRaises(click.BadParameter):
            convert(BinfileType(exists=True), f'{missing},0x0')

    def test_exists_false_accepts_a_missing_file(self):
        missing = os.path.join(self.tmp, 'not-here.bin')
        self.assertEqual(convert(BinfileType(exists=False), f'{missing},0x0').path, missing)

    def test_repr(self):
        self.assertEqual(repr(BinfileType()), 'BINFILE')


class PortForwardTypeTests(unittest.TestCase):
    """`lager python --port 8080:80/tcp`."""

    def test_single_port_mirrors_source_to_dest(self):
        self.assertEqual(convert(PortForwardType(), '8080'),
                         PortForwardSpecifier(8080, 8080, None))

    def test_src_and_dst(self):
        self.assertEqual(convert(PortForwardType(), '8080:80'),
                         PortForwardSpecifier(8080, 80, None))

    def test_with_protocol(self):
        self.assertEqual(convert(PortForwardType(), '8080:80/tcp'),
                         PortForwardSpecifier(8080, 80, 'tcp'))

    def test_protocol_without_dst(self):
        self.assertEqual(convert(PortForwardType(), '8080/udp'),
                         PortForwardSpecifier(8080, 8080, 'udp'))

    def test_reserved_ports_are_rejected(self):
        """These are the box's own services; forwarding them would collide."""
        for port in PortForwardType.RESERVED:
            with self.subTest(port=port):
                with self.assertRaises(click.BadParameter):
                    convert(PortForwardType(), str(port))

    def test_reserved_check_applies_to_source_only(self):
        """A reserved number as the DESTINATION is allowed.

        The collision is on the box's listening side, so 2331 as a dest inside
        the container is fine. Pinned so a later "tighten this" change has to
        be deliberate.
        """
        self.assertEqual(convert(PortForwardType(), '9000:2331').dst, 2331)

    def test_uppercase_protocol_is_rejected(self):
        with self.assertRaises(click.BadParameter):
            convert(PortForwardType(), '8080/TCP')

    def test_malformed_specifiers_are_rejected(self):
        for bad in ('', 'abc', '80:', ':80', '80:80:80', '80/', '-1'):
            with self.subTest(value=bad):
                with self.assertRaises(click.BadParameter):
                    convert(PortForwardType(), bad)


class CanFilterTypeTests(unittest.TestCase):
    """Export-only today (no `lager canbus` group), but it works."""

    def test_standard_11_bit_id(self):
        self.assertEqual(convert(CanFilterType(), '123:7FF'),
                         CanFilter(can_id=0x123, can_mask=0x7FF, extended=False))

    def test_extended_29_bit_id(self):
        result = convert(CanFilterType(), '12345678:1FFFFFFF')
        self.assertTrue(result.extended)
        self.assertEqual(result.can_id, 0x12345678)

    def test_id_length_must_be_3_or_8(self):
        for bad in ('12:7FF', '1234:7FF', '123456789:7FF'):
            with self.subTest(value=bad):
                with self.assertRaises(click.BadParameter):
                    convert(CanFilterType(), bad)

    def test_missing_colon_is_rejected(self):
        with self.assertRaises(click.BadParameter):
            convert(CanFilterType(), '123')


class CanbusRangeTests(unittest.TestCase):

    def test_single_value(self):
        self.assertEqual(convert(CanbusRange(), '0'), [0])

    def test_comma_list(self):
        self.assertEqual(convert(CanbusRange(), '0,2,1'), [0, 1, 2])

    def test_range_is_inclusive_of_the_end(self):
        self.assertEqual(convert(CanbusRange(), '0-3'), [0, 1, 2, 3])

    def test_mixed_and_deduplicated(self):
        self.assertEqual(convert(CanbusRange(), '0-2,1,4'), [0, 1, 2, 4])

    def test_too_many_dashes_is_rejected(self):
        with self.assertRaises(click.BadParameter):
            convert(CanbusRange(), '1-2-3')


class CanFrameParsingTests(unittest.TestCase):
    """Classic CAN 2.0 frames, which do work."""

    def test_data_frame(self):
        self.assertEqual(convert(CanFrameType(), '123#DEADBEEF'), CanFrame(
            arbitration_id=0x123, is_fd=False, is_error_frame=False,
            is_remote_frame=False, is_extended_id=False,
            data=[0xDE, 0xAD, 0xBE, 0xEF]))

    def test_remote_frame_has_no_data(self):
        result = convert(CanFrameType(), '123#R')
        self.assertTrue(result.is_remote_frame)
        self.assertIsNone(result.data)

    def test_dot_separated_data_groups_are_concatenated(self):
        self.assertEqual(parse_can_data('DE.AD.BEEF'), [0xDE, 0xAD, 0xBE, 0xEF])

    def test_empty_data(self):
        self.assertEqual(parse_can2('123#').data, [])


class CanFdIsBrokenTests(unittest.TestCase):
    """CAN-FD send is non-functional. Two independent defects.

    Not fixed here: nothing reaches this code (no ``lager canbus`` group is
    registered in ``cli/main.py``, and no command references ``CanFrameType``),
    and choosing the right fix means deciding what ``flags`` should map to on
    ``CanFrame`` -- a design call, not a test-coverage one.

    Both are ``xfail(strict=True)``: they record the defect in the suite today,
    and the moment someone fixes it these XPASS, which strict mode turns into a
    failure -- forcing whoever fixes it to delete the marker rather than
    silently leaving a stale "known broken" note behind.
    """

    @pytest.mark.xfail(strict=True, reason=(
        "CanFrameType.convert tests `'#' in value` before `'##' in value`, so a "
        "CAN-FD frame always takes the CAN-2.0 branch and dies in parse_can2 "
        "with 'too many values to unpack'. The '##' branch is unreachable."))
    def test_canfd_frame_is_routed_to_parse_canfd(self):
        result = convert(CanFrameType(), '123##1AABB')
        self.assertTrue(result.is_fd)

    @pytest.mark.xfail(strict=True, reason=(
        "parse_canfd passes flags= to the CanFrame namedtuple, which has no "
        "'flags' field, so it raises TypeError even when called directly."))
    def test_parse_canfd_constructs_a_frame(self):
        result = parse_canfd('123##1AABB')
        self.assertEqual(result.arbitration_id, 0x123)

    def test_the_two_defects_are_still_exactly_these(self):
        """Pin the failure MODES, so the xfails above cannot drift.

        A strict xfail only says "this does not work". If the failure changed
        from TypeError to something else, the xfails would still pass and the
        reasons above would quietly become wrong.
        """
        with self.assertRaises(ValueError) as ctx:
            convert(CanFrameType(), '123##1AABB')
        self.assertIn('too many values to unpack', str(ctx.exception))

        with self.assertRaises(TypeError) as ctx:
            parse_canfd('123##1AABB')
        self.assertIn('flags', str(ctx.exception))


class ADCChannelTypeTests(unittest.TestCase):
    """Export-only today (no command references it)."""

    def test_single_channel(self):
        self.assertEqual(convert(ADCChannelType(), '3'), {'channel': 3})

    def test_special_names_pass_through(self):
        self.assertEqual(convert(ADCChannelType(), 'VTREF'), 'VTREF')
        self.assertEqual(convert(ADCChannelType(), 'VIO'), 'VIO')

    def test_special_names_are_case_sensitive(self):
        with self.assertRaises(ValueError):
            convert(ADCChannelType(), 'vtref')

    def test_range(self):
        self.assertEqual(convert(ADCChannelType(), '1-4'), {'start': 1, 'end': 4})

    def test_channel_above_range_is_rejected(self):
        with self.assertRaises(click.BadParameter):
            convert(ADCChannelType(), '6')

    def test_negative_channel_is_read_as_a_malformed_range(self):
        """The `value < 0` guard on the single-channel path is UNREACHABLE.

        '-1' contains '-', so convert() takes the range branch first and does
        `'-1'.split('-', 1)` -> ['', '1'] -> int('') -> ValueError. Control
        never reaches the `if value < 0 or value > 5` check below it, so that
        half of the condition is dead code.

        Pinned as-is rather than "fixed" because nothing reaches this module
        today. Whoever wires up a command that uses it needs to handle the
        leading-minus case before the '-' split, not after.
        """
        with self.assertRaises(ValueError) as ctx:
            convert(ADCChannelType(), '-1')
        self.assertNotIsInstance(ctx.exception, click.BadParameter)
        self.assertIn("invalid literal for int()", str(ctx.exception))

    def test_inverted_range_is_rejected(self):
        with self.assertRaises(click.BadParameter):
            convert(ADCChannelType(), '4-1')

    def test_equal_range_is_rejected(self):
        with self.assertRaises(click.BadParameter):
            convert(ADCChannelType(), '2-2')

    def test_non_numeric_raises_raw_valueerror_not_badparameter(self):
        """Documents rough edge, does not endorse it.

        Every other rejection here goes through self.fail() and reaches the
        user as a clean usage message. This one escapes as a bare ValueError,
        which click does not catch -- so it would surface as a traceback. It is
        unreachable today (no command uses ADCChannelType); if one is ever
        wired up, this test is the reminder to route it through self.fail().
        """
        with self.assertRaises(ValueError) as ctx:
            convert(ADCChannelType(), 'abc')
        self.assertNotIsInstance(ctx.exception, click.BadParameter)


if __name__ == '__main__':
    unittest.main()
