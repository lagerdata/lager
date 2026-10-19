# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

import pytest


@pytest.fixture(autouse=True)
def _gateway_login_store_off_the_real_home(tmp_path_factory, monkeypatch):
    """Keep every test away from the developer's own gateway login.

    `lager devenv terminal` and `lager exec` create the store before mounting
    it, so a test that reaches them with the real HOME writes to the machine
    it runs on. Tests of the default location unset this and point HOME at a
    temporary directory themselves.
    """
    store = tmp_path_factory.mktemp('gateway_login') / 'gateway_auth.json'
    monkeypatch.setenv('LAGER_GATEWAY_AUTH_FILE', str(store))


class FakeBox:
    """A box that answers the CLI's HTTP calls in-process and records them.

    Used by the command-surface tests (``test_cmd_*``). Each POST is kept as
    ``(url, json)`` so a test can assert the exact payload a command sends;
    ``answer`` decides the reply. Nets come back from ``/nets/list``.
    """

    IP = '192.0.2.10'

    def __init__(self):
        self.nets = []
        self.posts = []
        self.gets = []
        self.execs = []
        self.answer = lambda url, body: {'success': True, 'message': 'done'}
        self.status = 200

    def net(self, name, role, **extra):
        self.nets.append({'name': name, 'role': role, **extra})
        return self

    @property
    def last(self):
        assert self.posts, 'the command sent nothing to the box'
        return self.posts[-1][1]

    def _resp(self, payload, status=200):
        import requests
        resp = requests.Response()
        resp.status_code = status
        resp._content = __import__('json').dumps(payload).encode()
        resp.headers['Content-Type'] = 'application/json'
        return resp

    def post(self, url, json=None, **kwargs):
        self.posts.append((url, json))
        return self._resp(self.answer(url, json), self.status)

    def run_python(self, ctx, path, box, *args, env=(), **kwargs):
        """Stand-in for `run_python_internal`: records the script and its data."""
        import json as _json
        import os as _os
        data = None
        for item in env:
            if item.startswith('LAGER_COMMAND_DATA='):
                data = _json.loads(item.split('=', 1)[1])
        self.execs.append((_os.path.basename(str(path)), box, data))

    @property
    def last_exec(self):
        assert self.execs, 'the command uploaded no script to the box'
        return self.execs[-1]

    def get(self, url, **kwargs):
        self.gets.append(url)
        if url.endswith('/nets/list'):
            return self._resp(self.nets)
        return self._resp({}, 404)


@pytest.fixture
def fake_box(monkeypatch, tmp_path):
    """Route every box call made by a command to an in-process FakeBox.

    Resolution, the auto-lock, the gateway and auth headers are all replaced,
    and the config dir and cwd point at an empty temp dir so no developer
    config or project `.lager` can leak in.
    """
    import cli.box_storage as box_storage
    import cli.gateway_auth as gateway_auth
    from cli import config as cfg

    box = FakeBox()
    monkeypatch.setenv('LAGER_CONFIG_FILE_DIR', str(tmp_path))
    monkeypatch.setenv('LAGER_NO_UPDATE_CHECK', '1')
    monkeypatch.setenv('LAGER_AUTO_LOCK_DISABLE', '1')
    monkeypatch.chdir(tmp_path)
    cfg._config_cache.clear()
    cfg._config_cache_mtime.clear()
    monkeypatch.setattr(box_storage, 'resolve_and_validate_box',
                        lambda ctx, name=None, *a, **k: FakeBox.IP)
    monkeypatch.setattr(box_storage, 'resolve_and_validate_box_with_name',
                        lambda ctx, name=None, *a, **k: (FakeBox.IP, 'bench-a'))
    monkeypatch.setattr(box_storage, 'auto_lock_acquire_for_command',
                        lambda *a, **k: (lambda: None))
    monkeypatch.setattr(box_storage, '_check_gateway', lambda resp, ip, **k: resp)
    monkeypatch.setattr(gateway_auth, 'auth_headers_for_box', lambda ip, *a, **k: {})
    monkeypatch.setattr('requests.post', box.post)
    monkeypatch.setattr('requests.get', box.get)
    import importlib
    for name in ('cli.commands.development.python', 'cli.commands.measurement.scope'):
        monkeypatch.setattr(importlib.import_module(name), 'run_python_internal',
                            box.run_python)
    yield box
    cfg._config_cache.clear()
    cfg._config_cache_mtime.clear()


@pytest.fixture
def lager(fake_box):
    """Run `lager <argv>` through the real root group against ``fake_box``."""
    from click.testing import CliRunner
    from cli.main import cli

    def run(*argv, input=None):
        return CliRunner().invoke(cli, list(argv), input=input, catch_exceptions=False)
    return run
