# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for box/lager/security/hsm/yubihsm2.py and box/lager/hsm_hs.py.

The driver opens the device, logs in, runs one operation and closes the
session and the device on every call, including when the operation fails.
These tests swap the driver's ``_lib()`` namespace for fakes that record
every open, login and close, so they need neither a YubiHSM nor the
``yubihsm`` package. A parity test against the real package runs when it is
installed (it is in the box image).
"""

import base64
import enum
import os
import sys
import types
import unittest
from unittest.mock import patch

_BOX_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', '..', 'box')
)
if _BOX_ROOT not in sys.path:
    sys.path.insert(0, _BOX_ROOT)

from lager.exceptions import HsmBackendError  # noqa: E402
from lager.security.hsm import yubihsm2  # noqa: E402
from lager.security.hsm.yubihsm2 import YubiHsm2  # noqa: E402

_ADDRESS = 'USB0::0x1050::0x0030::12345678::INSTR'
_PASSWORD = 'correct-horse-battery'


# --------------------------------------------------------------------------
# Fake yubihsm / cryptography namespace
# --------------------------------------------------------------------------

ALGORITHM = enum.Enum('ALGORITHM', [
    'EC_P256', 'EC_P384', 'EC_P521', 'EC_K256', 'EC_ED25519',
    'RSA_2048', 'RSA_3072', 'RSA_4096', 'AES128_CCM_WRAP',
])
OBJECT = enum.Enum('OBJECT', [
    'OPAQUE', 'AUTHENTICATION_KEY', 'ASYMMETRIC_KEY', 'WRAP_KEY',
    'HMAC_KEY', 'TEMPLATE', 'OTP_AEAD_KEY',
])
COMMAND = enum.Enum('COMMAND', ['ECHO', 'BLINK_DEVICE'])
ERROR = enum.Enum('ERROR', [
    'AUTHENTICATION_FAILED', 'OBJECT_NOT_FOUND', 'OBJECT_EXISTS',
    'INSUFFICIENT_PERMISSIONS', 'SESSIONS_FULL', 'INVALID_DATA',
])


class CAPABILITY(enum.IntFlag):
    SIGN_PKCS = 1 << 5
    SIGN_PSS = 1 << 6
    SIGN_ECDSA = 1 << 7
    SIGN_EDDSA = 1 << 8
    GENERATE_ASYMMETRIC_KEY = 1 << 4
    PUT_ASYMMETRIC = 1 << 3
    DELETE_ASYMMETRIC_KEY = 1 << 41
    RESET_DEVICE = 1 << 28


class YubiHsmError(Exception):
    pass


class YubiHsmDeviceError(YubiHsmError):
    def __init__(self, code):
        super().__init__('Device error: %s' % code.name)
        self.code = code


class YubiHsmAuthenticationError(YubiHsmError):
    pass


class YubiHsmConnectionError(YubiHsmError):
    pass


class _Hash:
    digest_size = 0

    def __eq__(self, other):
        return type(self) is type(other)

    def __hash__(self):
        return hash(type(self))


class SHA256(_Hash):
    digest_size = 32


class SHA384(_Hash):
    digest_size = 48


class SHA512(_Hash):
    digest_size = 64


class _PublicKey:
    def public_bytes(self, encoding, fmt):
        assert (encoding, fmt) == ('PEM', 'SPKI')
        return b'-----BEGIN PUBLIC KEY-----\nFAKE\n-----END PUBLIC KEY-----\n'


class _FakeKey:
    """An asymmetric key object on the fake device."""

    def __init__(self, object_id):
        self.id = object_id
        self.calls = []
        self.deleted = False

    def get_public_key(self):
        return _PublicKey()

    def get_info(self):
        return types.SimpleNamespace(
            id=self.id, object_type=OBJECT.ASYMMETRIC_KEY, label='signer',
            algorithm=ALGORITHM.EC_P256, domains=0b101,
            capabilities=CAPABILITY.SIGN_ECDSA | CAPABILITY.DELETE_ASYMMETRIC_KEY,
            size=32, sequence=0)

    def delete(self):
        self.deleted = True

    def sign_ecdsa(self, data, hash):
        self.calls.append(('ecdsa', data, hash))
        return b'ecdsa-sig'

    def sign_eddsa(self, data):
        self.calls.append(('eddsa', data))
        return b'eddsa-sig'

    def sign_pkcs1v1_5(self, data, hash):
        self.calls.append(('pkcs1', data, hash))
        return b'pkcs1-sig'

    def sign_pss(self, data, salt_len, hash, mgf_hash):
        self.calls.append(('pss', data, salt_len, hash, mgf_hash))
        return b'pss-sig'


class _FakeSession:
    def __init__(self, device):
        self.device = device
        self.closed = False
        self.secure_cmds = []
        self.reset = False

    def close(self):
        self.closed = True

    def get_object(self, object_id, object_type):
        if self.device.fail_with is not None:
            raise self.device.fail_with
        key = self.device.objects.get(object_id)
        if key is None:
            raise YubiHsmDeviceError(ERROR.OBJECT_NOT_FOUND)
        return key

    def list_objects(self, **kwargs):
        self.device.list_kwargs = kwargs
        return list(self.device.objects.values())

    def send_secure_cmd(self, command, data=b''):
        self.secure_cmds.append((command, data))
        return b''

    def reset_device(self):
        self.reset = True


class _FakeDevice:
    """Shared state of the one fake YubiHSM; every ``YubiHsm()`` opens it."""

    def __init__(self):
        self.objects = {7: _FakeKey(7)}
        self.opens = []
        self.closes = 0
        self.logins = []
        self.sessions = []
        self.open_error = None
        self.login_error = None
        self.fail_with = None
        self.list_kwargs = None
        self.info_failures = 0


def _make_lib(device):
    class UsbBackend:
        def __init__(self, serial=None, timeout=None):
            if device.open_error is not None:
                raise device.open_error
            device.opens.append({'serial': serial, 'timeout': timeout})

    class YubiHsm:
        def __init__(self, backend):
            self.backend = backend

        def close(self):
            device.closes += 1

        def get_device_info(self):
            if device.info_failures:
                device.info_failures -= 1
                raise YubiHsmConnectionError('No YubiHSM found.')
            return types.SimpleNamespace(
                version=(2, 4, 0), serial=12345678, log_used=3, log_size=62,
                part_number='', supported_algorithms={ALGORITHM.EC_P256, ALGORITHM.RSA_2048})

        def send_cmd(self, command, data=b''):
            assert command is COMMAND.ECHO
            return data

        def create_session_derived(self, auth_key_id, password):
            device.logins.append((auth_key_id, password))
            if device.login_error is not None:
                raise device.login_error
            session = _FakeSession(device)
            device.sessions.append(session)
            return session

    class AsymmetricKey:
        generated = []
        put = []

        @staticmethod
        def generate(session, object_id, label, domains, capabilities, algorithm):
            AsymmetricKey.generated.append((object_id, label, domains, capabilities, algorithm))
            if object_id in device.objects:
                raise YubiHsmDeviceError(ERROR.OBJECT_EXISTS)
            key = _FakeKey(object_id or 0x0042)
            device.objects[key.id] = key
            return key

    return types.SimpleNamespace(
        YubiHsm=YubiHsm, UsbBackend=UsbBackend, AsymmetricKey=AsymmetricKey,
        ALGORITHM=ALGORITHM, CAPABILITY=CAPABILITY, COMMAND=COMMAND, OBJECT=OBJECT,
        exceptions=types.SimpleNamespace(
            YubiHsmDeviceError=YubiHsmDeviceError,
            YubiHsmAuthenticationError=YubiHsmAuthenticationError,
            YubiHsmConnectionError=YubiHsmConnectionError),
        hashes=types.SimpleNamespace(SHA256=SHA256, SHA384=SHA384, SHA512=SHA512),
        serialization=types.SimpleNamespace(
            Encoding=types.SimpleNamespace(PEM='PEM'),
            PublicFormat=types.SimpleNamespace(SubjectPublicKeyInfo='SPKI')),
        ec=types.SimpleNamespace(EllipticCurvePrivateKey=type('EC', (), {})),
        ed25519=types.SimpleNamespace(Ed25519PrivateKey=type('Ed', (), {})),
        rsa=types.SimpleNamespace(RSAPrivateKey=type('RSA', (), {})),
    )


class _DriverTestCase(unittest.TestCase):

    def setUp(self):
        self.device = _FakeDevice()
        self.lib = _make_lib(self.device)
        patcher = patch.object(yubihsm2, '_lib', return_value=self.lib)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.hsm = YubiHsm2('hsm1', _ADDRESS)

    def assertAllClosed(self):
        self.assertEqual(self.device.closes, len(self.device.opens),
                         'a device handle was left open')
        for session in self.device.sessions:
            self.assertTrue(session.closed, 'a session was left open')


# --------------------------------------------------------------------------
# Address and argument parsing
# --------------------------------------------------------------------------

class AddressTests(unittest.TestCase):

    def test_serial_from_visa_style_address(self):
        self.assertEqual(yubihsm2._serial_from_address(_ADDRESS), 12345678)

    def test_no_address_means_any_device(self):
        self.assertIsNone(yubihsm2._serial_from_address(None))
        self.assertIsNone(yubihsm2._serial_from_address(''))

    def test_address_without_serial_is_rejected(self):
        with self.assertRaises(HsmBackendError):
            yubihsm2._serial_from_address('USB0::0x1050::0x0030')

    def test_domain_mask(self):
        self.assertEqual(yubihsm2._domain_mask([1]), 0b1)
        self.assertEqual(yubihsm2._domain_mask([1, 3]), 0b101)
        self.assertEqual(yubihsm2._domain_mask(16), 1 << 15)
        for bad in ([], [0], [17]):
            with self.assertRaises(ValueError):
                yubihsm2._domain_mask(bad)

    def test_object_id_range(self):
        with self.assertRaises(ValueError):
            yubihsm2._object_id(0x10000)
        with self.assertRaises(ValueError):
            yubihsm2._object_id(-1)


# --------------------------------------------------------------------------
# Session handling
# --------------------------------------------------------------------------

class SessionHandlingTests(_DriverTestCase):

    def test_info_does_not_log_in(self):
        info = self.hsm.info()
        self.assertEqual(info['firmware'], '2.4.0')
        self.assertEqual(info['serial'], 12345678)
        self.assertEqual((info['log_used'], info['log_size']), (3, 62))
        self.assertIsNone(info['part_number'])
        self.assertEqual(info['algorithms'], ['ec-p256', 'rsa2048'])
        self.assertEqual(self.device.opens, [{'serial': 12345678, 'timeout': 5}])
        self.assertEqual(self.device.logins, [])
        self.assertAllClosed()

    def test_echo(self):
        self.assertEqual(self.hsm.echo(b'ping'), b'ping')
        self.assertAllClosed()

    def test_one_login_and_one_close_per_call(self):
        self.hsm.sign(7, b'msg', 'ecdsa-sha256', auth_key_id=2, password=_PASSWORD)
        self.hsm.get_public_key(7, auth_key_id=2, password=_PASSWORD)
        self.assertEqual(self.device.logins, [(2, _PASSWORD), (2, _PASSWORD)])
        self.assertEqual(len(self.device.opens), 2)
        self.assertAllClosed()

    def test_closes_on_operation_error(self):
        with self.assertRaises(HsmBackendError):
            self.hsm.sign(9, b'msg', 'ecdsa-sha256', password=_PASSWORD)
        self.assertEqual(len(self.device.sessions), 1)
        self.assertAllClosed()

    def test_closes_device_on_login_error(self):
        self.device.login_error = YubiHsmAuthenticationError('Authentication failed')
        with self.assertRaises(HsmBackendError):
            self.hsm.blink(password=_PASSWORD)
        self.assertEqual(self.device.sessions, [])
        self.assertAllClosed()

    def test_missing_password_fails_before_opening(self):
        for password in (None, ''):
            with self.assertRaises(ValueError):
                self.hsm.list_objects(password=password)
        self.assertEqual(self.device.opens, [])

    def test_bad_arguments_fail_before_opening(self):
        with self.assertRaises(ValueError):
            self.hsm.sign(7, b'msg', 'md5', password=_PASSWORD)
        with self.assertRaises(ValueError):
            self.hsm.generate_key(1, 'dsa', password=_PASSWORD)
        with self.assertRaises(ValueError):
            self.hsm.blink(256, password=_PASSWORD)
        self.assertEqual(self.device.opens, [])

    def test_session_context_manager_closes(self):
        with self.hsm.session(password=_PASSWORD) as session:
            self.assertIs(session, self.device.sessions[0])
        self.assertAllClosed()


class MissingPackageTests(unittest.TestCase):

    def test_missing_package_raises_backend_error(self):
        with patch.dict(sys.modules, {'yubihsm': None}):
            with self.assertRaises(HsmBackendError) as ctx:
                yubihsm2._lib()
        self.assertIn("pip install 'yubihsm[usb]'", str(ctx.exception))


# --------------------------------------------------------------------------
# Operations
# --------------------------------------------------------------------------

class OperationTests(_DriverTestCase):

    def test_sign_mechanisms(self):
        key = self.device.objects[7]
        self.assertEqual(self.hsm.sign(7, b'm', 'ecdsa-sha384', password=_PASSWORD), b'ecdsa-sig')
        self.assertEqual(self.hsm.sign(7, b'm', 'eddsa', password=_PASSWORD), b'eddsa-sig')
        self.assertEqual(self.hsm.sign(7, b'm', 'rsa-pkcs1-sha512', password=_PASSWORD), b'pkcs1-sig')
        self.assertEqual(self.hsm.sign(7, b'm', 'RSA-PSS-SHA256', password=_PASSWORD), b'pss-sig')
        self.assertEqual(key.calls, [
            ('ecdsa', b'm', SHA384()),
            ('eddsa', b'm'),
            ('pkcs1', b'm', SHA512()),
            ('pss', b'm', 32, SHA256(), SHA256()),
        ])

    def test_blink_sends_seconds(self):
        self.hsm.blink(9, password=_PASSWORD)
        self.assertEqual(self.device.sessions[0].secure_cmds, [(COMMAND.BLINK_DEVICE, bytes([9]))])

    def test_list_objects_filters_and_converts(self):
        objects = self.hsm.list_objects(type='asymmetric-key', label='signer', password=_PASSWORD)
        self.assertEqual(self.device.list_kwargs,
                         {'object_type': OBJECT.ASYMMETRIC_KEY, 'label': 'signer'})
        self.assertEqual(objects, [{
            'id': 7, 'type': 'asymmetric-key', 'label': 'signer', 'algorithm': 'ec-p256',
            'domains': [1, 3], 'capabilities': ['sign-ecdsa', 'delete-asymmetric-key'],
            'size': 32, 'sequence': 0,
        }])

    def test_get_object_info(self):
        info = self.hsm.get_object_info(7, password=_PASSWORD)
        self.assertEqual(info['id'], 7)

    def test_generate_key_defaults(self):
        result = self.hsm.generate_key(0, 'ec-p256', label='k', domains=[1, 2], password=_PASSWORD)
        self.assertEqual(result['id'], 0x0042)
        self.assertIn('BEGIN PUBLIC KEY', result['public_key_pem'])
        self.assertEqual(self.lib.AsymmetricKey.generated,
                         [(0, 'k', 0b11, CAPABILITY.SIGN_ECDSA, ALGORITHM.EC_P256)])

    def test_generate_key_capabilities(self):
        self.hsm.generate_key(5, 'rsa2048', password=_PASSWORD)
        self.hsm.generate_key(6, 'ed25519', capabilities=['sign-eddsa', 'delete-asymmetric-key'],
                              password=_PASSWORD)
        caps = [g[3] for g in self.lib.AsymmetricKey.generated]
        self.assertEqual(caps, [CAPABILITY.SIGN_PKCS | CAPABILITY.SIGN_PSS,
                                CAPABILITY.SIGN_EDDSA | CAPABILITY.DELETE_ASYMMETRIC_KEY])

    def test_rsa_generation_gets_a_long_usb_timeout(self):
        self.hsm.generate_key(5, 'rsa4096', password=_PASSWORD)
        self.hsm.generate_key(6, 'ec-p256', password=_PASSWORD)
        self.hsm.sign(7, b'm', 'ecdsa-sha256', password=_PASSWORD)
        self.assertEqual([o['timeout'] for o in self.device.opens],
                         [yubihsm2.RSA_KEYGEN_TIMEOUT_S, 5, 5])

    def test_unknown_capability(self):
        with self.assertRaises(ValueError):
            self.hsm.generate_key(5, 'ec-p256', capabilities=['fly'], password=_PASSWORD)
        self.assertAllClosed()

    def test_delete_object(self):
        self.hsm.delete_object(7, password=_PASSWORD)
        self.assertTrue(self.device.objects[7].deleted)

    def test_reset_waits_for_the_device(self):
        self.device.info_failures = 2
        with patch.object(yubihsm2.time, 'sleep') as sleep:
            self.hsm.reset(password=_PASSWORD)
        self.assertTrue(self.device.sessions[0].reset)
        self.assertEqual(sleep.call_count, 3)
        self.assertAllClosed()

    def test_reset_gives_up_after_the_wait(self):
        self.device.info_failures = 10 ** 6
        with patch.object(yubihsm2, 'RESET_WAIT_S', 0.0), \
                patch.object(yubihsm2.time, 'sleep'):
            with self.assertRaises(HsmBackendError) as ctx:
                self.hsm.reset(password=_PASSWORD)
        self.assertIn('did not come back', str(ctx.exception))


# --------------------------------------------------------------------------
# Error mapping
# --------------------------------------------------------------------------

class ErrorMappingTests(_DriverTestCase):

    def _error(self, call):
        with self.assertRaises(HsmBackendError) as ctx:
            call()
        message = str(ctx.exception)
        self.assertNotIn(_PASSWORD, message)
        self.assertTrue(message.startswith('[HSM] '), message)
        return message[len('[HSM] '):]

    def test_authentication_failure(self):
        self.device.login_error = YubiHsmAuthenticationError('bad')
        message = self._error(lambda: self.hsm.list_objects(auth_key_id=3, password=_PASSWORD))
        self.assertEqual(message, 'Authentication failed for auth key 3')

    def test_authentication_failure_device_code(self):
        self.device.login_error = YubiHsmDeviceError(ERROR.AUTHENTICATION_FAILED)
        message = self._error(lambda: self.hsm.list_objects(password=_PASSWORD))
        self.assertEqual(message, 'Authentication failed for auth key 1')

    def test_object_not_found(self):
        message = self._error(lambda: self.hsm.get_public_key(9, password=_PASSWORD))
        self.assertEqual(message, 'No asymmetric-key with ID 0x0009')

    def test_object_exists(self):
        message = self._error(lambda: self.hsm.generate_key(7, 'ec-p256', password=_PASSWORD))
        self.assertEqual(message, 'An object with ID 0x0007 already exists')

    def test_insufficient_permissions_names_capability(self):
        self.device.fail_with = YubiHsmDeviceError(ERROR.INSUFFICIENT_PERMISSIONS)
        message = self._error(lambda: self.hsm.sign(7, b'm', 'rsa-pss-sha256', password=_PASSWORD))
        self.assertEqual(message, 'Auth key 1 lacks the capability sign-pss')

    def test_sessions_full(self):
        self.device.login_error = YubiHsmDeviceError(ERROR.SESSIONS_FULL)
        self.assertIn('no free sessions', self._error(lambda: self.hsm.blink(password=_PASSWORD)))

    def test_other_device_error(self):
        self.device.fail_with = YubiHsmDeviceError(ERROR.INVALID_DATA)
        message = self._error(lambda: self.hsm.delete_object(7, password=_PASSWORD))
        self.assertEqual(message, 'The YubiHSM refused the command (INVALID_DATA)')

    def test_device_not_found(self):
        self.device.open_error = YubiHsmConnectionError('No YubiHSM found.')
        self.assertEqual(self._error(self.hsm.info), 'No YubiHSM with serial 12345678 on this box')

    def test_permission_problem(self):
        self.device.open_error = ValueError('The device has no langid')
        self.assertIn('udev rule', self._error(self.hsm.info))

    def test_device_busy(self):
        self.device.open_error = OSError('[Errno 16] Resource busy')
        self.assertIn('in use by another process', self._error(self.hsm.info))

    def test_password_not_kept_on_driver(self):
        self.hsm.list_objects(password=_PASSWORD)
        self.assertNotIn(_PASSWORD, repr(self.hsm))
        self.assertNotIn(_PASSWORD, repr(vars(self.hsm)))


# --------------------------------------------------------------------------
# hardware_service adapter
# --------------------------------------------------------------------------

class AdapterTests(_DriverTestCase):

    def setUp(self):
        super().setUp()
        from lager import hsm_hs
        self.adapter = hsm_hs.create_device({'name': 'hsm1'})
        patcher = patch('lager.security.hsm.dispatcher.get_driver', return_value=self.hsm)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_sign_crosses_as_base64(self):
        signature = self.adapter.sign('7', base64.b64encode(b'msg').decode(), 'eddsa',
                                      auth_key_id='1', password=_PASSWORD)
        self.assertEqual(base64.b64decode(signature), b'eddsa-sig')
        self.assertEqual(self.device.objects[7].calls, [('eddsa', b'msg')])

    def test_sign_rejects_bad_base64(self):
        with self.assertRaises(ValueError):
            self.adapter.sign(7, 'not base64!', 'eddsa', password=_PASSWORD)

    def test_info_passes_through(self):
        self.assertEqual(self.adapter.info()['serial'], 12345678)


# --------------------------------------------------------------------------
# Parity with the real package
# --------------------------------------------------------------------------

try:
    import yubihsm  # noqa: F401
    _HAVE_YUBIHSM = True
except ImportError:
    _HAVE_YUBIHSM = False


@unittest.skipUnless(_HAVE_YUBIHSM, 'yubihsm package not installed')
class RealPackageParityTests(unittest.TestCase):

    def test_table_names_exist(self):
        lib = yubihsm2._lib()
        for name in yubihsm2.ALGORITHMS.values():
            self.assertIn(name, lib.ALGORITHM.__members__)
        for name in yubihsm2.OBJECT_TYPES.values():
            self.assertIn(name, lib.OBJECT.__members__)
        capabilities = {cap for _, _, cap in yubihsm2.MECHANISMS.values()}
        capabilities |= {'generate-asymmetric-key', 'put-asymmetric', 'reset-device',
                         'delete-asymmetric-key'}
        for cap in capabilities:
            self.assertIn(cap.upper().replace('-', '_'), lib.CAPABILITY.__members__)
        for name in yubihsm2.MECHANISMS.values():
            if name[1]:
                self.assertTrue(hasattr(lib.hashes, name[1]))


if __name__ == '__main__':
    unittest.main()
