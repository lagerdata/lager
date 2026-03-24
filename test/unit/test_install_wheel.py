# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

# test_install_wheel.py
# Unit tests for the install-wheel command (no box required).
# Run with: pytest test/unit/test_install_wheel.py -v

import os
import tempfile
import pytest
from click.testing import CliRunner
from cli.commands.utility.install_wheel import install_wheel


# ---------------------------------------------------------------------------
# Wheel filename → package name parsing
# ---------------------------------------------------------------------------

def _parse_package_name(wheel_basename):
    """Replicate the parsing logic from the command."""
    return wheel_basename.split('-')[0].replace('_', '-')


class TestPackageNameParsing:
    def test_simple_name(self):
        assert _parse_package_name('requests-2.31.0-py3-none-any.whl') == 'requests'

    def test_underscore_converted_to_hyphen(self):
        assert _parse_package_name('my_package-0.1.0-cp310-abi3-manylinux_2_17_x86_64.manylinux2014_x86_64.whl') == 'my-package'

    def test_multiple_underscores(self):
        assert _parse_package_name('my_cool_package-1.0.0-py3-none-any.whl') == 'my-cool-package'

    def test_no_underscores(self):
        assert _parse_package_name('numpy-1.24.0-cp310-cp310-linux_x86_64.whl') == 'numpy'

    def test_with_build_tag(self):
        # {distribution}-{version}-{build tag}-{python}-{abi}-{platform}.whl
        assert _parse_package_name('greenlet-3.0.0-1-cp310-cp310-linux_x86_64.whl') == 'greenlet'


# ---------------------------------------------------------------------------
# CLI validation (no box connection needed)
# ---------------------------------------------------------------------------

class TestInstallWheelCLI:
    def setup_method(self):
        self.runner = CliRunner()

    def test_missing_file_exits_with_error(self):
        result = self.runner.invoke(install_wheel, ['nonexistent_file.whl'])
        assert result.exit_code != 0
        assert 'not found' in result.output.lower() or 'not found' in (result.stderr or '').lower()

    def test_non_whl_extension_exits_with_error(self):
        with tempfile.NamedTemporaryFile(suffix='.tar.gz', delete=False) as f:
            f.write(b'fake')
            tmp = f.name
        try:
            result = self.runner.invoke(install_wheel, [tmp])
            assert result.exit_code != 0
            assert '.whl' in result.output or '.whl' in (result.stderr or '')
        finally:
            os.unlink(tmp)

    def test_missing_wheel_arg_shows_usage(self):
        result = self.runner.invoke(install_wheel, [])
        # Click should report missing argument
        assert result.exit_code != 0

    def test_unreadable_file_exits_with_error(self):
        with tempfile.NamedTemporaryFile(suffix='.whl', delete=False) as f:
            f.write(b'fake')
            tmp = f.name
        try:
            os.chmod(tmp, 0o000)
            result = self.runner.invoke(install_wheel, [tmp])
            assert result.exit_code != 0
            assert 'permission denied' in result.output.lower() or 'permission denied' in (result.stderr or '').lower()
        finally:
            os.chmod(tmp, 0o644)
            os.unlink(tmp)

    def test_oversized_file_exits_with_error(self):
        from cli.commands.development.python import MAX_ZIP_SIZE
        with tempfile.NamedTemporaryFile(suffix='.whl', delete=False) as f:
            f.write(b'x' * (MAX_ZIP_SIZE + 1))
            tmp = f.name
        try:
            result = self.runner.invoke(install_wheel, [tmp])
            assert result.exit_code != 0
            assert 'too large' in result.output.lower() or 'too large' in (result.stderr or '').lower()
        finally:
            os.unlink(tmp)
