# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
`lager box-config` pip / udev / volume / restart -- the subcommands that had no
test.

`test_box_config_cli.py` covers apply, mount, apt, sysctl, env, cargo and npm.
These four groups use the same shim protocol and had nothing: what reaches the
box-side `box_config.py` shim, and what the operator is told when the shim
refuses. The shim is replaced at `_run_box_config_py`, so no SSH, HTTP or
/etc/lager is touched.
"""

import json
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from cli.commands.box import config as box_config_cli
from cli.commands.box import _shim_verbs as verbs


class Shim:
    """Records each shim call and answers with a canned JSON payload."""

    def __init__(self, **answers):
        self.answers = answers
        self.calls = []

    def __call__(self, ctx, box, *args, **kwargs):
        verb = args[0]
        self.calls.append((verb, args[1:]))
        if verb not in self.answers:
            raise AssertionError(f"unexpected shim verb {verb!r}")
        return json.dumps(self.answers[verb])


def _run(shim, argv, input=None):
    with patch.object(box_config_cli, "_resolve_box", return_value="192.0.2.10"), \
            patch.object(box_config_cli, "_run_box_config_py", side_effect=shim):
        return CliRunner().invoke(box_config_cli.box_config, argv, input=input)


# ---------------------------------------------------------------------------
# pip
# ---------------------------------------------------------------------------

def test_pip_add_sends_the_packages_as_json(monkeypatch):
    shim = Shim(**{verbs.PIP_ADD: {"ok": True, "added": ["requests==2.31.0", "pyyaml"]}})
    result = _run(shim, ["pip", "add", "requests==2.31.0", "pyyaml", "--no-validate-pypi"])
    assert result.exit_code == 0, result.output
    (verb, args), = shim.calls
    assert verb == verbs.PIP_ADD
    assert json.loads(args[0]) == {"packages": ["requests==2.31.0", "pyyaml"]}
    assert "Added 2 pip package(s) on 192.0.2.10" in result.output


def test_pip_add_stops_on_a_package_pypi_does_not_have():
    shim = Shim()  # nothing registered: reaching the shim fails the test
    with patch.object(box_config_cli, "validate_on_pypi",
                      return_value=([("nopkg-xyz", "not found on PyPI")], [])):
        result = _run(shim, ["pip", "add", "nopkg-xyz"])
    assert result.exit_code == 1
    assert "nopkg-xyz: not found on PyPI" in result.output
    assert "No changes made" in result.output
    assert shim.calls == []


def test_pip_add_proceeds_past_a_network_error():
    shim = Shim(**{verbs.PIP_ADD: {"ok": True, "added": ["requests"]}})
    with patch.object(box_config_cli, "validate_on_pypi",
                      return_value=([], [("requests", "network error: offline")])):
        result = _run(shim, ["pip", "add", "requests"])
    assert result.exit_code == 0, result.output
    assert "did not validate (network)" in result.output
    assert [c[0] for c in shim.calls] == [verbs.PIP_ADD]


def test_pip_add_skips_the_pypi_check_for_a_direct_reference():
    shim = Shim(**{verbs.PIP_ADD: {"ok": True}})
    with patch.object(box_config_cli, "validate_on_pypi",
                      side_effect=AssertionError("checked a direct reference")):
        result = _run(shim, ["pip", "add", "pkg @ https://example.com/pkg.whl"])
    assert result.exit_code == 0, result.output
    assert "Skipped PyPI check for 1 direct reference(s)" in result.output


def test_pip_add_shim_refusal_is_reported_and_non_zero():
    shim = Shim(**{verbs.PIP_ADD: {"ok": False, "errors": ["bad spec: '; rm -rf'"]}})
    result = _run(shim, ["pip", "add", "x", "--no-validate-pypi"])
    assert result.exit_code == 1
    assert "Failed to add pip packages" in result.output
    assert "bad spec" in result.output


def test_pip_remove_passes_each_package_as_an_argument():
    shim = Shim(**{verbs.PIP_REMOVE: {"ok": True, "removed": ["pyyaml"]}})
    result = _run(shim, ["pip", "remove", "pyyaml", "requests"])
    assert result.exit_code == 0, result.output
    assert shim.calls == [(verbs.PIP_REMOVE, ("pyyaml", "requests"))]
    assert "Removed 1 pip package(s): pyyaml" in result.output


def test_pip_remove_nothing_matched():
    shim = Shim(**{verbs.PIP_REMOVE: {"ok": True, "removed": []}})
    result = _run(shim, ["pip", "remove", "absent"])
    assert result.exit_code == 0
    assert "No matching pip packages were configured." in result.output


def test_pip_import_legacy_reports_imported_and_skipped():
    shim = Shim(**{verbs.PIP_IMPORT_LEGACY: {
        "ok": True, "imported": ["numpy"],
        "skipped": [{"package": "-e ./local", "reason": "editable installs are not supported"}],
    }})
    result = _run(shim, ["pip", "import-legacy"])
    assert result.exit_code == 0, result.output
    assert shim.calls == [(verbs.PIP_IMPORT_LEGACY, ())]
    assert "Imported 1 package(s): numpy" in result.output
    assert "'-e ./local': editable installs are not supported" in result.output


def test_pip_import_legacy_failure_is_non_zero():
    shim = Shim(**{verbs.PIP_IMPORT_LEGACY: {"ok": False, "error": "unreadable file"}})
    result = _run(shim, ["pip", "import-legacy"])
    assert result.exit_code == 1
    assert "unreadable file" in result.output


def test_pip_list_reads_the_config():
    shim = Shim(**{verbs.SHOW: {"pip_packages": ["requests==2.31.0"]}})
    result = _run(shim, ["pip", "list", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == ["requests==2.31.0"]


# ---------------------------------------------------------------------------
# udev
# ---------------------------------------------------------------------------

def test_udev_add_normalizes_and_sends_each_rule():
    shim = Shim(**{verbs.UDEV_ADD: {"ok": True}})
    result = _run(shim, ["udev", "add", "1209:0001", "0x0483:DF11", "--usbtmc", "--mode", "0666"])
    assert result.exit_code == 0, result.output
    (verb, args), = shim.calls
    assert verb == verbs.UDEV_ADD
    assert json.loads(args[0]) == {"rules": [
        {"vid": "1209", "pid": "0001", "mode": "0666", "usbtmc": True},
        {"vid": "0483", "pid": "df11", "mode": "0666", "usbtmc": True},
    ]}
    assert "Added 2 udev rule(s)" in result.output


def test_udev_add_default_mode():
    shim = Shim(**{verbs.UDEV_ADD: {"ok": True}})
    _run(shim, ["udev", "add", "1209:0001"])
    rule = json.loads(shim.calls[0][1][0])["rules"][0]
    assert rule["mode"] == "0660" and rule["usbtmc"] is False


@pytest.mark.parametrize("token", ["12090001", "1209:0001:2"])
def test_udev_add_refuses_a_malformed_id_before_the_box(token):
    shim = Shim()
    result = _run(shim, ["udev", "add", token])
    assert result.exit_code == 1
    assert "expected VID:PID" in result.output
    assert shim.calls == []


def test_udev_remove_sends_normalized_tokens():
    shim = Shim(**{verbs.UDEV_REMOVE: {"ok": True, "removed": ["0483:df11"]}})
    result = _run(shim, ["udev", "remove", "0x0483:DF11"])
    assert result.exit_code == 0, result.output
    assert shim.calls == [(verbs.UDEV_REMOVE, ("0483:df11",))]
    assert "Removed 1 udev rule(s): 0483:df11" in result.output


def test_udev_remove_refusal_is_non_zero():
    shim = Shim(**{verbs.UDEV_REMOVE: {"ok": False, "errors": ["not configured"]}})
    result = _run(shim, ["udev", "remove", "1209:0001"])
    assert result.exit_code == 1
    assert "Failed to remove udev rules" in result.output


def test_udev_list_renders_rules():
    shim = Shim(**{verbs.SHOW: {"udev_rules": [
        {"vid": "1209", "pid": "0001", "mode": "0660", "usbtmc": True}]}})
    result = _run(shim, ["udev", "list"])
    assert result.exit_code == 0, result.output
    assert "1209:0001 mode=0660 (usbtmc)" in result.output


def test_udev_list_empty():
    shim = Shim(**{verbs.SHOW: {}})
    result = _run(shim, ["udev", "list"])
    assert result.exit_code == 0
    assert "No udev rules configured." in result.output


# ---------------------------------------------------------------------------
# volume
# ---------------------------------------------------------------------------

def test_volume_add_payload():
    shim = Shim(**{verbs.VOLUME_ADD: {"ok": True}})
    result = _run(shim, ["volume", "add", "fw-cache", "/cache"])
    assert result.exit_code == 0, result.output
    (verb, args), = shim.calls
    assert verb == verbs.VOLUME_ADD
    assert json.loads(args[0]) == {"name": "fw-cache", "container": "/cache"}
    assert "Added volume fw-cache -> /cache" in result.output


def test_volume_add_refusal_is_non_zero():
    shim = Shim(**{verbs.VOLUME_ADD: {"ok": False, "errors": ["container path must be absolute"]}})
    result = _run(shim, ["volume", "add", "fw-cache", "cache"])
    assert result.exit_code == 1
    assert "container path must be absolute" in result.output


def test_volume_remove_needs_confirmation():
    shim = Shim()
    result = _run(shim, ["volume", "remove", "fw-cache"], input="n\n")
    assert "Aborted." in result.output
    assert shim.calls == []


@pytest.mark.parametrize("removed, message", [(True, "Removed volume fw-cache"),
                                              (False, "No volume named fw-cache")])
def test_volume_remove(removed, message):
    shim = Shim(**{verbs.VOLUME_REMOVE: {"ok": True, "removed": removed}})
    result = _run(shim, ["volume", "remove", "fw-cache", "--yes"])
    assert result.exit_code == 0, result.output
    assert shim.calls == [(verbs.VOLUME_REMOVE, ("fw-cache",))]
    assert message in result.output


def test_volume_list_renders_entries():
    shim = Shim(**{verbs.SHOW: {"volumes": [{"name": "fw-cache", "container": "/cache"}]}})
    result = _run(shim, ["volume", "list"])
    assert result.exit_code == 0, result.output
    assert "fw-cache -> /cache" in result.output


# ---------------------------------------------------------------------------
# restart
# ---------------------------------------------------------------------------

def _restart(bounce_rc, api_up=True, argv=("restart", "--yes"), input=None):
    with patch.object(box_config_cli, "_resolve_box", return_value="192.0.2.10"), \
            patch.object(box_config_cli, "_bounce_container_rc", return_value=bounce_rc) as bounce, \
            patch.object(box_config_cli, "_wait_for_box_api", return_value=api_up):
        result = CliRunner().invoke(box_config_cli.box_config, list(argv), input=input)
    return result, bounce


def test_restart_success():
    result, bounce = _restart(box_config_cli._BOUNCE_OK)
    assert result.exit_code == 0, result.output
    assert bounce.call_count == 1
    assert "Restarted the lager container on 192.0.2.10" in result.output


def test_restart_declined_does_not_bounce():
    result, bounce = _restart(box_config_cli._BOUNCE_OK, argv=("restart",), input="n\n")
    assert bounce.call_count == 0
    assert "Aborted." in result.output


def test_restart_that_came_up_without_the_config_is_a_failure():
    result, _ = _restart(box_config_cli._BOUNCE_CONFIG_NOT_APPLIED)
    assert result.exit_code == 1
    assert "box_config.json was not applied" in result.output


def test_restart_bounce_failure():
    result, _ = _restart(1)
    assert result.exit_code == 1
    assert "Container restart failed" in result.output


def test_restart_api_never_came_up():
    result, _ = _restart(box_config_cli._BOUNCE_OK, api_up=False)
    assert result.exit_code == 1
    assert "box API didn't come up" in result.output
