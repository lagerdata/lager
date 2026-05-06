# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

# test_custom_binaries.py
# Run with: lager python test/api/utility/test_custom_binaries.py --box <BOX_NAME>

import sys

_results = []

def _record(name, passed, detail=""):
    _results.append((name, passed, detail))
    status = "PASS" if passed else "FAIL"
    msg = f"  {status}: {name}"
    if detail:
        msg += f" -- {detail}"
    print(msg)

def main():
    from lager.binaries import run_custom_binary, list_binaries, BinaryNotFoundError

    print("=== Custom Binaries Test ===\n")

    # Test 1: list_binaries returns a list
    binaries = []
    try:
        binaries = list_binaries()
        _record("list_returns_list", isinstance(binaries, list),
                f"type={type(binaries).__name__}")
        _record("list_count", True, f"found {len(binaries)} binaries")
        if binaries:
            for b in binaries:
                print(f"    - {b}")
    except Exception as e:
        _record("list_binaries", False, str(e))

    # Test 2: Missing binary raises BinaryNotFoundError
    try:
        run_custom_binary('nonexistent_binary_xyz', '--help')
        _record("missing_binary_raises", False, "no exception raised")
    except BinaryNotFoundError:
        _record("missing_binary_raises", True, "BinaryNotFoundError raised")
    except Exception as e:
        _record("missing_binary_raises", False, f"wrong exception: {type(e).__name__}: {e}")

    # Test 3: Run existing binary (if available)
    if 'test_tool' in binaries:
        try:
            result = run_custom_binary('test_tool')
            _record("run_existing_binary", result is not None and result.returncode == 0,
                    f"returncode={result.returncode if result else 'None'}")
        except Exception as e:
            _record("run_existing_binary", False, str(e))

        # Test 4: Pass arguments to binary
        try:
            result = run_custom_binary('test_tool', 'arg1', 'arg2', 'arg3')
            ok = result is not None and result.returncode == 0
            _record("binary_with_args", ok,
                    f"returncode={result.returncode if result else 'None'}")
        except Exception as e:
            _record("binary_with_args", False, str(e))
    else:
        _record("run_existing_binary", True, "no test_tool installed (skip)")

    # Summary
    total = len(_results)
    passed = sum(1 for _, p, _ in _results if p)
    failed = total - passed
    print(f"\nRESULTS: {passed}/{total} passed, {failed} failed")
    return 1 if failed else 0

if __name__ == "__main__":
    sys.exit(main())
