# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
The webcam viewer page (box/lager/static/webcam/webcam.js), run under node
against a stand-in DOM and fetch.

What only this covers: the status the page shows from /api/info (LIVE,
STALLED, WAITING, OFFLINE), the signed-out state a gateway's 401 puts it in,
the Net picker built from /api/streams, and that the net's name is shown as
text, never parsed as HTML.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
WEBCAM_JS = REPO_ROOT / "box" / "lager" / "static" / "webcam" / "webcam.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="node is required to run the webcam UI code")

# A DOM just large enough for webcam.js: elements by id, with the properties
# and methods the page touches. `routes` maps a request path to
# [status, json body]; `scenario` runs after the page's first round of
# requests has settled and prints what the test asserts on.
HARNESS = r"""
const vm = require('vm');
const fs = require('fs');
const [jsPath, routesJson, scenario] = process.argv.slice(1);
const routes = JSON.parse(routesJson);
const requests = [];

function element(id) {
  const el = {
    id, textContent: '', hidden: false, className: '', value: '', disabled: false,
    title: '', href: '', download: '', dataset: {}, attrs: {}, children: [], listeners: {},
    src: undefined,
    style: { setProperty(k, v) { this[k] = v; } },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return this.attrs[k]; },
    removeAttribute(k) { if (k === 'src') this.src = undefined; delete this.attrs[k]; },
    addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
    fire(type) { (this.listeners[type] || []).forEach((fn) => fn()); },
    append(part) { this.children.push(part); },
    appendChild(child) { this.children.push(child); return child; },
    get text() {
      return this.children.length
        ? this.children.map((c) => (typeof c === 'string' ? c : c.textContent)).join('')
        : this.textContent;
    },
  };
  Object.defineProperty(el, 'textContent', {
    get() { return this._text || ''; },
    set(v) { this._text = String(v); this.children = []; },
  });
  return el;
}

const elements = {};
const document = {
  title: '',
  getElementById(id) { return elements[id] || (elements[id] = element(id)); },
  createElement(tag) { const el = element(''); el.tag = tag; return el; },
  documentElement: { classList: { toggle() { return true; } } },
};
document.getElementById('focus').value = '128';
document.getElementById('brightness').value = '128';
document.getElementById('zoom').value = '1';

function fetchStub(path, options) {
  requests.push({ path, method: (options && options.method) || 'GET',
                  body: options && options.body ? JSON.parse(options.body) : null });
  const key = path.split('?')[0];
  const route = routes[key];
  if (!route) return Promise.reject(new Error('network down'));
  const [status, body] = route;
  return Promise.resolve({ status, ok: status >= 200 && status < 300,
                           json: () => Promise.resolve(body) });
}

const location = { protocol: 'http:', hostname: '10.0.0.5', href: 'http://10.0.0.5:8086/' };
const context = vm.createContext({
  document, fetch: fetchStub, window: { location }, localStorage: { setItem() {}, getItem() { return null; } },
  setInterval() {}, setTimeout() {}, clearTimeout() {}, console, Date, Math, String, Number,
  JSON, Promise, parseInt, parseFloat,
});
vm.runInContext(fs.readFileSync(jsPath, 'utf8'), context);

const $ = (id) => document.getElementById(id);
setTimeout(() => {
  const out = {};
  new Function('$', 'out', 'requests', 'location', scenario)($, out, requests, location);
  setTimeout(() => process.stdout.write(JSON.stringify(out)), 20);
}, 20);
"""

INFO = {"net": "cam1", "video_device": "/dev/video0", "model": "Logitech BRIO HD",
        "port": 8086, "source": "cli", "started_by": "alice", "fps": 29.8,
        "zoom": 2.0, "width": 640, "height": 480, "frame_age": 0.1}

BASE_ROUTES = {
    "/api/focus": [200, {"mode": "auto", "value": 0}],
    "/api/brightness": [200, {"value": 140}],
    "/api/streams": [200, {"streams": [{"name": "cam1", "port": 8086},
                                       {"name": "cam2", "port": 8087}],
                           "current": "cam1", "box_ip": "10.0.0.5"}],
}

REPORT = """
out.live = $('live').textContent;
out.liveClass = $('live').className;
out.empty = $('feed-empty').hidden ? null : $('feed-empty-title').textContent;
out.hint = $('feed-empty-hint').text;
out.hintParts = $('feed-empty-hint').children.map((c) => typeof c === 'string' ? 'text' : c.tag);
out.src = $('feed').src || null;
out.model = $('model').textContent;
out.stats = [$('stat-fps').textContent, $('stat-size').textContent, $('stat-zoom').textContent];
out.started = $('about-started').textContent;
out.brightness = $('brightness').value;
out.focusDisabled = $('focus').disabled;
out.options = $('net-select').children.map((o) => [o.textContent, o.value, o.selected]);
out.ratio = $('frame').style['--ratio'] || null;
"""


def _run(routes, scenario=""):
    result = subprocess.run(
        ["node", "-e", HARNESS, str(WEBCAM_JS), json.dumps(routes), REPORT + scenario],
        capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _routes(info=None, **extra):
    routes = dict(BASE_ROUTES)
    if info is not None:
        routes["/api/info"] = [200, info]
    routes.update(extra)
    return routes


def test_a_stream_with_fresh_frames_is_live():
    out = _run(_routes(INFO))
    assert out["live"] == "LIVE" and out["liveClass"] == "live live--on"
    assert out["empty"] is None
    assert out["src"].startswith("/stream")
    assert out["model"] == "Logitech BRIO HD"
    assert out["stats"] == ["29.8 fps", "640×480", "2.0×"]
    assert out["started"] == "cli · alice"
    assert out["brightness"] == 140
    assert out["focusDisabled"] is True
    # The picture is sized to the camera's own aspect ratio.
    assert out["ratio"] == "640 / 480"


def test_old_frames_mean_stalled():
    out = _run(_routes(dict(INFO, frame_age=12.4)))
    assert out["live"] == "STALLED"
    assert out["empty"] == "The camera stopped sending frames."
    assert "12 s ago" in out["hint"]


def test_no_frame_yet_is_waiting():
    out = _run(_routes(dict(INFO, frame_age=None, width=None, height=None, fps=0)))
    assert out["live"] == "WAITING"
    assert out["stats"][:2] == ["— fps", "—"]


def test_a_server_that_does_not_answer_is_offline():
    out = _run(_routes(None))
    assert out["live"] == "OFFLINE"
    assert out["empty"] == "The stream does not answer."


def test_a_401_signs_the_viewer_out_and_stops_the_feed():
    # A gateway whose browser session ended answers every request 401. The
    # page says how to get a new link and stops asking for frames.
    out = _run(_routes(None, **{"/api/info": [401, {}]}))
    assert out["live"] == "SIGNED OUT"
    assert out["empty"] == "This viewer’s sign-in has ended."
    assert out["hint"] == "Run lager webcam url for a new link."
    assert out["src"] is None


def test_nothing_from_the_server_is_parsed_as_html():
    # The net's name, the camera's model and who started the stream are
    # whatever someone typed. The page only ever sets text, so none of them
    # can inject markup. A stand-in DOM cannot show that, so check the source
    # for the HTML sinks instead.
    source = WEBCAM_JS.read_text()
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert sink not in source, sink
    hostile = "<img src=x onerror=alert(1)>"
    out = _run(_routes(dict(INFO, net=hostile, model=None)))
    assert out["model"] == hostile


def test_the_offline_hint_is_a_command_in_a_code_node():
    out = _run(_routes(None))
    assert out["hintParts"] == ["text", "code", "text"]
    assert out["hint"] == "It may have been stopped. Start it with lager webcam NET start."


def test_the_net_picker_lists_every_stream_and_moves_to_its_port():
    scenario = """
    $('net-select').value = '8087';
    $('net-select').fire('change');
    out.went = location.href;
    """
    out = _run(_routes(INFO), scenario)
    assert out["options"] == [["cam1", "8086", True], ["cam2", "8087", False]]
    assert out["went"] == "http://10.0.0.5:8087/"


def test_zoom_buttons_post_to_the_server():
    scenario = """
    $('zoom-in').fire('click');
    $('zoom-reset').fire('click');
    $('focus-manual').fire('click');
    setTimeout(() => {}, 0);
    out.posts = requests.filter((r) => r.method === 'POST').map((r) => [r.path, r.body]);
    """
    out = _run(_routes(INFO), scenario)
    assert out["posts"] == [["/api/zoom/in", {}], ["/api/zoom/reset", {}],
                            ["/api/focus/manual", {"value": 128}]]


def test_snapshot_names_the_file_after_the_net():
    scenario = """
    $('btn-snapshot').fire('click');
    out.download = $('btn-snapshot').download;
    out.href = $('btn-snapshot').href;
    """
    out = _run(_routes(INFO), scenario)
    assert out["download"].startswith("cam1-") and out["download"].endswith(".jpg")
    assert out["href"].startswith("/snapshot?t=")
