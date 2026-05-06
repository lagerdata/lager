#!/usr/bin/python
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

# coding: utf8

__version__ = '0.2'

import logging
import struct
import time
from threading import Thread
from .vendor.PyCRC.CRCCCITT import CRCCCITT


logger = logging.getLogger(__name__)


def calcCRC(data):
    crc = CRCCCITT("FFFF").calculate(bytes(data))
    b = bytearray(struct.pack(">H", crc))
    return b

class Frame(object):
    STATE_READ = 0x01
    STATE_ESCAPE = 0x02

    def __init__(self):
        self.finished = False
        self.error = False
        self.state = self.STATE_READ
        self.data = bytearray()
        self.crc = bytearray()
        self.reader = None

    def __len__(self):
        return len(self.data)

    def addByte(self, b):
        if b == 0x7D:
            self.state = self.STATE_ESCAPE
        elif self.state == self.STATE_ESCAPE:
            self.state = self.STATE_READ
            b = b ^ 0x20
            self.data.append(b)
        else:
            self.data.append(b)

    def finish(self):
        self.crc = self.data[-2:]
        self.data = self.data[:-2]
        self.finished = True

    def checkCRC(self):
        res = bool(self.crc == calcCRC(self.data))
        if not res:
            c1 = str(self.crc)
            c2 = str(calcCRC(self.data))
            logger.warning("invalid crc %s != %s", c1, c2)
            self.error = True
        return res

    def bytes(self):
        return bytes(self.data)


class HDLC(object):
    def __init__(self, debug=False):
        self.current_frame = None
        self.last_frame = None
        self.frame_callback = None
        self.error_callback = None
        self.running = False
        self.debug = debug

    @classmethod
    def toBytes(cls, data):
        return bytearray(data)

    def sendFrame(self, data):
        bs = self._encode(data)
        if self.debug: print("\tHDLC Sending Frame: %s" % bs.hex())
        return bs

    def _onFrame(self, frame):
        self.last_frame = frame
        s = self.last_frame.bytes()
        if self.frame_callback is not None:
            self.frame_callback(s)

    def _onError(self, frame):
        self.last_frame = frame
        s = self.last_frame.bytes()
        logger.warning("Frame Error: %s", s)
        if self.error_callback is not None:
            self.error_callback(s)

    def _readByte(self, b):
        assert 0 <= b <= 255
        if b == 0x7E:
            # Start or End
            if not self.current_frame or len(self.current_frame) < 1:
                # Start
                self.current_frame = Frame()
            else:
                # End
                self.current_frame.finish()
                self.current_frame.checkCRC()
        elif self.current_frame is None:
            # Ignore before Start
            return False
        elif not self.current_frame.finished:
            self.current_frame.addByte(b)
        else:
            # Ignore Bytes
            pass

        # Validate and return
        if self.current_frame.finished and not self.current_frame.error:
            # Success
            self._onFrame(self.current_frame)
            self.current_frame = None
            return True
        elif self.current_frame.finished:
            # Error
            self._onError(self.current_frame)
            self.current_frame = None
            return True
        return False

    @classmethod
    def _encode(cls, bs):
        data = bytearray()
        data.append(0x7E)
        crc = calcCRC(bs)
        bs = bs + crc
        for byte in bs:
            if byte == 0x7E or byte == 0x7D:
                data.append(0x7D)
                data.append(byte ^ 0x20)
            else:
                data.append(byte)
        data.append(0x7E)
        return bytes(data)

    def _receiveLoop(self):
        while self.running:
            i = self.serial.in_waiting
            if i < 1:
                time.sleep(0.001)
                continue
            res = self._readBytes(i)

    def stopReader(self):
        self.running = False
        self.reader.join()
        self.reader = None