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
    """Make box 10.0.0.5 look access-gated with (or without) a stored token."""
    monkeypatch.setattr('cli.gateway_auth.auth_server_for_box',
                        lambda ip: auth_url)
    monkeypatch.setattr('cli.gateway_auth.access_token_for',
                        lambda url: token)
    monkeypatch.setattr('cli.gateway_auth._token_expires_at',
                        lambda tok: time.time() + ttl)


class TestViewerUrl:
    def test_ungated_box_leaves_url_alone(self, monkeypatch):
        monkeypatch.setattr('cli.gateway_auth.auth_server_for_box', lambda ip: None)
        assert webcam_mod._viewer_url('10.0.0.5', 'http://10.0.0.5:8086/') == \
            'http://10.0.0.5:8086/'

    def test_gated_box_appends_token(self, monkeypatch):
        _gate(monkeypatch, token='a b/c')
        assert webcam_mod._viewer_url('10.0.0.5', 'http://10.0.0.5:8086/') == \
            'http://10.0.0.5:8086/?token=a%20b%2Fc'

    def test_existing_query_is_extended_not_replaced(self, monkeypatch):
        _gate(monkeypatch, token='t')
        assert webcam_mod._viewer_url('10.0.0.5', 'http://10.0.0.5:8086/?x=1') == \
            'http://10.0.0.5:8086/?x=1&token=t'

    def test_gated_box_without_login_leaves_url_alone(self, monkeypatch):
        _gate(monkeypatch, token=None)
        assert webcam_mod._viewer_url('10.0.0.5', 'http://10.0.0.5:8086/') == \
            'http://10.0.0.5:8086/'

    def test_none_url_passes_through(self, monkeypatch):
        _gate(monkeypatch)
        assert webcam_mod._viewer_url('10.0.0.5', None) is None


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
        assert 'Webcam URL: http://10.0.0.5:8086/' in output
        assert 'token' not in output

    def test_gated_box_prints_tokenised_link_and_lifetime(self, monkeypatch):
        _gate(monkeypatch, token='tok', ttl=14 * 60 + 5)
        result, output = _invoke(['cam1', 'start', '--box', 'bench'])
        assert result.exit_code == 0, output
        assert 'Webcam URL: http://10.0.0.5:8086/?token=tok' in output
        assert 'about 15 minutes' in output
        assert 'lager webcam url --box bench' in output
        # The old "not reachable" warning is gone: the gateway fronts the port.
        assert 'not exposed' not in output

    def test_gated_box_without_login_points_at_login(self, monkeypatch):
        _gate(monkeypatch, token=None)
        result, output = _invoke(['cam1', 'start', '--box', 'bench'])
        assert result.exit_code == 0, output
        assert 'Webcam URL: http://10.0.0.5:8086/' in output
        assert 'lager login http://plane.example' in output


class TestUrl:
    def test_lists_origin_and_tokenises(self, monkeypatch):
        _gate(monkeypatch, token='tok', ttl=120)
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
            result, output = _invoke(['url', '--box', 'bench'])
        finally:
            mock.patch.stopall()
        assert result.exit_code == 0, output
        assert 'URL: http://10.0.0.5:8086/?token=tok' in output
        assert 'Origin: started via api by bob' in output
        assert 'cam2' not in output
        assert 'about 2 minutes' in output


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
