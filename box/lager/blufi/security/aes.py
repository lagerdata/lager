# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms

try:
    # cryptography >= 48 ships CFB here; 50.0 deprecates the primitives
    # path with removal upstream calls imminent but has not scheduled.
    from cryptography.hazmat.decrepit.ciphers.modes import CFB
except ImportError:
    # Older installs predate the decrepit package entirely. Box runtime
    # pins cryptography==43.0.3 (box.Dockerfile); unit floor is >=42.
    # Same class either way, byte-identical output.
    from cryptography.hazmat.primitives.ciphers.modes import CFB

# https://cryptography.io/en/latest/hazmat/primitives/symmetric-encryption/#cryptography.hazmat.primitives.ciphers.algorithms.AES
class BlufiAES(object):
    """ AES/CFB/NoPadding """
    def __init__(self, key, iv):
        self.key = key
        self.iv = iv
        # AES128 was a fixed-size alias; AES(key) is the stable API across
        # cryptography versions (and what manylinux wheels for 3.12 expect).
        # BluFi always uses a 16-byte key.
        self.cipher = Cipher(algorithms.AES(self.key), CFB(self.iv))
        self.encryptor = self.cipher.encryptor()
        self.decryptor = self.cipher.decryptor()

    def encrypt(self, data):
        ct = self.encryptor.update(data) + self.encryptor.finalize()
        return ct

    def decrypt(self, data):
        pt = self.decryptor.update(data) + self.decryptor.finalize()
        return pt
