# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for cli/core/net_helpers.py echo_box_request_failure.

v0.32.0 hardware-testing finding: a slow box-side operation (first contact
with an Acroname hub takes >10s of BrainStem discovery) raised
requests.ReadTimeout, which the funnels reported as "cannot reach box ...
Check network/Tailscale" — the wrong diagnosis, since the box had accepted
the request. Read timeouts and genuine connection failures now get distinct
messages. requests.ConnectTimeout subclasses both ConnectionError and
Timeout and IS a can't-reach condition, so it must keep the unreachable
message.
"""

from __future__ import annotations

import pytest
import requests

from cli.core.net_helpers import echo_box_request_failure


BOX = "10.0.0.5"


def _stderr(capsys):
    return capsys.readouterr().err


def test_read_timeout_reports_slow_operation(capsys):
    echo_box_request_failure(BOX, requests.ReadTimeout("boom"), timeout=30)
    err = _stderr(capsys)
    assert "accepted the request" in err
    assert "within 30s" in err
    assert "cannot reach box" not in err


def test_connection_error_reports_unreachable(capsys):
    echo_box_request_failure(BOX, requests.ConnectionError("refused"))
    err = _stderr(capsys)
    assert "cannot reach box" in err
    assert "Tailscale" in err


def test_connect_timeout_is_still_unreachable(capsys):
    # ConnectTimeout subclasses Timeout too — but the box never answered,
    # so the network diagnosis is the right one.
    echo_box_request_failure(BOX, requests.ConnectTimeout("no route"), timeout=10)
    err = _stderr(capsys)
    assert "cannot reach box" in err
    assert "accepted the request" not in err


def test_read_timeout_without_budget_omits_duration(capsys):
    echo_box_request_failure(BOX, requests.ReadTimeout("boom"))
    err = _stderr(capsys)
    assert "accepted the request" in err
    assert "within" not in err
