# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
What the macOS host agent lets ``/invoke`` reach.

On a Mac the hardware service runs as the user, not in a container, so
``/invoke`` must not let the container name any ``lager.*`` module and call
anything in it. These tests pin that the Box's own callers still get through
(every ``*_hs`` adapter, the drivers the nets map to, a class built from
``net_info``, a module's own functions) and that the rest is refused: a dotted
module path, a top-level module that is not an adapter, a private name, and a
name a module only imported. The last test builds the agent's real
application and checks the guard is in front of ``/invoke`` there.
"""

import types
import unittest
import unittest.mock

from flask import Flask, jsonify

from lager.host import invoke_guard, worker
from lager.hostlink import auth, config, protocol


def _req(device, function, net_info=None):
    body = {"device": device, "function": function}
    if net_info is not None:
        body["net_info"] = net_info
    return body


class AdmittedTests(unittest.TestCase):
    """Requests the Box itself sends."""

    def test_every_hs_adapter(self):
        for name in ("adc_hs", "dac_hs", "gpio_hs", "spi_hs", "i2c_hs", "scope_hs",
                     "watt_hs", "energy_hs", "thermocouple_hs", "arm_hs", "hsm_hs",
                     "solar_hs", "dmm_hs", "labjack_boot_config_hs"):
            with self.subTest(name=name):
                self.assertIsNone(invoke_guard.check(_req(name, "read", {"pin": 0})))

    def test_the_drivers_nets_map_to(self):
        for name in ("rigol_dp800", "rigol_dp700", "keithley", "keithley_battery",
                     "ea", "siglent_spd1000", "rigol_dl3021", "keysight_e36000",
                     "rigol_mso5000"):
            with self.subTest(name=name):
                self.assertIsNone(invoke_guard.check(
                    _req(name, "enable_output", {"address": "USB0::1::2::3::INSTR"})))

    def test_malformed_requests_are_left_to_invoke(self):
        for body in (None, [], {}, {"device": "gpio_hs"}, {"function": "read"},
                     _req("no_such_driver_anywhere", "read")):
            with self.subTest(body=body):
                self.assertIsNone(invoke_guard.check(body))


class RefusedTests(unittest.TestCase):
    """Requests that would reach code on the Mac outside a driver."""

    def test_a_dotted_module_path(self):
        for name in ("host.darwin_debug", "debug.jlink", "hostlink.config",
                     "util.self_restart", "lager.core", "../core", "gpio_hs.os"):
            with self.subTest(name=name):
                self.assertIsNotNone(invoke_guard.check(_req(name, "read")))

    def test_a_top_level_module_that_is_not_an_adapter(self):
        for name in ("core", "hardware_service", "breakpoint", "cache", "safety",
                     "box_http_server", "lock_state"):
            with self.subTest(name=name):
                reason = invoke_guard.check(_req(name, "anything"))
                self.assertIsNotNone(reason)
                self.assertIn("not a hardware driver", reason)

    def test_a_private_or_dunder_function(self):
        for fn in ("_get_handle", "__class__", "__init__", "__globals__", "", "a.b", "a b"):
            with self.subTest(fn=fn):
                if fn == "":
                    # Missing function: /invoke answers it with its own 400.
                    self.assertIsNone(invoke_guard.check(_req("gpio_hs", fn)))
                else:
                    self.assertIsNotNone(invoke_guard.check(_req("gpio_hs", fn)))

    def test_a_non_string_name(self):
        self.assertIsNotNone(invoke_guard.check({"device": ["gpio_hs"], "function": "read"}))
        self.assertIsNotNone(invoke_guard.check({"device": "gpio_hs", "function": 3}))


class ModuleAsDeviceTests(unittest.TestCase):
    """A driver module with no factory is itself the device: only its own
    functions may be called, not what it imported."""

    def _fake(self, name):
        mod = types.ModuleType(f"lager.nets.mappers.{name}")

        def own(x=1):
            return x

        own.__module__ = mod.__name__
        mod.own = own
        import subprocess
        mod.run = subprocess.run            # an imported function
        mod.Thing = type("Thing", (), {})   # an imported or local class
        return mod

    def _check(self, function):
        name = "fake_mapper_for_guard"
        mod = self._fake(name)
        with unittest.mock.patch.object(invoke_guard, "_resolve",
                                        return_value=mod.__name__), \
             unittest.mock.patch.object(invoke_guard.importlib, "import_module",
                                        return_value=mod):
            return invoke_guard.check(_req(name, function))

    def test_its_own_function_is_admitted(self):
        self.assertIsNone(self._check("own"))

    def test_an_imported_function_is_refused(self):
        self.assertIn("is not a function of", self._check("run"))

    def test_a_class_is_refused(self):
        self.assertIsNotNone(self._check("Thing"))

    def test_the_real_dl3000_mapper(self):
        # Resolves to lager.nets.mappers.rigol_dl3000, which only imports Mode.
        self.assertIsNotNone(invoke_guard.check(_req("rigol_dl3000", "Mode")))


class AgentApplicationTests(unittest.TestCase):
    """The guard is installed in front of /invoke in the agent's real app."""

    def test_create_app_refuses_a_dotted_device(self):
        from lager import hardware_service

        app = Flask("stand-in-hardware-service")
        calls = []

        @app.route("/invoke", methods=["POST"])
        def _invoke():
            calls.append(1)
            return jsonify({"result": "ran"})

        endpoint = config.Endpoint(token=config.new_token(), port=1,
                                   connect_host="127.0.0.1")
        noop = unittest.mock.Mock()
        with unittest.mock.patch.object(hardware_service, "app", app), \
             unittest.mock.patch.object(worker.sys, "platform", "linux"), \
             unittest.mock.patch("lager.host.debug_rpc.install", noop), \
             unittest.mock.patch("lager.host.usb_rpc.install", noop), \
             unittest.mock.patch("lager.host.net_rpc.install", noop), \
             unittest.mock.patch("lager.host.serial_rpc.install", noop):
            built = worker.create_app(endpoint)
        client = built.test_client()
        headers = {"Authorization": auth.bearer_header(endpoint.token),
                   protocol.PROTOCOL_HEADER: str(protocol.PROTOCOL_VERSION)}

        refused = client.post("/invoke", headers=headers,
                              json=_req("host.darwin_debug", "commander_wrapper"))
        self.assertEqual(refused.status_code, 403)
        self.assertEqual(refused.get_json()["code"], protocol.ERR_BAD_REQUEST)
        self.assertEqual(calls, [])

        admitted = client.post("/invoke", headers=headers, json=_req("gpio_hs", "read"))
        self.assertEqual(admitted.status_code, 200)
        self.assertEqual(calls, [1])


if __name__ == "__main__":
    unittest.main()
