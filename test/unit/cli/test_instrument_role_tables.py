# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""The three instrument role tables must agree.

An instrument's roles are written down three times:

* ``SUPPORTED_USB[name]["net_type"]``  -- box-side, what roles exist
* ``CHANNEL_MAPS[name]``               -- box-side, which channels per role
* ``INSTRUMENT_NET_MAP[name]``         -- CLI-side, what `lager nets add` allows

`nets.py` already calls this duplication out in a comment ("Roles mirror
box/lager/devices/catalog.py -- same catalog-data duplication tech debt as the
scanner's SUPPORTED_USB tables"). The failure it produces is not a crash: the
Rigol MSO5204 advertised a `logic` channel in `lager instruments` while
`lager nets add ... logic ...` refused it, so `lager logic` could not be used on
the one instrument in the fleet that does logic capture. Nothing detected that
for as long as it was true.

These are cheap invariants -- they held for every other instrument in the tree
when this test was written.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCANNER = ROOT / 'box' / 'lager' / 'http_handlers' / 'usb_scanner.py'
NETS = ROOT / 'cli' / 'commands' / 'box' / 'nets.py'


def _literal_table(path: Path, name: str):
    """Read a module-level table without importing the module.

    The box package is not importable from the CLI unit environment, and these
    are pure data, so parse rather than import.
    """
    for node in ast.walk(ast.parse(path.read_text())):
        target = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target = node.target.id
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            target = node.targets[0].id
        if target == name:
            return ast.literal_eval(node.value)
    raise AssertionError(f'{name} not found in {path}')


SUPPORTED_USB = _literal_table(SCANNER, 'SUPPORTED_USB')
CHANNEL_MAPS = _literal_table(SCANNER, 'CHANNEL_MAPS')
INSTRUMENT_NET_MAP = _literal_table(NETS, 'INSTRUMENT_NET_MAP')

# The alias helper is real code, not data, so exercise the function rather than
# a re-implementation of it. cli.commands.box.nets IS importable here (only the
# box package is not); sibling tests import it the same way.
_nets_mod = importlib.import_module('cli.commands.box.nets')
canonical_instrument = _nets_mod.canonical_instrument
_INSTRUMENT_ALIASES = _nets_mod._INSTRUMENT_ALIASES


def test_the_tables_were_actually_parsed():
    """Guard the guard: empty tables would make every check below vacuous."""
    assert len(SUPPORTED_USB) > 40
    assert len(CHANNEL_MAPS) > 30
    assert len(INSTRUMENT_NET_MAP) > 40
    # and that intersecting them did not leave the parametrized checks empty
    assert len(CHARTED_AND_USB) > 30
    assert len(CLI_MAPPED_AND_USB) > 40


# Filtered at collection rather than skipped inside the test. A skipped test
# makes `tools/check_coverage_counts.py` treat the whole `unit (cli)` suite as
# platform-gated and stop maintaining its count -- so a data-driven skip here
# would quietly disable a gate for every other suite in the file.
CHARTED_AND_USB = sorted(set(CHANNEL_MAPS) & set(SUPPORTED_USB))
CLI_MAPPED_AND_USB = sorted(set(INSTRUMENT_NET_MAP) & set(SUPPORTED_USB))


@pytest.mark.parametrize('instrument', CHARTED_AND_USB)
def test_channel_map_roles_match_the_role_list(instrument):
    """A channel advertised for a role the instrument does not claim is
    unreachable: `lager instruments` shows it, `lager nets add` refuses it."""
    declared = set(SUPPORTED_USB[instrument].get('net_type', []))
    charted = set(CHANNEL_MAPS[instrument])
    assert charted == declared, (
        f'{instrument}: CHANNEL_MAPS has {sorted(charted)} but SUPPORTED_USB '
        f'declares {sorted(declared)}'
    )


@pytest.mark.parametrize('instrument', CLI_MAPPED_AND_USB)
def test_cli_role_map_matches_the_box(instrument):
    """`lager nets add` is the only gate on role -- the box stores what it is
    given -- so a CLI map narrower than the box's is a silent capability loss."""
    assert set(INSTRUMENT_NET_MAP[instrument]) == set(SUPPORTED_USB[instrument]['net_type']), (
        f'{instrument}: CLI allows {sorted(set(INSTRUMENT_NET_MAP[instrument]))}, '
        f'box declares {sorted(set(SUPPORTED_USB[instrument]["net_type"]))}'
    )


def test_the_logic_capable_scope_accepts_a_logic_net():
    """The specific regression: #261's subcommands need a logic net to exist."""
    assert 'logic' in INSTRUMENT_NET_MAP['Rigol_MSO5204']
    assert 'logic' in SUPPORTED_USB['Rigol_MSO5204']['net_type']
    assert 'logic' in CHANNEL_MAPS['Rigol_MSO5204']


def test_the_legacy_scope_spelling_still_resolves():
    """The key was spelled with a zero for the O until #373.

    Every net saved before the rename persists the old string, and the lookups
    that consume it are exact-key -- a miss silently drops the restriction the
    key carried rather than failing. The alias is what keeps those records
    working without a fleet-wide migration of saved_nets.json.
    """
    assert canonical_instrument('Rigol_MS05204') == 'Rigol_MSO5204'
    assert 'logic' in INSTRUMENT_NET_MAP[canonical_instrument('Rigol_MS05204')]

    # The typo must be gone from the tables themselves, or the rename was
    # only half-applied and the scanner would still emit it.
    for table in (SUPPORTED_USB, CHANNEL_MAPS, INSTRUMENT_NET_MAP):
        assert 'Rigol_MS05204' not in table


def test_aliases_point_at_keys_that_exist():
    """An alias to a key that was never added is a silent no-op."""
    for legacy, canonical in _INSTRUMENT_ALIASES.items():
        assert canonical in SUPPORTED_USB, canonical
        assert canonical != legacy
