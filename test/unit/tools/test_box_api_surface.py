# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
The release-time lager-rs check must see every kind of API addition.

`tools/box_api_surface.py` reads the box HTTP API out of the source, and the
`Release: lager-rs API check` workflow diffs it between releases to tell the
Rust crate what it has not picked up. A kind of addition it cannot see is one
the crate falls behind on silently, which is how it ended sixteen releases
behind. So each way the box registers a route, an action or a feature has a
test here, and a test against this checkout fails if a refactor of the box
moves something the extractor no longer finds.
"""

import importlib.util
import json
import pathlib
import textwrap

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
TOOL = REPO_ROOT / "tools" / "box_api_surface.py"

_spec = importlib.util.spec_from_file_location("box_api_surface", TOOL)
bas = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bas)


def write(root, rel, source):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source), encoding="utf-8")


@pytest.fixture
def box(tmp_path):
    """A small box tree using each registration form the real one uses."""
    write(tmp_path, "box/lager/http_handlers/net_command.py", '''
        _HSM_ACTIONS = ("info", "sign")
        _MEASUREMENTS = {"measure_vpp": ("vpp", "V")}

        def _adc(netname, role, action, params):
            if action != "read":
                raise UnknownAction(action)

        def _hsm(netname, role, action, params):
            if action not in _HSM_ACTIONS:
                raise UnknownAction(action)

        def _measure(dev, action):
            return _MEASUREMENTS[action]

        def _scope(netname, role, action, params):
            if action == "enable_net":
                return
            if action in ("set_scale", "get_scale"):
                return
            return _measure(None, action)

        ROLE_ACTIONS = {
            "adc": _adc,
            "hsm": _hsm,
            "scope": _scope,
        }

        def register(app):
            @app.route('/net/command', methods=['POST'])
            def net_command_http():
                pass
    ''')
    write(tmp_path, "box/lager/http_handlers/supply.py", '''
        _EXTRA = frozenset({"clear_ocp"})

        def register(app, socketio):
            @app.route('/supply/command', methods=['POST'])
            def supply_command():
                if action == 'voltage':
                    pass
                elif action in _EXTRA:
                    pass

            @app.route('/nets/<name>', methods=['GET', 'DELETE'])
            def net(name):
                pass

            @app.route('/health')
            def health():
                pass

            @socketio.on('start_supply_monitor', namespace='/supply')
            def start():
                pass
    ''')
    write(tmp_path, "box/lager/debug/service.py", '''
        _LOCKED = {
            '/debug/connect': 'connect',
            '/debug/halt': 'halt',
        }
        SERVICE_FEATURES = ('erase_range', 'halt')

        class Handler:
            def do_GET(self):
                if self.path == '/health':
                    pass

            def do_POST(self):
                operation = _LOCKED.get(self.path)
                if self.path == '/debug/info':
                    pass
    ''')
    write(tmp_path, "box/lager/box_http_server.py", '''
        def status():
            return jsonify({
                'healthy': True,
                'capabilities': {'netCommand': True, 'customDrivers': False},
            })
    ''')
    write(tmp_path, "box/lager/tests/test_ignored.py", '''
        def register(app):
            @app.route('/only-in-a-test', methods=['GET'])
            def f():
                pass
    ''')
    return tmp_path


def test_extract_reads_every_registration_form(box):
    s = bas.extract(box)
    assert s["routes"] == [
        "DELETE /nets/<name>", "GET /health", "GET /nets/<name>",
        "POST /net/command", "POST /supply/command",
    ]
    assert s["route_actions"] == {"POST /supply/command": ["clear_ocp", "voltage"]}
    assert s["net_command"] == {
        "adc": ["read"],
        "hsm": ["info", "sign"],
        "scope": ["enable_net", "get_scale", "measure_vpp", "set_scale"],
    }
    assert s["socketio"] == {"/supply": ["start_supply_monitor"]}
    assert s["debug_service"] == {
        "routes": ["GET /health", "POST /debug/connect", "POST /debug/halt",
                   "POST /debug/info"],
        "features": ["erase_range", "halt"],
    }
    assert s["capabilities"] == ["customDrivers", "netCommand"]


def test_extract_finds_the_known_surface_of_this_checkout():
    """Fails when a refactor moves part of the API out of the extractor's
    sight, before a release goes out with the crate check blind to it."""
    s = bas.extract(REPO_ROOT)
    assert "POST /net/command" in s["routes"]
    assert "GET /nets/state" in s["routes"]
    assert {"voltage", "current", "enable"} <= set(s["route_actions"]["POST /supply/command"])
    roles = s["net_command"]
    for role in ("gpio", "adc", "scope", "scope-channel", "hsm", "webcam", "arm"):
        assert roles.get(role), f"no actions found for role {role}"
    assert {"read", "set"} <= set(roles["dac"])
    assert "measure_vpp" in roles["scope"]
    assert {"/uart", "/rtt"} <= set(s["socketio"])
    debug = s["debug_service"]
    assert {"POST /debug/flash", "POST /debug/erase", "GET /health"} <= set(debug["routes"])
    assert "erase_range" in debug["features"]
    assert {"netCommand", "netCommandRoles", "customDrivers"} <= set(s["capabilities"])


def test_diff_is_none_when_nothing_changed(box):
    s = bas.extract(box)
    assert bas.diff(s, s, "v1.0.0", "v1.1.0") is None


def test_diff_reports_each_kind_of_change(box):
    old = bas.extract(box)
    new = json.loads(json.dumps(old))
    new["routes"].append("POST /drivers/<dtype>/deps")
    new["routes"].remove("GET /health")
    new["route_actions"]["POST /supply/command"].append("set_mode")
    new["net_command"]["arm"] = ["get_acceleration", "move_to"]
    new["net_command"]["hsm"].remove("sign")
    new["socketio"]["/rtt"] = ["rtt_write"]
    new["debug_service"]["features"].append("trace")
    new["capabilities"].append("customDriverDeps")

    report = bas.diff(old, new, "v0.59.0", "v0.60.0")

    assert report.startswith("lager v0.60.0 changed the box HTTP API since v0.59.0.")
    assert "- Added: `POST /drivers/<dtype>/deps`" in report
    assert "- Removed: `GET /health`" in report
    assert "- `POST /supply/command`: added `set_mode`" in report
    assert "- New role `arm`: `get_acceleration`, `move_to`" in report
    assert "- `hsm`: removed `sign`" in report
    assert "- New namespace `/rtt`: `rtt_write`" in report
    assert "- Added: `trace`" in report
    assert "- Added: `customDriverDeps`" in report
    # The report says what it cannot see, so silence is not read as "done".
    assert "response shape" in report


def test_cli_writes_a_report_only_when_the_surface_changed(box, tmp_path, capsys):
    old_json = tmp_path / "old.json"
    new_json = tmp_path / "new.json"
    report = tmp_path / "report.md"
    assert bas.main(["extract", str(box), "-o", str(old_json)]) == 0
    assert bas.main(["extract", str(box), "-o", str(new_json)]) == 0
    capsys.readouterr()

    assert bas.main(["diff", str(old_json), str(new_json), "--old-label", "a",
                     "--new-label", "b", "-o", str(report)]) == 0
    assert capsys.readouterr().out.strip() == "unchanged"
    assert not report.exists()

    surface = json.loads(new_json.read_text())
    surface["capabilities"].append("trace")
    new_json.write_text(json.dumps(surface))
    assert bas.main(["diff", str(old_json), str(new_json), "--old-label", "a",
                     "--new-label", "b", "-o", str(report)]) == 0
    assert capsys.readouterr().out.strip() == "changed"
    assert "`trace`" in report.read_text()


def test_cli_refuses_a_directory_that_is_not_a_lager_checkout(tmp_path):
    with pytest.raises(SystemExit):
        bas.main(["extract", str(tmp_path)])
