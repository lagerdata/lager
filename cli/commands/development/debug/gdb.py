# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
    lager.debug.gdb

    Debug an elf file
"""
import pathlib
import os
import atexit
import math
import hashlib
from io import BytesIO
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED
import click
from ....context import get_default_box
from ....elftools.elf.elffile import ELFFile
from ....elftools.common.exceptions import ELFError
from .... import status as status_module


class PathResolutionError(Exception):
    pass


def zip_files(root, filenames, ignore_missing=False, max_content_size=10_000_000):
    """
        Zip a set of files
    """
    archive = BytesIO()
    total_size = 0

    with ZipFile(archive, 'w') as zip_archive:
        for filename in filenames:
            try:
                total_size += os.path.getsize(filename)

                try:
                    resolved = filename.resolve().relative_to(root)
                except ValueError as exc:
                    raise PathResolutionError from exc
                fileinfo = ZipInfo(str(resolved))
                with open(filename, 'rb') as f:
                    zip_archive.writestr(fileinfo, f.read(), ZIP_DEFLATED)
            except FileNotFoundError:
                if not ignore_missing:
                    raise

    return archive.getbuffer()

def get_comp_dir(die):
    comp_dir = die.attributes.get('DW_AT_comp_dir', None)
    if comp_dir:
        return pathlib.Path(os.fsdecode(comp_dir.value))
    return None

def line_entry_mapping(top_die, line_program):
    """
    The line program, when decoded, returns a list of line program
    entries. Each entry contains a state, which we'll use to build
    a reverse mapping of filename -> #entries.
    """

    filenames = set()

    lp_entries = line_program.get_entries()
    comp_dir = get_comp_dir(top_die)

    for lpe in lp_entries:
        # We skip LPEs that don't have an associated file.
        # This can happen if instructions in the compiled binary
        # don't correspond directly to any original source file.
        if not lpe.state or lpe.state.file == 0:
            continue
        filename = lpe_filename(filenames, comp_dir, line_program, lpe.state.file)
        if filename is not None:
            filenames.add(filename)

    return filenames

def lpe_filename(filenames, comp_dir, line_program, file_index):
    """
    Retrieving the filename associated with a line program entry
    involves two levels of indirection: we take the file index from
    the LPE to grab the file_entry from the line program header,
    then take the directory index from the file_entry to grab the
    directory name from the line program header. Finally, we
    join the (base) filename from the file_entry to the directory
    name to get the absolute filename.
    """
    lp_header = line_program.header
    file_entries = lp_header["file_entry"]

    # File and directory indices are 1-indexed.
    file_entry = file_entries[file_index - 1]
    dir_index = file_entry["dir_index"]

    # A dir_index of 0 indicates that no absolute directory was recorded during
    # compilation; return just the basename.
    if dir_index == 0:
        basepath = pathlib.Path(os.fsdecode(file_entry.name))
    else:
        directory = pathlib.Path(os.fsdecode(lp_header["include_directory"][dir_index - 1]))
        basepath = directory / os.fsdecode(file_entry.name)

    if comp_dir:
        full_candidate = comp_dir / basepath
        if full_candidate in filenames:
            return None
        if full_candidate.exists():
            return full_candidate

    return basepath

def collect_filenames(ctx, elf_file, verbose):

    try:
        elffile = ELFFile(open(elf_file, 'rb'))
    except ELFError:
        click.echo(f'Error: \'{elf_file}\' is not an ELF file', err=True)
        ctx.exit(1)

    if not elffile.has_dwarf_info():
        click.echo(f'Error: \'{elf_file}\' does not have debug info', err=True)
        ctx.exit(1)

    filenames = set()

    dwarfinfo = elffile.get_dwarf_info()
    top_die = None
    for cu in dwarfinfo.iter_CUs():
        # Every compilation unit in the DWARF information may or may not
        # have a corresponding line program in .debug_line.
        line_program = dwarfinfo.line_program_for_CU(cu)
        if line_program is None:
            continue

        top_die = cu.get_top_DIE()

        # Print a reverse mapping of filename -> #entries
        filenames = filenames | line_entry_mapping(top_die, line_program)

    if not top_die:
        click.echo('No compilation units found', err=True)
        ctx.exit(1)

    comp_dir = get_comp_dir(top_die)
    if verbose:
        click.echo(f'Using comp dir: {comp_dir}', err=True)
    root = pathlib.Path.cwd()
    if comp_dir:
        if root.parent / comp_dir == root:
            root = root.parent

    if verbose:
        click.echo(f'Using root dir: {root}', err=True)

    gdb_init = root / '.gdbinit'
    if gdb_init.exists():
        if verbose:
            click.echo(f'Adding gdb init: {gdb_init}', err=True)
        filenames.add(gdb_init)
    else:
        if verbose:
            click.echo('No gdbinit found', err=True)

    elf_candidate = root / elf_file
    if not elf_candidate.exists():
        elf_candidate = root / comp_dir / elf_file

    return root, elf_candidate, filenames

SOURCE_LINK = '/tmp/lager_gdb_source'

def remove_source_link():
    try:
        os.remove(SOURCE_LINK)
    except FileNotFoundError:
        pass

def sha256sum(filename):
    h  = hashlib.sha256()
    b  = bytearray(128*1024)
    mv = memoryview(b)
    with open(filename, 'rb', buffering=0) as f:
        for n in iter(lambda: f.readinto(mv), 0):
            h.update(mv[:n])
    return h.hexdigest()


def debug(ctx, box, interpreter, verbose, tty, quiet, args, ignore_missing, cwd, cache, mcu, elf_only, debugfile, elf_file):
    """
        Debug a device using an ELF file
    """
    _ = tty
    _ = args

    if cwd is not None:
        os.chdir(cwd)

    cwd = pathlib.Path.cwd()
    elf_file = cwd / elf_file
    if not elf_file.exists():
        raise click.UsageError(
            f"Invalid value for 'ELF_FILE': Path '{elf_file}' does not exist.",
            ctx=ctx,
        )

    if ctx.obj.interpreter is not None:
        interpreter = ctx.obj.interpreter
    if interpreter and interpreter.startswith('='):
        interpreter = interpreter[1:]

    if interpreter not in ('default', 'mi'):
        raise click.UsageError(
            f"Interpreter '{interpreter}' not recognized",
            ctx=ctx,
        )
    remove_source_link()
    session = ctx.obj.session
    if box is None:
        box = get_default_box(ctx)

    root, elf_path, filenames = collect_filenames(ctx, elf_file, verbose)
    if verbose:
        for filename in filenames:
            click.echo(filename, err=True)

    if elf_only:
        filenames = set()

    if elf_path.exists():
        filenames.add(elf_path)
    else:
        click.echo('Could not find ELF file', err=True)
        ctx.exit(1)

    while True:
        if root == pathlib.Path(root.root):
            raise ValueError('Could not build archive, components may exist on different partitions')
        try:
            archive = zip_files(root, filenames, ignore_missing)
            break
        except PathResolutionError:
            root = root.parent

    if verbose:
        click.echo(f'Archive size: {len(archive)}', err=True)
    os.symlink(root, SOURCE_LINK, target_is_directory=True)
    atexit.register(remove_source_link)

    relative_cwd = pathlib.Path.cwd().relative_to(root)
    elf_file = str(elf_path.relative_to(root).relative_to(relative_cwd))

    elf_hash = sha256sum(elf_file)

    args = {
        'elf_file': elf_file,
        'interpreter': interpreter,
        'quiet': quiet,
        'cwd_parts': relative_cwd.parts,
        'elf_hash': elf_hash,
        'elf_only': elf_only,
    }
    if debugfile:
        with open(debugfile, 'rb') as f:
            args['debugfile'] = f.read().decode()
    resp = session.remote_debug(box, cache, archive, args)

    test_run = resp.json()
    job_id = test_run['test_run']['id']
    region = test_run['test_run']['gateway']['region_name']  # API returns 'gateway' key

    connection_params = ctx.obj.websocket_connection_params(socktype='job', job_id=job_id, region=region)
    test_runner = 'none'
    interactive = 'cooked'
    ptty = False
    line_ending = 'LF'
    message_timeout = math.inf
    overall_timeout = math.inf
    eof_timeout = 1

    status_module.run_job_output(connection_params, test_runner, interactive, line_ending, message_timeout, overall_timeout,
        eof_timeout, ctx.obj.debug, opost=False, serial_channel=None, ptty=ptty, catch_sigint=True, disconnect=box)
