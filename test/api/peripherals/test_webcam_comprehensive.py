# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

# test_webcam_comprehensive.py
# Run with: lager python test_webcam_comprehensive.py --box <YOUR-BOX>

from lager import Net, NetType
import sys
import time

_results = []
def _record(name, passed, detail=""):
    _results.append((name, passed, detail))
    status = "PASS" if passed else "FAIL"
    msg = f"  {status}: {name}"
    if detail:
        msg += f" -- {detail}"
    print(msg)

def main():
    print("=== Webcam Comprehensive Test ===\n")

    net_name = 'webcam1'
    box_ip = '<BOX_IP>'  # Your box IP
    webcam = None

    try:
        webcam = Net.get(net_name, type=NetType.Webcam)
        _record("get_net", True, f"retrieved {net_name}")

        # Start stream
        result = webcam.start(box_ip=box_ip)
        has_url = isinstance(result, dict) and 'url' in result
        has_port = isinstance(result, dict) and 'port' in result
        _record("start_returns_dict", isinstance(result, dict), f"type={type(result).__name__}")
        _record("start_has_url_key", has_url, f"url={result.get('url', 'MISSING')}" if isinstance(result, dict) else "not a dict")
        _record("start_has_port_key", has_port, f"port={result.get('port', 'MISSING')}" if isinstance(result, dict) else "not a dict")
        time.sleep(1)

        # Check active
        is_active = webcam.is_active()
        _record("is_active_returns_bool", isinstance(is_active, bool), f"type={type(is_active).__name__}, value={is_active}")
        _record("is_active_after_start", bool(is_active), f"active={is_active}")
        time.sleep(1)

        # Get info
        try:
            info = webcam.get_info(box_ip=box_ip)
            _record("get_info", True, f"keys={list(info.keys()) if isinstance(info, dict) else type(info).__name__}")
        except Exception as e:
            _record("get_info", False, str(e))
        time.sleep(1)

        # Get URL
        try:
            url = webcam.get_url(box_ip=box_ip)
            _record("get_url_returns_string", isinstance(url, str), f"type={type(url).__name__}, value={url}")
        except Exception as e:
            _record("get_url_returns_string", False, str(e))
        time.sleep(1)

        # Stop stream
        stopped = webcam.stop()
        _record("stop", True, f"result={stopped}")

        # Verify inactive after stop
        is_active_after = webcam.is_active()
        _record("inactive_after_stop", not is_active_after, f"active={is_active_after}")
        time.sleep(1)

        # Test already-running behavior
        print("\n--- Already-running behavior ---")
        webcam.start(box_ip=box_ip)
        _record("restart_first", True)
        result2 = webcam.start(box_ip=box_ip)
        already = result2.get('already_running', None) if isinstance(result2, dict) else None
        _record("restart_already_running", already is not None, f"already_running={already}")

        # Final cleanup
        webcam.stop()
        _record("final_stop", True)

    except Exception as e:
        _record("unexpected_error", False, str(e))

    finally:
        if webcam is not None:
            try:
                webcam.stop()
            except Exception:
                pass

    # Summary
    passed = sum(1 for _, p, _ in _results if p)
    failed = sum(1 for _, p, _ in _results if not p)
    print(f"\n=== Summary: {passed} passed, {failed} failed out of {len(_results)} tests ===")
    return 0 if failed == 0 else 1

if __name__ == "__main__":
    sys.exit(main())
