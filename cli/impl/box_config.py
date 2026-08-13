#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Box config CLI shim shipped into the container by run_python_internal.

This file is uploaded and executed on the box; it is never imported by the
CLI. It still ships inside the lager-cli wheel, because get_impl_path()
resolves it from the installed package on disk -- so it has to remain
importable on a host that has no box tree. Both the /app/lager path insert
and the `lager` import therefore live inside __main__ rather than at module
scope. Same pattern as cli/impl/measurement/scope.py.
"""

if __name__ == "__main__":
    import sys

    sys.path.insert(0, '/app/lager')

    from lager.box_config.box_config_cli import _cli

    _cli()
