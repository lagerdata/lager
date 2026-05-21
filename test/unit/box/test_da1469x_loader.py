# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for box/lager/debug/da1469x_loader.py

Covers:
  - Minimal ELF32 symbol-table reader vs. a synthesised fixture ELF.
  - ``_resolve_loader_paths`` env override + missing-file error.
  - ``flash_image`` / ``erase_range`` against an in-memory fake OpenOcdRpc
    that mimics the real loader's ``fl_*`` global behaviour, and verifies
    the exact OpenOCD command sequence matches the working ``flash.gdb`` /
    ``erase.gdb`` scripts in [xl/openocd/flash_loader/].
  - Failure modes: rc != 1 from ping / erase / program raises with the rc.
  - Timeout paths: a fake that never advances ``fl_state`` raises
    ``Da1469xLoaderError``.

Module is loaded via the same stub-package trick as
``test_openocd_dispatch.py`` so the real ``lager`` package's hardware
imports stay out of the test environment.
"""

import importlib.util
import os
import struct
import sys
import tempfile
import types
import unittest


HERE = os.path.dirname(__file__)
DEBUG_DIR = os.path.normpath(
    os.path.join(HERE, '..', '..', '..', 'box', 'lager', 'debug')
)
PROBES_PATH = os.path.join(DEBUG_DIR, 'probes.py')
OPENOCD_PATH = os.path.join(DEBUG_DIR, 'openocd.py')
LOADER_PATH = os.path.join(DEBUG_DIR, 'da1469x_loader.py')


def _load_module(name, path, package=None):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    if package:
        module.__package__ = package
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_pkg_name = 'stub_debug_loader_pkg'
_pkg = types.ModuleType(_pkg_name)
_pkg.__path__ = [DEBUG_DIR]
sys.modules[_pkg_name] = _pkg
_load_module(f'{_pkg_name}.probes', PROBES_PATH, package=_pkg_name)
openocd = _load_module(f'{_pkg_name}.openocd', OPENOCD_PATH, package=_pkg_name)
loader = _load_module(f'{_pkg_name}.da1469x_loader', LOADER_PATH, package=_pkg_name)


# ---------------------------------------------------------------------------
# Tiny ELF32 fixture builder
# ---------------------------------------------------------------------------


def _build_elf32(symbols):
    """Synthesise a minimal little-endian ELF32 with *symbols* in SYMTAB.

    *symbols* is ``{name: st_value}`` — section indices are made up because
    our reader doesn't care, it only pulls names + addresses.

    Layout:
        [Elf32_Ehdr (52)][SYMTAB data][STRTAB data][SHT (4 sections * 40)]

    Sections:
        [0] = SHN_UNDEF (zeroed entry, mandatory)
        [1] = .symtab (SHT_SYMTAB), sh_link = 2
        [2] = .strtab (SHT_STRTAB)
    """
    # Build .strtab first (\0-prefixed, \0-separated) so we know each name's
    # offset before laying out symbols.
    strtab = bytearray(b'\x00')
    name_offsets = {}
    for name in symbols:
        name_offsets[name] = len(strtab)
        strtab += name.encode('utf-8') + b'\x00'

    # Build .symtab. Entry 0 is reserved (all zeros).
    sym_entry_size = 16
    symtab = bytearray(sym_entry_size)  # zero entry
    for name, value in symbols.items():
        st_name = name_offsets[name]
        st_value = value & 0xFFFFFFFF
        st_size = 0
        st_info = 0x12  # STB_GLOBAL << 4 | STT_FUNC — irrelevant to reader
        st_other = 0
        st_shndx = 1
        symtab += struct.pack('<IIIBBH',
                              st_name, st_value, st_size,
                              st_info, st_other, st_shndx)

    # Lay out the file: Ehdr (52) + symtab + strtab + 4 section headers.
    eh_size = 52
    sh_entsize = 40
    sh_count = 4

    symtab_off = eh_size
    strtab_off = symtab_off + len(symtab)
    shoff = strtab_off + len(strtab)
    file_size = shoff + sh_count * sh_entsize

    out = bytearray(file_size)

    # ---- Ehdr ----
    e_ident = bytearray(16)
    e_ident[:4] = b'\x7fELF'
    e_ident[4] = 1   # ELFCLASS32
    e_ident[5] = 1   # ELFDATA2LSB
    e_ident[6] = 1   # EV_CURRENT
    out[0:16] = bytes(e_ident)
    struct.pack_into(
        '<HHIIIIIHHHHHH',
        out, 16,
        2,            # e_type ET_EXEC
        0x28,         # e_machine EM_ARM
        1,            # e_version
        0,            # e_entry
        0,            # e_phoff
        shoff,        # e_shoff
        0,            # e_flags
        eh_size,      # e_ehsize
        0, 0,         # e_phentsize / e_phnum
        sh_entsize,   # e_shentsize
        sh_count,     # e_shnum
        2,            # e_shstrndx (we don't use it but a valid index helps)
    )

    # ---- payload ----
    out[symtab_off:symtab_off + len(symtab)] = bytes(symtab)
    out[strtab_off:strtab_off + len(strtab)] = bytes(strtab)

    # ---- Section headers ----
    # Helper: write a section header at `out[base:base+40]`.
    def _write_sh(idx, sh_type, sh_offset, sh_size, sh_link=0, sh_entsize_=0):
        base = shoff + idx * sh_entsize
        struct.pack_into(
            '<IIIIIIIIII',
            out, base,
            0, sh_type, 0, 0,
            sh_offset, sh_size, sh_link, 0, 0, sh_entsize_,
        )

    _write_sh(0, 0, 0, 0)                      # SHN_UNDEF
    _write_sh(1, 2, symtab_off, len(symtab),
              sh_link=2, sh_entsize_=sym_entry_size)  # SHT_SYMTAB
    _write_sh(2, 3, strtab_off, len(strtab))   # SHT_STRTAB

    return bytes(out)


# ---------------------------------------------------------------------------
# Fake OpenOcdRpc that mimics the real flash_loader's behaviour
# ---------------------------------------------------------------------------


class FakeRpc:
    """In-memory model of the loader's protocol.

    Tracks every issued OpenOCD command in ``calls``, maintains a ``mem``
    dict of address -> 32-bit word, and auto-services ``fl_cmd`` writes by
    transitioning the loader's state machine the way the real loader does.

    Parameters let individual tests inject failures at precise points:

    * ``ping_rc`` / ``erase_rc`` / ``program_rc`` — value to return in
      ``fl_cmd_rc`` after each command class. Default 1 (FL_RC_OK).
    * ``boot_state`` — value to publish at ``fl_state``. Default 1 (ready);
      tests pass 0 to model a loader that never comes up so the boot
      timeout fires.
    * ``buf_sz`` — value to return for ``fl_cmd_data_sz``. Default 0x40.
    * ``fl_cmd_data_base`` — base address of the heap-allocated double
      buffer the loader's ``fl_cmd_data`` pointer toggles between. We
      model the upstream ``fl_rotate_databuf()`` toggle on every
      ``FL_CMD_PROGRAM_VERIFY`` write so tests catch the regression
      class "host stages chunks at the static symbol address of the
      pointer variable instead of dereferencing it".
    """

    def __init__(self, syms, *, ping_rc=1, erase_rc=1, program_rc=1,
                 boot_state=1, buf_sz=0x40, stale_bps=(),
                 fl_cmd_data_base=0x20010000):
        self.syms = syms
        self.calls = []
        self.mem = {}
        self.ping_rc = ping_rc
        self.erase_rc = erase_rc
        self.program_rc = program_rc
        self.boot_state = boot_state
        self.buf_sz = buf_sz
        # Pre-existing breakpoints — drains via ``bp_list``+``rbp``; tests
        # that exercise the defensive cleanup pass populate this.
        self.bps = list(stale_bps)
        self.bp_set_failures = set()  # addresses for which bp() raises
        self.error_after_bp_set = False
        # Pre-populate the loader-internal globals the loader code reads.
        self.mem[syms['fl_state']] = boot_state
        self.mem[syms['fl_cmd']] = 0
        self.mem[syms['fl_cmd_rc']] = 0
        self.mem[syms['fl_cmd_data_sz']] = buf_sz
        # Model the upstream loader's double buffer: ``fl_data`` is
        # malloc'd at ``fl_cmd_data_base`` and is ``2 * buf_sz`` bytes
        # long; ``fl_cmd_data`` (a *pointer variable* — see
        # ``apache/mynewt-core apps/flash_loader/src/fl.c``) starts
        # aimed at the low half and toggles on every LOAD/LOAD_VERIFY.
        # The driver must dereference it via ``mdw`` per iteration.
        self._fl_cmd_data_low = fl_cmd_data_base
        self._fl_cmd_data_high = fl_cmd_data_base + buf_sz
        self.mem[syms['fl_cmd_data']] = self._fl_cmd_data_low
        # ``mdw(LOADER_RAM_BASE)`` / ``mdw(LOADER_RAM_BASE+4)`` need
        # plausible MSP/PC values so ``_prepare_loader`` doesn't blow up
        # decoding.
        self.mem[loader.LOADER_RAM_BASE] = 0x20040000     # MSP
        self.mem[loader.LOADER_RAM_BASE + 4] = 0x20000401  # PC
        # Don't auto-set fl_state to ready until after _prepare_loader has
        # walked through reset/load_image/etc — the tests using a
        # boot_state=0 fake start "broken" and stay broken. Tests using
        # default boot_state=1 already see ready immediately.

    # ---- low-level helpers --------------------------------------------------

    def _record(self, cmd):
        self.calls.append(cmd)

    # ---- methods used by da1469x_loader ------------------------------------

    def reset(self, halt=False):
        self._record(f'reset {"halt" if halt else "run"}')

    def sleep_ms(self, ms):
        self._record(f'sleep {int(ms)}')

    def load_image(self, path, addr, fmt='bin'):
        self._record(f'load_image {path} {hex(int(addr))} {fmt}')
        # Emulate "loader app dropped into RAM, loader greets us".
        # If this is the loader's main bin (target == LOADER_RAM_BASE), make
        # sure MSP/PC stay populated.
        return ''

    def mww(self, addr, value):
        addr = int(addr)
        value = int(value) & 0xFFFFFFFF
        self._record(f'mww {hex(addr)} {hex(value)}')
        self.mem[addr] = value
        # Trigger the protocol transitions the real loader would make:
        if addr == self.syms['fl_cmd']:
            if value == loader.FL_CMD_PING:
                self.mem[self.syms['fl_cmd_rc']] = self.ping_rc
                self.mem[self.syms['fl_cmd']] = 0
            elif value == loader.FL_CMD_ERASE:
                self.mem[self.syms['fl_cmd_rc']] = self.erase_rc
                self.mem[self.syms['fl_cmd']] = 0
            elif value == loader.FL_CMD_PROGRAM_VERIFY:
                # Real protocol (per the upstream ``fl_program`` macro at
                # [openocd/flash_loader/flash_loader.gdb:104-130]): the
                # loader does NOT touch ``fl_cmd_rc`` on a successful
                # chunk. ``rc`` stays latched at ``1`` (set by the ping
                # that precedes the program loop) until either an error
                # occurs or the run completes. We model that here so the
                # tests catch the bug we hit on first hardware bring-up
                # (per-chunk ``rc=0`` clears producing false failures).
                if self.program_rc != loader.FL_RC_OK:
                    self.mem[self.syms['fl_cmd_rc']] = self.program_rc
                # Mirror ``fl_rotate_databuf`` (apps/flash_loader/src/fl.c):
                # toggle ``fl_cmd_data`` between the two halves of the
                # heap buffer *before* setting fl_cmd back to 0. The
                # driver re-reads ``fl_cmd_data`` on the next iteration
                # to find the staging area for the upcoming chunk.
                cur = self.mem[self.syms['fl_cmd_data']]
                if cur == self._fl_cmd_data_low:
                    self.mem[self.syms['fl_cmd_data']] = self._fl_cmd_data_high
                else:
                    self.mem[self.syms['fl_cmd_data']] = self._fl_cmd_data_low
                self.mem[self.syms['fl_cmd']] = 0

    def mwb(self, addr, value):
        self._record(f'mwb {hex(int(addr))} {hex(int(value) & 0xFF)}')

    def mdw(self, addr, count=1):
        addr = int(addr)
        # Always record the read so tests can assert on poll order.
        self._record(f'mdw {hex(addr)} {count}')
        if count == 1:
            return self.mem.get(addr, 0)
        return [self.mem.get(addr + 4 * i, 0) for i in range(count)]

    def reg_write(self, name, value):
        self._record(f'reg {name} {hex(int(value) & 0xFFFFFFFF)}')

    def bp(self, address, length=4, hw=True):
        self._record(f'bp {hex(int(address))} {length} {"hw" if hw else "sw"}')
        if int(address) in self.bp_set_failures:
            raise openocd.OpenOcdRpcError(
                f'OpenOCD bp failed:\nError: Breakpoint at {hex(int(address))} '
                f'already exists'
            )
        self.bps.append(int(address))

    def rbp(self, address):
        self._record(f'rbp {hex(int(address))}')
        try:
            self.bps.remove(int(address))
        except ValueError:
            pass

    def bp_list(self):
        self._record('bp')
        return list(self.bps)

    def resume(self, address=None):
        self._record('resume' if address is None else f'resume {hex(int(address))}')

    def wait_halt(self, timeout_ms=5000):
        self._record(f'wait_halt {int(timeout_ms)}')


_DEFAULT_SYM_ADDRS = {
    'fl_state': 0x20003000,
    'fl_cmd': 0x20003004,
    'fl_cmd_rc': 0x20003008,
    'fl_cmd_flash_id': 0x2000300C,
    'fl_cmd_flash_addr': 0x20003010,
    'fl_cmd_amount': 0x20003014,
    'fl_cmd_data': 0x20004000,
    'fl_cmd_data_sz': 0x20003018,
    'mynewt_main': 0x20000801,
}


def _bake_loader_dir(tmpdir, family='da1469x'):
    """Drop fixture loader artefacts under *tmpdir*/<family>/. Returns the
    paths so tests can assert on them.
    """
    family_dir = os.path.join(tmpdir, family)
    os.makedirs(family_dir, exist_ok=True)
    elf_path = os.path.join(family_dir, loader.LOADER_ELF_NAME)
    bin_path = os.path.join(family_dir, loader.LOADER_BIN_NAME)
    with open(elf_path, 'wb') as f:
        f.write(_build_elf32(_DEFAULT_SYM_ADDRS))
    with open(bin_path, 'wb') as f:
        # Vector table-ish: MSP at +0, PC at +4, then padding.
        f.write(struct.pack('<II', 0x20040000, 0x20000401) + b'\x00' * 32)
    return elf_path, bin_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class ParseElf32SymbolsTests(unittest.TestCase):
    def test_returns_expected_addresses(self):
        elf = _build_elf32({'foo': 0x12345678, 'bar': 0xDEADBEEF, 'baz': 0})
        syms = loader._parse_elf32_symbols(elf)
        self.assertEqual(syms.get('foo'), 0x12345678)
        self.assertEqual(syms.get('bar'), 0xDEADBEEF)
        # Symbols with st_name=0 are skipped (the reserved zero entry); a
        # named symbol with st_value=0 should still appear, though.
        self.assertIn('baz', syms)

    def test_rejects_non_elf(self):
        with self.assertRaises(ValueError):
            loader._parse_elf32_symbols(b'not an elf')

    def test_rejects_64bit_elf(self):
        elf = bytearray(_build_elf32({'foo': 1}))
        elf[4] = 2  # ELFCLASS64
        with self.assertRaises(ValueError):
            loader._parse_elf32_symbols(bytes(elf))


class ResolveLoaderSymbolsTests(unittest.TestCase):
    def test_resolves_required_symbols(self):
        with tempfile.TemporaryDirectory() as tmp:
            elf_path, _ = _bake_loader_dir(tmp)
            syms = loader._resolve_loader_symbols(elf_path)
        for name in loader.LOADER_SYMBOLS:
            self.assertIn(name, syms)
            self.assertEqual(syms[name], _DEFAULT_SYM_ADDRS[name])

    def test_missing_symbol_raises_actionable_error(self):
        partial = dict(_DEFAULT_SYM_ADDRS)
        partial.pop('fl_cmd_data')
        elf = _build_elf32(partial)
        with tempfile.NamedTemporaryFile(suffix='.elf', delete=False) as f:
            f.write(elf)
            elf_path = f.name
        try:
            with self.assertRaises(loader.Da1469xLoaderError) as ctx:
                loader._resolve_loader_symbols(elf_path)
            self.assertIn('fl_cmd_data', str(ctx.exception))
        finally:
            os.unlink(elf_path)


class ResolveLoaderPathsTests(unittest.TestCase):
    def test_env_override_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            elf_path, bin_path = _bake_loader_dir(tmp)
            old = os.environ.get(loader.ENV_LOADER_DIR_OVERRIDE)
            os.environ[loader.ENV_LOADER_DIR_OVERRIDE] = tmp
            try:
                got_elf, got_bin = loader._resolve_loader_paths('da1469x')
            finally:
                if old is None:
                    os.environ.pop(loader.ENV_LOADER_DIR_OVERRIDE, None)
                else:
                    os.environ[loader.ENV_LOADER_DIR_OVERRIDE] = old
            self.assertEqual(got_elf, elf_path)
            self.assertEqual(got_bin, bin_path)

    def test_missing_files_raises_actionable_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get(loader.ENV_LOADER_DIR_OVERRIDE)
            os.environ[loader.ENV_LOADER_DIR_OVERRIDE] = tmp
            try:
                with self.assertRaises(loader.Da1469xLoaderError) as ctx:
                    loader._resolve_loader_paths('da1469x')
            finally:
                if old is None:
                    os.environ.pop(loader.ENV_LOADER_DIR_OVERRIDE, None)
                else:
                    os.environ[loader.ENV_LOADER_DIR_OVERRIDE] = old
            msg = str(ctx.exception)
            self.assertIn('flash_loader.elf', msg)
            self.assertIn('lager box ssh', msg)


class XipToFlashOffsetTests(unittest.TestCase):
    """``xip_to_flash_offset`` translates the CLI's absolute XIP addresses
    (e.g. ``0x16000000``) to the flash-relative offsets the loader's
    ``fl_cmd_flash_addr`` expects. This is the missing translation that on
    real hardware caused ``lager debug SWD flash --bin xl.img,0x16000000``
    to silently mean "flash to absolute offset 0x16000000 in QSPI" — far
    past the end of any plausible flash chip.
    """

    def test_none_maps_to_zero(self):
        # No CLI ``--bin <file>,<addr>`` (hex/elf path) → start of QSPI.
        self.assertEqual(loader.xip_to_flash_offset(None), 0)

    def test_zero_maps_to_zero(self):
        # ``--bin <file>,0x0`` is the explicit "boot image at QSPI start"
        # form. We accept it and don't treat it as out-of-range.
        self.assertEqual(loader.xip_to_flash_offset(0), 0)

    def test_xip_base_maps_to_zero(self):
        # The whole point of this helper: ``--bin <file>,0x16000000`` →
        # flash offset 0, identical to ``fl_load <file> 0 0x00000000``.
        self.assertEqual(
            loader.xip_to_flash_offset(loader.QSPI_XIP_BASE), 0,
        )

    def test_xip_offset_is_subtracted(self):
        self.assertEqual(
            loader.xip_to_flash_offset(loader.QSPI_XIP_BASE + 0x4000),
            0x4000,
        )

    def test_below_xip_base_rejected_with_actionable_message(self):
        # 0x1000 (e.g. someone passes a flash offset by mistake) is
        # below the XIP window and almost always indicates user error.
        with self.assertRaises(loader.Da1469xLoaderError) as ctx:
            loader.xip_to_flash_offset(0x1000)
        msg = str(ctx.exception)
        self.assertIn('XIP', msg)
        self.assertIn(hex(loader.QSPI_XIP_BASE), msg)

    def test_above_xip_window_rejected(self):
        with self.assertRaises(loader.Da1469xLoaderError):
            loader.xip_to_flash_offset(loader.QSPI_XIP_END)
        with self.assertRaises(loader.Da1469xLoaderError):
            loader.xip_to_flash_offset(loader.QSPI_XIP_END + 0x100)


class FlashImageTests(unittest.TestCase):
    """End-to-end command-trace assertions for ``flash_image``.

    Patches the resolver/symbol seams so we can drive the loader against
    fixture artefacts without dropping them under
    ``/home/www-data/flash-loaders``.
    """

    def _run(self, fake, image_size=80, family='da1469x', **flash_kwargs):
        with tempfile.NamedTemporaryFile(suffix='.bin', delete=False) as f:
            f.write(b'\xab' * image_size)
            image_path = f.name

        def fake_resolver(_family):
            return ('/fake/elf', '/fake/bin')

        def fake_sym_resolver(_path):
            return _DEFAULT_SYM_ADDRS

        try:
            output = list(loader.flash_image(
                fake, image_path,
                family=family,
                _resolver=fake_resolver,
                _symbol_resolver=fake_sym_resolver,
                **flash_kwargs,
            ))
        finally:
            os.unlink(image_path)
        return output

    def test_prepare_then_erase_then_chunked_program(self):
        fake = FakeRpc(_DEFAULT_SYM_ADDRS, buf_sz=0x20)  # 32-byte chunks
        out = self._run(fake, image_size=80)  # 32 + 32 + 16 = 3 chunks
        # The protocol trace must contain, in order:
        # 1. POR pin debug enable, reset halt, sleep
        # 2. load_image of loader bin into RAM
        # 3. MSP/PC reads (mdw 0x20000000 / +4) + reg writes
        # 4. QSPIC + MTB pokes
        # 5. bp <mynewt_main>; resume; wait_halt; rbp; resume; mww MPU
        # 6. fl_state poll == 1
        # 7. ping (fl_cmd_rc=0; fl_cmd=1; poll fl_cmd_rc != 0)
        # 8. erase params + fl_cmd=3 + poll
        # 9. ping again at start of program; fl_cmd_data_sz read; flash_id
        #    write; per-chunk: load_image of slice; flash_addr/amount/rc;
        #    fl_cmd=5; poll fl_cmd==0; rc check; final fl_state poll
        # 10. SYS_CTRL_REG write (software reset).
        c = fake.calls

        # Reset / load / vector-table read sequence.
        self.assertEqual(c[0],
                         f'mww {hex(loader.REG_POR_PIN_DEBUG_ENABLE)} '
                         f'{hex(loader.REG_POR_PIN_DEBUG_ENABLE_VALUE)}')
        self.assertEqual(c[1], 'reset halt')
        self.assertEqual(c[2], 'sleep 1000')
        self.assertEqual(c[3],
                         f'load_image /fake/bin {hex(loader.LOADER_RAM_BASE)} bin')
        self.assertEqual(c[4], 'sleep 1000')
        self.assertEqual(c[5], f'mdw {hex(loader.LOADER_RAM_BASE)} 1')
        self.assertEqual(c[6], f'mdw {hex(loader.LOADER_RAM_BASE + 4)} 1')
        self.assertEqual(c[7], 'reg msp 0x20040000')
        self.assertEqual(c[8], 'reg pc 0x20000401')
        # QSPIC / MTB writes (4 of them, in the same order as the GDB script).
        self.assertEqual(c[9], f'mww {hex(loader.REG_QSPIC_DUMMYBYTES)} 0x0')
        self.assertEqual(c[10], f'mww {hex(loader.REG_MTB_POSITION)} 0x0')
        self.assertEqual(c[11], f'mww {hex(loader.REG_MTB_MASTER)} 0x0')
        self.assertEqual(c[12], f'mww {hex(loader.REG_MTB_FLOW)} 0x0')
        # Defensive bp list (no stale bps in this test) precedes our own
        # bp set — same address space, but the list is read-only and
        # short-circuits when empty.
        self.assertEqual(c[13], 'bp')
        # Breakpoint dance.
        self.assertEqual(c[14],
                         f'bp {hex(_DEFAULT_SYM_ADDRS["mynewt_main"])} 4 hw')
        self.assertEqual(c[15], 'resume')
        self.assertEqual(c[16], 'wait_halt 5000')
        self.assertEqual(c[17],
                         f'rbp {hex(_DEFAULT_SYM_ADDRS["mynewt_main"])}')
        self.assertEqual(c[18], 'resume')
        self.assertEqual(c[19], f'mww {hex(loader.REG_MPU_CTRL)} 0x0')

        # SYS_CTRL_REG SW reset is the very last command issued by flash_image.
        self.assertEqual(c[-1], f'mww {hex(loader.REG_SYS_CTRL_REG)} 0x1')

        # The trace must contain three program commands (one per chunk).
        program_writes = [
            cmd for cmd in c
            if cmd == f'mww {hex(_DEFAULT_SYM_ADDRS["fl_cmd"])} 0x5'
        ]
        self.assertEqual(len(program_writes), 3,
                         msg=f'expected 3 chunked programs, saw {len(program_writes)}')

        # And exactly one erase command.
        erase_writes = [
            cmd for cmd in c
            if cmd == f'mww {hex(_DEFAULT_SYM_ADDRS["fl_cmd"])} 0x3'
        ]
        self.assertEqual(len(erase_writes), 1)

        # Output must mention progress + final reset.
        joined = '\n'.join(out)
        self.assertIn('Programmed 80 bytes successfully', joined)
        self.assertIn('Issued software reset', joined)

    def test_ping_rc_failure_raises(self):
        fake = FakeRpc(_DEFAULT_SYM_ADDRS, ping_rc=99)
        with self.assertRaises(loader.Da1469xLoaderError) as ctx:
            self._run(fake, image_size=16)
        self.assertIn('ping returned rc=99', str(ctx.exception))

    def test_erase_rc_failure_raises(self):
        fake = FakeRpc(_DEFAULT_SYM_ADDRS, erase_rc=2)
        with self.assertRaises(loader.Da1469xLoaderError) as ctx:
            self._run(fake, image_size=16)
        self.assertIn('rc=2', str(ctx.exception))
        self.assertIn('erase', str(ctx.exception))

    def test_program_rc_failure_raises(self):
        # Non-OK program rc is now surfaced *after the loop*, mirroring the
        # upstream macro (which does not read rc inside the loop). The error
        # message changes from per-chunk to overall, but the rc value still
        # appears verbatim and the message still mentions "program".
        fake = FakeRpc(_DEFAULT_SYM_ADDRS, program_rc=7)
        with self.assertRaises(loader.Da1469xLoaderError) as ctx:
            self._run(fake, image_size=16)
        self.assertIn('rc=7', str(ctx.exception))
        self.assertIn('program', str(ctx.exception))

    def test_program_does_not_read_rc_mid_loop(self):
        # Regression: on real DA1469x hardware (JUL-5) the loader can leave
        # ``fl_cmd_rc`` holding a transient address-shaped value at the
        # moment ``fl_cmd`` returns to 0, e.g. ``0x66A4E0``. The upstream
        # macro side-steps that race by only reading rc once, after the
        # final ``while fl_state != 1`` poll. Lager must do the same — no
        # ``mdw fl_cmd_rc`` calls between chunks. Lock that in.
        fake = FakeRpc(_DEFAULT_SYM_ADDRS, buf_sz=0x20)
        self._run(fake, image_size=64)  # 2 chunks
        rc_addr = _DEFAULT_SYM_ADDRS['fl_cmd_rc']
        # ``mdw <addr> 1`` is the only form we use, so this matches every
        # rc read in the trace. We expect:
        #   * 1 read inside the program-loop bring-up's `_fl_ping`
        #     (poll until fl_cmd_rc != 0). That call only happens once
        #     before the loop starts, not per-chunk.
        #   * 1 final read after the loop.
        # Plus one for `_fl_ping` during `_prepare_loader`, plus one for
        # `_fl_erase`'s ping/poll, plus one for the erase poll itself —
        # all of which are *outside* the program chunk loop. The hard
        # constraint we want to lock in is "no rc reads PER CHUNK".
        # Use the rc-read count as a ceiling that doesn't grow with
        # chunk count: re-run with twice as many chunks and assert it
        # didn't double.
        rc_reads_2_chunks = sum(
            1 for cmd in fake.calls if cmd == f'mdw {hex(rc_addr)} 1'
        )

        fake4 = FakeRpc(_DEFAULT_SYM_ADDRS, buf_sz=0x20)
        self._run(fake4, image_size=128)  # 4 chunks
        rc_reads_4_chunks = sum(
            1 for cmd in fake4.calls if cmd == f'mdw {hex(rc_addr)} 1'
        )
        self.assertEqual(
            rc_reads_2_chunks, rc_reads_4_chunks,
            msg=(
                f'fl_cmd_rc reads scaled with chunk count '
                f'(2-chunk: {rc_reads_2_chunks}, 4-chunk: {rc_reads_4_chunks}). '
                'A per-chunk rc read regressed; this races against the '
                "loader's transient use of fl_cmd_rc and produces "
                'address-shaped false failures on hardware.'
            ),
        )

    def test_program_does_not_clear_rc_per_chunk(self):
        # Regression: the upstream ``fl_program`` macro at
        # [openocd/flash_loader/flash_loader.gdb:104-130] never clears
        # ``fl_cmd_rc`` inside the chunk loop — it relies on rc being
        # latched at ``1`` by the preceding ping. Our first port DID
        # clear it before each chunk, which on real hardware produced
        # ``flash_loader program chunk @0x0 (+32768 bytes) returned
        # rc=0`` on the very first chunk because the loader doesn't
        # re-assert rc on each successful write. Lock the cleaner-up
        # behaviour in.
        fake = FakeRpc(_DEFAULT_SYM_ADDRS, buf_sz=0x20)
        self._run(fake, image_size=64)  # 2 chunks
        rc_addr = _DEFAULT_SYM_ADDRS['fl_cmd_rc']
        rc_clears_to_zero = [
            cmd for cmd in fake.calls
            if cmd == f'mww {hex(rc_addr)} 0x0'
        ]
        # The only legitimate rc-clears are inside ``_fl_ping`` (called
        # twice: once during _prepare_loader, once at the start of the
        # program loop) and inside ``_fl_erase`` (called once before
        # the program loop). That's three. Anything more means we're
        # clobbering rc per-chunk again.
        self.assertEqual(
            len(rc_clears_to_zero), 3,
            msg=(
                'Expected exactly 3 fl_cmd_rc=0 writes (2x ping + 1x erase); '
                f'saw {len(rc_clears_to_zero)}. If this jumped up, the '
                'per-chunk clear regressed and the loader will return '
                'rc=0 on the first program chunk again.'
            ),
        )

    def test_program_load_image_uses_dereferenced_fl_cmd_data(self):
        # Regression for the JUL-5 hang at chunk@0x10000: ``fl_cmd_data``
        # is a *pointer variable* in the upstream loader
        # (``apache/mynewt-core apps/flash_loader/src/fl.c``) that the
        # loader's ``fl_rotate_databuf()`` toggles between two halves of
        # the malloc'd ``fl_data`` buffer. The driver must dereference
        # it (``mdw``) per chunk; using the static ELF symbol address
        # writes chunks on top of the loader's own BSS, which on
        # hardware bus-faults the Cortex-M33 once the chunk's first 4
        # bytes form an unmappable pointer.
        buf_sz = 0x20
        base = 0x20020000  # distinct from any sym address
        fake = FakeRpc(
            _DEFAULT_SYM_ADDRS, buf_sz=buf_sz, fl_cmd_data_base=base,
        )
        self._run(fake, image_size=4 * buf_sz)  # 4 chunks
        load_dests = [
            int(cmd.split()[2], 16)
            for cmd in fake.calls
            if cmd.startswith('load_image ') and 'fake/bin' not in cmd
        ]
        self.assertEqual(
            len(load_dests), 4,
            msg=f'expected 4 chunk load_images, saw {len(load_dests)}: '
                f'{load_dests}',
        )
        # The destinations must alternate between the two halves of the
        # double buffer — never landing on the static symbol address.
        self.assertEqual(
            load_dests, [base, base + buf_sz, base, base + buf_sz],
            msg=(
                f'load_image destinations did not follow fl_cmd_data '
                f'rotation. Got {[hex(d) for d in load_dests]}; '
                f'expected alternation between {hex(base)} and '
                f'{hex(base + buf_sz)}. If any destination equals '
                f'{hex(_DEFAULT_SYM_ADDRS["fl_cmd_data"])} the '
                f'pointer-deref regressed.'
            ),
        )
        # And explicitly: never the static symbol address.
        self.assertNotIn(
            _DEFAULT_SYM_ADDRS['fl_cmd_data'], load_dests,
            msg='load_image was called at the fl_cmd_data symbol address; '
                'the driver is treating the pointer var as if it were a '
                'buffer. This is the JUL-5 chunk@0x10000 hang regression.',
        )

    def test_program_rereads_fl_cmd_data_each_chunk(self):
        # Sentinel test: if the driver caches fl_cmd_data once (instead
        # of re-reading per chunk), changing the pointer mid-flash will
        # not affect later chunk destinations. A correct driver re-reads
        # so the new value takes effect on the very next chunk.
        buf_sz = 0x20
        base = 0x20030000
        sentinel = 0xDEADBEE0  # obviously distinct, word-aligned

        class TogglingFake(FakeRpc):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self._chunks_seen = 0
                self._sentinel_after = 0  # after chunk 0, swap in sentinel

            def mww(self, addr, value):
                super().mww(addr, value)
                if (int(addr) == self.syms['fl_cmd']
                        and int(value) == loader.FL_CMD_PROGRAM_VERIFY):
                    if self._chunks_seen == self._sentinel_after:
                        # Override the (just-toggled) pointer with the
                        # sentinel so the next iteration's ``mdw`` would
                        # observe it iff the driver re-reads.
                        self.mem[self.syms['fl_cmd_data']] = sentinel
                    self._chunks_seen += 1

        fake = TogglingFake(
            _DEFAULT_SYM_ADDRS, buf_sz=buf_sz, fl_cmd_data_base=base,
        )
        self._run(fake, image_size=3 * buf_sz)  # 3 chunks
        load_dests = [
            int(cmd.split()[2], 16)
            for cmd in fake.calls
            if cmd.startswith('load_image ') and 'fake/bin' not in cmd
        ]
        self.assertEqual(len(load_dests), 3)
        # Chunk 0 uses the initial low half; after chunk 0 the fake
        # patches the pointer to the sentinel, so chunk 1 must observe
        # it. (Chunk 2 then re-toggles via the fake's normal rotate.)
        self.assertEqual(load_dests[0], base)
        self.assertEqual(
            load_dests[1], sentinel,
            msg=(
                f'chunk 1 load_image destination was {hex(load_dests[1])}; '
                f'expected sentinel {hex(sentinel)}. The driver appears to '
                f'have cached fl_cmd_data instead of re-reading it per '
                f'chunk — the same bug class that produced the JUL-5 hang.'
            ),
        )

    def test_stale_breakpoints_are_cleared_before_bp_set(self):
        # Regression: if a previous bring-up timed out at ``wait_halt``,
        # OpenOCD's internal bp table still holds the stale entry. Without
        # the defensive sweep, the next ``rpc.bp(mynewt_main, ...)`` would
        # fail with "Breakpoint at 0x... already exists". With the sweep,
        # we ``rbp`` every listed address before setting our own.
        stale = [_DEFAULT_SYM_ADDRS['mynewt_main'], 0x20009999]
        fake = FakeRpc(_DEFAULT_SYM_ADDRS, buf_sz=0x40, stale_bps=stale)
        out = self._run(fake, image_size=16)
        c = fake.calls
        # The ``bp`` listing must come before each rbp, and all rbps must
        # come before our own ``bp <mynewt_main> 4 hw`` set.
        list_idx = c.index('bp')
        rbp_main_defensive = c.index(
            f'rbp {hex(_DEFAULT_SYM_ADDRS["mynewt_main"])}', list_idx,
        )
        rbp_other = c.index('rbp 0x20009999', list_idx)
        bp_set_idx = c.index(
            f'bp {hex(_DEFAULT_SYM_ADDRS["mynewt_main"])} 4 hw',
        )
        self.assertLess(list_idx, rbp_main_defensive)
        self.assertLess(list_idx, rbp_other)
        self.assertLess(rbp_main_defensive, bp_set_idx)
        self.assertLess(rbp_other, bp_set_idx)
        # Output mentions the cleanup so the operator can see what happened.
        self.assertTrue(
            any('Clearing stale breakpoint' in line for line in out),
            msg=f'expected stale-clear progress in output, got: {out}',
        )

    def test_bp_list_failure_does_not_block_flash(self):
        # If OpenOCD's bp listing itself errors (very unusual — e.g. target
        # not yet examined), we log + skip the defensive clear and proceed.
        # The flash must still complete because losing the cleanup pass is
        # only painful when there *is* a stale bp.
        class FakeRpcFlakyBpList(FakeRpc):
            def bp_list(self):
                self._record('bp')
                raise openocd.OpenOcdRpcError('OpenOCD bp list failed:\n'
                                              'Error: target not examined yet')

        fake = FakeRpcFlakyBpList(_DEFAULT_SYM_ADDRS, buf_sz=0x40)
        out = self._run(fake, image_size=16)
        self.assertIn(
            f'bp {hex(_DEFAULT_SYM_ADDRS["mynewt_main"])} 4 hw',
            fake.calls,
            msg='our own bp set must still happen when bp_list errors',
        )
        self.assertIn('Programmed 16 bytes successfully', '\n'.join(out))

    def test_loader_boot_timeout_raises(self):
        # boot_state=0 -> fl_state never reaches READY.
        fake = FakeRpc(_DEFAULT_SYM_ADDRS, boot_state=0)
        # Shrink the boot timeout so the test is fast — patch via attr swap.
        orig = loader._LOADER_BOOT_TIMEOUT_S
        loader._LOADER_BOOT_TIMEOUT_S = 0.01
        try:
            with self.assertRaises(loader.Da1469xLoaderError) as ctx:
                self._run(fake, image_size=16)
        finally:
            loader._LOADER_BOOT_TIMEOUT_S = orig
        self.assertIn('boot', str(ctx.exception))


class EraseRangeTests(unittest.TestCase):
    def _run(self, fake, **kwargs):
        def fake_resolver(_family):
            return ('/fake/elf', '/fake/bin')

        def fake_sym_resolver(_path):
            return _DEFAULT_SYM_ADDRS

        return list(loader.erase_range(
            fake,
            _resolver=fake_resolver,
            _symbol_resolver=fake_sym_resolver,
            **kwargs,
        ))

    def test_erase_range_emits_one_erase_command(self):
        fake = FakeRpc(_DEFAULT_SYM_ADDRS)
        out = self._run(fake, length=0x1000)
        c = fake.calls
        # Bring-up sequence is the same — last commands should be:
        # ping (set rc=0; fl_cmd=1; poll), then erase (set params; fl_cmd=3;
        # poll). No software reset (erase doesn't reboot the chip).
        erases = [cmd for cmd in c
                  if cmd == f'mww {hex(_DEFAULT_SYM_ADDRS["fl_cmd"])} 0x3']
        self.assertEqual(len(erases), 1)
        self.assertNotIn(f'mww {hex(loader.REG_SYS_CTRL_REG)} 0x1', c)
        # The erase parameters use the requested length / offset.
        self.assertIn(
            f'mww {hex(_DEFAULT_SYM_ADDRS["fl_cmd_amount"])} {hex(0x1000)}', c,
        )
        self.assertIn('Erased 4096 bytes successfully', '\n'.join(out))

    def test_zero_length_rejected(self):
        fake = FakeRpc(_DEFAULT_SYM_ADDRS)
        with self.assertRaises(loader.Da1469xLoaderError):
            self._run(fake, length=0)


if __name__ == '__main__':
    unittest.main()
