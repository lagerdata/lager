# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Guard the python/rust shared-core refactor.

`run_python_internal` / `run_python_internal_get_output` are called positionally
from ~30 sites across the CLI, so their signatures are frozen. We also confirm the
helpers that moved into _runner.py are still importable from python.py (some call
sites and tooling import them from there) and that rust exposes a matching engine.
"""

from __future__ import annotations

import inspect
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
sys.path.insert(0, _REPO_ROOT)

import importlib  # noqa: E402

# NB: the development package binds `python`/`rust` to the *commands*, shadowing
# the submodules — so import the modules via sys.modules, not attribute access.
py_mod = importlib.import_module('cli.commands.development.python')  # noqa: E402
rust_mod = importlib.import_module('cli.commands.development.rust')  # noqa: E402


_RUN_INTERNAL_PARAMS = [
    'ctx', 'runnable', 'box', 'env', 'passenv', 'kill', 'download',
    'allow_overwrite', 'signum', 'timeout', 'detach', 'port', 'org', 'args',
    'extra_files', 'callback', 'dut_name',
]
_GET_OUTPUT_PARAMS = [
    'ctx', 'runnable', 'box', 'env', 'passenv', 'kill', 'download',
    'allow_overwrite', 'signum', 'timeout', 'detach', 'port', 'org', 'args',
    'extra_files',
]


def test_run_python_internal_signature_frozen():
    params = list(inspect.signature(py_mod.run_python_internal).parameters)
    assert params == _RUN_INTERNAL_PARAMS


def test_run_python_internal_get_output_signature_frozen():
    params = list(inspect.signature(py_mod.run_python_internal_get_output).parameters)
    assert params == _GET_OUTPUT_PARAMS


def test_run_rust_internal_signature_matches_python():
    assert list(inspect.signature(rust_mod.run_rust_internal).parameters) == _RUN_INTERNAL_PARAMS
    assert list(inspect.signature(rust_mod.run_rust_internal_get_output).parameters) == _GET_OUTPUT_PARAMS


def test_moved_helpers_still_importable_from_python():
    from cli.commands.development.python import (  # noqa: F401
        _SIGNAL_MAP,
        _SIGNAL_CHOICES,
        _get_signal_number,
        sigint_handler,
        _do_exit,
        _handle_reattach,
        collect_output_callback,
        _ORIGINAL_SIGINT_HANDLER,
    )
    assert _SIGNAL_MAP['SIGTERM'] == 15
    assert _get_signal_number('sigkill') == 9


def test_init_still_exports_python_internals():
    from cli.commands.development import (  # noqa: F401
        run_python_internal,
        run_python_internal_get_output,
        python,
        rust,
    )
