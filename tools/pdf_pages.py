#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Extract text or render images for a specific page range of a PDF.

This runs on the agent/developer machine, never on the Lager box — the box
only stores document references, it never processes file bytes. Use it to
render just the relevant pages of a schematic/datasheet (as advertised by a
DUT doc ref's `pages` field) so render quality and file naming stay
consistent.

Usage:
    python tools/pdf_pages.py <pdf_path> --pages <range> [--mode text|png]
                              [--dpi N] [--out DIR]

    --pages   1-indexed; a single page (5), a range (5-8), or a comma list
              (5,7,9). Ranges and lists may be combined (e.g. 1,4-6,9).
    --mode    png (default) renders each page to page_NNN.png in --out;
              text extracts and prints each page's text.
    --dpi     render resolution for png mode (default: 216).
    --out     output directory for png mode (default: current directory).

Examples:
    python tools/pdf_pages.py board.pdf --pages 5-8
    python tools/pdf_pages.py board.pdf --pages 5-8 --mode text
    python tools/pdf_pages.py board.pdf --pages 3,5,7 --dpi 300 --out renders

Dependencies: pymupdf (`pip install pymupdf`).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import fitz  # pymupdf


def parse_pages(spec: str) -> list[int]:
    """Parse a 1-indexed page spec ("5", "5-8", "5,7,9", "1,4-6,9").

    Returns a sorted, de-duplicated list of 1-indexed page numbers.
    """
    pages: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo_str, _, hi_str = part.partition("-")
            lo, hi = int(lo_str), int(hi_str)
            if lo > hi:
                raise ValueError(f"invalid range (start > end): {part!r}")
            pages.update(range(lo, hi + 1))
        else:
            pages.add(int(part))
    if any(p < 1 for p in pages):
        raise ValueError("page numbers must be >= 1")
    return sorted(pages)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract text or render images for a PDF page range.",
    )
    parser.add_argument("pdf", type=Path, help="Path to the source PDF")
    parser.add_argument(
        "--pages",
        required=True,
        help="1-indexed pages: single (5), range (5-8), or list (5,7,9)",
    )
    parser.add_argument(
        "--mode",
        choices=("text", "png"),
        default="png",
        help="png (render images, default) or text (extract page text)",
    )
    parser.add_argument(
        "--dpi", type=int, default=216, help="Render resolution for png mode (default: 216)"
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("."),
        help="Output directory for png mode (default: current directory)",
    )
    args = parser.parse_args()

    if not args.pdf.is_file():
        print(f"error: file not found: {args.pdf}", file=sys.stderr)
        return 1

    try:
        wanted = parse_pages(args.pages)
    except ValueError as exc:
        print(f"error: bad --pages value: {exc}", file=sys.stderr)
        return 1
    if not wanted:
        print("error: --pages selected no pages", file=sys.stderr)
        return 1

    doc = fitz.open(args.pdf)
    try:
        out_of_range = [p for p in wanted if p > doc.page_count]
        if out_of_range:
            print(
                f"error: pages {out_of_range} out of range "
                f"(PDF has {doc.page_count} page(s))",
                file=sys.stderr,
            )
            return 1

        if args.mode == "text":
            for p in wanted:
                print(f"--- page {p} ---")
                print(doc[p - 1].get_text())
            print(f"\nextracted text from {len(wanted)} page(s)", file=sys.stderr)
            return 0

        args.out.mkdir(parents=True, exist_ok=True)
        mat = fitz.Matrix(args.dpi / 72, args.dpi / 72)
        for p in wanted:
            out_path = args.out / f"page_{p:03d}.png"
            doc[p - 1].get_pixmap(matrix=mat).save(out_path)
            print(f"wrote {out_path}")
        return 0
    finally:
        doc.close()


if __name__ == "__main__":
    raise SystemExit(main())
