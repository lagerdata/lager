# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""The HTTP debug service and the Python Net API must select the same flash
backend for the same target.

The regression this pins: a DA1469x behind an OpenOCD probe, where mainline
OpenOCD has no QSPI driver, so lager drives a RAM-resident flash_loader. The
service (`lager debug <net> flash`) had that special case; the Net API
(`dbg.flash()`) did not. A box updated from a branch carrying the Net API
fix to a release without it kept flashing perfectly by hand and failed only
under automation -- and `erase()` "succeeding" (it returned nothing after
touching nothing) followed by a failed `flash()` left the DUT with no
firmware. The two paths having different capabilities was the bug, and a
second copy of the decision would let them drift again.

So both route through one module, ``lager.debug.openocd_flash``, and this
file holds two guards against a private copy coming back:

1. Behavioural parity. `/debug/flash` + `/debug/erase` (service.py) and
   `DebugNet.flash()` + `.erase()` (nets/debug_net.py) are driven for a
   DA1469x and for an nRF52 through the REAL dispatch, with the loader
   faked at its module seam and the RPC faked, and must produce the same
   dispatch record -- and the same failure text when the loader fails.

2. A source scan. Under box/lager, the generic OpenOCD flash commands
   (``program``, ``flash_erase_all``, ``flash_erase_range``) and the loader
   generators (``flash_image``, ``erase_range``) are called from exactly one
   file: openocd_flash.py. A caller that reaches past the dispatch -- in
   either direction -- fails here before it can ship.

Both real modules import here: service.py the way test_debug_erase_verdict.py
imports it, debug_net.py from the real package (conftest imports it).
"""

import ast
import base64
import contextlib
import os
import pathlib
import sys
import types
import unittest
from unittest.mock import MagicMock, patch


def _make_module(name):
    mod = types.ModuleType(name)
    mod.__getattr__ = lambda attr: MagicMock()  # type: ignore[method-assign]
    mod.__path__ = []
    return mod


def _stub(dotted):
    parts = dotted.split('.')
    for i in range(1, len(parts) + 1):
        key = '.'.join(parts[:i])
        if key not in sys.modules:
            sys.modules[key] = _make_module(key)


# service.py pulls in the hardware driver stack transitively; same
# `setdefault`-style stubs as test_debug_erase_verdict.py, so a real
# dependency that IS installed keeps winning.
for _dep in ['pyvisa', 'pyvisa.constants', 'usb', 'usb.util', 'usb.core', 'pigpio',
             'labjack', 'labjack.ljm', 'nidaqmx', 'bleak', 'serial',
             'serial.tools', 'serial.tools.list_ports',
             'pygdbmi', 'pygdbmi.gdbcontroller', 'pygdbmi.constants']:
    _stub(_dep)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
BOX_DIR = REPO_ROOT / 'box'
sys.path.insert(0, str(BOX_DIR))

from lager.debug import openocd_flash, service  # noqa: E402
from lager.debug.da1469x_loader import DEFAULT_ERASE_LENGTH, Da1469xLoaderError  # noqa: E402
from lager.debug.probes import BACKEND_OPENOCD  # noqa: E402
from lager.nets import debug_net  # noqa: E402

DA1469X = 'DA14695'
OTHER = 'NRF52840_XXAA'
XIP = 0x16000000
IMAGE = b'\x00\x01\x02\x03'


class _Rpc:
    """Records the generic commands and how it was built. No socket."""

    def __init__(self):
        self.built_with = None
        self.calls = []

    def program(self, file_path, verify=True, reset_after=True, address=None):
        # The path is a per-call temp file on the service side, so the
        # record leaves it out; everything else must match exactly.
        self.calls.append(('program', address, verify, reset_after))
        return 'programmed'

    def flash_erase_all(self):
        self.calls.append(('flash_erase_all',))
        return 'erased'


class _Loader:
    """Fake ``flash_image`` / ``erase_range``, patched at the dispatch's seam."""

    def __init__(self, fail_program=False):
        self.calls = []
        self.fail_program = fail_program

    def flash_image(self, rpc, image_path, *, family, flash_id=0, offset=0):
        self.calls.append(('flash_image', family, flash_id, offset))
        yield 'Preparing DA1469x flash_loader from /stub/flash_loader.elf'
        yield f'Erasing flash_id={flash_id} offset={hex(offset)} bytes={len(IMAGE)}'
        if self.fail_program:
            raise Da1469xLoaderError('fl_cmd_status=3 (program error)')
        yield f'Programmed {len(IMAGE)} bytes successfully'

    def erase_range(self, rpc, *, family, flash_id=0, offset=0, length):
        self.calls.append(('erase_range', family, flash_id, offset, length))
        yield f'Erasing flash_id={flash_id} offset={hex(offset)} bytes={length}'
        yield f'Erased {length} bytes successfully'


class _Run:
    """What one entry point did: the loader record, the RPC record, the
    output lines it reported, and (service) the HTTP verdict / (net) the
    exception."""

    def __init__(self):
        self.rpc = _Rpc()
        self.loader = None
        self.status = None
        self.payload = None
        self.output = None
        self.error = None

    def send_json_response(self, status_code, data):
        self.status = status_code
        self.payload = data


# ---- the service side --------------------------------------------------------

def _handler(run):
    """A DebugServiceHandler recording responses instead of writing them.
    Built with __new__: BaseHTTPRequestHandler.__init__ would service a socket."""
    handler = service.DebugServiceHandler.__new__(service.DebugServiceHandler)
    handler.send_json_response = run.send_json_response
    handler.send_error_response = lambda code, message: (
        run.send_json_response(code, {'error': message, 'status': 'error'}))
    return handler


@contextlib.contextmanager
def _service_env(device, run, loader):
    def build_rpc(**kwargs):
        run.rpc.built_with = kwargs
        return run.rpc

    with patch.object(service.safety, 'check_destructive', lambda net, op: None), \
         patch.object(service, '_resolve_device_type', lambda net: device), \
         patch.object(service, 'resolve_backend', lambda net: BACKEND_OPENOCD), \
         patch.object(service, '_resolve_probe',
                      lambda net: ('FT4232H01', 0, 2331, 2332, 2333, 9090)), \
         patch.object(service, '_openocd_ports_for_slot', lambda slot: (4444, 6666)), \
         patch.object(service, 'get_openocd_status',
                      lambda serial=None: {'running': True, 'pid': 9}), \
         patch.object(service, 'OpenOcdRpc', build_rpc), \
         patch.object(openocd_flash, 'flash_image', loader.flash_image), \
         patch.object(openocd_flash, 'erase_range', loader.erase_range):
        yield


def _service_flash(device, address, **loader_kw):
    run = _Run()
    run.loader = _Loader(**loader_kw)
    with _service_env(device, run, run.loader):
        _handler(run).handle_flash({
            'net': {'name': 'SWD', 'role': 'debug'},
            'binfile': {'content': base64.b64encode(IMAGE).decode(), 'address': address},
        })
    if run.status == 200:
        run.output = run.payload['output']
    else:
        run.error = run.payload['error']
    return run


def _service_erase(device, **loader_kw):
    run = _Run()
    run.loader = _Loader(**loader_kw)
    with _service_env(device, run, run.loader):
        _handler(run).handle_erase({'net': {'name': 'SWD', 'role': 'debug'}})
    if run.status == 200:
        run.output = run.payload['output'].split('\n')
    else:
        run.error = run.payload['error']
    return run


# ---- the Net API side ----------------------------------------------------------

@contextlib.contextmanager
def _net_env(run, loader):
    def build_rpc(**kwargs):
        run.rpc.built_with = kwargs
        return run.rpc

    with patch.object(debug_net, 'get_openocd_status', lambda **kw: {'running': True, 'pid': 9}), \
         patch.object(debug_net, 'OpenOcdRpc', build_rpc), \
         patch.object(openocd_flash, 'flash_image', loader.flash_image), \
         patch.object(openocd_flash, 'erase_range', loader.erase_range):
        yield


def _net(device):
    assert debug_net._debug_available, 'expected the real DebugNet, got _NullDebug'
    return debug_net.DebugNet('SWD', {
        'channel': device, 'instrument': 'debugger', 'debug_backend': BACKEND_OPENOCD,
    })


def _net_flash(device, address, **loader_kw):
    run = _Run()
    run.loader = _Loader(**loader_kw)
    with _net_env(run, run.loader):
        try:
            run.output = _net(device).flash('/tmp/xl.img.bin', address).split('\n')
        except Da1469xLoaderError as exc:
            run.error = str(exc)
    return run


def _net_erase(device, **loader_kw):
    run = _Run()
    run.loader = _Loader(**loader_kw)
    with _net_env(run, run.loader):
        try:
            run.output = _net(device).erase().split('\n')
        except Da1469xLoaderError as exc:
            run.error = str(exc)
    return run


# ---- 1. behavioural parity ----------------------------------------------------

class FlashParityTests(unittest.TestCase):
    def test_da1469x_selects_the_loader_on_both_paths(self):
        svc, net = _service_flash(DA1469X, XIP), _net_flash(DA1469X, XIP)

        self.assertEqual(svc.loader.calls, [('flash_image', 'da1469x', 0, 0)],
                         'XIP 0x16000000 must reach the loader as flash offset 0x0')
        self.assertEqual(net.loader.calls, svc.loader.calls)
        self.assertEqual(svc.rpc.calls, [], 'the service must not fall through to program')
        self.assertEqual(net.rpc.calls, [], 'the Net API must not fall through to program')

        self.assertEqual(svc.status, 200)
        self.assertEqual(svc.payload['status'], 'flash_complete')
        self.assertEqual(svc.payload['backend'], BACKEND_OPENOCD)
        # Both report the loader's own progress, so an operator reading
        # either can see the flash_loader ran (the tell for this path).
        self.assertEqual(net.output, svc.output)
        self.assertIn('Preparing DA1469x flash_loader from /stub/flash_loader.elf', svc.output)

    def test_da1469x_nonzero_xip_address_translates_identically(self):
        svc, net = _service_flash(DA1469X, XIP + 0x8000), _net_flash(DA1469X, XIP + 0x8000)
        self.assertEqual(svc.loader.calls[0][3], 0x8000)
        self.assertEqual(net.loader.calls, svc.loader.calls)

    def test_other_device_selects_program_on_both_paths(self):
        svc, net = _service_flash(OTHER, 0x0), _net_flash(OTHER, 0x0)
        self.assertEqual(svc.rpc.calls, [('program', 0x0, True, True)])
        self.assertEqual(net.rpc.calls, svc.rpc.calls)
        self.assertEqual(svc.loader.calls, [])
        self.assertEqual(net.loader.calls, [])
        self.assertEqual(svc.status, 200)
        self.assertEqual(net.output, svc.output)

    def test_both_build_the_rpc_the_same_way(self):
        # Same flash budget, and both tell the RPC its device so the RPC
        # layer's own refusal of a generic `program` on a DA1469x can fire
        # for either caller if the dispatch is ever bypassed.
        svc, net = _service_flash(DA1469X, XIP), _net_flash(DA1469X, XIP)
        self.assertEqual(svc.rpc.built_with['timeout'], openocd_flash.FLASH_RPC_TIMEOUT_S)
        self.assertEqual(net.rpc.built_with['timeout'], svc.rpc.built_with['timeout'])
        self.assertEqual(svc.rpc.built_with['device'], DA1469X)
        self.assertEqual(net.rpc.built_with['device'], DA1469X)

    def test_loader_failure_reads_the_same_on_both_paths(self):
        # No raw startup.tcl traceback on either side: the failed step is
        # named, and a flash that died after its erase says the board may
        # be blank -- the exact sequence that left the bench with no
        # firmware.
        svc = _service_flash(DA1469X, XIP, fail_program=True)
        net = _net_flash(DA1469X, XIP, fail_program=True)
        self.assertEqual(svc.status, 500)
        self.assertEqual(net.error, svc.error)
        self.assertIn('DA1469x flash_loader flash failed', svc.error)
        self.assertIn('fl_cmd_status=3', svc.error)
        self.assertIn('blank', svc.error)


class EraseParityTests(unittest.TestCase):
    def test_da1469x_selects_the_loader_range_erase_on_both_paths(self):
        svc, net = _service_erase(DA1469X), _net_erase(DA1469X)
        self.assertEqual(svc.loader.calls,
                         [('erase_range', 'da1469x', 0, 0, DEFAULT_ERASE_LENGTH)])
        self.assertEqual(net.loader.calls, svc.loader.calls)
        self.assertEqual(svc.rpc.calls, [])
        self.assertEqual(net.rpc.calls, [])
        self.assertEqual(svc.status, 200)
        self.assertEqual(svc.payload['status'], 'erase_complete')
        # Neither reports an empty success: the caller can tell the erase
        # ran and how much it covered.
        self.assertEqual(net.output, svc.output)
        self.assertIn(f'Erased {DEFAULT_ERASE_LENGTH} bytes successfully', svc.output)

    def test_other_device_erases_every_bank_on_both_paths(self):
        svc, net = _service_erase(OTHER), _net_erase(OTHER)
        self.assertEqual(svc.rpc.calls, [('flash_erase_all',)])
        self.assertEqual(net.rpc.calls, svc.rpc.calls)
        self.assertEqual(svc.loader.calls, [])
        self.assertEqual(net.loader.calls, [])
        self.assertEqual(net.output, svc.output)

    def test_both_build_the_rpc_the_same_way(self):
        svc, net = _service_erase(DA1469X), _net_erase(DA1469X)
        self.assertEqual(svc.rpc.built_with['timeout'], openocd_flash.ERASE_RPC_TIMEOUT_S)
        self.assertEqual(net.rpc.built_with['timeout'], svc.rpc.built_with['timeout'])
        self.assertEqual(svc.rpc.built_with['device'], DA1469X)
        self.assertEqual(net.rpc.built_with['device'], DA1469X)


# ---- 2. the source scan --------------------------------------------------------

BOX_LAGER = BOX_DIR / 'lager'
DISPATCH = BOX_LAGER / 'debug' / 'openocd_flash.py'

#: OpenOcdRpc's generic flash commands -- none of which can reach a DA1469x.
GENERIC_FLASH_CALLS = frozenset({'program', 'flash_erase_all', 'flash_erase_range'})
#: da1469x_loader's generators -- the only way to flash one.
LOADER_CALLS = frozenset({'flash_image', 'erase_range'})
FLASH_CALLS = GENERIC_FLASH_CALLS | LOADER_CALLS


def _callee(node):
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _flash_calls(path):
    """(line, callee) for every call of a flash entry point in *path*.

    An AST walk, not a regex: docstrings and comments in these files talk
    about ``program`` and ``flash_image`` constantly, and only a call is a
    call.
    """
    tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _callee(node)
            if name in FLASH_CALLS:
                yield node.lineno, name


def _offenders():
    found = []
    for path in sorted(BOX_LAGER.rglob('*.py')):
        if path == DISPATCH:
            continue
        for lineno, name in _flash_calls(path):
            found.append((f'{path.relative_to(REPO_ROOT)}:{lineno}', name))
    return found


class OneDispatchInTheTree(unittest.TestCase):
    def test_only_openocd_flash_calls_the_flash_entry_points(self):
        self.assertEqual(
            _offenders(), [],
            '\n\nThese call an OpenOCD flash entry point directly. Every flash '
            'and erase of an OpenOCD-backed target goes through '
            'lager.debug.openocd_flash.flash_target / erase_target, which is '
            'where the DA1469x flash_loader decision lives; a second copy of '
            'that decision is how the service and the Net API drifted apart.',
        )

    def test_the_dispatch_itself_calls_all_of_them(self):
        # A scanner that silently matches nothing is worse than no scanner.
        names = {name for _, name in _flash_calls(DISPATCH)}
        self.assertEqual(names, {'program', 'flash_erase_all', 'flash_image', 'erase_range'})

    def test_the_scanner_sees_attribute_and_name_calls(self):
        src = 'rpc.program(p)\nx = self._rpc().flash_erase_all()\nfor l in flash_image(r, p): pass\n'
        tmp = pathlib.Path(os.path.join(os.environ.get('TMPDIR', '/tmp'), 'scan_probe.py'))
        tmp.write_text(src)
        try:
            self.assertEqual(
                [name for _, name in _flash_calls(tmp)],
                ['program', 'flash_erase_all', 'flash_image'],
            )
        finally:
            tmp.unlink()

    def test_both_callers_route_through_the_dispatch(self):
        # The behavioural tests above prove it; this names the seam so a
        # refactor that keeps the behaviour but moves the import is still
        # visible here.
        for rel in ('debug/service.py', 'nets/debug_net.py'):
            with self.subTest(file=rel):
                tree = ast.parse((BOX_LAGER / rel).read_text(encoding='utf-8'))
                called = {_callee(n) for n in ast.walk(tree) if isinstance(n, ast.Call)}
                self.assertTrue({'flash_target', 'erase_target'} <= called)


if __name__ == '__main__':
    unittest.main()
