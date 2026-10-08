# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Code derived from an upstream project keeps that project's notice.

Two parts of the tree are derived from MIT-licensed code: the CLI's HDLC
framing (`cli/simple_hdlc.py`, from simple-hdlc) and the box's BluFi client
(`box/lager/blufi/`, from EspBlufiForAndroid). Both licenses require their
copyright and permission notice in every copy. The source files name the
upstream in their header, and the license text lives in `NOTICE`. The wheel
ships `cli/NOTICE`, and the box image ships a copy of the root `NOTICE`
(pinned byte-identical by test_box_image_notices.py).

The BluFi files are found by globbing, so a new file in that package fails
here until it carries the header too.
"""
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
NOTICE = REPO / "NOTICE"
CLI_NOTICE = REPO / "cli" / "NOTICE"
BANNER = "=" * 80

HDLC_FILES = [REPO / "cli" / "simple_hdlc.py"]
BLUFI_FILES = sorted((REPO / "box" / "lager" / "blufi").rglob("*.py"))


def _entry(notice_text, name):
    """The NOTICE entry for ``name``: its banner through the next banner."""
    match = re.search(
        rf"^{BANNER}\n{re.escape(name)}\n{BANNER}\n(.*?)(?=^{BANNER}\n|\Z)",
        notice_text, re.S | re.M,
    )
    assert match, f"no NOTICE entry for {name}"
    # The separating blank line before the next banner is not part of it.
    return match.group(0).rstrip("\n")


def _header(path):
    lines = []
    for line in path.read_text().splitlines():
        if not line.startswith("#"):
            break
        lines.append(line)
    return "\n".join(lines)


@pytest.mark.parametrize("path", HDLC_FILES, ids=lambda p: str(p.relative_to(REPO)))
def test_simple_hdlc_derived_file_names_its_upstream(path):
    header = _header(path)
    assert "Copyright (c) 2016 wuttem" in header
    assert "SPDX-License-Identifier: Apache-2.0 AND MIT" in header
    assert "https://github.com/wuttem/simple-hdlc" in header


def test_the_blufi_package_is_found():
    # An empty glob would pass every parametrized case below by having none.
    assert len(BLUFI_FILES) >= 10


@pytest.mark.parametrize("path", BLUFI_FILES, ids=lambda p: str(p.relative_to(REPO)))
def test_every_blufi_file_names_its_upstream(path):
    header = _header(path)
    assert "Copyright 2019 ESPRESSIF SYSTEMS (SHANGHAI) PTE LTD" in header
    assert "SPDX-License-Identifier: Apache-2.0 AND LicenseRef-ESPRESSIF-MIT" in header
    assert "ESP8266/ESP32" in header


@pytest.mark.parametrize("name, phrases", [
    ("simple-hdlc", [
        "Copyright (c) 2016 wuttem",
        "Permission is hereby granted, free of charge",
        "The above copyright notice and this permission notice shall be included",
    ]),
    ("EspBlufiForAndroid", [
        "Copyright © 2019 <ESPRESSIF SYSTEMS (SHANGHAI) PTE LTD>",
        "ESPRESSIF SYSTEMS ESP8266/ESP32 only",
        "The above copyright notice and this permission notice shall be included",
    ]),
])
def test_notice_carries_the_full_upstream_license(name, phrases):
    entry = _entry(NOTICE.read_text(), name)
    for phrase in phrases:
        assert phrase in entry, (name, phrase)


def test_the_wheel_notice_carries_the_same_simple_hdlc_entry():
    # The wheel ships only the CLI, so cli/NOTICE names only what the CLI
    # derives from. Its entry must not drift from the root's.
    assert _entry(CLI_NOTICE.read_text(), "simple-hdlc") == \
        _entry(NOTICE.read_text(), "simple-hdlc")


def test_the_sdist_includes_the_wheel_notice():
    lines = (REPO / "cli" / "MANIFEST.in").read_text().splitlines()
    assert "include NOTICE" in lines
