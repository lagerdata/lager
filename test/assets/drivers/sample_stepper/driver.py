# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Example custom driver: a stepper rig controlled over a UART net.

Install it with ``lager drivers install test/assets/drivers/sample_stepper``.
A real driver would add actions such as ``home`` or ``jog``; ``diag`` needs no
hardware, and ``echo`` works against any UART loopback.
"""

from lager.drivers import CustomDriver, action

_ENDINGS = {"cr": b"\r", "lf": b"\n", "crlf": b"\r\n"}


class Driver(CustomDriver):
    @action
    def diag(self):
        uart = self.base_net("uart_net")
        try:
            path = uart.get_path()
        except FileNotFoundError as e:
            path = None
            self.log.warning("UART device not found: %s", e)
        return {
            "message": "uart_net %s -> %s" % (self.params["uart_net"], path or "not connected"),
            "uart_net": self.params["uart_net"],
            "device_path": path,
            "baudrate": self.params["baudrate"],
        }

    @action
    def echo(self, text, line_ending, read_timeout):
        port = self.base_net("uart_net").connect(
            baudrate=self.params["baudrate"], timeout=read_timeout)
        try:
            port.reset_input_buffer()
            port.write(text.encode() + _ENDINGS[line_ending])
            reply = port.readline().decode(errors="replace").strip()
        finally:
            port.close()
        return {"message": reply or "(no reply)", "reply": reply}
