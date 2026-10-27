# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""A DP800 reports OVP/OCP maxima above its rated output.

The instrument accepts OVP and OCP up to 110% of a channel's rating and ships
with them there. `lager supply` bounds OVP/OCP by these maxima, so a lowered
limit can be put back to the factory value (#732).
"""

import pytest

from lager.power.supply.rigol_dp800 import RigolDP800


def _dp800(model):
    supply = RigolDP800.__new__(RigolDP800)
    supply.get_identification = lambda: f"RIGOL TECHNOLOGIES,{model},DP8TEST,00.01.14"
    return supply


@pytest.mark.parametrize("model, channel, rated, protection", [
    ("DP821A", 1, (60.0, 1.0), (66.0, 1.1)),
    ("DP821A", 2, (8.0, 10.0), (8.8, 11.0)),
    ("DP832A", 3, (5.0, 3.0), (5.5, 3.3)),
])
def test_protection_maxima_are_110_percent_of_the_rating(model, channel, rated, protection):
    limits = _dp800(model).get_channel_limits(channel)
    assert (limits["voltage_max"], limits["current_max"]) == rated
    assert (limits["ovp_max"], limits["ocp_max"]) == protection
