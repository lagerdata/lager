# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

import asyncio
import contextlib
import os
import platform

class Event_ts(asyncio.Event):
    """An ``asyncio.Event`` that can be set from outside its loop's thread.

    ``asyncio.Event`` is not thread-safe: setting one from another thread
    wakes no waiter, because the waiter's future is only scheduled from
    inside the loop. Here the BLE callbacks arrive on bleak's own thread and
    the waiters live on this loop, so every mutation is handed to the loop
    with ``call_soon_threadsafe``.

    The loop is required rather than discovered. ``asyncio.get_event_loop()``
    has been deprecated since 3.10 and raises with no running loop on 3.12,
    and the one thing this class must not do is guess which loop its waiters
    are on.
    """

    def __init__(self, loop):
        super().__init__()
        self._loop_ts = loop

    def set(self):
        self._loop_ts.call_soon_threadsafe(super().set)

    def clear(self):
        self._loop_ts.call_soon_threadsafe(super().clear)

def generateAESIV(seq):
    iv = bytearray(16)
    iv[0] = seq & 0xff
    return iv

async def event_wait(evt, timeout):
    # suppress TimeoutError because we'll return False in case of timeout
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(evt.wait(), timeout)
    return evt.is_set()

def get_platform_type() -> str:
    """
    Gets the platform type.
    """
    if platform.system() == "Linux":
        return 'Linux'

    if platform.system() == "Darwin":
        return 'Darwin'

    if platform.system() == "Windows":
        return 'Windows'

    raise Exception(f"Unsupported platform: {platform.system()}")
