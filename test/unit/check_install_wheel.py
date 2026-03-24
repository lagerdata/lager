# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

# check_install_wheel.py
# Verifies that a wheel installed via lager install-wheel is importable
# Run with: lager python test/unit/check_install_wheel.py --box <BOX>

import importlib
import subprocess
import sys

PACKAGE = 'requests'        # Change to the package you installed
IMPORT_NAME = 'requests'    # Module name to import (usually same as package)

def main():
    print(f'Checking package: {PACKAGE}')

    # Check pip knows about it
    result = subprocess.run(
        ['pip3', 'show', PACKAGE],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f'FAIL: {PACKAGE} is not installed (pip3 show returned non-zero)')
        sys.exit(1)

    # Parse version from pip show output
    version = None
    for line in result.stdout.splitlines():
        if line.startswith('Version:'):
            version = line.split(':', 1)[1].strip()
    print(f'  pip3 show: installed version {version}')

    # Check it is importable
    try:
        mod = importlib.import_module(IMPORT_NAME)
        pkg_version = getattr(mod, '__version__', '(no __version__)')
        print(f'  import {IMPORT_NAME}: OK (version {pkg_version})')
    except ImportError as e:
        print(f'FAIL: could not import {IMPORT_NAME}: {e}')
        sys.exit(1)

    print(f'PASS: {PACKAGE} is installed and importable')

if __name__ == '__main__':
    main()
