# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for fail-fast locking and hang recovery on the USB hub path.

A hub command hung indefinitely on a box and took every subsequent USB command
down with it. The chain was all lock-shaped:

* ``http_handlers/usb.py`` serialised every hub call behind a module-level
  ``_usb_lock`` taken with a plain ``with``. Its own comment said it existed to
  "fail fast"; it blocked forever.
* ``usb_hub/usb_net.py``'s ``hub_access`` took the per-hub in-process lock with
  no timeout at all, ahead of the cross-process flock that always had one.
* ``util/self_restart.py`` — the recovery for exactly this wedged-USB-context
  state — only fired from an ``except`` block, and a hung call never raises.

So the failure mode that most needed the recovery was the one that could never
reach it. These tests pin the three bounds that close that: a busy lock answers
503 instead of queueing, a lock that cannot be claimed raises the same type the
flock raises, and an operation that never returns produces a 504 *and* schedules
the supervisor respawn.

No hardware and no vendor SDK: the deadline is exercised with a plain
``time.sleep`` stand-in for a wedged native call. What cannot be reproduced here
is BrainStem/pykush actually hanging — that needs a wedged hub.
"""

import threading
import time
import traceback
import unittest
from unittest.mock import MagicMock, patch

from flask import Flask

from lager.automation.usb_hub import usb_net
from lager.automation.usb_hub import acroname as acroname_mod
from lager.automation.usb_hub import ykush as ykush_mod
from lager.automation.usb_hub.usb_net import HubOperationTimeout
from lager.http_handlers import usb as usb_handler
from lager.util import watchdog
from lager.util.device_lock import DeviceLockError


def _unique_key(name):
    """A lock key no other test shares. ``_local_hub_locks`` is module state
    that outlives a test, and one test deliberately leaves a lock held."""
    return f"test::{name}::{time.monotonic_ns()}"


class _HangHookIsolated(unittest.TestCase):
    """``usb_net._hang_hook`` is process-global by design (see
    ``set_hang_hook``), so a test that triggers a hang would otherwise fire
    whatever hook a previously-run test installed — including the real one,
    which schedules a self-restart. Each test starts with no hook and puts the
    previous one back."""

    def setUp(self):
        super().setUp()
        self.addCleanup(usb_net.set_hang_hook, usb_net._hang_hook)
        usb_net.set_hang_hook(None)


class RunWithDeadlineTests(_HangHookIsolated):
    """The primitive underneath both services (``util/watchdog.py``)."""

    def test_returns_value_of_completed_call(self):
        self.assertEqual(watchdog.run_with_deadline(lambda: 42, 5), 42)

    def test_reraises_worker_exception_preserving_traceback(self):
        """The re-raise must keep the worker's frames: hardware_service
        classifies failures with ``traceback.format_exc()``
        (``self_restart.looks_like_open_failure``), and a traceback that started
        at the re-raise would make every wedged-open error unrecognisable."""
        def _driver_open():
            raise RuntimeError("could not open instrument")

        try:
            watchdog.run_with_deadline(_driver_open, 5)
        except RuntimeError:
            blob = traceback.format_exc()
        else:
            self.fail("the worker's exception was not re-raised")

        self.assertIn("_driver_open", blob)
        self.assertIn("could not open instrument", blob)

    def test_timeout_raises_requested_error_type(self):
        started = threading.Event()

        def _wedged():
            started.set()
            time.sleep(30)

        with self.assertRaises(HubOperationTimeout):
            watchdog.run_with_deadline(
                _wedged, 0.2, what="wedged op",
                timeout_error=HubOperationTimeout)
        self.assertTrue(started.is_set())

    def test_timeout_does_not_wait_for_the_worker(self):
        """The whole point: the caller stops waiting. The worker cannot be
        killed, so this only holds if we abandon it."""
        start = time.monotonic()
        with self.assertRaises(watchdog.OperationTimeout):
            watchdog.run_with_deadline(lambda: time.sleep(30), 0.2)
        self.assertLess(time.monotonic() - start, 5.0)


class HubAccessLockTimeoutTests(_HangHookIsolated):
    """``hub_access``'s in-process layer, which had no timeout at all."""

    def test_in_process_lock_timeout_raises_device_lock_error(self):
        """Same type the flock's timeout raises, so ``dispatcher.states``'s
        one "hub unreadable" handler covers both layers."""
        key = _unique_key("inproc-timeout")
        held = usb_net._local_hub_lock(key)
        held.acquire()
        try:
            start = time.monotonic()
            with self.assertRaises(DeviceLockError) as caught:
                with usb_net.hub_access(key, timeout=0.2):
                    self.fail("hub_access entered while the lock was held")
            elapsed = time.monotonic() - start
        finally:
            held.release()

        self.assertLess(elapsed, 5.0, "hub_access queued instead of failing fast")
        self.assertIn("busy", str(caught.exception).lower())

    def test_lock_released_after_a_normal_operation(self):
        key = _unique_key("normal")
        with usb_net.hub_access(key, timeout=1.0):
            pass
        self.assertTrue(usb_net._local_hub_lock(key).acquire(timeout=0.1))
        usb_net._local_hub_lock(key).release()

    def test_lock_released_after_an_ordinary_error(self):
        """Only a hang keeps the lock. A driver that raises has released the
        hub, so the next caller must get in."""
        key = _unique_key("driver-error")
        with self.assertRaises(ValueError):
            with usb_net.hub_access(key, timeout=1.0):
                raise ValueError("port state error")
        self.assertTrue(usb_net._local_hub_lock(key).acquire(timeout=0.1))
        usb_net._local_hub_lock(key).release()

    def test_hang_keeps_the_in_process_lock_held(self):
        """A thread is still inside the driver, so the hub is genuinely still
        claimed; handing it to the next caller would just wedge that one too.
        The next caller gets a fast DeviceLockError instead."""
        key = _unique_key("wedged")
        with self.assertRaises(HubOperationTimeout):
            with usb_net.hub_access(key, timeout=1.0):
                raise HubOperationTimeout("hub op abandoned")

        with self.assertRaises(DeviceLockError):
            with usb_net.hub_access(key, timeout=0.2):
                self.fail("hub_access entered a hub with a wedged thread in it")

    def test_run_hub_op_expiry_leaves_the_lock_held(self):
        """End to end through the two pieces a driver actually composes."""
        key = _unique_key("run-hub-op")
        with self.assertRaises(HubOperationTimeout):
            with usb_net.hub_access(key, timeout=1.0):
                usb_net.run_hub_op(key, lambda: time.sleep(30), timeout=0.2)

        self.assertFalse(usb_net._local_hub_lock(key).acquire(timeout=0.1))


class DriverDeadlineTests(_HangHookIsolated):
    """The drivers must put their whole open→operate→close cycle under the
    deadline, not just the operate: the open is the part that hangs
    (``discoverAndConnect`` on a hub whose USB link is wedged)."""

    def test_acroname_hanging_open_raises_hub_operation_timeout(self):
        net = acroname_mod.AcronameUSBNet(
            {"address": "USB0::0x24FF::0x0013::DEADBEEF::INSTR"})
        with patch.object(acroname_mod, "HUB_OP_TIMEOUT_S", 0.2), \
                patch.object(net, "_open_hub", lambda: time.sleep(30)):
            start = time.monotonic()
            with self.assertRaises(HubOperationTimeout):
                net.enable("CHARGE", 0)
            self.assertLess(time.monotonic() - start, 5.0)

    def test_ykush_hanging_open_raises_hub_operation_timeout(self):
        net = ykush_mod.YKUSHUSBNet(
            {"address": "USB0::0x04D8::0xF11B::YKTEST01::INSTR"})
        with patch.object(ykush_mod, "HUB_OP_TIMEOUT_S", 0.2), \
                patch.object(ykush_mod, "_ensure_library", lambda: None), \
                patch.object(net, "_run_once", lambda fn: time.sleep(30)):
            start = time.monotonic()
            with self.assertRaises(HubOperationTimeout):
                net.enable("CLI_USB", 2)
            self.assertLess(time.monotonic() - start, 5.0)


class _FakeHubModule:
    """Stands in for ``lager.automation.usb_hub`` in the handler's namespace.
    Every action shares one behaviour so a test can say what the hub does
    without caring which verb reaches it."""

    def __init__(self, behaviour=None):
        self.calls = []
        self._behaviour = behaviour or (lambda netname: True)

    def _dispatch(self, netname):
        self.calls.append(netname)
        return self._behaviour(netname)

    enable = _dispatch
    disable = _dispatch
    toggle = _dispatch
    state = _dispatch


class UsbCommandFailFastTests(_HangHookIsolated):
    """``POST /usb/command`` — the endpoint a wedged hub used to take down."""

    def setUp(self):
        super().setUp()
        app = Flask(__name__)
        usb_handler.register_usb_routes(app)
        self.client = app.test_client()
        # Never let a test reach the real self-restart machinery: it ends in
        # os._exit(70), which would take the test runner with it.
        self.restart = MagicMock()
        patcher = patch.object(usb_handler, "_self_restart", self.restart)
        patcher.start()
        self.addCleanup(patcher.stop)
        addr = patch.object(
            usb_handler, "_usb_net_address",
            lambda netname: "USB0::0x04D8::0xF11B::YKTEST01::INSTR")
        addr.start()
        self.addCleanup(addr.stop)

    def _install_hub(self, behaviour):
        fake = _FakeHubModule(behaviour)
        patcher = patch.object(usb_handler, "usb_hub", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    def test_busy_lock_answers_503_without_queueing(self):
        fake = self._install_hub(lambda netname: True)
        usb_handler._usb_lock.acquire()
        try:
            with patch.object(usb_handler, "_USB_LOCK_TIMEOUT_S", 0.3):
                start = time.monotonic()
                resp = self.client.post(
                    "/usb/command", json={"netname": "usb1", "action": "state"})
                elapsed = time.monotonic() - start
        finally:
            usb_handler._usb_lock.release()

        self.assertEqual(resp.status_code, 503)
        body = resp.get_json()
        self.assertFalse(body["success"])
        self.assertTrue(body["error"].startswith("hub-busy:"), body["error"])
        self.assertLess(elapsed, 5.0, "the request queued instead of failing fast")
        # It never reached the driver — that is what "without queueing" means.
        self.assertEqual(fake.calls, [])

    def test_lock_is_released_after_a_busy_answer(self):
        """A 503 must not leak the lock; the next call has to work."""
        fake = self._install_hub(lambda netname: True)
        usb_handler._usb_lock.acquire()
        with patch.object(usb_handler, "_USB_LOCK_TIMEOUT_S", 0.2):
            self.client.post("/usb/command",
                             json={"netname": "usb1", "action": "state"})
        usb_handler._usb_lock.release()

        resp = self.client.post("/usb/command",
                                json={"netname": "usb1", "action": "state"})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertEqual(fake.calls, ["usb1"])

    def test_hang_answers_504(self):
        def _hang(netname):
            raise HubOperationTimeout("USB hub operation on usb1 did not complete")

        self._install_hub(_hang)
        resp = self.client.post("/usb/command",
                                json={"netname": "usb1", "action": "toggle"})

        self.assertEqual(resp.status_code, 504)
        body = resp.get_json()
        self.assertFalse(body["success"])
        self.assertTrue(body["error"].startswith("hub-op-timeout:"), body["error"])

    def test_registering_routes_installs_the_hang_hook(self):
        """The response to a hang belongs to the process, not to whichever
        request noticed — see ``usb_net.set_hang_hook``."""
        self.assertIs(usb_net._hang_hook, usb_handler._self_restart_on_hung_hub)

    def test_hang_hook_schedules_the_self_restart_for_the_hub(self):
        usb_handler._self_restart_on_hung_hub(
            "USB0::0x24FF::0x0013::DEADBEEF::INSTR")

        # The hang path — not the unreachable path — and it goes through the
        # scheduler so an in-flight response can be written before os._exit.
        self.restart.schedule_self_restart_for_hang.assert_called_once()
        args, kwargs = self.restart.schedule_self_restart_for_hang.call_args
        self.assertEqual(args[0], "USB0::0x24FF::0x0013::DEADBEEF::INSTR")
        self.assertEqual(kwargs["service"], "box_http_server")
        self.assertEqual(kwargs["stamp_path"],
                         usb_handler._BHS_SELF_RESTART_STAMP)
        self.restart.maybe_self_restart.assert_not_called()

    def test_a_real_hang_reaches_the_hook_from_any_caller(self):
        """End to end through the piece a driver composes, not the handler:
        a hang during a ``/nets/state`` sweep must trigger recovery too, and
        ``dispatcher.states`` swallows the exception before any handler sees
        it."""
        key = _unique_key("hook-e2e")
        with self.assertRaises(HubOperationTimeout):
            with usb_net.hub_access(key, timeout=1.0):
                usb_net.run_hub_op(key, lambda: time.sleep(30), timeout=0.2)

        self.restart.schedule_self_restart_for_hang.assert_called_once()
        self.assertEqual(
            self.restart.schedule_self_restart_for_hang.call_args.args[0], key)

    def test_hang_releases_the_global_lock(self):
        """``_usb_lock`` spans every hub, so keeping it on a hang would let one
        wedged hub block commands to a different, healthy one. The per-hub lock
        inside ``hub_access`` is the one that stays held."""
        def _hang(netname):
            raise HubOperationTimeout("wedged")

        self._install_hub(_hang)
        self.client.post("/usb/command",
                         json={"netname": "usb1", "action": "toggle"})
        self.assertTrue(usb_handler._usb_lock.acquire(timeout=0.1))
        usb_handler._usb_lock.release()

    def test_hub_lock_timeout_answers_503(self):
        """``DeviceLockError`` from either lock layer is "someone else has the
        hub", which is a retryable busy answer — not the 500 it used to fall
        through to."""
        def _locked(netname):
            raise DeviceLockError("Device at USB0::... is locked by pid 42")

        self._install_hub(_locked)
        resp = self.client.post("/usb/command",
                                json={"netname": "usb1", "action": "state"})
        self.assertEqual(resp.status_code, 503)
        self.assertTrue(resp.get_json()["error"].startswith("hub-busy:"))
        self.restart.schedule_self_restart_for_hang.assert_not_called()

    def test_successful_command_still_answers_200(self):
        """The success shape is unchanged — only new error shapes were added."""
        self._install_hub(lambda netname: False)
        resp = self.client.post("/usb/command",
                                json={"netname": "usb1", "action": "toggle"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {
            "success": True,
            "action": "toggle",
            "state": "disabled",
            "message": "USB port 'usb1' toggled → disabled",
        })


class ScheduleSelfRestartForHangTests(_HangHookIsolated):
    """``self_restart``'s new no-exception entry point."""

    def test_runs_on_a_timer_with_the_hang_wedge_label(self):
        from lager.util import self_restart

        seen = {}

        def _fake_maybe(address, context, **kwargs):
            seen["address"] = address
            seen["context"] = context
            seen.update(kwargs)

        with patch.object(self_restart, "maybe_self_restart", _fake_maybe):
            timer = self_restart.schedule_self_restart_for_hang(
                "USB0::0x24FF::0x0013::DEADBEEF::INSTR", "usb toggle usb1",
                service="box_http_server", stamp_path="/tmp/stamp-under-test",
                delay_s=0.01)
            timer.join(5)

        self.assertFalse(timer.is_alive())
        self.assertEqual(seen["context"], "usb toggle usb1")
        self.assertEqual(seen["service"], "box_http_server")
        self.assertEqual(seen["stamp_path"], "/tmp/stamp-under-test")
        # The gating is shared with the unreachable path; only the label differs.
        self.assertEqual(seen["wedge"], "hung")

    def test_timer_is_a_daemon_so_a_pending_restart_cannot_hold_exit(self):
        from lager.util import self_restart

        with patch.object(self_restart, "maybe_self_restart", lambda *a, **k: None):
            timer = self_restart.schedule_self_restart_for_hang(
                "USB0::0xFFFF::0x0000::X::INSTR", "ctx",
                service="svc", stamp_path="/tmp/stamp-under-test", delay_s=30)
        try:
            self.assertTrue(timer.daemon)
        finally:
            timer.cancel()


if __name__ == "__main__":
    unittest.main()
