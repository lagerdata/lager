# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for the BluFi module -- pure-logic code only, no hardware needed.

Run with:
    python -m pytest test/unit/blufi/ -v
"""
import asyncio
import io
import struct
import sys
import os
import threading
from unittest.mock import patch, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Ensure box/lager is importable, and mock bleak if not installed
# ---------------------------------------------------------------------------
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
BOX_DIR = os.path.join(REPO_ROOT, "box")
if BOX_DIR not in sys.path:
    sys.path.insert(0, BOX_DIR)

# bleak is a BLE library only available on boxes. Mock it so we can import
# the BluFi package locally for unit-testing pure-logic code.
if "bleak" not in sys.modules:
    _mock_bleak = MagicMock()
    sys.modules["bleak"] = _mock_bleak
    sys.modules["bleak.backends"] = _mock_bleak.backends
    sys.modules["bleak.backends.characteristic"] = _mock_bleak.backends.characteristic


# ============================= CRC =========================================

from lager.blufi.security.crc import BlufiCRC


class TestBlufiCRC:
    def test_empty_data(self):
        result = BlufiCRC.calcCRC(0, b"")
        assert isinstance(result, int)
        # CRC of empty data with seed 0 should be deterministic
        assert result == BlufiCRC.calcCRC(0, b"")

    def test_single_byte_zero(self):
        result = BlufiCRC.calcCRC(0, b"\x00")
        assert isinstance(result, int)
        assert 0 <= result <= 0xFFFF

    def test_single_byte_ff(self):
        result = BlufiCRC.calcCRC(0, b"\xff")
        assert isinstance(result, int)
        assert 0 <= result <= 0xFFFF

    def test_single_byte_42(self):
        result = BlufiCRC.calcCRC(0, b"\x42")
        assert isinstance(result, int)
        assert 0 <= result <= 0xFFFF

    def test_multi_byte(self):
        result = BlufiCRC.calcCRC(0, b"Hello, BluFi!")
        assert isinstance(result, int)
        assert 0 <= result <= 0xFFFF

    def test_incremental(self):
        """Feeding data in chunks with chaining should equal feeding it all at once."""
        data = b"Hello, BluFi!"
        whole = BlufiCRC.calcCRC(0, data)
        chunked = BlufiCRC.calcCRC(0, data[:5])
        chunked = BlufiCRC.calcCRC(chunked, data[5:])
        assert whole == chunked

    def test_deterministic(self):
        data = b"\x01\x02\x03\x04\x05"
        assert BlufiCRC.calcCRC(0, data) == BlufiCRC.calcCRC(0, data)

    def test_different_data_different_crc(self):
        a = BlufiCRC.calcCRC(0, b"\x00")
        b = BlufiCRC.calcCRC(0, b"\xff")
        assert a != b


# ============================= AES =========================================

from lager.blufi.security.aes import BlufiAES


class TestBlufiAES:
    KEY = b"\x00" * 16
    IV = b"\x00" * 16

    def test_encrypt_decrypt_roundtrip(self):
        plaintext = b"test data for aes roundtrip"
        aes_enc = BlufiAES(self.KEY, self.IV)
        ct = aes_enc.encrypt(plaintext)
        aes_dec = BlufiAES(self.KEY, self.IV)
        pt = aes_dec.decrypt(ct)
        assert pt == plaintext

    def test_different_keys_different_output(self):
        plaintext = b"same plaintext"
        key2 = b"\x01" + b"\x00" * 15
        ct1 = BlufiAES(self.KEY, self.IV).encrypt(plaintext)
        ct2 = BlufiAES(key2, self.IV).encrypt(plaintext)
        assert ct1 != ct2

    def test_different_iv_different_output(self):
        plaintext = b"same plaintext"
        iv2 = b"\x01" + b"\x00" * 15
        ct1 = BlufiAES(self.KEY, self.IV).encrypt(plaintext)
        ct2 = BlufiAES(self.KEY, iv2).encrypt(plaintext)
        assert ct1 != ct2

    def test_known_vector(self):
        """AES-128 CFB with all-zero key/IV -- first block XORs plaintext with AES(key, IV)."""
        plaintext = b"\x00" * 16
        ct = BlufiAES(self.KEY, self.IV).encrypt(plaintext)
        # Ciphertext of zeros is the raw AES block output
        assert len(ct) == 16
        assert ct != plaintext  # should not be identity

    def test_empty_data(self):
        ct = BlufiAES(self.KEY, self.IV).encrypt(b"")
        assert ct == b""
        pt = BlufiAES(self.KEY, self.IV).decrypt(b"")
        assert pt == b""

    def test_large_data(self):
        plaintext = os.urandom(1024)
        ct = BlufiAES(self.KEY, self.IV).encrypt(plaintext)
        pt = BlufiAES(self.KEY, self.IV).decrypt(ct)
        assert pt == plaintext


# ============================= Crypto (DH) =================================

from lager.blufi.security.crypto import BlufiCrypto


class TestBlufiCrypto:
    def test_get_p_bytes_length(self):
        c = BlufiCrypto()
        assert len(c.getPBytes()) == 128  # 1024-bit prime

    def test_get_p_bytes_deterministic(self):
        c = BlufiCrypto()
        assert c.getPBytes() == c.getPBytes()

    def test_get_g_bytes(self):
        c = BlufiCrypto()
        assert c.getGBytes() == b"\x02"

    def test_gen_keys(self):
        """Key generation succeeds. Skip if cryptography rejects the 1024-bit DH prime."""
        c = BlufiCrypto()
        try:
            c.genKeys()
        except ValueError as e:
            pytest.skip(f"cryptography library rejects 1024-bit DH: {e}")
        assert c.privKey is not None
        assert c.pubKey is not None

    def test_get_y_bytes_length(self):
        c = BlufiCrypto()
        try:
            c.genKeys()
        except ValueError:
            pytest.skip("cryptography library rejects 1024-bit DH")
        y = c.getYBytes()
        assert len(y) == 256  # 2048 // 8

    def test_derive_shared_key_length(self):
        c = BlufiCrypto()
        try:
            c.genKeys()
        except ValueError:
            pytest.skip("cryptography library rejects 1024-bit DH")
        # Derive against own public key (self-exchange for testing)
        shared = c.deriveSharedKey(c.getYBytes())
        assert len(shared) == 16  # MD5 digest

    def test_two_party_key_agreement(self):
        """Two BlufiCrypto instances should derive the same shared key."""
        alice = BlufiCrypto()
        bob = BlufiCrypto()
        try:
            alice.genKeys()
            bob.genKeys()
        except ValueError:
            pytest.skip("cryptography library rejects 1024-bit DH")
        key_a = alice.deriveSharedKey(bob.getYBytes())
        key_b = bob.deriveSharedKey(alice.getYBytes())
        assert key_a == key_b
        assert len(key_a) == 16


# ============================= FrameCtrl ===================================

from lager.blufi.framectrl import getTypeValue, getPackageType, getSubType, FrameCtrlData
from lager.blufi.constants import CTRL, DATA, DIRECTION_OUTPUT, DIRECTION_INPUT


class TestFrameCtrl:
    def test_get_type_value_ctrl_ack(self):
        assert getTypeValue(CTRL.PACKAGE_VALUE, CTRL.SUBTYPE_ACK) == 0

    def test_get_type_value_data_neg(self):
        assert getTypeValue(DATA.PACKAGE_VALUE, DATA.SUBTYPE_NEG) == 1  # (0 << 2) | 1

    def test_get_type_value_ctrl_get_version(self):
        expected = (CTRL.SUBTYPE_GET_VERSION << 2) | CTRL.PACKAGE_VALUE
        assert getTypeValue(CTRL.PACKAGE_VALUE, CTRL.SUBTYPE_GET_VERSION) == expected

    def test_get_package_type(self):
        tv = getTypeValue(DATA.PACKAGE_VALUE, DATA.SUBTYPE_VERSION)
        assert getPackageType(tv) == DATA.PACKAGE_VALUE

    def test_get_sub_type(self):
        tv = getTypeValue(DATA.PACKAGE_VALUE, DATA.SUBTYPE_VERSION)
        assert getSubType(tv) == DATA.SUBTYPE_VERSION

    def test_roundtrip_type_value_ctrl(self):
        for subtype in [CTRL.SUBTYPE_ACK, CTRL.SUBTYPE_SET_SEC_MODE, CTRL.SUBTYPE_SET_OP_MODE,
                        CTRL.SUBTYPE_CONNECT_WIFI, CTRL.SUBTYPE_DISCONNECT_WIFI,
                        CTRL.SUBTYPE_GET_WIFI_STATUS, CTRL.SUBTYPE_DEAUTHENTICATE,
                        CTRL.SUBTYPE_GET_VERSION, CTRL.SUBTYPE_CLOSE_CONNECTION,
                        CTRL.SUBTYPE_GET_WIFI_LIST]:
            tv = getTypeValue(CTRL.PACKAGE_VALUE, subtype)
            assert getPackageType(tv) == CTRL.PACKAGE_VALUE
            assert getSubType(tv) == subtype

    def test_roundtrip_type_value_data(self):
        for subtype in [DATA.SUBTYPE_NEG, DATA.SUBTYPE_STA_WIFI_SSID, DATA.SUBTYPE_VERSION,
                        DATA.SUBTYPE_WIFI_LIST, DATA.SUBTYPE_ERROR, DATA.SUBTYPE_CUSTOM_DATA]:
            tv = getTypeValue(DATA.PACKAGE_VALUE, subtype)
            assert getPackageType(tv) == DATA.PACKAGE_VALUE
            assert getSubType(tv) == subtype

    def test_frame_ctrl_encrypted_bit(self):
        assert FrameCtrlData(0x01).isEncrypted() is True
        assert FrameCtrlData(0x00).isEncrypted() is False

    def test_frame_ctrl_checksum_bit(self):
        assert FrameCtrlData(0x02).isChecksum() is True
        assert FrameCtrlData(0x00).isChecksum() is False

    def test_frame_ctrl_ack_bit(self):
        assert FrameCtrlData(0x08).isAckRequirement() is True
        assert FrameCtrlData(0x00).isAckRequirement() is False

    def test_frame_ctrl_frag_bit(self):
        assert FrameCtrlData(0x10).hasFrag() is True
        assert FrameCtrlData(0x00).hasFrag() is False

    def test_get_frame_ctrl_value_all_false(self):
        val = FrameCtrlData.getFrameCTRLValue(False, False, DIRECTION_OUTPUT, False, False)
        assert val == 0

    def test_get_frame_ctrl_value_all_true(self):
        val = FrameCtrlData.getFrameCTRLValue(True, True, DIRECTION_INPUT, True, True)
        # encrypted(1) | checksum(2) | direction(4) | ack(8) | frag(16) = 31
        assert val == 0b11111  # 31

    def test_get_frame_ctrl_value_roundtrip(self):
        for enc, cs, dir_, ack, frag in [
            (True, False, DIRECTION_OUTPUT, False, False),
            (False, True, DIRECTION_OUTPUT, True, False),
            (True, True, DIRECTION_INPUT, True, True),
            (False, False, DIRECTION_OUTPUT, False, True),
        ]:
            val = FrameCtrlData.getFrameCTRLValue(enc, cs, dir_, ack, frag)
            fc = FrameCtrlData(val)
            assert fc.isEncrypted() == enc
            assert fc.isChecksum() == cs
            assert fc.isAckRequirement() == ack
            assert fc.hasFrag() == frag


# ============================= Utils =======================================

from lager.blufi.utils import generateAESIV, get_platform_type, Event_ts


class TestUtils:
    def test_generate_aes_iv_zero(self):
        iv = generateAESIV(0)
        assert len(iv) == 16
        assert iv[0] == 0
        assert iv[1:] == bytearray(15)

    def test_generate_aes_iv_values(self):
        assert generateAESIV(1)[0] == 1
        assert generateAESIV(127)[0] == 127
        assert generateAESIV(255)[0] == 255

    def test_generate_aes_iv_overflow(self):
        assert generateAESIV(256)[0] == 0
        assert generateAESIV(257)[0] == 1

    @patch("lager.blufi.utils.platform.system", return_value="Linux")
    @patch.dict(os.environ, {}, clear=False)
    def test_get_platform_type_linux(self, mock_sys):
        # Remove P4A_BOOTSTRAP if present
        os.environ.pop("P4A_BOOTSTRAP", None)
        assert get_platform_type() == "Linux"

    @patch("lager.blufi.utils.platform.system", return_value="Darwin")
    @patch.dict(os.environ, {}, clear=False)
    def test_get_platform_type_darwin(self, mock_sys):
        os.environ.pop("P4A_BOOTSTRAP", None)
        assert get_platform_type() == "Darwin"

    @patch.dict(os.environ, {"P4A_BOOTSTRAP": "sdl2"})
    def test_get_platform_type_android(self):
        assert get_platform_type() == "Android"

    @patch("lager.blufi.utils.platform.system", return_value="FreeBSD")
    @patch.dict(os.environ, {}, clear=False)
    def test_get_platform_type_unsupported(self, mock_sys):
        os.environ.pop("P4A_BOOTSTRAP", None)
        with pytest.raises(Exception, match="Unsupported platform"):
            get_platform_type()

    @patch("lager.blufi.utils.platform.system", return_value="Windows")
    @patch.dict(os.environ, {}, clear=False)
    def test_get_platform_type_windows(self, mock_sys):
        os.environ.pop("P4A_BOOTSTRAP", None)
        assert get_platform_type() == "Windows"

    def test_event_ts_set_clear(self):
        loop = asyncio.new_event_loop()
        t = threading.Thread(target=loop.run_forever, daemon=True)
        t.start()
        try:
            evt = Event_ts(loop)
            assert not evt.is_set()
            evt.set()
            # Give the loop a moment to process call_soon_threadsafe
            import time; time.sleep(0.05)
            assert evt.is_set()
            evt.clear()
            time.sleep(0.05)
            assert not evt.is_set()
        finally:
            loop.call_soon_threadsafe(loop.stop)
            t.join(timeout=2)


# ============================= BlufiClient Parsing =========================

# We need to construct a BlufiClient without requiring bleak to connect.
# The constructor spawns a thread with an asyncio loop -- that's fine locally.
# We mock bleak imports at the module level if bleak is not installed.

def _make_client():
    """Create a BlufiClient for local testing (bleak is mocked)."""
    from lager.blufi.client import BlufiClient
    client = BlufiClient()
    return client


@pytest.fixture
def client():
    c = _make_client()
    yield c
    import atexit
    try:
        atexit.unregister(c._cleanup)
    except Exception:
        pass
    try:
        c._bleak_loop.call_soon_threadsafe(c._bleak_loop.stop)
    except Exception:
        pass


class TestBlufiClientSequence:
    def test_generate_send_sequence_starts_at_zero(self, client):
        assert client.generateSendSequence() == 0

    def test_generate_send_sequence_increments(self, client):
        client.generateSendSequence()  # 0
        assert client.generateSendSequence() == 1
        assert client.generateSendSequence() == 2

    def test_generate_send_sequence_wraps(self, client):
        client.mSendSequence = 254
        assert client.generateSendSequence() == 255
        assert client.generateSendSequence() == 0


class TestBlufiClientPackageLength:
    def test_negative_sets_minus_one(self, client):
        client.setPostPackageLengthLimit(-5)
        assert client.mPackageLengthLimit == -1

    def test_zero_sets_minus_one(self, client):
        client.setPostPackageLengthLimit(0)
        assert client.mPackageLengthLimit == -1

    def test_small_value_clamps_to_min(self, client):
        from lager.blufi.constants import MIN_PACKAGE_LENGTH
        client.setPostPackageLengthLimit(5)
        assert client.mPackageLengthLimit == MIN_PACKAGE_LENGTH

    def test_normal_value(self, client):
        client.setPostPackageLengthLimit(100)
        assert client.mPackageLengthLimit == 96  # 100 - 4


class TestBlufiClientParseVersion:
    def test_parse_version(self, client):
        client.parseVersion(bytes([1, 2]))
        assert client.getVersion() == "1.2"

    def test_parse_version_zero(self, client):
        client.parseVersion(bytes([0, 0]))
        assert client.getVersion() == "0.0"

    def test_parse_version_high(self, client):
        client.parseVersion(bytes([255, 128]))
        assert client.getVersion() == "255.128"


class TestBlufiClientParseWifiState:
    def test_parse_wifi_state_normal(self, client):
        data = bytes([0x01, 0x00, 0x03])
        client.parseWifiState(data)
        ws = client.getWifiState()
        assert ws["opMode"] == 0x01
        assert ws["staConn"] == 0x00
        assert ws["softAPConn"] == 0x03

    def test_parse_wifi_state_short_data(self, client):
        """< 3 bytes should not crash and should not update state."""
        client._reset_state()
        client.parseWifiState(bytes([0x01]))
        ws = client.getWifiState()
        assert ws["opMode"] == -1  # unchanged


class TestBlufiClientParseWifiScanList:
    def test_single_entry(self, client):
        ssid = b"TestNet"
        # Format: [length][rssi][ssid_bytes...]
        # length = 1 (rssi) + len(ssid) = 1 + 7 = 8
        data = bytes([len(ssid) + 1, 0xD0 & 0xFF]) + ssid  # rssi = -48 signed
        # rssi is signed byte: -48 = 0xD0
        data = bytearray()
        data.append(len(ssid) + 1)
        data.extend(struct.pack('<b', -48))
        data.extend(ssid)
        client.parseWifiScanList(bytes(data))
        lst = client.getSSIDList()
        assert len(lst) == 1
        assert lst[0]["ssid"] == "TestNet"
        assert lst[0]["rssi"] == -48

    def test_multiple_entries(self, client):
        data = bytearray()
        for ssid, rssi in [(b"Net1", -30), (b"Net2", -70)]:
            data.append(len(ssid) + 1)
            data.extend(struct.pack('<b', rssi))
            data.extend(ssid)
        client.parseWifiScanList(bytes(data))
        lst = client.getSSIDList()
        assert len(lst) == 2
        assert lst[0]["ssid"] == "Net1"
        assert lst[0]["rssi"] == -30
        assert lst[1]["ssid"] == "Net2"
        assert lst[1]["rssi"] == -70

    def test_malformed_truncated(self, client):
        """Truncated data should not crash."""
        data = bytes([5, 0xD0])  # length says 5 but only 0 ssid bytes follow
        client.parseWifiScanList(data)
        # Should not crash; list may be empty or partial
        assert isinstance(client.getSSIDList(), list)

    def test_utf8_error(self, client):
        """Non-UTF-8 SSID bytes handled gracefully (not added to list)."""
        bad_ssid = b"\xff\xfe\xfd"
        data = bytearray()
        data.append(len(bad_ssid) + 1)
        data.extend(struct.pack('<b', -50))
        data.extend(bad_ssid)
        client.parseWifiScanList(bytes(data))
        # The malformed SSID is logged but not appended to ssidList
        lst = client.getSSIDList()
        assert isinstance(lst, list)
        assert len(lst) == 0


class TestBlufiClientParseAck:
    def test_parse_ack_with_data(self, client):
        client.parseAck(bytes([0x05]))
        assert client.mAck.get_nowait() == 0x05

    def test_parse_ack_empty(self, client):
        client.parseAck(bytes())
        assert client.mAck.get_nowait() == 0x100


class TestBlufiClientReceiveAck:
    def test_receive_ack_matching(self, client):
        client.mAck.put(42)
        assert client.receiveAck(42) is True

    def test_receive_ack_mismatch(self, client):
        client.mAck.put(99)
        assert client.receiveAck(42) is False

    def test_receive_ack_timeout(self, client):
        assert client.receiveAck(0, timeout=0.1) is False


class TestBlufiClientResetState:
    def test_reset_state(self, client):
        client.connected = True
        client.mSendSequence = 42
        client.mEncrypted = True
        client.version = "1.0"
        client._reset_state()
        assert client.connected is False
        assert client.mSendSequence == -1
        assert client.mReadSequence == -1
        assert client.mEncrypted is False
        assert client.mChecksum is False
        assert client.version is None
        assert client.wifiState["opMode"] == -1


class TestBlufiClientGetPostBytes:
    def test_no_data(self, client):
        """Frame with no payload: 4-byte header only."""
        result = client.getPostBytes(
            type=0, encrypt=False, checksum=False,
            requireAck=False, hasFrag=False, sequence=0, data=None
        )
        assert len(result) == 4
        assert result[0] == 0   # type
        assert result[1] == 0   # frameCtrl (all false, direction output)
        assert result[2] == 0   # sequence
        assert result[3] == 0   # dataLength

    def test_with_data(self, client):
        payload = b"\x01\x02\x03"
        tv = getTypeValue(CTRL.PACKAGE_VALUE, CTRL.SUBTYPE_GET_VERSION)
        result = client.getPostBytes(
            type=tv, encrypt=False, checksum=False,
            requireAck=False, hasFrag=False, sequence=5, data=payload
        )
        assert len(result) == 4 + 3
        assert result[0] == tv
        assert result[2] == 5   # sequence
        assert result[3] == 3   # data length
        assert result[4:] == payload

    def test_with_checksum(self, client):
        payload = b"\xAA\xBB"
        result = client.getPostBytes(
            type=0, encrypt=False, checksum=True,
            requireAck=False, hasFrag=False, sequence=1, data=payload
        )
        # Header(4) + data(2) + checksum(2) = 8
        assert len(result) == 8
        # Verify checksum is appended
        crc_bytes = result[6:]
        assert len(crc_bytes) == 2
        # Verify CRC matches manual computation
        check_input = struct.pack("<BB", 1, 2)  # seq=1, dataLen=2
        crc = BlufiCRC.calcCRC(0, check_input)
        crc = BlufiCRC.calcCRC(crc, payload)
        expected = struct.pack("<H", crc)
        assert crc_bytes == expected

    def test_with_encrypt_and_checksum(self, client):
        """Both flags set: CRC computed on plaintext, then data encrypted."""
        client.mAESKey = b"\x00" * 16
        payload = b"\x01\x02\x03\x04"
        seq = 3
        result = client.getPostBytes(
            type=0, encrypt=True, checksum=True,
            requireAck=False, hasFrag=False, sequence=seq, data=payload
        )
        # Header(4) + encrypted_data(4) + checksum(2) = 10
        assert len(result) == 10
        # The CRC should be computed on the plaintext
        check_input = struct.pack("<BB", seq, len(payload))
        crc = BlufiCRC.calcCRC(0, check_input)
        crc = BlufiCRC.calcCRC(crc, payload)
        expected_crc = struct.pack("<H", crc)
        assert result[8:] == expected_crc
        # Data portion should be encrypted (different from plaintext)
        encrypted_data = result[4:8]
        assert encrypted_data != payload


class TestBlufiClientParseNotification:
    """Test the parseNotification method with crafted byte sequences."""

    def test_ctrl_ack(self, client):
        """Inject a CTRL ACK frame and verify parseAck enqueues the value."""
        tv = getTypeValue(CTRL.PACKAGE_VALUE, CTRL.SUBTYPE_ACK)
        seq = 0
        fc = 0  # no flags
        data = bytes([0x05])  # ack value
        frame = bytes([tv, fc, seq, len(data)]) + data
        client.mReadSequence = -1
        client.parseNotification(bytearray(frame))
        assert client.mAck.get_nowait() == 0x05

    def test_data_version(self, client):
        """Inject a DATA VERSION frame and verify version is parsed."""
        tv = getTypeValue(DATA.PACKAGE_VALUE, DATA.SUBTYPE_VERSION)
        seq = 0
        fc = 0
        data = bytes([2, 5])
        frame = bytes([tv, fc, seq, len(data)]) + data
        client.mReadSequence = -1
        client.parseNotification(bytearray(frame))
        assert client.getVersion() == "2.5"

    def test_data_wifi_state(self, client):
        """Inject a DATA WIFI_CONNECTION_STATE frame."""
        tv = getTypeValue(DATA.PACKAGE_VALUE, DATA.SUBTYPE_WIFI_CONNECTION_STATE)
        seq = 0
        fc = 0
        data = bytes([0x01, 0x00, 0x02])  # STA mode, connected, 2 softAP
        frame = bytes([tv, fc, seq, len(data)]) + data
        client.mReadSequence = -1
        client.parseNotification(bytearray(frame))
        ws = client.getWifiState()
        assert ws["opMode"] == 0x01
        assert ws["staConn"] == 0x00
        assert ws["softAPConn"] == 0x02

    def test_fragmented_reassembly(self, client):
        """Two fragments are reassembled before parsing."""
        tv = getTypeValue(DATA.PACKAGE_VALUE, DATA.SUBTYPE_VERSION)
        client.mReadSequence = -1

        # Fragment 1: hasFrag bit set
        fc_frag = FrameCtrlData.getFrameCTRLValue(False, False, DIRECTION_OUTPUT, False, True)
        # Frag data: first 2 bytes = total remaining length, then actual data
        total_len = 2  # total payload after reassembly = 2 bytes
        frag_payload = struct.pack("<H", total_len) + bytes([3])
        frame1 = bytes([tv, fc_frag, 0, len(frag_payload)]) + frag_payload
        client.parseNotification(bytearray(frame1))
        # Version should NOT be set yet (still fragmented)
        assert client.getVersion() is None

        # Fragment 2: no frag bit (final fragment)
        fc_final = FrameCtrlData.getFrameCTRLValue(False, False, DIRECTION_OUTPUT, False, False)
        frag2_payload = bytes([7])
        frame2 = bytes([tv, fc_final, 1, len(frag2_payload)]) + frag2_payload
        client.parseNotification(bytearray(frame2))
        # Now version should be parsed from reassembled buffer [3, 7]
        assert client.getVersion() == "3.7"

    def test_encrypted_notification(self, client):
        """Inject an encrypted DATA VERSION frame and verify decryption works."""
        client.mAESKey = b"\x00" * 16
        tv = getTypeValue(DATA.PACKAGE_VALUE, DATA.SUBTYPE_VERSION)
        seq = 0
        fc = FrameCtrlData.getFrameCTRLValue(True, False, DIRECTION_OUTPUT, False, False)
        plaintext = bytes([4, 9])

        # Encrypt the data
        aes = BlufiAES(client.mAESKey, generateAESIV(seq))
        encrypted_data = aes.encrypt(plaintext)

        frame = bytes([tv, fc, seq, len(encrypted_data)]) + encrypted_data
        client.mReadSequence = -1
        client.parseNotification(bytearray(frame))
        assert client.getVersion() == "4.9"

    def test_checksum_notification(self, client):
        """Inject a frame with valid CRC and verify it passes."""
        tv = getTypeValue(DATA.PACKAGE_VALUE, DATA.SUBTYPE_VERSION)
        seq = 0
        fc = FrameCtrlData.getFrameCTRLValue(False, True, DIRECTION_OUTPUT, False, False)
        data = bytes([5, 3])

        # Compute CRC
        check_input = struct.pack("<BB", seq, len(data))
        crc = BlufiCRC.calcCRC(0, check_input)
        crc = BlufiCRC.calcCRC(crc, data)
        crc_bytes = struct.pack("<H", crc)

        frame = bytes([tv, fc, seq, len(data)]) + data + crc_bytes
        client.mReadSequence = -1
        client.parseNotification(bytearray(frame))
        assert client.getVersion() == "5.3"
