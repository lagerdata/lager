# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the tools/pdf_pages.py helper.

Builds a tiny in-memory PDF with fitz, then exercises both png and text
modes plus the out-of-range guard. Skipped if pymupdf isn't installed.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

fitz = pytest.importorskip("fitz")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "tools" / "pdf_pages.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("pdf_pages", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pdf_pages = _load_module()


def _make_pdf(path: Path, n_pages: int = 8) -> None:
    doc = fitz.open()
    for i in range(n_pages):
        page = doc.new_page()
        page.insert_text((72, 72), f"Page {i + 1} body text")
    doc.save(path)
    doc.close()


def test_parse_pages_variants():
    assert pdf_pages.parse_pages("5") == [5]
    assert pdf_pages.parse_pages("5-8") == [5, 6, 7, 8]
    assert pdf_pages.parse_pages("5,7,9") == [5, 7, 9]
    assert pdf_pages.parse_pages("1,4-6,9") == [1, 4, 5, 6, 9]
    with pytest.raises(ValueError):
        pdf_pages.parse_pages("8-5")


def test_png_mode_writes_files(tmp_path, monkeypatch):
    pdf = tmp_path / "sample.pdf"
    _make_pdf(pdf)
    out = tmp_path / "renders"

    monkeypatch.setattr(
        sys, "argv", ["pdf_pages.py", str(pdf), "--pages", "5-8", "--out", str(out)]
    )
    assert pdf_pages.main() == 0
    for p in range(5, 9):
        assert (out / f"page_{p:03d}.png").is_file()


def test_text_mode_prints_headers(tmp_path, monkeypatch, capsys):
    pdf = tmp_path / "sample.pdf"
    _make_pdf(pdf)

    monkeypatch.setattr(
        sys, "argv", ["pdf_pages.py", str(pdf), "--pages", "5-8", "--mode", "text"]
    )
    assert pdf_pages.main() == 0
    out = capsys.readouterr().out
    for p in range(5, 9):
        assert f"--- page {p} ---" in out


def test_out_of_range_errors(tmp_path, monkeypatch):
    pdf = tmp_path / "sample.pdf"
    _make_pdf(pdf, n_pages=3)

    monkeypatch.setattr(
        sys, "argv", ["pdf_pages.py", str(pdf), "--pages", "5-8", "--out", str(tmp_path)]
    )
    assert pdf_pages.main() == 1
