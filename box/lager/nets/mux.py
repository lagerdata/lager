# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

import requests
import json
import os
from typing import Dict, Optional, Set, Tuple
from .constants import HARDWARE_HOST, HARDWARE_PORT

class InvalidNetNameError(Exception):
    pass

class InvalidScopePointForNetNameError(Exception):
    pass


class MuxIndexer:
    """Indexed mux lookup for O(1) access.

    Builds a dictionary index from the mux config to enable fast lookups
    instead of triple nested loop iteration.
    """

    def __init__(self):
        # Index: (net_name, role) -> (mapping_pin, scope_points_set)
        self._net_index: Dict[Tuple[str, str], Tuple[str, Set[str]]] = {}
        # Index for role=None lookups: net_name -> [(role, mapping_pin, scope_points_set), ...]
        self._net_only_index: Dict[str, list] = {}
        self._built = False
        self._muxes_hash: Optional[int] = None

    def build_index(self, muxes: list):
        """Build O(1) lookup indexes from mux config."""
        # Check if we need to rebuild
        muxes_hash = hash(json.dumps(muxes, sort_keys=True))
        if self._built and self._muxes_hash == muxes_hash:
            return  # Already built with same data

        self._net_index.clear()
        self._net_only_index.clear()

        for mux in muxes:
            role = mux.get('role', '')
            # Build set of scope point names for O(1) membership check
            scope_points_set = {name for pin, name in mux.get('scope_points', [])}

            for mapping in mux.get('mappings', []):
                net = mapping.get('net')
                mapping_pin = mapping.get('pin')

                if net:
                    # Index by (net, role) for exact lookup
                    key = (net, role)
                    self._net_index[key] = (mapping_pin, scope_points_set)

                    # Also index by net only for role=None lookups
                    if net not in self._net_only_index:
                        self._net_only_index[net] = []
                    self._net_only_index[net].append((role, mapping_pin, scope_points_set))

        self._built = True
        self._muxes_hash = muxes_hash

    def lookup(self, net_name: str, scope_point: str, role: str = None) -> Optional[str]:
        """O(1) lookup to find the pin for a given net and scope point.

        Args:
            net_name: The net name to look up
            scope_point: The scope point name that must exist in the mux
            role: Optional role filter. If None, searches all roles.

        Returns:
            The mapping pin if found, None otherwise.

        Raises:
            InvalidScopePointForNetNameError: If net is found but scope_point doesn't match.
        """
        if role is not None:
            # Exact role lookup - O(1)
            result = self._net_index.get((net_name, role))
            if result:
                mapping_pin, scope_points_set = result
                if scope_point in scope_points_set:
                    return mapping_pin
                else:
                    raise InvalidScopePointForNetNameError()
            return None
        else:
            # Role=None: check all entries for this net, find first match
            entries = self._net_only_index.get(net_name, [])
            for entry_role, mapping_pin, scope_points_set in entries:
                if scope_point in scope_points_set:
                    return mapping_pin
            # Net was found but scope_point didn't match any mux
            if entries:
                raise InvalidScopePointForNetNameError()
            return None


# Module-level singleton indexer
_mux_indexer = MuxIndexer()


class Mux:
    def __init__(self, scope_point):
        self.scope_point = scope_point

    def clear(self):
        data = [
            {
                'current_state': [{
                    'scope': self.scope_point,
                    'box': None,
                }],
            },
        ]
        resp = requests.post(f'http://{HARDWARE_HOST}:{HARDWARE_PORT}/mux', json=data)
        resp.raise_for_status()



    def connect(self, net, *, role=None):
        scope_point = self.scope_point
        net_name = net.name
        muxes = json.loads(os.getenv('LAGER_MUXES', '[]'))

        # Build index (only rebuilds if muxes changed)
        _mux_indexer.build_index(muxes)

        # O(1) lookup instead of triple nested loop
        pin = _mux_indexer.lookup(net_name, scope_point, role)

        if pin is None:
            raise InvalidNetNameError()

        data = [
            {
                'current_state': [{
                    'scope': scope_point,
                    'box': pin,
                }],
            },
        ]
        resp = requests.post(f'http://{HARDWARE_HOST}:{HARDWARE_PORT}/mux', json=data)
        resp.raise_for_status()
