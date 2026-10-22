# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

# test_box_info.py
# Run with: lager python test/api/utility/test_box_info.py --box <BOX_NAME>
#
# What the box says about itself, read from inside the box: the box name the
# CLI hands a script, the :9000 /hello and /status answers, the version and
# ref files, the saved nets, and the lock this very script runs under.
#
# Instruments are left out on purpose: the instrument scan writes a G-code
# handshake to candidate arm ports, and the deployment suite already lists
# them.
#
# Environment (each one is optional; an unset one skips its checks, and
# LAGER_BENCH_MAX_SKIPS turns a skip into a failure):
#   EXPECT_BOX          the box name `lager python --box` was given
#   EXPECT_VERSION      the lager version the box must report, e.g. 0.59.0
#   EXPECT_SHA          the commit the box must be on (its ref is <sha>@<short sha>)

import os
import sys
from datetime import datetime, timezone

BOX_API = "http://127.0.0.1:9000"
_results = []
_skipped = []

def _record(name, passed, detail=""):
    _results.append((name, passed, detail))
    status = "PASS" if passed else "FAIL"
    msg = f"  {status}: {name}"
    if detail:
        msg += f" -- {detail}"
    print(msg)

def _skip(name, reason):
    _skipped.append(name)
    print(f"  SKIP: {name} -- {reason}")

def _get(path):
    import requests
    resp = requests.get(BOX_API + path, timeout=10)
    return resp.status_code, resp.json()

def _age_seconds(stamp):
    when = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).total_seconds()

def main():
    from lager import Net, get_saved_nets

    print("=== Box Info Test ===\n")

    # The box name the CLI passes to the script.
    expect_box = os.environ.get("EXPECT_BOX", "")
    box = os.environ.get("LAGER_BOX", "")
    if expect_box:
        _record("LAGER_BOX is the box asked for", box == expect_box,
                f"LAGER_BOX={box!r}")
    else:
        _skip("LAGER_BOX is the box asked for", "EXPECT_BOX not set")

    # /hello
    try:
        code, body = _get("/hello")
        _record("/hello answers", code == 200 and body.get("service") == "lager-python-box",
                f"HTTP {code}, service={body.get('service')!r}")
    except Exception as e:
        _record("/hello answers", False, f"{type(e).__name__}: {e}")

    # /status, against the files it is read from
    status = {}
    try:
        code, status = _get("/status")
        _record("/status answers", code == 200 and isinstance(status, dict), f"HTTP {code}")
    except Exception as e:
        _record("/status answers", False, f"{type(e).__name__}: {e}")

    version_file = ""
    try:
        with open("/etc/lager/version") as f:
            version_file = f.read().strip().split("|", 1)[0]
    except OSError as e:
        print(f"  INFO: /etc/lager/version unreadable: {e}")
    _record("/status version matches /etc/lager/version",
            bool(version_file) and status.get("version") == version_file,
            f"status={status.get('version')!r}, file={version_file!r}")

    expect_version = os.environ.get("EXPECT_VERSION", "")
    if expect_version:
        _record("box reports the expected version", status.get("version") == expect_version,
                f"version={status.get('version')!r}, expected {expect_version!r}")
    else:
        _skip("box reports the expected version", "EXPECT_VERSION not set")

    ref = status.get("ref") or ""
    expect_sha = os.environ.get("EXPECT_SHA", "")
    if expect_sha:
        _record("box ref is the expected commit", ref.startswith(expect_sha + "@"),
                f"ref={ref!r}")
    else:
        _skip("box ref is the expected commit", "EXPECT_SHA not set")

    # The saved nets, three ways: the Net API, the raw file and /status.
    api_names = sorted(n.get("name", "") for n in Net.list_saved())
    file_names = sorted(n.get("name", "") for n in get_saved_nets())
    status_names = sorted(n.get("name", "") for n in status.get("nets", []))
    _record("Net.list_saved() matches saved_nets.json", api_names == file_names,
            f"{len(api_names)} vs {len(file_names)} nets")
    _record("/status lists the same nets", status_names == api_names,
            f"{len(status_names)} vs {len(api_names)} nets")

    # `lager python` runs this script under a lock: its own auto-lock, or one
    # the caller's CI job already holds. Either way /lock must show a lock that
    # expires on its own and has not outlived its TTL (the box reaps it then).
    try:
        code, lock = _get("/lock")
        print(f"  INFO: lock holder_type={lock.get('holder_type')!r}, "
              f"ttl_seconds={lock.get('ttl_seconds')!r}")
        _record("the box is locked while the script runs", code == 200 and lock.get("locked") is True,
                f"HTTP {code}, locked={lock.get('locked')!r}")
        ttl = lock.get("ttl_seconds")
        _record("the lock has a TTL", isinstance(ttl, (int, float)) and ttl > 0,
                f"ttl_seconds={ttl!r}")
        stamp = lock.get("last_heartbeat") or lock.get("locked_at") or ""
        age = _age_seconds(stamp) if stamp else None
        _record("the lock has not outlived its TTL",
                age is not None and isinstance(ttl, (int, float)) and age <= ttl,
                f"heartbeat age={age if age is None else round(age)}s, ttl={ttl!r}")
    except Exception as e:
        _record("the lock /lock reports", False, f"{type(e).__name__}: {e}")

    # Summary
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
