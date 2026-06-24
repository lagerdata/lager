# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the hardened ``stop_jlink_gdbserver`` (GAP 1a).

The reap now kills the *pidfile PID's process group* (``os.killpg``) and reaps
zombies, instead of relying solely on a ``pkill -f 'JLinkGDBServerCLExe.*USB=<serial>'``
pattern that silently missed whenever the running cmdline's ``-select USB=<serial>``
did not match the serial we were asked to stop (the flash-path reap in api.py).

We load ``gdbserver`` standalone under a private package name (its only relative
import is the light ``.probes`` helper module) so the test runs in isolation
without dragging in ``lager/__init__`` and its hardware-only transitive imports.
Everything risky (``os.killpg``/``os.kill``/``os.waitpid``/``subprocess.run``)
is mocked, so no real process is ever signalled.
"""

import importlib.util
import os
import signal
import sys
import tempfile
import types
import unittest
from unittest.mock import patch, MagicMock

HERE = os.path.dirname(__file__)
BOX = os.path.normpath(os.path.join(HERE, "..", "..", "..", "box"))
DEBUG_DIR = os.path.join(BOX, "lager", "debug")

PKG = "gdbserver_killpg_ut_pkg"
DEBUG_PKG = f"{PKG}.debug"
_INSTALLED = []


def _install(name, mod):
    sys.modules[name] = mod
    _INSTALLED.append(name)


def _load_gdbserver():
    pkg = types.ModuleType(PKG)
    pkg.__path__ = []
    _install(PKG, pkg)

    dbg = types.ModuleType(DEBUG_PKG)
    dbg.__path__ = [DEBUG_DIR]
    _install(DEBUG_PKG, dbg)

    for name in ("probes", "gdbserver"):
        spec = importlib.util.spec_from_file_location(
            f"{DEBUG_PKG}.{name}", os.path.join(DEBUG_DIR, f"{name}.py")
        )
        mod = importlib.util.module_from_spec(spec)
        mod.__package__ = DEBUG_PKG
        _install(f"{DEBUG_PKG}.{name}", mod)
        spec.loader.exec_module(mod)
    return sys.modules[f"{DEBUG_PKG}.gdbserver"]


gdbserver = _load_gdbserver()


def tearDownModule():
    for key in _INSTALLED:
        sys.modules.pop(key, None)


class _PidfileSandbox:
    """Redirect the per-serial pidfile helper into a tmp dir + seed a PID."""

    def __init__(self, pid=None, serial="SERIAL"):
        self.pid = pid
        self.serial = serial

    def __enter__(self):
        self.tmpdir = tempfile.mkdtemp(prefix="lager_gdb_killpg_")
        self._orig = gdbserver.jlink_gdbserver_pidfile
        gdbserver.jlink_gdbserver_pidfile = lambda s: os.path.join(
            self.tmpdir, f'jlink_gdbserver_{s or "legacy"}.pid'
        )
        if self.pid is not None:
            with open(gdbserver.jlink_gdbserver_pidfile(self.serial), "w") as f:
                f.write(str(self.pid))
        return self

    def path(self, serial="SERIAL"):
        return gdbserver.jlink_gdbserver_pidfile(serial)

    def __exit__(self, *exc):
        gdbserver.jlink_gdbserver_pidfile = self._orig
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)


def _kill_sig0_dead(pid, sig):
    """os.kill stand-in: signal-0 liveness checks raise (process gone)."""
    if sig == 0:
        raise ProcessLookupError()
    return None


class StopKillpgTests(unittest.TestCase):
    PID = 4242

    @patch("time.sleep", lambda *a, **k: None)
    def test_killpg_terminates_group_and_reaps(self):
        with _PidfileSandbox(self.PID) as sb, \
                patch("os.killpg") as killpg, \
                patch("os.kill", side_effect=_kill_sig0_dead), \
                patch("os.waitpid", return_value=(0, 0)) as waitpid, \
                patch("subprocess.run", return_value=MagicMock(returncode=1)), \
                patch.object(gdbserver, "_proc_cmdline", return_value=""):
            gdbserver.stop_jlink_gdbserver(serial="SERIAL")

        killpg.assert_any_call(self.PID, signal.SIGTERM)
        waitpid.assert_called_with(-self.PID, os.WNOHANG)
        self.assertFalse(os.path.exists(sb.path()), "pidfile should be removed")

    @patch("time.sleep", lambda *a, **k: None)
    def test_serial_mismatch_still_killed_via_pidfile_pgid(self):
        # Running cmdline advertises a DIFFERENT serial than the one we stop:
        # the old serial-scoped pkill would miss; killpg by pidfile PID does not.
        mismatch = "JLinkGDBServerCLExe -select USB=OTHERSERIAL -port 2331"
        with _PidfileSandbox(self.PID, serial="WANTEDSERIAL"), \
                patch("os.killpg") as killpg, \
                patch("os.kill", side_effect=_kill_sig0_dead), \
                patch("os.waitpid", return_value=(0, 0)), \
                patch("subprocess.run", return_value=MagicMock(returncode=1)), \
                patch.object(gdbserver, "_proc_cmdline", return_value=mismatch):
            gdbserver.stop_jlink_gdbserver(serial="WANTEDSERIAL")

        killpg.assert_any_call(self.PID, signal.SIGTERM)

    @patch("time.sleep", lambda *a, **k: None)
    def test_escalates_to_sigkill_when_still_alive(self):
        with _PidfileSandbox(self.PID), \
                patch("os.killpg") as killpg, \
                patch("os.kill", return_value=None), \
                patch("os.waitpid", return_value=(0, 0)), \
                patch("subprocess.run", return_value=MagicMock(returncode=1)), \
                patch.object(gdbserver, "_proc_cmdline", return_value=""):
            gdbserver.stop_jlink_gdbserver(serial="SERIAL")

        killpg.assert_any_call(self.PID, signal.SIGTERM)
        killpg.assert_any_call(self.PID, signal.SIGKILL)

    @patch("time.sleep", lambda *a, **k: None)
    def test_recycled_pid_is_not_killpg_d(self):
        # pidfile PID now belongs to an unrelated process -> never killpg it.
        other = "/usr/bin/python3 some_unrelated_daemon.py"
        with _PidfileSandbox(self.PID), \
                patch("os.killpg") as killpg, \
                patch("os.kill", side_effect=_kill_sig0_dead), \
                patch("os.waitpid", return_value=(0, 0)), \
                patch("subprocess.run", return_value=MagicMock(returncode=1)) as run, \
                patch.object(gdbserver, "_proc_cmdline", return_value=other):
            gdbserver.stop_jlink_gdbserver(serial="SERIAL")

        killpg.assert_not_called()
        # fell back to the serial-scoped pgrep/pkill sweep instead
        self.assertTrue(any("pgrep" in c.args[0] for c in run.call_args_list))

    @patch("time.sleep", lambda *a, **k: None)
    def test_no_pidfile_orphan_pkill_when_pgrep_matches(self):
        with _PidfileSandbox(pid=None), \
                patch("os.killpg") as killpg, \
                patch("subprocess.run", return_value=MagicMock(returncode=0)) as run:
            gdbserver.stop_jlink_gdbserver(serial="SERIAL")

        killpg.assert_not_called()
        cmds = [c.args[0] for c in run.call_args_list]
        self.assertTrue(any(c[:2] == ["pkill", "-TERM"] for c in cmds))
        self.assertTrue(any(c[:2] == ["pkill", "-KILL"] for c in cmds))

    @patch("time.sleep", lambda *a, **k: None)
    def test_no_pidfile_no_match_is_noop(self):
        with _PidfileSandbox(pid=None), \
                patch("os.killpg") as killpg, \
                patch("subprocess.run", return_value=MagicMock(returncode=1)) as run:
            gdbserver.stop_jlink_gdbserver(serial="SERIAL")

        killpg.assert_not_called()
        cmds = [c.args[0] for c in run.call_args_list]
        self.assertEqual(len(cmds), 1, "only the pgrep probe should run")
        self.assertEqual(cmds[0][0], "pgrep")


if __name__ == "__main__":
    unittest.main()
