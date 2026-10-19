# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
The web scope's Screenshot button (box/lager/static/scope/scope.js).

It saves the waveform as the page draws it, with the settings needed to read
it in a line underneath, as a PNG named for the net and the time.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
SCOPE_DIR = REPO_ROOT / "box" / "lager" / "static" / "scope"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="node is required to run the scope UI code")

PRELUDE = """
import { ScopeApp, screenshotName, screenshotCaption } from %s;
const when = new Date(2026, 9, 19, 12, 54, 42);

// A browser's canvas, link and DOM, recording what is done with them.
const made = [];
const ops = [];
function fakeCanvas(width = 0, height = 0) {
  const canvas = {
    width, height,
    getContext() {
      return {
        set fillStyle(v) { ops.push(['fillStyle', v]); },
        set font(v) { ops.push(['font', v]); },
        set textBaseline(v) {},
        fillRect(...a) { ops.push(['fillRect', ...a]); },
        drawImage(image, ...a) { ops.push(['drawImage', image.id, ...a]); },
        fillText(text, ...a) { ops.push(['fillText', text, ...a]); },
      };
    },
    toBlob(done, type) { ops.push(['toBlob', type]); done({ type }); },
  };
  return canvas;
}
const elements = {
  model: { textContent: ' MSO5074 ' },
  timebase: { selectedOptions: [{ text: '1.00 ms/div' }] },
  'trigger-source': { selectedOptions: [{ text: 'A' }] },
  'trigger-slope': { selectedOptions: [{ text: 'Rising' }] },
  'trigger-mode': { selectedOptions: [{ text: 'Auto' }] },
  'trigger-level': { value: '1.5' },
};
globalThis.window = { devicePixelRatio: 2 };
globalThis.document = {
  documentElement: {},
  getElementById: (id) => elements[id] || null,
  createElement(tag) {
    const node = tag === 'canvas' ? fakeCanvas()
      : { click() { ops.push(['click', this.download, this.href]); }, remove() {} };
    made.push(tag);
    return node;
  },
  body: { appendChild() {} },
};
globalThis.getComputedStyle = (node) => node === document.documentElement
  ? { getPropertyValue: (name) => ({ '--bg-panel': ' #161b22', '--text': '#e6edf3' })[name] || '' }
  : { backgroundColor: 'rgb(5, 8, 13)' };
globalThis.URL = { createObjectURL: () => 'blob:shot', revokeObjectURL() {} };
globalThis.setTimeout = () => 0;

function app(extra = {}) {
  const lines = [];
  const source = Object.assign(fakeCanvas(800, 600), { id: 'scope-canvas', parentElement: {} });
  return Object.assign(Object.create(ScopeApp.prototype), {
    net: 'rigol1',
    canvas: source,
    channelState: new Map([
      ['A', { enabled: true, voltsPerDiv: 1 }],
      ['B', { enabled: false, voltsPerDiv: 0.1 }],
      ['C', { enabled: true, voltsPerDiv: 0.005 }],
    ]),
    console: { lines, write(text, kind) { lines.push([kind, text]); },
               error(text) { lines.push(['error', text]); } },
  }, extra);
}
""" % json.dumps(str(SCOPE_DIR / "scope.js"))


def _run_js(body):
    result = subprocess.run(
        ["node", "--input-type=module", "-e", PRELUDE + body],
        capture_output=True, text=True, timeout=60, check=False, env=dict(os.environ))
    if result.returncode != 0:
        pytest.fail("node failed: %s" % result.stderr.strip())
    return json.loads(result.stdout)


def _out(expr):
    return _run_js("process.stdout.write(JSON.stringify(%s));" % expr)


class TestTheFileName:

    def test_is_the_net_and_the_local_time(self):
        assert _out("screenshotName('rigol1', when)") == "scope-rigol1-20261019-125442.png"

    def test_a_net_name_cannot_make_a_path(self):
        assert _out("screenshotName('../a b', when)") == "scope-.._a_b-20261019-125442.png"

    def test_no_net_is_still_a_name(self):
        assert _out("screenshotName(null, when)") == "scope-scope-20261019-125442.png"


class TestTheCaption:

    def test_reads_like_the_scope_s_own_screen(self):
        caption = _out("screenshotCaption({model: 'MSO5074', net: 'rigol1', "
                       "timebase: '1.00 ms/div', channels: [{label: 'A', scale: '1.00 V/div'}], "
                       "trigger: {source: 'A', slope: 'Rising', level: '1.50 V', mode: 'Auto'}, "
                       "when})")
        assert caption.startswith(
            "MSO5074 rigol1   1.00 ms/div   A 1.00 V/div   Trig A Rising 1.50 V Auto   ")

    def test_takes_the_settings_from_the_sidebar_and_the_shown_channels(self):
        settings = _out("app().screenshotSettings(when)")
        assert settings["model"] == "MSO5074"
        assert settings["timebase"] == "1.00 ms/div"
        assert settings["channels"] == [
            {"label": "A", "scale": "1.00 V/div"},
            {"label": "C", "scale": "5.00 mV/div"},
        ], "B is off"
        assert settings["trigger"] == {
            "source": "A", "slope": "Rising", "level": "1.50 V", "mode": "Auto"}


class TestSaving:

    def test_with_nothing_drawn_says_so_and_saves_nothing(self):
        result = _run_js("""
            const a = app();
            a.saveScreenshot();
            process.stdout.write(JSON.stringify({lines: a.console.lines, made}));
        """)
        assert result["made"] == []
        assert result["lines"] == [["error", "Nothing to save yet: connect, then start."]]

    def test_saves_the_plot_on_its_colour_with_the_caption_under_it(self):
        result = _run_js("""
            const a = app({latest: {}});
            a.saveScreenshot();
            process.stdout.write(JSON.stringify({lines: a.console.lines, ops}));
        """)
        ops = result["ops"]
        # The plot's colour under the transparent canvas, the canvas, then a
        # 24 px (48 device px at 2x) panel with the settings in it.
        assert ops[0] == ["fillStyle", "rgb(5, 8, 13)"]
        assert ops[1] == ["fillRect", 0, 0, 800, 600]
        assert ops[2] == ["drawImage", "scope-canvas", 0, 0]
        assert ops[3] == ["fillStyle", "#161b22"]
        assert ops[4] == ["fillRect", 0, 600, 800, 48]
        text = next(op for op in ops if op[0] == "fillText")
        assert text[1].startswith("MSO5074 rigol1   1.00 ms/div   A 1.00 V/div   C 5.00 mV/div   ")
        assert ["toBlob", "image/png"] in ops
        click = next(op for op in ops if op[0] == "click")
        assert click[1].startswith("scope-rigol1-") and click[1].endswith(".png")
        assert click[2] == "blob:shot"
        assert result["lines"][-1] == ["note", "Saved %s" % click[1]]
