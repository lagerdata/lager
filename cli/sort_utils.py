# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
    lager.sort_utils

    Natural sorting utilities for human-friendly ordering.
    Makes "BOX7" sort before "BOX10" instead of after.
"""
import re


def natural_sort_key(text):
    """
    Convert a string into a list of mixed strings and integers for natural sorting.
    This makes "BOX7" come before "BOX10" instead of alphabetical order.

    Examples:
        "BOX1"  -> ["BOX", 1]
        "BOX10" -> ["BOX", 10]
        "adc2"  -> ["adc", 2]
    """
    def atoi(s):
        return int(s) if s.isdigit() else s.lower()
    return [atoi(c) for c in re.split(r'(\d+)', text)]
