# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""CSV parsing/writing for custom battery model curves.

A model CSV is two columns — open-circuit voltage (V), internal
resistance (Ω) — with an optional header row, and exactly 11 or 101 data
rows (11-point files are interpolated to 101 points on the instrument).
Index 0 is the empty battery, so VOC must be non-decreasing and resistance
non-increasing down the file.

Kept free of click/CLI imports so the validation is unit-testable and every
mistake it can catch fails on the laptop, before anything reaches the box.
The driver on the box (KeithleyBattery.define_model) enforces the same rules
for callers that bypass the CLI.
"""
from __future__ import annotations

import csv
import math

# Keithley 2281S model geometry: a complete model is 101 points per element;
# 11 points are also accepted and interpolated to 101 by the instrument.
MODEL_ROWS = (11, 101)
VOC_MAX = 60.0        # V, matches the driver's set_voc bound
RESISTANCE_MAX = 100.0  # Ω, matches the driver's ESR-offset scale


def parse_model_csv(path: str) -> tuple[list[float], list[float]]:
    """Parse and validate a battery model CSV.

    Returns:
        (voc, resistance) as equal-length float lists of 11 or 101 points.

    Raises:
        ValueError: With a line-numbered, user-facing message on the first
            problem found.
    """
    rows = []  # (line_number, voc, resistance)
    header_skipped = False
    with open(path, newline="") as f:
        for line_number, row in enumerate(csv.reader(f), start=1):
            cells = [cell.strip() for cell in row]
            if not any(cells):
                continue  # blank line
            if len(cells) != 2:
                raise ValueError(
                    f"{path}, line {line_number}: expected 2 columns "
                    f"(voc,resistance), got {len(cells)}.")
            try:
                voc, res = float(cells[0]), float(cells[1])
            except ValueError:
                if not rows and not header_skipped:
                    header_skipped = True
                    continue  # optional header row
                raise ValueError(
                    f"{path}, line {line_number}: could not parse "
                    f"'{cells[0]},{cells[1]}' as two numbers.") from None
            for name, value, limit, unit in (
                    ("voc", voc, VOC_MAX, "V"),
                    ("resistance", res, RESISTANCE_MAX, "Ω")):
                if not math.isfinite(value):
                    raise ValueError(
                        f"{path}, line {line_number}: {name} is not a finite "
                        f"number.")
                if value <= 0 or value > limit:
                    raise ValueError(
                        f"{path}, line {line_number}: {name} {value:g} {unit} "
                        f"is out of range (must be > 0 and <= {limit:g} {unit}).")
            if rows:
                _, prev_voc, prev_res = rows[-1]
                prev_line = rows[-1][0]
                if voc < prev_voc:
                    raise ValueError(
                        f"{path}, line {line_number}: voc {voc:g} V is below "
                        f"line {prev_line}'s {prev_voc:g} V — voc must be "
                        f"non-decreasing (row 1 is the empty battery).")
                if res > prev_res:
                    raise ValueError(
                        f"{path}, line {line_number}: resistance {res:g} Ω is "
                        f"above line {prev_line}'s {prev_res:g} Ω — resistance "
                        f"must be non-increasing.")
            rows.append((line_number, voc, res))

    if len(rows) not in MODEL_ROWS:
        raise ValueError(
            f"{path}: expected exactly 11 or 101 data rows "
            f"(11-point files are interpolated to 101 on the instrument), "
            f"got {len(rows)}.")
    return [r[1] for r in rows], [r[2] for r in rows]


def write_model_csv(path: str, points: list[dict]) -> None:
    """Write exported model points ([{"voc": v, "resistance": r}, ...]) as CSV
    in the format parse_model_csv accepts, for the export→edit→create loop."""
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["voc", "resistance"])
        for point in points:
            writer.writerow([f"{point['voc']:g}", f"{point['resistance']:g}"])
