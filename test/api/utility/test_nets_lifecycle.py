# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

# test_nets_lifecycle.py
# Run with: lager python test/api/utility/test_nets_lifecycle.py --box <BOX_NAME>
#
# One scratch net through its whole life with the Net API: add, rename,
# describe (purpose, notes, tags), delete. After each step the net list and
# the box's :9000 /status must agree with what the API just did.
#
# The scratch net is a uart net on /dev/null, which names no instrument, so it
# cannot reach any hardware. It is deleted in a `finally`, and the last check
# is that every other net on the box is exactly as it was before the test.
#
# Environment:
#   SCRATCH_NET  the scratch net's name (default ci_scratch_<pid>). It must
#                start with "ci_scratch_": the test deletes a leftover by that
#                name, so it refuses (exit 2) any name a real net could have.

import copy
import os
import sys

BOX_API = "http://127.0.0.1:9000"
PREFIX = "ci_scratch_"
ROLE = "uart"

_results = []

def _record(name, passed, detail=""):
    _results.append((name, passed, detail))
    status = "PASS" if passed else "FAIL"
    msg = f"  {status}: {name}"
    if detail:
        msg += f" -- {detail}"
    print(msg)

def _status_nets():
    """{name: net} from the box's own /status."""
    import requests
    resp = requests.get(BOX_API + "/status", timeout=10)
    return {n.get("name"): n for n in resp.json().get("nets", [])}

def _saved_names():
    from lager import Net
    return {n.get("name") for n in Net.list_saved()}

def _others(skip):
    """Every saved net except the scratch names, as stored."""
    from lager import Net
    out = []
    for n in Net.get_local_nets():
        if n.get("name") in skip:
            continue
        n = copy.deepcopy(n)
        n.pop("live_path", None)
        out.append(n)
    return sorted(out, key=lambda n: (str(n.get("name")), str(n.get("role"))))

def main():
    from lager import Net

    name = os.environ.get("SCRATCH_NET") or f"{PREFIX}{os.getpid()}"
    if not name.startswith(PREFIX):
        print(f"Refusing SCRATCH_NET={name!r}: it must start with {PREFIX!r}, so this "
              f"test can never delete a real net.")
        return 2
    renamed = name + "_renamed"
    scratch = (name, renamed)

    print("=== Nets Lifecycle Test ===\n")
    print(f"  INFO: scratch net {name!r}")

    # A leftover from a killed run.
    for n in scratch:
        if Net.delete_local_net(n, ROLE):
            print(f"  INFO: removed a leftover {n!r}")

    before = _others(scratch)
    print(f"  INFO: {len(before)} other nets on the box")

    try:
        # add
        Net.save_local_net({
            "name": name,
            "role": ROLE,
            "instrument": "Unknown_UART_Device",
            "pin": "/dev/null",
            "device_path": "/dev/null",
        })
        _record("add: the net is listed", name in _saved_names())
        _record("add: /status lists it", name in _status_nets())

        # rename
        changed = Net.rename_local_net(name, renamed)
        names = _saved_names()
        _record("rename: reports a change", changed is True, f"returned {changed!r}")
        _record("rename: the old name is gone", name not in names)
        _record("rename: the new name is listed", renamed in names)
        status = _status_nets()
        _record("rename: /status agrees", renamed in status and name not in status)

        # describe
        rec = next((n for n in Net.get_local_nets() if n.get("name") == renamed), None)
        if rec is None:
            _record("describe: the renamed net is found", False)
        else:
            rec = copy.deepcopy(rec)
            rec.update({
                "purpose": "scratch net for the nets lifecycle test",
                "notes": "deleted by the test",
                "tags": ["ci", "scratch"],
            })
            Net.save_local_net(rec)
            got = _status_nets().get(renamed, {})
            _record("describe: /status shows the purpose",
                    got.get("purpose") == "scratch net for the nets lifecycle test",
                    f"purpose={got.get('purpose')!r}")
            _record("describe: /status shows the notes",
                    got.get("notes") == "deleted by the test", f"notes={got.get('notes')!r}")
            _record("describe: /status shows the tags",
                    got.get("tags") == ["ci", "scratch"], f"tags={got.get('tags')!r}")
            count = sum(1 for n in Net.get_local_nets() if n.get("name") == renamed)
            _record("describe: the net is not duplicated", count == 1, f"{count} records")

        # delete
        deleted = Net.delete_local_net(renamed, ROLE)
        _record("delete: reports a change", deleted is True, f"returned {deleted!r}")
        _record("delete: the net is gone", renamed not in _saved_names())
        _record("delete: /status agrees", renamed not in _status_nets())
    except Exception as e:
        _record("lifecycle", False, f"{type(e).__name__}: {e}")
    finally:
        for n in scratch:
            Net.delete_local_net(n, ROLE)

    after = _others(scratch)
    _record("every other net is unchanged", after == before,
            f"{len(before)} before, {len(after)} after")
    _record("no scratch net is left", not (set(scratch) & _saved_names()))

    # Summary
    total = len(_results)
    passed = sum(1 for _, p, _ in _results if p)
    failed = total - passed
    print(f"\nRESULTS: {passed}/{total} passed, {failed} failed")
    return 1 if failed else 0

if __name__ == "__main__":
    sys.exit(main())
