# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for the user-script / slot helpers in
``box/lager/nets/debug_net.py``.

These two helpers are what wire the saved-net ``openocd_config`` /
``jlink_script`` (base64) and ``serial`` fields through to the underlying
backend invocations. Without them, in-box Python tests can't pick up the
custom debug scripts the user attached via ``lager nets set-openocd-config``
/ ``set-script`` — see the parity-audit notes for the back-story.

We load the module by file path so we don't drag in the rest of the lager
package (the production import chain needs the box-side hardware drivers
which aren't installable in the unit-test env).
"""

import base64
import importlib.util
import os
import sys
import tempfile
import types
import unittest


HERE = os.path.dirname(__file__)
NETS_DIR = os.path.normpath(
    os.path.join(HERE, '..', '..', '..', 'box', 'lager', 'nets')
)
DEBUG_NET_PATH = os.path.join(NETS_DIR, 'debug_net.py')
CONSTANTS_PATH = os.path.join(NETS_DIR, 'constants.py')


_INSTALLED_STUB_KEYS = []


def _load_debug_net_helpers():
    """Load ``debug_net`` just enough to exercise the module-level helpers.

    The real module's ``try/except`` import block pulls in box-side drivers
    we don't want to require here. The helpers we care about
    (``materialise_user_script``, ``allocate_probe_slot``) are defined at
    module top level *above* that try block, so importing the file under a
    stub package picks them up without executing the heavy branch.

    Records every sys.modules key it added so the tearDownModule hook can
    pop them — otherwise our minimal ``lager`` stub would shadow the real
    package for any later test in the same pytest session.
    """
    def _ensure(name, factory):
        if name not in sys.modules:
            sys.modules[name] = factory()
            _INSTALLED_STUB_KEYS.append(name)

    def _lager_pkg():
        # Real-package shape (with __path__) so any later ``import
        # lager.<submod>`` that *does* happen continues to work.
        m = types.ModuleType('lager')
        m.__path__ = []  # marks as package
        return m

    def _lager_constants():
        m = types.ModuleType('lager.constants')
        m.HARDWARE_SERVICE_PORT = 0  # value irrelevant here
        return m

    _ensure('lager', _lager_pkg)
    _ensure('lager.constants', _lager_constants)

    pkg = types.ModuleType('stub_nets_pkg')
    pkg.__path__ = [NETS_DIR]
    sys.modules['stub_nets_pkg'] = pkg
    _INSTALLED_STUB_KEYS.append('stub_nets_pkg')

    constants_spec = importlib.util.spec_from_file_location(
        'stub_nets_pkg.constants', CONSTANTS_PATH,
    )
    constants_mod = importlib.util.module_from_spec(constants_spec)
    constants_mod.__package__ = 'stub_nets_pkg'
    sys.modules['stub_nets_pkg.constants'] = constants_mod
    _INSTALLED_STUB_KEYS.append('stub_nets_pkg.constants')
    constants_spec.loader.exec_module(constants_mod)

    spec = importlib.util.spec_from_file_location(
        'stub_nets_pkg.debug_net', DEBUG_NET_PATH,
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = 'stub_nets_pkg'
    sys.modules['stub_nets_pkg.debug_net'] = mod
    _INSTALLED_STUB_KEYS.append('stub_nets_pkg.debug_net')
    # The inner try-block import fails (no ..debug pkg under stub), which
    # is fine: helpers we want are above it. The except branch installs the
    # _NullDebug factory.
    spec.loader.exec_module(mod)
    return mod


debug_net = _load_debug_net_helpers()


def tearDownModule():
    """Remove our sys.modules stubs so later test modules see the real lager."""
    for key in _INSTALLED_STUB_KEYS:
        sys.modules.pop(key, None)


class MaterialiseUserScriptTests(unittest.TestCase):
    """``materialise_user_script`` — base64 → shared temp file."""

    def setUp(self):
        # Redirect the shared paths into a temp dir so we don't clobber
        # whatever the real lager service may have written there.
        self._tmpdir = tempfile.mkdtemp()
        self._addCleanup_remove(self._tmpdir)
        self._patched = {}
        for suffix, real in list(debug_net._SHARED_PATH_FOR_SUFFIX.items()):
            self._patched[suffix] = real
            debug_net._SHARED_PATH_FOR_SUFFIX[suffix] = os.path.join(
                self._tmpdir, f'lager_user{suffix}',
            )

    def _addCleanup_remove(self, path):
        import shutil
        self.addCleanup(lambda: shutil.rmtree(path, ignore_errors=True))

    def tearDown(self):
        for suffix, real in self._patched.items():
            debug_net._SHARED_PATH_FOR_SUFFIX[suffix] = real

    def _b64(self, blob):
        return base64.b64encode(blob).decode('ascii')

    # ---- happy path ---------------------------------------------------------

    def test_openocd_config_b64_writes_shared_cfg(self):
        cfg_bytes = b'adapter driver ftdi\ntransport select swd\n'
        net = {'openocd_config': self._b64(cfg_bytes)}
        path = debug_net.materialise_user_script(
            net, explicit_key='openocd_config_path',
            b64_key='openocd_config', suffix='.cfg',
        )
        self.assertTrue(path.endswith('lager_user.cfg'))
        with open(path, 'rb') as f:
            self.assertEqual(f.read(), cfg_bytes)

    def test_jlink_script_b64_writes_shared_script(self):
        # J-Link scripts are usually short ASCII; bytes round-trip is what
        # matters, not the file format.
        script_bytes = b'/* JLink script */\nh\n'
        net = {'jlink_script': self._b64(script_bytes)}
        path = debug_net.materialise_user_script(
            net, explicit_key='jlink_script_path',
            b64_key='jlink_script', suffix='.JLinkScript',
        )
        self.assertTrue(path.endswith('lager_user.JLinkScript'))
        with open(path, 'rb') as f:
            self.assertEqual(f.read(), script_bytes)

    # ---- explicit *_path field wins ----------------------------------------

    def test_explicit_path_wins_over_b64(self):
        # When both are set, explicit *_path takes precedence and we don't
        # rewrite the shared file (no clobbering of out-of-band content).
        existing = os.path.join(self._tmpdir, 'preexisting.cfg')
        with open(existing, 'wb') as f:
            f.write(b'# user-supplied cfg already on disk\n')
        net = {
            'openocd_config_path': existing,
            'openocd_config': self._b64(b'<would be ignored>'),
        }
        path = debug_net.materialise_user_script(
            net, explicit_key='openocd_config_path',
            b64_key='openocd_config', suffix='.cfg',
        )
        self.assertEqual(path, existing)
        # The shared cfg path should not have been touched.
        self.assertFalse(os.path.exists(debug_net._SHARED_PATH_FOR_SUFFIX['.cfg']))

    def test_explicit_path_missing_falls_back_to_b64(self):
        net = {
            'openocd_config_path': '/nonexistent/path.cfg',
            'openocd_config': self._b64(b'fallback cfg\n'),
        }
        path = debug_net.materialise_user_script(
            net, explicit_key='openocd_config_path',
            b64_key='openocd_config', suffix='.cfg',
        )
        self.assertEqual(path, debug_net._SHARED_PATH_FOR_SUFFIX['.cfg'])

    # ---- absent / malformed ------------------------------------------------

    def test_no_user_cfg_returns_none(self):
        for net in [{}, {'unrelated': 'field'}, None]:
            with self.subTest(net=net):
                self.assertIsNone(debug_net.materialise_user_script(
                    net, explicit_key='openocd_config_path',
                    b64_key='openocd_config', suffix='.cfg',
                ))

    def test_empty_b64_blob_returns_none(self):
        net = {'openocd_config': ''}
        self.assertIsNone(debug_net.materialise_user_script(
            net, explicit_key='openocd_config_path',
            b64_key='openocd_config', suffix='.cfg',
        ))


class AllocateProbeSlotTests(unittest.TestCase):
    """``allocate_probe_slot`` — share the slot pool with the HTTP service."""

    class _FakeCache:
        def __init__(self, nets):
            self._nets = nets
        def get_nets(self):
            return self._nets

    def _cache_with(self, nets):
        cache = self._FakeCache(nets)
        return lambda: cache

    @staticmethod
    def _parse_serial(addr):
        # Mirror the real probes.parse_probe_serial just enough for the test:
        # the serial is the 4th `::` segment of a VISA address.
        if not isinstance(addr, str):
            return None
        parts = addr.split('::')
        if len(parts) < 5:
            return None
        return parts[3] or None

    @staticmethod
    def _compute_slot(serial, all_serials):
        # Mirror probes.compute_slot's contract: sorted index, or 0 if absent.
        if serial is None:
            return 0
        for i, s in enumerate(sorted(s for s in all_serials if s)):
            if s == serial:
                return i
        return 0

    # ---- serial-less / cache-less paths ------------------------------------

    def test_no_serial_returns_slot_zero(self):
        slot = debug_net.allocate_probe_slot(
            None,
            get_nets_cache_fn=self._cache_with([]),
            parse_probe_serial_fn=self._parse_serial,
            compute_slot_fn=self._compute_slot,
        )
        self.assertEqual(slot, 0)

    def test_serial_with_empty_cache_returns_zero(self):
        # ``compute_slot`` returns 0 when *serial* isn't in the list.
        slot = debug_net.allocate_probe_slot(
            'SOMEPROBE',
            get_nets_cache_fn=self._cache_with([]),
            parse_probe_serial_fn=self._parse_serial,
            compute_slot_fn=self._compute_slot,
        )
        self.assertEqual(slot, 0)

    # ---- single-probe and multi-probe ordering -----------------------------

    def test_single_probe_resolves_to_slot_zero(self):
        nets = [{
            'role': 'debug',
            'address': 'USB0::0x1366::0x0101::000051014439::INSTR',
        }]
        slot = debug_net.allocate_probe_slot(
            '000051014439',
            get_nets_cache_fn=self._cache_with(nets),
            parse_probe_serial_fn=self._parse_serial,
            compute_slot_fn=self._compute_slot,
        )
        self.assertEqual(slot, 0)

    def test_multi_probe_gets_sorted_index(self):
        nets = [
            {'role': 'debug', 'address': 'USB0::0x1366::0x0101::AAA::INSTR'},
            {'role': 'debug', 'address': 'USB0::0x1366::0x0101::CCC::INSTR'},
            {'role': 'debug', 'address': 'USB0::0x1366::0x0101::BBB::INSTR'},
            # Non-debug nets must be skipped during enumeration.
            {'role': 'supply', 'address': 'USB0::0x0000::0x0000::IGNORE::INSTR'},
        ]
        # Sorted order is AAA, BBB, CCC -> slots 0, 1, 2.
        self.assertEqual(0, debug_net.allocate_probe_slot(
            'AAA', get_nets_cache_fn=self._cache_with(nets),
            parse_probe_serial_fn=self._parse_serial,
            compute_slot_fn=self._compute_slot,
        ))
        self.assertEqual(1, debug_net.allocate_probe_slot(
            'BBB', get_nets_cache_fn=self._cache_with(nets),
            parse_probe_serial_fn=self._parse_serial,
            compute_slot_fn=self._compute_slot,
        ))
        self.assertEqual(2, debug_net.allocate_probe_slot(
            'CCC', get_nets_cache_fn=self._cache_with(nets),
            parse_probe_serial_fn=self._parse_serial,
            compute_slot_fn=self._compute_slot,
        ))

    # ---- defensive fallbacks ------------------------------------------------

    def test_cache_explosion_falls_back_to_zero(self):
        def boom():
            raise RuntimeError('cache on fire')
        slot = debug_net.allocate_probe_slot(
            'SOMEPROBE',
            get_nets_cache_fn=boom,
            parse_probe_serial_fn=self._parse_serial,
            compute_slot_fn=self._compute_slot,
        )
        self.assertEqual(slot, 0)

    def test_unparseable_addresses_are_skipped(self):
        nets = [
            {'role': 'debug', 'address': 'garbage'},
            {'role': 'debug', 'address': 'USB0::0x1366::0x0101::REAL::INSTR'},
        ]
        slot = debug_net.allocate_probe_slot(
            'REAL',
            get_nets_cache_fn=self._cache_with(nets),
            parse_probe_serial_fn=self._parse_serial,
            compute_slot_fn=self._compute_slot,
        )
        self.assertEqual(slot, 0)


class OpenocdSpeedLadderTests(unittest.TestCase):
    """``openocd_speed_ladder`` — speed-fallback parity with J-Link."""

    def test_adaptive_walks_full_ladder(self):
        # ``adaptive`` first so adaptive-capable adapters still get the
        # original behaviour; then descending fixed speeds.
        self.assertEqual(
            debug_net.openocd_speed_ladder('adaptive'),
            ['adaptive', '4000', '1000', '500', '100'],
        )

    def test_high_speed_walks_descending(self):
        self.assertEqual(
            debug_net.openocd_speed_ladder(4000),
            ['4000', '1000', '500', '100'],
        )
        self.assertEqual(
            debug_net.openocd_speed_ladder('2000'),
            ['2000', '1000', '500', '100'],
        )

    def test_medium_speed_skips_lower_tiers(self):
        self.assertEqual(
            debug_net.openocd_speed_ladder(750),
            ['750', '500', '100'],
        )

    def test_low_speed_drops_to_100_only(self):
        self.assertEqual(
            debug_net.openocd_speed_ladder(200),
            ['200', '100'],
        )

    def test_already_slow_no_fallback(self):
        self.assertEqual(debug_net.openocd_speed_ladder(100), ['100'])
        self.assertEqual(debug_net.openocd_speed_ladder(50), ['50'])

    def test_requested_in_ladder_dedupes(self):
        # 500 appears in the standard descent — should only show up once.
        self.assertEqual(
            debug_net.openocd_speed_ladder(500),
            ['500', '100'],
        )
        self.assertEqual(
            debug_net.openocd_speed_ladder(1000),
            ['1000', '500', '100'],
        )

    def test_garbage_input_returns_single_attempt(self):
        # Don't blow up on opaque user input — single-element list lets
        # the connect path still surface the underlying OpenOCD error.
        self.assertEqual(
            debug_net.openocd_speed_ladder('garbage'),
            ['garbage'],
        )
        self.assertEqual(debug_net.openocd_speed_ladder(None), [None])


if __name__ == '__main__':
    unittest.main()
