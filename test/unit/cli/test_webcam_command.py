# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for ``cli/commands/utility/webcam.py`` -- the `lager webcam`
command layer.

What lives only here:

  * the viewer link on an access-gated box. A browser cannot attach a
    sign-in token to a plain link, so the CLI has to put it in the query
    string; a regression here silently hands users a link that 401s.
  * the origin fields (``source``/``started_by``) on the start wire, which
    other surfaces sharing the box rely on to label CLI-started streams.
  * ``snapshot`` writing the decoded frame to disk.

``post_net_command`` and the box resolver are patched on the webcam module,
so nothing resolves a real box, takes a lock, or opens a socket.
"""

import base64
import time
from importlib import import_module
from types import SimpleNamespace
from unittest import mock

from click.testing import CliRunner

# import_module for the same reason as test_login_commands.py: the package
# __init__ re-exports `webcam` as a click Group, shadowing the module name.
webcam_mod = import_module('cli.commands.utility.webcam')


def _invoke(args):
    result = CliRunner().invoke(webcam_mod.webcam, args, obj=SimpleNamespace(),
                                catch_exceptions=False)
    try:
        stderr = result.stderr
    except ValueError:
        stderr = ''
    output = result.output
    if stderr and stderr not in output:
        output += stderr
    return result, output


def _gate(monkeypatch, auth_url='http://plane.example', token='tok.en', ttl=900):
    """Make box 10.0.0.5 look access-gated with (or without) a stored token.

    Returns the list of keyword arguments each token lookup was made with.
    """
    lookups = []

    def access_token_for(url, **kwargs):
        lookups.append(kwargs)
        return token

    monkeypatch.delenv('LAGER_GATEWAY_TOKEN', raising=False)
    monkeypatch.setattr('cli.gateway_auth.auth_server_for_box',
                        lambda ip: auth_url)
    monkeypatch.setattr('cli.gateway_auth.access_token_for', access_token_for)
    monkeypatch.setattr('cli.gateway_auth._token_expires_at',
                        lambda tok: time.time() + ttl)
    return lookups


GATE = ('http://plane.example', 'tok')


class TestGatedToken:
    def test_ungated_box_has_no_token(self, monkeypatch):
        monkeypatch.setattr('cli.gateway_auth.auth_server_for_box', lambda ip: None)
        assert webcam_mod._gated_token('10.0.0.5') == (None, None)

    def test_link_token_asks_for_most_of_a_lifetime(self, monkeypatch):
        # A link cannot be refreshed once printed, so the lookup asks for a
        # token with at least half its lifetime left.
        lookups = _gate(monkeypatch, token='tok')
        assert webcam_mod._gated_token('10.0.0.5') == ('http://plane.example', 'tok')
        assert lookups == [{'min_remaining_fraction': 0.5}]


class TestViewerUrl:
    def test_ungated_box_leaves_url_alone(self):
        assert webcam_mod._viewer_url('http://10.0.0.5:8086/', (None, None)) == \
            'http://10.0.0.5:8086/'

    def test_gated_box_appends_token(self):
        assert webcam_mod._viewer_url('http://10.0.0.5:8086/',
                                      ('http://plane.example', 'a b/c')) == \
            'http://10.0.0.5:8086/?token=a%20b%2Fc'

    def test_existing_query_is_extended_not_replaced(self):
        assert webcam_mod._viewer_url('http://10.0.0.5:8086/?x=1',
                                      ('http://plane.example', 't')) == \
            'http://10.0.0.5:8086/?x=1&token=t'

    def test_gated_box_without_login_leaves_url_alone(self):
        assert webcam_mod._viewer_url('http://10.0.0.5:8086/',
                                      ('http://plane.example', None)) == \
            'http://10.0.0.5:8086/'

    def test_none_url_passes_through(self):
        assert webcam_mod._viewer_url(None, GATE) is None


def _note(capsys, gate, **kwargs):
    printed = webcam_mod._gated_link_note(gate, 'bench', **kwargs)
    return printed, capsys.readouterr().out


class TestGatedLinkNote:
    def test_ungated_prints_nothing(self, capsys):
        assert _note(capsys, (None, None)) == (False, '')

    def test_signed_in_note_promises_only_the_open_window(self, capsys, monkeypatch):
        # Opening the link trades the token for a gateway session whose length
        # depends on the gateway, so the note must not promise how long the
        # viewer lasts -- only how long the link can still be opened.
        _gate(monkeypatch, ttl=14 * 60 + 50)
        printed, out = _note(capsys, GATE)
        assert printed
        assert out == (
            'Open this link within 15 minutes; it carries your sign-in.\n'
            'If it has expired, or the viewer later asks for sign-in, run: '
            'lager webcam url --box bench\n')
        assert 'valid for' not in out

    def test_several_links_and_one_minute(self, capsys, monkeypatch):
        _gate(monkeypatch, ttl=20)
        _printed, out = _note(capsys, GATE, links=2, refresh='lager x')
        assert out.startswith(
            'Open these links within 1 minute; they carry your sign-in.\n'
            'If they have expired')
        assert out.rstrip().endswith('run: lager x')

    def test_pinned_token_claims_no_minute_count(self, capsys, monkeypatch):
        # A pinned CI token is opaque, so there is no `exp` to read: the
        # minute count would report a token minted for a week as one minute.
        _gate(monkeypatch)
        monkeypatch.setenv('LAGER_GATEWAY_TOKEN', 'ci-token')
        printed, out = _note(capsys, ('http://plane.example', 'ci-token'))
        assert printed
        assert 'LAGER_GATEWAY_TOKEN' in out
        assert 'minute' not in out
        assert out.rstrip().endswith('lager webcam url --box bench')

    def test_no_stored_login_points_at_login(self, capsys):
        printed, out = _note(capsys, ('http://plane.example', None))
        assert printed
        assert 'lager login http://plane.example' in out
        assert 'Open this link' not in out


def _lines(output):
    return output.splitlines()


class TestStart:
    def setup_method(self):
        self.post = mock.patch.object(
            webcam_mod, 'post_net_command',
            return_value={'message': 'ok',
                          'value': {'url': 'http://10.0.0.5:8086/', 'port': 8086}}).start()
        mock.patch.object(webcam_mod, '_get_box_ip_address',
                          return_value='10.0.0.5').start()
        mock.patch('cli.box_storage.get_lager_user', return_value='alice').start()

    def teardown_method(self):
        mock.patch.stopall()

    def test_start_records_cli_origin_on_the_wire(self, monkeypatch):
        monkeypatch.setattr('cli.gateway_auth.auth_server_for_box', lambda ip: None)
        result, output = _invoke(['cam1', 'start', '--box', 'bench'])
        assert result.exit_code == 0, output
        kwargs = self.post.call_args.kwargs
        assert kwargs['source'] == 'cli'
        assert kwargs['started_by'] == 'alice'
        assert kwargs['box_ip'] == '10.0.0.5'
        assert self.post.call_args.args[3] == 'start'
        assert 'token' not in output

    def test_ungated_layout(self, monkeypatch):
        monkeypatch.setattr('cli.gateway_auth.auth_server_for_box', lambda ip: None)
        result, output = _invoke(['cam1', 'start', '--box', 'bench'])
        assert result.exit_code == 0, output
        assert _lines(output)[-5:] == [
            'Webcam URL:',
            'http://10.0.0.5:8086/',
            'Open this URL in your browser to view the live feed.',
            '',
            'To stop the stream: lager webcam cam1 stop --box bench',
        ]

    def test_gated_layout(self, monkeypatch):
        # The link alone on its line, the note directly under it, the stop
        # hint last, and no second "open this in your browser" line.
        lookups = _gate(monkeypatch, token='tok', ttl=14 * 60 + 5)
        result, output = _invoke(['cam1', 'start', '--box', 'bench'])
        assert result.exit_code == 0, output
        assert _lines(output)[-6:] == [
            'Webcam URL:',
            'http://10.0.0.5:8086/?token=tok',
            'Open this link within 14 minutes; it carries your sign-in.',
            'If it has expired, or the viewer later asks for sign-in, run: '
            'lager webcam url --box bench',
            '',
            'To stop the stream: lager webcam cam1 stop --box bench',
        ]
        assert 'in your browser' not in output
        # One lookup serves the link and the note, so they agree on a token.
        assert lookups == [{'min_remaining_fraction': 0.5}]

    def test_pinned_token_link(self, monkeypatch):
        _gate(monkeypatch, token='ci-token')
        monkeypatch.setenv('LAGER_GATEWAY_TOKEN', 'ci-token')
        result, output = _invoke(['cam1', 'start', '--box', 'bench'])
        assert result.exit_code == 0, output
        assert 'http://10.0.0.5:8086/?token=ci-token' in _lines(output)
        assert 'LAGER_GATEWAY_TOKEN' in output
        assert 'minute' not in output
        assert _lines(output)[-1].startswith('To stop the stream: ')

    def test_gated_box_without_login_points_at_login(self, monkeypatch):
        _gate(monkeypatch, token=None)
        result, output = _invoke(['cam1', 'start', '--box', 'bench'])
        assert result.exit_code == 0, output
        lines = _lines(output)
        link = lines.index('http://10.0.0.5:8086/')
        assert 'lager login http://plane.example' in lines[link + 1]
        assert 'in your browser' not in output

    def test_printed_stop_hint_is_a_command_that_parses(self, monkeypatch):
        # The hint is the line a first-time user copies. It used to print
        # `webcam stop cam1`, which click rejects with "unexpected extra
        # argument" before it reaches the box: NETNAME comes first. Replay
        # the printed line through the group rather than string-matching it,
        # so the two can never drift.
        monkeypatch.setattr('cli.gateway_auth.auth_server_for_box', lambda ip: None)
        result, output = _invoke(['cam1', 'start', '--box', 'bench'])
        assert result.exit_code == 0, output
        hint = next(line for line in output.splitlines()
                    if line.startswith('To stop the stream: '))
        assert hint.endswith('lager webcam cam1 stop --box bench'), hint

        replay, replay_output = _invoke(hint.split('lager webcam ', 1)[1].split())
        assert replay.exit_code == 0, replay_output
        assert self.post.call_args.args[3] == 'stop'

    def test_start_all_stop_hint_replays_too(self, monkeypatch):
        # Same contract for the all-nets pair, which prints its own hint.
        monkeypatch.setattr('cli.gateway_auth.auth_server_for_box', lambda ip: None)
        with mock.patch.object(webcam_mod, '_list_webcam_nets',
                               return_value=[{'name': 'cam1'}]):
            result, output = _invoke(['start-all', '--box', 'bench'])
            assert result.exit_code == 0, output
            hint = next(line for line in output.splitlines()
                        if line.startswith('To stop all streams: '))
            assert hint.endswith('lager webcam stop-all --box bench'), hint

            replay, replay_output = _invoke(hint.split('lager webcam ', 1)[1].split())
        assert replay.exit_code == 0, replay_output
        assert self.post.call_args.args[3] == 'stop'

    def test_start_all_gated_layout(self, monkeypatch):
        _gate(monkeypatch, token='tok', ttl=900)
        with mock.patch.object(webcam_mod, '_list_webcam_nets',
                               return_value=[{'name': 'cam1'}, {'name': 'cam2'}]):
            result, output = _invoke(['start-all', '--box', 'bench'])
        assert result.exit_code == 0, output
        assert _lines(output)[-8:] == [
            '  cam1: started',
            '  http://10.0.0.5:8086/?token=tok',
            '  cam2: started',
            '  http://10.0.0.5:8086/?token=tok',
            'Open these links within 15 minutes; they carry your sign-in.',
            'If they have expired, or the viewer later asks for sign-in, run: '
            'lager webcam url --box bench',
            '',
            'To stop all streams: lager webcam stop-all --box bench',
        ]
        assert 'in your browser' not in output


class TestUrl:
    def _run(self):
        mock.patch.object(webcam_mod, '_get_box_ip_address',
                          return_value='10.0.0.5').start()
        mock.patch.object(webcam_mod, '_list_webcam_nets',
                          return_value=[{'name': 'cam1'}, {'name': 'cam2'}]).start()

        def fake_post(ctx, box_addr, net, action, **kw):
            assert action == 'status'
            if net == 'cam1':
                return {'value': {'running': True, 'url': 'http://10.0.0.5:8086/',
                                  'port': 8086, 'video_device': '/dev/video0',
                                  'source': 'api', 'started_by': 'bob'}}
            return {'value': {'running': False}}

        mock.patch.object(webcam_mod, 'post_net_command', side_effect=fake_post).start()
        try:
            return _invoke(['url', '--box', 'bench'])
        finally:
            mock.patch.stopall()

    def test_lists_origin_and_tokenises(self, monkeypatch):
        _gate(monkeypatch, token='tok', ttl=120)
        result, output = self._run()
        assert result.exit_code == 0, output
        assert 'cam2' not in output
        assert _lines(output) == [
            'Active webcam streams on 10.0.0.5:',
            '',
            'cam1:',
            '  Port: 8086',
            '  Device: /dev/video0',
            '  Origin: started via api by bob',
            '  http://10.0.0.5:8086/?token=tok',
            'Open this link within 2 minutes; it carries your sign-in.',
            'If it has expired, or the viewer later asks for sign-in, run: '
            'lager webcam url --box bench',
        ]

    def test_ungated_ends_with_the_link(self, monkeypatch):
        monkeypatch.setattr('cli.gateway_auth.auth_server_for_box', lambda ip: None)
        result, output = self._run()
        assert result.exit_code == 0, output
        assert _lines(output)[-1] == '  http://10.0.0.5:8086/'


class TestSnapshot:
    def setup_method(self):
        self.frame = b'\xff\xd8not-really-a-jpeg'
        self.post = mock.patch.object(
            webcam_mod, 'post_net_command',
            return_value={'message': 'Snapshot captured',
                          'value': {'jpeg_base64': base64.b64encode(self.frame).decode(),
                                    'bytes': len(self.frame)}}).start()
        mock.patch.object(webcam_mod, '_get_box_ip_address',
                          return_value='10.0.0.5').start()

    def teardown_method(self):
        mock.patch.stopall()

    def test_writes_decoded_frame_to_out(self, tmp_path):
        out = tmp_path / 'frame.jpg'
        result, output = _invoke(['cam1', 'snapshot', '--box', 'bench', '--out', str(out)])
        assert result.exit_code == 0, output
        assert out.read_bytes() == self.frame
        assert f'Saved {out} ({len(self.frame)} bytes)' in output
        assert self.post.call_args.args[2:4] == ('cam1', 'snapshot')

    def test_default_filename_is_net_and_timestamp(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result, output = _invoke(['cam1', 'snapshot', '--box', 'bench'])
        assert result.exit_code == 0, output
        files = list(tmp_path.glob('cam1-*.jpg'))
        assert len(files) == 1
        assert files[0].read_bytes() == self.frame

    def test_requires_net_name(self):
        mock.patch.object(webcam_mod, 'get_default_net', return_value=None).start()
        result, output = _invoke(['snapshot', '--box', 'bench'])
        assert result.exit_code != 0
        assert 'NET_NAME required' in output
