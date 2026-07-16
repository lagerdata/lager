# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for box/lager/box_auth.py: offline Ed25519 bearer-token verification.

Two properties matter most here and are tested hardest:

  1. A box with no auth configured behaves exactly as it did before this module
     existed. That is the state every box ships in, and breaking it would break
     every user who never asked for authentication.

  2. A trust anchor that cannot be read is never mistaken for permission to skip
     it. Every way of corrupting the config must refuse requests, not admit them.

Plus the forgeries. A JWT verifier that dispatches on the token's own `alg` can
be talked into checking a signature the attacker chose -- "none", or HS256 with
the public key as the HMAC secret. This module never dispatches on alg, so both
are structurally impossible; the tests assert that rather than assume it.

box_auth.py imports `.constants` relatively, so a stub `lager` package is
registered before loading it. That keeps the test off the full box-side package,
which needs hardware libraries this test has no use for.
"""

import base64
import hashlib
import hmac
import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import time
import types
import unittest

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
except ImportError:  # pragma: no cover
    raise unittest.SkipTest('cryptography is required for box_auth tests')


HERE = os.path.dirname(__file__)
BOX_LAGER_DIR = os.path.normpath(os.path.join(HERE, '..', '..', '..', 'box', 'lager'))

ISSUER = 'https://control-plane.example'
BOX_ID = 'PRD-1'


def _load_box_auth(auth_config_path, box_id_path):
    """Load box_auth with its constants pointed at a temp directory."""
    package = types.ModuleType('lager_boxauth_test_pkg')
    package.__path__ = [BOX_LAGER_DIR]
    sys.modules['lager_boxauth_test_pkg'] = package

    spec = importlib.util.spec_from_file_location(
        'lager_boxauth_test_pkg.constants', os.path.join(BOX_LAGER_DIR, 'constants.py')
    )
    constants = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(constants)
    constants.AUTH_CONFIG_PATH = str(auth_config_path)
    constants.BOX_ID_PATH = str(box_id_path)
    sys.modules['lager_boxauth_test_pkg.constants'] = constants

    spec = importlib.util.spec_from_file_location(
        'lager_boxauth_test_pkg.box_auth', os.path.join(BOX_LAGER_DIR, 'box_auth.py')
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def b64u(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip('=')


def public_jwk(private_key, kid='k1'):
    raw = private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return {'kid': kid, 'kty': 'OKP', 'crv': 'Ed25519', 'x': b64u(raw)}


class BoxAuthTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.auth_path = self.tmp / 'auth.json'
        self.box_id_path = self.tmp / 'box_id'
        self.box_auth = _load_box_auth(self.auth_path, self.box_id_path)
        self.key = Ed25519PrivateKey.generate()

    def write_config(self, **overrides):
        config = {
            'schema_version': 1,
            'issuer': ISSUER,
            'audience': BOX_ID,
            'keys': [public_jwk(self.key)],
        }
        config.update(overrides)
        self.auth_path.write_text(json.dumps(config))
        self.box_auth._config_cache = None

    def make_token(self, key=None, kid='k1', alg='EdDSA', **claim_overrides):
        now = int(time.time())
        claims = {
            'iss': ISSUER,
            'aud': BOX_ID,
            'sub': 'user-1',
            'jti': 'j1',
            'exp': now + 600,
        }
        claims.update(claim_overrides)
        claims = {k: v for k, v in claims.items() if v is not None}

        header = b64u(json.dumps({'alg': alg, 'kid': kid, 'typ': 'JWT'}).encode())
        payload = b64u(json.dumps(claims).encode())
        signer = key or self.key
        signature = signer.sign(f'{header}.{payload}'.encode())
        return f'{header}.{payload}.{b64u(signature)}'

    def verify(self, token):
        return self.box_auth.verify_bearer('Bearer ' + token)


class UnconfiguredBoxTests(BoxAuthTestCase):
    """No config means no auth. This is the default and must never change."""

    def test_not_enforcing(self):
        self.assertFalse(self.box_auth.is_enforcing())

    def test_guard_allows_everything(self):
        for path in ('/python', '/pip', '/debug/flash', '/invoke'):
            with self.subTest(path=path):
                self.assertIsNone(self.box_auth.guard(None, path=path))

    def test_guard_ignores_a_token_it_was_sent(self):
        # A caller configured for another box shouldn't get an error here.
        self.assertIsNone(self.box_auth.guard('Bearer whatever', path='/python'))

    def test_removing_config_reopens_the_box(self):
        # The documented rollback: delete the file, get the old box back.
        self.write_config()
        self.assertEqual(self.box_auth.guard(None, path='/python')[0], 401)
        self.auth_path.unlink()
        self.box_auth._config_cache = None
        self.assertIsNone(self.box_auth.guard(None, path='/python'))
        self.assertFalse(self.box_auth.is_enforcing())


class MalformedConfigTests(BoxAuthTestCase):
    """A broken trust anchor must refuse, never fall open."""

    def assert_fails_closed(self):
        self.box_auth._config_cache = None
        status, _ = self.box_auth.guard(None, path='/python')
        self.assertEqual(status, 503)
        # Critically: it must not read as "no auth configured".
        self.assertTrue(self.box_auth.is_enforcing())

    def test_not_json(self):
        self.auth_path.write_text('{ not json at all')
        self.assert_fails_closed()

    def test_wrong_schema_version(self):
        self.write_config(schema_version=99)
        self.assert_fails_closed()

    def test_no_keys(self):
        self.write_config(keys=[])
        self.assert_fails_closed()

    def test_missing_issuer(self):
        self.auth_path.write_text(json.dumps(
            {'schema_version': 1, 'audience': BOX_ID, 'keys': [public_jwk(self.key)]}
        ))
        self.assert_fails_closed()

    def test_non_ed25519_key(self):
        self.write_config(keys=[{'kid': 'k1', 'kty': 'RSA', 'n': 'x', 'e': 'AQAB'}])
        self.assert_fails_closed()

    def test_undecodable_key_material(self):
        self.write_config(keys=[
            {'kid': 'k1', 'kty': 'OKP', 'crv': 'Ed25519', 'x': '!!!not-base64!!!'}
        ])
        self.assert_fails_closed()

    def test_duplicate_kid(self):
        jwk = public_jwk(self.key)
        self.write_config(keys=[jwk, dict(jwk)])
        self.assert_fails_closed()

    def test_health_stays_reachable_so_this_is_diagnosable(self):
        # A box refusing everything is useless if you cannot ask it why.
        self.auth_path.write_text('{ not json at all')
        self.box_auth._config_cache = None
        self.assertIsNone(self.box_auth.guard(None, path='/health'))


class ValidTokenTests(BoxAuthTestCase):
    def setUp(self):
        super().setUp()
        self.write_config()

    def test_accepted(self):
        claims = self.verify(self.make_token())
        self.assertEqual(claims['sub'], 'user-1')

    def test_guard_admits_it(self):
        self.assertIsNone(
            self.box_auth.guard('Bearer ' + self.make_token(), path='/python')
        )

    def test_audience_may_be_a_list(self):
        self.verify(self.make_token(aud=[BOX_ID, 'PRD-9']))

    def test_bearer_scheme_is_case_insensitive(self):
        # RFC 7235 says the scheme is case-insensitive.
        token = self.make_token()
        self.assertIsNone(self.box_auth.guard('bearer ' + token, path='/python'))

    def test_audience_defaults_to_box_id_file(self):
        self.box_id_path.write_text('PRD-1\n')
        self.auth_path.write_text(json.dumps(
            {'schema_version': 1, 'issuer': ISSUER, 'keys': [public_jwk(self.key)]}
        ))
        self.box_auth._config_cache = None
        self.verify(self.make_token())


class RejectedTokenTests(BoxAuthTestCase):
    def setUp(self):
        super().setUp()
        self.write_config()

    def assert_rejected(self, token):
        with self.assertRaises(self.box_auth.AuthError):
            self.verify(token)

    def test_expired(self):
        self.assert_rejected(self.make_token(exp=int(time.time()) - 300))

    def test_expiry_within_clock_leeway_accepted(self):
        # Boxes are not guaranteed a good clock, and a box that rejects every
        # token has no console to fix it from. Leeway is deliberate.
        self.verify(self.make_token(exp=int(time.time()) - 60))

    def test_not_yet_valid(self):
        self.assert_rejected(self.make_token(nbf=int(time.time()) + 300))

    def test_nbf_within_clock_leeway_accepted(self):
        self.verify(self.make_token(nbf=int(time.time()) + 60))

    def test_no_expiry(self):
        self.assert_rejected(self.make_token(exp=None))

    def test_wrong_audience(self):
        # The containment property: a token for another box is useless here, so
        # one captured in flight cannot be replayed across the fleet.
        self.assert_rejected(self.make_token(aud='PRD-2'))

    def test_wrong_issuer(self):
        self.assert_rejected(self.make_token(iss='https://someone-else.example'))

    def test_unknown_kid(self):
        self.assert_rejected(self.make_token(kid='not-a-key-we-have'))

    def test_signed_by_a_key_we_do_not_trust(self):
        self.assert_rejected(self.make_token(key=Ed25519PrivateKey.generate()))

    def test_tampered_payload(self):
        token = self.make_token()
        header, _, signature = token.split('.')
        forged = b64u(json.dumps(
            {'iss': ISSUER, 'aud': BOX_ID, 'sub': 'attacker', 'exp': int(time.time()) + 600}
        ).encode())
        self.assert_rejected(f'{header}.{forged}.{signature}')

    def test_missing_header(self):
        with self.assertRaises(self.box_auth.AuthError):
            self.box_auth.verify_bearer(None)

    def test_not_a_bearer_token(self):
        with self.assertRaises(self.box_auth.AuthError):
            self.box_auth.verify_bearer('Basic dXNlcjpwYXNz')

    def test_malformed_token(self):
        for bad in ('', 'not-a-jwt', 'a.b', 'a.b.c.d', '...'):
            with self.subTest(token=bad):
                with self.assertRaises(self.box_auth.AuthError):
                    self.box_auth.verify_bearer('Bearer ' + bad)


class ForgeryTests(BoxAuthTestCase):
    """The attacks that have broken real JWT libraries."""

    def setUp(self):
        super().setUp()
        self.write_config()

    def test_alg_none(self):
        # A verifier that reads alg to decide how to check would accept an
        # unsigned token. This one requires EdDSA and only ever calls Ed25519.
        header = b64u(json.dumps({'alg': 'none', 'kid': 'k1'}).encode())
        payload = b64u(json.dumps(
            {'iss': ISSUER, 'aud': BOX_ID, 'sub': 'attacker', 'exp': int(time.time()) + 600}
        ).encode())
        with self.assertRaises(self.box_auth.AuthError):
            self.box_auth.verify_bearer(f'Bearer {header}.{payload}.')

    def test_hs256_signed_with_our_public_key_as_the_hmac_secret(self):
        # The classic algorithm-confusion attack: the public key is public, so
        # if the verifier can be steered to HS256 the attacker can sign with it.
        raw_public = self.key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        header = b64u(json.dumps({'alg': 'HS256', 'kid': 'k1'}).encode())
        payload = b64u(json.dumps(
            {'iss': ISSUER, 'aud': BOX_ID, 'sub': 'attacker', 'exp': int(time.time()) + 600}
        ).encode())
        signature = hmac.new(
            raw_public, f'{header}.{payload}'.encode(), hashlib.sha256
        ).digest()
        with self.assertRaises(self.box_auth.AuthError):
            self.box_auth.verify_bearer(f'Bearer {header}.{payload}.{b64u(signature)}')

    def test_alg_is_not_used_to_select_a_verifier(self):
        # Even a correctly Ed25519-signed token is refused if it claims another
        # algorithm: alg is a thing to check, not a thing to obey.
        self.write_config()
        with self.assertRaises(self.box_auth.AuthError):
            self.verify(self.make_token(alg='RS256'))


class OpenPathTests(BoxAuthTestCase):
    """The few paths that stay reachable without a token, and their edges."""

    def setUp(self):
        super().setUp()
        self.write_config()

    def test_health_is_open(self):
        self.assertIsNone(self.box_auth.guard(None, path='/health'))
        self.assertIsNone(self.box_auth.guard(None, path='/health/'))

    def test_authorize_key_is_open(self):
        # It carries its own credential and runs before a box has a key set.
        self.assertIsNone(self.box_auth.guard(None, path='/authorize-key'))

    def test_dangerous_paths_are_not_open(self):
        for path in ('/python', '/pip', '/binaries/add', '/debug/flash', '/invoke'):
            with self.subTest(path=path):
                self.assertEqual(self.box_auth.guard(None, path=path)[0], 401)

    def test_exemption_cannot_be_smuggled(self):
        # An exemption matched against the raw path would let any of these
        # through.
        for path in (
            '/python?x=/health',
            '/python#/health',
            '/health/../python',
            '/healthz',
            '/api/health',
            '/python/health',
        ):
            with self.subTest(path=path):
                self.assertEqual(self.box_auth.guard(None, path=path)[0], 401)


class KeyRotationTests(BoxAuthTestCase):
    """Two keys are trusted at once so rotation cannot strand an offline box."""

    def test_both_keys_accepted_during_overlap(self):
        old_key, new_key = self.key, Ed25519PrivateKey.generate()
        self.write_config(keys=[
            public_jwk(new_key, kid='new'),
            public_jwk(old_key, kid='old'),
        ])
        self.verify(self.make_token(key=old_key, kid='old'))
        self.verify(self.make_token(key=new_key, kid='new'))

    def test_dropped_key_stops_working(self):
        old_key = self.key
        new_key = Ed25519PrivateKey.generate()
        self.write_config(keys=[public_jwk(new_key, kid='new')])
        with self.assertRaises(self.box_auth.AuthError):
            self.verify(self.make_token(key=old_key, kid='old'))


class ConfigReloadTests(BoxAuthTestCase):
    def test_config_change_is_picked_up_without_a_restart(self):
        self.write_config()
        self.verify(self.make_token())

        replacement = Ed25519PrivateKey.generate()
        # Bypass write_config's cache reset: the point is that the module
        # notices the file changed on its own.
        self.auth_path.write_text(json.dumps({
            'schema_version': 1, 'issuer': ISSUER, 'audience': BOX_ID,
            'keys': [public_jwk(replacement)],
        }))
        os.utime(self.auth_path, (time.time() + 2, time.time() + 2))

        with self.assertRaises(self.box_auth.AuthError):
            self.verify(self.make_token())
        self.verify(self.make_token(key=replacement))


if __name__ == '__main__':
    unittest.main()
