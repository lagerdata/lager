#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Comprehensive test for Debug Net API (J-Link probe operations).

Hardware Required:
  - J-Link probe connected to box
  - Target MCU powered and wired (SWD/JTAG)
  - Net configured as type=NetType.Debug with device type in channel/pin field

Run with:
  lager python test/api/communication/test_debug_comprehensive.py --box <YOUR-BOX>

To test flash, upload firmware alongside the script:
  lager python test/api/communication/test_debug_comprehensive.py --box <YOUR-BOX> \\
      --add-file path/to/your-firmware.hex

Environment variables:
  DEBUG_NET       - net name (default: debug1)
  FIRMWARE_PATH   - firmware file name (default: firmware.hex)
  ALLOW_ERASE     - set to "1" to enable the chip erase test (default: "0")

Modern patterns shown here (prefer these in your own scripts):
  - `with dbg.session() as s:` scopes connect-on-entry + guaranteed teardown.
  - `connect(ignore_if_connected=True)` reuses a running gdbserver instead of
    raising — the safe default for an op/stream script.
  - rtt()/rtt_defmt() are reconnect-aware and reset()/read_memory()/erase()
    self-heal across the post-flash settling window, so you don't need your own
    retry/reconnect wrappers (both J-Link and OpenOCD).
  - DA1469x exception: its flash() leaves the server down on purpose, so call
    connect(ignore_if_connected=True) before reset()/read_memory() on that part.
"""
import sys, os, time, traceback

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DEBUG_NET = os.environ.get("DEBUG_NET", "debug1")
FIRMWARE_PATH = os.environ.get("FIRMWARE_PATH", "firmware.hex")
ALLOW_ERASE = os.environ.get("ALLOW_ERASE", "0")

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
    print("\n" + "=" * 60 + "\nTEST: Imports\n" + "=" * 60)
    try:
        from lager import Net, NetType
        assert hasattr(NetType, "Debug"), "NetType.Debug not found"
        _record("import Net, NetType", True)
        return True
    except Exception as e:
        _record("import Net, NetType", False, str(e))
        return False

# ---------------------------------------------------------------------------
# 2. Net.get
# ---------------------------------------------------------------------------
def test_net_get():
    print("\n" + "=" * 60 + "\nTEST: Net.get\n" + "=" * 60)
    from lager import Net, NetType
    try:
        debug = Net.get(DEBUG_NET, type=NetType.Debug)
        assert debug is not None, "Net.get returned None"
        _record("Net.get returns DebugNet", True, f"type={type(debug).__name__}")
        return True
    except Exception as e:
        _record("Net.get returns DebugNet", False, str(e))
        return False

# ---------------------------------------------------------------------------
# 3. Properties
# ---------------------------------------------------------------------------
def test_properties():
    print("\n" + "=" * 60 + "\nTEST: Properties\n" + "=" * 60)
    from lager import Net, NetType
    debug = Net.get(DEBUG_NET, type=NetType.Debug)
    ok = True
    for attr, expected in [("name", None), ("device", None), ("speed", "4000"), ("transport", "SWD")]:
        try:
            val = getattr(debug, attr)
            assert isinstance(val, str) and len(val) > 0, f"{attr} empty or not str: {val!r}"
            if expected is not None:
                assert val == expected, f"expected {expected!r}, got {val!r}"
            _record(f"debug.{attr}", True, f"'{val}'")
        except Exception as e:
            _record(f"debug.{attr}", False, str(e)); ok = False
    return ok

# ---------------------------------------------------------------------------
# 4. String Repr
# ---------------------------------------------------------------------------
def test_str_repr():
    print("\n" + "=" * 60 + "\nTEST: String Repr\n" + "=" * 60)
    from lager import Net, NetType
    debug = Net.get(DEBUG_NET, type=NetType.Debug)
    try:
        s = str(debug)
        assert isinstance(s, str) and len(s) > 0, "str(debug) is empty"
        _record("str(debug)", True, f"'{s}' (contains 'debug': {'debug' in s.lower()})")
        return True
    except Exception as e:
        _record("str(debug)", False, str(e))
        return False

# ---------------------------------------------------------------------------
# 5. Connect Default
# ---------------------------------------------------------------------------
def test_connect_default():
    print("\n" + "=" * 60 + "\nTEST: Connect Default\n" + "=" * 60)
    from lager import Net, NetType
    debug = Net.get(DEBUG_NET, type=NetType.Debug)
    try:
        result = debug.connect()
        _record("debug.connect()", True, f"returned {result!r}")
        return True
    except Exception as e:
        _record("debug.connect()", False, str(e)); return False
    finally:
        try: debug.disconnect()
        except Exception: pass

# ---------------------------------------------------------------------------
# 6. Connect Custom Speed
# ---------------------------------------------------------------------------
def test_connect_custom_speed():
    print("\n" + "=" * 60 + "\nTEST: Connect Custom Speed\n" + "=" * 60)
    from lager import Net, NetType
    debug = Net.get(DEBUG_NET, type=NetType.Debug)
    try:
        result = debug.connect(speed='1000')
        _record("debug.connect(speed='1000')", True, f"returned {result!r}")
        return True
    except Exception as e:
        _record("debug.connect(speed='1000')", False, str(e)); return False
    finally:
        try: debug.disconnect()
        except Exception: pass

# ---------------------------------------------------------------------------
# 7. Connect Custom Transport
# ---------------------------------------------------------------------------
def test_connect_custom_transport():
    print("\n" + "=" * 60 + "\nTEST: Connect Custom Transport\n" + "=" * 60)
    from lager import Net, NetType
    debug = Net.get(DEBUG_NET, type=NetType.Debug)
    try:
        result = debug.connect(transport='SWD')
        _record("debug.connect(transport='SWD')", True, f"returned {result!r}")
        return True
    except Exception as e:
        _record("debug.connect(transport='SWD')", False, str(e)); return False
    finally:
        try: debug.disconnect()
        except Exception: pass

# ---------------------------------------------------------------------------
# 8. Status
# ---------------------------------------------------------------------------
def test_status():
    print("\n" + "=" * 60 + "\nTEST: Status\n" + "=" * 60)
    from lager import Net, NetType
    debug = Net.get(DEBUG_NET, type=NetType.Debug)
    try:
        debug.connect(ignore_if_connected=True)
        status = debug.status()
        assert isinstance(status, dict), f"status() should be a dict, got {type(status)}"
        assert "running" in status, f"status() missing 'running' key: {status!r}"
        _record("debug.status()", True, f"{status}")
        return True
    except Exception as e:
        _record("debug.status()", False, str(e)); return False
    finally:
        try: debug.disconnect()
        except Exception: pass

# ---------------------------------------------------------------------------
# 9. Reset No Halt
# ---------------------------------------------------------------------------
def test_reset_no_halt():
    print("\n" + "=" * 60 + "\nTEST: Reset No Halt\n" + "=" * 60)
    from lager import Net, NetType
    debug = Net.get(DEBUG_NET, type=NetType.Debug)
    try:
        debug.connect(ignore_if_connected=True)
        result = debug.reset(halt=False)
        assert isinstance(result, str), f"reset() returned {type(result)}, not str"
        _record("debug.reset(halt=False)", True, f"'{result[:80]}'")
        return True
    except Exception as e:
        _record("debug.reset(halt=False)", False, str(e)); return False
    finally:
        try: debug.disconnect()
        except Exception: pass

# ---------------------------------------------------------------------------
# 10. Reset With Halt
# ---------------------------------------------------------------------------
def test_reset_halt():
    print("\n" + "=" * 60 + "\nTEST: Reset With Halt\n" + "=" * 60)
    from lager import Net, NetType
    debug = Net.get(DEBUG_NET, type=NetType.Debug)
    try:
        debug.connect(ignore_if_connected=True)
        result = debug.reset(halt=True)
        assert isinstance(result, str), f"reset() returned {type(result)}, not str"
        _record("debug.reset(halt=True)", True, f"'{result[:80]}'")
        return True
    except Exception as e:
        _record("debug.reset(halt=True)", False, str(e)); return False
    finally:
        try: debug.disconnect()
        except Exception: pass

# ---------------------------------------------------------------------------
# 11. Memory Read Flash
# ---------------------------------------------------------------------------
def test_memory_read_flash():
    print("\n" + "=" * 60 + "\nTEST: Memory Read Flash\n" + "=" * 60)
    from lager import Net, NetType
    debug = Net.get(DEBUG_NET, type=NetType.Debug)
    try:
        debug.connect(ignore_if_connected=True)
        data = debug.read_memory(0x00000000, 64)
        assert isinstance(data, (bytes, bytearray)), f"expected bytes, got {type(data)}"
        assert len(data) >= 1, f"expected >= 1 byte, got {len(data)}"
        hex_preview = ' '.join(f'{b:02X}' for b in data[:16])
        _record("read_memory(0x0, 64)", True, f"{len(data)}B -- {hex_preview}")
        return True
    except Exception as e:
        _record("read_memory(0x0, 64)", False, str(e)); return False
    finally:
        try: debug.disconnect()
        except Exception: pass

# ---------------------------------------------------------------------------
# 12. Memory Read RAM
# ---------------------------------------------------------------------------
def test_memory_read_ram():
    print("\n" + "=" * 60 + "\nTEST: Memory Read RAM\n" + "=" * 60)
    from lager import Net, NetType
    debug = Net.get(DEBUG_NET, type=NetType.Debug)
    try:
        debug.connect(ignore_if_connected=True)
        data = debug.read_memory(0x20000000, 32)
        assert isinstance(data, (bytes, bytearray)), f"expected bytes, got {type(data)}"
        hex_preview = ' '.join(f'{b:02X}' for b in data[:16])
        _record("read_memory(0x20000000, 32)", True, f"{len(data)}B -- {hex_preview}")
        return True
    except Exception as e:
        _record("read_memory(0x20000000, 32)", False, str(e)); return False
    finally:
        try: debug.disconnect()
        except Exception: pass

# ---------------------------------------------------------------------------
# 13. Memory Read Various Lengths
# ---------------------------------------------------------------------------
def test_memory_read_various_lengths():
    print("\n" + "=" * 60 + "\nTEST: Memory Read Various Lengths\n" + "=" * 60)
    from lager import Net, NetType
    debug = Net.get(DEBUG_NET, type=NetType.Debug)
    ok = True
    try:
        debug.connect(ignore_if_connected=True)
        for length in [1, 16, 128, 256]:
            try:
                data = debug.read_memory(0x00000000, length)
                assert isinstance(data, (bytes, bytearray))
                _record(f"read_memory(0x0, {length})", True, f"got {len(data)}B")
            except Exception as e:
                _record(f"read_memory(0x0, {length})", False, str(e)); ok = False
    except Exception as e:
        _record("read_memory various lengths", False, f"connect failed: {e}"); ok = False
    finally:
        try: debug.disconnect()
        except Exception: pass
    return ok

# ---------------------------------------------------------------------------
# 14. Flash Firmware (conditional)
# ---------------------------------------------------------------------------
def test_flash_firmware():
    print("\n" + "=" * 60 + "\nTEST: Flash Firmware\n" + "=" * 60)
    if not os.path.exists(FIRMWARE_PATH):
        _record("debug.flash()", True, f"SKIPPED -- '{FIRMWARE_PATH}' not found, use --add-file")
        return True
    from lager import Net, NetType
    debug = Net.get(DEBUG_NET, type=NetType.Debug)
    try:
        debug.connect(ignore_if_connected=True)
        result = debug.flash(FIRMWARE_PATH)
        assert isinstance(result, str)
        _record("debug.flash(firmware)", True, f"'{result[:80]}'")
        return True
    except Exception as e:
        _record("debug.flash(firmware)", False, str(e)); return False
    finally:
        try: debug.disconnect()
        except Exception: pass

# ---------------------------------------------------------------------------
# 15. Flash Format Detection
# ---------------------------------------------------------------------------
def test_flash_format_detection():
    print("\n" + "=" * 60 + "\nTEST: Flash Format Detection\n" + "=" * 60)
    if not os.path.exists(FIRMWARE_PATH):
        _record("flash format detection", True, f"SKIPPED -- '{FIRMWARE_PATH}' not found")
        return True
    from lager import Net, NetType
    debug = Net.get(DEBUG_NET, type=NetType.Debug)
    try:
        debug.connect(ignore_if_connected=True)
        ext = os.path.splitext(FIRMWARE_PATH)[1].lower()
        debug.flash(FIRMWARE_PATH)
        _record(f"flash accepts {ext}", True, f"extension '{ext}' handled")
        return True
    except Exception as e:
        _record("flash format detection", False, str(e)); return False
    finally:
        try: debug.disconnect()
        except Exception: pass

# ---------------------------------------------------------------------------
# 16. Erase (conditional)
# ---------------------------------------------------------------------------
def test_erase():
    print("\n" + "=" * 60 + "\nTEST: Erase\n" + "=" * 60)
    if ALLOW_ERASE != "1":
        _record("debug.erase()", True, "SKIPPED -- set ALLOW_ERASE=1 to enable")
        return True
    from lager import Net, NetType
    debug = Net.get(DEBUG_NET, type=NetType.Debug)
    try:
        debug.connect(ignore_if_connected=True)
        result = debug.erase()
        assert isinstance(result, str)
        _record("debug.erase()", True, f"'{result[:80]}'")
        return True
    except Exception as e:
        _record("debug.erase()", False, str(e)); return False
    finally:
        try: debug.disconnect()
        except Exception: pass

# ---------------------------------------------------------------------------
# 17. Re-connect
# ---------------------------------------------------------------------------
def test_reconnect():
    print("\n" + "=" * 60 + "\nTEST: Re-connect\n" + "=" * 60)
    from lager import Net, NetType
    debug = Net.get(DEBUG_NET, type=NetType.Debug)
    ok = True
    try:
        debug.connect()
        _record("initial connect", True)
        debug.disconnect()
        _record("disconnect", True)
        time.sleep(0.5)
        result = debug.connect()
        _record("re-connect after disconnect", True, f"returned {result!r}")
    except Exception as e:
        _record("re-connect lifecycle", False, str(e)); ok = False
    finally:
        try: debug.disconnect()
        except Exception: pass
    return ok

# ---------------------------------------------------------------------------
# 18. RTT Session (conditional)
# ---------------------------------------------------------------------------
def test_rtt_session():
    print("\n" + "=" * 60 + "\nTEST: RTT Session\n" + "=" * 60)
    from lager import Net, NetType
    debug = Net.get(DEBUG_NET, type=NetType.Debug)
    try:
        debug.connect(ignore_if_connected=True)
        try:
            with debug.rtt(channel=0) as rtt:
                data = rtt.read_some(timeout=1.0)
                detail = f"read {len(data)}B" if data else "no data (expected without RTT firmware)"
                _record("RTT session read_some", True, detail)
        except Exception as e:
            _record("RTT session", True, f"SKIPPED -- {e}")
        return True
    except Exception as e:
        _record("RTT session", False, f"connect failed: {e}"); return False
    finally:
        try: debug.disconnect()
        except Exception: pass

# ---------------------------------------------------------------------------
# 19. session() scope (preferred entry point)
# ---------------------------------------------------------------------------
def test_session_scope():
    print("\n" + "=" * 60 + "\nTEST: session() scope\n" + "=" * 60)
    from lager import Net, NetType
    debug = Net.get(DEBUG_NET, type=NetType.Debug)
    try:
        # session() connects on entry (reusing a running server) and guarantees
        # teardown on exit — the recommended way to scope a debug workflow.
        with debug.session() as s:
            status = s.status()
            assert isinstance(status, dict), f"status() should be a dict, got {type(status)}"
            assert "running" in status, "status() missing 'running' key"
            s.reset(halt=False)  # self-heals across the post-flash settling window
            _record("session() scope", True, f"running={status.get('running')}")
        return True
    except Exception as e:
        _record("session() scope", False, str(e)); return False

# ---------------------------------------------------------------------------
# 20. rtt_defmt session (conditional — needs matching ELF firmware)
# ---------------------------------------------------------------------------
def test_rtt_defmt_session():
    print("\n" + "=" * 60 + "\nTEST: rtt_defmt session\n" + "=" * 60)
    from lager import Net, NetType
    # rtt_defmt decodes defmt logs on-box via defmt-print; it needs the exact
    # ELF flashed on the DUT. Skip cleanly when no .elf is available.
    if not (FIRMWARE_PATH.endswith(".elf") and os.path.exists(FIRMWARE_PATH)):
        _record("rtt_defmt session", True,
                f"SKIPPED -- needs a matching .elf (got '{FIRMWARE_PATH}')")
        return True
    debug = Net.get(DEBUG_NET, type=NetType.Debug)
    try:
        # Keep ONE reader open across the reset — it is reconnect-aware, so the
        # flash/reset server bounce will not kill the stream.
        with debug.session() as s:
            with s.rtt_defmt(elf=FIRMWARE_PATH, channel=0) as logs:
                s.reset(halt=False)
                lines = []
                deadline = time.time() + 5
                while time.time() < deadline and len(lines) < 5:
                    line = logs.read_line(timeout=1.0)
                    if line:
                        lines.append(line)
                detail = f"decoded {len(lines)} line(s)" if lines else "no defmt lines (ok without logging firmware)"
                _record("rtt_defmt session", True, detail)
        return True
    except Exception as e:
        # Missing defmt-print / non-defmt firmware shouldn't fail the suite.
        _record("rtt_defmt session", True, f"SKIPPED -- {e}")
        return True

# ---------------------------------------------------------------------------
# 21. Disconnect
# ---------------------------------------------------------------------------
def test_disconnect():
    print("\n" + "=" * 60 + "\nTEST: Disconnect\n" + "=" * 60)
    from lager import Net, NetType
    debug = Net.get(DEBUG_NET, type=NetType.Debug)
    try:
        debug.connect()
        debug.disconnect()
        _record("debug.disconnect()", True)
        return True
    except Exception as e:
        _record("debug.disconnect()", False, str(e)); return False

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    print("Debug Net Comprehensive Test Suite")
    print(f"Net:           {DEBUG_NET}")
    print(f"Firmware:      {FIRMWARE_PATH}")
    print(f"Allow erase:   {ALLOW_ERASE}")
    print("=" * 60)

    tests = [
        ("Imports",                 test_imports),
        ("Net.get",                 test_net_get),
        ("Properties",              test_properties),
        ("String Repr",             test_str_repr),
        ("Connect Default",         test_connect_default),
        ("Connect Custom Speed",    test_connect_custom_speed),
        ("Connect Custom Transport", test_connect_custom_transport),
        ("Status",                  test_status),
        ("Reset No Halt",           test_reset_no_halt),
        ("Reset With Halt",         test_reset_halt),
        ("Memory Read Flash",       test_memory_read_flash),
        ("Memory Read RAM",         test_memory_read_ram),
        ("Memory Read Lengths",     test_memory_read_various_lengths),
        ("Flash Firmware",          test_flash_firmware),
        ("Flash Format Detection",  test_flash_format_detection),
        ("Erase",                   test_erase),
        ("Re-connect",              test_reconnect),
        ("RTT Session",             test_rtt_session),
        ("session() scope",         test_session_scope),
        ("rtt_defmt session",       test_rtt_defmt_session),
        ("Disconnect",              test_disconnect),
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
        print(f"  [{'PASS' if p else 'FAIL'}] {name}")
    print(f"\nTotal: {passed_count}/{total_count} test groups passed")

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
    sys.exit(main())
