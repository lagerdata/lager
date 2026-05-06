# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Host-side helpers for `lager box config pip add`:
  - format check (delegates to box-side validate_pip_format so behavior stays
    in sync; see box/lager/box_config/config.py)
  - PyPI existence check (best-effort, network-error tolerant)
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Optional


_PYPI_TIMEOUT = 10
_USER_AGENT = "lager-cli"

_PIP_SPEC_RE = re.compile(
    r'^[a-zA-Z][a-zA-Z0-9\-_\.]*'
    r'(\[[a-zA-Z0-9\-_,\s]+\])?'
    r'(([<>=!~]=?|@)[a-zA-Z0-9\.\-_,\s\*<>=!~@]+)?$'
)


def validate_format(pkg: str) -> tuple[bool, Optional[str]]:
    """Mirror of box-side validate_pip_format. Kept in sync intentionally."""
    if not isinstance(pkg, str) or not pkg.strip():
        return False, "package name cannot be empty"
    if pkg[0] in '0123456789.-_':
        return False, "package name must start with a letter"
    if not _PIP_SPEC_RE.match(pkg):
        return False, "invalid package specification format"
    return True, None


def normalize_for_pypi(pkg: str) -> str:
    """Strip extras + version specs to get the bare distribution name."""
    name = pkg.split("@", 1)[0]
    name = name.split("[", 1)[0]
    for sep in ("==", ">=", "<=", "!=", "~=", ">", "<"):
        if sep in name:
            name = name.split(sep, 1)[0]
            break
    return name.strip()


def is_direct_ref(pkg: str) -> bool:
    """PEP 508 direct reference (`pkg @ url`). Skip PyPI lookup for these."""
    return "@" in pkg


def validate_on_pypi(packages: list[str]) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Check that each package exists on PyPI.

    Returns (invalid, network_errors) — both lists of (pkg, reason) tuples.
    Direct references (PEP 508 `@ url` form) are skipped.
    """
    invalid: list[tuple[str, str]] = []
    network_errors: list[tuple[str, str]] = []

    for pkg in packages:
        if is_direct_ref(pkg):
            continue
        name = normalize_for_pypi(pkg)
        if not name:
            invalid.append((pkg, "could not extract package name"))
            continue
        url = f"https://pypi.org/pypi/{name}/json"
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=_PYPI_TIMEOUT) as resp:
                if resp.status != 200:
                    invalid.append((pkg, f"HTTP {resp.status}"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                invalid.append((pkg, "not found on PyPI"))
            elif e.code == 403:
                invalid.append((pkg, "access forbidden"))
            else:
                invalid.append((pkg, f"HTTP error {e.code}"))
        except urllib.error.URLError as e:
            network_errors.append((pkg, f"network error: {e.reason}"))
        except TimeoutError:
            network_errors.append((pkg, "connection timed out"))
        except Exception as e:  # noqa: BLE001
            network_errors.append((pkg, str(e)))

    return invalid, network_errors
