# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
DA1469x QSPI flash via the Apache Mynewt RAM-resident ``flash_loader`` app,
driven over OpenOCD's TCL/RPC channel.

Mainline OpenOCD has no QSPI flash driver for the Dialog/Renesas DA1469x
family, so the OpenOCD ``program`` command can't touch external NOR at the
XIP base ``0x16000000``. The standard workaround (also what works against
this rig from a laptop) is to load the upstream
``apache-mynewt-core/apps/flash_loader`` app into RAM, jump to it, and drive
its small command struct over the debug link to do erase / program / verify.

This module is the box-side, pure-Python translation of the GDB scripts in
[xl/openocd/flash_loader/](xl/openocd/flash_loader/) (``flash.gdb``,
``erase.gdb``, ``flash_loader.gdb``) — same sequence, same memory writes,
same protocol — but issued via :class:`OpenOcdRpc` instead of an external
``gdb-multiarch``. That keeps the box-side debug service self-contained
(no extra subprocess, no extra binary dependency) and lets the existing
``lager debug SWD flash`` / ``lager debug SWD erase`` CLI dispatch into
the same code path used for J-Link DA14695.

Loader artefacts (``flash_loader.elf`` + ``flash_loader.elf.bin``) are not
shipped as part of lager — they're build artefacts of the chip-specific
loader build. The operator drops them onto the box under
``/home/www-data/flash-loaders/<family>/`` (override the parent dir with
``LAGER_FLASH_LOADERS_DIR``); we resolve symbol addresses by parsing the
``.elf`` directly so any compatible loader build "just works".
"""

import logging
import os
import re
import struct
import tempfile
import time
from typing import Dict, Iterator, Iterable, Optional, Tuple

from .openocd import OpenOcdRpc, OpenOcdRpcError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants / memory map
# ---------------------------------------------------------------------------

# Default location for chip-family-keyed loader artefacts on the box. Sits
# next to ``customer-binaries`` so operators can drop files via ``lager box
# ssh`` the same way they upload custom tools.
DEFAULT_FLASH_LOADERS_DIR = '/home/www-data/flash-loaders'
ENV_LOADER_DIR_OVERRIDE = 'LAGER_FLASH_LOADERS_DIR'
LOADER_ELF_NAME = 'flash_loader.elf'
LOADER_BIN_NAME = 'flash_loader.elf.bin'

# DA1469x chip-family identifier — used as the per-family subdirectory name.
# The matching device-name substring is ``DA1469`` (covers DA14691/DA14693/
# DA14695/DA14697/DA14699). Adding another family later is just another
# directory and (optionally) another sequencer module.
DA1469X_FAMILY = 'da1469x'

# DA1469x QSPI XIP window. Absolute CPU memory addresses inside this range
# correspond to ``addr - QSPI_XIP_BASE`` flash offsets — which is what the
# loader's ``fl_cmd_flash_addr`` actually wants. Keeping the constants here
# (and not in jlink.py) lets both backends translate the same way; the J-Link
# path uses absolute XIP, the loader path uses flash-relative offsets, and
# the CLI accepts the absolute form (matching the J-Link convention).
QSPI_XIP_BASE = 0x16000000
QSPI_XIP_RANGE = 0x02000000  # 32 MiB — datasheet maximum XIP window.
QSPI_XIP_END = QSPI_XIP_BASE + QSPI_XIP_RANGE  # exclusive upper bound

# RAM address the Mynewt RAM-resident loader links at. Vector table sits at
# the very start: word[0] = MSP, word[1] = reset handler PC. Matches
# [xl/openocd/flash_loader/flash.gdb:7-10](xl/openocd/flash_loader/flash.gdb).
LOADER_RAM_BASE = 0x20000000

# Pre-load DA1469x register pokes (mirrors the working flash.gdb / erase.gdb).
# Names from the DA1469x datasheet / SVD. We don't expose these as args
# because the loader bring-up sequence is fixed for this family.
REG_POR_PIN_DEBUG_ENABLE = 0x50000098  # CRG_TOP+0x98 — debug-enable poke
REG_POR_PIN_DEBUG_ENABLE_VALUE = 0x3F
REG_QSPIC_DUMMYBYTES = 0x38000080
REG_MTB_POSITION = 0xE0043000
REG_MTB_MASTER = 0xE0043004
REG_MTB_FLOW = 0xE0043008
REG_MPU_CTRL = 0xE000ED94
REG_SYS_CTRL_REG = 0x100C0050  # write 1 -> bootrom re-runs (software reset)

# Symbols we resolve from the loader ELF. Names match the upstream Apache
# Mynewt ``apps/flash_loader/src/main.c`` globals (same set the ``fl_*``
# GDB macros at [xl/openocd/flash_loader/flash_loader.gdb] reference).
LOADER_SYMBOLS = (
    'fl_state',
    'fl_cmd',
    'fl_cmd_rc',
    'fl_cmd_flash_id',
    'fl_cmd_flash_addr',
    'fl_cmd_amount',
    'fl_cmd_data',
    'fl_cmd_data_sz',
    'mynewt_main',
)

# Loader command IDs (Apache Mynewt ``apps/flash_loader/src/main.c``).
FL_CMD_NONE = 0
FL_CMD_PING = 1
FL_CMD_ERASE = 3
FL_CMD_PROGRAM_VERIFY = 5
# return code conventions: 0 = pending, 1 = ok, anything else = error.
FL_RC_OK = 1

# Loader state values: 1 = ready/idle. Anything else means the loader is
# either still booting or busy with a command.
FL_STATE_READY = 1

# Default sizes / timeouts. Most are conservative — the protocol is fast on
# this chip family, but we'd rather wait an extra second than give up early.
DEFAULT_FAMILY = DA1469X_FAMILY
DEFAULT_FLASH_ID = 0
DEFAULT_FLASH_OFFSET = 0
DEFAULT_ERASE_LENGTH = 1 << 20  # 1 MiB — matches J-Link _DA1469X_QSPI_RANGE_BYTES.

_LOADER_BOOT_TIMEOUT_S = 10.0
_FL_PING_TIMEOUT_S = 5.0
_FL_ERASE_TIMEOUT_S = 60.0  # whole-bank erase can take a few seconds on QSPI NOR.
_FL_CHUNK_TIMEOUT_S = 30.0  # one program-and-verify iteration.
_FL_FINAL_READY_TIMEOUT_S = 10.0
_POLL_INTERVAL_S = 0.05


class Da1469xLoaderError(Exception):
    """Raised when the DA1469x flash_loader path cannot complete the request.

    Distinct from :class:`OpenOcdRpcError` so callers can tell "the loader
    bounced us back with an error code" / "the loader never came up" apart
    from raw RPC transport / OpenOCD-side failures.
    """


# ---------------------------------------------------------------------------
# Address translation: CLI XIP addresses → loader flash offsets
# ---------------------------------------------------------------------------


def xip_to_flash_offset(addr: Optional[int]) -> int:
    """Translate a CLI ``--bin <file>,<addr>`` value to a QSPI flash offset.

    The CLI convention (matching the J-Link DA1469x path at
    ``lager/box/lager/debug/jlink.py``) is to accept absolute CPU/XIP
    addresses, e.g. ``0x16000000`` for "start of QSPI". The loader's
    ``fl_cmd_flash_addr`` field, however, is a flash-relative offset
    (``0x0`` for the same location). Translate by subtracting
    :data:`QSPI_XIP_BASE`. ``None`` and ``0`` are treated as "no address
    given — write from the start of QSPI".

    Raises :class:`Da1469xLoaderError` when *addr* is non-zero but does
    not lie inside the QSPI XIP window — that nearly always means the
    caller passed a flash offset by mistake (where they meant XIP) or
    something completely off-target.
    """
    if addr is None or addr == 0:
        return 0
    if not (QSPI_XIP_BASE <= addr < QSPI_XIP_END):
        raise Da1469xLoaderError(
            f'flash address {hex(addr)} is outside the DA1469x QSPI XIP '
            f'window ({hex(QSPI_XIP_BASE)}–{hex(QSPI_XIP_END - 1)}). Pass '
            f'an absolute XIP address (e.g. {hex(QSPI_XIP_BASE)}) or omit '
            f'the address to flash from the start of QSPI.'
        )
    return addr - QSPI_XIP_BASE


# ---------------------------------------------------------------------------
# Loader artefact resolution
# ---------------------------------------------------------------------------


def _flash_loaders_root() -> str:
    """Return the per-box flash-loaders root, honouring the env override."""
    return os.environ.get(ENV_LOADER_DIR_OVERRIDE) or DEFAULT_FLASH_LOADERS_DIR


def _resolve_loader_paths(family: str) -> Tuple[str, str]:
    """Return ``(elf_path, bin_path)`` for *family*, raising with an
    actionable message if either is missing.

    Layout: ``<root>/<family>/flash_loader.elf`` and ``...elf.bin``. The
    operator is expected to drop these via ``lager box ssh`` once per box.
    """
    root = _flash_loaders_root()
    family_dir = os.path.join(root, family)
    elf_path = os.path.join(family_dir, LOADER_ELF_NAME)
    bin_path = os.path.join(family_dir, LOADER_BIN_NAME)

    missing = [p for p in (elf_path, bin_path) if not os.path.isfile(p)]
    if missing:
        raise Da1469xLoaderError(
            f'{family} flash via OpenOCD requires flash_loader artefacts on the box.\n'
            f'Expected:\n'
            f'  {elf_path}\n'
            f'  {bin_path}\n'
            f'Run `lager box ssh <box>` and copy the matching loader build into '
            f'{family_dir} (override the parent dir with '
            f'{ENV_LOADER_DIR_OVERRIDE}=<path>). Missing: '
            f'{", ".join(os.path.basename(p) for p in missing)}.'
        )
    return elf_path, bin_path


# ---------------------------------------------------------------------------
# Minimal ELF32 symbol-table reader
# ---------------------------------------------------------------------------
#
# Just enough of ELF to pull a name -> st_value map for global symbols.
# We avoid pulling pyelftools onto the box for one feature; the format we
# care about is little-endian ELF32 (Cortex-M33). The standard layout is
# documented in the System V gABI; offsets used below match that spec.


_ELF_MAGIC = b'\x7fELF'
_EI_CLASS = 4
_EI_DATA = 5
_ELFCLASS32 = 1
_ELFDATA2LSB = 1
_SHT_SYMTAB = 2
_SHT_STRTAB = 3


def _parse_elf32_symbols(elf_bytes: bytes) -> Dict[str, int]:
    """Return ``{symbol_name: st_value}`` for every symbol in *elf_bytes*.

    Supports little-endian ELF32 only (which is what every Cortex-M Mynewt
    build produces). Raises ``ValueError`` for anything else so callers see
    a sharp failure if someone drops the wrong loader on the box.
    """
    if elf_bytes[:4] != _ELF_MAGIC:
        raise ValueError('not an ELF file (bad magic)')
    if elf_bytes[_EI_CLASS] != _ELFCLASS32:
        raise ValueError('flash_loader ELF is not ELF32 (loader is built RAM-resident '
                         'for Cortex-M, expected ELFCLASS32)')
    if elf_bytes[_EI_DATA] != _ELFDATA2LSB:
        raise ValueError('flash_loader ELF is not little-endian')

    # ELF32 header (offset, size in bytes) — only the fields we use:
    #   e_shoff      @ 0x20  (4)
    #   e_shentsize  @ 0x2E  (2)
    #   e_shnum      @ 0x30  (2)
    e_shoff, = struct.unpack_from('<I', elf_bytes, 0x20)
    e_shentsize, e_shnum = struct.unpack_from('<HH', elf_bytes, 0x2E)
    if e_shentsize < 0x28:
        raise ValueError(f'ELF32 section header too short ({e_shentsize} bytes)')

    # ELF32 section header layout (40 bytes / Elf32_Shdr):
    #   sh_name       @ 0x00 (4)
    #   sh_type       @ 0x04 (4)
    #   sh_flags      @ 0x08 (4)
    #   sh_addr       @ 0x0C (4)
    #   sh_offset     @ 0x10 (4)
    #   sh_size       @ 0x14 (4)
    #   sh_link       @ 0x18 (4)
    #   sh_info       @ 0x1C (4)
    #   sh_addralign  @ 0x20 (4)
    #   sh_entsize    @ 0x24 (4)
    # Pull just the fields we use, individually, to keep the offsets obvious.
    symtab_off = symtab_size = symtab_entsize = symtab_link = 0
    for i in range(e_shnum):
        base = e_shoff + i * e_shentsize
        sh_type, = struct.unpack_from('<I', elf_bytes, base + 0x04)
        if sh_type == _SHT_SYMTAB:
            symtab_off, = struct.unpack_from('<I', elf_bytes, base + 0x10)
            symtab_size, = struct.unpack_from('<I', elf_bytes, base + 0x14)
            symtab_link, = struct.unpack_from('<I', elf_bytes, base + 0x18)
            symtab_entsize, = struct.unpack_from('<I', elf_bytes, base + 0x24)
            break
    if not symtab_size:
        raise ValueError('flash_loader ELF has no SYMTAB section')

    # The symbol table's sh_link field points at the section index of its
    # paired string table. Pull that section's offset + size.
    if symtab_link >= e_shnum:
        raise ValueError('SYMTAB sh_link out of range')
    strtab_base = e_shoff + symtab_link * e_shentsize
    strtab_type, = struct.unpack_from('<I', elf_bytes, strtab_base + 0x04)
    if strtab_type != _SHT_STRTAB:
        raise ValueError('SYMTAB sh_link does not point at a STRTAB')
    strtab_off, = struct.unpack_from('<I', elf_bytes, strtab_base + 0x10)
    strtab_size, = struct.unpack_from('<I', elf_bytes, strtab_base + 0x14)

    # ELF32 Sym layout (16 bytes):
    #   st_name  (4)  - byte offset into strtab
    #   st_value (4)  - the value (address for our globals)
    #   st_size  (4)
    #   st_info  (1)
    #   st_other (1)
    #   st_shndx (2)
    if symtab_entsize < 16:
        raise ValueError(f'ELF32 SYMTAB entsize too small ({symtab_entsize})')

    syms: Dict[str, int] = {}
    n = symtab_size // symtab_entsize
    for i in range(n):
        sym_off = symtab_off + i * symtab_entsize
        st_name, st_value = struct.unpack_from('<II', elf_bytes, sym_off)
        if st_name == 0 or st_name >= strtab_size:
            continue
        end = elf_bytes.index(b'\x00', strtab_off + st_name)
        name = elf_bytes[strtab_off + st_name:end].decode('utf-8', errors='replace')
        if not name:
            continue
        # Last write wins — duplicate names are exceedingly rare for the
        # globals we care about, and this matches what GDB's symbol-file
        # would resolve.
        syms[name] = st_value
    return syms


def _resolve_loader_symbols(elf_path: str,
                            required: Iterable[str] = LOADER_SYMBOLS) -> Dict[str, int]:
    """Read *elf_path* and return a ``{name: address}`` map for *required*.

    Raises :class:`Da1469xLoaderError` (not ``ValueError``) so the caller
    surfaces the same error type as the rest of this module.
    """
    try:
        with open(elf_path, 'rb') as f:
            data = f.read()
    except OSError as exc:
        raise Da1469xLoaderError(f'failed to read {elf_path}: {exc}') from exc
    try:
        all_syms = _parse_elf32_symbols(data)
    except ValueError as exc:
        raise Da1469xLoaderError(f'cannot parse {elf_path}: {exc}') from exc

    missing = [name for name in required if name not in all_syms]
    if missing:
        raise Da1469xLoaderError(
            f'flash_loader ELF {elf_path} is missing required symbols: '
            f'{", ".join(missing)} (is this an Apache Mynewt apps/flash_loader '
            f'build for the right BSP?)'
        )
    return {name: all_syms[name] for name in required}


# ---------------------------------------------------------------------------
# Loader bring-up + protocol
# ---------------------------------------------------------------------------


def _poll_word(rpc: OpenOcdRpc, address: int, predicate, *,
               timeout_s: float, label: str) -> int:
    """Poll ``mdw(address)`` until *predicate(value)* is True or *timeout_s*
    elapses. Returns the matching value; raises :class:`Da1469xLoaderError`
    on timeout.
    """
    deadline = time.monotonic() + timeout_s
    last = None
    while True:
        last = rpc.mdw(int(address))
        if predicate(last):
            return last
        if time.monotonic() >= deadline:
            raise Da1469xLoaderError(
                f'flash_loader {label}: timed out after {timeout_s:.1f}s '
                f'(last value at {hex(address)} = {hex(last)})'
            )
        time.sleep(_POLL_INTERVAL_S)


def _prepare_loader(rpc: OpenOcdRpc, elf_path: str, bin_path: str,
                    syms: Dict[str, int]) -> Iterator[str]:
    """Reset, load the loader into RAM, jump to it, wait for ready.

    Mirrors [xl/openocd/flash_loader/flash.gdb:1-26](xl/openocd/flash_loader/flash.gdb)
    line-for-line. Yields human-readable progress lines; raises on any
    OpenOCD or loader-level error.
    """
    yield f'Preparing DA1469x flash_loader from {elf_path}'

    # POR-pin debug enable poke. The user added this to the OpenOCD scripts
    # specifically (the J-Link path doesn't need it because Commander's
    # device profile handles equivalent setup).
    rpc.mww(REG_POR_PIN_DEBUG_ENABLE, REG_POR_PIN_DEBUG_ENABLE_VALUE)

    # ``mon reset halt`` -> ``shell sleep 1``. Reset must put the core in a
    # halted, known state before we drop a fresh image into RAM. The 1s
    # settle matches the GDB script — gives QSPI / clocks time to come up
    # via the bootrom before we override them.
    rpc.reset(halt=True)
    rpc.sleep_ms(1000)

    # Load the loader image into RAM at 0x20000000 (the linked base).
    yield f'Loading {LOADER_BIN_NAME} into RAM at {hex(LOADER_RAM_BASE)}'
    rpc.load_image(bin_path, LOADER_RAM_BASE, fmt='bin')
    rpc.sleep_ms(1000)

    # Set MSP and PC from the loader's vector table — same as
    # ``set $msp=*(int *)0x20000000; set $pc=*(int *)0x20000004``.
    msp = rpc.mdw(LOADER_RAM_BASE)
    pc = rpc.mdw(LOADER_RAM_BASE + 4)
    yield f'Loader vector table: MSP={hex(msp)} PC={hex(pc)}'
    rpc.reg_write('msp', msp)
    rpc.reg_write('pc', pc)

    # Disable QSPIC / MTB before we run — same writes the GDB scripts make.
    # MPU is intentionally disabled later, while the loader is running.
    rpc.mww(REG_QSPIC_DUMMYBYTES, 0)
    rpc.mww(REG_MTB_POSITION, 0)
    rpc.mww(REG_MTB_MASTER, 0)
    rpc.mww(REG_MTB_FLOW, 0)

    # Defensive: clear any breakpoints OpenOCD is still holding from a
    # previous half-finished bring-up. ``reset halt`` resets the target's
    # hardware FPB registers, but OpenOCD's *internal* bp table is software
    # and survives reset — so a stale entry at our ``mynewt_main`` address
    # makes the next ``rpc.bp(mynewt_main, ...)`` call fail with
    # ``Breakpoint at 0x... already exists``. List + remove is cheap and
    # only runs at bring-up; we tolerate per-bp cleanup failures so a
    # transient ``rbp`` problem doesn't block the real flash work.
    try:
        stale_bps = rpc.bp_list()
    except OpenOcdRpcError as exc:
        logger.info('bp listing failed (%s); skipping defensive clear', exc)
        stale_bps = []
    for stale_addr in stale_bps:
        yield f'Clearing stale breakpoint at {hex(stale_addr)}'
        try:
            rpc.rbp(stale_addr)
        except OpenOcdRpcError as exc:
            logger.warning(
                'rbp at %s failed during defensive clear: %s',
                hex(stale_addr), exc,
            )

    # ``b mynewt_main; c; d 1`` — break at the loader's entry, run until we
    # hit it, then drop the breakpoint so the next ``resume`` goes straight
    # into the command-poll loop.
    mynewt_main = syms['mynewt_main']
    rpc.bp(mynewt_main, length=4, hw=True)
    try:
        rpc.resume()
        rpc.wait_halt(timeout_ms=5000)
    finally:
        # Always clear the BP, even if wait_halt times out — leaves the
        # breakpoint table clean for the next attempt.
        try:
            rpc.rbp(mynewt_main)
        except OpenOcdRpcError as exc:
            logger.warning('rbp at %s after wait_halt failed: %s',
                           hex(mynewt_main), exc)

    # Resume into the loader's main, then disable the MPU on the fly. The
    # MPU write happens through the debug AP while the CPU runs — Cortex-M
    # allows that. Keeps the loader's command-poll loop from being trapped
    # by any default region permissions inherited from the boot path.
    rpc.resume()
    rpc.mww(REG_MPU_CTRL, 0)

    # Wait for the loader to finish its own init and report ``fl_state==1``.
    yield 'Waiting for flash_loader to be ready...'
    _poll_word(
        rpc, syms['fl_state'], lambda v: v == FL_STATE_READY,
        timeout_s=_LOADER_BOOT_TIMEOUT_S,
        label='boot (fl_state==1)',
    )
    yield 'flash_loader ready'


def _fl_ping(rpc: OpenOcdRpc, syms: Dict[str, int]) -> None:
    """Equivalent of the ``fl_ping`` GDB macro: clear ``fl_cmd_rc``, set
    ``fl_cmd=1``, wait for ``fl_cmd_rc != 0``, raise on non-OK."""
    rpc.mww(syms['fl_cmd_rc'], 0)
    rpc.mww(syms['fl_cmd'], FL_CMD_PING)
    rc = _poll_word(
        rpc, syms['fl_cmd_rc'], lambda v: v != 0,
        timeout_s=_FL_PING_TIMEOUT_S, label='ping (fl_cmd_rc!=0)',
    )
    if rc != FL_RC_OK:
        raise Da1469xLoaderError(f'flash_loader ping returned rc={rc}')


def _fl_erase(rpc: OpenOcdRpc, syms: Dict[str, int],
              flash_id: int, addr: int, amount: int) -> None:
    """Equivalent of the ``fl_erase`` GDB macro: ping, set parameters,
    issue ``fl_cmd=3``, wait for completion."""
    _fl_ping(rpc, syms)
    rpc.mww(syms['fl_cmd_rc'], 0)
    rpc.mww(syms['fl_cmd_flash_id'], flash_id)
    rpc.mww(syms['fl_cmd_flash_addr'], addr)
    rpc.mww(syms['fl_cmd_amount'], amount)
    rpc.mww(syms['fl_cmd'], FL_CMD_ERASE)
    rc = _poll_word(
        rpc, syms['fl_cmd_rc'], lambda v: v != 0,
        timeout_s=_FL_ERASE_TIMEOUT_S, label='erase (fl_cmd_rc!=0)',
    )
    if rc != FL_RC_OK:
        raise Da1469xLoaderError(
            f'flash_loader erase {hex(addr)}+{amount} returned rc={rc}'
        )


def _fl_program(rpc: OpenOcdRpc, syms: Dict[str, int],
                image_path: str, flash_id: int, offset: int) -> Iterator[str]:
    """Equivalent of the ``fl_program`` GDB macro: stream the image through
    the ``fl_cmd_data`` buffer in ``fl_cmd_data_sz``-byte chunks."""
    _fl_ping(rpc, syms)

    file_size = os.path.getsize(image_path)
    if file_size == 0:
        raise Da1469xLoaderError(f'flash_loader program: empty image {image_path}')

    buf_sz = rpc.mdw(syms['fl_cmd_data_sz'])
    if buf_sz <= 0:
        raise Da1469xLoaderError(
            f'flash_loader fl_cmd_data_sz={buf_sz}; loader did not initialise correctly'
        )
    yield (f'Programming {file_size} bytes from {os.path.basename(image_path)} '
           f'at flash_id={flash_id} offset={hex(offset)} (chunk={buf_sz})')

    rpc.mww(syms['fl_cmd_flash_id'], flash_id)

    fl_cmd_addr = syms['fl_cmd']
    fl_cmd_rc_addr = syms['fl_cmd_rc']
    fl_cmd_addr_addr = syms['fl_cmd_flash_addr']
    fl_cmd_amount_addr = syms['fl_cmd_amount']
    fl_cmd_data_addr = syms['fl_cmd_data']
    fl_state_addr = syms['fl_state']

    written = 0
    with open(image_path, 'rb') as image:
        with tempfile.NamedTemporaryFile(suffix='.bin', delete=False) as chunk_tmp:
            chunk_path = chunk_tmp.name
        try:
            while written < file_size:
                this_chunk = min(buf_sz, file_size - written)
                chunk_bytes = image.read(this_chunk)
                if len(chunk_bytes) != this_chunk:
                    raise Da1469xLoaderError(
                        f'short read from {image_path}: wanted {this_chunk}, '
                        f'got {len(chunk_bytes)} at offset {written}'
                    )
                # Write the chunk to a temp file so OpenOCD's ``load_image``
                # can stream it efficiently into RAM at fl_cmd_data. The GDB
                # script uses ``restore <file> binary <bias> <off> <end>``
                # with a per-chunk bias to land every chunk at fl_cmd_data;
                # our equivalent is "write the slice, load it at the buffer
                # address every iteration".
                with open(chunk_path, 'wb') as f:
                    f.write(chunk_bytes)
                rpc.load_image(chunk_path, fl_cmd_data_addr, fmt='bin')

                rpc.mww(fl_cmd_addr_addr, offset + written)
                rpc.mww(fl_cmd_amount_addr, this_chunk)
                # NB: do NOT clear ``fl_cmd_rc`` per-chunk. The upstream
                # ``fl_program`` macro in
                # [openocd/flash_loader/flash_loader.gdb:104-130] only ever
                # sets ``fl_cmd_rc=0`` inside ``fl_ping`` / ``fl_erase``;
                # for program it relies on the value latched to ``1`` by the
                # preceding ``fl_ping`` and only checks rc *after the whole
                # loop*. The loader writes ``fl_cmd_rc`` only on overall
                # completion or on error — it does NOT re-assert ``rc=1``
                # after each chunk. Clearing it here turns every successful
                # chunk into a false ``rc=0`` failure (which is exactly the
                # bug we hit on first hardware bring-up: erase ok, then
                # ``program chunk @0x0 (+32768 bytes) returned rc=0``).
                rpc.mww(fl_cmd_addr, FL_CMD_PROGRAM_VERIFY)

                # Per-chunk handshake: the loader sets ``fl_cmd`` back to 0
                # once it has consumed the command (data buffer copied into
                # flash + verified). This is the ONLY per-chunk signal we
                # consume — we deliberately do NOT read ``fl_cmd_rc`` mid-
                # loop. Rationale:
                #
                # 1. The upstream ``fl_program`` macro at
                #    [openocd/flash_loader/flash_loader.gdb:104-130] never
                #    reads rc inside the loop except via the loop guard,
                #    which only triggers on rc *transitioning away from 1*.
                #    On every successful program we've seen by hand, rc
                #    stays latched at 1 the whole time.
                # 2. On real DA1469x hardware (JUL-5) the loader appears
                #    to use ``fl_cmd_rc`` as scratch during a chunk, so an
                #    eager mid-loop read after ``fl_cmd==0`` can catch a
                #    transient value (we observed ``rc=0x66A4E0``) right
                #    before the loader restores it. The upstream macro
                #    side-steps this race entirely by only checking rc
                #    *after* the post-loop ``while fl_state != 1`` returns.
                #    We do the same.
                _poll_word(
                    rpc, fl_cmd_addr, lambda v: v == FL_CMD_NONE,
                    timeout_s=_FL_CHUNK_TIMEOUT_S,
                    label=f'program chunk@{hex(offset + written)} (fl_cmd==0)',
                )

                written += this_chunk
                yield f'  programmed {hex(offset + written)} ({written}/{file_size})'
        finally:
            try:
                os.unlink(chunk_path)
            except OSError:
                pass

    # Final phase, mirroring the ``while fl_state != 1`` + ``if fl_cmd_rc == 1``
    # tail of the upstream ``fl_program`` macro at
    # [openocd/flash_loader/flash_loader.gdb:128-135]: wait for the loader to
    # return to the idle state, then confirm overall success once.
    _poll_word(
        rpc, fl_state_addr, lambda v: v == FL_STATE_READY,
        timeout_s=_FL_FINAL_READY_TIMEOUT_S,
        label='post-program (fl_state==1)',
    )
    final_rc = rpc.mdw(fl_cmd_rc_addr)
    if final_rc != FL_RC_OK:
        raise Da1469xLoaderError(
            f'flash_loader program: overall rc={final_rc} after '
            f'{written}/{file_size} bytes'
        )
    yield f'Programmed {file_size} bytes successfully'


def _software_reset(rpc: OpenOcdRpc) -> None:
    """Issue a DA1469x software reset (``SYS_CTRL_REG.SW_RESET = 1``).

    Mirrors the trailing ``set *(int *)0x100C0050 = 1`` in the GDB scripts.
    Lets the bootrom re-run and pick up the freshly programmed image.
    """
    try:
        rpc.mww(REG_SYS_CTRL_REG, 1)
    except OpenOcdRpcError as exc:
        # The write itself can fail because writing to SYS_CTRL_REG may
        # cause the chip to reset before OpenOCD acks the AP transaction.
        # That's fine for our purposes — log it and move on.
        logger.info('SYS_CTRL_REG write returned %s (expected if chip already reset)', exc)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def flash_image(rpc: OpenOcdRpc, image_path: str, *,
                family: str = DEFAULT_FAMILY,
                flash_id: int = DEFAULT_FLASH_ID,
                offset: int = DEFAULT_FLASH_OFFSET,
                _resolver=_resolve_loader_paths,
                _symbol_resolver=_resolve_loader_symbols) -> Iterator[str]:
    """Flash *image_path* to the chip's external flash via the RAM-resident
    flash_loader. Equivalent to running [xl/openocd/flash_loader/flash.gdb]
    against the OpenOCD GDB server, but executed in-process via TCL/RPC.

    Yields human-readable progress lines for the box-side log; raises
    :class:`Da1469xLoaderError` (or :class:`OpenOcdRpcError`) on failure.

    Parameters mirror the GDB ``fl_load <file> <id> <offset>`` macro: by
    default, flash_id=0, offset=0 — same as the ``fl_load xl.img 0 0`` line
    in [xl/openocd/flash_loader/flash.gdb:27].

    The ``_resolver`` / ``_symbol_resolver`` hooks are dependency-injection
    seams for tests; production callers don't pass them.
    """
    if not os.path.isfile(image_path):
        raise Da1469xLoaderError(f'image not found: {image_path}')

    elf_path, bin_path = _resolver(family)
    syms = _symbol_resolver(elf_path)

    yield from _prepare_loader(rpc, elf_path, bin_path, syms)
    # ``fl_load`` in the GDB macros is just "erase the file size, then
    # program". The loader's erase aligns up to the next sector boundary.
    file_size = os.path.getsize(image_path)
    yield f'Erasing flash_id={flash_id} offset={hex(offset)} bytes={file_size}'
    _fl_erase(rpc, syms, flash_id, offset, file_size)
    yield from _fl_program(rpc, syms, image_path, flash_id, offset)
    _software_reset(rpc)
    yield 'Issued software reset; bootrom will re-init from new image'


def erase_range(rpc: OpenOcdRpc, *,
                family: str = DEFAULT_FAMILY,
                flash_id: int = DEFAULT_FLASH_ID,
                offset: int = DEFAULT_FLASH_OFFSET,
                length: int = DEFAULT_ERASE_LENGTH,
                _resolver=_resolve_loader_paths,
                _symbol_resolver=_resolve_loader_symbols) -> Iterator[str]:
    """Erase ``[offset, offset+length)`` on flash bank *flash_id* via the
    RAM-resident flash_loader. Default 1 MiB at offset 0 — matches the
    ``fl_erase 0 0x00000000 1048576`` line in
    [xl/openocd/flash_loader/erase.gdb:27].

    Yields progress lines; raises on failure.
    """
    if length <= 0:
        raise Da1469xLoaderError(f'erase length must be positive, got {length}')

    elf_path, bin_path = _resolver(family)
    syms = _symbol_resolver(elf_path)

    yield from _prepare_loader(rpc, elf_path, bin_path, syms)
    yield f'Erasing flash_id={flash_id} offset={hex(offset)} bytes={length}'
    _fl_erase(rpc, syms, flash_id, offset, length)
    yield f'Erased {length} bytes successfully'


__all__ = [
    'DA1469X_FAMILY',
    'DEFAULT_ERASE_LENGTH',
    'DEFAULT_FAMILY',
    'DEFAULT_FLASH_ID',
    'DEFAULT_FLASH_OFFSET',
    'Da1469xLoaderError',
    'erase_range',
    'flash_image',
]
