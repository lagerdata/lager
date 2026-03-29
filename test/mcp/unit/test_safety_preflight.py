# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the safety preflight engine."""

import pytest

from lager.mcp.safety import preflight_check, reset_rate_limits
from lager.mcp.schemas.safety_types import RateLimit, SafetyConstraints


@pytest.fixture(autouse=True)
def _reset():
    reset_rate_limits()
    yield
    reset_rate_limits()


class TestVoltageLimits:
    def test_within_limit(self):
        sc = SafetyConstraints(max_voltage={"psu1": 5.0})
        result = preflight_check(tool_name="set_voltage", params={"voltage": 3.3}, constraints=sc, target_net="psu1")
        assert result.allowed is True

    def test_exceeds_limit(self):
        sc = SafetyConstraints(max_voltage={"psu1": 5.0})
        result = preflight_check(tool_name="set_voltage", params={"voltage": 6.0}, constraints=sc, target_net="psu1")
        assert result.allowed is False
        assert "exceeds" in result.blocked_reason

    def test_near_limit_warning(self):
        sc = SafetyConstraints(max_voltage={"psu1": 5.0})
        result = preflight_check(tool_name="set_voltage", params={"voltage": 4.8}, constraints=sc, target_net="psu1")
        assert result.allowed is True
        assert len(result.warnings) > 0

    def test_no_limit_for_net(self):
        sc = SafetyConstraints(max_voltage={"psu1": 5.0})
        result = preflight_check(tool_name="set_voltage", params={"voltage": 100}, constraints=sc, target_net="psu2")
        assert result.allowed is True


class TestCurrentLimits:
    def test_exceeds_current(self):
        sc = SafetyConstraints(max_current={"psu1": 1.0})
        result = preflight_check(tool_name="set_current", params={"current": 2.0}, constraints=sc, target_net="psu1")
        assert result.allowed is False
        assert "current" in result.blocked_reason.lower()

    def test_current_limit_param(self):
        sc = SafetyConstraints(max_current={"psu1": 1.0})
        result = preflight_check(tool_name="x", params={"current_limit": 1.5}, constraints=sc, target_net="psu1")
        assert result.allowed is False


class TestDangerousActions:
    def test_blocked_without_destructive_mode(self):
        sc = SafetyConstraints(dangerous_actions=["flash_firmware"])
        result = preflight_check(tool_name="flash_firmware", params={}, constraints=sc)
        assert result.allowed is False
        assert "dangerous" in result.blocked_reason.lower()

    def test_allowed_with_destructive_mode(self):
        sc = SafetyConstraints(dangerous_actions=["flash_firmware"], destructive_mode=True)
        result = preflight_check(tool_name="flash_firmware", params={}, constraints=sc)
        assert result.allowed is True


class TestRateLimits:
    def test_rate_limit_not_exceeded(self):
        sc = SafetyConstraints(rate_limits={"read_logs": RateLimit(max_calls=5, window_seconds=60)})
        for _ in range(5):
            result = preflight_check(tool_name="read_logs", params={}, constraints=sc)
            assert result.allowed is True

    def test_rate_limit_exceeded(self):
        sc = SafetyConstraints(rate_limits={"read_logs": RateLimit(max_calls=3, window_seconds=60)})
        for _ in range(3):
            preflight_check(tool_name="read_logs", params={}, constraints=sc)
        result = preflight_check(tool_name="read_logs", params={}, constraints=sc)
        assert result.allowed is False
        assert "rate limit" in result.blocked_reason.lower()


class TestNoConstraints:
    def test_none_constraints(self):
        result = preflight_check(tool_name="anything", params={"voltage": 999}, constraints=None)
        assert result.allowed is True
