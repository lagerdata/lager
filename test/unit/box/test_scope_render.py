# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
The arithmetic the web scope draws with (box/lager/static/scope/render.js).

Run under node, since executing the module is the only way to know what it
computes. These are the functions a smooth display rests on: how many frames
may be in flight, where a rolling screen is drawn from, what each pixel
column shows, and how a persistence layer fades -- each a way the trace once
stuttered, froze or smeared.
"""
from __future__ import annotations

import json
import math
import os
import pathlib
import shutil
import subprocess

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
RENDER_JS = REPO_ROOT / "box" / "lager" / "static" / "scope" / "render.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="node is required to run the scope UI code")


def _js(expression):
    """Evaluate `expression` with render.js imported as `r`; return its JSON."""
    script = "import * as r from %s;\nprocess.stdout.write(JSON.stringify(%s));" % (
        json.dumps(str(RENDER_JS)), expression)
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=60, check=False, env=dict(os.environ))
    if result.returncode != 0:
        pytest.fail("node failed: %s" % result.stderr.strip())
    return json.loads(result.stdout)


class TestCredits:

    def test_a_lan_gets_the_floor(self):
        assert _js("r.creditWindow(5, 60)") == _js("r.CREDIT_WINDOW")

    def test_a_long_round_trip_gets_enough_to_cover_it(self):
        # 150 ms at 60 Hz is nine frames in flight and one waiting, plus two.
        assert _js("r.creditWindow(150, 60)") == 12

    def test_the_window_is_capped(self):
        assert _js("r.creditWindow(5000, 60)") == 16

    def test_nonsense_measurements_do_not_break_it(self):
        assert _js("r.creditWindow(-10, 0)") == _js("r.CREDIT_WINDOW")


class TestRollWindow:

    def test_a_frame_just_due_is_drawn_as_it_came(self):
        window = _js("r.rollWindow(4000, 1, 1000, 1000 + r.ROLL_DELAY_MS)")
        assert window == {"start": 0, "end": 4000}

    def test_it_scrolls_with_the_time_since_the_frame(self):
        # 10 ms later at 1 ms a pair is 10 pairs on, 20 samples.
        window = _js("r.rollWindow(4000, 1, 1000, 1010 + r.ROLL_DELAY_MS)")
        assert window == {"start": 20, "end": 4020}

    def test_clocks_that_disagree_by_an_hour_are_clamped(self):
        window = _js("r.rollWindow(4000, 1, 0, 3600e3)")
        assert window == {"start": 2000, "end": 6000}


class TestColumnExtremes:

    def _columns(self, counts, columns, envelope=False, start=0, end=None):
        end = len(counts) if end is None else end
        return _js(
            "(() => { const out = {min: new Array(%d), max: new Array(%d)};"
            " const n = r.columnExtremes(out, Int16Array.from(%s), %d, %d, %d, %s);"
            " return {n, min: out.min.map(v => Number.isNaN(v) ? null : v),"
            " max: out.max.map(v => Number.isNaN(v) ? null : v)}; })()"
            % (columns, columns, json.dumps(counts), start, end, columns,
               "true" if envelope else "false"))

    def test_each_column_holds_the_lowest_and_highest_under_it(self):
        result = self._columns([1, 5, -3, 2, 7, 0, 4, 4], 2)
        assert result["n"] == 2
        assert result["min"][0] == -3 and result["max"][0] == 7
        assert result["min"][1] == 0 and result["max"][1] == 7

    def test_a_column_with_nothing_captured_is_empty(self):
        # Each column also takes the first sample of the next, so neighbours
        # join; the first here is clear of captured samples even so.
        missing = -32768
        result = self._columns([missing] * 6 + [1, 2, 3], 3)
        assert result["min"][0] is None and result["max"][0] is None
        assert result["min"][2] == 1 and result["max"][2] == 3

    def test_an_envelope_pair_is_never_split(self):
        # Pairs (-5, 5), (-1, 1), (-2, 2); three pairs over two columns would
        # split the middle one if columns ignored the pairing.
        result = self._columns([-5, 5, -1, 1, -2, 2], 2, envelope=True)
        for low, high in zip(result["min"], result["max"]):
            assert low == -high, result

    def test_an_empty_span_writes_nothing(self):
        assert self._columns([1, 2, 3], 2, start=2, end=2)["n"] == 0


class TestZoom:

    def test_no_zoom_is_the_whole_record(self):
        assert _js("r.zoomWindow(1000, 500, 1e-6, null)") == {"start": 0, "end": 1000}
        assert _js("r.zoomWindow(1000, 500, 1e-6, {factor: 1})") == {"start": 0, "end": 1000}

    def test_a_zoom_centres_on_its_time(self):
        # 4x around 100 us after a mid-record trigger at 1 us a sample.
        window = _js("r.zoomWindow(1000, 500, 1e-6, {factor: 4, center: 1e-4})")
        assert window == {"start": 475, "end": 725}

    def test_a_zoom_near_an_edge_stays_inside_the_record(self):
        window = _js("r.zoomWindow(1000, 500, 1e-6, {factor: 4, center: 1})")
        assert window == {"start": 750, "end": 1000}


class TestMath:

    def test_an_expression_names_two_channels_and_an_operator(self):
        assert _js("r.parseMath(' a - b ')") == {"left": "A", "op": "-", "right": "B"}
        assert _js("r.parseMath('B*A')") == {"left": "B", "op": "*", "right": "A"}

    @pytest.mark.parametrize("expr", ["a-a", "a/b", "a", "", "ab-c"])
    def test_anything_else_is_no_math(self, expr):
        assert _js("r.parseMath(%s)" % json.dumps(expr)) is None

    def test_combine(self):
        assert _js("[r.combine('+', 3, 2), r.combine('-', 3, 2), r.combine('*', 3, 2)]") == [5, 1, 6]


class TestPersistence:

    def test_infinite_never_fades(self):
        assert _js("r.persistenceFade('infinite', 1e6)") == 0

    def test_no_time_passed_is_no_fade(self):
        # Two draws in one millisecond used to clear the whole layer.
        assert _js("r.persistenceFade(2, 0)") == 0

    def test_a_trace_is_nearly_gone_after_the_persistence_time(self):
        assert _js("r.persistenceFade(3, 3000)") == pytest.approx(1 - math.exp(-3))

    def test_no_persistence_clears_at_once(self):
        assert _js("r.persistenceFade(0, 16)") == 1

    def test_one_display_frame_is_below_the_smallest_step(self):
        # So it is saved up rather than applied and rounded away.
        assert _js("r.persistenceFade(2, 1000 / 60) < r.MIN_FADE_STEP") is True


class TestSpectrum:

    def test_window_shapes(self):
        hann = _js("Array.from(r.windowCoefficients('hann', 5))")
        assert hann == pytest.approx([0, 0.5, 1, 0.5, 0], abs=1e-12)
        assert _js("Array.from(r.windowCoefficients('rectangular', 3))") == [1, 1, 1]

    def test_the_transform_is_a_power_of_two_no_bigger_than_the_record(self):
        assert _js("[r.fftSize(1000), r.fftSize(1024), r.fftSize(100000), r.fftSize(8)]") == [
            512, 1024, 16384, 8]

    def test_a_one_volt_rms_tone_reads_zero_dbv_in_its_bin(self):
        # Bin 64 of 1024 at 1 MS/s, so the tone sits on a bin centre and every
        # window's coherent gain is exact.
        result = _js(
            "(() => { const n = 1024, k = 64, s = new Float64Array(n);"
            " for (let i = 0; i < n; i++) s[i] = Math.SQRT2 * Math.sin(2 * Math.PI * k * i / n);"
            " const out = {};"
            " for (const w of r.FFT_WINDOWS) {"
            "   const res = r.spectrumDbv(s, n, 1e6, w, {});"
            "   let best = 1; for (let b = 2; b < res.db.length; b++) if (res.db[b] > res.db[best]) best = b;"
            "   out[w] = {bin: best, db: res.db[best], resolution: res.resolution};"
            " } return out; })()")
        for window, peak in result.items():
            assert peak["bin"] == 64, window
            assert peak["resolution"] == pytest.approx(1e6 / 1024)
            # The symmetric windows here are a sample longer than periodic
            # ones, which costs a few hundredths of a dB.
            assert peak["db"] == pytest.approx(0.0, abs=0.1), window

    def test_too_few_samples_is_no_spectrum(self):
        assert _js("r.spectrumDbv(new Float64Array(8), 8, 1e6, 'hann', {})") is None

    def test_the_working_arrays_are_reused_between_frames(self):
        assert _js(
            "(() => { const cache = {}, s = new Float64Array(256);"
            " r.spectrumDbv(s, 256, 1e6, 'hann', cache); const first = cache.re;"
            " r.spectrumDbv(s, 256, 1e6, 'hann', cache); return cache.re === first; })()") is True
