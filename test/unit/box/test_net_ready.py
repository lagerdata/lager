# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for box/lager/util/net_ready.py.

The window this covers is a sequence, not an event: an instrument that loses
power disappears, re-enumerates, restarts the box's hardware service via the
USB hotplug, and only then answers. Each stage raises something different, and
the point of the helper is that the caller does not have to know which.

No sleeping in real time -- the clock and sleep are injected via monkeypatch so
the deadline arithmetic is pinned exactly rather than approximately.
"""

import importlib.util
import os
import sys
import types
import unittest
from unittest.mock import patch

_created_pkg_stubs = []


def _load_real(module_name, relpath):
    box_root = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "box")
    )
    for pkg in ("lager", "lager.util"):
        if pkg not in sys.modules:
            mod = types.ModuleType(pkg)
            mod.__path__ = []
            sys.modules[pkg] = mod
            _created_pkg_stubs.append(pkg)
    if module_name in sys.modules and getattr(sys.modules[module_name], "__file__", None):
        return sys.modules[module_name]
    path = os.path.join(box_root, relpath)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


net_ready = _load_real("lager.util.net_ready", "lager/util/net_ready.py")

# Same reasoning as test_acroname_driver: a pathless `lager` left in
# sys.modules poisons every later real `import lager.*` in this process.
for _pkg in _created_pkg_stubs:
    sys.modules.pop(_pkg, None)


class _FakeClock:
    """Monotonic clock that only advances when something sleeps."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class _Net:
    """A net whose probe fails `fail_times` times, then succeeds."""

    def __init__(self, fail_times, exc=RuntimeError("not ready")):
        self.fail_times = fail_times
        self.exc = exc
        self.calls = 0

    def voltage(self):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.exc
        return 5.0


class NetReadyTests(unittest.TestCase):
    def setUp(self):
        self.clock = _FakeClock()
        self.get_calls = []

    def _run(self, net, **kwargs):
        """Drive wait_for_net against `net` with the fake clock."""
        fake_lager = types.ModuleType("lager")

        def _get(name, type=None):  # noqa: A002 -- mirrors Net.get's signature
            self.get_calls.append((name, type))
            if isinstance(net, Exception):
                raise net
            return net

        fake_lager.Net = types.SimpleNamespace(get=_get)
        with patch.dict(sys.modules, {"lager": fake_lager}), \
                patch.object(net_ready.time, "monotonic", self.clock), \
                patch.object(net_ready.time, "sleep", self.clock.sleep):
            return net_ready.wait_for_net("supply3", **kwargs)

    def test_a_net_that_answers_immediately_returns_true(self):
        net = _Net(fail_times=0)
        self.assertTrue(self._run(net))
        self.assertEqual(1, net.calls)

    def test_it_keeps_polling_until_the_net_answers(self):
        net = _Net(fail_times=3)
        self.assertTrue(self._run(net, interval=5.0, timeout=180.0))
        self.assertEqual(4, net.calls)
        self.assertEqual(1015.0, self.clock.now, "should have slept 3 intervals")

    def test_it_returns_false_rather_than_raising_when_the_deadline_passes(self):
        net = _Net(fail_times=10_000)
        self.assertFalse(self._run(net, interval=5.0, timeout=20.0))

    def test_the_sleep_never_overshoots_the_deadline(self):
        """A long interval against a short timeout must not sleep past it."""
        net = _Net(fail_times=10_000)
        self.assertFalse(self._run(net, interval=60.0, timeout=10.0))
        self.assertLessEqual(
            self.clock.now, 1010.0,
            "slept past the deadline; a caller's timeout would not be honoured",
        )

    def test_a_failure_to_resolve_the_net_is_retried_too(self):
        """`Net.get` itself raises while the hardware service is restarting."""
        net = _Net(fail_times=0)
        calls = {"n": 0}
        fake_lager = types.ModuleType("lager")

        def _get(name, type=None):  # noqa: A002
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionError("service restarting")
            return net

        fake_lager.Net = types.SimpleNamespace(get=_get)
        with patch.dict(sys.modules, {"lager": fake_lager}), \
                patch.object(net_ready.time, "monotonic", self.clock), \
                patch.object(net_ready.time, "sleep", self.clock.sleep):
            self.assertTrue(net_ready.wait_for_net("supply3", interval=1.0))
        self.assertEqual(2, calls["n"])

    def test_on_wait_is_told_about_each_failure_and_cannot_break_the_wait(self):
        seen = []

        def _noisy(attempt, elapsed, exc):
            seen.append((attempt, type(exc).__name__))
            raise ValueError("a progress callback must not be fatal")

        net = _Net(fail_times=2)
        self.assertTrue(self._run(net, interval=1.0, on_wait=_noisy))
        self.assertEqual([(1, "RuntimeError"), (2, "RuntimeError")], seen)

    def test_a_custom_probe_decides_what_counts_as_ready(self):
        net = _Net(fail_times=0)
        probed = []
        self.assertTrue(self._run(net, probe=lambda n: probed.append(n)))
        self.assertEqual([net], probed)
        self.assertEqual(0, net.calls, "custom probe should replace the default")

    def test_the_default_probe_prefers_a_call_that_touches_the_instrument(self):
        """`get_config` is served from the net record and proves nothing."""
        class _ConfigOnly:
            def __init__(self):
                self.config_calls = 0

            def get_config(self):
                self.config_calls += 1
                return {}

        class _WithState(_ConfigOnly):
            def __init__(self):
                super().__init__()
                self.state_calls = 0

            def state(self):
                self.state_calls += 1
                return True

        with_state = _WithState()
        net_ready._default_probe(with_state)
        self.assertEqual(1, with_state.state_calls)
        self.assertEqual(0, with_state.config_calls,
                         "should not settle for get_config when state() exists")

        # A net with nothing better falls back rather than failing.
        config_only = _ConfigOnly()
        net_ready._default_probe(config_only)
        self.assertEqual(1, config_only.config_calls)

    def test_a_net_with_no_probe_method_says_so(self):
        with self.assertRaises(AttributeError):
            net_ready._default_probe(object())

    def test_the_net_type_is_passed_through_when_given(self):
        net = _Net(fail_times=0)
        self._run(net, net_type="PowerSupply")
        self.assertEqual([("supply3", "PowerSupply")], self.get_calls)


if __name__ == "__main__":
    unittest.main()
