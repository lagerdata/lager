# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the agent-facing test-pattern catalog.

Each pattern names the net types its script needs, and an agent turns those
names straight into ``NetType.<name>`` in generated code. A name that is not
a real enum member therefore does not degrade gracefully -- the script the
agent writes raises ``AttributeError`` before it touches any hardware.

Regression this guards: the ``oscilloscope`` pattern claimed ``"Scope"``,
which has never been a ``NetType`` member. A scope channel is
``NetType.Analog``. Nothing checked the catalog against the enum, so the
wrong name sat there advertising an API that could not be called.
"""

import pytest

from lager.mcp.data.test_patterns import TEST_PATTERNS
from lager.nets.constants import NetType

# Patterns whose net type genuinely has no NetType member yet.
#
# This is not a style exemption -- it is a real gap in the catalog, kept
# explicit so it stays visible instead of being papered over by mapping the
# name onto some unrelated enum member that happens to exist.
_NO_NET_TYPE_YET = {
    # There is no BLE role or NetType anywhere in the box. Deciding what a
    # BLE net should be is a product question, not a rename.
    "ble_scan": {"BLE"},
}

_VALID_NET_TYPES = {member.name for member in NetType}


@pytest.mark.parametrize("pattern_key", sorted(TEST_PATTERNS))
def test_pattern_net_types_are_real_net_types(pattern_key):
    """Every net type a pattern names must be constructible as NetType.<name>."""
    pattern = TEST_PATTERNS[pattern_key]
    named = set(pattern.get("net_types", []))
    assert named, "%s names no net types at all" % pattern_key

    unknown = named - _VALID_NET_TYPES - _NO_NET_TYPE_YET.get(pattern_key, set())
    assert not unknown, (
        "pattern %r names %s, which is not a NetType member. An agent will "
        "write NetType.%s and get an AttributeError. Valid names: %s"
        % (pattern_key, sorted(unknown), sorted(unknown)[0],
           ", ".join(sorted(_VALID_NET_TYPES)))
    )


def test_the_scope_pattern_points_at_the_analog_net_type():
    """A scope channel is NetType.Analog, for both Rigol and PicoScope."""
    assert TEST_PATTERNS["oscilloscope"]["net_types"] == ["Analog"]


def test_the_known_gaps_are_still_gaps():
    """Drop an entry from _NO_NET_TYPE_YET once its NetType exists.

    Without this, the exemption list silently outlives the gap it documents
    and starts hiding a name that has since become checkable.
    """
    for pattern_key, names in _NO_NET_TYPE_YET.items():
        resolved = names & _VALID_NET_TYPES
        assert not resolved, (
            "%s is exempted for %s, but those are now real NetType members. "
            "Remove the exemption." % (pattern_key, sorted(resolved))
        )
