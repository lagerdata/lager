# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
The gateway login store shared with a container through a single-file mount.

A bind mount is attached to the file's inode. Saves therefore overwrite the
store in place: a save that renamed a new file over it left the container
holding a file nothing updated any more (on Docker Desktop it reads as
missing), and made the container's own saves fail on the mount point.
"""
import json
import os
import stat

import pytest

from cli import gateway_auth


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / '.lager_gateway_auth'
    monkeypatch.setenv(gateway_auth.STORE_FILE_ENV, str(path))
    monkeypatch.delenv(gateway_auth.PINNED_TOKEN_ENV, raising=False)
    monkeypatch.setattr(gateway_auth, '_unparseable', set())
    return path


def test_a_save_keeps_the_inode(store):
    gateway_auth.record_box_auth_server('10.0.0.1', 'https://auth.example')
    inode = store.stat().st_ino

    gateway_auth.record_box_auth_server('10.0.0.2', 'https://auth.example')
    gateway_auth.save_login('https://auth.example', 'token', {'refresh': 'r'})

    assert store.stat().st_ino == inode
    assert json.loads(store.read_text())['boxes'] == {
        '10.0.0.1': 'https://auth.example', '10.0.0.2': 'https://auth.example'}
    assert [p.name for p in store.parent.iterdir()] == [store.name]


def test_a_new_store_is_created_0600(store):
    gateway_auth.record_box_auth_server('10.0.0.1', 'https://auth.example')
    assert stat.S_IMODE(store.stat().st_mode) == 0o600


def test_a_store_made_with_touch_ends_up_0600(store):
    """Dev container setups `touch` the file before mounting it: 0644, empty."""
    store.touch(mode=0o644)
    os.chmod(store, 0o644)
    inode = store.stat().st_ino

    gateway_auth.save_login('https://auth.example', 'token', {'refresh': 'r'})

    assert stat.S_IMODE(store.stat().st_mode) == 0o600
    assert store.stat().st_ino == inode


def test_a_shorter_store_leaves_no_old_tail(store):
    """The write is not preceded by a truncate, so it must end with one."""
    for n in range(5):
        gateway_auth.record_box_auth_server(f'10.0.0.{n}', 'https://auth.example')
    store_now = json.loads(store.read_text())
    store_now['boxes'] = {}

    gateway_auth._save_store(store_now)

    assert json.loads(store.read_text())['boxes'] == {}


def test_a_save_through_a_symlink_keeps_the_symlink(store, tmp_path):
    real = tmp_path / 'real_store.json'
    real.write_text('{}')
    store.symlink_to(real)

    gateway_auth.record_box_auth_server('10.0.0.1', 'https://auth.example')

    assert store.is_symlink()
    assert json.loads(real.read_text())['boxes'] == {'10.0.0.1': 'https://auth.example'}


def test_a_read_that_lands_inside_a_write_is_retried(store, monkeypatch):
    """Another process's in-place write can be caught half done."""
    store.write_text('{"boxes": {"10.0.0.1": "https://au')
    sleeps = []

    def finish_the_write(seconds):
        sleeps.append(seconds)
        store.write_text('{"boxes": {"10.0.0.1": "https://auth.example"}}')

    monkeypatch.setattr(gateway_auth.time, 'sleep', finish_the_write)

    assert gateway_auth.auth_server_for_box('10.0.0.1') == 'https://auth.example'
    assert len(sleeps) == 1


def test_a_store_that_stays_broken_reads_empty_and_waits_once(store, monkeypatch):
    store.write_text('not json')
    sleeps = []
    monkeypatch.setattr(gateway_auth.time, 'sleep', sleeps.append)

    assert gateway_auth._load_store() == {}
    waited = len(sleeps)
    assert gateway_auth._load_store() == {}

    assert waited == gateway_auth._READ_ATTEMPTS - 1
    assert len(sleeps) == waited


def test_a_container_gets_the_file_created_first(store):
    """Docker creates a missing bind-mount source as a directory."""
    volume, assignment = gateway_auth.container_store_mount()

    assert volume == f'{os.path.realpath(store)}:/lager/.lager_gateway_auth'
    assert assignment == 'LAGER_GATEWAY_AUTH_FILE=/lager/.lager_gateway_auth'
    assert store.read_text() == '{}'
    assert stat.S_IMODE(store.stat().st_mode) == 0o600


def test_a_container_mounts_the_real_file_behind_a_symlink(store, tmp_path):
    real = tmp_path / 'elsewhere' / 'store.json'
    real.parent.mkdir()
    real.write_text('{}')
    store.symlink_to(real)

    volume, _ = gateway_auth.container_store_mount()

    assert volume == f'{os.path.realpath(real)}:/lager/.lager_gateway_auth'


def test_a_directory_where_the_store_belongs_is_not_mounted(store):
    store.mkdir()
    assert gateway_auth.container_store_mount() is None


def test_a_project_that_mounts_the_store_itself_keeps_its_own(store):
    assert gateway_auth.container_store_mount(
        ['~/.lager_gateway_auth:/lager/.lager_gateway_auth']) is None
    assert gateway_auth.container_store_mount(['/data:/data']) is not None
