#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the `lager devenv mount` and `lager devenv env` subgroups
(cli/commands/development/devenv.py), which edit the project-local `.lager`
`volumes` / `environment` keys so users don't hand-edit JSON.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from unittest.mock import patch

from click.testing import CliRunner

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

devenv_mod = importlib.import_module('cli.commands.development.devenv')
from cli.commands.development.devenv import devenv as devenv_group  # noqa: E402


def _write_cfg(tmp_path, devenv_config):
    path = tmp_path / '.lager'
    path.write_text(json.dumps({'DEVENV': devenv_config}))
    return str(path)


def _invoke(path, args):
    """Run a devenv subcommand, re-reading the file fresh each get_devenv_json."""
    def fresh(*_a, **_k):
        with open(path) as f:
            return path, json.load(f)

    with patch.object(devenv_mod, 'get_devenv_json', side_effect=fresh):
        return CliRunner().invoke(devenv_group, args)


def _read(path):
    with open(path) as f:
        return json.load(f)['DEVENV']


BASE_CFG = {'image': 'example/img', 'mount_dir': '/app', 'shell': '/bin/bash'}


def test_mount_add_appends(tmp_path):
    path = _write_cfg(tmp_path, dict(BASE_CFG))
    result = _invoke(path, ['mount', 'add', 'cursor-data:/root/.cursor'])
    assert result.exit_code == 0, result.output
    assert _read(path)['volumes'] == ['cursor-data:/root/.cursor']


def test_mount_add_is_idempotent(tmp_path):
    path = _write_cfg(tmp_path, {**BASE_CFG, 'volumes': ['a:/b']})
    result = _invoke(path, ['mount', 'add', 'a:/b'])
    assert result.exit_code == 0, result.output
    assert 'already configured' in result.output
    assert _read(path)['volumes'] == ['a:/b']


def test_mount_add_rejects_specs_without_colon(tmp_path):
    path = _write_cfg(tmp_path, dict(BASE_CFG))
    result = _invoke(path, ['mount', 'add', 'justaname'])
    assert result.exit_code != 0
    assert 'invalid volume' in result.output
    assert 'volumes' not in _read(path)


def test_mount_remove_drops_key_when_empty(tmp_path):
    path = _write_cfg(tmp_path, {**BASE_CFG, 'volumes': ['a:/b']})
    result = _invoke(path, ['mount', 'remove', 'a:/b'])
    assert result.exit_code == 0, result.output
    assert 'volumes' not in _read(path)


def test_mount_remove_missing_errors(tmp_path):
    path = _write_cfg(tmp_path, dict(BASE_CFG))
    result = _invoke(path, ['mount', 'remove', 'nope:/x'])
    assert result.exit_code != 0
    assert 'not found' in result.output


def test_mount_list(tmp_path):
    path = _write_cfg(tmp_path, {**BASE_CFG, 'volumes': ['a:/b', 'c:/d']})
    result = _invoke(path, ['mount', 'list'])
    assert result.exit_code == 0, result.output
    assert 'a:/b' in result.output
    assert 'c:/d' in result.output


def test_env_set_adds(tmp_path):
    path = _write_cfg(tmp_path, dict(BASE_CFG))
    result = _invoke(path, ['env', 'set', 'HISTFILE=/x'])
    assert result.exit_code == 0, result.output
    assert _read(path)['environment'] == ['HISTFILE=/x']


def test_env_set_replaces_same_name(tmp_path):
    path = _write_cfg(tmp_path, {**BASE_CFG, 'environment': ['HISTFILE=/old', 'KEEP=1']})
    result = _invoke(path, ['env', 'set', 'HISTFILE=/new'])
    assert result.exit_code == 0, result.output
    env = _read(path)['environment']
    assert 'HISTFILE=/new' in env
    assert 'HISTFILE=/old' not in env
    assert 'KEEP=1' in env
    # exactly one entry for HISTFILE
    assert sum(1 for e in env if e.startswith('HISTFILE=')) == 1


def test_env_set_rejects_invalid_name(tmp_path):
    path = _write_cfg(tmp_path, dict(BASE_CFG))
    result = _invoke(path, ['env', 'set', 'bad name=1'])
    assert result.exit_code != 0
    assert 'environment' not in _read(path)


def test_env_unset(tmp_path):
    path = _write_cfg(tmp_path, {**BASE_CFG, 'environment': ['HISTFILE=/x', 'KEEP=1']})
    result = _invoke(path, ['env', 'unset', 'HISTFILE'])
    assert result.exit_code == 0, result.output
    assert _read(path)['environment'] == ['KEEP=1']


def test_env_unset_missing_errors(tmp_path):
    path = _write_cfg(tmp_path, dict(BASE_CFG))
    result = _invoke(path, ['env', 'unset', 'NOPE'])
    assert result.exit_code != 0
    assert 'not found' in result.output


def test_env_list(tmp_path):
    path = _write_cfg(tmp_path, {**BASE_CFG, 'environment': ['FOO=bar']})
    result = _invoke(path, ['env', 'list'])
    assert result.exit_code == 0, result.output
    assert 'FOO=bar' in result.output
