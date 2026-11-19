# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Background capture jobs for the box HTTP API.

``start_job`` runs ``BleSnifferBase.capture`` on a daemon thread and returns
once the sniffer records, so a busy, missing or silent sniffer still fails
the start request itself; after that the client polls ``get_job`` and ends a
capture early with ``request_stop``. A job lives in the box HTTP server's
process: jobs end with it, and a capture from a ``lager python`` script is
not a job. The last ``_KEEP_JOBS`` finished jobs stay queryable; their files
are pruned with the other captures.
"""

from __future__ import annotations

import collections
import logging
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from lager.exceptions import BleSnifferBackendError
from lager.protocols.ble_sniffer.ble_sniffer_net import BleSnifferBusyError

if TYPE_CHECKING:
    from lager.protocols.ble_sniffer.ble_sniffer_net import (
        BleSnifferBase,
        CaptureContext,
        CaptureOptions,
    )

logger = logging.getLogger(__name__)

# Seconds start_job waits for the sniffer to record: the lock retry, opening
# the port and the 2 s version exchange, with room to spare.
_START_TIMEOUT_S = 10.0
_KEEP_JOBS = 50

# Every job of this process, oldest first; shared by the HTTP request threads
# and the capture threads, so it is guarded by _jobs_lock.
_jobs: collections.OrderedDict[str, CaptureJob] = collections.OrderedDict()
_jobs_lock = threading.Lock()


@dataclass
class CaptureJob:
    """One capture running or finished on a background thread.

    ``state`` is ``starting``, ``running``, ``done`` or ``failed``. A failed
    job has ``error`` and ``error_kind`` (``busy``, ``invalid`` or
    ``backend``); ``file_saved`` says whether its file still holds the
    packets recorded before the failure.
    """

    id: str
    net: str
    identity: str
    file: Path
    duration: float
    state: str = "starting"
    result: dict | None = None
    error: str | None = None
    error_kind: str | None = None
    _ctx: CaptureContext | None = field(default=None, repr=False)
    _final_elapsed: float | None = field(default=None, repr=False)
    _ready: threading.Event = field(default_factory=threading.Event, repr=False)
    _done: threading.Event = field(default_factory=threading.Event, repr=False)

    def to_dict(self) -> dict:
        """The job as the box HTTP API reports it."""
        ctx = self._ctx
        elapsed = self._final_elapsed
        if elapsed is None:
            elapsed = ctx.elapsed if ctx is not None else 0.0
        return {
            "job": self.id, "net": self.net, "state": self.state, "file": str(self.file),
            "duration": self.duration,
            "elapsed": round(elapsed, 3),
            "packets": ctx.writer.packets if ctx is not None else 0,
            "result": self.result, "error": self.error, "error_kind": self.error_kind,
            "file_saved": self.state == "failed" and self.file.is_file(),
        }

    def _run(self, driver: BleSnifferBase, options: CaptureOptions) -> None:
        try:
            self.result = driver.capture(options, self.file, on_start=self._started)
            self.state = "done"
        except BleSnifferBusyError as e:
            self._fail("busy", e)
        except ValueError as e:
            self._fail("invalid", e)
        except BleSnifferBackendError as e:
            self._fail("backend", e)
        except Exception as e:
            logger.exception("ble-sniffer: capture job %s failed", self.id)
            self._fail("backend", e)
        finally:
            if self._ctx is not None:
                self._final_elapsed = (self.result or {}).get("duration", self._ctx.elapsed)
            self._ready.set()
            self._done.set()

    def _started(self, ctx: CaptureContext) -> None:
        self._ctx = ctx
        self.state = "running"
        self._ready.set()

    def _fail(self, kind: str, error: Exception) -> None:
        self.error, self.error_kind, self.state = str(error), kind, "failed"


def start_job(net: str, driver: BleSnifferBase, options: CaptureOptions,
              out: Path) -> CaptureJob:
    """Start a capture on a background thread; return when the sniffer records.

    Raises:
        ValueError: An option is invalid.
        BleSnifferBusyError: A capture already holds the sniffer.
        BleSnifferBackendError: The sniffer is missing or did not start.
    """
    options.validate()
    job = CaptureJob(id=uuid.uuid4().hex[:12], net=net, identity=driver.identity,
                     file=Path(out), duration=float(options.duration))
    with _jobs_lock:
        _jobs[job.id] = job
        _trim()
    threading.Thread(target=job._run, args=(driver, options), daemon=True,
                     name=f"ble-sniffer-{job.id}").start()
    if not job._ready.wait(_START_TIMEOUT_S):
        logger.warning("ble-sniffer: capture job %s still starting after %.0f s",
                       job.id, _START_TIMEOUT_S)
    if job.state == "failed":
        with _jobs_lock:
            _jobs.pop(job.id, None)
        if job.error_kind == "busy":
            raise BleSnifferBusyError(job.error)
        if job.error_kind == "invalid":
            raise ValueError(job.error)
        raise BleSnifferBackendError(job.error)
    return job


def get_job(job_id: str) -> CaptureJob | None:
    """The job with this ID, or None if this process has no such job."""
    with _jobs_lock:
        return _jobs.get(job_id)


def latest_job(identity: str) -> CaptureJob | None:
    """The most recent job on the sniffer with this identity, or None."""
    with _jobs_lock:
        for job in reversed(_jobs.values()):
            if job.identity == identity:
                return job
    return None


def wait_for(job: CaptureJob, timeout: float) -> bool:
    """Wait up to ``timeout`` seconds for a job to finish; whether it did."""
    return job._done.wait(timeout)


def _trim() -> None:
    # Called with _jobs_lock held. Drops the oldest finished jobs.
    finished = [j for j in _jobs.values() if j.state in ("done", "failed")]
    for job in finished[:max(0, len(finished) - _KEEP_JOBS)]:
        del _jobs[job.id]
