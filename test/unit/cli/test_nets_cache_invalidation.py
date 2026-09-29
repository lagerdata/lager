# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
A `lager nets` change takes effect on the next `lager debug` command (#604).

`lager debug` caches each resolved debug net for five minutes in
`~/.lager_cache/debug_net_cache.json`, and `flash` / `erase` send the cached
`jlink_script` in their request bodies, which the box prefers over the script
saved on the net. `nets set-script` changed the box's record and left the
entry alone, so for the rest of the TTL every erase and flash ran the OLD
script: a `LAGER_ERASE_RANGE` line the box reported as `source: default`
until the cache file was deleted.

Every write to a net record now drops the box's cached debug nets: all of
them, because an entry cached without a net name holds the box's first debug
net. The box is the in-memory :9000 fake; the cache is the real
`DebugNetCache` on a temp file.
"""

import base64
import importlib
import os
import sys
from unittest.mock import patch

import pytest
from click.testing import CliRunner

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

nets_mod = importlib.import_module("cli.commands.box.nets")
net_tui = importlib.import_module("cli.commands.box.net_tui")
cache_mod = importlib.import_module("cli.commands.development.debug.net_cache")
from cli.commands.box.nets import nets as nets_group  # noqa: E402
from test.unit.cli.nets_http_fake import FakeBoxHTTP  # noqa: E402

BOX = "192.0.2.7"
OTHER_BOX = "192.0.2.8"
OLD_SCRIPT = base64.b64encode(b"void InitTarget(void) {}\n").decode("ascii")
NET = {
    "name": "debug1",
    "role": "debug",
    "instrument": "J-Link",
    "channel": "nRF5340_xxAA_APP",
    "address": "USB0::0x1366::0x0101::000012345678::INSTR",
    "jlink_script": OLD_SCRIPT,
}


@pytest.fixture
def cache(tmp_path, monkeypatch):
    """The real cache on a temp file, holding stale entries for BOX and one
    for another box that must survive."""
    class TempCache(cache_mod.DebugNetCache):
        CACHE_FILE = tmp_path / "debug_net_cache.json"

    c = TempCache()
    monkeypatch.setattr(cache_mod, "_net_cache", c)
    c.set(BOX, "debug1", dict(NET))
    c.set(BOX, None, dict(NET))          # `lager debug` with no net name
    c.set(OTHER_BOX, "debug1", dict(NET))
    return c


@pytest.fixture
def box():
    fake = FakeBoxHTTP()
    fake.saved_nets.append(dict(NET))
    with patch("requests.request", fake.request), \
            patch.object(nets_mod, "_resolve_box", lambda _ctx, name=None: BOX):
        yield fake


def _run(args):
    return CliRunner().invoke(nets_group, args + ["--box", "bench"],
                              catch_exceptions=False)


def _forgotten(cache):
    """True when BOX's entries are gone and OTHER_BOX's is untouched."""
    return (cache.get(BOX, "debug1") is None
            and cache.get(BOX, None) is None
            and cache.get(OTHER_BOX, "debug1") is not None)


class TestNetsCommandsForgetTheBoxsCachedDebugNets:

    def test_set_script(self, box, cache, tmp_path):
        script = tmp_path / "new.JLinkScript"
        script.write_text("void InitTarget(void) { /* LAGER_ERASE_RANGE: 0x0 0xFFF */ }\n")
        result = _run(["set-script", "debug1", str(script)])
        assert result.exit_code == 0, result.output
        assert box.saved_nets[0]["jlink_script"] != OLD_SCRIPT
        assert _forgotten(cache)

    def test_remove_script(self, box, cache):
        result = _run(["remove-script", "debug1"])
        assert result.exit_code == 0, result.output
        assert "jlink_script" not in box.saved_nets[0]
        assert _forgotten(cache)

    def test_rename(self, box, cache):
        result = _run(["rename", "debug1", "debug2"])
        assert result.exit_code == 0, result.output
        assert _forgotten(cache)

    def test_delete(self, box, cache):
        result = _run(["delete", "debug1", "debug", "--yes"])
        assert result.exit_code == 0, result.output
        assert box.saved_nets == []
        assert _forgotten(cache)

    def test_delete_all(self, box, cache):
        result = _run(["delete-all", "--yes"])
        assert result.exit_code == 0, result.output
        assert _forgotten(cache)

    def test_a_failed_write_keeps_the_cache(self, box, cache):
        """Nothing changed on the box, so nothing is dropped."""
        result = _run(["delete", "no-such-net", "debug", "--yes"])
        assert result.exit_code != 0
        assert cache.get(BOX, "debug1") is not None


class TestTheNetManagerForgetsToo:

    def test_tui_save_drops_the_boxs_entries(self, cache):
        fake = FakeBoxHTTP()
        with patch("requests.request", fake.request), \
                patch.object(net_tui, "auth_headers_for_box", lambda _b: {}, create=True):
            net_tui._save_net_http(BOX, dict(NET))
        assert _forgotten(cache)


class TestForgettingIsBestEffort:

    def test_a_broken_cache_never_fails_a_nets_command(self, box, monkeypatch):
        class Broken:
            def clear(self, box=None):
                raise OSError("read-only file system")

        monkeypatch.setattr(cache_mod, "get_net_cache", lambda: Broken())
        result = _run(["remove-script", "debug1"])
        assert result.exit_code == 0, result.output
