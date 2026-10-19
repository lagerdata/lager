# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

# test_list_nets.py
# Run with: lager python test/api/utility/test_list_nets.py --box <BOX_NAME>

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
    from lager import Net

    print("=== List Saved Nets Test ===\n")

    # Test 1: list_saved returns a list
    nets = Net.list_saved()
    _record("returns_list", isinstance(nets, list),
            f"type={type(nets).__name__}")

    # Test 2: Non-empty (any configured box should have nets)
    _record("non_empty", len(nets) > 0, f"found {len(nets)} nets")

    # Test 3: Each net has 'name' and 'role' keys
    if nets:
        all_have_keys = all(
            isinstance(n, dict) and 'name' in n and 'role' in n
            for n in nets
        )
        _record("nets_have_name_and_role", all_have_keys)

        for n in nets:
            name = n.get('name', 'Unknown')
            role = n.get('role', 'Unknown')
            print(f"    {name} ({role})")
    else:
        _record("nets_have_name_and_role", False, "no nets to check")

    # Test 4: the nets this box is known to carry are listed, with the right
    # role. "Non-empty" alone passes for a registry that lost every net but
    # one; EXPECT_NETS names the ones that matter, as name:role pairs.
    expected = [e.strip() for e in os.environ.get("EXPECT_NETS", "").split(",") if e.strip()]
    by_name = {n.get("name"): n.get("role") for n in nets if isinstance(n, dict)}
    for entry in expected:
        name, _, role = entry.partition(":")
        got = by_name.get(name)
        if got is None:
            _record(f"expected net {name}", False, "not in list_saved()")
        elif role and got != role:
            _record(f"expected net {name}", False, f"role {got!r}, expected {role!r}")
        else:
            _record(f"expected net {name}", True, f"role {got!r}")

    # Summary
    total = len(_results)
    passed = sum(1 for _, p, _ in _results if p)
    failed = total - passed
    print(f"\nRESULTS: {passed}/{total} passed, {failed} failed")
    return 1 if failed else 0

if __name__ == "__main__":
    sys.exit(main())
