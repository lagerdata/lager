# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for ``lager.debug.api.detect_and_configure_rtt`` — the core must
not be left halted by the RTT control-block RAM scan.

The RAM scan issues GDB ``-data-read-memory-bytes`` reads. In GDB's all-stop
mode (which ``get_controller`` falls back to when JLinkGDBServer rejects
non-stop) those reads implicitly halt the core, and nothing else resumes it —
so ``gdbserver --rtt`` would leave the device halted (a regression vs the
non-stop path). detect_and_configure_rtt must therefore issue ``monitor go``
after the scan whenever the controller is in the all-stop fallback, and must
NOT touch a non-stop controller (whose core was never halted).

Hermetic: ``api.py``'s heavy siblings are stubbed in ``sys.modules`` (mirroring
test_debug_rtt_reconnect.py) so the module imports without a real probe.
"""

import importlib.util
import os
import sys
import types
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
BOX_DIR = os.path.join(REPO_ROOT, "box")
API_PATH = os.path.join(BOX_DIR, "lager", "debug", "api.py")

# Load api.py under a PRIVATE package name so we never shadow the real
# ``lager.debug.*`` modules in sys.modules. api.py only uses relative imports,
# which resolve against this stub package.
PKG = "rttdetectstub.debug"
_INSTALLED = []


def _install(dotted, mod):
    sys.modules[dotted] = mod
    _INSTALLED.append(dotted)


def _stub(dotted, **attrs):
    mod = types.ModuleType(dotted)
    for k, v in attrs.items():
        setattr(mod, k, v)
    _install(dotted, mod)
    return mod


def _load_api():
    root = types.ModuleType("rttdetectstub")
    root.__path__ = []
    _install("rttdetectstub", root)
    pkg = types.ModuleType(PKG)
    pkg.__path__ = []
    _install(PKG, pkg)

    _stub(f"{PKG}.jlink", JLink=object, commander=lambda *a, **k: None)
    _stub(
        f"{PKG}.mappings",
        get_jlink_status=lambda **k: {"running": True, "pid": 1},
        readfile=lambda *a, **k: "",
        JL_LOGFILE="/tmp/jl.log",
    )
    _stub(f"{PKG}.process", stop_jlink=lambda **k: None)
    _stub(
        f"{PKG}.gdbserver",
        get_jlink_gdbserver_status=lambda **k: {"running": True, "pid": 1},
        stop_jlink_gdbserver=lambda **k: None,
        start_jlink_gdbserver=lambda **k: {},
    )
    # get_controller is overridden per-test to hand back a FakeGdb.
    _stub(
        f"{PKG}.gdb",
        get_arch=lambda *a, **k: "arm",
        reset=lambda *a, **k: None,
        read_memory=lambda *a, **k: b"",
        get_controller=lambda *a, **k: None,
    )
    _stub(
        f"{PKG}.probes",
        gdb_port_for_slot=lambda slot: 2331 + 3 * slot,
        rtt_port_for_slot=lambda slot: 9090 + 2 * slot,
        jlink_gdbserver_logfile=lambda serial=None: "/tmp/jlink_gdbserver.log",
    )

    spec = importlib.util.spec_from_file_location(f"{PKG}.api", API_PATH)
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = PKG
    _install(f"{PKG}.api", mod)
    spec.loader.exec_module(mod)
    return mod


api = _load_api()


def tearDownModule():
    for key in _INSTALLED:
        sys.modules.pop(key, None)


# "SEGGER RTT" is the control-block magic detect_and_configure_rtt scans for.
_RTT_SIG_HEX = b"SEGGER RTT".hex()


class FakeGdb:
    """Minimal stand-in for a pygdbmi controller.

    Records every command written and returns realistic MI responses for the
    RAM-scan reads and the SetRTTAddr / monitor-go calls. ``lager_non_stop``
    mirrors the flag get_controller records on a real controller.
    """

    def __init__(self, non_stop, rtt_present):
        self.lager_non_stop = non_stop
        self._rtt_present = rtt_present
        self.written = []

    def write(self, cmd, *args, **kwargs):
        self.written.append(cmd)
        if cmd.startswith("-data-read-memory-bytes"):
            # cmd == '-data-read-memory-bytes <addr> <chunk_size>'
            addr = int(cmd.split()[1], 0)
            contents = "00" * 64
            if self._rtt_present and addr == 0x20000000:
                contents = _RTT_SIG_HEX + "00" * 64
            return [{
                "type": "result",
                "message": "done",
                "payload": {"memory": [{"addr": hex(addr), "contents": contents}]},
            }]
        if cmd.startswith("monitor exec SetRTTAddr"):
            return [{"type": "console", "payload": "SetRTTAddr\n"}]
        return []


class DetectAndConfigureRttResumeTests(unittest.TestCase):
    def _run(self, *, non_stop, rtt_present):
        fake = FakeGdb(non_stop=non_stop, rtt_present=rtt_present)
        sys.modules[f"{PKG}.gdb"].get_controller = lambda *a, **k: fake
        result = api.detect_and_configure_rtt()
        return fake, result

    def test_all_stop_resumes_core_after_scan(self):
        """All-stop fallback: the scan halts the core, so detect must resume it."""
        fake, result = self._run(non_stop=False, rtt_present=True)
        self.assertTrue(result["found"])
        self.assertIn("monitor go", fake.written)

    def test_non_stop_does_not_touch_running_core(self):
        """Non-stop: the core was never halted, so detect must NOT resume it."""
        fake, result = self._run(non_stop=True, rtt_present=True)
        self.assertTrue(result["found"])
        self.assertNotIn("monitor go", fake.written)

    def test_all_stop_resumes_even_when_rtt_not_found(self):
        """The reads halt the core whether or not the block is found, so the
        resume must fire even on the not-found path."""
        fake, result = self._run(non_stop=False, rtt_present=False)
        self.assertFalse(result["found"])
        self.assertIn("monitor go", fake.written)


if __name__ == "__main__":
    unittest.main()
