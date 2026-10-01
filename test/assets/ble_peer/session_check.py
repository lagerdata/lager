# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Exercise a BLE session from an on-box script against `lager-ble-peer`
(ble_peer.py in this directory) running on a second box in radio range:

    lager python test/assets/ble_peer/ble_peer.py --box <PEER-BOX> --detach
    lager python test/assets/ble_peer/session_check.py --box <BOX> AA:BB:CC:DD:EE:01

Checks the box has a radio (skipping if not), scans for the peer, then opens
a session, echoes a 600-byte message in 500-byte writes, collects a
500-notification burst, reads a characteristic, and has the peer drop the
link, checking the notifications already received come out before
SessionClosed. Prints one line per step; exits non-zero on a failed check.
"""
import sys
import time

from lager.ble import Session, SessionClosed, adapter, scan

ECHO = "12345678-1234-5678-1234-56789abcdef1"
RX = "12345678-1234-5678-1234-56789abcdef2"
CTRL = "12345678-1234-5678-1234-56789abcdef3"
TICK = "12345678-1234-5678-1234-56789abcdef4"
INFO = "12345678-1234-5678-1234-56789abcdef5"


def main(address):
    radio = adapter()
    if not radio["available"]:
        print("skipping:", radio["reason"])
        return 0
    print("adapter:", [(a["name"], a["address"], a["powered"]) for a in radio["adapters"]])

    found = [d for d in scan(5.0) if d["address"].upper() == address.upper()]
    if found:
        d = found[0]
        print("scan: %s address_type=%s random_type=%s" % (
            d["address"], d["address_type"], d["random_type"]))
    else:
        print("scan: peer not listed (a device the box knows is not always re-announced)")

    t = time.monotonic()
    with Session(address) as s:
        print("open: %.1fs, MTU %d (measured: %s), max write %d" % (
            time.monotonic() - t, s.mtu, s.mtu_is_measured, s.max_write_len))

        s.subscribe(ECHO)
        message = bytes(i % 256 for i in range(600))
        t = time.monotonic()
        s.write(RX, message, chunk_size=500)
        echoed = b""
        while len(echoed) < len(message):
            echoed += s.recv(timeout=3.0).data
        print("echo: %d bytes back in %.2fs, identical: %s" % (
            len(echoed), time.monotonic() - t, echoed == message))
        s.unsubscribe(ECHO)

        s.subscribe(TICK)
        s.write(CTRL, b"\x02\x01\xf4")
        values = set()
        received = 0
        while received < 500:
            try:
                n = s.recv(timeout=2.0)
            except TimeoutError:
                break
            received += 1
            values.add(int.from_bytes(n.data[:4], "big"))
        print("burst: %d notifications, %d distinct values" % (received, len(values)))

        print("read:", s.read(INFO))

        s.write(CTRL, b"\x01")
        drained = 0
        try:
            while True:
                s.recv(timeout=10.0)
                drained += 1
        except SessionClosed as e:
            print("disconnect: %d notification(s) drained first, then: %s (%s)"
                  % (drained, e.message, e.code))
        print("close reason:", s.close_reason)

    ok = echoed == message and received == 500 and len(values) == 500
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
