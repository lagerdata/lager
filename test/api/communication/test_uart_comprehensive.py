#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
UART Python API tests against the CI UART peer.

Run with: lager python test/api/communication/test_uart_comprehensive.py --box <BOX>

Prerequisites:
- A UART net wired to a peer running test/assets/uart_ci_peer (115200 8N1,
  line based: PING -> PONG, ID? -> LAGER-UART-PEER v1, ECHO <text> -> <text>,
  COUNT? -> a counter). The peer echoes every line before it replies.

Every data check moves real bytes through the peer, so a port that opens but
carries nothing fails here. A peer that does not answer is a failure, not a
skip.

The port is always opened with hardware flow control off. rtscts and dsrdtr
hand RTS and DTR to the driver, and on a board with an auto-reset circuit
(the ESP32 DevKit peer) those lines drive the chip's EN and boot pins: the
peer is held in reset, and every later test on the bench loses it.

Environment:
  UART_NET               the peer's net (default ESP_UART)
  LAGER_BENCH_MAX_SKIPS  more skips than this is a failure
"""
import os
import sys
import time

UART_NET = os.environ.get("UART_NET", "ESP_UART")
PEER_BAUD = 115200
PEER_ID = "LAGER-UART-PEER v1"
# 60 characters round-trip cleanly. Longer lines come back corrupted on the
# CI peer (bit errors from about 100 characters, framing garbage at 200), so
# this stays well inside what the peer's link has carried.
LONG_TEXT = ("lager-uart-api-" * 4)[:60]

_results = []
_skipped = []


def _record(name, passed, detail=""):
    """Record a sub-test result."""
    _results.append((name, passed, detail))
    status = "PASS" if passed else "FAIL"
    msg = f"  {status}: {name}"
    if detail:
        msg += f" -- {detail}"
    print(msg)


def _section(title):
    print("\n" + "=" * 60)
    print(f"TEST: {title}")
    print("=" * 60)


def _uart():
    from lager import Net, NetType
    return Net.get(UART_NET, type=NetType.UART)


def _lines(ser, want, deadline_s=2.0):
    """Read up to `want` non-empty lines, stripped, before the deadline."""
    out = []
    end = time.time() + deadline_s
    while len(out) < want and time.time() < end:
        raw = ser.readline()
        if raw:
            text = raw.decode("utf-8", "replace").strip()
            if text:
                out.append(text)
    return out


def _ask(ser, command):
    """Send one command line; return the peer's (echo, reply)."""
    ser.reset_input_buffer()
    ser.write(command.encode() + b"\n")
    ser.flush()
    got = _lines(ser, 2)
    echo = got[0] if got else None
    reply = got[1] if len(got) > 1 else None
    return echo, reply


def _close(ser):
    if ser is not None:
        try:
            ser.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# The net
# ---------------------------------------------------------------------------
def test_net():
    _section("Net")
    from lager.protocols.uart.uart_net import UARTNet
    uart = _uart()
    _record("Net.get returns a UARTNet", isinstance(uart, UARTNet), type(uart).__name__)
    _record("its name is the net asked for", uart.name == UART_NET, f"name={uart.name!r}")
    _record("str() names the class", "UARTNet" in str(uart), str(uart))
    path = uart.get_path()
    _record("get_path() is a /dev path", isinstance(path, str) and path.startswith("/dev/"),
            f"path={path!r}")
    _record("get_baudrate() is the peer's rate", uart.get_baudrate() == PEER_BAUD,
            f"baudrate={uart.get_baudrate()!r}")
    config = uart.get_config()
    _record("get_config() is a dict", isinstance(config, dict), type(config).__name__)


# ---------------------------------------------------------------------------
# Opening the port
# ---------------------------------------------------------------------------
def test_connect():
    _section("Connect")
    import serial
    uart = _uart()

    ser = None
    try:
        ser = uart.connect(timeout=1.0)
        _record("connect() returns an open serial.Serial",
                isinstance(ser, serial.Serial) and ser.is_open, type(ser).__name__)
        _record("it opens at the net's baudrate", ser.baudrate == PEER_BAUD,
                f"baudrate={ser.baudrate}")
        _record("hardware flow control is off",
                ser.rtscts is False and ser.dsrdtr is False,
                f"rtscts={ser.rtscts}, dsrdtr={ser.dsrdtr}")

        # The port is opened exclusively: a second opener fails fast instead
        # of interleaving reads with the first.
        second = None
        try:
            second = uart.connect(timeout=0.5)
            _record("a second open of the same port is refused", False,
                    "the second open succeeded")
        except (serial.SerialException, OSError) as e:
            _record("a second open of the same port is refused", True, type(e).__name__)
        finally:
            _close(second)
    except Exception as e:
        _record("connect()", False, f"{type(e).__name__}: {e}")
    finally:
        _close(ser)

    # Each override reaches the port. Nothing is sent at these settings.
    overrides = [
        ({"baudrate": 9600}, "baudrate", 9600),
        ({"bytesize": 7}, "bytesize", 7),
        ({"parity": "E"}, "parity", "E"),
        ({"stopbits": 2}, "stopbits", 2),
        ({"xonxoff": True}, "xonxoff", True),
    ]
    for kwargs, attr, want in overrides:
        ser = None
        label = ", ".join(f"{k}={v!r}" for k, v in kwargs.items())
        try:
            ser = uart.connect(timeout=0.5, **kwargs)
            got = getattr(ser, attr)
            _record(f"connect({label})", got == want, f"{attr}={got!r}")
        except Exception as e:
            _record(f"connect({label})", False, f"{type(e).__name__}: {e}")
        finally:
            _close(ser)

    ser = None
    try:
        for i in range(5):
            ser = uart.connect(timeout=0.5)
            opened = ser.is_open
            ser.close()
            if not opened or ser.is_open:
                _record("open and close 5 times", False, f"cycle {i + 1}")
                break
        else:
            _record("open and close 5 times", True)
    except Exception as e:
        _record("open and close 5 times", False, f"{type(e).__name__}: {e}")
    finally:
        _close(ser)

    ser = None
    try:
        ser = uart.connect(baudrate=-1)
        _record("connect(baudrate=-1) is refused", False, "the port opened")
    except (ValueError, serial.SerialException) as e:
        _record("connect(baudrate=-1) is refused", True, type(e).__name__)
    except Exception as e:
        _record("connect(baudrate=-1) is refused", False,
                f"wrong exception {type(e).__name__}: {e}")
    finally:
        _close(ser)


# ---------------------------------------------------------------------------
# Data through the peer
# ---------------------------------------------------------------------------
def test_peer_round_trip():
    _section("Data through the peer")
    uart = _uart()
    ser = None
    try:
        ser = uart.connect(baudrate=PEER_BAUD, timeout=0.5)
        time.sleep(0.2)

        echo, reply = _ask(ser, "ID?")
        _record("ID? names the CI peer", reply == PEER_ID, f"echo={echo!r}, reply={reply!r}")
        if reply != PEER_ID:
            print(f"  INFO: no peer on {UART_NET}; the data checks below fail with it")

        echo, reply = _ask(ser, "PING")
        _record("the peer echoes the line it got", echo == "PING", f"echo={echo!r}")
        _record("PING gets PONG", reply == "PONG", f"reply={reply!r}")

        echo, reply = _ask(ser, "ECHO " + LONG_TEXT)
        _record("a 60-character ECHO comes back whole", reply == LONG_TEXT,
                f"len={len(reply or '')}")

        _ask(ser, "RESET")
        _, first = _ask(ser, "COUNT?")
        _, second = _ask(ser, "COUNT?")
        ok = first is not None and second is not None and first.isdigit() and second.isdigit() \
            and int(second) == int(first) + 1
        _record("COUNT? counts up by one", ok, f"{first!r} then {second!r}")

        # The reset drops what has arrived but not been read.
        ser.write(b"PING\n")
        ser.flush()
        time.sleep(0.3)
        waiting = ser.in_waiting
        ser.reset_input_buffer()
        _record("reset_input_buffer() drops unread input",
                waiting > 0 and ser.in_waiting == 0,
                f"in_waiting {waiting} then {ser.in_waiting}")

        # The peer sends nothing unprompted: a read waits out its timeout.
        t0 = time.time()
        data = ser.read(10)
        elapsed = time.time() - t0
        _record("a read with nothing sent returns empty", data == b"", f"data={data!r}")
        _record("and waits about the 0.5 s timeout", 0.3 <= elapsed <= 1.5,
                f"elapsed={elapsed:.2f}s")
    except Exception as e:
        _record("peer round trip", False, f"{type(e).__name__}: {e}")
    finally:
        _close(ser)


def main():
    print("=" * 60)
    print(f"UART API Test -- net: {UART_NET}")
    print("=" * 60)

    for test in (test_net, test_connect, test_peer_round_trip):
        try:
            test()
        except Exception as e:
            _record(test.__name__, False, f"{type(e).__name__}: {e}")

    total = len(_results)
    passed = sum(1 for _, p, _ in _results if p)
    failed = total - passed
    skipped = len(_skipped)
    print(f"\nRESULTS: {passed}/{total} passed, {failed} failed, {skipped} skipped")
    max_skips = os.environ.get("LAGER_BENCH_MAX_SKIPS", "")
    if max_skips.isdigit() and skipped > int(max_skips):
        print(f"FAIL: {skipped} checks skipped, more than the {max_skips} allowed "
              f"(LAGER_BENCH_MAX_SKIPS)")
        return 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
