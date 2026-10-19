# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

# test_custom_binaries.py
# Run with: lager python test/api/utility/test_custom_binaries.py --box <BOX_NAME>

import os
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
        # Informational: any count is valid, so it is printed rather than
        # recorded -- a check that cannot fail only inflates the pass count.
        print(f"  INFO: found {len(binaries)} binaries")
        # Count, not names. This runs in public CI, and the names under
        # CUSTOMER_BINARIES_PATH are whatever a customer uploaded via
        # `lager binaries add` -- a name is not ours to publish. Anyone
        # debugging a real box can still run `lager binaries list`.
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

    # Tests 3-4 need a binary on the box. The bench workflow uploads one that
    # echoes its arguments (`lager binaries add`) and names it here; by hand,
    # upload any binary that exits 0 and set TEST_BINARY_NAME.
    name = os.environ.get("TEST_BINARY_NAME", "test_tool")
    skipped = 0
    if name in binaries:
        # Test 3: Run existing binary
        try:
            result = run_custom_binary(name)
            _record("run_existing_binary", result is not None and result.returncode == 0,
                    f"returncode={result.returncode if result else 'None'}")
        except Exception as e:
            _record("run_existing_binary", False, str(e))

        # Test 4: Pass arguments to binary. An echoing binary must print them
        # back in order; any other binary only has to exit 0.
        try:
            result = run_custom_binary(name, 'arg1', 'arg2', 'arg3')
            ok = result is not None and result.returncode == 0
            detail = f"returncode={result.returncode if result else 'None'}"
            if ok and os.environ.get("TEST_BINARY_ECHOES") == "1":
                ok = "arg1 arg2 arg3" in (result.stdout or "")
                detail += f", stdout={(result.stdout or '').strip()!r}"
            _record("binary_with_args", ok, detail)
        except Exception as e:
            _record("binary_with_args", False, str(e))
    else:
        # Not a pass: nothing ran.
        skipped = 2
        print(f"  SKIP: run_existing_binary / binary_with_args -- no '{name}' binary on the box")

    # Summary
    total = len(_results)
    passed = sum(1 for _, p, _ in _results if p)
    failed = total - passed
    print(f"\nRESULTS: {passed}/{total} passed, {failed} failed, {skipped} skipped")
    max_skips = os.environ.get("LAGER_BENCH_MAX_SKIPS", "")
    if max_skips.isdigit() and skipped > int(max_skips):
        print(f"FAIL: {skipped} checks skipped, more than the {max_skips} allowed "
              f"(LAGER_BENCH_MAX_SKIPS)")
        return 1
    return 1 if failed else 0

if __name__ == "__main__":
    sys.exit(main())
