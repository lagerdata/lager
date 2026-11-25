# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for how box/lager/hardware_service.py recovers a network-attached
instrument whose connection dropped.

``/invoke`` keeps one driver, and so one open connection, per instrument. For
a USB instrument a dead session is recognised and the driver is rebuilt once
before the call is retried. A dropped TCP connection was not recognised: the
operating system reports it as "Connection reset by peer" and then "Broken
pipe", neither of which the stale-session check matched. The dead driver
stayed cached, so every later call failed the same way until the service was
restarted -- including after the instrument was back.

These tests drive the real service against a simulated supply on a loopback
socket and cut the connection the way a power-cycled instrument does. The
driver is a minimal one defined here that talks to the socket directly:
other test modules in this suite replace pyvisa with stubs for the whole
process, so a test that needed the real VISA stack would pass or fail
depending on what ran before it. What is under test is the service's
recovery, and the socket errors it must recognise are the operating
system's own either way.

The fix is scoped to TCPIP addresses. The tests at the end pin that a USB
address never takes the new path, whatever the error says.

Not covered, and not fixed: a command that only writes (a setpoint, an output
switch) sent as the very first call after a drop. TCP accepts that write
locally before it learns the peer is gone, so no error is raised and the
command is lost. The next call that reads fails, reconnects, and everything
after it works. Detecting the lost write would need a read-back in every
driver.
"""

import socket
import sys
import threading
import types
import unittest

from test.framework.fake_scpi_supply import FakeSupplyServer

import lager.hardware_service as hs

DRIVER = "tcp_supply_under_test"


class _SocketSupply:
    """The least a driver can be: one TCP connection, SCPI lines over it."""

    def __init__(self, address):
        _, host, port, _ = address.split("::")
        self._sock = socket.create_connection((host, int(port)), timeout=5)
        self._file = self._sock.makefile("rb")

    def _write(self, line):
        self._sock.sendall((line + "\n").encode("ascii"))

    def _query(self, line):
        self._write(line)
        reply = self._file.readline()
        if not reply:
            raise ConnectionResetError(54, "Connection reset by peer")
        return reply.decode("ascii").strip()

    def measure_voltage(self):
        return float(self._query("MEASure:VOLTage? CH1"))

    def get_channel_voltage(self):
        return float(self._query("CH1:VOLTage?"))

    def set_channel_voltage(self, value):
        self._write(f"CH1:VOLTage {value:.3f}")

    def close(self):
        self._sock.close()


def _install_driver():
    module = types.ModuleType(f"lager.{DRIVER}")
    module.create_device = lambda net_info: _SocketSupply(net_info["address"])
    sys.modules[module.__name__] = module
    return module.__name__


def _serve(port=0):
    server = FakeSupplyServer(port=port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class DroppedNetworkInstrumentTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(sys.modules.pop, _install_driver(), None)
        self.client = hs.app.test_client()
        self.server = _serve()
        self.port = self.server.port
        self.address = self.server.visa_address
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self._stop_server()
        key = (DRIVER, self.address)
        device = hs.device_cache.pop(key, None)
        hs.module_cache.pop(key, None)
        if device is not None:
            hs._close_device(device, key)

    def _stop_server(self):
        if self.server is not None:
            self.server.drop_connections()
            self.server.shutdown()
            self.server.server_close()
            self.server = None

    def _invoke(self, function, *args):
        return self.client.post("/invoke", json={
            "device": DRIVER,
            "function": function,
            "args": list(args),
            "kwargs": {},
            "net_info": {"name": "s", "address": self.address, "channel": 1,
                         "instrument": "Siglent_SPD1168X"},
        })

    def test_a_dropped_connection_is_reopened_within_the_same_call(self):
        self.assertEqual(self._invoke("set_channel_voltage", 3.3).status_code, 200)
        # A query, so the simulator has applied the set and registered the
        # connection before it is cut; the set alone is a bare write and
        # returns before the simulator has necessarily seen it.
        self.assertEqual(self._invoke("get_channel_voltage").get_json(), 3.3)
        self.assertEqual(self.server.drop_connections(), 1)

        resp = self._invoke("get_channel_voltage")

        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertEqual(resp.get_json(), 3.3)

    def test_the_instrument_works_again_once_it_is_back(self):
        self.assertEqual(self._invoke("measure_voltage").status_code, 200)
        self._stop_server()

        gone = self._invoke("measure_voltage")
        self.assertEqual(gone.status_code, 500)
        still_gone = self._invoke("measure_voltage")
        self.assertEqual(still_gone.status_code, 500)

        self.server = _serve(self.port)
        back = self._invoke("measure_voltage")
        self.assertEqual(back.status_code, 200, back.get_data(as_text=True))

    def test_while_it_is_gone_the_error_comes_from_a_fresh_attempt(self):
        # Each call now tries to open the instrument again, so the error is
        # the driver's own account of that attempt, not the stale pipe.
        self.assertEqual(self._invoke("measure_voltage").status_code, 200)
        self._stop_server()

        error = self._invoke("measure_voltage").get_json()["error"]

        self.assertIn("(after retry)", error)
        self.assertNotIn("Broken pipe", error)
        self.assertNotIn("Connection reset", error)

    def test_a_dead_driver_is_not_left_in_the_cache(self):
        self.assertEqual(self._invoke("measure_voltage").status_code, 200)
        first = hs.device_cache[(DRIVER, self.address)]
        self._stop_server()
        self._invoke("measure_voltage")
        self.assertIsNot(hs.device_cache.get((DRIVER, self.address)), first)

    def test_a_setting_made_after_reconnection_reaches_the_instrument(self):
        self.assertEqual(self._invoke("measure_voltage").status_code, 200)
        self.server.drop_connections()
        self.assertEqual(self._invoke("measure_voltage").status_code, 200)  # reconnects

        self.assertEqual(self._invoke("set_channel_voltage", 5.0).status_code, 200)

        # Read back over the same connection: the set is a bare write, so the
        # simulator may not have applied it by the time the call returns.
        self.assertEqual(self._invoke("get_channel_voltage").get_json(), 5.0)
        self.assertEqual(self.server.state.voltage_set, 5.0)


class ScopeOfTheFixTests(unittest.TestCase):
    """Only TCPIP addresses take the new path."""

    DROPS = (BrokenPipeError(32, "Broken pipe"),
             ConnectionResetError(54, "Connection reset by peer"),
             ConnectionAbortedError(53, "Software caused connection abort"))

    def test_socket_drops_on_a_network_address_are_recognised(self):
        for address in ("TCPIP0::10.0.0.5::5025::SOCKET", "TCPIP::10.0.0.5::INSTR",
                        "tcpip0::host::inst0::INSTR"):
            for exc in self.DROPS:
                with self.subTest(address=address, exc=type(exc).__name__):
                    self.assertTrue(hs._is_dropped_network_session(exc, address))

    def test_a_wrapped_drop_is_recognised_through_its_cause(self):
        try:
            try:
                raise BrokenPipeError(32, "Broken pipe")
            except BrokenPipeError as inner:
                raise RuntimeError("write failed") from inner
        except RuntimeError as wrapped:
            self.assertTrue(hs._is_dropped_network_session(wrapped, "TCPIP0::h::5025::SOCKET"))

    def test_a_drop_reported_only_as_text_is_recognised(self):
        exc = Exception("[Errno 104] Connection reset by peer")
        self.assertTrue(hs._is_dropped_network_session(exc, "TCPIP0::h::5025::SOCKET"))

    def test_a_usb_address_never_takes_this_path(self):
        usb = "USB0::0x1AB1::0x0E11::DP8C000001::INSTR"
        for exc in self.DROPS + (Exception("[Errno 32] Pipe error"),
                                 Exception("Broken pipe")):
            with self.subTest(exc=str(exc)):
                self.assertFalse(hs._is_dropped_network_session(exc, usb))

    def test_serial_and_addressless_devices_never_take_this_path(self):
        for address in ("ASRL/dev/ttyUSB0::INSTR", "", None, "labjack"):
            with self.subTest(address=address):
                self.assertFalse(hs._is_dropped_network_session(self.DROPS[0], address))

    def test_ordinary_instrument_errors_on_a_network_address_are_left_alone(self):
        tcp = "TCPIP0::10.0.0.5::5025::SOCKET"
        for exc in (ValueError("Voltage 99.0V exceeds hardware limit 16.0V"),
                    TimeoutError("timed out"),
                    Exception("VI_ERROR_TMO (-1073807339): Timeout expired"),
                    ConnectionRefusedError(61, "Connection refused")):
            with self.subTest(exc=str(exc)):
                self.assertFalse(hs._is_dropped_network_session(exc, tcp))

    def test_the_usb_stale_session_keywords_are_unchanged(self):
        self.assertEqual(hs._VISA_SESSION_ERROR_KEYWORDS, (
            'session', 'closed', 'invalid',
            'no such device', 'cannot find', 'errno 19', 'enodev',
        ))
        self.assertFalse(hs._is_visa_session_error(Exception("[Errno 32] Broken pipe")))


if __name__ == "__main__":
    unittest.main()
