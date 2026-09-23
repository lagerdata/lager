# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""DUT-context HTTP handler for the Lager Box HTTP server.

``GET|PUT /dut`` reads and replaces the ``dut_slots`` block of
``/etc/lager/bench.json``: what the box tests, the DUT's MCU and peripherals,
its subsystems, and the references to schematics and datasheets. Until now
that block could be written only over SSH by ``lager dut``; this route lets a
control plane that offers a DUT editor push what an engineer authored there.

The write replaces the whole list rather than merging fields. Both sides edit
the block as a unit (``$EDITOR`` on the box, a form in the control plane), and
per-field clocks like the ones on ``/nets/<name>/metadata`` would carry a cost
nothing consumes. One clock, ``dut_updated_at``, is stored beside the list so
the two sides can tell which copy is newer before either overwrites the other
(``GET /bench`` reports the later of that key and the file's mtime).

Every slot is validated the way the bench loader would read it, so a payload
the loader would silently skip is refused here with a 400 that names the
problem. The other keys of ``bench.json`` (``net_overrides``, ``constraints``,
``interfaces``...) are left exactly as they were.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, jsonify, request

# Reused rather than reimplemented: stages through `<path>.tmp` and
# os.replace, so a crashed write never leaves a half-written bench.json.
from ..nets.net import _atomic_write_json

logger = logging.getLogger(__name__)

BENCH_JSON_PATH = "/etc/lager/bench.json"


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )


def _read_bench() -> Tuple[Dict[str, Any], Optional[str]]:
    """The raw bench.json object, or ``({}, error)`` when it cannot be used.

    A missing file is an empty bench, not an error: the first PUT creates it.
    A file that is not a JSON object is an error, because replacing it would
    destroy whatever an operator put there.
    """
    try:
        with open(BENCH_JSON_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}, None
    except (OSError, json.JSONDecodeError) as e:
        return {}, 'bench.json cannot be read: %s' % e
    if not isinstance(data, dict):
        return {}, 'bench.json is not a JSON object'
    return data, None


def _current_slots(bench: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The DUT slots as a list, whichever of the two spellings the file uses."""
    slots = bench.get('dut_slots')
    if isinstance(slots, list):
        return [s for s in slots if isinstance(s, dict)]
    short = bench.get('dut_context')
    if isinstance(short, dict):
        return [short]
    return []


def validate_slots(payload: Any) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    """``(slots, None)`` for a well-formed payload, else ``(None, error)``.

    Runs each slot through the bench loader's own constructor so the file
    written here is one the loader will read whole, not one it will skip
    entries from with a warning nobody sees.
    """
    if not isinstance(payload, dict):
        return None, 'Body must be a JSON object'
    slots = payload.get('dut_slots')
    if not isinstance(slots, list):
        return None, 'dut_slots must be an array'
    updated_at = payload.get('updated_at')
    if updated_at is not None and (not isinstance(updated_at, str) or not updated_at.strip()):
        return None, 'updated_at must be a non-empty ISO 8601 string or absent'

    from ..mcp.engine.bench_loader import _dut_context_from_raw

    names = set()
    for index, slot in enumerate(slots):
        if not isinstance(slot, dict):
            return None, 'dut_slots[%d] must be an object' % index
        try:
            built = _dut_context_from_raw(slot)
        except (TypeError, ValueError) as e:
            return None, 'dut_slots[%d]: %s' % (index, e)
        if built.name in names:
            return None, "dut_slots[%d]: duplicate DUT name '%s'" % (index, built.name)
        names.add(built.name)
    return slots, None


def register_dut_routes(app: Flask) -> None:
    """Register the DUT-context REST routes with the Flask app."""

    @app.route('/dut', methods=['GET'])
    def get_dut():
        """The DUT slots and the clock of their last change."""
        bench, error = _read_bench()
        if error:
            return jsonify({'error': error}), 500
        return jsonify({
            'dut_slots': _current_slots(bench),
            'dut_updated_at': bench.get('dut_updated_at') or None,
        })

    @app.route('/dut', methods=['PUT'])
    def put_dut():
        """Replace the DUT slots, leaving every other key of bench.json alone."""
        payload = request.get_json(force=True, silent=True)
        slots, error = validate_slots(payload)
        if error or slots is None or not isinstance(payload, dict):
            return jsonify({'error': error or 'Body must be a JSON object'}), 400

        bench, read_error = _read_bench()
        if read_error:
            return jsonify({'error': read_error}), 500

        updated_at = str(payload.get('updated_at') or '').strip() or _now_iso()
        bench['dut_slots'] = slots
        # The single-DUT short form would shadow the list on the next load.
        bench.pop('dut_context', None)
        bench['dut_updated_at'] = updated_at
        try:
            _atomic_write_json(BENCH_JSON_PATH, bench)
        except OSError as e:
            logger.error("bench.json write failed: %s", e)
            return jsonify({'error': 'bench.json write failed: %s' % e}), 500

        logger.info("DUT context replaced: %d slot(s), updated_at %s", len(slots), updated_at)
        return jsonify({
            'ok': True,
            'dut_slots': slots,
            'dut_updated_at': updated_at,
        })
