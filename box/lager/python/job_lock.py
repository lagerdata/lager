# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
lager.python.job_lock - box-lock lifetime for a detached job.

A `lager python` run auto-locks the box. For an attached run the CLI owns that
lock: it heartbeats while the script runs and releases when the stream ends, and
if the CLI dies the lock's TTL reaps it.

A detached run has no CLI to do any of that -- the whole point is that the
client goes away. The lock was therefore acquired with ``ttl_seconds: null``,
an eternal hold released by hand with `lager boxes unlock`. That is workable
when the job runs, and a trap when it does not: a detached job that fails to
start left the box locked with nothing running on it and nothing to notice.

So the box holds the lock for exactly as long as the job it launched is alive.
This module is that: a timer thread that heartbeats the holder the CLI acquired
with, and releases when the job ends, whether it ended by finishing, by being
killed, or by never starting at all.

Two properties matter and are easy to get wrong:

* **It can only ever touch the launching client's lock.** ``lock_state``
  matches on the holder string -- ``heartbeat`` answers 403 for a foreign
  holder and 404 when the box is not locked, and ``release`` refuses on a
  mismatch -- so every operation here is a no-op unless the lock is still the
  one that came with this job.
* **It stops rather than fights.** Anything other than a 200 from a heartbeat
  means the lock is no longer ours: someone forced it, or it lapsed. Log once,
  stop, and leave it alone.
"""

import logging
import threading

from .. import lock_state

logger = logging.getLogger(__name__)

# How often to refresh. Matches the CLI's own default heartbeat cadence, and
# sits far enough inside the default 1800s TTL that a missed tick is harmless.
HEARTBEAT_INTERVAL_S = 60


class DetachedJobLock:
    """
    Keeps the box lock alive for exactly as long as a detached job runs.

    Inert when constructed without a holder, which is the case for a run that
    did not auto-lock (`LAGER_AUTO_LOCK_DISABLE`), one whose lock was a
    pre-existing reservation the CLI must not hand over, or a request from a
    CLI too old to send one. Callers do not branch on that -- start() and
    stop() are always safe to call.
    """

    def __init__(self, holder, interval=HEARTBEAT_INTERVAL_S):
        """
        Args:
            holder: the lock holder string the CLI acquired with, or None
            interval: seconds between heartbeats
        """
        self.holder = holder
        self.interval = interval
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        """Begin heartbeating. No-op without a holder, or if already started."""
        if not self.holder or self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name='lager-detached-lock',
            daemon=True,
        )
        self._thread.start()

    def stop(self, release=True):
        """
        Stop heartbeating, and by default release the lock.

        Releasing rather than merely letting the TTL lapse matters most under
        CI, where the holder string embeds the runner's pid: a lock left behind
        by a finished job has a holder the next run can never match, so it
        would block that run for a whole TTL for no reason.

        Args:
            release: whether to release the lock as well as stop refreshing it
        """
        self._stop.set()
        if not self.holder:
            return
        if release:
            try:
                code, body = lock_state.release(user=self.holder, force=False)
            except Exception as exc:
                logger.warning(f"Failed to release lock for {self.holder}: {exc}")
                return
            if code == 200:
                logger.info(f"Released box lock held for detached job ({self.holder})")
            else:
                # 403: someone else holds it now. Not ours to release.
                logger.info(
                    f"Did not release box lock for {self.holder}: "
                    f"{body.get('error', code)}"
                )

    def _run(self):
        """Heartbeat until the job ends or the lock stops being ours."""
        while not self._stop.wait(self.interval):
            try:
                code, body = lock_state.heartbeat(user=self.holder)
            except Exception as exc:
                logger.warning(f"Lock heartbeat failed for {self.holder}: {exc}")
                return
            if code != 200:
                logger.warning(
                    f"Stopping lock heartbeat for detached job: "
                    f"{body.get('error', code)}. The box lock is no longer held "
                    f"by {self.holder}, so this job is running unlocked."
                )
                return
