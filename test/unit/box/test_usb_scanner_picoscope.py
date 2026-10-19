# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0
"""PicoScope discovery in ``box/lager/http_handlers/usb_scanner.py``.

Pico Technology ships a distinct USB product id for nearly every PicoScope
model. The scanner used to carry exactly one of them (0ce9:1007, the
2204A/2205A), so every other PicoScope -- the whole 3000, 4000 and 5000
series, and most of the 2000 series -- enumerated on the box and was silently
dropped, with no message anywhere to say why.

These tests cover the vendor-wide match that replaced it, and the channel
count read from the device rather than from a static table (2- and 4-channel
models share every series, so the entry name cannot imply it).

The count comes from the scope daemon, which reads the model off the unit when
it opens it. The USB product string is only a fallback: Pico's product ids are
per series, and a real unit reports the series ("PicoScope 2000A series PC
Oscilloscope"), not a model number.
"""

import importlib.util
import os
import sys
import tempfile
import unittest


HERE = os.path.dirname(__file__)
SCANNER_PATH = os.path.normpath(
    os.path.join(HERE, '..', '..', '..', 'box', 'lager', 'http_handlers',
                 'usb_scanner.py')
)

PICO_VID = '0ce9'

# What a real 2000A unit reports as its USB product string.
SERIES_PRODUCT = 'PicoScope 2000A series PC Oscilloscope'


def _load_scanner():
    spec = importlib.util.spec_from_file_location(
        'usb_scanner_pico_test', SCANNER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules['usb_scanner_pico_test'] = module
    spec.loader.exec_module(module)
    return module


class _FakePicoSysfs:
    """A minimal USB device node: idVendor, idProduct, serial, product."""

    def __init__(self, root, *, vid, pid, serial, product, bus_name):
        self.sys_bus = os.path.join(root, 'sys', 'bus', 'usb', 'devices')
        self.sys_class_tty = os.path.join(root, 'sys', 'class', 'tty')
        os.makedirs(self.sys_class_tty, exist_ok=True)
        dev = os.path.join(self.sys_bus, bus_name)
        os.makedirs(dev, exist_ok=True)
        for name, value in (('idVendor', vid), ('idProduct', pid),
                            ('serial', serial), ('product', product)):
            if value is not None:
                with open(os.path.join(dev, name), 'w') as f:
                    f.write(value + '\n')


class PicoScopeDiscovery(unittest.TestCase):

    def setUp(self):
        self.scanner = _load_scanner()
        tmpdir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__('shutil').rmtree(
            tmpdir, ignore_errors=True))
        self.tmpdir = tmpdir

    def _scan(self, devices, daemon=None):
        """Build a fake sysfs holding `devices` and return scanner entries.

        `daemon` is what the scope daemon's GetCapabilities answers with; the
        default None is a box with no daemon running.
        """
        self.daemon_queries = 0

        def _capabilities():
            self.daemon_queries += 1
            return daemon

        self.scanner._pico_daemon_capabilities = _capabilities
        fake = None
        for i, (pid, serial, product) in enumerate(devices):
            fake = _FakePicoSysfs(self.tmpdir, vid=PICO_VID, pid=pid,
                                  serial=serial, product=product,
                                  bus_name=f'3-{i + 1}')

        import pathlib
        real_path = pathlib.Path
        sys_bus, sys_tty = fake.sys_bus, fake.sys_class_tty

        def _path_shim(*args, **kw):
            if args and args[0] == '/sys/bus/usb/devices':
                return real_path(sys_bus)
            if args and args[0] == '/sys/class/tty':
                return real_path(sys_tty)
            return real_path(*args, **kw)

        original = self.scanner.Path
        self.scanner.Path = _path_shim
        try:
            return self.scanner.scan_usb()
        finally:
            self.scanner.Path = original

    def test_the_tested_2204a_still_resolves_to_its_named_entry(self):
        """The one model with a specific entry must keep using it."""
        entries = self._scan([('1007', 'AR911/011', 'PicoScope 2204A')])

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['name'], 'Picoscope_2000')
        self.assertEqual(entries[0]['net_type'], ['scope', 'scope-channel'])
        self.assertEqual(entries[0]['channels']['scope-channel'], ['1', '2'])

    def test_a_pico_with_an_unlisted_product_id_is_still_found(self):
        """The regression this change exists to prevent.

        Before the vendor-wide match, this device produced no entry at all.
        """
        entries = self._scan([('1018', 'JY123/456', 'PicoScope 3403D')])

        self.assertEqual(len(entries), 1, 'unlisted PicoScope was dropped')
        self.assertEqual(entries[0]['name'], 'Picoscope')
        self.assertEqual(entries[0]['net_type'], ['scope', 'scope-channel'])

    def test_without_the_daemon_a_model_number_is_read_from_the_product(self):
        """A 4-channel scope must offer 4 channels, a 2-channel one 2, when
        the product string carries a model number."""
        four = self._scan([('1018', 'S4', 'PicoScope 3403D')])[0]
        self.assertEqual(four['channels']['scope-channel'], ['1', '2', '3', '4'])

        two = self._scan([('10ff', 'S2', 'PicoScope 5242D')])[0]
        self.assertEqual(two['channels']['scope-channel'], ['1', '2'])

    def test_channel_count_falls_back_to_two_when_unreadable(self):
        """With no daemon to ask, an unparseable product string must not
        invent channels.

        This under-reports a 4-channel unit, and nothing corrects it later;
        offering a channel that is not there would be offered as a net and
        fail only when someone drives it.
        """
        entries = self._scan([('1018', 'S5', 'Some New PicoScope')])

        self.assertEqual(entries[0]['channels']['scope-channel'], ['1', '2'])

    def test_the_model_string_is_reported(self):
        """Callers need the real model; the entry name is now generic."""
        entries = self._scan([('1018', 'S6', 'PicoScope 3403D')])

        self.assertEqual(entries[0]['model'], 'PicoScope 3403D')

    def test_channel_counts_across_every_supported_series(self):
        """The second digit of a Pico model number is the channel count."""
        cases = {
            'PicoScope 2204A': 2, 'PicoScope 2205A': 2, 'PicoScope 2405A': 4,
            'PicoScope 3204D': 2, 'PicoScope 3403D': 4,
            'PicoScope 4224': 2, 'PicoScope 4424': 4,
            'PicoScope 5242D': 2, 'PicoScope 5442D': 4,
        }
        for model, expected in cases.items():
            with self.subTest(model=model):
                self.assertEqual(
                    self.scanner._pico_channel_count(model), expected)

    def test_an_eight_channel_scope_offers_the_four_that_lager_names(self):
        """Channels stop at D everywhere above the daemon, so a 4824 offers
        A to D rather than falling back to two."""
        self.assertEqual(self.scanner._pico_channel_count('PicoScope 4824'), 4)

        entries = self._scan([('1018', 'S6', 'PicoScope 4824')])

        self.assertEqual(entries[0]['channels']['scope-channel'],
                         ['1', '2', '3', '4'])

    def test_a_series_level_product_string_takes_the_daemons_count(self):
        """The reported bug: a 4-channel 2000A reports its series, not its
        model, so the product string alone offered channels 1 and 2."""
        entries = self._scan(
            [('1016', 'JO123/0045', SERIES_PRODUCT)],
            daemon={'serial': 'JO123/0045', 'model': '2405A',
                    'analog_channels': 4})

        self.assertEqual(entries[0]['channels']['scope-channel'],
                         ['1', '2', '3', '4'])
        self.assertEqual(entries[0]['channels']['scope'], [])
        self.assertEqual(entries[0]['model'], 'PicoScope 2405A')

    def test_the_daemon_count_wins_over_the_product_string(self):
        """The daemon read the unit itself, so it also corrects downward."""
        entries = self._scan(
            [('1018', 'S4', 'PicoScope 3403D')],
            daemon={'serial': 'S4', 'model': '3203D', 'analog_channels': 2})

        self.assertEqual(entries[0]['channels']['scope-channel'], ['1', '2'])

    def test_a_series_level_product_string_without_the_daemon_offers_two(self):
        entries = self._scan([('1016', 'JO123/0045', SERIES_PRODUCT)])

        self.assertEqual(entries[0]['channels']['scope-channel'], ['1', '2'])
        self.assertEqual(entries[0]['model'], SERIES_PRODUCT)

    def test_a_lone_scope_takes_the_daemons_count_without_a_serial(self):
        """One PicoScope and one open unit can only be the same device."""
        entries = self._scan(
            [('1016', None, SERIES_PRODUCT)],
            daemon={'serial': 'JO123/0045', 'model': '2405A',
                    'analog_channels': 4})

        self.assertEqual(entries[0]['channels']['scope-channel'],
                         ['1', '2', '3', '4'])

    def test_the_daemons_count_is_not_applied_to_another_unit(self):
        """The daemon has one unit open; on a two-scope box, the other unit
        must not inherit its channel count."""
        entries = self._scan(
            [('1016', 'OPEN/1', SERIES_PRODUCT),
             ('1016', 'OTHER/2', SERIES_PRODUCT)],
            daemon={'serial': 'OPEN/1', 'model': '2405A',
                    'analog_channels': 4})

        by_serial = {e['serial']: e['channels']['scope-channel'] for e in entries}
        self.assertEqual(by_serial['OPEN/1'], ['1', '2', '3', '4'])
        self.assertEqual(by_serial['OTHER/2'], ['1', '2'])

    def test_without_serials_two_scopes_do_not_take_the_daemons_count(self):
        entries = self._scan(
            [('1016', None, SERIES_PRODUCT), ('1016', None, SERIES_PRODUCT)],
            daemon={'serial': 'OPEN/1', 'model': '2405A',
                    'analog_channels': 4})

        for entry in entries:
            self.assertEqual(entry['channels']['scope-channel'], ['1', '2'])

    def test_an_eight_channel_daemon_answer_offers_four(self):
        entries = self._scan(
            [('1018', 'S8', SERIES_PRODUCT)],
            daemon={'serial': 'S8', 'model': '4824', 'analog_channels': 8})

        self.assertEqual(entries[0]['channels']['scope-channel'],
                         ['1', '2', '3', '4'])

    def test_the_daemon_is_asked_once_per_scan(self):
        self._scan([('1016', 'A/1', SERIES_PRODUCT),
                    ('1016', 'B/2', SERIES_PRODUCT)])

        self.assertEqual(self.daemon_queries, 1)

    def test_an_unreachable_daemon_answers_none(self):
        """The real lookup, against a port nothing listens on, must fall back
        rather than raise out of the scan."""
        scanner = _load_scanner()
        old = os.environ.get('LAGER_SCOPE_DATA_URL')
        os.environ['LAGER_SCOPE_DATA_URL'] = 'ws://127.0.0.1:9/'
        try:
            self.assertIsNone(scanner._pico_daemon_capabilities())
        finally:
            if old is None:
                os.environ.pop('LAGER_SCOPE_DATA_URL', None)
            else:
                os.environ['LAGER_SCOPE_DATA_URL'] = old

    def test_the_vendor_wide_entry_is_not_in_the_vidpid_table(self):
        """It has no product id, so it must not shadow a real one."""
        pico_entries = [name for (vid, _pid), name
                        in self.scanner._VIDPID_TO_NAME.items()
                        if vid == PICO_VID]

        self.assertEqual(pico_entries, ['Picoscope_2000'])
        self.assertEqual(self.scanner._VID_ONLY_VENDORS[PICO_VID], 'Picoscope')


class TheScopeIsOfferedAsANetOfItsOwn(unittest.TestCase):
    """A 2-channel PicoScope should offer three nets: the two channels and
    the scope they belong to.

    Most of an oscilloscope is not per-channel -- the timebase, the trigger,
    run/stop -- and until the scope net existed those had to be addressed to
    whichever channel the caller happened to be holding.

    The scope's entry carries an empty channel list, which is what says it is
    one net rather than a list of them. That is more than a display detail: a
    channel number saved onto a scope net is exactly what the box reads to
    tell a scope from one of its channels, so an entry offering "1" here
    would be saved with a pin and migrated straight back into a channel.
    """

    def setUp(self):
        self.helper = PicoScopeDiscovery('run')
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)

    def _entry(self, product='PicoScope 2204A', pid='1007'):
        return self.helper._scan([(pid, 'AR911/011', product)])[0]

    def test_both_roles_are_offered(self):
        self.assertEqual(self._entry()['net_type'], ['scope', 'scope-channel'])

    def test_the_scope_has_no_channels_of_its_own(self):
        self.assertEqual(self._entry()['channels']['scope'], [])

    def test_the_channels_are_numbered_from_one(self):
        self.assertEqual(self._entry()['channels']['scope-channel'], ['1', '2'])

    def test_a_two_channel_model_offers_three_nets(self):
        channels = self._entry()['channels']
        offered = sum(len(chs) or 1 for chs in channels.values())
        self.assertEqual(offered, 3)

    def test_a_four_channel_model_offers_five(self):
        channels = self._entry(product='PicoScope 3403D', pid='1018')['channels']
        offered = sum(len(chs) or 1 for chs in channels.values())
        self.assertEqual(offered, 5)


class UdevGrantsAccessToEveryPicoScope(unittest.TestCase):
    """The scanner finding a scope is useless if the box cannot open it.

    Detection and permission have to widen together: a per-PID udev rule with
    a vendor-wide scanner would list a scope that then fails with EACCES.
    """

    def test_the_rule_is_vendor_wide(self):
        rules_path = os.path.normpath(
            os.path.join(HERE, '..', '..', '..', 'box', 'udev_rules',
                         '99-instrument.rules'))
        with open(rules_path) as f:
            rules = f.read()

        pico_rules = [line for line in rules.splitlines()
                      if f'"{PICO_VID}"' in line and not line.startswith('#')]

        self.assertEqual(len(pico_rules), 1,
                         'expected exactly one Pico udev rule, got: %s'
                         % pico_rules)
        self.assertNotIn('idProduct', pico_rules[0],
                         'Pico udev rule is per-product, so scopes the '
                         'scanner now finds would fail to open')
        self.assertIn('GROUP="lager"', pico_rules[0])


if __name__ == '__main__':
    unittest.main()
