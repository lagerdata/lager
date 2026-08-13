# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

import secrets

from cryptography.hazmat.primitives import hashes

DH_P = "0xcf5cf5c38419a724957ff5dd323b9c45c3cdd261eb740f69aa94b8bb1a5c9640" + \
    "9153bd76b24222d03274e4725a5406092e9e82e9135c643cae98132b0d95f7d6" + \
    "5347c68afc1e677da90e51bbab5f5cf429c291b4ba39c6b2dc5e8c7231e46aa7" + \
    "728e87664532cdf547be20c9a3fa8342be6e34371a27c06f7dc0edddd2f86373"

# The modulus is 1024 bits. cryptography's exchange() zero-padded the shared
# secret up to key_size // 8 before it was hashed, and that padding is part of
# the contract with the device: about one exchange in 256 produces a secret
# with a leading zero byte, and hashing the unpadded form there yields a
# different AES key from the device's. The padding is explicit below.
_SECRET_BYTES = 128

# The public key goes out padded to 2048 // 8 -- twice the modulus width, so it
# always carries 128 leading zero bytes. That is what the device has always
# received and what postNegotiateSecurity() computes its length field from. It
# is a quirk rather than a requirement, but narrowing it would change the bytes
# on the wire.
_PUBKEY_WIRE_BYTES = 256


class BlufiCrypto(object):
    """Fixed-parameter Diffie-Hellman for the BluFi handshake.

    The exchange is plain modular exponentiation over the group below rather
    than cryptography's ``hazmat.primitives.asymmetric.dh``, which deprecates
    finite-field DH wholesale in 50.0. The box is the initiator and sends P, G
    and its own public key to the device, so it owns the group outright and the
    only compatibility constraint is byte-level.

    Note that CPython's three-argument pow() is not constant-time, where
    cryptography's exchange() was. The prime here is Espressif's published
    BluFi constant -- hardcoded in every BluFi implementation, and not a safe
    prime -- so this handshake does not withstand an active attacker either
    way, but the timing characteristic is a real change and is called out here
    rather than left implicit.
    """

    def __init__(self):
        self.p = int(DH_P, 0)
        self.g = 2
        self.y = 0
        self.privKey = None
        self.pubKey = None

    def genKeys(self):
        # Private exponent in [2, p-2].
        self.privKey = secrets.randbelow(self.p - 3) + 2
        self.pubKey = pow(self.g, self.privKey, self.p)
        self.y = self.pubKey

    def deriveSharedKey(self, peer_pub_bytes):
        if self.privKey is None:
            raise ValueError('genKeys() must be called before deriveSharedKey()')

        y = int.from_bytes(peer_pub_bytes, "big")
        # Reject the degenerate public keys: 0, 1 and p-1 generate a subgroup
        # small enough that the shared secret is guessable, and anything at or
        # above p is malformed. cryptography rejected these inside public_key();
        # nothing else does now.
        if y <= 1 or y >= self.p - 1:
            raise ValueError('peer public key is out of range')

        shared_key = pow(y, self.privKey, self.p).to_bytes(_SECRET_BYTES, "big")
        digest = hashes.Hash(hashes.MD5())
        digest.update(shared_key)
        return digest.finalize()

    def getPBytes(self):
        return bytes.fromhex(DH_P[2:])

    def getGBytes(self):
        return bytes.fromhex('02')

    def getYBytes(self):
        return self.y.to_bytes(_PUBKEY_WIRE_BYTES, 'big')
