# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""A simulated Siglent SPD1168X power supply, served over a raw TCP socket.

Stands in for a physical instrument where none is attached, so the path from
the CLI to a driver can be exercised end to end. The real, unmodified driver
(``lager.power.supply.siglent_spd1000``) talks to it through pyvisa-py using
an address such as ``TCPIP0::127.0.0.1::5025::SOCKET``.

It proves the software path and nothing else. A pass against this simulator
is not evidence that any USB instrument works.

Run it:  python3 test/framework/fake_scpi_supply.py --port 5025
"""
from __future__ import annotations

import argparse
import socket
import socketserver
import threading

IDN = "Siglent Technologies,SPD1168X,SPD1XFAKE000001,2.1.1.9,V1.0"
VOLTAGE_MAX = 16.0
CURRENT_MAX = 8.0

STATUS_CC = 1 << 0
STATUS_OUTPUT_ON = 1 << 4
STATUS_FOUR_WIRE = 1 << 5


class SupplyState:
    """One supply's state, shared by every connection to the server."""

    def __init__(self, load_ohms: float = 0.0):
        self.lock = threading.Lock()
        self.voltage_set = 0.0
        self.current_set = 0.0
        self.output_on = False
        self.four_wire = False
        self.load_ohms = load_ohms  # 0 means nothing is connected
        self.error = (0, "No Error")
        self.commands = []  # every line received, for assertions

    def _measured(self):
        """(volts, amps, in_cc) at the terminals."""
        if not self.output_on:
            return 0.0, 0.0, False
        if self.load_ohms <= 0:
            return self.voltage_set, 0.0, False
        amps = self.voltage_set / self.load_ohms
        if amps > self.current_set:
            return self.current_set * self.load_ohms, self.current_set, True
        return self.voltage_set, amps, False

    def handle(self, line: str):
        """Apply one SCPI line. Returns the reply, or None for a command."""
        with self.lock:
            self.commands.append(line)
            upper = line.upper()
            if upper == "*IDN?":
                return IDN
            if upper in ("SYSTEM:ERROR?", "SYST:ERR?"):
                code, message = self.error
                self.error = (0, "No Error")
                return f"{code}  {message}"
            if upper in ("SYSTEM:STATUS?", "SYST:STAT?"):
                _, _, in_cc = self._measured()
                bits = ((STATUS_CC if in_cc else 0)
                        | (STATUS_OUTPUT_ON if self.output_on else 0)
                        | (STATUS_FOUR_WIRE if self.four_wire else 0))
                return f"0x{bits:02x}"
            if upper in ("CH1:VOLTAGE?", "CH1:VOLT?"):
                return f"{self.voltage_set:.3f}"
            if upper in ("CH1:CURRENT?", "CH1:CURR?"):
                return f"{self.current_set:.3f}"
            if upper.startswith(("MEASURE:VOLTAGE?", "MEAS:VOLT?")):
                return f"{self._measured()[0]:.3f}"
            if upper.startswith(("MEASURE:CURRENT?", "MEAS:CURR?")):
                return f"{self._measured()[1]:.3f}"
            if upper.startswith(("MEASURE:POWER?", "MEAS:POWE?")):
                volts, amps, _ = self._measured()
                return f"{volts * amps:.3f}"
            if upper.startswith(("CH1:VOLTAGE ", "CH1:VOLT ")):
                return self._set("voltage_set", line, VOLTAGE_MAX)
            if upper.startswith(("CH1:CURRENT ", "CH1:CURR ")):
                return self._set("current_set", line, CURRENT_MAX)
            if upper.startswith(("OUTPUT CH1,", "OUTP CH1,")):
                self.output_on = upper.rsplit(",", 1)[1].strip() == "ON"
                return None
            if upper.startswith("MODE:SET "):
                self.four_wire = upper.split()[-1] == "4W"
                return None
            self.error = (-113, "Undefined header")
            return "" if upper.endswith("?") else None

    def _set(self, field: str, line: str, maximum: float):
        try:
            value = float(line.split(None, 1)[1])
        except (IndexError, ValueError):
            self.error = (-104, "Data type error")
            return None
        if not 0.0 <= value <= maximum:
            self.error = (-222, "Data out of range")
            return None
        setattr(self, field, value)
        return None


class _Handler(socketserver.StreamRequestHandler):
    def setup(self):
        super().setup()
        with self.server.connections_lock:
            self.server.connections.add(self.request)

    def finish(self):
        with self.server.connections_lock:
            self.server.connections.discard(self.request)
        try:
            super().finish()
        except OSError:
            pass  # the connection was dropped on purpose

    def handle(self):
        try:
            self._serve()
        except OSError:
            pass  # the connection was dropped on purpose

    def _serve(self):
        for raw in self.rfile:
            line = raw.decode("ascii", "replace").strip()
            if not line:
                continue
            reply = self.server.state.handle(line)
            if reply is not None:
                self.wfile.write((reply + "\n").encode("ascii"))
                self.wfile.flush()


class FakeSupplyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, host: str = "127.0.0.1", port: int = 0, load_ohms: float = 0.0):
        self.connections = set()
        self.connections_lock = threading.Lock()
        super().__init__((host, port), _Handler)
        self.state = SupplyState(load_ohms)

    def drop_connections(self) -> int:
        """Cut every open client connection, as an instrument does when it
        loses power or is unplugged from the network. Returns how many
        connections were cut.
        """
        with self.connections_lock:
            live = list(self.connections)
            self.connections.clear()
        for conn in live:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        return len(live)

    @property
    def port(self) -> int:
        return self.server_address[1]

    @property
    def visa_address(self) -> str:
        return f"TCPIP0::{self.server_address[0]}::{self.port}::SOCKET"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5025)
    parser.add_argument("--load-ohms", type=float, default=0.0,
                        help="resistive load on the output (0 = open circuit)")
    args = parser.parse_args(argv)
    with FakeSupplyServer(args.host, args.port, args.load_ohms) as server:
        print(f"Fake SPD1168X at {server.visa_address}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
