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
    classes: new Set(),
    get classList() {
      const set = this.classes;
      return {
        add: (c) => set.add(c), remove: (c) => set.delete(c), contains: (c) => set.has(c),
        toggle: (c, on) => { const want = on === undefined ? !set.has(c) : !!on;
                             if (want) set.add(c); else set.delete(c); return want; },
      };
    },
    getBoundingClientRect() { return { left: 0, top: 0, width: 800, height: 600 }; },
    fire(type, event) { (this.listeners[type] || []).forEach((fn) => fn(event || { preventDefault() {} })); },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return this.attrs[k]; },
    removeAttribute(k) { if (k === 'src') this.src = undefined; delete this.attrs[k]; },
    addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
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
  fullscreenElement: null,
  addEventListener() {},
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

const location = { protocol: 'http:', hostname: '10.0.0.5', href: 'http://10.0.0.5:8086/?x=1#y',
                   assign(url) { this.went = url; } };
const context = vm.createContext({
  document, fetch: fetchStub, window: { location }, localStorage: { setItem() {}, getItem() { return null; } },
  setInterval() {}, setTimeout() {}, clearTimeout() {}, console, Date, Math, String, Number,
  JSON, Promise, parseInt, parseFloat, URL,
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

MODES = {"modes": [{"width": 1920, "height": 1080, "fps": [30, 15]},
                   {"width": 640, "height": 480, "fps": [30]}],
         "current": {"width": 640, "height": 480, "fps": 30}, "error": None}

CONTROLS = {"exposure": {"auto": True, "value": 250, "min": 3, "max": 2047, "step": 1},
            "gain": {"value": 0, "min": 0, "max": 255, "step": 1}}

BASE_ROUTES = {
    "/api/modes": [200, MODES],
    "/api/controls": [200, CONTROLS],
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
    out.went = location.went;
    """
    out = _run(_routes(INFO), scenario)
    assert out["options"] == [["cam1", "8086", True], ["cam2", "8087", False]]
    # Same host, the new port, and nothing carried over from the old address.
    assert out["went"] == "http://10.0.0.5:8087/"


@pytest.mark.parametrize("port", ["80@evil.example", "8087/../x", "javascript:alert(1)", "0", "70000", ""])
def test_the_net_picker_moves_only_to_a_port_on_this_host(port):
    scenario = f"""
    $('net-select').value = {json.dumps(port)};
    $('net-select').fire('change');
    out.went = location.went || null;
    """
    out = _run(_routes(INFO), scenario)
    assert out["went"] is None


def test_zoom_buttons_post_to_the_server():
    scenario = """
    $('zoom-in').fire('click');
    $('zoom-reset').fire('click');
    $('focus-manual').fire('click');
    setTimeout(() => {}, 0);
    out.posts = requests.filter((r) => r.method === 'POST').map((r) => [r.path, r.body]);
    """
    out = _run(_routes(INFO), scenario)
    # Zoom in and reset take no body, so none is sent. Manual focus holds the
    # distance the camera reports (0 here), not a value the page made up.
    assert out["posts"] == [["/api/zoom/in", None], ["/api/zoom/reset", None],
                            ["/api/focus/manual", {"value": 0}]]


def test_snapshot_names_the_file_after_the_net():
    scenario = """
    $('btn-snapshot').fire('click');
    out.download = $('btn-snapshot').download;
    out.href = $('btn-snapshot').href;
    """
    out = _run(_routes(INFO), scenario)
    assert out["download"].startswith("cam1-") and out["download"].endswith(".jpg")
    assert out["href"].startswith("/snapshot?t=")


# ---------- resolution ----------

def test_resolution_lists_the_camera_sizes_and_the_current_rates():
    scenario = """
    out.sizes = $('mode-size').children.map((o) => [o.textContent, o.selected]);
    out.rates = $('mode-fps').children.map((o) => o.textContent);
    """
    out = _run(_routes(INFO), scenario)
    assert out["sizes"] == [["1920×1080", False], ["640×480", True]]
    assert out["rates"] == ["30 fps"]


def test_choosing_a_size_asks_for_its_fastest_rate():
    scenario = """
    $('mode-size').value = '1920x1080';
    $('mode-size').fire('change');
    out.posts = requests.filter((r) => r.method === 'POST').map((r) => [r.path, r.body]);
    out.rates = $('mode-fps').children.map((o) => o.textContent);
    out.note = $('mode-note').textContent;
    """
    out = _run(_routes(INFO), scenario)
    assert out["posts"] == [["/api/mode/set", {"width": 1920, "height": 1080, "fps": 30}]]
    assert out["rates"] == ["30 fps", "15 fps"]
    assert out["note"].startswith("Switching to 1920×1080 at 30 fps")


def test_a_refused_mode_is_shown_under_the_pickers():
    error = "The camera would not stream 1920x1080 at 30 fps, so it stays at 640x480 at 30 fps."
    info = dict(INFO, mode={"width": 640, "height": 480, "fps": 30}, mode_error=error)
    routes = _routes(info, **{"/api/modes": [200, dict(MODES, error=error)]})
    out = _run(routes, "out.note = $('mode-note').hidden ? null : $('mode-note').textContent;")
    assert out["note"].startswith("The camera would not stream 1920x1080")


# ---------- exposure and gain ----------

def test_exposure_and_gain_show_what_the_camera_has():
    scenario = """
    out.exposureShown = !$('exposure-group').hidden;
    out.autoPressed = $('exposure-auto').getAttribute('aria-pressed');
    out.sliderDisabled = $('exposure').disabled;
    out.range = [$('exposure').min, $('exposure').max];
    out.gainShown = !$('gain-group').hidden;
    """
    out = _run(_routes(INFO), scenario)
    assert out["exposureShown"] and out["gainShown"]
    assert out["autoPressed"] == "true" and out["sliderDisabled"] is True
    assert out["range"] == [3, 2047]


def test_a_camera_without_exposure_or_gain_hides_them():
    scenario = """
    out.exposureShown = !$('exposure-group').hidden;
    out.gainShown = !$('gain-group').hidden;
    """
    out = _run(_routes(INFO, **{"/api/controls": [200, {}]}), scenario)
    assert out == {**out, "exposureShown": False, "gainShown": False}


def test_an_exposure_with_no_auto_mode_has_no_switch():
    controls = {"exposure": {"auto": None, "value": 100, "min": 1, "max": 500, "step": 1}}
    scenario = """
    out.switchShown = !$('exposure-mode').hidden;
    out.sliderDisabled = $('exposure').disabled;
    """
    out = _run(_routes(INFO, **{"/api/controls": [200, controls]}), scenario)
    assert out["switchShown"] is False and out["sliderDisabled"] is False


@pytest.mark.parametrize("exposure,held", [
    ({"auto": True, "value": 250, "min": 3, "max": 2047, "step": 1}, True),
    ({"auto": False, "value": 250, "min": 3, "max": 2047, "step": 1}, False),
    ({"auto": None, "value": 250, "min": 3, "max": 2047, "step": 1}, False),
    (None, False),
])
def test_gain_is_held_only_while_auto_exposure_is_on(exposure, held):
    controls = {"gain": {"value": 12, "min": 0, "max": 255, "step": 1}}
    if exposure:
        controls["exposure"] = exposure
    scenario = """
    out.disabled = $('gain').disabled;
    out.shown = $('gain-value').textContent;
    """
    out = _run(_routes(INFO, **{"/api/controls": [200, controls]}), scenario)
    assert (out["disabled"], out["shown"]) == (held, "auto" if held else "12")


def test_manual_exposure_is_one_post():
    scenario = """
    $('exposure-manual').fire('click');
    out.posts = requests.filter((r) => r.method === 'POST').map((r) => [r.path, r.body]);
    """
    out = _run(_routes(INFO), scenario)
    assert out["posts"] == [["/api/controls/set", {"control": "exposure_auto", "value": False}]]


# ---------- pan and orientation ----------

ZOOMED = dict(INFO, zoom=2.0, view={"pan_x": 0.5, "pan_y": 0.5, "rotate": 0,
                                    "flip_h": False, "flip_v": False})


def _posts(path):
    return f"out.posts = requests.filter((r) => r.path === '{path}').map((r) => r.body);"


def test_dragging_a_zoomed_picture_pans_against_the_hand():
    # The stand-in picture is 800x600. At 2x a 200 px drag right moves a
    # quarter of the screen, an eighth of the frame, to the left.
    scenario = """
    $('frame').fire('pointerdown', { button: 0, clientX: 400, clientY: 300, pointerId: 1, preventDefault() {} });
    $('frame').fire('pointermove', { clientX: 600, clientY: 300 });
    $('frame').fire('pointerup', {});
    out.pannable = $('frame').classList.contains('is-pannable');
    """ + _posts('/api/pan/set')
    out = _run(_routes(ZOOMED, **{"/api/pan/set": [200, {"success": True}]}), scenario)
    assert out["pannable"] is True
    assert out["posts"] == [{"x": 0.375, "y": 0.5}]


def test_an_unzoomed_picture_does_not_pan():
    scenario = """
    $('frame').fire('pointerdown', { button: 0, clientX: 400, clientY: 300, pointerId: 1, preventDefault() {} });
    $('frame').fire('pointermove', { clientX: 600, clientY: 300 });
    $('frame').fire('keydown', { key: 'ArrowLeft', preventDefault() {} });
    out.centreDisabled = $('pan-centre').disabled;
    """ + _posts('/api/pan/set')
    out = _run(_routes(INFO, **{"/api/info": [200, dict(INFO, zoom=1.0)]}), scenario)
    assert out["posts"] == []
    assert out["centreDisabled"] is True


def test_arrow_keys_pan_a_tenth_of_the_screen():
    scenario = """
    $('frame').fire('keydown', { key: 'ArrowDown', preventDefault() {} });
    """ + _posts('/api/pan/set')
    out = _run(_routes(ZOOMED, **{"/api/pan/set": [200, {"success": True}]}), scenario)
    assert out["posts"] == [{"x": 0.5, "y": 0.55}]


def test_double_click_zooms_in_on_the_point():
    scenario = """
    $('frame').fire('dblclick', { clientX: 200, clientY: 150 });
    setTimeout(() => {}, 0);
    """
    routes = _routes(dict(INFO, zoom=1.0, view=ZOOMED["view"]),
                     **{"/api/zoom/set": [200, {"success": True, "zoom": 2.0}],
                        "/api/pan/set": [200, {"success": True}]})
    out = _run(routes, scenario + """
    out.paths = requests.filter((r) => r.method === 'POST').map((r) => [r.path, r.body]);
    """)
    # Posted in order once the zoom answers; the harness reads them after.
    assert ["/api/zoom/set", {"zoom": 2.0}] in out["paths"]


def test_rotate_and_flip_post_the_orientation():
    scenario = """
    $('rotate').value = '180';
    $('rotate').fire('change');
    $('flip-h').fire('click');
    """ + _posts('/api/orientation/set')
    out = _run(_routes(ZOOMED), scenario)
    assert out["posts"] == [{"rotate": 180}, {"flip_h": True}]


# ---------- the camera's own ranges ----------

# A global-shutter camera's controls, as /api/controls describes them.
GS_CONTROLS = {
    "exposure": {"auto": False, "value": 312, "min": 1, "max": 10000, "step": 1},
    "gain": {"value": 0, "min": 0, "max": 1023, "step": 1},
    "white_balance_temperature": {"auto": False, "value": 4600, "min": 2800, "max": 6500, "step": 1},
    "contrast": {"value": 32, "min": 0, "max": 95, "step": 1},
    "gamma": {"value": 100, "min": 64, "max": 300, "step": 1},
    "backlight_compensation": {"value": 36, "min": 36, "max": 160, "step": 1},
    "power_line_frequency": {"value": 2, "min": 0, "max": 2, "step": 1,
                             "options": [{"value": 0, "label": "Disabled"},
                                         {"value": 1, "label": "50 Hz"},
                                         {"value": 2, "label": "60 Hz"}]},
}
GS_ROUTES = {
    "/api/controls": [200, GS_CONTROLS],
    "/api/focus": [200, {"mode": "manual", "value": 383, "min": 0, "max": 1023, "step": 1}],
    "/api/brightness": [200, {"value": 0, "min": -64, "max": 64, "step": 1}],
}

ADJUST_REPORT = """
out.adjust = $('image-adjust').children.map((row) => [
  row.children[0].children[0].textContent, row.children[1].min, row.children[1].max,
  row.children[1].value, row.hidden]);
"""


def test_focus_and_brightness_take_the_cameras_range():
    scenario = """
    out.focus = [$('focus').min, $('focus').max, $('focus').value, $('focus').disabled];
    out.focusShown = !$('focus-group').hidden;
    out.brightnessRange = [$('brightness').min, $('brightness').max, $('brightness').value];
    """
    out = _run(_routes(INFO, **GS_ROUTES), scenario)
    assert out["focus"] == [0, 1023, 383, False]
    assert out["focusShown"] is True
    assert out["brightnessRange"] == [-64, 64, 0]


def test_a_camera_without_focus_hides_it():
    scenario = "out.shown = !$('focus-group').hidden;"
    out = _run(_routes(INFO, **{"/api/focus": [200, {"mode": None, "value": None}]}), scenario)
    assert out["shown"] is False


def test_focus_without_autofocus_has_no_switch():
    scenario = "out.switchShown = !$('focus-mode').hidden; out.disabled = $('focus').disabled;"
    focus = {"mode": None, "value": 40, "min": 0, "max": 255, "step": 1}
    out = _run(_routes(INFO, **{"/api/focus": [200, focus]}), scenario)
    assert out == {**out, "switchShown": False, "disabled": False}


def test_image_sliders_are_built_for_what_the_camera_has():
    out = _run(_routes(INFO, **GS_ROUTES), ADJUST_REPORT)
    assert out["adjust"] == [["Contrast", 0, 95, 32, False],
                             ["Gamma", 64, 300, 100, False],
                             ["Backlight compensation", 36, 160, 36, False]]


def test_an_image_slider_shows_its_value_as_it_moves():
    # The POST waits on the slider debounce, a setTimeout the harness never
    # runs; what shows at once is the readout, and the debounced listener.
    scenario = """
    const row = $('image-adjust').children[1];
    const gamma = row.children[1];
    gamma.value = '120';
    gamma.fire('input');
    out.readout = row.children[0].children[1].textContent;
    out.listeners = gamma.listeners.input.length;
    out.label = gamma.getAttribute('aria-label');
    """
    out = _run(_routes(INFO, **GS_ROUTES), scenario)
    assert out["readout"] == "120"
    assert out["listeners"] == 2
    assert out["label"] == "Gamma"


def test_white_balance_is_shown_and_held_under_auto():
    scenario = """
    out.shown = !$('wb-group').hidden;
    out.range = [$('wb').min, $('wb').max, $('wb').value, $('wb').disabled];
    out.label = $('wb-value').textContent;
    """
    out = _run(_routes(INFO, **GS_ROUTES), scenario)
    assert out["shown"] is True
    assert out["range"] == [2800, 6500, 4600, False]
    assert out["label"] == "4600 K"
    auto = dict(GS_CONTROLS, white_balance_temperature=dict(
        GS_CONTROLS["white_balance_temperature"], auto=True))
    out = _run(_routes(INFO, **dict(GS_ROUTES, **{"/api/controls": [200, auto]})), scenario)
    assert out["range"][3] is True and out["label"] == "auto"


def test_auto_white_balance_buttons_post_the_switch():
    scenario = """
    $('wb-auto').fire('click');
    $('wb-manual').fire('click');
    """ + _posts('/api/controls/set')
    out = _run(_routes(INFO, **GS_ROUTES), scenario)
    assert out["posts"] == [{"control": "white_balance_auto", "value": True},
                            {"control": "white_balance_auto", "value": False}]


def test_power_line_frequency_lists_the_cameras_options_and_posts():
    scenario = """
    out.options = $('power-line').children.map((o) => [o.value, o.textContent]);
    out.value = $('power-line').value;
    $('power-line').value = '1';
    $('power-line').fire('change');
    """ + _posts('/api/controls/set')
    out = _run(_routes(INFO, **GS_ROUTES), scenario)
    assert out["options"] == [["0", "Disabled"], ["1", "50 Hz"], ["2", "60 Hz"]]
    assert out["value"] == "2"
    assert out["posts"] == [{"control": "power_line_frequency", "value": 1}]


def test_a_camera_with_only_exposure_shows_no_extra_controls():
    scenario = ADJUST_REPORT + """
    out.wb = !$('wb-group').hidden;
    out.powerLine = !$('power-line-group').hidden;
    """
    out = _run(_routes(INFO), scenario)
    assert out["adjust"] == [] and out["wb"] is False and out["powerLine"] is False
