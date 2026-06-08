# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the on-box defmt RTT decoding wrapper.

``lager.nets.debug_net._DefmtRtt`` wraps a raw RTT session and pipes its
bytes through ``defmt-print -e <elf>``, surfacing decoded log lines. These
tests are fully hermetic: no debug probe, no real ``defmt-print``, and no
cargo. We stub ``defmt-print`` with a tiny Python script and feed a fake RTT
session, so we exercise the threading/piping/teardown logic in isolation.

The module is imported without executing ``lager/__init__.py`` (which pulls
in requests/simplejson and other heavy deps) by registering bare package
namespaces in ``sys.modules`` first — the same trick the measurement
conftest uses.
"""

import collections
import importlib.util
import os
import stat
import sys
import threading
import types
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
BOX_DIR = os.path.join(REPO_ROOT, "box")

if BOX_DIR not in sys.path:
    sys.path.insert(0, BOX_DIR)


def _ensure_package(dotted, *parts):
    if dotted in sys.modules:
        return sys.modules[dotted]
    mod = types.ModuleType(dotted)
    mod.__path__ = [os.path.join(BOX_DIR, *parts)]
    mod.__package__ = dotted
    sys.modules[dotted] = mod
    return mod


def _load_module(dotted, filepath):
    if dotted in sys.modules:
        return sys.modules[dotted]
    spec = importlib.util.spec_from_file_location(dotted, filepath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[dotted] = mod
    spec.loader.exec_module(mod)
    return mod


# 1. Bare ``lager`` package (don't run its __init__).
_ensure_package("lager", "lager")

# 2. Stub ``lager.constants`` — debug_net's constants import only needs
#    HARDWARE_SERVICE_PORT.
if "lager.constants" not in sys.modules:
    _const = types.ModuleType("lager.constants")
    _const.HARDWARE_SERVICE_PORT = 5000
    sys.modules["lager.constants"] = _const

# 3. ``lager.nets`` package namespace, then the real constants + debug_net.
_ensure_package("lager.nets", "lager", "nets")
_load_module(
    "lager.nets.constants",
    os.path.join(BOX_DIR, "lager", "nets", "constants.py"),
)
_debug_net = _load_module(
    "lager.nets.debug_net",
    os.path.join(BOX_DIR, "lager", "nets", "debug_net.py"),
)

_DefmtRtt = _debug_net._DefmtRtt
_resolve_defmt_print = _debug_net._resolve_defmt_print


class FakeRttSession:
    """Minimal stand-in for a backend RTT session.

    Hands back queued byte chunks on ``read_some`` then ``None`` forever
    (idle line). Records context-manager entry/exit so tests can assert the
    underlying session is always torn down.
    """

    def __init__(self, chunks):
        self._chunks = collections.deque(chunks)
        self._lock = threading.Lock()
        self.entered = False
        self.exited = False

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, *exc):
        self.exited = True
        return False

    def read_some(self, timeout=1.0):
        with self._lock:
            if self._chunks:
                return self._chunks.popleft()
        return None


def _write_stub_defmt_print(tmpdir):
    """Write an executable stub that mimics ``defmt-print``.

    Reads raw bytes line-by-line from stdin and emits ``decoded:<line>`` to
    stdout, flushing each line so the reader thread sees it promptly. Ignores
    its ``-e <elf>`` args, exactly like a real invocation would accept them.
    """
    path = os.path.join(tmpdir, "stub-defmt-print")
    script = (
        f"#!{sys.executable}\n"
        "import sys\n"
        "for raw in iter(sys.stdin.buffer.readline, b''):\n"
        "    line = raw.rstrip(b'\\n')\n"
        "    if line:\n"
        "        sys.stdout.write('decoded:' + line.decode() + '\\n')\n"
        "        sys.stdout.flush()\n"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(script)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


class TestResolveDefmtPrint(unittest.TestCase):
    def test_absolute_path_must_exist(self):
        self.assertIsNone(_resolve_defmt_print("/no/such/defmt-print"))

    def test_missing_name_on_path_returns_none(self):
        self.assertIsNone(_resolve_defmt_print("definitely-not-a-real-binary-xyz"))

    def test_explicit_absolute_path_returned_when_present(self):
        self.assertEqual(_resolve_defmt_print(sys.executable), sys.executable)


class TestDefmtRttDecoding(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmpdir = self._tmp.name
        # A fake-but-present ELF so the existence check passes.
        self.elf = os.path.join(self.tmpdir, "app.elf")
        with open(self.elf, "wb") as f:
            f.write(b"\x7fELF fake")
        self.stub = _write_stub_defmt_print(self.tmpdir)

    def test_streams_decoded_lines(self):
        session = FakeRttSession([b"boot ok\n", b"temp=25\n"])
        wrapper = _DefmtRtt(
            session, self.elf, defmt_print_bin=self.stub, read_timeout=0.02,
        )
        lines = []
        with wrapper as logs:
            while len(lines) < 2:
                line = logs.read_line(timeout=3.0)
                if line is None:
                    break
                lines.append(line)
        self.assertEqual(lines, ["decoded:boot ok", "decoded:temp=25"])
        # Underlying RTT session must be entered and torn down.
        self.assertTrue(session.entered)
        self.assertTrue(session.exited)

    def test_missing_elf_raises(self):
        session = FakeRttSession([])
        wrapper = _DefmtRtt(
            session, os.path.join(self.tmpdir, "missing.elf"),
            defmt_print_bin=self.stub,
        )
        with self.assertRaises(FileNotFoundError):
            wrapper.__enter__()
        # We never entered the RTT session.
        self.assertFalse(session.entered)

    def test_missing_defmt_print_raises(self):
        session = FakeRttSession([])
        wrapper = _DefmtRtt(
            session, self.elf, defmt_print_bin="not-a-real-defmt-print-xyz",
        )
        with self.assertRaises(RuntimeError):
            wrapper.__enter__()
        self.assertFalse(session.entered)

    def test_iteration_yields_lines_until_eof(self):
        session = FakeRttSession([b"line one\n", b"line two\n", b"line three\n"])
        wrapper = _DefmtRtt(
            session, self.elf, defmt_print_bin=self.stub, read_timeout=0.02,
        )
        collected = []
        with wrapper as logs:
            for line in logs:
                collected.append(line)
                if len(collected) == 3:
                    break
        self.assertEqual(
            collected,
            ["decoded:line one", "decoded:line two", "decoded:line three"],
        )


if __name__ == "__main__":
    unittest.main()
