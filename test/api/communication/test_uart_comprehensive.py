#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Comprehensive UART API tests targeting a UART net.

Run with: lager python test/api/communication/test_uart_comprehensive.py --box MY-BOX

Prerequisites:
- A UART net configured in /etc/lager/saved_nets.json
- Example net configuration:
  {
    "name": "uart1",
    "role": "uart",
    "instrument": "UARTBridge",
    "pin": "<usb_serial>",
    "channel": "0"
  }

Optional: Loopback adapter (TX wired to RX) for read-back tests.
  - Without loopback, write-only tests still run; read-back tests skip gracefully.

Wiring:
  +-------------------+
  |   UART Adapter    |
  |   TX ----> RX     |  (loopback jumper)
  +-------------------+
"""
import sys
import os
import time
import traceback

# Configuration - change these or set env vars
UART_NET = os.environ.get("UART_NET", "uart1")

# Track results
_results = []


def _record(name, passed, detail=""):
    """Record a sub-test result."""
    _results.append((name, passed, detail))
    status = "PASS" if passed else "FAIL"
    msg = f"  {status}: {name}"
    if detail:
        msg += f" -- {detail}"
    print(msg)


# ---------------------------------------------------------------------------
# 1. Imports
# ---------------------------------------------------------------------------
def test_imports():
    """Verify all UART-related imports work."""
    print("\n" + "=" * 60)
    print("TEST: Imports")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        assert hasattr(NetType, "UART"), "NetType.UART not found"
        _record("import Net, NetType", True)
    except Exception as e:
        _record("import Net, NetType", False, str(e))
        ok = False

    try:
        import serial
        _record("import serial (pyserial)", True)
    except Exception as e:
        _record("import serial (pyserial)", False, str(e))
        ok = False

    try:
        from lager.protocols.uart.uart_net import UARTNet
        _record("import UARTNet", True)
    except Exception as e:
        _record("import UARTNet", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 2. Net.get
# ---------------------------------------------------------------------------
def test_net_get():
    """Verify Net.get returns a UARTNet object."""
    print("\n" + "=" * 60)
    print("TEST: Net.get")
    print("=" * 60)

    ok = True

    try:
        from lager import Net, NetType
        from lager.protocols.uart.uart_net import UARTNet

        uart = Net.get(UART_NET, type=NetType.UART)
        is_uart = isinstance(uart, UARTNet)
        _record("Net.get returns UARTNet", is_uart,
                f"type={type(uart).__name__}")
        if not is_uart:
            ok = False
    except Exception as e:
        _record("Net.get returns UARTNet", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 3. Properties
# ---------------------------------------------------------------------------
def test_properties():
    """Verify UARTNet exposes expected properties."""
    print("\n" + "=" * 60)
    print("TEST: Properties")
    print("=" * 60)

    ok = True
    from lager import Net, NetType
    uart = Net.get(UART_NET, type=NetType.UART)

    # name
    try:
        name = uart.name
        valid = isinstance(name, str) and len(name) > 0
        _record("uart.name", valid, f"name={name!r}")
        if not valid:
            ok = False
    except Exception as e:
        _record("uart.name", False, str(e))
        ok = False

    # usb_serial
    try:
        usb_serial = uart.usb_serial
        valid = isinstance(usb_serial, str)
        _record("uart.usb_serial", valid, f"usb_serial={usb_serial!r}")
        if not valid:
            ok = False
    except Exception as e:
        _record("uart.usb_serial", False, str(e))
        ok = False

    # channel
    try:
        channel = uart.channel
        valid = channel is not None
        _record("uart.channel", valid, f"channel={channel!r}")
        if not valid:
            ok = False
    except Exception as e:
        _record("uart.channel", False, str(e))
        ok = False

    # params
    try:
        params = uart.params
        valid = isinstance(params, dict)
        _record("uart.params", valid, f"params={params!r}")
        if not valid:
            ok = False
    except Exception as e:
        _record("uart.params", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 4. String Representation
# ---------------------------------------------------------------------------
def test_string_repr():
    """Verify str(uart) contains 'UARTNet'."""
    print("\n" + "=" * 60)
    print("TEST: String Representation")
    print("=" * 60)

    ok = True
    from lager import Net, NetType
    uart = Net.get(UART_NET, type=NetType.UART)

    try:
        s = str(uart)
        has_marker = "UARTNet" in s
        _record("str(uart) contains 'UARTNet'", has_marker, f"str={s!r}")
        if not has_marker:
            ok = False
    except Exception as e:
        _record("str(uart) contains 'UARTNet'", False, str(e))
        ok = False

    try:
        r = repr(uart)
        has_marker = "UARTNet" in r
        _record("repr(uart) contains 'UARTNet'", has_marker, f"repr={r!r}")
        if not has_marker:
            ok = False
    except Exception as e:
        _record("repr(uart) contains 'UARTNet'", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 5. Get Path
# ---------------------------------------------------------------------------
def test_get_path():
    """Verify get_path() returns a valid device path."""
    print("\n" + "=" * 60)
    print("TEST: Get Path")
    print("=" * 60)

    ok = True
    from lager import Net, NetType
    uart = Net.get(UART_NET, type=NetType.UART)

    try:
        path = uart.get_path()
        is_str = isinstance(path, str)
        is_dev = path.startswith("/dev/")
        non_empty = len(path) > 5
        _record("get_path() returns string", is_str, f"type={type(path).__name__}")
        _record("get_path() starts with /dev/", is_dev, f"path={path!r}")
        _record("get_path() non-empty", non_empty, f"len={len(path)}")
        if not (is_str and is_dev and non_empty):
            ok = False
    except Exception as e:
        _record("get_path()", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 6. Get Baudrate
# ---------------------------------------------------------------------------
def test_get_baudrate():
    """Verify get_baudrate() returns a positive integer."""
    print("\n" + "=" * 60)
    print("TEST: Get Baudrate")
    print("=" * 60)

    ok = True
    from lager import Net, NetType
    uart = Net.get(UART_NET, type=NetType.UART)

    try:
        baud = uart.get_baudrate()
        is_int = isinstance(baud, int)
        is_positive = baud > 0
        _record("get_baudrate() returns int", is_int, f"type={type(baud).__name__}")
        _record("get_baudrate() > 0", is_positive, f"baudrate={baud}")
        if not (is_int and is_positive):
            ok = False
    except Exception as e:
        _record("get_baudrate()", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 7. Get Config
# ---------------------------------------------------------------------------
def test_get_config():
    """Verify get_config() returns a dict with expected keys."""
    print("\n" + "=" * 60)
    print("TEST: Get Config")
    print("=" * 60)

    ok = True
    from lager import Net, NetType
    uart = Net.get(UART_NET, type=NetType.UART)

    try:
        config = uart.get_config()
        is_dict = isinstance(config, dict)
        _record("get_config() returns dict", is_dict,
                f"type={type(config).__name__}")
        if not is_dict:
            ok = False
        else:
            keys = list(config.keys())
            _record("get_config() has keys", len(keys) > 0,
                    f"keys={keys}")
            if len(keys) == 0:
                ok = False
    except Exception as e:
        _record("get_config()", False, str(e))
        ok = False

    return ok


# ---------------------------------------------------------------------------
# 8. Connect Default
# ---------------------------------------------------------------------------
def test_connect_default():
    """Verify connect() returns a serial.Serial with default settings."""
    print("\n" + "=" * 60)
    print("TEST: Connect Default")
    print("=" * 60)

    ok = True
    import serial
    from lager import Net, NetType
    uart = Net.get(UART_NET, type=NetType.UART)

    ser = None
    try:
        ser = uart.connect()
        is_serial = isinstance(ser, serial.Serial)
        _record("connect() returns serial.Serial", is_serial,
                f"type={type(ser).__name__}")
        if not is_serial:
            ok = False

        has_port = ser.port is not None and len(ser.port) > 0
        _record("serial.port is set", has_port, f"port={ser.port!r}")
        if not has_port:
            ok = False

        baud_valid = isinstance(ser.baudrate, int) and ser.baudrate > 0
        _record("serial.baudrate valid", baud_valid,
                f"baudrate={ser.baudrate}")
        if not baud_valid:
            ok = False
    except Exception as e:
        _record("connect() default", False, str(e))
        ok = False
    finally:
        if ser and hasattr(ser, 'close'):
            ser.close()

    return ok


# ---------------------------------------------------------------------------
# 9. Connect Custom Baudrate
# ---------------------------------------------------------------------------
def test_connect_custom_baudrate():
    """Verify connect() respects baudrate overrides."""
    print("\n" + "=" * 60)
    print("TEST: Connect Custom Baudrate")
    print("=" * 60)

    ok = True
    from lager import Net, NetType
    uart = Net.get(UART_NET, type=NetType.UART)

    # Test 9600
    ser = None
    try:
        ser = uart.connect(baudrate=9600)
        match = ser.baudrate == 9600
        _record("connect(baudrate=9600)", match, f"baudrate={ser.baudrate}")
        if not match:
            ok = False
    except Exception as e:
        _record("connect(baudrate=9600)", False, str(e))
        ok = False
    finally:
        if ser and hasattr(ser, 'close'):
            ser.close()

    # Test 115200
    ser = None
    try:
        ser = uart.connect(baudrate=115200)
        match = ser.baudrate == 115200
        _record("connect(baudrate=115200)", match, f"baudrate={ser.baudrate}")
        if not match:
            ok = False
    except Exception as e:
        _record("connect(baudrate=115200)", False, str(e))
        ok = False
    finally:
        if ser and hasattr(ser, 'close'):
            ser.close()

    return ok


# ---------------------------------------------------------------------------
# 10. Connect Parameters
# ---------------------------------------------------------------------------
def test_connect_parameters():
    """Verify connect() accepts bytesize, parity, stopbits overrides."""
    print("\n" + "=" * 60)
    print("TEST: Connect Parameters")
    print("=" * 60)

    ok = True
    import serial as serial_mod
    from lager import Net, NetType
    uart = Net.get(UART_NET, type=NetType.UART)

    # bytesize=7
    ser = None
    try:
        ser = uart.connect(bytesize=7)
        match = ser.bytesize == 7
        _record("connect(bytesize=7)", match, f"bytesize={ser.bytesize}")
        if not match:
            ok = False
    except Exception as e:
        _record("connect(bytesize=7)", False, str(e))
        ok = False
    finally:
        if ser and hasattr(ser, 'close'):
            ser.close()

    # parity='E' (even)
    ser = None
    try:
        ser = uart.connect(parity='E')
        match = ser.parity == 'E'
        _record("connect(parity='E')", match, f"parity={ser.parity!r}")
        if not match:
            ok = False
    except Exception as e:
        _record("connect(parity='E')", False, str(e))
        ok = False
    finally:
        if ser and hasattr(ser, 'close'):
            ser.close()

    # stopbits=2
    ser = None
    try:
        ser = uart.connect(stopbits=2)
        match = ser.stopbits == 2
        _record("connect(stopbits=2)", match, f"stopbits={ser.stopbits}")
        if not match:
            ok = False
    except Exception as e:
        _record("connect(stopbits=2)", False, str(e))
        ok = False
    finally:
        if ser and hasattr(ser, 'close'):
            ser.close()

    return ok


# ---------------------------------------------------------------------------
# 11. Flow Control
# ---------------------------------------------------------------------------
def test_flow_control():
    """Verify connect() accepts flow control overrides."""
    print("\n" + "=" * 60)
    print("TEST: Flow Control")
    print("=" * 60)

    ok = True
    from lager import Net, NetType
    uart = Net.get(UART_NET, type=NetType.UART)

    # xonxoff
    ser = None
    try:
        ser = uart.connect(xonxoff=True)
        match = ser.xonxoff is True
        _record("connect(xonxoff=True)", match, f"xonxoff={ser.xonxoff}")
        if not match:
            ok = False
    except Exception as e:
        _record("connect(xonxoff=True)", False, str(e))
        ok = False
    finally:
        if ser and hasattr(ser, 'close'):
            ser.close()

    # rtscts
    ser = None
    try:
        ser = uart.connect(rtscts=True)
        match = ser.rtscts is True
        _record("connect(rtscts=True)", match, f"rtscts={ser.rtscts}")
        if not match:
            ok = False
    except Exception as e:
        _record("connect(rtscts=True)", False, str(e))
        ok = False
    finally:
        if ser and hasattr(ser, 'close'):
            ser.close()

    # dsrdtr
    ser = None
    try:
        ser = uart.connect(dsrdtr=True)
        match = ser.dsrdtr is True
        _record("connect(dsrdtr=True)", match, f"dsrdtr={ser.dsrdtr}")
        if not match:
            ok = False
    except Exception as e:
        _record("connect(dsrdtr=True)", False, str(e))
        ok = False
    finally:
        if ser and hasattr(ser, 'close'):
            ser.close()

    return ok


# ---------------------------------------------------------------------------
# 12. Timeout Behavior
# ---------------------------------------------------------------------------
def test_timeout_behavior():
    """Verify read with timeout returns empty when no data available."""
    print("\n" + "=" * 60)
    print("TEST: Timeout Behavior")
    print("=" * 60)

    ok = True
    from lager import Net, NetType
    uart = Net.get(UART_NET, type=NetType.UART)

    ser = None
    try:
        ser = uart.connect(timeout=0.5)
        ser.reset_input_buffer()

        t0 = time.time()
        data = ser.read(10)
        elapsed = time.time() - t0

        is_empty = len(data) == 0
        _record("read with no data returns empty", is_empty,
                f"len={len(data)}, data={data!r}")
        if not is_empty:
            ok = False

        # Elapsed should be roughly the timeout value (0.5s +/- margin)
        in_range = 0.3 <= elapsed <= 1.5
        _record("read blocked ~0.5s then returned", in_range,
                f"elapsed={elapsed:.3f}s")
        if not in_range:
            ok = False
    except Exception as e:
        _record("timeout behavior", False, str(e))
        ok = False
    finally:
        if ser and hasattr(ser, 'close'):
            ser.close()

    return ok


# ---------------------------------------------------------------------------
# 13. Loopback Test
# ---------------------------------------------------------------------------
def test_loopback():
    """Write data and read it back via loopback (TX->RX jumper)."""
    print("\n" + "=" * 60)
    print("TEST: Loopback")
    print("=" * 60)

    ok = True
    from lager import Net, NetType
    uart = Net.get(UART_NET, type=NetType.UART)

    ser = None
    try:
        ser = uart.connect(baudrate=115200, timeout=1.0)
        ser.reset_input_buffer()
        ser.reset_output_buffer()

        test_msg = b"LAGER_LOOPBACK_TEST\r\n"
        ser.write(test_msg)
        ser.flush()
        time.sleep(0.1)

        if ser.in_waiting == 0:
            _record("loopback write+read", True,
                    "SKIPPED -- no loopback adapter detected (in_waiting=0)")
            return ok

        response = ser.read(len(test_msg))
        match = response == test_msg
        _record("loopback write+read", match,
                f"sent={test_msg!r}, recv={response!r}")
        if not match:
            ok = False
    except Exception as e:
        _record("loopback write+read", False, str(e))
        ok = False
    finally:
        if ser and hasattr(ser, 'close'):
            ser.close()

    return ok


# ---------------------------------------------------------------------------
# 14. Large Data Transfer
# ---------------------------------------------------------------------------
def test_large_data_transfer():
    """Write and read back 1024 bytes via loopback."""
    print("\n" + "=" * 60)
    print("TEST: Large Data Transfer")
    print("=" * 60)

    ok = True
    from lager import Net, NetType
    uart = Net.get(UART_NET, type=NetType.UART)

    ser = None
    try:
        ser = uart.connect(baudrate=115200, timeout=2.0)
        ser.reset_input_buffer()
        ser.reset_output_buffer()

        # Check for loopback with a small probe
        probe = b"\x55"
        ser.write(probe)
        ser.flush()
        time.sleep(0.05)
        if ser.in_waiting == 0:
            _record("large data transfer (1024 bytes)", True,
                    "SKIPPED -- no loopback adapter detected")
            return ok
        ser.read(ser.in_waiting)  # drain probe

        # Send 1024 bytes in chunks
        payload = bytes(range(256)) * 4  # 1024 bytes
        chunk_size = 256
        for i in range(0, len(payload), chunk_size):
            ser.write(payload[i:i + chunk_size])
            ser.flush()
            time.sleep(0.05)

        # Read back all data
        time.sleep(0.3)
        received = b""
        deadline = time.time() + 3.0
        while len(received) < len(payload) and time.time() < deadline:
            avail = ser.in_waiting
            if avail > 0:
                received += ser.read(avail)
            else:
                time.sleep(0.05)

        len_match = len(received) == len(payload)
        _record("large data length match", len_match,
                f"sent={len(payload)}, recv={len(received)}")
        if not len_match:
            ok = False

        if len_match:
            data_match = received == payload
            _record("large data content match", data_match,
                    f"first_diff={_first_diff(payload, received)}")
            if not data_match:
                ok = False
    except Exception as e:
        _record("large data transfer", False, str(e))
        ok = False
    finally:
        if ser and hasattr(ser, 'close'):
            ser.close()

    return ok


def _first_diff(a, b):
    """Return index of first differing byte, or -1 if equal."""
    for i in range(min(len(a), len(b))):
        if a[i] != b[i]:
            return f"index {i}: sent=0x{a[i]:02X} recv=0x{b[i]:02X}"
    if len(a) != len(b):
        return f"length mismatch at index {min(len(a), len(b))}"
    return "none"


# ---------------------------------------------------------------------------
# 15. Multiple Open/Close
# ---------------------------------------------------------------------------
def test_multiple_open_close():
    """Connect and close the serial port 5 times in sequence."""
    print("\n" + "=" * 60)
    print("TEST: Multiple Open/Close")
    print("=" * 60)

    ok = True
    from lager import Net, NetType
    uart = Net.get(UART_NET, type=NetType.UART)

    for i in range(5):
        ser = None
        try:
            ser = uart.connect(timeout=0.5)
            is_open = ser.is_open
            ser.close()
            is_closed = not ser.is_open
            passed = is_open and is_closed
            _record(f"open/close cycle {i + 1}/5", passed,
                    f"is_open={is_open}, is_closed={is_closed}")
            if not passed:
                ok = False
        except Exception as e:
            _record(f"open/close cycle {i + 1}/5", False, str(e))
            ok = False
            if ser and hasattr(ser, 'close'):
                try:
                    ser.close()
                except Exception:
                    pass

    return ok


# ---------------------------------------------------------------------------
# 16. Buffer Reset
# ---------------------------------------------------------------------------
def test_buffer_reset():
    """Verify reset_input_buffer and reset_output_buffer do not error."""
    print("\n" + "=" * 60)
    print("TEST: Buffer Reset")
    print("=" * 60)

    ok = True
    from lager import Net, NetType
    uart = Net.get(UART_NET, type=NetType.UART)

    ser = None
    try:
        ser = uart.connect(timeout=0.5)

        ser.reset_input_buffer()
        _record("reset_input_buffer()", True)
    except Exception as e:
        _record("reset_input_buffer()", False, str(e))
        ok = False

    try:
        if ser and ser.is_open:
            ser.reset_output_buffer()
            _record("reset_output_buffer()", True)
    except Exception as e:
        _record("reset_output_buffer()", False, str(e))
        ok = False

    if ser and hasattr(ser, 'close'):
        ser.close()

    return ok


# ---------------------------------------------------------------------------
# 17. Error: Invalid Baudrate
# ---------------------------------------------------------------------------
def test_invalid_baudrate():
    """Verify connect(baudrate=-1) raises an exception."""
    print("\n" + "=" * 60)
    print("TEST: Error -- Invalid Baudrate")
    print("=" * 60)

    ok = True
    from lager import Net, NetType
    uart = Net.get(UART_NET, type=NetType.UART)

    ser = None
    try:
        ser = uart.connect(baudrate=-1)
        # If we get here without error, that is a failure
        _record("connect(baudrate=-1) raises", False,
                "no exception raised")
        ok = False
    except (ValueError, OSError, serial.SerialException) as e:
        _record("connect(baudrate=-1) raises", True,
                f"{type(e).__name__}: {e}")
    except Exception as e:
        # Any other exception is still acceptable -- the point is it does not succeed
        _record("connect(baudrate=-1) raises", True,
                f"unexpected type {type(e).__name__}: {e}")
    finally:
        if ser and hasattr(ser, 'close'):
            try:
                ser.close()
            except Exception:
                pass

    return ok


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    print("=" * 60)
    print(f"UART Comprehensive Test -- net: {UART_NET}")
    print("=" * 60)

    tests = [
        ("Imports",                 test_imports),
        ("Net.get",                 test_net_get),
        ("Properties",              test_properties),
        ("String Repr",             test_string_repr),
        ("Get Path",                test_get_path),
        ("Get Baudrate",            test_get_baudrate),
        ("Get Config",              test_get_config),
        ("Connect Default",         test_connect_default),
        ("Connect Custom Baudrate", test_connect_custom_baudrate),
        ("Connect Parameters",      test_connect_parameters),
        ("Flow Control",            test_flow_control),
        ("Timeout Behavior",        test_timeout_behavior),
        ("Loopback",                test_loopback),
        ("Large Data Transfer",     test_large_data_transfer),
        ("Multiple Open/Close",     test_multiple_open_close),
        ("Buffer Reset",            test_buffer_reset),
        ("Invalid Baudrate",        test_invalid_baudrate),
    ]

    test_results = []
    for name, test_fn in tests:
        try:
            passed = test_fn()
            test_results.append((name, passed))
        except Exception as e:
            print(f"\nUNEXPECTED ERROR in {name}: {e}")
            traceback.print_exc()
            test_results.append((name, False))

    # Summary
    print("\n" + "=" * 60)
    print("TEST SUMMARY")
    print("=" * 60)

    passed_count = sum(1 for _, p in test_results if p)
    total_count = len(test_results)

    for name, p in test_results:
        status = "PASS" if p else "FAIL"
        print(f"  [{status}] {name}")

    print(f"\nTotal: {passed_count}/{total_count} test groups passed")

    # Detailed sub-test summary
    sub_passed = sum(1 for _, p, _ in _results if p)
    sub_total = len(_results)
    sub_failed = sub_total - sub_passed
    print(f"Sub-tests: {sub_passed}/{sub_total} passed", end="")
    if sub_failed > 0:
        print(f" ({sub_failed} failed)")
        print("\nFailed sub-tests:")
        for name, p, detail in _results:
            if not p:
                print(f"  FAIL: {name} -- {detail}")
    else:
        print()

    return 0 if passed_count == total_count else 1


if __name__ == "__main__":
    import serial  # noqa: F811 -- needed for exception type in test_invalid_baudrate
    sys.exit(main())
