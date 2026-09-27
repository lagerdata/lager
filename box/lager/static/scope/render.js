// Copyright 2024-2026 Lager Data
// SPDX-License-Identifier: Apache-2.0

/**
 * The arithmetic behind the scope's drawing, as pure functions.
 *
 * Kept apart from scope.js so it can be run under node without a DOM, and
 * written to allocate nothing per frame: at 60 frames a second a renderer
 * that builds arrays per trace hands the garbage collector megabytes a
 * second, and a collection pause is a dropped frame -- the stutter this page
 * existed to be rid of.
 */

import { NO_SAMPLE } from './lscp.js';

/** Frames the daemon may send ahead of the display.
 *
 * One is drawn per animation frame and its credit returned then, so this is
 * how many can be in flight across the network while the page draws. Three
 * covers a round trip of about two display frames; beyond that a slow link
 * lowers the frame rate rather than queueing frames that arrive stale.
 */
export const CREDIT_WINDOW = 3;

/**
 * The lowest and highest count in each pixel column of `counts[start, end)`.
 *
 * Fills `out.min` and `out.max`, which must hold `columns` entries, and
 * returns the number of columns written; a column with no captured sample
 * -- the unfilled part of a rolling screen -- is NaN in both. For a frame of
 * (minimum, maximum) pairs, columns are aligned to whole pairs so a pair is
 * never split between two.
 */
export function columnExtremes(out, counts, start, end, columns, envelope = false) {
  const span = end - start;
  if (!(span > 0) || columns < 1) return 0;
  const step = envelope ? 2 : 1;
  const perColumn = span / columns;
  for (let column = 0; column < columns; column += 1) {
    let from = Math.floor(start + column * perColumn);
    let to = Math.min(end, Math.floor(start + (column + 1) * perColumn) + 1);
    if (envelope) {
      from -= from % 2;
      to += to % 2;
    }
    let low = Infinity;
    let high = -Infinity;
    for (let i = Math.max(0, from); i < Math.min(counts.length, to); i += step) {
      const a = counts[i];
      if (a === NO_SAMPLE) continue;
      const b = envelope ? counts[i + 1] : a;
      if (b === NO_SAMPLE) continue;
      if (a < low) low = a;
      if (b > high) high = b;
    }
    if (low === Infinity) {
      out.min[column] = NaN;
      out.max[column] = NaN;
    } else {
      out.min[column] = low;
      out.max[column] = high;
    }
  }
  return columns;
}

/**
 * The span of samples to draw: the whole record, or a zoomed part of it.
 *
 * `zoom` is `{factor, center}`, center in seconds from the trigger. The
 * window is clamped inside the record rather than allowed off its end, so a
 * zoom centred near an edge still fills the screen with samples.
 */
export function zoomWindow(total, preTrigger, intervalS, zoom) {
  if (!zoom || !(zoom.factor > 1) || !(intervalS > 0) || total <= 0) {
    return { start: 0, end: total };
  }
  const span = total / zoom.factor;
  const centre = preTrigger + (Number(zoom.center) || 0) / intervalS;
  const start = Math.min(Math.max(0, centre - span / 2), total - span);
  return { start, end: start + span };
}

/** `a+b`, `a-b`, `b-a`, `a*b`, as `{left, op, right}`, or null. */
export function parseMath(expr) {
  const match = /^\s*([a-dA-D])\s*([-+*])\s*([a-dA-D])\s*$/.exec(String(expr || ''));
  if (!match || match[1].toUpperCase() === match[3].toUpperCase()) return null;
  return { left: match[1].toUpperCase(), op: match[2], right: match[3].toUpperCase() };
}

export function combine(op, a, b) {
  if (op === '+') return a + b;
  if (op === '-') return a - b;
  return a * b;
}

/** Fraction of a persistence layer to fade after `dtMs`.
 *
 * Exponential, with a time constant a third of `seconds`, so a trace is
 * down to about 5% of its brightness `seconds` after it was drawn. Frames
 * arrive unevenly, and fading by the time elapsed rather than per frame
 * keeps the look the same at 20 frames a second and at 60.
 */
export function persistenceFade(seconds, dtMs) {
  if (seconds === 'infinite') return 0;
  const tau = (Number(seconds) * 1000) / 3;
  if (!(tau > 0) || !(dtMs > 0)) return 1;
  return 1 - Math.exp(-dtMs / tau);
}

// Coherent gain of each window: the factor it scales a tone's amplitude by,
// which the spectrum divides back out so a 1 V RMS tone reads 0 dBV.
const COHERENT_GAIN = {
  rectangular: 1.0,
  hann: 0.5,
  hamming: 0.54,
  blackman: 0.42,
  flattop: 0.2156,
};

export const FFT_WINDOWS = Object.keys(COHERENT_GAIN);

export function windowCoefficients(name, n) {
  const w = new Float64Array(n);
  for (let i = 0; i < n; i += 1) {
    const phase = (2 * Math.PI * i) / Math.max(1, n - 1);
    switch (name) {
      case 'hann': w[i] = 0.5 - 0.5 * Math.cos(phase); break;
      case 'hamming': w[i] = 0.54 - 0.46 * Math.cos(phase); break;
      case 'blackman':
        w[i] = 0.42 - 0.5 * Math.cos(phase) + 0.08 * Math.cos(2 * phase); break;
      case 'flattop':
        w[i] = 0.21557895 - 0.41663158 * Math.cos(phase) + 0.277263158 * Math.cos(2 * phase)
          - 0.083578947 * Math.cos(3 * phase) + 0.006947368 * Math.cos(4 * phase);
        break;
      default: w[i] = 1;
    }
  }
  return w;
}

/** Largest power of two not above `n`, capped so a deep record stays cheap. */
export function fftSize(n, cap = 16384) {
  let size = 1;
  while (size * 2 <= Math.min(n, cap)) size *= 2;
  return size;
}

/**
 * Spectrum of `samples` (volts, at `sampleRate`) in dBV per bin, 0 to Nyquist.
 *
 * `cache` holds the working arrays between calls, keyed by size and window,
 * so a stream of same-sized frames allocates once. Radix-2, in place.
 */
export function spectrumDbv(samples, count, sampleRate, windowName, cache) {
  const n = fftSize(count);
  if (n < 16) return null;
  if (!cache.re || cache.n !== n || cache.windowName !== windowName) {
    cache.n = n;
    cache.windowName = windowName;
    cache.re = new Float64Array(n);
    cache.im = new Float64Array(n);
    cache.window = windowCoefficients(windowName, n);
    cache.db = new Float64Array(n / 2 + 1);
    cache.bits = Math.log2(n);
  }
  const { re, im, window, db } = cache;
  // The newest n samples: the end of the record is where a rolling screen
  // has data, and for a block it is as good as any other n.
  const offset = count - n;
  let mean = 0;
  for (let i = 0; i < n; i += 1) mean += samples[offset + i];
  mean /= n;
  for (let i = 0; i < n; i += 1) {
    re[i] = (samples[offset + i] - mean) * window[i];
    im[i] = 0;
  }
  // Bit-reversal permutation.
  for (let i = 1, j = 0; i < n; i += 1) {
    let bit = n >> 1;
    for (; j & bit; bit >>= 1) j ^= bit;
    j ^= bit;
    if (i < j) {
      [re[i], re[j]] = [re[j], re[i]];
      [im[i], im[j]] = [im[j], im[i]];
    }
  }
  for (let size = 2; size <= n; size *= 2) {
    const half = size / 2;
    const angle = (-2 * Math.PI) / size;
    const wr = Math.cos(angle);
    const wi = Math.sin(angle);
    for (let start = 0; start < n; start += size) {
      let cr = 1;
      let ci = 0;
      for (let k = 0; k < half; k += 1) {
        const a = start + k;
        const b = a + half;
        const tr = re[b] * cr - im[b] * ci;
        const ti = re[b] * ci + im[b] * cr;
        re[b] = re[a] - tr;
        im[b] = im[a] - ti;
        re[a] += tr;
        im[a] += ti;
        const next = cr * wr - ci * wi;
        ci = cr * wi + ci * wr;
        cr = next;
      }
    }
  }
  const gain = COHERENT_GAIN[windowName] || 1;
  for (let k = 0; k <= n / 2; k += 1) {
    const rms = (Math.hypot(re[k], im[k]) * Math.SQRT2) / (n * gain);
    db[k] = rms > 0 ? 20 * Math.log10(rms) : -200;
  }
  return { db, n, resolution: sampleRate / n };
}
