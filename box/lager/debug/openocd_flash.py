# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Program and erase an OpenOCD-backed target: the one place that decides how.

Two callers flash through OpenOCD -- the HTTP debug service behind
``lager debug <net> flash`` / ``erase`` (``service.py``) and the in-box
Python Net API behind ``DebugNet.flash()`` / ``.erase()``
(``nets/debug_net.py``). For most targets both hand the running daemon
``program <file> [addr] verify reset``, or erase every flash bank it
declares. The DA1469x family is the exception: it executes from external
QSPI at ``0x16000000``, mainline OpenOCD has no flash driver for that QSPI,
and lager drives the RAM-resident Apache Mynewt flash_loader instead
(``da1469x_loader.py``).

That special case used to be spelled out inline in the service handlers
only. The Net API had no copy, so on the same box, same board, same image,
``lager debug SWD flash`` succeeded while ``dbg.flash(...)`` died with a raw
``** Programming Failed **`` and ``dbg.erase()`` returned nothing after
touching nothing -- a bench that looks healthy by hand and fails only under
automation, and a sequence that leaves the DUT blank. Two implementations of
one decision drift. This module is the decision; both callers route through
it, and ``test/unit/box/test_debug_flash_dispatch_parity.py`` fails the
build if either grows a private copy or calls the generic commands directly.

Both entry points are generators, like the loader and the J-Link helpers
they front: progress lines stream to the caller, which logs and collects
them as it likes. A failure raises :class:`Da1469xLoaderError` on the loader
path -- naming the step that failed and, when the erase stage had already
run, warning that the board may now be blank -- or :class:`OpenOcdRpcError`
on the generic path. :data:`FLASH_ERRORS` is the pair, for callers that turn
failures into a response.
"""

import logging
from typing import Iterator, Optional

from .da1469x_loader import (
    DA1469X_FAMILY,
    DEFAULT_ERASE_LENGTH,
    Da1469xLoaderError,
    erase_range,
    flash_image,
    xip_to_flash_offset,
)
from .openocd import OpenOcdRpcError
from .probes import is_da1469x

logger = logging.getLogger(__name__)

#: RPC timeouts both callers build their ``OpenOcdRpc`` with. One value
#: each, so the service and the Net API cannot wait different lengths for
#: the same operation. The flash budget covers a full loader run on a
#: DA1469x (about 75 s for a 700 KiB image) with room for slow probes.
FLASH_RPC_TIMEOUT_S = 300
ERASE_RPC_TIMEOUT_S = 120

#: Everything :func:`flash_target` / :func:`erase_target` raise for a failed
#: operation.
FLASH_ERRORS = (Da1469xLoaderError, OpenOcdRpcError)


def flash_target(rpc, device, firmware_path, *,
                 address: Optional[int] = None) -> Iterator[str]:
    """Program *firmware_path* onto *device* through the daemon at *rpc*.

    *address* is the load address of a raw ``.bin`` and ``None`` for
    ``.hex`` / ``.elf``, which carry their own. On a DA1469x it is the
    absolute XIP address the CLI and the J-Link path accept (``0x16000000``
    for the start of QSPI); the flash-relative offset the loader wants is
    derived here, so every caller speaks the same address space.

    Yields progress lines. Raises one of :data:`FLASH_ERRORS`.
    """
    if is_da1469x(device):
        offset = xip_to_flash_offset(address)
        yield from _run_loader(
            flash_image(
                rpc, firmware_path,
                family=DA1469X_FAMILY, flash_id=0, offset=offset,
            ),
            doing='flash',
        )
        return
    out = rpc.program(firmware_path, verify=True, reset_after=True, address=address)
    if out:
        yield out


def erase_target(rpc, device) -> Iterator[str]:
    """Erase *device*'s flash through the daemon at *rpc*.

    Every flash bank on a target that declares them. On a DA1469x, the
    loader's address-range erase of the first :data:`DEFAULT_ERASE_LENGTH`
    bytes of QSPI, matching the J-Link path -- ``flash erase_sector`` has
    no bank to act on there and would report nothing after touching
    nothing.

    Yields progress lines. Raises one of :data:`FLASH_ERRORS`.
    """
    if is_da1469x(device):
        yield from _run_loader(
            erase_range(
                rpc,
                family=DA1469X_FAMILY, flash_id=0, offset=0,
                length=DEFAULT_ERASE_LENGTH,
            ),
            doing='erase',
        )
        return
    out = rpc.flash_erase_all()
    if out:
        yield out


def _run_loader(progress, *, doing) -> Iterator[str]:
    """Stream a loader generator, re-raising a failure with the step named.

    A loader or RPC failure surfaces as :class:`Da1469xLoaderError` naming
    the last progress line -- "the loader failed doing X" -- rather than an
    OpenOCD tcl traceback. A flash that dies after its erase stage says so:
    the loader erases before it programs, and "Programming Failed" alone
    does not tell the operator the board is now blank.
    """
    lines = []
    try:
        for line in progress:
            lines.append(line)
            yield line
    except (Da1469xLoaderError, OpenOcdRpcError) as exc:
        after = f" after '{lines[-1]}'" if lines else ''
        blank = ''
        if doing == 'flash' and any(l.startswith('Erasing') for l in lines):
            blank = (' The QSPI erase already ran, so the board may be left'
                     ' blank until a flash succeeds.')
        raise Da1469xLoaderError(
            f'DA1469x flash_loader {doing} failed{after}: {exc}.{blank}'
        ) from exc
