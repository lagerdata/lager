# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Yubico YubiHSM 2 driver, over direct USB with the ``yubihsm`` package.

Every method opens the device, logs in when the operation needs it, runs
one operation, and closes the session and the device in ``finally``. The
driver therefore never holds the USB claim between calls, and a ``lager
python`` script can open the same device at any time.

The password is a keyword-only argument on every method that logs in. It is
never stored on the driver and never put in an exception message.
"""

from __future__ import annotations

import contextlib
import time
import types
from functools import reduce
from operator import or_
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from lager.exceptions import HsmBackendError

from .hsm_net import HsmBase

# The library's default through yhusb:// is 600 s, which turns an unplugged
# device into a ten-minute hang.
USB_TIMEOUT_S = 5
# The HSM searches for random primes, so RSA key generation has no fixed
# duration: 5-18 s for RSA-2048 and 87 s for one RSA-4096 key were measured
# on firmware 2.4.1.
RSA_KEYGEN_TIMEOUT_S = 300
RESET_WAIT_S = 10.0
_RESET_POLL_S = 0.5

ALGORITHMS = {
    "ec-p256": "EC_P256",
    "ec-p384": "EC_P384",
    "ec-p521": "EC_P521",
    "ec-k256": "EC_K256",
    "ed25519": "EC_ED25519",
    "rsa2048": "RSA_2048",
    "rsa3072": "RSA_3072",
    "rsa4096": "RSA_4096",
}

OBJECT_TYPES = {
    "asymmetric-key": "ASYMMETRIC_KEY",
    "authentication-key": "AUTHENTICATION_KEY",
    "opaque": "OPAQUE",
    "wrap-key": "WRAP_KEY",
    "hmac-key": "HMAC_KEY",
    "template": "TEMPLATE",
    "otp-aead-key": "OTP_AEAD_KEY",
}

# mechanism -> (signing method, hash name or None, capability it needs)
MECHANISMS = {
    "ecdsa-sha256": ("ecdsa", "SHA256", "sign-ecdsa"),
    "ecdsa-sha384": ("ecdsa", "SHA384", "sign-ecdsa"),
    "ecdsa-sha512": ("ecdsa", "SHA512", "sign-ecdsa"),
    "eddsa": ("eddsa", None, "sign-eddsa"),
    "rsa-pkcs1-sha256": ("pkcs1", "SHA256", "sign-pkcs"),
    "rsa-pkcs1-sha384": ("pkcs1", "SHA384", "sign-pkcs"),
    "rsa-pkcs1-sha512": ("pkcs1", "SHA512", "sign-pkcs"),
    "rsa-pss-sha256": ("pss", "SHA256", "sign-pss"),
    "rsa-pss-sha384": ("pss", "SHA384", "sign-pss"),
    "rsa-pss-sha512": ("pss", "SHA512", "sign-pss"),
}


def _lib() -> types.SimpleNamespace:
    """The ``yubihsm`` and ``cryptography`` names the driver uses, imported on first use."""
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
        from yubihsm import exceptions
        from yubihsm.backends.usb import UsbBackend
        from yubihsm.core import YubiHsm
        from yubihsm.defs import ALGORITHM, CAPABILITY, COMMAND, OBJECT
        from yubihsm.objects import AsymmetricKey
    except ImportError as exc:
        raise HsmBackendError(
            "The yubihsm Python package is not installed on this box. "
            "Install it with: pip install 'yubihsm[usb]'") from exc
    return types.SimpleNamespace(
        YubiHsm=YubiHsm, UsbBackend=UsbBackend, AsymmetricKey=AsymmetricKey,
        ALGORITHM=ALGORITHM, CAPABILITY=CAPABILITY, COMMAND=COMMAND, OBJECT=OBJECT,
        exceptions=exceptions, hashes=hashes, serialization=serialization,
        ec=ec, ed25519=ed25519, rsa=rsa,
    )


def _cli_name(member: Any) -> str:
    """``EC_P256`` -> ``ec-p256``: the spelling users type and read."""
    return str(getattr(member, "name", member)).lower().replace("_", "-")


def _algorithm_name(member: Any) -> str:
    raw = getattr(member, "name", str(member))
    for cli, enum_name in ALGORITHMS.items():
        if enum_name == raw:
            return cli
    return _cli_name(member)


def _object_type_name(member: Any) -> str:
    raw = getattr(member, "name", str(member))
    for cli, enum_name in OBJECT_TYPES.items():
        if enum_name == raw:
            return cli
    return _cli_name(member)


def _choice(value: str, table: Dict[str, Any], what: str) -> Any:
    key = (value or "").strip().lower()
    if key not in table:
        raise ValueError("Unknown %s '%s'. Valid: %s" % (what, value, ", ".join(table)))
    return table[key]


def _object_id(value: Any) -> int:
    object_id = int(value)
    if not 0 <= object_id <= 0xFFFF:
        raise ValueError("Object ID %d is out of range 0-65535" % object_id)
    return object_id


def _domain_mask(domains: Any) -> int:
    if isinstance(domains, int):
        domains = [domains]
    numbers = [int(d) for d in domains]
    if not numbers:
        raise ValueError("At least one domain (1-16) is required")
    for d in numbers:
        if not 1 <= d <= 16:
            raise ValueError("Domain %d is out of range 1-16" % d)
    return reduce(or_, (1 << (d - 1) for d in numbers))


def _domain_list(mask: int) -> List[int]:
    return [d + 1 for d in range(16) if mask & (1 << d)]


def _serial_from_address(address: Optional[str]) -> Optional[int]:
    """The device serial from ``USB0::0x1050::0x0030::<serial>::INSTR``, or None without an address."""
    if not address:
        return None
    parts = address.split("::")
    if len(parts) > 3 and parts[3]:
        try:
            return int(parts[3])
        except ValueError:
            pass
    raise HsmBackendError("Address '%s' has no YubiHSM serial number" % address)


def _check_password(password: Any, auth_key_id: int) -> None:
    if not isinstance(password, str) or not password:
        raise ValueError("A password is required for auth key %d" % auth_key_id)


class YubiHsm2(HsmBase):
    """A YubiHSM 2 on this box, chosen by the serial in its net address."""

    def __init__(self, name: str, address: Optional[str] = None) -> None:
        super().__init__(name, address)
        self._serial = _serial_from_address(address)

    def __repr__(self) -> str:
        return "YubiHsm2(name=%r, serial=%r)" % (self.name, self._serial)

    # ------------------------------------------------------------------
    # Device and session handling
    # ------------------------------------------------------------------

    def _open(self, lib: types.SimpleNamespace, timeout: int = USB_TIMEOUT_S):
        try:
            return lib.YubiHsm(lib.UsbBackend(serial=self._serial, timeout=timeout))
        except Exception as exc:
            # pyusb raises ValueError ("no langid") when it cannot read the
            # serial string, which is a permission problem, not bad input.
            raise self._connection_error(exc) from exc

    def _run(self, op: Callable[[types.SimpleNamespace, Any, Any], Any], *,
             auth_key_id: Optional[int] = None, password: Optional[str] = None,
             context: Optional[Dict[str, Any]] = None, timeout: int = USB_TIMEOUT_S) -> Any:
        """Open the device, log in if ``auth_key_id`` is given, run ``op``, close everything."""
        if auth_key_id is not None:
            _check_password(password, auth_key_id)
        lib = _lib()
        hsm = self._open(lib, timeout)
        session = None
        try:
            if auth_key_id is not None:
                session = hsm.create_session_derived(auth_key_id, password)
            return op(lib, hsm, session)
        except (HsmBackendError, ValueError):
            raise
        except Exception as exc:
            raise self._translate(lib, exc, auth_key_id, context or {}) from exc
        finally:
            if session is not None:
                with contextlib.suppress(Exception):
                    session.close()
            with contextlib.suppress(Exception):
                hsm.close()

    def _connection_error(self, exc: BaseException) -> HsmBackendError:
        text = str(exc)
        low = text.lower()
        if "no yubihsm found" in low:
            if self._serial is None:
                return HsmBackendError("No YubiHSM on this box")
            return HsmBackendError("No YubiHSM with serial %d on this box" % self._serial)
        if "busy" in low or "errno 16" in low:
            return HsmBackendError(
                "The YubiHSM is in use by another process (yubihsm-connector?)")
        if "langid" in low or "access denied" in low or "errno 13" in low:
            return HsmBackendError(
                "No permission to open the YubiHSM USB device. "
                "Check the udev rule for 1050:0030 on the box")
        return HsmBackendError("Cannot talk to the YubiHSM: %s" % text)

    def _translate(self, lib: types.SimpleNamespace, exc: BaseException,
                   auth_key_id: Optional[int], context: Dict[str, Any]) -> HsmBackendError:
        errors = lib.exceptions
        if isinstance(exc, errors.YubiHsmAuthenticationError):
            return HsmBackendError("Authentication failed for auth key %s" % auth_key_id)
        if isinstance(exc, errors.YubiHsmDeviceError):
            code = getattr(getattr(exc, "code", None), "name", str(getattr(exc, "code", "")))
            object_id = context.get("object_id")
            object_type = context.get("object_type", "object")
            if code == "AUTHENTICATION_FAILED":
                return HsmBackendError("Authentication failed for auth key %s" % auth_key_id)
            if code == "OBJECT_NOT_FOUND":
                if object_id is not None:
                    return HsmBackendError("No %s with ID 0x%04X" % (object_type, object_id))
                return HsmBackendError("The object was not found")
            if code == "OBJECT_EXISTS" and object_id is not None:
                return HsmBackendError("An object with ID 0x%04X already exists" % object_id)
            if code == "INSUFFICIENT_PERMISSIONS":
                capability = context.get("capability")
                if capability:
                    return HsmBackendError(
                        "Auth key %s lacks the capability %s" % (auth_key_id, capability))
                return HsmBackendError(
                    "Auth key %s lacks the capability for this operation" % auth_key_id)
            if code == "SESSIONS_FULL":
                return HsmBackendError(
                    "The HSM has no free sessions; wait 30 s for idle sessions to expire")
            return HsmBackendError("The YubiHSM refused the command (%s)" % code)
        if isinstance(exc, errors.YubiHsmConnectionError) or type(exc).__name__ == "USBError":
            return self._connection_error(exc)
        return HsmBackendError("YubiHSM error: %s: %s" % (type(exc).__name__, exc))

    # ------------------------------------------------------------------
    # Value conversion
    # ------------------------------------------------------------------

    @staticmethod
    def _capabilities(lib: types.SimpleNamespace, names: Iterable[str]) -> Any:
        flags = []
        for name in names:
            enum_name = str(name).strip().upper().replace("-", "_")
            try:
                flags.append(lib.CAPABILITY[enum_name])
            except KeyError:
                raise ValueError("Unknown capability '%s'" % name) from None
        if not flags:
            raise ValueError("At least one capability is required")
        return reduce(or_, flags)

    @staticmethod
    def _capability_names(lib: types.SimpleNamespace, flags: Any) -> List[str]:
        value = int(flags)
        names = []
        for member in lib.CAPABILITY:
            bit = int(member)
            if bit and bit & (bit - 1) == 0 and value & bit:
                names.append(_cli_name(member))
        return names

    def _object_dict(self, lib: types.SimpleNamespace, info: Any) -> Dict[str, Any]:
        return {
            "id": int(info.id),
            "type": _object_type_name(info.object_type),
            "label": info.label,
            "algorithm": _algorithm_name(info.algorithm),
            "domains": _domain_list(int(info.domains)),
            "capabilities": self._capability_names(lib, info.capabilities),
            "size": int(info.size),
            "sequence": int(info.sequence),
        }

    @staticmethod
    def _pem(lib: types.SimpleNamespace, public_key: Any) -> str:
        return public_key.public_bytes(
            lib.serialization.Encoding.PEM,
            lib.serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("ascii")

    @staticmethod
    def _default_capabilities_for_algorithm(enum_name: str) -> List[str]:
        if enum_name == "EC_ED25519":
            return ["sign-eddsa"]
        if enum_name.startswith("EC_"):
            return ["sign-ecdsa"]
        return ["sign-pkcs", "sign-pss"]

    @staticmethod
    def _default_capabilities_for_key(lib: types.SimpleNamespace, key: Any) -> List[str]:
        if isinstance(key, lib.ed25519.Ed25519PrivateKey):
            return ["sign-eddsa"]
        if isinstance(key, lib.ec.EllipticCurvePrivateKey):
            return ["sign-ecdsa"]
        if isinstance(key, lib.rsa.RSAPrivateKey):
            return ["sign-pkcs", "sign-pss"]
        raise ValueError("Unsupported private key type %s" % type(key).__name__)

    # ------------------------------------------------------------------
    # Operations without a login
    # ------------------------------------------------------------------

    def info(self) -> Dict[str, Any]:
        def op(lib, hsm, _session):
            di = hsm.get_device_info()
            return {
                "firmware": ".".join(str(part) for part in di.version),
                "serial": int(di.serial),
                "log_used": int(di.log_used),
                "log_size": int(di.log_size),
                "part_number": getattr(di, "part_number", None) or None,
                "algorithms": sorted(_algorithm_name(a) for a in di.supported_algorithms),
            }
        return self._run(op)

    def echo(self, data: bytes) -> bytes:
        payload = bytes(data)

        def op(lib, hsm, _session):
            return bytes(hsm.send_cmd(lib.COMMAND.ECHO, payload))
        return self._run(op)

    # ------------------------------------------------------------------
    # Operations with a login
    # ------------------------------------------------------------------

    def blink(self, seconds: int = 5, *, auth_key_id: int = 1, password: str) -> None:
        seconds = int(seconds)
        if not 0 <= seconds <= 255:
            raise ValueError("Blink time %d s is out of range 0-255" % seconds)

        def op(lib, _hsm, session):
            session.send_secure_cmd(lib.COMMAND.BLINK_DEVICE, bytes([seconds]))
        self._run(op, auth_key_id=auth_key_id, password=password)

    def list_objects(self, type: Optional[str] = None, label: Optional[str] = None, *,
                     auth_key_id: int = 1, password: str) -> List[Dict[str, Any]]:
        type_name = _choice(type, OBJECT_TYPES, "object type") if type else None

        def op(lib, _hsm, session):
            kwargs: Dict[str, Any] = {}
            if type_name:
                kwargs["object_type"] = lib.OBJECT[type_name]
            if label:
                kwargs["label"] = label
            return [self._object_dict(lib, obj.get_info())
                    for obj in session.list_objects(**kwargs)]
        return self._run(op, auth_key_id=auth_key_id, password=password)

    def get_object_info(self, object_id: int, object_type: str = "asymmetric-key", *,
                        auth_key_id: int = 1, password: str) -> Dict[str, Any]:
        object_id = _object_id(object_id)
        type_name = _choice(object_type, OBJECT_TYPES, "object type")

        def op(lib, _hsm, session):
            obj = session.get_object(object_id, lib.OBJECT[type_name])
            return self._object_dict(lib, obj.get_info())
        return self._run(op, auth_key_id=auth_key_id, password=password,
                         context={"object_id": object_id, "object_type": object_type})

    def delete_object(self, object_id: int, object_type: str = "asymmetric-key", *,
                      auth_key_id: int = 1, password: str) -> None:
        object_id = _object_id(object_id)
        type_name = _choice(object_type, OBJECT_TYPES, "object type")

        def op(lib, _hsm, session):
            session.get_object(object_id, lib.OBJECT[type_name]).delete()
        self._run(op, auth_key_id=auth_key_id, password=password,
                  context={"object_id": object_id, "object_type": object_type,
                           "capability": "delete-%s" % object_type})

    def generate_key(self, object_id: int, algorithm: str, label: str = "",
                     domains: Sequence[int] = (1,), capabilities: Optional[Sequence[str]] = None, *,
                     auth_key_id: int = 1, password: str) -> Dict[str, Any]:
        object_id = _object_id(object_id)
        enum_name = _choice(algorithm, ALGORITHMS, "algorithm")
        mask = _domain_mask(domains)
        cap_names = list(capabilities) if capabilities else \
            self._default_capabilities_for_algorithm(enum_name)

        def op(lib, _hsm, session):
            key = lib.AsymmetricKey.generate(
                session, object_id, label or "", mask,
                self._capabilities(lib, cap_names), lib.ALGORITHM[enum_name])
            return {"id": int(key.id), "public_key_pem": self._pem(lib, key.get_public_key())}
        return self._run(op, auth_key_id=auth_key_id, password=password,
                         context={"object_id": object_id or None, "object_type": "asymmetric-key",
                                  "capability": "generate-asymmetric-key"},
                         timeout=RSA_KEYGEN_TIMEOUT_S if enum_name.startswith("RSA_")
                         else USB_TIMEOUT_S)

    def import_key(self, object_id: int, private_key_pem: str, label: str = "",
                   domains: Sequence[int] = (1,), capabilities: Optional[Sequence[str]] = None, *,
                   auth_key_id: int = 1, password: str) -> int:
        object_id = _object_id(object_id)
        mask = _domain_mask(domains)
        pem = private_key_pem.encode("ascii") if isinstance(private_key_pem, str) else bytes(private_key_pem)

        def op(lib, _hsm, session):
            try:
                key = lib.serialization.load_pem_private_key(pem, password=None)
            except Exception:
                # The loader's message can quote the input; never pass it on.
                raise ValueError("private_key_pem is not an unencrypted PEM private key") from None
            cap_names = list(capabilities) if capabilities else \
                self._default_capabilities_for_key(lib, key)
            stored = lib.AsymmetricKey.put(
                session, object_id, label or "", mask, self._capabilities(lib, cap_names), key)
            return int(stored.id)
        return self._run(op, auth_key_id=auth_key_id, password=password,
                         context={"object_id": object_id or None, "object_type": "asymmetric-key",
                                  "capability": "put-asymmetric"})

    def get_public_key(self, object_id: int, *, auth_key_id: int = 1, password: str) -> str:
        object_id = _object_id(object_id)

        def op(lib, _hsm, session):
            key = session.get_object(object_id, lib.OBJECT.ASYMMETRIC_KEY)
            return self._pem(lib, key.get_public_key())
        return self._run(op, auth_key_id=auth_key_id, password=password,
                         context={"object_id": object_id, "object_type": "asymmetric-key"})

    def sign(self, object_id: int, data: bytes, mechanism: str, *,
             auth_key_id: int = 1, password: str) -> bytes:
        object_id = _object_id(object_id)
        method, hash_name, capability = _choice(mechanism, MECHANISMS, "mechanism")
        payload = bytes(data)

        def op(lib, _hsm, session):
            key = session.get_object(object_id, lib.OBJECT.ASYMMETRIC_KEY)
            if method == "eddsa":
                return bytes(key.sign_eddsa(payload))
            digest = getattr(lib.hashes, hash_name)()
            if method == "ecdsa":
                return bytes(key.sign_ecdsa(payload, digest))
            if method == "pkcs1":
                return bytes(key.sign_pkcs1v1_5(payload, digest))
            return bytes(key.sign_pss(payload, digest.digest_size, digest, digest))
        return self._run(op, auth_key_id=auth_key_id, password=password,
                         context={"object_id": object_id, "object_type": "asymmetric-key",
                                  "capability": capability})

    def reset(self, *, auth_key_id: int = 1, password: str) -> None:
        def op(_lib, _hsm, session):
            session.reset_device()
        self._run(op, auth_key_id=auth_key_id, password=password,
                  context={"capability": "reset-device"})
        # The device reboots and re-enumerates on USB; wait until it answers.
        deadline = time.monotonic() + RESET_WAIT_S
        time.sleep(_RESET_POLL_S)
        while True:
            try:
                self.info()
                return
            except HsmBackendError:
                if time.monotonic() >= deadline:
                    raise HsmBackendError(
                        "The YubiHSM did not come back within %d s after the reset"
                        % RESET_WAIT_S) from None
                time.sleep(_RESET_POLL_S)

    @contextlib.contextmanager
    def session(self, *, auth_key_id: int = 1, password: str):
        _check_password(password, auth_key_id)
        lib = _lib()
        hsm = self._open(lib)
        try:
            try:
                auth_session = hsm.create_session_derived(auth_key_id, password)
            except Exception as exc:
                raise self._translate(lib, exc, auth_key_id, {}) from exc
            try:
                yield auth_session
            finally:
                with contextlib.suppress(Exception):
                    auth_session.close()
        finally:
            with contextlib.suppress(Exception):
                hsm.close()
