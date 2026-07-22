# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Tests for the user-facing error toolkit in cli.errors."""
import tempfile

import click
import pytest

from cli.errors import (
    LagerError,
    render_error,
    is_connection_error,
    connection_error,
    ssh_error,
    net_not_specified_error,
    system_error,
)


def plain(text):
    """Strip ANSI styling so assertions are color-independent."""
    return click.unstyle(text)


class TestRenderError:
    def test_includes_problem_cause_and_fixes(self):
        out = plain(render_error(
            'Box not found.',
            cause='No saved box matches.',
            fixes=['List boxes: lager boxes', 'Add it: lager boxes add'],
        ))
        assert 'Error: Box not found.' in out
        assert 'No saved box matches.' in out
        assert '→ List boxes: lager boxes' in out
        assert '→ Add it: lager boxes add' in out

    def test_problem_only(self):
        out = plain(render_error('Just a headline.'))
        assert out == 'Error: Just a headline.'

    def test_no_try_section_without_fixes(self):
        out = plain(render_error('Headline.', cause='Because.'))
        assert 'Try:' not in out

    def test_raw_hidden_without_debug(self, monkeypatch):
        monkeypatch.delenv('LAGER_DEBUG', raising=False)
        monkeypatch.setattr('sys.argv', ['lager'])
        out = plain(render_error('Boom.', raw='SECRET TRACEBACK'))
        assert 'SECRET TRACEBACK' not in out
        assert 'Run with --debug' in out

    def test_raw_shown_with_debug(self, monkeypatch):
        monkeypatch.setenv('LAGER_DEBUG', '1')
        out = plain(render_error('Boom.', raw='SECRET TRACEBACK'))
        assert 'SECRET TRACEBACK' in out
        assert '--- raw error ---' in out

    def test_no_debug_hint_when_nothing_hidden(self, monkeypatch):
        monkeypatch.delenv('LAGER_DEBUG', raising=False)
        monkeypatch.setattr('sys.argv', ['lager'])
        out = plain(render_error('Boom.'))  # no raw
        assert 'Run with --debug' not in out


class TestLagerError:
    def test_is_click_exception(self):
        # So Click renders it automatically when raised in a command.
        assert isinstance(LagerError('x'), click.ClickException)

    def test_default_exit_code(self):
        assert LagerError('x').exit_code == 1

    def test_custom_exit_code(self):
        assert LagerError('x', exit_code=3).exit_code == 3

    def test_format_message_matches_render(self):
        err = LagerError('Problem.', cause='Cause.', fixes=['Do thing'])
        assert 'Error: Problem.' in plain(err.format_message())
        assert '→ Do thing' in plain(err.format_message())

    def test_show_writes_to_stderr(self, capsys):
        LagerError('Problem.', fixes=['Do thing']).show()
        captured = capsys.readouterr()
        assert captured.err
        assert 'Error: Problem.' in plain(captured.err)
        assert not captured.out


class TestIsConnectionError:
    def test_builtin_connection_refused(self):
        assert is_connection_error(ConnectionRefusedError())

    def test_builtin_timeout(self):
        assert is_connection_error(TimeoutError())

    def test_requests_connection_error(self):
        exc = type('ConnectionError', (Exception,), {'__module__': 'requests.exceptions'})()
        assert is_connection_error(exc)

    def test_requests_timeout(self):
        exc = type('Timeout', (Exception,), {'__module__': 'requests.exceptions'})()
        assert is_connection_error(exc)

    def test_not_a_connection_error(self):
        assert not is_connection_error(ValueError('nope'))

    def test_unrelated_requests_exception(self):
        exc = type('JSONDecodeError', (Exception,), {'__module__': 'requests.exceptions'})()
        assert not is_connection_error(exc)


class TestConnectionErrorClassifier:
    def test_refused(self):
        err = connection_error(ConnectionRefusedError('[Errno 61] Connection refused'),
                               host='box1')
        out = plain(err.format_message())
        assert 'refused' in out.lower()
        assert 'box1' in out
        assert 'docker restart lager' in out

    def test_resolve_failure(self):
        err = connection_error(OSError('Name or service not known'), host='badname')
        out = plain(err.format_message())
        assert 'resolve' in out.lower()
        assert 'badname' in out

    def test_no_route(self):
        err = connection_error(OSError('No route to host'), host='box1')
        assert 'route' in plain(err.format_message()).lower()

    def test_timeout(self):
        exc = type('Timeout', (Exception,), {'__module__': 'requests.exceptions'})('timed out')
        err = connection_error(exc, host='box1')
        assert 'timed out' in plain(err.format_message()).lower()

    def test_generic_fallback(self):
        err = connection_error(OSError('something weird'), host='box1')
        out = plain(err.format_message())
        assert 'could not connect' in out.lower()

    def test_no_host_does_not_crash(self):
        err = connection_error(ConnectionRefusedError('Connection refused'))
        assert isinstance(err, LagerError)
        assert '[BOX_NAME]' in plain(err.format_message())


class TestSshErrorClassifier:
    def test_key_not_authorized(self):
        out = plain(ssh_error('Permission denied (publickey).', '10.0.0.5').format_message())
        assert 'key authentication failed' in out.lower()
        assert 'lager ssh-setup --box 10.0.0.5' in out
        assert 'ssh-copy-id lagerdata@10.0.0.5' in out

    def test_key_not_authorized_custom_user(self):
        out = plain(ssh_error('Permission denied (publickey).', '10.0.0.5', user='boxuser').format_message())
        assert 'lager ssh-setup --box 10.0.0.5' in out
        assert 'ssh-copy-id boxuser@10.0.0.5' in out

    def test_connection_refused(self):
        out = plain(ssh_error('connect to host x port 22: Connection refused', '10.0.0.5').format_message())
        assert 'refused' in out.lower()
        assert 'port 22' in out

    def test_no_route(self):
        out = plain(ssh_error('No route to host', '10.0.0.5').format_message())
        assert 'route' in out.lower()

    def test_resolve_failure(self):
        out = plain(ssh_error('Could not resolve hostname badbox', 'badbox').format_message())
        assert 'resolve' in out.lower()
        assert 'badbox' in out

    def test_host_key_changed(self):
        out = plain(ssh_error('Host key verification failed.', '10.0.0.5').format_message())
        assert 'host key' in out.lower()
        assert 'ssh-keygen -R 10.0.0.5' in out

    def test_generic_fallback(self):
        err = ssh_error('some weird ssh failure', '10.0.0.5')
        out = plain(err.format_message())
        assert isinstance(err, LagerError)
        assert 'over SSH' in out

    def test_empty_stderr_does_not_crash(self):
        assert isinstance(ssh_error('', '10.0.0.5'), LagerError)
        assert isinstance(ssh_error(None, '10.0.0.5'), LagerError)


class TestNetNotSpecifiedError:
    def test_basic(self):
        out = plain(net_not_specified_error('I2C', 'i2c').format_message())
        assert 'No I2C net specified' in out
        assert 'lager i2c [NET_NAME]' in out
        assert 'lager nets' in out

    def test_omits_default_hint_without_flag(self):
        out = plain(net_not_specified_error('I2C', 'i2c').format_message())
        assert 'defaults add' not in out

    def test_includes_default_hint_when_flag_given(self):
        out = plain(net_not_specified_error('UART', 'uart', default_flag='uart-net').format_message())
        assert 'lager defaults add --uart-net [NET_NAME]' in out


class TestDie:
    def test_die_prints_and_exits(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            LagerError('Boom.', exit_code=3).die()
        assert excinfo.value.code == 3
        assert 'Error: Boom.' in plain(capsys.readouterr().err)


class TestSystemErrorClassifier:
    def test_known_errno_maps(self):
        err = system_error(OSError('[Errno 16] Resource busy'))
        assert isinstance(err, LagerError)
        assert 'busy' in plain(err.format_message()).lower()

    def test_unknown_errno_returns_none(self):
        assert system_error(OSError('[Errno 2] No such file')) is None


class TestTopLevelFunnel:
    """cli.main.main() turns uncaught exceptions into friendly output."""

    def _run_with(self, monkeypatch, exc):
        import cli.main as m

        @click.command()
        def boom():
            raise exc
        monkeypatch.setattr(m, 'cli', boom)
        with pytest.raises(SystemExit) as excinfo:
            m.main()
        return excinfo.value.code

    def test_unexpected_exception_is_friendly(self, monkeypatch, capsys):
        monkeypatch.delenv('LAGER_DEBUG', raising=False)
        monkeypatch.setattr('sys.argv', ['lager'])
        code = self._run_with(monkeypatch, RuntimeError('internal detail'))
        err = plain(capsys.readouterr().err)
        assert code == 1
        assert 'Something went wrong.' in err
        assert '--debug' in err

    def test_debug_reraises_real_traceback(self, monkeypatch):
        monkeypatch.setenv('LAGER_DEBUG', '1')
        monkeypatch.setattr('sys.argv', ['lager'])
        import cli.main as m

        @click.command()
        def boom():
            raise RuntimeError('internal detail')
        monkeypatch.setattr(m, 'cli', boom)
        with pytest.raises(RuntimeError, match='internal detail'):
            m.main()

    def test_connection_error_is_classified(self, monkeypatch, capsys):
        monkeypatch.delenv('LAGER_DEBUG', raising=False)
        monkeypatch.setattr('sys.argv', ['lager'])
        code = self._run_with(monkeypatch,
                              ConnectionRefusedError('[Errno 61] Connection refused'))
        err = plain(capsys.readouterr().err)
        assert code == 1
        assert 'refused' in err.lower()

    def test_lager_error_uses_own_rendering(self, monkeypatch, capsys):
        monkeypatch.setattr('sys.argv', ['lager'])
        code = self._run_with(monkeypatch,
                              LagerError('Custom problem.', fixes=['Do X'], exit_code=2))
        err = plain(capsys.readouterr().err)
        assert code == 2
        assert 'Custom problem.' in err
        assert '→ Do X' in err


class TestBoxNotFoundError:
    def test_lists_saved_boxes(self, monkeypatch):
        import cli.box_storage as bs
        monkeypatch.setattr(bs, 'list_boxes',
                            lambda: {'bench-1': {'ip': '10.0.0.1'}})
        err = bs.box_not_found_error('typo')
        out = plain(err.format_message())
        assert isinstance(err, LagerError)
        assert "No box named 'typo'." in out
        assert 'bench-1 (10.0.0.1)' in out
        assert '--name typo --ip [IP_ADDRESS]' in out

    def test_no_saved_boxes(self, monkeypatch):
        import cli.box_storage as bs
        monkeypatch.setattr(bs, 'list_boxes', lambda: {})
        out = plain(bs.box_not_found_error('typo').format_message())
        assert 'no saved boxes yet' in out.lower()


class TestMigratedConfigPath:
    def test_invalid_json_raises_lager_error(self):
        from cli.config import read_config_file
        with tempfile.NamedTemporaryFile('w', suffix='.lager', delete=False) as f:
            f.write('{ not valid json ]')
            path = f.name
        with pytest.raises(LagerError) as excinfo:
            read_config_file(path)
        out = plain(excinfo.value.format_message())
        assert 'not valid JSON' in out
        assert path in out
