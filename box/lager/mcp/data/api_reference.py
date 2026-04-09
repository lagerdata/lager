# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
Structured API reference for the lager.Net Python API, organised by NetType.

Exposed via the ``lager://reference/{net_type}`` MCP resource template
and consumed by ``plan_firmware_test`` to teach agents how to write
on-box test scripts.

The ``methods`` list for each NetType is *introspected* at module import
time from the canonical driver class (see ``_DRIVER_CLASSES`` below) and
overwrites the hand-written list in ``API_REFERENCE``. This keeps the
agent-facing reference in lock-step with the real driver implementations
so a driver rename can never silently mislead an agent.

The hand-written ``methods`` lists below are kept as a fallback for the
case where a driver class fails to import (e.g. development environments
without the C extensions installed) and as the source of truth for
``Debug``, which has no Net subclass.

``gotchas`` and ``example_snippet`` cannot be introspected and are always
hand-curated.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any

logger = logging.getLogger(__name__)

API_REFERENCE: dict[str, dict] = {
    "PowerSupply": {
        "net_type_enum": "NetType.PowerSupply",
        "get_pattern": 'psu = Net.get("supply1", type=NetType.PowerSupply)',
        "methods": [
            {"name": "set_voltage", "sig": "set_voltage(volts: float)", "desc": "Set output voltage (V)"},
            {"name": "set_current", "sig": "set_current(amps: float)", "desc": "Set current limit (A)"},
            {"name": "enable", "sig": "enable()", "desc": "Turn output on"},
            {"name": "disable", "sig": "disable()", "desc": "Turn output off"},
            {"name": "voltage", "sig": "voltage() -> float", "desc": "Read back measured voltage"},
            {"name": "current", "sig": "current() -> float", "desc": "Read back measured current"},
            {"name": "state", "sig": "state() -> dict", "desc": "Get output state (enabled, voltage, current, mode)"},
            {"name": "set_ovp", "sig": "set_ovp(volts: float)", "desc": "Set over-voltage protection limit"},
            {"name": "set_ocp", "sig": "set_ocp(amps: float)", "desc": "Set over-current protection limit"},
            {"name": "clear_ovp", "sig": "clear_ovp()", "desc": "Clear OVP trip"},
            {"name": "clear_ocp", "sig": "clear_ocp()", "desc": "Clear OCP trip"},
        ],
        "gotchas": [
            "Always call set_voltage() BEFORE enable() — enabling with no voltage set may use the last-programmed value.",
            "Wrap tests in try/finally and call disable() in the finally block to avoid leaving supplies on.",
            "Readback values may differ slightly from setpoints due to instrument resolution.",
        ],
        "example_snippet": (
            'from lager import Net, NetType\n'
            '\n'
            'psu = Net.get("supply1", type=NetType.PowerSupply)\n'
            'try:\n'
            '    psu.set_voltage(3.3)\n'
            '    psu.set_current(1.0)\n'
            '    psu.enable()\n'
            '    import time; time.sleep(0.5)\n'
            '    v = psu.voltage()\n'
            '    print(f"Measured voltage: {v:.3f} V")\n'
            '    assert abs(v - 3.3) < 0.2, f"Voltage out of range: {v}"\n'
            'finally:\n'
            '    psu.disable()\n'
        ),
    },
    "GPIO": {
        "net_type_enum": "NetType.GPIO",
        "get_pattern": 'gpio = Net.get("gpio1", type=NetType.GPIO)',
        "methods": [
            {"name": "output", "sig": "output(level: int)", "desc": "Drive pin high (1) or low (0)"},
            {"name": "input", "sig": "input() -> int", "desc": "Read pin level (0 or 1)"},
            {"name": "wait_for_level", "sig": "wait_for_level(level: int, timeout: float) -> float", "desc": "Block until pin reaches level or timeout; returns elapsed seconds"},
        ],
        "gotchas": [
            "GPIO nets are LabJack/FT232H pins — they are box-side, not DUT-side. They drive signals INTO the DUT or read signals FROM the DUT.",
            "wait_for_level raises on timeout — wrap in try/except if you want to handle it gracefully.",
        ],
        "example_snippet": (
            'from lager import Net, NetType\n'
            '\n'
            'btn = Net.get("button1", type=NetType.GPIO)\n'
            'led = Net.get("led1", type=NetType.GPIO)\n'
            '\n'
            '# Simulate button press\n'
            'btn.output(1)\n'
            'import time; time.sleep(0.1)\n'
            '\n'
            '# Check DUT reacted\n'
            'assert led.input() == 1, "LED did not turn on after button press"\n'
            '\n'
            'btn.output(0)\n'
        ),
    },
    "ADC": {
        "net_type_enum": "NetType.ADC",
        "get_pattern": 'adc = Net.get("adc1", type=NetType.ADC)',
        "methods": [
            {"name": "input", "sig": "input() -> float", "desc": "Read voltage (V) from the ADC channel"},
        ],
        "gotchas": [
            "LabJack T7 ADC channels: 0-13 are single-ended (0-10 V default), 14+ are differential.",
            "For best accuracy, average several readings.",
        ],
        "example_snippet": (
            'from lager import Net, NetType\n'
            '\n'
            'adc = Net.get("vdd_sense", type=NetType.ADC)\n'
            'voltage = adc.input()\n'
            'print(f"VDD = {voltage:.3f} V")\n'
            'assert 3.1 < voltage < 3.5, f"VDD out of range: {voltage}"\n'
        ),
    },
    "DAC": {
        "net_type_enum": "NetType.DAC",
        "get_pattern": 'dac = Net.get("dac1", type=NetType.DAC)',
        "methods": [
            {"name": "output", "sig": "output(volts: float)", "desc": "Set output voltage (V)"},
        ],
        "gotchas": [
            "LabJack T7 DAC range: 0-5 V. Values outside this are clamped.",
        ],
        "example_snippet": (
            'from lager import Net, NetType\n'
            '\n'
            'dac = Net.get("test_signal", type=NetType.DAC)\n'
            'dac.output(2.5)  # Drive 2.5V into the DUT\n'
        ),
    },
    "UART": {
        "net_type_enum": "NetType.UART",
        "get_pattern": 'uart = Net.get("uart1", type=NetType.UART)',
        "methods": [
            {"name": "connect", "sig": "connect(baudrate=115200, timeout=1, **kwargs) -> serial.Serial", "desc": "Open a pyserial connection. Returns a standard serial.Serial object."},
            {"name": "get_path", "sig": "get_path() -> str", "desc": "Get the /dev/ttyUSBx device path"},
            {"name": "get_baudrate", "sig": "get_baudrate() -> int", "desc": "Get the default baudrate from net config"},
            {"name": "get_config", "sig": "get_config() -> dict", "desc": "Get full UART configuration"},
        ],
        "gotchas": [
            "connect() returns a standard pyserial Serial object — use ser.write(), ser.read(), ser.readline() etc.",
            "Always close the serial connection when done (use try/finally or with-statement pattern).",
            "For UART expect-style patterns: read in a loop, check for pattern match, respect timeouts.",
        ],
        "example_snippet": (
            'from lager import Net, NetType\n'
            '\n'
            'uart = Net.get("uart1", type=NetType.UART)\n'
            'ser = uart.connect(baudrate=115200, timeout=2)\n'
            'try:\n'
            '    ser.reset_input_buffer()\n'
            '    ser.write(b"version\\r\\n")\n'
            '    import time; time.sleep(0.5)\n'
            '    response = ser.read(ser.in_waiting).decode("utf-8", errors="ignore")\n'
            '    print(f"DUT response: {response}")\n'
            '    assert "v1." in response, f"Unexpected version: {response}"\n'
            'finally:\n'
            '    ser.close()\n'
        ),
    },
    "SPI": {
        "net_type_enum": "NetType.SPI",
        "get_pattern": 'spi = Net.get("spi1", type=NetType.SPI)',
        "methods": [
            {"name": "config", "sig": "config(mode=0, bit_order='msb', frequency_hz=1000000, word_size=8, cs_active='low')", "desc": "Configure SPI bus parameters"},
            {"name": "read", "sig": "read(n_words=1, fill=0xFF) -> list[int]", "desc": "Clock out fill bytes and return received data"},
            {"name": "write", "sig": "write(data: list[int])", "desc": "Write data (discard read)"},
            {"name": "read_write", "sig": "read_write(data: list[int]) -> list[int]", "desc": "Full-duplex: write data, return simultaneous read"},
            {"name": "transfer", "sig": "transfer(n_words, data, fill=0xFF) -> list[int]", "desc": "Write data padded/truncated to n_words, return read"},
        ],
        "gotchas": [
            "Call config() before first transfer to set mode, frequency, etc.",
            "Data is list of ints (bytes). For hex display: [hex(b) for b in result].",
            "SPI is full-duplex — read_write() sends and receives simultaneously.",
        ],
        "example_snippet": (
            'from lager import Net, NetType\n'
            '\n'
            'spi = Net.get("spi1", type=NetType.SPI)\n'
            'spi.config(mode=0, frequency_hz=1_000_000)\n'
            '\n'
            '# Read JEDEC ID from a flash chip\n'
            'rx = spi.read_write([0x9F, 0x00, 0x00, 0x00])\n'
            'jedec_id = rx[1:]  # First byte is dummy\n'
            'print(f"JEDEC ID: {[hex(b) for b in jedec_id]}")\n'
        ),
    },
    "I2C": {
        "net_type_enum": "NetType.I2C",
        "get_pattern": 'i2c = Net.get("i2c1", type=NetType.I2C)',
        "methods": [
            {"name": "config", "sig": "config(frequency_hz=100000, pull_ups=True)", "desc": "Configure I2C bus"},
            {"name": "scan", "sig": "scan() -> list[int]", "desc": "Scan bus and return list of responding addresses"},
            {"name": "read", "sig": "read(address: int, num_bytes: int) -> list[int]", "desc": "Read bytes from device"},
            {"name": "write", "sig": "write(address: int, data: list[int])", "desc": "Write bytes to device"},
            {"name": "write_read", "sig": "write_read(address: int, data: list[int], num_bytes: int) -> list[int]", "desc": "Write then read in one transaction (register read pattern)"},
        ],
        "gotchas": [
            "Addresses are 7-bit (0x00-0x7F). Some datasheets show 8-bit addresses — shift right by 1.",
            "scan() is a quick way to verify device presence before reading/writing.",
            "write_read() is the standard register-read pattern: write register address, then read data.",
        ],
        "example_snippet": (
            'from lager import Net, NetType\n'
            '\n'
            'i2c = Net.get("i2c1", type=NetType.I2C)\n'
            '\n'
            '# Scan for devices\n'
            'devices = i2c.scan()\n'
            'print(f"Found devices at: {[hex(a) for a in devices]}")\n'
            '\n'
            '# Read WHO_AM_I register from accelerometer at 0x68\n'
            'who_am_i = i2c.write_read(0x68, [0x75], 1)\n'
            'print(f"WHO_AM_I = 0x{who_am_i[0]:02X}")\n'
        ),
    },
    "Debug": {
        "net_type_enum": "NetType.Debug",
        "get_pattern": 'dbg = Net.get("debug1", type=NetType.Debug)',
        "methods": [
            {"name": "connect", "sig": "connect(speed=None, transport=None)", "desc": "Connect debug probe to DUT"},
            {"name": "disconnect", "sig": "disconnect()", "desc": "Disconnect debug probe"},
            {"name": "flash", "sig": "flash(firmware_path: str)", "desc": "Flash firmware binary (.hex/.elf/.bin) to DUT"},
            {"name": "reset", "sig": "reset(halt=False)", "desc": "Reset the DUT. halt=True stops at first instruction."},
            {"name": "erase", "sig": "erase()", "desc": "Mass-erase DUT flash memory"},
            {"name": "read_memory", "sig": "read_memory(address: int, length: int) -> bytes", "desc": "Read raw memory from DUT"},
            {"name": "rtt", "sig": "rtt(channel=0) -> context_manager", "desc": "Open an RTT channel (use as context manager). Returns object with .write() and .read_some()."},
            {"name": "status", "sig": "status() -> dict", "desc": "Get probe/target status"},
        ],
        "gotchas": [
            "Flash paths must be absolute or relative to the script's working directory on the box.",
            "Use rtt() as a context manager: `with dbg.rtt(channel=0) as rtt: rtt.write(b'cmd\\n')`",
            "RTT read_some() may return empty bytes — loop with timeout for reliable reads.",
            "connect() is often implicit on first operation, but explicit connect gives clearer errors.",
        ],
        "example_snippet": (
            'from lager import Net, NetType\n'
            '\n'
            'dbg = Net.get("debug1", type=NetType.Debug)\n'
            '\n'
            '# Flash and reset\n'
            'dbg.flash("/path/to/firmware.hex")\n'
            'dbg.reset()\n'
            '\n'
            '# Read RTT output\n'
            'import time\n'
            'with dbg.rtt(channel=0) as rtt:\n'
            '    time.sleep(1)  # Let firmware boot\n'
            '    data = rtt.read_some(timeout=2)\n'
            '    print(f"RTT output: {data.decode()}")\n'
        ),
    },
    "Battery": {
        "net_type_enum": "NetType.Battery",
        "get_pattern": 'batt = Net.get("battery1", type=NetType.Battery)',
        "methods": [
            {"name": "enable", "sig": "enable()", "desc": "Enable battery simulator output"},
            {"name": "disable", "sig": "disable()", "desc": "Disable battery simulator output"},
            {"name": "soc", "sig": "soc(value: float)", "desc": "Set state of charge (0-100%)"},
            {"name": "voc", "sig": "voc(value: float)", "desc": "Set open-circuit voltage (V)"},
            {"name": "set_mode_battery", "sig": "set_mode_battery()", "desc": "Apply battery configuration to hardware"},
            {"name": "terminal_voltage", "sig": "terminal_voltage() -> float", "desc": "Read terminal voltage"},
            {"name": "current", "sig": "current() -> float", "desc": "Read current draw"},
        ],
        "gotchas": [
            "Call set_mode_battery() after configuring SOC/VOC to apply settings.",
            "Battery simulators (Keithley 2281S) have warm-up time after enable.",
        ],
        "example_snippet": (
            'from lager import Net, NetType\n'
            '\n'
            'batt = Net.get("battery1", type=NetType.Battery)\n'
            'try:\n'
            '    batt.soc(80)  # 80% charge\n'
            '    batt.set_mode_battery()\n'
            '    batt.enable()\n'
            '    import time; time.sleep(1)\n'
            '    v = batt.terminal_voltage()\n'
            '    i = batt.current()\n'
            '    print(f"Battery: {v:.2f}V, {i*1000:.1f}mA")\n'
            'finally:\n'
            '    batt.disable()\n'
        ),
    },
    "ELoad": {
        "net_type_enum": "NetType.ELoad",
        "get_pattern": 'eload = Net.get("eload1", type=NetType.ELoad)',
        "methods": [
            {"name": "current", "sig": "current(amps: float)", "desc": "Set constant-current load value"},
            {"name": "voltage", "sig": "voltage(volts: float)", "desc": "Set constant-voltage load value"},
            {"name": "resistance", "sig": "resistance(ohms: float)", "desc": "Set constant-resistance load value"},
            {"name": "power", "sig": "power(watts: float)", "desc": "Set constant-power load value"},
            {"name": "enable", "sig": "enable()", "desc": "Enable the electronic load"},
            {"name": "disable", "sig": "disable()", "desc": "Disable the electronic load"},
            {"name": "measured_voltage", "sig": "measured_voltage() -> float", "desc": "Read measured voltage"},
            {"name": "measured_current", "sig": "measured_current() -> float", "desc": "Read measured current"},
        ],
        "gotchas": [
            "Set the load mode (current/voltage/resistance/power) before enabling.",
        ],
        "example_snippet": (
            'from lager import Net, NetType\n'
            '\n'
            'eload = Net.get("eload1", type=NetType.ELoad)\n'
            'try:\n'
            '    eload.current(0.5)  # 500mA constant current\n'
            '    eload.enable()\n'
            '    import time; time.sleep(1)\n'
            '    v = eload.measured_voltage()\n'
            '    print(f"Voltage under load: {v:.3f}V")\n'
            'finally:\n'
            '    eload.disable()\n'
        ),
    },
    "Thermocouple": {
        "net_type_enum": "NetType.Thermocouple",
        "get_pattern": 'tc = Net.get("temp1", type=NetType.Thermocouple)',
        "methods": [
            {"name": "read", "sig": "read() -> float", "desc": "Read temperature in degrees Celsius"},
        ],
        "gotchas": [
            "Phidget thermocouples need a few seconds after first read to stabilize.",
        ],
        "example_snippet": (
            'from lager import Net, NetType\n'
            '\n'
            'tc = Net.get("temp1", type=NetType.Thermocouple)\n'
            'temp = tc.read()\n'
            'print(f"Temperature: {temp:.1f}°C")\n'
            'assert 15 < temp < 45, f"Temperature out of range: {temp}"\n'
        ),
    },
    "WattMeter": {
        "net_type_enum": "NetType.WattMeter",
        "get_pattern": 'watt = Net.get("watt1", type=NetType.WattMeter)',
        "methods": [
            {"name": "read", "sig": "read() -> float", "desc": "Read power in watts"},
            {"name": "read_all", "sig": "read_all() -> dict", "desc": "Read voltage, current, and power"},
        ],
        "gotchas": [
            "Yocto-Watt is an inline power meter — it must be wired in series with the DUT power path.",
        ],
        "example_snippet": (
            'from lager import Net, NetType\n'
            '\n'
            'watt = Net.get("watt1", type=NetType.WattMeter)\n'
            'readings = watt.read_all()\n'
            'print(f"Power: {readings}")\n'
        ),
    },
    "Usb": {
        "net_type_enum": "NetType.Usb",
        "get_pattern": 'usb = Net.get("usb1", type=NetType.Usb)',
        "methods": [
            {"name": "enable", "sig": "enable()", "desc": "Enable USB port (power on)"},
            {"name": "disable", "sig": "disable()", "desc": "Disable USB port (power off)"},
        ],
        "gotchas": [
            "USB power cycling is useful for resetting DUTs that charge or boot via USB.",
            "Allow 1-2 seconds after enable for USB enumeration.",
        ],
        "example_snippet": (
            'from lager import Net, NetType\n'
            'import time\n'
            '\n'
            'usb = Net.get("usb1", type=NetType.Usb)\n'
            'usb.disable()\n'
            'time.sleep(1)\n'
            'usb.enable()\n'
            'time.sleep(2)  # Wait for enumeration\n'
        ),
    },
    "EnergyAnalyzer": {
        "net_type_enum": "NetType.EnergyAnalyzer",
        "get_pattern": 'energy = Net.get("energy1", type=NetType.EnergyAnalyzer)',
        "methods": [
            {"name": "read_energy", "sig": "read_energy(duration: float) -> dict", "desc": "Integrate energy over duration (seconds). Returns joules, watt-hours, coulombs."},
            {"name": "read_stats", "sig": "read_stats(duration: float) -> dict", "desc": "Compute current/voltage/power statistics over duration."},
        ],
        "gotchas": [
            "Duration is how long to sample — longer durations give more accurate averages.",
        ],
        "example_snippet": (
            'from lager import Net, NetType\n'
            '\n'
            'energy = Net.get("energy1", type=NetType.EnergyAnalyzer)\n'
            'stats = energy.read_stats(5.0)  # Sample for 5 seconds\n'
            'print(f"Average power: {stats}")\n'
        ),
    },
}


# ---------------------------------------------------------------------------
# Driver introspection — overwrites hand-written `methods` at import time
# ---------------------------------------------------------------------------
#
# Map NetType key in API_REFERENCE → dotted path of the canonical driver
# class to introspect. Add new entries here as new drivers are added.
# ``Debug`` is intentionally absent: it has no Net subclass (the debug
# API is procedural functions in box/lager/debug/api.py).

_DRIVER_CLASSES: dict[str, str] = {
    "PowerSupply":    "lager.power.supply.supply_net.SupplyNet",
    "Battery":        "lager.power.battery.battery_net.BatteryNet",
    "ELoad":          "lager.power.eload.eload_net.ELoadNet",
    "GPIO":           "lager.io.gpio.gpio_net.GPIOBase",
    "ADC":            "lager.io.adc.adc_net.ADCBase",
    "DAC":            "lager.io.dac.dac_net.DACBase",
    "UART":           "lager.protocols.uart.uart_net.UARTNet",
    "SPI":            "lager.protocols.spi.spi_net.SPINet",
    "I2C":            "lager.protocols.i2c.i2c_net.I2CNet",
    "Thermocouple":   "lager.measurement.thermocouple.thermocouple_net.ThermocoupleBase",
    "WattMeter":      "lager.measurement.watt.watt_net.WattMeterBase",
    "EnergyAnalyzer": "lager.measurement.energy_analyzer.energy_analyzer_net.EnergyAnalyzerBase",
    "Usb":            "lager.automation.usb_hub.usb_net.USBNet",
}

# Methods we never want to expose to agents (private, dunder, base-class
# plumbing, abstract decorator artifacts, etc.)
_INTROSPECT_SKIP = {
    "from_dict", "to_dict", "register", "unregister",
}


def _import_class(dotted: str) -> Any:
    mod_name, _, cls_name = dotted.rpartition(".")
    mod = __import__(mod_name, fromlist=[cls_name])
    return getattr(mod, cls_name)


def _introspect_methods(cls: Any) -> list[dict]:
    """Walk a driver class and return a structured method list.

    Skips private/dunder methods, things in ``_INTROSPECT_SKIP``, and any
    member that doesn't carry a usable signature.
    """
    out: list[dict] = []
    for name, member in inspect.getmembers(cls, predicate=inspect.isfunction):
        if name.startswith("_") or name in _INTROSPECT_SKIP:
            continue
        try:
            params = inspect.signature(member)
            # Drop the leading "self" param for agent-facing display
            kept = [p for p in params.parameters.values() if p.name != "self"]
            sig = f"{name}({', '.join(str(p) for p in kept)})"
            if params.return_annotation is not inspect.Signature.empty:
                ret = params.return_annotation
                ret_str = ret if isinstance(ret, str) else getattr(ret, "__name__", str(ret))
                sig += f" -> {ret_str}"
        except (TypeError, ValueError):
            sig = f"{name}(...)"
        doc = inspect.getdoc(member) or ""
        desc = next((ln.strip() for ln in doc.splitlines() if ln.strip()), "")
        out.append({"name": name, "sig": sig, "desc": desc})
    out.sort(key=lambda m: m["name"])
    return out


def _apply_introspection() -> None:
    """For every NetType with a known driver class, replace the hand-written
    ``methods`` list with the introspected one. Failures fall back to the
    hand-written list and emit a warning."""
    for net_type, dotted in _DRIVER_CLASSES.items():
        if net_type not in API_REFERENCE:
            logger.warning(
                "api_reference: %s in _DRIVER_CLASSES but not in API_REFERENCE",
                net_type,
            )
            continue
        try:
            cls = _import_class(dotted)
        except Exception as e:
            logger.warning(
                "api_reference: introspection failed for %s (%s); "
                "falling back to hand-written methods. Error: %s",
                net_type, dotted, e,
            )
            continue
        methods = _introspect_methods(cls)
        if methods:
            API_REFERENCE[net_type]["methods"] = methods
            API_REFERENCE[net_type]["source_module"] = dotted


_apply_introspection()


def get_reference_for_type(net_type: str) -> dict | None:
    """Look up API reference by net_type string or NetType enum name.

    Accepts both the raw net_type from saved_nets (e.g. "power-supply",
    "spi", "gpio") and the NetType enum name (e.g. "PowerSupply", "SPI").
    """
    _ALIAS_MAP = {
        "power-supply": "PowerSupply",
        "power-supply-2q": "PowerSupply",
        "supply": "PowerSupply",
        "gpio": "GPIO",
        "adc": "ADC",
        "dac": "DAC",
        "uart": "UART",
        "spi": "SPI",
        "i2c": "I2C",
        "debug": "Debug",
        "battery": "Battery",
        "batt": "Battery",
        "eload": "ELoad",
        "thermocouple": "Thermocouple",
        "watt-meter": "WattMeter",
        "usb": "Usb",
        "energy-analyzer": "EnergyAnalyzer",
    }
    key = _ALIAS_MAP.get(net_type.lower(), net_type)
    return API_REFERENCE.get(key)


def list_supported_types() -> list[str]:
    """Return the list of NetType names with API reference entries."""
    return sorted(API_REFERENCE.keys())
