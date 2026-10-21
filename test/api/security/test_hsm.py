# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

# test_hsm.py
# Run with:
#   LAGER_HSM_PASSWORD=... lager python test_hsm.py --box <YOUR-BOX> --passenv LAGER_HSM_PASSWORD
#
# Optional environment:
#   HSM_NET                 net name (default hsm1)
#   HSM_SCRATCH_ID          free object ID for the test key (default 0x0F00)
#   LAGER_HSM_ALLOW_RESET=1 also reset the HSM, which ERASES EVERY KEY on it
#                           and restores the factory login (auth key 1,
#                           password "password")

import os
import sys

from lager import Net, NetType
from lager.exceptions import HsmBackendError

_results = []


def _record(name, passed, detail=""):
    _results.append((name, passed, detail))
    status = "PASS" if passed else "FAIL"
    msg = f"  {status}: {name}"
    if detail:
        msg += f" -- {detail}"
    print(msg)


def _raises(fn, text):
    try:
        fn()
    except HsmBackendError as e:
        return text in str(e), str(e)
    return False, "no error raised"


def main():
    print("=== HSM Test ===\n")

    net_name = os.environ.get("HSM_NET", "hsm1")
    scratch = int(os.environ.get("HSM_SCRATCH_ID", "0x0F00"), 0)
    password = os.environ.get("LAGER_HSM_PASSWORD")
    if not password:
        print("LAGER_HSM_PASSWORD is not set; pass it with --passenv LAGER_HSM_PASSWORD")
        return 1
    auth = {"auth_key_id": 1, "password": password}

    hsm = Net.get(net_name, type=NetType.Hsm)

    # --- Device ---
    try:
        info = hsm.info()
        _record("info_firmware", bool(info.get("firmware")), f"fw={info.get('firmware')}")
        _record("info_serial", isinstance(info.get("serial"), int), f"serial={info.get('serial')}")
        _record("echo", hsm.echo(b"lager") == b"lager")
    except Exception as e:
        _record("info", False, f"exception: {e}")
        return 1

    ok, detail = _raises(lambda: hsm.list_objects(auth_key_id=1, password="wrong-password"),
                         "Authentication failed")
    _record("wrong_password_refused", ok, detail)

    ok, detail = _raises(lambda: hsm.get_public_key(scratch, **auth), "No asymmetric-key")
    if not ok:
        _record("scratch_id_free", False, f"object 0x{scratch:04X} in use? {detail}")
        return 1

    # --- Key lifecycle ---
    created = False
    try:
        result = hsm.generate_key(scratch, "ec-p256", label="lager-test", **auth)
        created = True
        _record("generate", result["id"] == scratch, f"id=0x{result['id']:04X}")
        pem = hsm.get_public_key(scratch, **auth)
        _record("pubkey_matches", pem == result["public_key_pem"])

        listed = [o for o in hsm.list_objects(type="asymmetric-key", **auth) if o["id"] == scratch]
        _record("listed", len(listed) == 1 and listed[0]["algorithm"] == "ec-p256")

        message = b"lager hsm api test"
        signature = hsm.sign(scratch, message, "ecdsa-sha256", **auth)
        _record("sign_returns_der", signature[:1] == b"\x30", f"{len(signature)} bytes")
        try:
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import ec
            public_key = serialization.load_pem_public_key(pem.encode())
            public_key.verify(signature, message, ec.ECDSA(hashes.SHA256()))
            _record("signature_verifies", True)
        except Exception as e:
            _record("signature_verifies", False, f"{e}")

        with hsm.session(**auth) as session:
            _record("session_context", session is not None)

        ok, detail = _raises(lambda: hsm.generate_key(scratch, "ec-p256", **auth),
                             "already exists")
        _record("duplicate_id_refused", ok, detail)
    except Exception as e:
        _record("key_lifecycle", False, f"exception: {e}")
    finally:
        if created:
            try:
                hsm.delete_object(scratch, **auth)
                _record("delete", True)
            except Exception as e:
                _record("delete", False, f"exception: {e}")

    # --- Reset (opt-in) ---
    if os.environ.get("LAGER_HSM_ALLOW_RESET") == "1":
        try:
            hsm.reset(**auth)
            hsm.list_objects(auth_key_id=1, password="password")
            _record("reset_restores_factory_login", True)
        except Exception as e:
            _record("reset_restores_factory_login", False, f"exception: {e}")
    else:
        print("  SKIP: reset (set LAGER_HSM_ALLOW_RESET=1 to erase the HSM)")

    print("\n=== HSM Test Complete ===")
    total = len(_results)
    passed = sum(1 for _, p, _ in _results if p)
    failed = total - passed
    print(f"\nTotal: {total}  Passed: {passed}  Failed: {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
