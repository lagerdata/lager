// Copyright 2024-2026 Lager Data
// SPDX-License-Identifier: Apache-2.0

/**
 * Lager scope UI.
 *
 * Data path: fetch a ticket from `GET /scope/<net>/stream`, open the
 * WebSocket it names, send `Subscribe`, then decode LSCP binary frames.
 * Commands go over `POST /net/command` -- the same endpoint the terminal CLI
 * uses -- so the UI has no privileged path to the hardware.
 *
 * The stream is paced by the page. It subscribes with a few credits and
 * returns one for each frame it draws, on the animation frame, so the daemon
 * sends at the rate the display consumes and always sends the newest capture.
 * Before this every capture was pushed as it was taken -- 130 a second, 2 MB/s
 * -- and on a link that could not keep up they queued, which showed as a
 * trace that froze and then caught up. A hidden tab draws nothing, returns
 * nothing, and is sent nothing.
 *
 * Settings arrive the same way: the daemon pushes its state after every
 * change, whoever made it, so a timebase set from the terminal moves the
 * dropdown on a page that is already open.
 */

import { decode, FLAG_TRIGGERED, NO_SAMPLE } from './lscp.js';
import * as grammar from './commands.js';
import * as render from './render.js';

const CHANNEL_COLORS = ['--ch-a', '--ch-b', '--ch-c', '--ch-d'];
const MATH_COLOR = '--ch-math';

const TIMEBASES = [
  1e-6, 2e-6, 5e-6, 1e-5, 2e-5, 5e-5, 1e-4, 2e-4, 5e-4,
  1e-3, 2e-3, 5e-3, 1e-2, 2e-2, 5e-2, 1e-1,
];

// Horizontal divisions on the graticule. Ten, as on a bench scope, and the
// same ten the daemon spreads a capture across -- it used eight, so time/div
// meant a different thing at each end of the wire.
const HORIZONTAL_DIVISIONS = 10;

// Volts/div in the 1-2-5 sequence a scope front panel steps through. Wide
// enough to cover any PicoScope range through any sane probe; what is
// actually offered gets clamped to the attached unit below.
const VOLTS_PER_DIV = [
  0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5,
  1, 2, 5, 10, 20, 50, 100,
];

// Sentinel option value. Not a number, so it cannot collide with a scale.
const CUSTOM_SCALE = 'custom';

// Cursor actions, so a typed cursor command can pull the plot's copy back
// into step with the box's. There is no control for these on purpose: the
// console places them and the renderer draws whatever the box says is set.
const CURSOR_ACTIONS = new Set([
  'set_cursor', 'get_cursor', 'clear_cursor', 'measure_cursor',
]);

// Input coupling, as the wire spells it and as the panel shows it. GND is
// deliberately absent: `ps2000_set_channel` carries one flag for coupling, so
// the hardware has no ground switch, and the daemon refuses the setting
// rather than leaving the input live under a control that reads GND.
const COUPLINGS = [['dc', 'DC'], ['ac', 'AC']];

// Every quantity the daemon computes from a capture, in the order a bench
// scope groups them: amplitude, then timing. The panel showed five of the
// fourteen, which made the other nine look unavailable when they were in the
// same response all along.
const MEASUREMENTS = [
  ['Vpp', 'vpp', 'V'],
  ['Vmax', 'vmax', 'V'],
  ['Vmin', 'vmin', 'V'],
  ['Vavg', 'vavg', 'V'],
  ['Vrms', 'vrms', 'V'],
  ['Overshoot', 'overshoot', '%'],
  ['Freq', 'frequency', 'Hz'],
  ['Period', 'period', 's'],
  ['Rise', 'rise_time', 's'],
  ['Fall', 'fall_time', 's'],
  ['Width +', 'pulse_width_positive', 's'],
  ['Width -', 'pulse_width_negative', 's'],
  ['Duty +', 'duty_cycle_positive', '%'],
  ['Duty -', 'duty_cycle_negative', '%'],
];

// How often the readouts refresh, in ms. Each one is a capture and a round
// trip, so this trades against the capture rate the trace gets.
const MEASURE_INTERVAL_MS = 500;

// The probe ratios stamped on a switch. Anything else -- a current clamp at
// 100 mV/A, say -- goes through the console's `probe`, and is added to the
// dropdown on readback so the panel never disagrees with the hardware.
const PROBE_RATIOS = [1, 10, 100, 1000];

// How far a trace can be moved from centre, in divisions. Four is the edge of
// an eight-division screen: past that the trace is off it, which is a way to
// lose a channel with no clue where it went.
const VERTICAL_LIMIT = 4;

// How far the window can be moved from the trigger, in divisions. Five puts
// the trigger on one edge of the ten-division screen, which is the whole of
// the travel: at -5 the capture is entirely pre-trigger, at +5 entirely post.
// Going further needs a trigger delay the daemon does not expose.
const HORIZONTAL_LIMIT = 5;

const clamp = (value, low, high) => Math.min(high, Math.max(low, value));

// What the position fields can be read in, as [label, factor to the base
// unit]. The value is held in volts or seconds; the selector only changes
// how it is written.
const VERTICAL_UNITS = [['V', 1], ['mV', 1e-3]];
const HORIZONTAL_UNITS = [['s', 1], ['ms', 1e-3], ['\u00b5s', 1e-6], ['ns', 1e-9]];

/** Decimal places `text` is written to: 2 for "1.20", 3 for "1e-3". */
function decimalsIn(text) {
  const match = /^[-+]?\d*(?:\.(\d*))?(?:e([-+]?\d+))?$/i.exec(String(text).trim());
  if (!match) return 0;
  const fraction = match[1] ? match[1].length : 0;
  return Math.max(0, fraction - (match[2] ? Number(match[2]) : 0));
}

/** The coarsest step an arrow key may take at `perDiv`: a tenth of a
 * division, rounded down to a power of ten.
 *
 * Without it "0" steps by a whole unit, which at 10 mV/div is a hundred
 * divisions -- off the screen in one press.
 */
function scaleStep(perDiv) {
  if (!(perDiv > 0)) return Infinity;
  return 10 ** Math.floor(Math.log10(perDiv / 10) + 1e-9);
}

/** `text` moved one step up (`direction` 1) or down (-1), as text.
 *
 * The step is the last digit written, so "1.15" goes to "1.16" and "1.1" to
 * "1.2"; no coarser than `maxStep`, a power of ten in the same unit. The
 * places are kept through a carry -- "1.19" goes to "1.20", not "1.2" -- or
 * the next press would step ten times as far.
 */
function stepPosition(text, direction, maxStep = Infinity) {
  const value = Number(text);
  const start = Number.isFinite(value) && String(text).trim() !== '' ? value : 0;
  let places = decimalsIn(text);
  if (Number.isFinite(maxStep) && maxStep > 0) {
    places = Math.max(places, -Math.round(Math.log10(maxStep)));
  }
  // toFixed takes 0 to 100 places and throws past that, so "1e-101" left the
  // arrow keys doing nothing at all.
  places = Math.min(places, 100);
  // In whole steps, so 1.19 + 0.01 cannot come out as 1.2000000000000002.
  const unit = 10 ** places;
  return ((Math.round(start * unit) + direction) / unit).toFixed(places);
}

/** A position for a field, without float noise: 0.1 s in ms is 100. */
function formatPosition(value) {
  return String(Number(value.toPrecision(6)));
}

/** A channel's vertical position in divisions, which is what is drawn. */
function positionDivisions(state) {
  if (!state || !state.positionV) return 0;
  return state.positionV / (state.voltsPerDiv || 1);
}

/** The 1-2-5 volts/div settings this unit can reach through `attenuation`.
 *
 * The list was previously the hardware's own range boundaries divided by
 * four, which offered 13 mV, 130 mV and 1.3 V/div: values nobody reaches for,
 * and none of the round ones they do. The daemon does not need them -- it
 * takes any volts/div and selects the smallest range containing it -- so the
 * choices can be the conventional ones and let the hardware follow.
 */
function voltsPerDivChoices(caps, attenuation) {
  const spans = ((caps && caps.voltage_ranges) || [])
    .map((r) => r.full_scale_volts)
    .filter((v) => Number.isFinite(v) && v > 0);
  if (!spans.length) return VOLTS_PER_DIV.slice();

  const probe = attenuation > 0 ? attenuation : 1;
  // full_scale_volts is a range's +/- deflection, so it fills the eight
  // divisions at full_scale/4 per division -- referred to the probe tip,
  // since that is what volts/div means here.
  const fills = (Math.min(...spans) / 4) * probe;
  const max = (Math.max(...spans) / 4) * probe;
  // Half a division below the smallest range still selects that range: the
  // driver picks the smallest range that contains the request, and no two
  // PicoScope ranges sit closer than a factor of two, so anything above half
  // the smallest cannot fall through to a range the unit does not have.
  // Worth allowing -- it is what keeps 100 mV/div reachable on a 10x probe,
  // whose smallest range already fills the screen at 125 mV/div.
  const min = fills / 2;

  const within = VOLTS_PER_DIV.filter((v) => v > min && v <= max * 1.001);
  // A unit whose ranges fall between two steps of the ladder would otherwise
  // offer nothing at all.
  return within.length ? within : [Number((fills).toPrecision(3))];
}

/** The time/div settings this unit can actually reach at `memoryDepth`.
 *
 * A PicoScope's sample interval doubles with each timebase step, so what it
 * can reach is the fastest screen time times powers of two -- 8 us, 16 us,
 * 32 us, 64 us and so on up on a 2204A -- and nothing in between. This
 * offered a 1-2-5 ladder filtered to the fast end instead, so all but a few
 * of the settings on it were unreachable: asking for 5 ms/div got 4.096, and
 * asking for 50 us got 64. The dropdown then had to correct itself to the
 * achieved value after every change, inserting an off-ladder entry each time
 * and rebuilding the list underneath whoever was using it, which is what made
 * the control feel like it ignored a change, snapped back, or applied the
 * previous one.
 *
 * Generating the reachable steps instead means a request is the setting, so
 * there is nothing to correct and the list only moves when the block depth
 * does.
 *
 * The powers of two are a PicoScope property. Nothing here bounds the slowest
 * interval either, so the ladder runs to the slowest we offer. A scope with no
 * daemon capabilities -- a Rigol, whose steps really are 1-2-5 -- has no rate
 * to work from and keeps the plain ladder.
 */
function timebaseChoices(caps, memoryDepth) {
  const rate = Number(caps && caps.max_sample_rate_hz);
  const depth = Number(memoryDepth);
  if (!(rate > 0) || !(depth > 0)) return TIMEBASES.slice();

  const fastest = depth / (rate * HORIZONTAL_DIVISIONS);
  if (!(fastest > 0)) return TIMEBASES.slice();

  // Roll mode takes a slow timebase as asked, so a unit that can roll gets
  // the ladder out to 10 s/div; block mode stops where the list always did.
  const slowest = caps && caps.roll_mode ? 10 : TIMEBASES[TIMEBASES.length - 1];
  const reachable = [];
  // Past the slowest offered by one step, so the ladder does not stop just
  // short of a whole second on a unit whose steps straddle it.
  for (let t = fastest; t <= slowest * 2 && reachable.length < 40; t *= 2) {
    reachable.push(t);
  }
  return reachable.length ? reachable : [fastest];
}

/** The trace's voltage at a time relative to the trigger, interpolated.
 *
 * Null when the cursor falls outside the record, matching the box's
 * `trace_voltage_at`: a cursor past the end has no signal under it, and the
 * nearest sample is a voltage from a different moment.
 *
 * Interpolated rather than snapped to the nearest sample because a cursor
 * lands where it was typed, not on the sample grid -- and the difference is
 * largest on a fast edge, where it is most worth reading.
 */
// The settings that belong to one channel. Everything else a scope takes --
// the timebase, the horizontal position, the trigger, run/stop/single/force,
// the cursors, whatever the unit reports about itself -- belongs to the
// instrument. Kept in step with _PER_CHANNEL_SCOPE_ACTIONS on the box, which
// a test compares this against: a name here that is device-wide there would
// send the command to a channel net, where it would quietly succeed.
const PER_CHANNEL_ACTIONS = new Set([
  'enable_net', 'disable_net', 'get_net_enabled',
  'set_scale', 'get_scale',
  'set_coupling', 'get_coupling',
  'set_probe', 'get_probe',
  'set_offset', 'get_offset',
  'measure_all',
  // `spectrum` with no channel named: refused on the instrument, which
  // would have to guess one.
  'fft',
]);

// Cursors are the scope's, and measure_cursor reads them against the channel
// they were placed on rather than against a net's own.
function isMeasurement(action) {
  return action.startsWith('measure_') && action !== 'measure_cursor';
}

function isPerChannelAction(action) {
  if (action === 'measure_cursor') return false;
  return isMeasurement(action) || PER_CHANNEL_ACTIONS.has(action);
}

function sampleTraceAt(frame, volts, seconds) {
  const exact = frame.preTriggerSamples
    + (seconds * 1e9) / frame.sampleIntervalNs;
  const low = Math.floor(exact);
  if (low < 0 || low >= volts.length) return null;
  if (low + 1 >= volts.length) return low === exact ? volts[low] : null;
  return volts[low] + (exact - low) * (volts[low + 1] - volts[low]);
}

const el = (id) => document.getElementById(id);

/** A channel as the daemon's JSON spells it -- {"Alphabetic": "A"} -- as "A". */
function channelName(id) {
  if (!id) return '';
  if (typeof id === 'string') return id;
  if (id.Alphabetic) return id.Alphabetic;
  if (id.Numeric !== undefined) return String.fromCharCode(64 + Number(id.Numeric));
  return String(id);
}

/**
 * The channel a net's pin names, counting from 0 for A, or null for none.
 *
 * Read the way the box reads a pin: a number counts from 1, a letter names
 * the channel, and CH2, CHAN2 or CHANNEL2 is the number with a prefix. Zero
 * is what the box saves for a net with no pin.
 */
function pinIndex(net) {
  const text = String((net && net.pin) ?? '').trim().toUpperCase()
    .replace(/^CH(?:AN(?:NEL)?)?/, '');
  if (/^\d+$/.test(text)) return Number(text) > 0 ? Number(text) - 1 : null;
  if (/^[A-Z]$/.test(text)) return text.charCodeAt(0) - 65;
  return null;
}

/**
 * The net of the lowest channel anything is wired to: A's, or B's if A has none.
 *
 * By pin, not by place in the list, which is only the order the nets were
 * saved in. The list decides only where no net names a pin.
 */
function firstChannelNet(nets) {
  let first = null;
  for (const net of nets) {
    const index = pinIndex(net);
    if (index !== null && (first === null || index < pinIndex(first))) first = net;
  }
  return first || nets[0] || null;
}

/**
 * The first channel that is on and has a net, or null if none is.
 *
 * Channels with no net of their own cannot be reached through this endpoint,
 * so they are passed over rather than reported as the answer. The
 * measurements panel and the console's per-channel commands both start here,
 * so the two cannot settle on different channels.
 */
function firstMeasurableChannel(channelState) {
  if (!channelState) return null;
  for (const [label, state] of channelState.entries()) {
    if (state && state.enabled && state.net) return { label, net: state.net };
  }
  return null;
}

// Switching a channel on or off is the one per-channel thing that must not
// follow the channel that is on: a bare `enable` would then reach a channel
// that already is.
const CHANNEL_SWITCH_ACTIONS = new Set(['enable_net', 'disable_net', 'get_net_enabled']);

/** The letter of the channel a net is wired to, or null. */
function labelForNet(name, channelState, nets) {
  if (channelState) {
    for (const [label, state] of channelState.entries()) {
      if (state && state.net === name) return label;
    }
  }
  const net = (nets || []).find((n) => n.name === name);
  const index = net ? pinIndex(net) : null;
  return index === null ? null : String.fromCharCode(65 + index);
}

/**
 * The channel a per-channel action reaches when the command names none, as
 * `{label, net}`, or null when there is no channel net at all.
 *
 * The first channel that is on, as the measurements panel reads it; the
 * lowest channel by pin when none is on, or before the page knows which are.
 * The box applies a setting to a channel that is off without complaint, so a
 * fixed lowest channel quietly changed A while the trace on screen was B.
 */
function defaultChannel(action, channelState, channelNets) {
  if (!CHANNEL_SWITCH_ACTIONS.has(action)) {
    const on = firstMeasurableChannel(channelState);
    if (on) return on;
  }
  const net = firstChannelNet(channelNets || []);
  if (!net) return null;
  return { label: labelForNet(net.name, channelState, channelNets), net: net.name };
}

/**
 * Which of CHANNEL_COLORS a channel is drawn in: A the first, B the second.
 *
 * By name rather than by place in a capture, since a capture carries only
 * the channels that are on: with A off, B is a frame's first channel.
 */
function colorSlot(label) {
  const name = String(label ?? '').trim().toUpperCase();
  let index = 0;
  if (/^[A-Z]$/.test(name)) index = name.charCodeAt(0) - 65;
  else if (/^[1-9]\d*$/.test(name)) index = Number(name) - 1;
  return index % CHANNEL_COLORS.length;
}

/** A channel's colour as CSS, for swatches the stylesheet resolves. */
function channelColorVar(label) {
  return `var(${CHANNEL_COLORS[colorSlot(label)]})`;
}

/**
 * Whether two settings are the same one, give or take float noise.
 *
 * The daemon works a timebase out from its sample interval and memory depth,
 * so 1.024 ms comes back as 0.0010240000000000002: not the string the
 * dropdown holds, though no different a setting.
 */
function sameSetting(a, b) {
  const x = Number(a);
  const y = Number(b);
  if (!Number.isFinite(x) || !Number.isFinite(y)) return false;
  return Math.abs(x - y) <= 1e-6 * Math.max(Math.abs(x), Math.abs(y));
}

/** Set a control from pushed state unless someone is editing it. */
function setIdle(id, value) {
  const control = typeof id === 'string' ? el(id) : id;
  if (!control || value === undefined || value === null) return;
  if (typeof document !== 'undefined' && document.activeElement === control) return;
  const text = String(value);
  if (control.type === 'checkbox') {
    control.checked = Boolean(value);
    return;
  }
  if (control.tagName === 'SELECT' && ![...control.options].some((o) => o.value === text)) {
    control.append(new Option(text, text));
  }
  control.value = text;
}

/** Format a value with an SI prefix, for axis labels and readouts. */
function si(value, unit, digits = 3) {
  if (value === null || value === undefined || !Number.isFinite(value)) return '\u2014';
  const abs = Math.abs(value);
  if (abs === 0) return `0 ${unit}`;
  const prefixes = [
    [1e9, 'G'], [1e6, 'M'], [1e3, 'k'], [1, ''],
    [1e-3, 'm'], [1e-6, '\u00b5'], [1e-9, 'n'], [1e-12, 'p'],
  ];
  for (const [factor, prefix] of prefixes) {
    if (abs >= factor) {
      return `${figures(value / factor, digits)} ${prefix}${unit}`;
    }
  }
  return `${value.toPrecision(digits)} ${unit}`;
}

/** `value` to `digits` significant figures, never as 1.0e+2. */
function figures(value, digits) {
  const text = value.toPrecision(digits);
  // More whole digits than figures -- 100 mV at two -- comes back in
  // exponent form; the whole number says the same.
  return text.includes('e+') ? String(Math.round(value)) : text;
}

/**
 * Wire a position field: a number in the unit beside it, held in the base
 * unit and stepped by the arrow keys at its last written digit.
 *
 * `set(value, {announce})` applies a value in volts or seconds and returns
 * (or resolves to) what it applied after clamping. With `live` it is called
 * as the field is typed in; without, only on Enter or when the field is
 * left, and Escape puts back the value last applied. Arrows apply at once
 * either way.
 *
 * Returns `{show(value)}`, which writes a value applied elsewhere into the
 * field unless someone is editing it.
 */
function wirePositionField(input, unitSelect, { live, perDiv, get, set }) {
  const factor = () => Number(unitSelect.value) || 1;
  const write = (value) => { input.value = formatPosition(value / factor()); };
  const typed = () => {
    const value = Number(input.value);
    return input.value.trim() === '' || !Number.isFinite(value) ? null : value * factor();
  };
  // Typed in since the field was last applied or put back. What the field
  // shows is rounded to six figures, so comparing it with the value held
  // cannot tell an untouched field from an edited one: 0.00123457 is not
  // 0.0012345678, and leaving the field re-applied it -- horizontally, a
  // re-arm -- though nobody had typed a thing.
  let edited = false;

  // One value in flight at a time, and only the newest waiting behind it. A
  // held arrow key repeats faster than the box answers, and sets sent side by
  // side can finish out of order and leave the box on an older value. A value
  // overtaken while waiting resolves to null and is never sent.
  let busy = false;
  let waiting = null;
  const apply = async (value, options) => {
    if (busy) {
      if (waiting) waiting.resolve(null);
      return new Promise((resolve) => { waiting = { value, options, resolve }; });
    }
    busy = true;
    try {
      return await set(value, options);
    } finally {
      busy = false;
      const next = waiting;
      waiting = null;
      if (next) next.resolve(apply(next.value, next.options));
    }
  };

  // Re-written only when the value applied is not the one typed -- a clamp --
  // so "1.20" is not cut to "1.2" and the next arrow keeps its step.
  const commit = async () => {
    edited = false;
    const value = typed();
    if (value === null) {
      write(get());
      return;
    }
    const applied = await apply(value, { announce: true });
    if (Number.isFinite(applied) && !sameSetting(applied, value)) write(applied);
  };

  input.addEventListener('keydown', async (event) => {
    if (event.key === 'ArrowUp' || event.key === 'ArrowDown') {
      event.preventDefault();
      const text = typed() === null ? formatPosition(get() / factor()) : input.value;
      const next = stepPosition(text, event.key === 'ArrowUp' ? 1 : -1,
        scaleStep(perDiv()) / factor());
      input.value = next;
      edited = false;
      const applied = await apply(Number(next) * factor(), { announce: true });
      // Not if a later press has moved the field on since.
      if (Number.isFinite(applied) && input.value === next
          && !sameSetting(applied, Number(next) * factor())) {
        input.value = (applied / factor()).toFixed(decimalsIn(next));
      }
    } else if (!live && event.key === 'Enter') {
      event.preventDefault();
      commit();
    } else if (!live && event.key === 'Escape') {
      event.preventDefault();
      write(get());
      edited = false;
    }
  });
  input.addEventListener('input', () => {
    edited = true;
    if (!live) return;
    // Blank part-way through typing a minus sign, or cleared outright.
    // Leaving the trace where it is beats snapping it to centre and back.
    const value = typed();
    if (value !== null) apply(value, { announce: false });
  });
  // Leaving the field commits a typed value, or, live, says if it was clamped.
  input.addEventListener('blur', () => {
    if (edited && (live || typed() === null || !sameSetting(typed(), get()))) commit();
    edited = false;
  });
  unitSelect.addEventListener('change', () => write(get()));

  return {
    show(value) {
      if (typeof document !== 'undefined' && document.activeElement === input) return;
      write(value);
    },
  };
}

class Console {
  constructor(output) {
    this.output = output;
    this.history = [];
    this.historyIndex = 0;
  }

  write(text, kind = 'ok') {
    const line = document.createElement('div');
    line.className = `line--${kind}`;
    line.textContent = text;
    this.output.appendChild(line);
    // Only autoscroll when already at the bottom, so reading back through
    // output is not yanked away by new arrivals.
    const nearBottom = this.output.scrollHeight - this.output.scrollTop
      - this.output.clientHeight < 40;
    if (nearBottom) this.output.scrollTop = this.output.scrollHeight;
  }

  echo(text) { this.write(`> ${text}`, 'echo'); }
  error(text) { this.write(text, 'error'); }
  note(text) { this.write(text, 'note'); }

  clear() { this.output.replaceChildren(); }

  remember(line) {
    if (this.history[this.history.length - 1] !== line) this.history.push(line);
    this.historyIndex = this.history.length;
  }

  recall(delta) {
    if (this.history.length === 0) return null;
    this.historyIndex = Math.min(
      this.history.length, Math.max(0, this.historyIndex + delta));
    return this.historyIndex === this.history.length
      ? '' : this.history[this.historyIndex];
  }
}

class ScopeApp {
  constructor() {
    this.net = null;
    // Every scope net on the box, kept so a channel can be mapped to the net
    // that addresses it rather than to whichever net is selected.
    this.scopeNets = [];
    // The stream's socket from the moment it is created, so one still
    // opening counts as a connection.
    this.socket = null;
    // True from Connect until the socket opens or the attempt is given up,
    // which covers the ticket request, before there is any socket at all.
    this.connecting = false;
    // Moved on by every disconnect(), so an attempt still waiting on its
    // ticket can tell it has been abandoned.
    this.connectAttempt = 0;
    this.ticket = null;
    this.capabilities = null;
    this.latest = null;
    this.dirty = false;
    // Whether the "not streaming" overlay is down, so drawing does not touch
    // the DOM every frame.
    this.overlayHidden = false;
    // A rolling screen held where it stopped: `{at, frame}`, or null.
    this.rollHold = null;
    this.channelState = new Map();
    // Trigger level and position drawn on the plot. On by default: a level
    // you cannot see is one you cannot set with any confidence.
    this.showTriggerMarkers = true;
    // Seconds the capture window is shifted from the trigger.
    this.timePositionS = 0;
    // Cursors, as the box last reported them: `{cursors, readings}` or null.
    // Held rather than derived, since the box owns them and the console is
    // the only thing that moves them.
    this.cursors = null;
    // Block depth the timebase list was last built for. Null until a capture
    // reports one, which is when the unreachable fast steps can be dropped.
    this.timebaseDepth = null;
    // Counts timebase changes, so a readback that arrives after a later
    // change can tell that it is stale and leave the control alone.
    this.timebaseGeneration = 0;
    // Measurement polling, so the readouts follow the signal rather than
    // describing whatever was on screen when Start was pressed.
    this.measureTimer = null;
    this.measureInFlight = false;

    this.captureCount = 0;
    this.lastRateAt = performance.now();
    this.rate = 0;
    this.latencyMs = null;

    // The newest capture not yet drawn, still encoded: decoding waits for
    // the animation frame, so a capture replaced before it is drawn costs
    // nothing but its arrival.
    this.pendingBuffer = null;
    // Frames received since credit was last returned.
    this.owedCredits = 0;
    // The daemon's last pushed state, and the display settings in it.
    this.state = null;
    this.display = {};
    // Working buffers the renderer reuses from frame to frame.
    this.extremes = { min: new Float32Array(0), max: new Float32Array(0) };
    this.spectrumCache = {};
    this.persist = null;
    this.colors = null;
    this.frameSeq = null;
    this.drawnCount = 0;
    this.status = '';
    // The display's refresh rate, measured from animation frames, which is
    // the rate worth asking the daemon for.
    this.refreshHz = 60;
    this.frameTimes = [];
    // Page clock minus box clock plus the quickest transit seen, so a
    // rolling screen can be scrolled by how long ago it was captured.
    this.clockOffsets = [];
    this.clockOffset = null;
    this.resetRollDelay();

    this.console = new Console(el('console-output'));
    this.canvas = el('scope-canvas');
    this.ctx = this.canvas.getContext('2d');

    this.wireControls();
    this.wireConsole();
    this.observeCanvas();

    requestAnimationFrame((t) => this.tick(t));
  }

  // ---------- setup ----------
  async init() {
    this.console.write('Lager scope. Type "help" for commands.', 'note');
    await this.loadNets();
  }

  async loadNets() {
    const select = el('net-select');
    try {
      const response = await fetch('/nets/list');
      const body = await response.json();
      const listed = body.nets || body || [];
      const scopes = listed.filter(
        (n) => n.role === 'scope' || n.role === 'scope-channel');
      // The stream comes from the PicoScope daemon, so the box refuses a
      // ticket for any other make; offered, a Rigol could only be refused.
      const nets = scopes.filter((n) => /pico/i.test(n.instrument || ''));
      // An analog net names a scope as well, but the box takes no commands
      // for that role, so offering one gave a page whose every control was
      // refused. Each kind left out is said once instead, naming them.
      const analog = listed.filter((n) => n.role === 'analog').map((n) => n.name);
      const otherMakes = scopes.filter((n) => n.role === 'scope' && !nets.includes(n))
        .map((n) => n.name);
      const notices = [];
      if (analog.length) {
        notices.push(`Not offered: ${analog.join(', ')}. The box takes no scope commands`
          + ' for the "analog" role; add a scope net with "lager nets add".');
      }
      if (otherMakes.length) {
        notices.push(`Not offered: ${otherMakes.join(', ')}. The live view streams`
          + ' a PicoScope only; "lager scope" drives the others.');
      }

      select.replaceChildren();
      if (nets.length === 0) {
        select.append(new Option('no scope nets', ''));
        this.console.error(notices.length
          ? `No PicoScope nets on this box. ${notices.join(' ')}`
          : 'No scope nets on this box. Create one with "lager nets add".');
        return;
      }
      for (const notice of notices) this.console.note(notice);

      // The dropdown picks an instrument, not a channel. A scope net is the
      // scope; its scope-channel nets are the channels, and the strips below
      // come from those. Where a box has no scope net -- nothing has been
      // added since the roles split -- every net is offered as before, so
      // the page still works on one that has not been through the migration.
      this.scopeNets = nets;
      const instruments = nets.filter((n) => n.role === 'scope');
      const offered = instruments.length ? instruments : nets;
      for (const net of offered) {
        select.append(new Option(net.name, net.name));
      }
      this.net = offered[0].name;
      select.value = this.net;
      this.adoptChannelNets();
      await this.loadCapabilities();
    } catch (e) {
      select.replaceChildren(new Option('unavailable', ''));
      this.console.error(`Could not list nets: ${e.message}`);
    }
  }

  /** The channel nets of the scope now selected.
   *
   * Matched on instrument and address, which is how a bench holding two of
   * the same model keeps their channels apart. A box that predates the role
   * split has no scope-channel nets at all, so the scope-family nets stand in
   * and the page behaves as it did.
   */
  adoptChannelNets() {
    const all = this.scopeNets || [];
    const me = all.find((n) => n.name === this.net);
    const sameUnit = (n) => me
      && (n.instrument || '') === (me.instrument || '')
      && (n.address || '') === (me.address || '');

    const channels = all.filter((n) => n.role === 'scope-channel' && sameUnit(n));
    this.channelNets = channels.length
      ? channels
      : all.filter((n) => n.name !== this.net || all.length === 1);
  }

  /** The net a command should be sent to.
   *
   * Most of a scope is not per-channel, so the instrument takes the timebase,
   * the trigger, acquisition and the cursors. The settings that do belong to
   * a channel go to that channel's net, because the scope net has no channel
   * and the box refuses to guess one. Without a channel named, that is
   * defaultChannel()'s answer.
   */
  netForAction(action, explicit) {
    if (explicit) return explicit;
    if (!isPerChannelAction(action)) return this.net;
    const channel = defaultChannel(action, this.channelState, this.channelNets);
    return channel ? channel.net : this.net;
  }

  /** The net `enable B` should reach, or null when that channel has none. */
  netForLabel(label) {
    const name = String(label || '').trim().toUpperCase();
    const state = this.channelState && this.channelState.get(name);
    // A strip that exists but has no net is unwired. Falling through to a
    // pin lookup would hand the command to a different channel.
    if (state) return state.net || null;
    let index = null;
    if (/^[A-D]$/.test(name)) index = name.charCodeAt(0) - 65;
    else if (/^[1-4]$/.test(name)) index = Number(name) - 1;
    if (index === null) return null;
    return this.netForChannel(index);
  }

  /**
   * Fetch a stream ticket for the selected net, and the capabilities that
   * come with it. Resolves to the ticket, or null when there is none.
   */
  async loadCapabilities() {
    // Cleared first: a ticket does not outlive a failed request, and the
    // last one may be expired or for another net.
    this.ticket = null;
    const net = this.net;
    if (!net) return null;
    try {
      const response = await fetch(`/scope/${encodeURIComponent(net)}/stream`);
      const body = await response.json();
      // The dropdown moved on while this was out; that net's request decides.
      if (net !== this.net) return null;
      if (!response.ok) {
        this.console.error(body.error || `Ticket request failed (${response.status})`);
        return null;
      }
      this.ticket = body;
      this.capabilities = body.capabilities;
      if (body.capability_error) {
        this.console.note(`Capabilities unavailable: ${body.capability_error}`);
      }
      this.applyCapabilities();
      return body;
    } catch (e) {
      if (net === this.net) this.console.error(`Could not reach the scope: ${e.message}`);
      return null;
    }
  }

  /**
   * Build the controls from what this unit actually supports. A 2-channel
   * 2204A gets two channel strips, a 4-channel unit gets four, and features
   * the unit lacks are not offered at all rather than failing when used.
   */
  applyCapabilities() {
    const caps = this.capabilities || {};
    el('model').textContent = caps.model || 'unknown';
    el('serial').textContent = caps.serial ? `#${caps.serial}` : '';

    const count = Number(caps.analog_channels) || 1;
    const labels = caps.channel_labels && caps.channel_labels.length
      ? caps.channel_labels
      : Array.from({ length: count }, (_, i) => String.fromCharCode(65 + i));

    // Channel strips.
    const host = el('channels');
    host.replaceChildren();
    this.channelState.clear();
    labels.forEach((label, index) => {
      this.channelState.set(label, {
        enabled: index === 0,
        voltsPerDiv: 1,
        // 1x until the probe is read back below. Volts/div is at the probe
        // tip, so this decides which settings the channel can reach.
        attenuation: 1,
        // Volts this trace is drawn above centre, for viewing only. In volts
        // so a trace stays on its level through a scale change, which is
        // what a control labelled in volts promises.
        positionV: 0,
        net: this.netForChannel(index),
      });
      host.appendChild(this.buildChannelStrip(label, index, caps));
    });

    // The strips above render a guess. Replace it with what the hardware
    // actually reports, so the UI cannot claim a channel is on while the
    // scope has it off -- which showed up as an enabled channel producing
    // empty captures and "channel X is not enabled" from every measurement.
    this.syncChannelState(labels);

    // Trigger sources are exactly the channels that exist.
    const source = el('trigger-source');
    source.replaceChildren();
    labels.forEach((label) => source.append(new Option(label, label)));

    // Timebase choices. Not yet trimmed to what the unit can reach: that
    // needs a block depth, which only a capture reports, so the list is
    // rebuilt when the first one arrives.
    const timebase = el('timebase');
    timebase.replaceChildren();
    for (const value of timebaseChoices(caps, this.timebaseDepth)) {
      timebase.append(new Option(`${si(value, 's', 3)}/div`, String(value)));
    }
    timebase.value = String(1e-3);

    // Spectrum: one choice per channel this unit has.
    const fft = el('display-fft');
    if (fft) {
      fft.replaceChildren(new Option('Off', 'off'));
      labels.forEach((label) => fft.append(new Option(`Channel ${label}`, label)));
    }
    // Roll mode only where the unit can stream; peak detect only where its
    // driver can aggregate, and disabled rather than hidden so its absence
    // is explained.
    const rollField = el('roll-field');
    if (rollField) rollField.hidden = !caps.roll_mode;
    const peak = el('acquire-mode') && [...el('acquire-mode').options].find((o) => o.value === 'peak');
    if (peak) {
      peak.disabled = !caps.peak_detect;
      peak.title = caps.peak_detect ? ''
        : `The ${caps.model || 'unit'} keeps one sample per interval in block mode; roll mode is always peak-detected`;
    }

    this.showCapabilityNotes(caps);
  }

  /**
   * Bring every control into step with the state the daemon pushed.
   *
   * Nothing is sent back: this is what the hardware already has. A field
   * someone is typing in is left alone until they finish.
   */
  applyState(state) {
    const previous = this.state;
    this.state = state;
    this.display = state.display || {};
    this.adoptCursors(this.display.cursors || null);

    for (const channel of state.channels || []) {
      const label = channelName(channel.channel);
      const cs = this.channelState.get(label);
      if (!cs) continue;
      cs.enabled = Boolean(channel.enabled);
      if (cs.toggle) cs.toggle.checked = cs.enabled;
      if (channel.attenuation && channel.attenuation !== cs.attenuation) {
        cs.attenuation = channel.attenuation;
        this.showProbe(cs, channel.attenuation);
        this.rebuildScaleChoices(label);
      }
      if (channel.volts_per_div && channel.volts_per_div !== cs.voltsPerDiv) {
        this.applyVoltsPerDiv(label, channel.volts_per_div, { push: false });
      }
      if (cs.couplingSelect && channel.coupling) {
        setIdle(cs.couplingSelect, String(channel.coupling).toLowerCase());
      }
    }

    const timebase = state.timebase || {};
    const select = el('timebase');
    if (timebase.time_per_div > 0 && select && document.activeElement !== select) {
      this.showTimebase(timebase.time_per_div);
    }
    if (Number.isFinite(timebase.time_offset)
        && !sameSetting(timebase.time_offset, this.timePositionS)
        && document.activeElement !== el('time-position')) {
      this.applyTimePosition(timebase.time_offset, { push: false });
    }

    const trigger = state.trigger || {};
    setIdle('trigger-mode', state.capture_mode);
    setIdle('trigger-source', channelName(trigger.source));
    setIdle('trigger-slope', trigger.slope);
    if (Number.isFinite(trigger.level)) setIdle('trigger-level', Number(trigger.level.toPrecision(6)));
    if (Number.isFinite(trigger.holdoff_s)) setIdle('trigger-holdoff', trigger.holdoff_s);

    const acquisition = state.acquisition || {};
    setIdle('acquire-mode', acquisition.mode);
    setIdle('acquire-count', acquisition.average_count);
    setIdle('roll-mode', timebase.roll);

    const display = this.display;
    const persistence = display.persistence;
    setIdle('display-persistence', persistence === undefined ? 'off' : persistence);
    setIdle('display-xy', Boolean(display.xy));
    setIdle('display-zoom', display.zoom ? display.zoom.factor : 1);
    setIdle('display-zoom-center', display.zoom ? display.zoom.center : 0);
    setIdle('display-math', display.math ? display.math.expr : 'off');
    setIdle('display-fft', display.fft ? display.fft.channel : 'off');

    // Earlier traces were drawn at another scale or another time; keeping
    // them would smear the old settings over the new ones.
    if (previous && JSON.stringify(previous.channels) !== JSON.stringify(state.channels)) {
      this.persist = null;
    }
    if (previous && previous.timebase && previous.timebase.time_per_div !== timebase.time_per_div) {
      this.persist = null;
    }
    this.status = '';
    this.requestRedraw();
  }

  /** Change display settings, drawing them now and telling the box. */
  setDisplay(settings) {
    // Drawn before the reply, so the control feels immediate; the state the
    // box pushes back confirms it, or puts it right if it refused.
    const merged = { ...(this.display || {}) };
    for (const [key, value] of Object.entries(settings)) {
      if (value === 'off') delete merged[key];
      else if (key === 'xy') merged.xy = value === 'on';
      else if (key === 'math') merged.math = { expr: value };
      else merged[key] = value;
    }
    this.display = merged;
    this.persist = null;
    this.requestRedraw();
    return this.runCommand('set_display', settings, 'display');
  }

  buildChannelStrip(label, index, caps) {
    const color = channelColorVar(label);
    const strip = document.createElement('div');
    strip.className = 'channel';

    const head = document.createElement('div');
    head.className = 'channel__head';
    const swatch = document.createElement('span');
    swatch.className = 'channel__swatch';
    swatch.style.background = color;
    const name = document.createElement('span');
    name.className = 'channel__name';
    name.textContent = label;
    const toggleLabel = document.createElement('label');
    const toggle = document.createElement('input');
    toggle.type = 'checkbox';
    toggle.checked = index === 0;
    toggle.addEventListener('change', async () => {
      const state = this.channelState.get(label);
      state.enabled = toggle.checked;
      await this.runCommand(
        toggle.checked ? 'enable_net' : 'disable_net', {},
        `Channel ${label} ${toggle.checked ? 'on' : 'off'}`, state.net, label);
      // The panel measures the selected net's channel, so this switch decides
      // whether there is anything to measure. It is otherwise only refreshed
      // on Start, which left "Channel A is off" showing over a channel that
      // had just been switched on.
      // Any channel's switch can change what the panel reads: it shows the
      // first one that is on, so switching A off moves it to B.
      this.refreshMeasurements();
    });
    // Held so the readback below can correct the control without rebuilding
    // the strip and losing the listener.
    this.channelState.get(label).toggle = toggle;
    toggleLabel.append(toggle, document.createTextNode('on'));
    head.append(swatch, name, toggleLabel);

    // Volts/div on the conventional 1-2-5 steps, clamped to what this unit
    // can reach, with Custom for anything in between.
    const field = document.createElement('label');
    field.className = 'field field--stack';
    const caption = document.createElement('span');
    caption.textContent = 'Volts / div';
    const select = document.createElement('select');
    const state = this.channelState.get(label);
    state.choices = voltsPerDivChoices(caps, state.attenuation);
    for (const value of state.choices) {
      select.append(new Option(si(value, 'V', 2), String(value)));
    }
    select.append(new Option('Custom\u2026', CUSTOM_SCALE));
    // Mid-list rather than the widest range: the widest makes any small
    // signal a flat line, which reads as a dead probe.
    select.value = String(
      state.choices[Math.min(state.choices.length - 1,
        Math.floor(state.choices.length / 2))]);
    state.select = select;

    // Free-entry volts/div, hidden until Custom is chosen. The daemon accepts
    // any value and picks the smallest range that holds it, so this is a real
    // setting rather than display-only zoom.
    const custom = document.createElement('div');
    custom.className = 'channel__custom';
    custom.hidden = true;
    const customInput = document.createElement('input');
    customInput.type = 'number';
    customInput.step = 'any';
    customInput.min = '0';
    customInput.setAttribute('aria-label', `Channel ${label} custom volts per division`);
    custom.append(customInput);
    state.customField = custom;
    state.customInput = customInput;

    select.addEventListener('change', () => {
      if (select.value === CUSTOM_SCALE) {
        // Seed with the value in use, so the field opens on something real
        // and a stray Enter cannot jump the scale.
        custom.hidden = false;
        customInput.value = String(state.voltsPerDiv);
        customInput.focus();
        customInput.select();
        return;
      }
      custom.hidden = true;
      this.applyVoltsPerDiv(label, Number(select.value));
    });
    // Committed edits only, not each keystroke, which would send a command
    // per digit and re-range the hardware on the way to the value wanted.
    customInput.addEventListener('change', () => {
      const value = Number(customInput.value);
      if (!Number.isFinite(value) || value <= 0) {
        this.console.error('Volts/div must be a positive number');
        return;
      }
      this.applyVoltsPerDiv(label, value);
    });

    // Keep the state in step with the control it was built from. They were
    // seeded separately -- state at 1 V/div, the select at whatever the
    // hardware's range list put in that slot -- so the trace was drawn to a
    // scale the sidebar did not show.
    state.voltsPerDiv = Number(select.value);
    field.append(caption, select);

    // Coupling and probe, the two per-channel settings that change what the
    // trace means rather than how it is drawn. Both already worked from the
    // console; they were the only front-panel controls with no control here,
    // and the probe ratio in particular was doing invisible work -- it is
    // what decides which volts/div settings the unit can reach.
    const pair = document.createElement('div');
    pair.className = 'channel__pair';

    const couplingField = document.createElement('label');
    couplingField.className = 'field field--stack';
    const couplingCaption = document.createElement('span');
    couplingCaption.textContent = 'Coupling';
    const couplingSelect = document.createElement('select');
    for (const [value, text] of COUPLINGS) {
      couplingSelect.append(new Option(text, value));
    }
    couplingField.append(couplingCaption, couplingSelect);
    state.couplingSelect = couplingSelect;
    couplingSelect.addEventListener('change', () => {
      this.runCommand('set_coupling', { mode: couplingSelect.value },
        `Channel ${label} ${couplingSelect.value.toUpperCase()} coupled`,
        state.net, label);
    });

    const probeField = document.createElement('label');
    probeField.className = 'field field--stack';
    const probeCaption = document.createElement('span');
    probeCaption.textContent = 'Probe';
    const probeSelect = document.createElement('select');
    for (const ratio of PROBE_RATIOS) {
      probeSelect.append(new Option(`${ratio}x`, String(ratio)));
    }
    probeSelect.value = String(state.attenuation);
    probeField.append(probeCaption, probeSelect);
    state.probeSelect = probeSelect;
    probeSelect.addEventListener('change', () => {
      this.applyProbe(label, Number(probeSelect.value));
    });

    pair.append(couplingField, probeField);

    // Where this channel's zero sits on screen, so two traces can be pulled
    // apart instead of drawn on top of each other.
    //
    // A view control: it moves the drawing and nothing else. Deliberately not
    // wired to the hardware's volts offset, which is added into the samples
    // themselves -- moving a trace up two divisions with it would also move
    // Vmax, Vmin and Vavg by two divisions' worth, so a trace shifted for
    // legibility would come back with readings that no longer describe the
    // signal.
    const position = document.createElement('div');
    position.className = 'field field--stack';
    const positionCaption = document.createElement('span');
    positionCaption.textContent = 'Position';
    const positionRow = document.createElement('div');
    positionRow.className = 'field-row';
    const positionInput = document.createElement('input');
    positionInput.type = 'text';
    positionInput.inputMode = 'decimal';
    positionInput.value = '0';
    positionInput.setAttribute(
      'aria-label', `Channel ${label} vertical position`);
    const positionUnit = document.createElement('select');
    positionUnit.setAttribute('aria-label', `Channel ${label} vertical position unit`);
    for (const [text, factor] of VERTICAL_UNITS) {
      positionUnit.append(new Option(text, String(factor)));
    }
    const positionReset = document.createElement('button');
    positionReset.type = 'button';
    positionReset.className = 'btn btn--small';
    positionReset.textContent = '0';
    positionReset.title = `Centre channel ${label}`;
    positionRow.append(positionInput, positionUnit, positionReset);
    position.append(positionCaption, positionRow);
    state.positionInput = positionInput;
    state.positionUnit = positionUnit;
    state.positionReset = positionReset;

    // On input rather than on Enter: nothing is sent to the scope, so the
    // trace can follow the field as a key is held down.
    state.positionField = wirePositionField(positionInput, positionUnit, {
      live: true,
      perDiv: () => state.voltsPerDiv,
      get: () => state.positionV,
      set: (volts, options) => this.applyVerticalPosition(label, volts, options),
    });
    positionReset.addEventListener('click', () => this.applyVerticalPosition(label, 0));

    strip.append(head, field, custom, pair, position);

    // No net means no way to address this channel: the box has capabilities
    // reporting it, but nothing wired to it. Disable rather than let the
    // controls fall back to the selected net, which is how channel B's
    // switch came to operate channel A.
    if (!this.channelState.get(label).net) {
      const why = `No scope net is wired to channel ${label}. `
        + 'Add one with "lager nets add" to control it here.';
      // The position field goes with them: it needs no net, but a channel
      // that can never be switched on has no trace to move.
      for (const control of [toggle, select, customInput, couplingSelect,
        probeSelect, positionInput, positionUnit, positionReset]) {
        control.disabled = true;
        control.title = why;
      }
      strip.classList.add('channel--unwired');
    }

    return strip;
  }

  /** Redraw the frame already on screen.
   *
   * The render loop draws only when a capture arrives, so a control that
   * changes how the SAME samples are drawn -- volts/div, the trigger markers
   * -- did nothing visible until the next frame. Stopped, or on a slow
   * trigger, that is indistinguishable from a control that does not work:
   * volts/div could be moved from 125 mV to 2.5 V, a twentyfold change, and
   * the trace would not move a pixel.
   */
  requestRedraw() {
    this.dirty = true;
  }

  /** Set a channel's volts/div: on the hardware, in the state, on the control.
   *
   * One path for the dropdown, the custom field and the hardware readback, so
   * the three cannot drift. The control is made to show the value in use even
   * when it is not one of the offered steps -- otherwise the sidebar reads
   * 2.5 V while the trace is drawn at 1 V, and picking the 2.5 V already
   * displayed fires no change event, so the scale appears stuck.
   */
  applyVoltsPerDiv(label, voltsPerDiv, { push = true } = {}) {
    const state = this.channelState.get(label);
    if (!state) return;

    state.voltsPerDiv = voltsPerDiv;
    this.showVoltsPerDiv(state, voltsPerDiv);
    // The position is held in volts, so its reach is four divisions of the
    // new scale; one that no longer fits is pulled in, and said so.
    if (state.positionV) this.applyVerticalPosition(label, state.positionV);
    // The scale is applied to the drawing as well as the hardware, so the
    // picture has to be redrawn even if the command fails or the scope is
    // stopped.
    this.requestRedraw();

    if (push) {
      this.runCommand('set_scale', { volts_per_div: voltsPerDiv },
        `Channel ${label} ${si(voltsPerDiv, 'V', 2)}/div`, state.net, label);
    }
  }

  /** Move a channel's trace up or down the screen by `volts`.
   *
   * A view control: it moves the drawing and nothing else. Deliberately not
   * the hardware's analog offset, which is added into the samples
   * themselves -- moving a trace up two divisions with it would also move
   * Vmax, Vmin and Vavg by two divisions' worth, so a trace shifted for
   * legibility would come back with readings that no longer describe the
   * signal.
   *
   * Held to VERTICAL_LIMIT divisions of the channel's scale. Resolves to the
   * volts applied.
   */
  applyVerticalPosition(label, volts, { announce = true } = {}) {
    const state = this.channelState.get(label);
    if (!state || !Number.isFinite(volts)) return null;
    const limit = VERTICAL_LIMIT * (state.voltsPerDiv || 1);
    const value = clamp(volts, -limit, limit);
    if (announce && value !== volts) {
      this.console.note(`Channel ${label} position limited to ${si(value, 'V', 3)}: `
        + `${VERTICAL_LIMIT} divisions at ${si(state.voltsPerDiv, 'V', 2)}/div is the edge of the screen`);
    }
    state.positionV = value;
    if (state.positionField) state.positionField.show(value);
    this.requestRedraw();
    return value;
  }

  /** Set the timebase, then show what the hardware actually landed on.
   *
   * A scope has a fixed set of sample intervals, so a request is rounded to
   * one of them: 1 ms/div on a 2204A becomes 1.024 ms/div. The dropdown used
   * to keep displaying the request, which is the same fault the volts/div
   * list had -- a control that reads back its own input tells you nothing
   * about the instrument.
   */
  async applyTimebase(seconds, { push = true } = {}) {
    if (!(seconds > 0)) return;
    this.showTimebase(seconds);

    if (push) {
      // Which request this is. Two changes in quick succession each set and
      // then read back, and the replies need not arrive in order -- so the
      // slower one used to land last and put the earlier setting back on the
      // control, which is the "it reverted to what it was" of a dropdown that
      // seemed to lag a step behind.
      const generation = (this.timebaseGeneration || 0) + 1;
      this.timebaseGeneration = generation;

      await this.runCommand('set_timebase', { seconds_per_div: seconds },
        `timebase ${si(seconds, 's', 2)}/div`);
      try {
        const body = await this.send('get_timebase', {});
        const achieved = Number(body.value);
        if (this.timebaseGeneration !== generation) return;
        if (Number.isFinite(achieved) && achieved > 0) this.showTimebase(achieved);
      } catch { /* older box: leave the request showing */ }
    }

    // Re-sent, not just kept: the box turns seconds into a pre/post-trigger
    // split of the window in force when it arrives, so the old split is a
    // different time at the new scale. Re-clamped too, since the travel is
    // five divisions of whichever scale that is.
    if (this.timePositionS) await this.applyTimePosition(this.timePositionS);
  }

  /** Make the timebase dropdown display `seconds`, offered or not. */
  showTimebase(seconds) {
    const select = el('timebase');
    if (!select) return;

    const offered = [...select.options].find((o) => sameSetting(o.value, seconds));
    if (offered) {
      select.value = offered.value;
      return;
    }
    // Off the ladder, because the hardware rounded to an interval that is
    // not a round number of seconds. Inserted in order so the list stays
    // monotonic.
    const asOption = String(seconds);
    const next = [...select.options].find((o) => Number(o.value) > seconds);
    select.add(new Option(`${si(seconds, 's', 3)}/div`, asOption), next || null);
    select.value = asOption;
  }

  /** Rebuild the timebase list for a block of `depth` samples.
   *
   * Depth comes from a capture rather than the capabilities: it is what the
   * daemon chose, and it is half of what sets the fastest reachable screen.
   */
  rebuildTimebaseChoices(depth) {
    const select = el('timebase');
    if (!select || depth === this.timebaseDepth) return;
    this.timebaseDepth = depth;

    const inUse = Number(select.value);
    select.replaceChildren();
    for (const value of timebaseChoices(this.capabilities, depth)) {
      select.append(new Option(`${si(value, 's', 3)}/div`, String(value)));
    }
    if (inUse > 0) this.showTimebase(inUse);
  }

  /** Set a channel's probe ratio, and follow it everywhere it reaches.
   *
   * Attenuation is not a display setting. Volts/div, the trigger level and
   * the samples are all at the probe tip, so changing the ratio changes which
   * scales the unit can reach and which input range a given scale maps onto.
   * The dropdown is rebuilt for the new ladder and the scale is read back
   * rather than assumed: the daemon may land on a different range, and a
   * panel showing the old one would be the lie this control is here to end.
   */
  async applyProbe(label, ratio, { push = true } = {}) {
    const state = this.channelState.get(label);
    if (!state || !(ratio > 0)) return;

    state.attenuation = ratio;
    this.showProbe(state, ratio);
    if (push) {
      await this.runCommand('set_probe', { ratio },
        `Channel ${label} ${ratio}x probe`, state.net, label);
    }
    this.rebuildScaleChoices(label);

    try {
      const body = await this.send('get_scale', {}, state.net);
      const scale = Number(body.value);
      if (Number.isFinite(scale) && scale > 0) {
        this.applyVoltsPerDiv(label, scale, { push: false });
      }
    } catch { /* keep showing the scale we had */ }
    this.requestRedraw();
  }

  /** Make a channel's probe dropdown display `ratio`, listed or not. */
  showProbe(state, ratio) {
    const select = state.probeSelect;
    if (!select) return;

    const asOption = String(ratio);
    if (![...select.options].some((o) => o.value === asOption)) {
      const next = [...select.options].find((o) => Number(o.value) > ratio);
      select.add(new Option(`${ratio}x`, asOption), next || null);
    }
    select.value = asOption;
  }

  /** Rebuild a channel's volts/div list for its current probe.
   *
   * A 10x probe multiplies every reachable setting by ten, so the offered
   * steps are not the same list. The value in use is kept, whether or not the
   * new list contains it, so a rebuild cannot silently change the scale.
   */
  rebuildScaleChoices(label) {
    const state = this.channelState.get(label);
    if (!state || !state.select) return;

    const inUse = state.voltsPerDiv;
    state.choices = voltsPerDivChoices(this.capabilities, state.attenuation);
    state.select.replaceChildren();
    for (const value of state.choices) {
      state.select.append(new Option(si(value, 'V', 2), String(value)));
    }
    state.select.append(new Option('Custom\u2026', CUSTOM_SCALE));
    this.showVoltsPerDiv(state, inUse);
  }

  /** Make a channel's dropdown display `voltsPerDiv`, offered or not. */
  showVoltsPerDiv(state, voltsPerDiv) {
    const select = state.select;
    if (!select) return;

    const asOption = String(voltsPerDiv);
    if (![...select.options].some((o) => o.value === asOption)) {
      // A value off the ladder -- from the custom field, or set by the CLI
      // while this page was open. Insert it in order rather than appending,
      // so the list stays monotonic and does not read as a bug.
      const option = new Option(si(voltsPerDiv, 'V', 2), asOption);
      const next = [...select.options].find(
        (o) => o.value !== CUSTOM_SCALE && Number(o.value) > voltsPerDiv);
      // Ahead of Custom when it belongs at the end, so Custom stays last.
      const custom = [...select.options].find((o) => o.value === CUSTOM_SCALE);
      select.add(option, next || custom || null);
    }
    select.value = asOption;
    if (state.customField) state.customField.hidden = true;
  }

  /** The net that addresses channel `index`, by the pin it is wired to.
   *
   * Scope nets carry a pin that is the channel, so channel A is the net on
   * pin 1, or pin "A" or "CH1" (see pinIndex). Position in the list is only
   * a fallback: nets come back in whatever order the box lists them, and a
   * box needn't define one net per channel -- with only `scope2` defined,
   * index 0 must not silently become channel B's net.
   */
  netForChannel(index) {
    const nets = this.channelNets || this.scopeNets || [];
    const byPin = nets.find((n) => pinIndex(n) === index);
    if (byPin) return byPin.name;
    // Position is a fallback only where no net declares a usable pin. If any
    // does, an unmatched channel genuinely has no net: with just `scope2`
    // (pin 2) defined, channel A must come back unwired rather than picking
    // up channel B's net and driving the wrong channel.
    const anyPinned = nets.some((n) => pinIndex(n) !== null);
    if (!anyPinned && nets[index]) return nets[index].name;
    return null;
  }

  /** Replace the assumed channel state with the hardware's own.
   *
   * Best-effort: a channel with no net cannot be asked, and a box that does
   * not know `get_net_enabled` yet (an older image) should leave the UI as
   * it was rather than blanking the controls.
   */
  async syncChannelState(labels) {
    await Promise.all(labels.map(async (label) => {
      const state = this.channelState.get(label);
      if (!state || !state.net) return;
      // Probe first: volts/div is at the probe tip, so the attenuation
      // decides which settings the unit can reach and therefore what the
      // dropdown should offer. The strips are built before this is known,
      // defaulting to 1x, so the list is rebuilt once the answer is in.
      try {
        const body = await this.send('get_probe', {}, state.net);
        const probe = Number(body.value);
        if (Number.isFinite(probe) && probe > 0) {
          state.attenuation = probe;
          this.showProbe(state, probe);
          this.rebuildScaleChoices(label);
        }
      } catch { /* keep the 1x list */ }
      try {
        const body = await this.send('get_coupling', {}, state.net);
        const coupling = String(body.value ?? '').toLowerCase();
        if (state.couplingSelect
            && COUPLINGS.some(([value]) => value === coupling)) {
          state.couplingSelect.value = coupling;
        }
      } catch { /* leave it showing DC, the default the daemon sets */ }
      try {
        const body = await this.send('get_net_enabled', {}, state.net);
        if (typeof body.value !== 'boolean') return;
        state.enabled = body.value;
        if (state.toggle) state.toggle.checked = body.value;
      } catch { /* older box, or the net is unreachable */ }
      try {
        const body = await this.send('get_scale', {}, state.net);
        const scale = Number(body.value);
        if (!Number.isFinite(scale) || scale <= 0) return;
        // push: false -- this is what the hardware already has, so sending it
        // back would re-range the channel for nothing. Off-ladder values are
        // added to the dropdown rather than dropped, so it never displays a
        // scale the trace is not drawn at.
        this.applyVoltsPerDiv(label, scale, { push: false });
      } catch { /* leave the built-in default showing */ }
    }));

    // These three belong to the scope, not to a channel, so they are read
    // from the scope net. They used to be read from whichever channel strip
    // happened to have a net wired first -- which gave the right answer, the
    // settings being device-wide however you reach them, but only by
    // accident, and it read as though the timebase were channel A's.
    if (!this.net) return;

    // Before the offset, which is clamped against whatever time/div the
    // control shows: reading the offset first would clamp it to a stale one.
    try {
      const body = await this.send('get_timebase', {}, this.net);
      const seconds = Number(body.value);
      if (Number.isFinite(seconds) && seconds > 0) this.showTimebase(seconds);
    } catch { /* leave the default showing */ }

    // The horizontal position outlives the page: the daemon holds it, and
    // every re-arm uses it. Read it back so a window left looking forward in
    // an earlier session is not shown as centred.
    try {
      const body = await this.send('get_time_offset', {}, this.net);
      const seconds = Number(body.value);
      if (Number.isFinite(seconds)) {
        // push: false -- the hardware is already there.
        this.applyTimePosition(seconds, { push: false });
      }
    } catch { /* older box: leave it centred */ }

    await this.syncTriggerState();

    // Cursors outlive the page the same way: the box holds them, so a pair
    // placed from the terminal before this page was opened is drawn on it.
    await this.refreshCursors(this.net);
  }

  /** Show the trigger the instrument actually has.
   *
   * Nothing read the trigger back at all, so the panel showed the values in
   * the HTML -- Auto, 0 V, channel A, rising -- however the scope was set.
   * That is worse than merely uninformative, because the capture mode is
   * written behind the panel's back from three places: Run sets Auto, Single
   * sets Single, and the daemon returns a completed single-shot to Normal. A
   * panel that never re-reads goes stale on its own, and then reports a mode
   * the scope is not in.
   */
  async syncTriggerState() {
    if (!this.net) return;

    const fields = [
      ['get_capture_mode', 'trigger-mode', (v) => String(v).toLowerCase()],
      ['get_trigger_source', 'trigger-source', (v) => String(v).toUpperCase()],
      ['get_trigger_slope', 'trigger-slope', (v) => String(v).toLowerCase()],
      ['get_trigger_level', 'trigger-level', (v) => Number(v)],
    ];

    for (const [action, id, coerce] of fields) {
      try {
        const body = await this.send(action, {}, this.net);
        const value = coerce(body.value);
        const control = el(id);
        if (!control) continue;
        if (control.tagName === 'SELECT') {
          // Only if the instrument named something the control offers. A
          // scope reporting a source this build has no option for should
          // leave the control alone rather than blank it.
          if ([...control.options].some((o) => o.value === value)) {
            control.value = value;
          }
        } else if (Number.isFinite(value)) {
          control.value = String(value);
        }
      } catch { /* older box: leave the control showing what it had */ }
    }
  }

  /** Move the capture window earlier or later than the trigger.
   *
   * Unlike the vertical position this cannot be done in the renderer. The
   * capture already fills the screen, so signal past either edge was never
   * sampled -- there is nothing on hand to pan to. Seeing it means asking the
   * scope for a window in a different place, which is the pre/post-trigger
   * split, and that means a fresh capture. Positive seconds look forward, to
   * signal later than the trigger; negative look back before it.
   *
   * Held to HORIZONTAL_LIMIT divisions of the timebase. Resolves to the
   * seconds applied.
   */
  async applyTimePosition(seconds, { push = true, announce = true } = {}) {
    if (!Number.isFinite(seconds)) return null;
    const perDiv = Number(el('timebase').value) || 0;
    const limit = HORIZONTAL_LIMIT * perDiv;
    // A readback is shown as the box has it, in range or not: clamping it
    // here would show a window the scope is not capturing.
    const value = push && limit > 0 ? clamp(seconds, -limit, limit) : seconds;
    if (announce && value !== seconds) {
      this.console.note(`Horizontal position limited to ${si(value, 's', 3)}: `
        + `${HORIZONTAL_LIMIT} divisions at ${si(perDiv, 's', 3)}/div puts the trigger on the edge`);
    }
    this.timePositionS = value;
    if (this.timePositionField) this.timePositionField.show(value);
    this.requestRedraw();
    if (!push) return value;
    await this.runCommand('set_time_offset', { offset: value },
      `hpos ${si(value, 's', 3)}`);
    return value;
  }

  showCapabilityNotes(caps) {
    const notes = [];
    if (caps.max_sample_rate_hz) notes.push(`Max sample rate ${si(caps.max_sample_rate_hz, 'S/s', 3)}`);
    if (caps.bandwidth_hz) notes.push(`Bandwidth ${si(caps.bandwidth_hz, 'Hz', 3)}`);
    if (caps.max_memory_samples) notes.push(`Memory ${caps.max_memory_samples.toLocaleString()} samples`);
    if (caps.resolution) notes.push(`${caps.resolution.current_bits}-bit resolution`);
    if (caps.digital_ports) notes.push(`${caps.digital_ports} digital port(s)`);
    if (caps.signal_generator) notes.push('Built-in signal generator');
    if (caps.rapid_block) notes.push('Rapid block capture');
    if (caps.streaming_mode) notes.push('Continuous streaming');

    const group = el('capability-notes');
    const list = el('capability-list');
    list.replaceChildren();
    for (const note of notes) {
      const item = document.createElement('li');
      item.textContent = note;
      list.appendChild(item);
    }
    group.hidden = notes.length === 0;
  }

  // ---------- transport ----------
  /** Whether the capture stream is open, as opposed to closed or still opening. */
  streamOpen() {
    return Boolean(this.socket) && this.socket.readyState === 1;
  }

  async connect() {
    // An attempt under way counts as a connection: the button reads Connect
    // until the socket opens, and a second press, or a double click, opened
    // a second socket streaming into the same page.
    if (this.socket || this.connecting) return;
    if (!this.net) {
      this.console.error('No scope net selected.');
      return;
    }
    const attempt = this.connectAttempt + 1;
    this.connectAttempt = attempt;
    this.connecting = true;
    this.setLink('connecting', 'link--down');

    // Tickets expire, so fetch a fresh one per connection rather than
    // reusing the one from page load.
    const ticket = await this.loadCapabilities();
    // Disconnected, or another net picked, while the ticket was on its way.
    if (attempt !== this.connectAttempt) return;
    if (!ticket) {
      this.connecting = false;
      this.setLink('disconnected', 'link--down');
      return;
    }

    const url = new URL(ticket.ws_path, window.location.href);
    url.protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';

    const socket = new WebSocket(url);
    socket.binaryType = 'arraybuffer';
    this.socket = socket;

    // Every listener first checks that its socket is still the page's: one
    // that has been replaced can still deliver, and its late close marked
    // the next connection closed.
    socket.addEventListener('open', () => {
      if (socket !== this.socket) {
        socket.close();
        return;
      }
      this.connecting = false;
      this.setLink('connected', 'link--up');
      el('connect').textContent = 'Disconnect';
      this.console.write('Capture stream connected.', 'note');
      // Captures are off until asked for, so that a control-only client is
      // not sent the stream. This is the client that wants it -- paced by
      // credit, and with the state pushed after every change.
      this.owedCredits = 0;
      this.pendingBuffer = null;
      this.clockOffsets = [];
      this.clockOffset = null;
      this.resetRollDelay();
      this.streamFps = Math.round(Math.min(120, Math.max(30, this.refreshHz)));
      this.subscribedAt = performance.now();
      socket.send(JSON.stringify({
        command: 'Subscribe', credits: render.CREDIT_WINDOW,
        max_fps: this.streamFps, state: true,
      }));
    });

    socket.addEventListener('message', (event) => {
      if (socket !== this.socket) return;
      if (typeof event.data === 'string') {
        this.onControlMessage(event.data);
      } else {
        this.onCapture(event.data);
      }
    });

    socket.addEventListener('error', () => {
      if (socket !== this.socket) return;
      this.setLink('error', 'link--error');
    });

    socket.addEventListener('close', () => {
      if (socket !== this.socket) return;
      this.socket = null;
      this.connecting = false;
      this.streamEnded();
    });
  }

  disconnect() {
    // Before the early return: the timer outlives the socket otherwise, and
    // goes on taking a capture every half second against a scope nobody is
    // watching.
    this.stopMeasurementPolling();
    // An attempt still waiting on its ticket sees this and opens nothing.
    this.connectAttempt += 1;
    const socket = this.socket;
    const wasConnecting = this.connecting;
    this.socket = null;
    this.connecting = false;
    if (socket) {
      // Unsubscribe only where it can be sent: a socket still connecting
      // throws on send. Closing that one abandons the handshake, so it does
      // not open afterwards and stream into a page that let it go.
      if (socket.readyState === 1) {
        try {
          socket.send(JSON.stringify({ command: 'Unsubscribe' }));
        } catch { /* closing anyway */ }
      }
      socket.close();
    }
    if (socket || wasConnecting) this.streamEnded();
  }

  /** Show the stream as gone, whichever end closed it. */
  streamEnded() {
    this.setLink('disconnected', 'link--down');
    el('connect').textContent = 'Connect';
    // A capture still waiting for its animation frame is not drawn, and its
    // credit is not owed to a socket that has gone.
    this.pendingBuffer = null;
    this.owedCredits = 0;
    this.showNotStreaming(true);
    this.showIdleRate();
  }

  /** Read zero once captures stop, rather than the last rate measured. */
  showIdleRate() {
    // The rate is only recomputed as frames arrive, so with none coming the
    // header went on claiming the scope was capturing.
    this.rate = 0;
    this.fps = 0;
    this.captureCount = 0;
    this.drawnCount = 0;
    this.lastRateAt = performance.now();
    const stat = el('stat-rate');
    if (stat) stat.textContent = '0 cap/s';
  }

  /** Show or take down the overlay saying nothing is streaming. */
  showNotStreaming(visible) {
    if (this.overlayHidden === !visible) return;
    const overlay = el('plot-empty');
    if (overlay) overlay.hidden = !visible;
    this.overlayHidden = !visible;
  }

  onControlMessage(text) {
    let message;
    try {
      message = JSON.parse(text);
    } catch {
      this.console.error(`Unparseable message from scope: ${text.slice(0, 120)}`);
      return;
    }
    const response = message.Response || message;
    if (response.response === 'State' && response.state) {
      this.applyState(response.state);
      return;
    }
    if (response.response === 'Subscribed' && this.subscribedAt) {
      // The reply's round trip is the link's, near enough: the daemon
      // answers it without touching the hardware. Top the credit up to what
      // that round trip needs, which on a slow link is more than the start.
      const rtt = performance.now() - this.subscribedAt;
      this.subscribedAt = null;
      const window = render.creditWindow(rtt, this.streamFps || 60);
      if (window > render.CREDIT_WINDOW && this.socket) {
        this.socket.send(JSON.stringify({
          command: 'Credit', count: window - render.CREDIT_WINDOW,
        }));
      }
      return;
    }
    if (response.response === 'Error') {
      // The daemon reports dropped captures this way; it is a warning about
      // the display, not a failed command.
      const kind = /dropped \d+ captures/.test(response.message || '') ? 'note' : 'error';
      this.console.write(response.message, kind);
    }
  }

  onCapture(buffer) {
    // Kept encoded until the animation frame: the newest wins, and one
    // replaced before it is drawn is never decoded at all.
    this.pendingBuffer = buffer;
    this.pendingArrival = performance.now();
    this.owedCredits += 1;
  }

  /** Fold a frame's arrival into the page-to-box clock mapping.
   *
   * Both clocks are monotonic, so arrival minus capture is a constant offset
   * plus that frame's transit. The smallest over recent frames is the offset
   * plus the quickest transit, which is the mapping a rolling screen needs.
   */
  noteArrival(frame, arrival) {
    const offset = arrival - frame.captureMonoNs / 1e6;
    this.clockOffsets.push(offset);
    if (this.clockOffsets.length > 240) this.clockOffsets.shift();
    this.clockOffset = Math.min(...this.clockOffsets);

    if (!frame.streaming) {
      this.rollLastEnd = null;
      return;
    }
    // How stale the screen on display had become by the time this one came:
    // the delay that would have kept its right edge on arrived samples. The
    // worst of the last few seconds, with a margin, is the delay to draw at.
    const end = frame.captureMonoNs / 1e6;
    if (this.rollLastEnd !== null) {
      this.rollNeeds.push([arrival, arrival - this.clockOffset - this.rollLastEnd]);
      while (arrival - this.rollNeeds[0][0] > 3000) this.rollNeeds.shift();
      let worst = 0;
      for (const [, need] of this.rollNeeds) if (need > worst) worst = need;
      this.rollTarget = worst + render.ROLL_DELAY_MARGIN_MS;
    }
    this.rollLastEnd = end;
  }

  resetRollDelay() {
    this.rollNeeds = [];
    this.rollLastEnd = null;
    this.rollTarget = render.ROLL_DELAY_MS;
    this.rollDelay = NaN;
    this.rollDelayAt = null;
  }

  /** Whether captures are still coming: the stream open and the scope running. */
  capturesLive() {
    return this.streamOpen() && !(this.state && this.state.acquiring === false);
  }

  /** The sample range of a rolling frame to draw now, behind live. */
  rollView(frame) {
    const now = performance.now();
    const live = this.capturesLive();
    // A held screen waits for a newer frame: Run does not move the old one.
    if (live && this.rollHold && this.rollHold.frame !== frame) this.rollHold = null;
    if (!this.rollHold) {
      this.rollDelay = render.nextRollDelay(this.rollDelay, this.rollTarget,
        this.rollDelayAt === null ? 0 : now - this.rollDelayAt);
      this.rollDelayAt = now;
    }
    const pairMs = (frame.sampleIntervalNs * 2) / 1e6;
    const screen = frame.screen;
    // No further behind than the history reaches, or the left edge empties.
    const historyMs = ((frame.samplesPerChannel - screen) / 2) * pairMs;
    const delay = Math.min(this.rollDelay, historyMs);
    const end = frame.captureMonoNs / 1e6;
    if (!live && (!this.rollHold || this.rollHold.frame !== frame)) {
      // Stopped, or with the stream gone, nothing will fill the right edge,
      // and drawn against the clock the screen scrolled on into blank. It
      // goes as far as this frame's newest samples and is held there.
      this.rollHold = { frame, at: end + this.clockOffset + delay };
    }
    const at = this.rollHold ? Math.min(now, this.rollHold.at) : now;
    return render.rollWindow(frame.samplesPerChannel, screen, pairMs, end,
      at - this.clockOffset, delay);
  }

  /** Account for a frame that is about to be drawn. */
  noteFrame(frame) {
    this.drawnCount += 1;
    // Captures between this frame and the last, from their sequence numbers:
    // the rate the scope is capturing at, as opposed to the rate drawn.
    if (this.frameSeq !== null && frame.seq > this.frameSeq) {
      this.captureCount += frame.seq - this.frameSeq;
    }
    this.frameSeq = frame.seq;

    const now = performance.now();
    if (now - this.lastRateAt >= 500) {
      const seconds = (now - this.lastRateAt) / 1000;
      this.rate = this.captureCount / seconds;
      this.fps = this.drawnCount / seconds;
      this.captureCount = 0;
      this.drawnCount = 0;
      this.lastRateAt = now;
      this.updateStats(frame);
    }
  }

  updateStats(frame) {
    el('stat-rate').textContent = frame.streaming
      ? `${(this.fps || 0).toFixed(0)} fps rolling`
      : `${this.rate.toFixed(0)} cap/s \u00b7 ${(this.fps || 0).toFixed(0)} fps`;
    const interval = frame.envelope ? frame.sampleIntervalNs * 2 : frame.sampleIntervalNs;
    el('stat-rate-samples').textContent = frame.envelope
      ? `${si(1e9 / interval, 'pts/s', 3)}` : si(1e9 / interval, 'S/s', 3);
    const points = frame.screen !== undefined ? frame.screen : frame.samplesPerChannel;
    el('stat-latency').textContent = `${points.toLocaleString()} pts`;
    // The capture says how deep a block is, which with the unit's fastest
    // interval is what bounds the reachable timebases. Only known once one
    // has arrived, so the list is trimmed here rather than at connect -- and
    // only from a block: a rolling screen's length is its column count.
    if (!frame.streaming) this.rebuildTimebaseChoices(frame.samplesPerChannel);
  }

  /** Return credit for every frame received since the last return. */
  returnCredits() {
    if (!this.owedCredits || !this.socket) return;
    if (this.socket.readyState !== undefined && this.socket.readyState !== 1) return;
    try {
      this.socket.send(JSON.stringify({ command: 'Credit', count: this.owedCredits }));
      this.owedCredits = 0;
    } catch { /* the close handler reports it */ }
  }

  setLink(text, className) {
    const link = el('link');
    link.textContent = text;
    link.className = `link ${className}`;
  }

  // ---------- commands ----------
  /**
   * Send one command through the same REST endpoint the terminal CLI uses.
   * Returns the parsed body so the console can report it.
   */
  async send(action, params, net) {
    // `net` overrides the selected one. A scope channel is addressed BY net
    // on this box -- each scope net carries a pin, and the device behind it
    // is bound to that channel -- so a per-channel control has to talk to
    // its own net. Sending to the selected net instead made channel B's
    // toggle and volts/div silently drive whichever channel was selected.
    const target = this.netForAction(action, net);
    if (!target) throw new Error('no scope net selected');
    const response = await fetch('/net/command', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ netname: target, action, params: params || {} }),
    });
    let body = {};
    try {
      body = await response.json();
    } catch { /* non-JSON error page */ }
    if (!response.ok) {
      throw new Error(body.error || `${action} failed (${response.status})`);
    }
    return body;
  }

  async runCommand(action, params, summary, net, label) {
    try {
      const body = await this.send(action, params, net);
      // The box's reply says what changed but not on which channel, and a
      // console command may not have named one.
      const reply = body.message || `${summary || action}: ok`;
      const named = label && reply.toLowerCase().includes(`channel ${label.toLowerCase()}`);
      this.console.write(label && !named ? `channel ${label}: ${reply}` : reply);
      if (action === 'capabilities' && body.value) {
        this.capabilities = body.value;
        this.applyCapabilities();
      }
      // Cursors are typed, not dragged, so the console is the only thing that
      // moves them and the plot has to follow it. The reply is the box's own
      // answer for where they ended up rather than an echo of the request --
      // `set_cursor` reports what it stored -- so the plot takes it directly
      // instead of asking again.
      if (CURSOR_ACTIONS.has(action)) this.adoptCursors(body.value);
      return body;
    } catch (e) {
      this.console.error(e.message);
      return null;
    }
  }

  // ---------- cursors ----------

  /** Take the box's cursors as the plot's, from any of the cursor replies.
   *
   * `set_cursor` and `get_cursor` answer with the positions; `measure_cursor`
   * wraps them alongside the readings; `clear_cursor` answers with nothing.
   * All four end up here, so there is one place that decides what is drawn.
   *
   * An unset pair becomes null rather than an object of nulls, so the
   * renderer has a single thing to test.
   */
  adoptCursors(value) {
    const cursors = value && (value.cursors || value);
    this.cursors = (cursors && (cursors.time || cursors.volts)) ? cursors : null;
    this.requestRedraw();
  }

  /** Read the box's cursors, for a page that has just connected.
   *
   * `get_cursor` rather than `measure_cursor`: the positions are all the
   * renderer needs, and the readings come off the frame already in hand,
   * which keeps the labels on the trace being drawn instead of on a separate
   * capture taken a moment later.
   */
  async refreshCursors(net) {
    try {
      const body = await this.send('get_cursor', {}, net || this.net);
      this.adoptCursors(body.value);
    } catch {
      this.adoptCursors(null); // Older box with no cursor actions.
    }
  }

  /** The channel the measurements panel reads; see firstMeasurableChannel(). */
  measuredChannel() {
    return firstMeasurableChannel(this.channelState);
  }

  // ---------- measurements ----------
  async refreshMeasurements() {
    const host = el('measurements');
    if (!this.net) return;

    // Measurements are per channel, so the panel needs one -- the dropdown
    // now selects the scope, which has no channel of its own. It reads the
    // first that is switched on rather than a fixed channel A, so switching
    // A off moves the panel to B instead of leaving it stuck on a dead
    // channel, and it says which one it settled on.
    const measured = this.measuredChannel();
    if (!measured) {
      host.replaceChildren();
      const p = document.createElement('p');
      p.className = 'dim';
      p.textContent = 'No channel is on. Switch one on to measure.';
      host.appendChild(p);
      return;
    }

    try {
      // One request, one capture, the whole set. Reading them one action at a
      // time cost a capture each and mixed moments of a live signal together,
      // so the panel could show a Vpp that was not Vmax - Vmin.
      const body = await this.send('measure_all', {}, measured.net);
      const values = body.value || {};
      host.replaceChildren();
      const heading = document.createElement('p');
      heading.className = 'measurements__channel';
      const swatch = document.createElement('span');
      swatch.className = 'channel__swatch';
      swatch.style.background = channelColorVar(measured.label);
      heading.append(swatch, document.createTextNode(`Channel ${measured.label}`));
      host.append(heading);
      for (const [label, key, unit] of MEASUREMENTS) {
        const dt = document.createElement('dt');
        dt.textContent = label;
        const dd = document.createElement('dd');
        const value = values[key];
        // Absent rather than zero: a DC level has no period and a clean
        // square wave has no overshoot. Kept in the list, with a dash, so
        // the panel does not reshuffle as quantities come and go.
        if (value === undefined || value === null) {
          dd.textContent = '\u2014';
          dd.className = 'dim';
        } else {
          dd.textContent = si(value, unit, 4);
        }
        host.append(dt, dd);
      }
    } catch (e) {
      host.replaceChildren();
      const p = document.createElement('p');
      p.className = 'dim';
      p.textContent = e.message;
      host.appendChild(p);
    }
  }

  /** Keep the measurement panel following the signal while it runs.
   *
   * A bench scope updates its readouts continuously; this panel only read
   * once, on Start, so every value on screen described a capture from
   * whenever that was. Slower than the frame rate on purpose: each refresh
   * is a capture and a round trip, and a number that changes ten times a
   * second cannot be read anyway.
   */
  startMeasurementPolling() {
    this.stopMeasurementPolling();
    this.pollMeasurementsOnce();
    this.measureTimer = setInterval(() => this.pollMeasurementsOnce(),
      MEASURE_INTERVAL_MS);
  }

  /** One reading, skipped if the last one has not come back yet.
   *
   * The single path for every unawaited refresh -- the first one, each tick,
   * and the one on returning to the tab -- because each needs the same two
   * guards and getting either wrong is invisible until the panel stops.
   *
   * Overlapping requests would queue captures behind each other on a slow
   * trigger and arrive out of order. And a rejection has to be absorbed:
   * `finally` passes one through, so a refresh that threw would otherwise
   * escape as an unhandled rejection. The panel reports its own errors in
   * place, so there is nothing to do with one here.
   */
  pollMeasurementsOnce() {
    if (this.measureInFlight) return;
    this.measureInFlight = true;
    this.refreshMeasurements()
      .finally(() => { this.measureInFlight = false; })
      .catch(() => {});
  }

  stopMeasurementPolling() {
    if (this.measureTimer) clearInterval(this.measureTimer);
    this.measureTimer = null;
  }

  // ---------- drawing ----------
  observeCanvas() {
    const resize = () => {
      const ratio = window.devicePixelRatio || 1;
      const { clientWidth, clientHeight } = this.canvas;
      // Size the backing store to device pixels so the trace is not blurry
      // on a HiDPI display.
      this.canvas.width = Math.max(1, Math.floor(clientWidth * ratio));
      this.canvas.height = Math.max(1, Math.floor(clientHeight * ratio));
      this.ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      this.dirty = true;
    };
    new ResizeObserver(resize).observe(this.canvas);
    resize();
  }

  tick(timestamp) {
    if (timestamp !== undefined && this.frameTimes) {
      this.frameTimes.push(timestamp);
      if (this.frameTimes.length > 31) this.frameTimes.shift();
      if (this.frameTimes.length === 31) {
        const gaps = this.frameTimes.slice(1).map((t, i) => t - this.frameTimes[i]).sort((a, b) => a - b);
        const median = gaps[15];
        if (median > 0) this.refreshHz = 1000 / median;
      }
    }
    if (this.pendingBuffer) {
      const buffer = this.pendingBuffer;
      this.pendingBuffer = null;
      try {
        this.latest = decode(buffer);
        this.dirty = true;
        // Here, on a capture that has just arrived, rather than on drawing:
        // the last capture is redrawn after the stream has gone, and that
        // took the overlay saying so down again.
        this.showNotStreaming(false);
        this.noteFrame(this.latest);
        if (this.pendingArrival !== undefined) this.noteArrival(this.latest, this.pendingArrival);
      } catch (e) {
        if (!this.decodeFailed) {
          this.decodeFailed = true;
          this.console.error(`Bad capture frame: ${e.message}`);
        }
      }
    }
    if (this.dirty && this.latest) {
      // Guarded because the next frame is scheduled below: an exception
      // escaping here would take the whole render loop with it, and every
      // control would go dead at once with nothing on screen to say why.
      // One malformed capture should cost one frame.
      try {
        this.draw(this.latest);
      } catch (e) {
        if (!this.drawFailed) {
          this.drawFailed = true;
          this.console.error(`Could not draw the capture: ${e.message}`);
        }
      }
      this.dirty = false;
    }
    // After drawing, not on arrival: credit returned per frame drawn is what
    // paces the stream to the display, and a hidden tab, which gets no
    // animation frames, stops being sent frames at all.
    if (this.returnCredits) this.returnCredits();
    // A rolling screen moves between frames, so it is drawn every time,
    // until it comes to rest where it stopped (see rollView).
    const resting = this.rollHold && performance.now() >= this.rollHold.at;
    if (this.latest && this.latest.streaming && !resting) this.dirty = true;
    requestAnimationFrame((t) => this.tick(t));
  }

  draw(frame) {
    const ctx = this.ctx;
    const ratio = window.devicePixelRatio || 1;
    const width = this.canvas.width / ratio;
    const height = this.canvas.height / ratio;
    const display = this.display || {};

    ctx.clearRect(0, 0, width, height);

    // The spectrum pane takes the lower part of the plot when it is on.
    const spectrum = display.fft && display.fft.channel ? display.fft : null;
    const plotHeight = spectrum ? Math.round(height * 0.62) : height;

    if (display.xy) {
      this.drawXY(ctx, frame, width, plotHeight);
    } else {
      this.drawTimeDomain(ctx, frame, width, plotHeight);
    }
    if (spectrum) this.drawSpectrum(ctx, frame, spectrum, plotHeight, width, height - plotHeight);
    this.updateTriggerStatus(frame);
  }

  /** The channels against time, with everything drawn over them. */
  drawTimeDomain(ctx, frame, width, height) {
    const display = this.display || {};
    this.drawGraticule(ctx, width, height);

    // The part of the record across the screen: all of it, or the zoomed
    // window. Held so the cursors and markers use the same mapping.
    const intervalS = (frame.envelope ? frame.sampleIntervalNs * 2 : frame.sampleIntervalNs) / 1e9;
    let view;
    if (frame.streaming && this.clockOffset !== null) {
      view = this.rollView(frame);
    } else if (frame.envelope) {
      const screen = frame.screen;
      view = { start: frame.samplesPerChannel - screen, end: frame.samplesPerChannel };
    } else {
      view = render.zoomWindow(frame.samplesPerChannel, frame.preTriggerSamples,
        frame.sampleIntervalNs / 1e9, display.zoom);
    }
    this.currentView = view;

    if (display.persistence) {
      this.drawPersistence(ctx, frame, view, width, height, display.persistence);
    } else if (this.persist) {
      this.persist = null;
    }

    const overflowed = [];
    frame.channels.forEach((descriptor, index) => {
      this.drawTrace(ctx, frame, index, view, width, height,
        this.channelColor(descriptor.channel));
      if (frame.overflowed && frame.overflowed(index)) overflowed.push(descriptor.channel);
    });
    if (display.math) this.drawMath(ctx, frame, view, width, height, display.math);

    // Clipping silently distorts every measurement taken from the capture,
    // so it has to be visible rather than inferred from a flat top.
    const warning = el('overflow-warning');
    const message = overflowed.length
      ? `Channel ${overflowed.join(', ')} clipped \u2014 increase volts/div` : '';
    if (warning && warning.textContent !== message) {
      warning.textContent = message;
      warning.hidden = !message;
    }

    if (this.showTriggerMarkers) {
      // The time marker needs a triggered capture to mean anything -- in auto
      // mode an untriggered one has no trigger point to mark -- but the level
      // is a setting rather than a property of the capture, so it is drawn
      // either way. That is the case that matters: when nothing is
      // triggering, the level is exactly what you want to see.
      if ((frame.flags & FLAG_TRIGGERED) && !frame.streaming) {
        this.drawTriggerMarker(ctx, frame, width, height);
      }
      if (!frame.streaming) this.drawTriggerLevel(ctx, width, height);
    }

    // Last, so the readout box sits over the trace rather than under it.
    if (this.cursors) this.drawCursors(ctx, frame, width, height);
    if (display.zoom && !frame.envelope) this.drawZoomOverview(ctx, frame, view, width, intervalS);
  }

  /** The channel colours and then math's, read from the stylesheet once rather than per frame. */
  palette() {
    if (!this.colors) {
      const styles = getComputedStyle(document.documentElement);
      this.colors = [...CHANNEL_COLORS, MATH_COLOR].map(
        (name) => styles.getPropertyValue(name).trim() || '#9fe870');
    }
    return this.colors;
  }

  /** Channel `label`'s colour: the same one its strip, trace and readouts use. */
  channelColor(label) {
    return this.palette()[colorSlot(label)];
  }

  mathColor() {
    return this.palette()[CHANNEL_COLORS.length];
  }

  /** Vertical mapping for a channel: volts to y, with its scale and position. */
  channelMapping(label, height) {
    const state = this.channelState.get(label);
    const fullScale = ((state && state.voltsPerDiv) || 1) * 4; // 8 divisions, centre at zero.
    // The channel's vertical position, converted from divisions to the
    // units the plot works in: half the screen is four divisions, so a
    // division is a quarter of it.
    const shift = positionDivisions(state) / 4;
    const half = height / 2;
    return (volts) => half - (volts / fullScale + shift) * half;
  }

  /**
   * One channel, as a vertical span per pixel column.
   *
   * With 8000 samples across ~1000 px, plotting every sample would draw the
   * same column many times and lose the peaks; the lowest and highest of
   * each column keep the envelope, which is what makes a narrow glitch
   * visible at all. Zoomed in past a sample per column, it joins the samples
   * instead, and marks them once they are far enough apart to tell apart.
   */
  drawTrace(ctx, frame, index, view, width, height, color) {
    const descriptor = frame.channels[index];
    const counts = frame.counts(index);
    const scale = descriptor.scaleVPerCount;
    const offset = descriptor.offsetV;
    const toY = this.channelMapping(descriptor.channel, height);
    const span = view.end - view.start;
    const columns = Math.max(1, Math.floor(width));

    ctx.strokeStyle = color;
    ctx.lineWidth = 1.25;
    ctx.beginPath();

    if (!frame.envelope && span < columns) {
      // Fewer samples than columns: a line through the samples.
      const first = Math.max(0, Math.floor(view.start));
      const last = Math.min(counts.length - 1, Math.ceil(view.end));
      const pixelsPerSample = width / span;
      for (let i = first; i <= last; i += 1) {
        const x = ((i - view.start) / span) * width;
        const y = toY(counts[i] * scale + offset);
        if (i === first) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      }
      ctx.stroke();
      if (pixelsPerSample >= 6) {
        ctx.fillStyle = color;
        for (let i = first; i <= last; i += 1) {
          const x = ((i - view.start) / span) * width;
          ctx.fillRect(x - 1.5, toY(counts[i] * scale + offset) - 1.5, 3, 3);
        }
      }
      return;
    }

    if (this.extremes.min.length < columns) {
      this.extremes = { min: new Float32Array(columns), max: new Float32Array(columns) };
    }
    render.columnExtremes(this.extremes, counts, view.start, view.end, columns, frame.envelope);
    const { min, max } = this.extremes;
    let drawing = false;
    for (let column = 0; column < columns; column += 1) {
      if (Number.isNaN(min[column])) {
        // Not captured yet: the unfilled part of a rolling screen.
        drawing = false;
        continue;
      }
      const yMax = toY(max[column] * scale + offset);
      const yMin = toY(min[column] * scale + offset);
      if (!drawing) {
        ctx.moveTo(column, yMax);
        drawing = true;
      }
      ctx.lineTo(column, yMax);
      ctx.lineTo(column, yMin);
    }
    ctx.stroke();
  }

  /** A trace computed from two channels, sample by sample. */
  drawMath(ctx, frame, view, width, height, math) {
    const parsed = render.parseMath(math.expr);
    const a = parsed ? frame.channelIndex(parsed.left) : -1;
    const b = parsed ? frame.channelIndex(parsed.right) : -1;
    if (a < 0 || b < 0 || frame.envelope) {
      this.drawNote(ctx, width, height, frame.envelope
        ? 'Math is not drawn in roll mode'
        : `Math ${math.expr} needs channels ${parsed ? `${parsed.left} and ${parsed.right}` : ''} on`);
      return;
    }
    const ca = frame.counts(a);
    const cb = frame.counts(b);
    const da = frame.channels[a];
    const db = frame.channels[b];
    // On the first channel's scale for a sum or difference; a product is in
    // volts squared, drawn at the product of the two scales.
    const perDiv = (label) => ((this.channelState.get(label) || {}).voltsPerDiv || 1);
    const scale = parsed.op === '*' ? perDiv(parsed.left) * perDiv(parsed.right)
      : Math.max(perDiv(parsed.left), perDiv(parsed.right));
    const toY = (v) => height / 2 - (v / (scale * 4)) * (height / 2);
    const columns = Math.max(1, Math.floor(width));
    const span = view.end - view.start;
    const perColumn = span / columns;

    ctx.strokeStyle = this.mathColor();
    ctx.lineWidth = 1.25;
    ctx.beginPath();
    for (let column = 0; column < columns; column += 1) {
      const from = Math.floor(view.start + column * perColumn);
      const to = Math.min(ca.length, Math.floor(view.start + (column + 1) * perColumn) + 1);
      let low = Infinity;
      let high = -Infinity;
      for (let i = from; i < to; i += 1) {
        const v = render.combine(parsed.op,
          ca[i] * da.scaleVPerCount + da.offsetV, cb[i] * db.scaleVPerCount + db.offsetV);
        if (v < low) low = v;
        if (v > high) high = v;
      }
      if (low === Infinity) continue;
      if (column === 0) ctx.moveTo(column, toY(high));
      ctx.lineTo(column, toY(high));
      ctx.lineTo(column, toY(low));
    }
    ctx.stroke();

    const unit = parsed.op === '*' ? 'V\u00b2' : 'V';
    this.drawNote(ctx, width, height,
      `M ${parsed.left}${parsed.op}${parsed.right}  ${si(scale, unit, 2)}/div`, 'right');
  }

  /** Channel B against channel A. */
  drawXY(ctx, frame, width, height) {
    this.drawGraticule(ctx, width, height);
    if (frame.channels.length < 2) {
      this.drawNote(ctx, width, height, 'XY needs two channels on');
      return;
    }
    const [da, db] = frame.channels;
    const ca = frame.counts(0);
    const cb = frame.counts(1);
    const stateA = this.channelState.get(da.channel) || {};
    const stateB = this.channelState.get(db.channel) || {};
    // A across ten divisions, B up eight, each at its own volts/div and moved
    // by its own position.
    const halfW = width / 2;
    const halfH = height / 2;
    const xOf = (v) => halfW + (v / ((stateA.voltsPerDiv || 1) * 5) + positionDivisions(stateA) / 5) * halfW;
    const yOf = (v) => halfH - (v / ((stateB.voltsPerDiv || 1) * 4) + positionDivisions(stateB) / 4) * halfH;

    const target = this.display.persistence ? this.persistLayer(width, height) : null;
    const draw = (context) => {
      context.strokeStyle = this.channelColor(da.channel);
      context.lineWidth = 1;
      context.beginPath();
      const step = frame.envelope ? 2 : 1;
      let moved = false;
      for (let i = 0; i < ca.length; i += step) {
        if (ca[i] === NO_SAMPLE || cb[i] === NO_SAMPLE) { moved = false; continue; }
        const x = xOf(ca[i] * da.scaleVPerCount + da.offsetV);
        const y = yOf(cb[i] * db.scaleVPerCount + db.offsetV);
        if (!moved) { context.moveTo(x, y); moved = true; } else context.lineTo(x, y);
      }
      context.stroke();
    };
    if (target) {
      this.fadePersistence(this.display.persistence);
      draw(target.ctx);
      ctx.drawImage(target.canvas, 0, 0, width, height);
    } else {
      draw(ctx);
    }
    this.drawNote(ctx, width, height,
      `X ${da.channel} ${si(stateA.voltsPerDiv || 1, 'V', 2)}/div   Y ${db.channel} ${si(stateB.voltsPerDiv || 1, 'V', 2)}/div`);
  }

  /** The offscreen layer traces accumulate on, sized to the plot. */
  persistLayer(width, height) {
    const ratio = window.devicePixelRatio || 1;
    const w = Math.max(1, Math.floor(width * ratio));
    const h = Math.max(1, Math.floor(height * ratio));
    if (!this.persist || this.persist.canvas.width !== w || this.persist.canvas.height !== h) {
      const canvas = typeof OffscreenCanvas !== 'undefined'
        ? new OffscreenCanvas(w, h) : Object.assign(document.createElement('canvas'), { width: w, height: h });
      const context = canvas.getContext('2d');
      context.setTransform(ratio, 0, 0, ratio, 0, 0);
      this.persist = { canvas, ctx: context, at: performance.now() };
    }
    return this.persist;
  }

  /** Fade the persistence layer by the time since it was last faded. */
  fadePersistence(seconds) {
    const layer = this.persist;
    const now = performance.now();
    const fade = render.persistenceFade(seconds, now - layer.at);
    if (fade < render.MIN_FADE_STEP) return;
    layer.at = now;
    if (fade > 0) {
      layer.ctx.save();
      layer.ctx.setTransform(1, 0, 0, 1, 0, 0);
      layer.ctx.globalCompositeOperation = 'destination-out';
      layer.ctx.fillStyle = `rgba(0, 0, 0, ${Math.min(1, fade)})`;
      layer.ctx.fillRect(0, 0, layer.canvas.width, layer.canvas.height);
      layer.ctx.restore();
    }
  }

  /** Earlier traces, fading, under the live one. */
  drawPersistence(ctx, frame, view, width, height, seconds) {
    const layer = this.persistLayer(width, height);
    this.fadePersistence(seconds);
    layer.ctx.globalAlpha = 0.55;
    frame.channels.forEach((descriptor, index) => {
      this.drawTrace(layer.ctx, frame, index, view, width, height,
        this.channelColor(descriptor.channel));
    });
    layer.ctx.globalAlpha = 1;
    ctx.drawImage(layer.canvas, 0, 0, width, height);
  }

  /** Where the zoomed window sits in the whole record, along the top. */
  drawZoomOverview(ctx, frame, view, width, intervalS) {
    const total = frame.samplesPerChannel || 1;
    const x0 = (view.start / total) * width;
    const x1 = (view.end / total) * width;
    ctx.save();
    ctx.fillStyle = '#1c2430';
    ctx.fillRect(0, 0, width, 5);
    ctx.fillStyle = '#d29922';
    ctx.fillRect(x0, 0, Math.max(2, x1 - x0), 5);
    ctx.font = '11px ui-monospace, monospace';
    ctx.fillStyle = '#8b98a5';
    ctx.textBaseline = 'top';
    const factor = this.display.zoom.factor;
    const span = (view.end - view.start) * intervalS;
    ctx.fillText(`zoom \u00d7${factor}  ${si(span / 10, 's', 3)}/div`, 4, 8);
    ctx.restore();
  }

  /** A line of text in a corner of the plot. */
  drawNote(ctx, width, height, text, corner = 'left') {
    ctx.save();
    ctx.font = '11px ui-monospace, monospace';
    ctx.textBaseline = 'bottom';
    const w = ctx.measureText(text).width + 8;
    const x = corner === 'right' ? width - w - 4 : 4;
    ctx.fillStyle = '#05080dcc';
    ctx.fillRect(x, height - 18, w, 15);
    ctx.fillStyle = '#8b98a5';
    ctx.fillText(text, x + 4, height - 5);
    ctx.restore();
  }

  /** The spectrum of one channel, in a pane under the trace. */
  drawSpectrum(ctx, frame, settings, top, width, height) {
    ctx.save();
    ctx.translate(0, top);
    ctx.fillStyle = '#05080d';
    ctx.fillRect(0, 0, width, height);
    ctx.strokeStyle = '#2b3542';
    ctx.beginPath();
    ctx.moveTo(0, 0.5);
    ctx.lineTo(width, 0.5);
    ctx.stroke();

    const index = frame.channelIndex(settings.channel);
    if (index < 0) {
      this.drawNote(ctx, width, height, `FFT: channel ${settings.channel} is off`);
      ctx.restore();
      return;
    }
    const counts = frame.counts(index);
    const d = frame.channels[index];
    const envelope = frame.envelope;
    const count = envelope ? counts.length / 2 : counts.length;
    if (!this.spectrumSamples || this.spectrumSamples.length < count) {
      this.spectrumSamples = new Float64Array(count);
    }
    const samples = this.spectrumSamples;
    let filled = 0;
    for (let i = 0; i < count; i += 1) {
      const c = envelope ? (counts[2 * i] + counts[2 * i + 1]) / 2 : counts[i];
      if (counts[envelope ? 2 * i : i] === NO_SAMPLE) continue;
      samples[filled] = c * d.scaleVPerCount + d.offsetV;
      filled += 1;
    }
    const intervalS = (envelope ? frame.sampleIntervalNs * 2 : frame.sampleIntervalNs) / 1e9;
    const result = render.spectrumDbv(samples, filled, 1 / intervalS,
      settings.window || 'hann', this.spectrumCache);
    if (!result) {
      this.drawNote(ctx, width, height, 'FFT: too few samples');
      ctx.restore();
      return;
    }

    // +20 dBV at the top to -100 at the bottom, a line every 20.
    const topDb = 20;
    const bottomDb = -100;
    const yOf = (db) => ((topDb - Math.max(bottomDb, Math.min(topDb, db))) / (topDb - bottomDb)) * height;
    ctx.strokeStyle = '#1c2430';
    ctx.fillStyle = '#56606b';
    ctx.font = '10px ui-monospace, monospace';
    ctx.textBaseline = 'top';
    ctx.beginPath();
    for (let db = topDb; db >= bottomDb; db -= 20) {
      const y = Math.round(yOf(db)) + 0.5;
      ctx.moveTo(0, y);
      ctx.lineTo(width, y);
    }
    ctx.stroke();
    for (let db = topDb - 20; db > bottomDb; db -= 20) ctx.fillText(`${db} dBV`, 2, yOf(db) + 1);

    // Peak per pixel column, as on the trace, so a narrow spur survives.
    const bins = result.db;
    const columns = Math.max(1, Math.floor(width));
    const perColumn = (bins.length - 1) / columns;
    let peakBin = 1;
    ctx.strokeStyle = this.channelColor(d.channel);
    ctx.beginPath();
    for (let column = 0; column < columns; column += 1) {
      const from = Math.max(1, Math.floor(column * perColumn));
      const to = Math.min(bins.length, Math.floor((column + 1) * perColumn) + 1);
      let high = -Infinity;
      for (let k = from; k < to; k += 1) {
        if (bins[k] > high) high = bins[k];
        if (bins[k] > bins[peakBin]) peakBin = k;
      }
      if (high === -Infinity) continue;
      if (column === 0) ctx.moveTo(column, yOf(high)); else ctx.lineTo(column, yOf(high));
    }
    ctx.stroke();

    const nyquist = result.resolution * (bins.length - 1);
    this.drawNote(ctx, width, height,
      `FFT ${settings.channel} ${settings.window || 'hann'}  0\u2013${si(nyquist, 'Hz', 3)}  `
      + `RBW ${si(result.resolution, 'Hz', 3)}  peak ${si(peakBin * result.resolution, 'Hz', 4)} `
      + `${bins[peakBin].toFixed(1)} dBV`);
    ctx.restore();
  }

  /** "Trig'd", "Auto", "Roll" or "Stop", as a bench scope's status reads. */
  updateTriggerStatus(frame) {
    const acquiring = this.state ? this.state.acquiring : true;
    let status;
    // Stopped before rolling: the last frame of a stopped roll is still a
    // streaming one, and the badge read ROLL over a screen that had stopped.
    if (!acquiring) status = 'stop';
    else if (frame.streaming) status = 'roll';
    else status = (frame.flags & FLAG_TRIGGERED) ? 'trigd' : 'auto';
    if (status === this.status) return;
    this.status = status;
    if (status === 'stop') this.showIdleRate();
    const badge = el('trig-status');
    if (!badge) return;
    badge.textContent = { roll: 'ROLL', stop: 'STOP', trigd: 'TRIG\u2019D', auto: 'AUTO' }[status];
    badge.className = `trig trig--${status}`;
  }

  /** The cursors, and what they read on the frame being drawn.
   *
   * There is nothing to grab here and no field in the sidebar: these are
   * placed by typing, in this page's console or in the terminal CLI, and the
   * box holds the pair so both mean the same two markers. Drawing them is
   * still the point -- a delta between two positions you cannot see is a
   * calculator, not a cursor.
   */
  drawCursors(ctx, frame, width, height) {
    const { time, volts, channel } = this.cursors;
    const labels = [];

    ctx.save();
    ctx.font = '11px ui-monospace, monospace';

    if (time && frame.samplesPerChannel) {
      const xs = time.map((t) => this.timeToX(frame, t, width));
      xs.forEach((x, i) => {
        // Clamped, so a cursor past an edge is still visible as being that
        // way rather than vanishing and reading as unset.
        this.drawCursorLine(ctx, true, clamp(x, 1, width - 1), width, height,
          `t${i + 1}`);
      });
      const deltaT = time[1] - time[0];
      labels.push(`\u0394t ${si(deltaT, 's', 4)}`);
      if (deltaT) labels.push(`1/\u0394t ${si(1 / deltaT, 'Hz', 4)}`);

      // The voltage under each cursor, read off this frame on the channel the
      // box measures against, so the number matches the trace it is drawn on.
      const trace = this.traceAt(frame, channel);
      if (trace) {
        const readings = time.map((t) => sampleTraceAt(frame, trace, t));
        if (readings.every((v) => v !== null)) {
          labels.push(`\u0394V ${si(readings[1] - readings[0], 'V', 4)}`);
        }
      }
    }

    if (volts) {
      // On the cursor channel's scale and shifted with its trace: a voltage
      // means a different height on a channel at a different volts/div. In
      // its colour too, which is how you tell whose scale that is.
      const label = this.channelState.has(channel)
        ? channel : this.channelState.keys().next().value;
      const state = this.channelState.get(label);
      const fullScale = ((state && state.voltsPerDiv) || 1) * 4;
      const shift = positionDivisions(state) / 4;
      const color = this.channelColor(label);
      volts.forEach((v, i) => {
        const y = height / 2 - (v / fullScale + shift) * (height / 2);
        this.drawCursorLine(ctx, false, clamp(y, 1, height - 1), width, height,
          `v${i + 1}`, color);
      });
      labels.push(`\u0394V ${si(volts[1] - volts[0], 'V', 4)}`);
    }

    // One box, top-right: the trigger level labels at the left and the time
    // marker at the centre, so this is the corner left free.
    if (labels.length) {
      const lines = labels;
      const boxWidth = Math.max(...lines.map((t) => ctx.measureText(t).width)) + 10;
      const boxHeight = lines.length * 13 + 6;
      ctx.fillStyle = '#05080dcc';
      ctx.fillRect(width - boxWidth - 4, 4, boxWidth, boxHeight);
      ctx.fillStyle = '#8b98a5';
      ctx.textBaseline = 'top';
      lines.forEach((text, i) => {
        ctx.fillText(text, width - boxWidth + 1, 8 + i * 13);
      });
    }
    ctx.restore();
  }

  /** One cursor: a dashed line across the plot with its name at the edge. */
  drawCursorLine(ctx, vertical, at, width, height, name, color = '#8b98a5') {
    const position = Math.round(at) + 0.5;
    ctx.strokeStyle = color;
    ctx.lineWidth = 1;
    ctx.setLineDash([3, 3]);
    ctx.beginPath();
    if (vertical) {
      ctx.moveTo(position, 0);
      ctx.lineTo(position, height);
    } else {
      ctx.moveTo(0, position);
      ctx.lineTo(width, position);
    }
    ctx.stroke();
    ctx.setLineDash([]);

    // Named, because two identical dashed lines do not say which is which and
    // the readout box reports them in order.
    ctx.fillStyle = color;
    ctx.textBaseline = 'top';
    if (vertical) {
      ctx.fillText(name, Math.min(width - 14, position + 2), height - 14);
    } else {
      ctx.fillText(name, 2, Math.min(height - 14, position + 2));
    }
  }

  /** Where a time relative to the trigger falls, in pixels across the plot. */
  timeToX(frame, seconds, width) {
    const view = this.currentView || { start: 0, end: frame.samplesPerChannel };
    const index = frame.preTriggerSamples
      + (seconds * 1e9) / frame.sampleIntervalNs;
    return ((index - view.start) / (view.end - view.start)) * width;
  }

  /** A frame's volts for a channel label, or null if it is not in the frame. */
  traceAt(frame, label) {
    const index = frame.channels.findIndex((d) => d.channel === label);
    return index < 0 ? null : frame.volts(index);
  }

  drawGraticule(ctx, width, height) {
    ctx.strokeStyle = '#1c2430';
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (let i = 1; i < HORIZONTAL_DIVISIONS; i += 1) {
      const x = Math.round((width * i) / HORIZONTAL_DIVISIONS) + 0.5;
      ctx.moveTo(x, 0);
      ctx.lineTo(x, height);
    }
    for (let i = 1; i < 8; i += 1) {
      const y = Math.round((height * i) / 8) + 0.5;
      ctx.moveTo(0, y);
      ctx.lineTo(width, y);
    }
    ctx.stroke();

    // Centre lines brighter, since they are the zero references.
    ctx.strokeStyle = '#2b3542';
    ctx.beginPath();
    ctx.moveTo(Math.round(width / 2) + 0.5, 0);
    ctx.lineTo(Math.round(width / 2) + 0.5, height);
    ctx.moveTo(0, Math.round(height / 2) + 0.5);
    ctx.lineTo(width, Math.round(height / 2) + 0.5);
    ctx.stroke();
  }

  /** Horizontal line at the trigger level, on the source channel's scale.
   *
   * The Level field was the only setting with nothing on the plot to show it:
   * you could set 1.02 V against a trace whose peaks never reached it and get
   * no hint why nothing was triggering.
   *
   * Drawn against the source channel's volts/div, and in its colour, because
   * that is the trace it has to be read against -- with two channels at
   * different scales, a level on one is at a different height on the other.
   */
  drawTriggerLevel(ctx, width, height) {
    const source = el('trigger-source').value;
    const state = this.channelState.get(source);
    // Checked as text before converting: Number('') is 0, so an empty field
    // would otherwise draw a line across the centre claiming a 0 V trigger
    // that nobody set.
    const entered = el('trigger-level').value;
    const level = Number(entered);
    if (!state || entered === '' || !Number.isFinite(level)) return;

    const fullScale = (state.voltsPerDiv || 1) * 4;
    // Shifted with the source's trace. The level is read against that trace,
    // so a channel moved up two divisions has to take its level line with it
    // -- left at the unshifted height it would cross the waveform somewhere
    // the scope is not triggering.
    const shift = positionDivisions(state) / 4;
    const exact = height / 2 - (level / fullScale + shift) * (height / 2);
    // A level beyond the top or bottom is pinned to that edge rather than
    // dropped. "Off screen, that way" is the useful thing to know, and a line
    // drawn outside the canvas looks identical to no trigger at all -- which
    // is the state someone reads as the feature being broken.
    const y = Math.min(height - 1, Math.max(1, exact));
    const offBy = exact < 1 ? '\u2191' : (exact > height - 1 ? '\u2193' : '');

    const styles = getComputedStyle(document.documentElement);
    const colour = styles.getPropertyValue(CHANNEL_COLORS[colorSlot(source)]).trim();

    ctx.save();
    ctx.strokeStyle = colour;
    ctx.lineWidth = 1;
    // A tighter dash when pinned, so a level that is merely at the edge of
    // the screen is not mistaken for one that is on it.
    ctx.setLineDash(offBy ? [2, 3] : [6, 4]);
    ctx.beginPath();
    ctx.moveTo(0, Math.round(y) + 0.5);
    ctx.lineTo(width, Math.round(y) + 0.5);
    ctx.stroke();
    ctx.setLineDash([]);

    // Labelled at the left, where the time marker (centre) and the overflow
    // warning (top) are not.
    const text = `T ${source} ${si(level, 'V', 3)}${offBy}`;
    ctx.font = '11px ui-monospace, monospace';
    ctx.textBaseline = 'bottom';
    const pad = 3;
    const box = ctx.measureText(text).width + pad * 2;
    // Behind the text, so it stays readable where the trace crosses it.
    ctx.fillStyle = '#05080dcc';
    const top = Math.min(height - 14, Math.max(0, Math.round(y) - 14));
    ctx.fillRect(0, top, box, 13);
    ctx.fillStyle = colour;
    ctx.fillText(text, pad, top + 12);
    ctx.restore();
  }

  drawTriggerMarker(ctx, frame, width, height) {
    const total = frame.samplesPerChannel;
    if (!total) return;
    const x = this.timeToX(frame, 0, width);
    if (x < 0 || x > width) return;
    ctx.strokeStyle = '#d29922';
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, height);
    ctx.stroke();
    ctx.setLineDash([]);
  }

  // ---------- wiring ----------
  wireControls() {
    el('connect').addEventListener('click', () => {
      // Open, not merely started: a press while it is still connecting
      // leaves it to finish, so a double click connects once rather than
      // connecting and then cancelling.
      if (this.streamOpen()) this.disconnect(); else this.connect();
    });

    el('net-select').addEventListener('change', async (event) => {
      this.disconnect();
      this.net = event.target.value || null;
      this.adoptChannelNets();
      await this.loadCapabilities();
    });

    // Run and Single both move the trigger mode -- Single sets it, Run
    // promotes a single-shot out of it -- so the panel is re-read rather than
    // left showing what it said before the click.
    el('btn-start').addEventListener('click', async () => {
      await this.runCommand('start_capture', {}, 'start');
      this.startMeasurementPolling();
      await this.syncTriggerState();
    });
    // Single takes one capture and stops, so one reading is the whole of what
    // there is to show; polling would keep re-arming a scope the user stopped.
    el('btn-single').addEventListener('click', async () => {
      await this.runCommand('start_single', {}, 'single');
      this.stopMeasurementPolling();
      this.refreshMeasurements();
      await this.syncTriggerState();
    });
    el('btn-stop').addEventListener('click', () => {
      // The last values stay on screen, as they do on a stopped bench scope:
      // they describe the frame still being displayed.
      this.stopMeasurementPolling();
      this.runCommand('stop_capture', {}, 'stop');
    });
    el('btn-force').addEventListener('click', () => this.runCommand('force_trigger', {}, 'force'));

    // Coming back to a backgrounded tab. A hidden page's timers are throttled
    // hard -- to once a second, and to once a minute after a few minutes
    // hidden -- so the readouts on screen when it comes back can describe a
    // capture from a minute ago. A bench scope shows the signal now, so this
    // refreshes on return instead of waiting for the next throttled tick.
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible' && this.measureTimer) {
        this.pollMeasurementsOnce();
      }
    });

    el('timebase').addEventListener('change', (event) => {
      this.applyTimebase(Number(event.target.value));
    });

    // Horizontal position. Typed values wait for Enter or for the field to be
    // left: unlike the vertical position this re-arms the scope, and a
    // capture per keystroke on the way to "0.015" is three windows nobody
    // asked for. The arrow keys still apply at once; they are the knob.
    const timeUnit = el('time-position-unit');
    for (const [text, factor] of HORIZONTAL_UNITS) timeUnit.append(new Option(text, String(factor)));
    timeUnit.value = String(1e-3);
    this.timePositionField = wirePositionField(el('time-position'), timeUnit, {
      live: false,
      perDiv: () => Number(el('timebase').value) || 0,
      get: () => this.timePositionS,
      set: (seconds, options) => this.applyTimePosition(seconds, options),
    });
    el('time-position-reset').addEventListener('click', () => this.applyTimePosition(0));

    // One field per command. This sent all four on every change, so adjusting
    // the level also re-sent the mode, the slope and the source from controls
    // that may never have been touched -- and since nothing read the trigger
    // back, "never touched" meant the values in the HTML rather than the ones
    // on the instrument. Moving the level was enough to put a scope left in
    // Normal back into Auto. `trigger_edge` applies only what it is given,
    // which is what makes one field per command safe.
    const sendTrigger = (settings) => this.runCommand('trigger_edge', settings,
                                                      'trigger');

    el('trigger-slope').addEventListener('change', (event) => {
      sendTrigger({ slope: event.target.value });
    });
    el('trigger-mode').addEventListener('change', (event) => {
      sendTrigger({ mode: event.target.value });
    });
    // Source also decides which channel's scale the level line is drawn
    // against, so the plot changes with it and not only the hardware.
    el('trigger-source').addEventListener('change', (event) => {
      sendTrigger({ source: event.target.value });
      this.requestRedraw();
    });
    // On the number input, react to committed edits rather than each
    // keystroke, which would send a command per digit.
    el('trigger-level').addEventListener('change', (event) => {
      const level = Number(event.target.value);
      if (event.target.value === '' || !Number.isFinite(level)) return;
      // Redraw as well as send: the level line moves with this field, and
      // waiting for the next capture to show where the trigger went defeats
      // the point of drawing it.
      sendTrigger({ level });
      this.requestRedraw();
    });

    el('trigger-holdoff').addEventListener('change', (event) => {
      const seconds = Number(event.target.value);
      if (event.target.value === '' || !Number.isFinite(seconds)) return;
      this.runCommand('set_trigger_holdoff', { seconds }, `holdoff ${seconds}`);
    });

    const sendAcquire = () => {
      const mode = el('acquire-mode').value;
      const params = { mode };
      if (mode === 'average') params.count = Number(el('acquire-count').value) || 16;
      this.runCommand('set_acquire', params, `acquire ${mode}`);
    };
    el('acquire-mode').addEventListener('change', sendAcquire);
    el('acquire-count').addEventListener('change', () => {
      if (el('acquire-mode').value === 'average') sendAcquire();
    });
    el('roll-mode').addEventListener('change', (event) => {
      this.runCommand('set_roll', { mode: event.target.value }, `roll ${event.target.value}`);
    });

    el('display-persistence').addEventListener('change', (event) => {
      const value = event.target.value;
      this.setDisplay({
        persistence: value === 'off' || value === 'infinite' ? value : Number(value),
      });
    });
    el('display-xy').addEventListener('change', (event) => {
      this.setDisplay({ xy: event.target.checked ? 'on' : 'off' });
    });
    const sendZoom = () => {
      const factor = Number(el('display-zoom').value);
      const center = Number(el('display-zoom-center').value) || 0;
      this.setDisplay({ zoom: !(factor > 1) ? 'off' : { factor, center } });
    };
    el('display-zoom').addEventListener('change', sendZoom);
    el('display-zoom-center').addEventListener('change', sendZoom);
    el('display-math').addEventListener('change', (event) => {
      this.setDisplay({ math: event.target.value });
    });
    el('display-fft').addEventListener('change', (event) => {
      const value = event.target.value;
      this.setDisplay({ fft: value === 'off' ? 'off' : { channel: value, window: 'hann' } });
    });

    el('trigger-markers').addEventListener('change', (event) => {
      this.showTriggerMarkers = event.target.checked;
      this.requestRedraw();
    });

    el('btn-clear').addEventListener('click', () => this.console.clear());
  }

  wireConsole() {
    this.wireConsoleResize();
    const form = el('console-form');
    const input = el('console-line');

    form.addEventListener('submit', (event) => {
      event.preventDefault();
      const line = input.value.trim();
      if (!line) return;
      input.value = '';
      this.console.echo(line);
      this.console.remember(line);
      this.execute(line);
    });

    input.addEventListener('keydown', (event) => {
      if (event.key === 'ArrowUp' || event.key === 'ArrowDown') {
        const recalled = this.console.recall(event.key === 'ArrowUp' ? -1 : 1);
        if (recalled !== null) {
          input.value = recalled;
          event.preventDefault();
        }
      } else if (event.key === 'Tab') {
        event.preventDefault();
        const matches = grammar.complete(input.value.trim());
        if (matches.length === 1) input.value = `${matches[0]} `;
        else if (matches.length > 1) this.console.write(matches.join('  '), 'table');
      }
    });
  }

  /** Drag the console's top edge. The height is remembered for the next visit. */
  wireConsoleResize() {
    const handle = el('console-resize');
    const log = el('console-output');
    const section = handle && handle.closest('.console');
    if (!handle || !log || !section) return;

    // What the log leaves above it at its tallest: a readable waveform, and
    // on a narrow window the panel stacked under it as well. The stylesheet
    // says how much, being what stacks the panel there.
    const reserve = () => {
      const value = parseFloat(getComputedStyle(document.documentElement)
        .getPropertyValue('--plot-reserve'));
      return value > 0 ? value : 160;
    };
    const room = () => {
      const header = document.querySelector('.bar');
      const headerH = header ? header.offsetHeight : 0;
      const chrome = section.offsetHeight - log.offsetHeight;
      return window.innerHeight - headerH - chrome - reserve();
    };
    // `preferred` is the height the user asked for. The pane shows that
    // height clamped to the window, so shrinking the window and growing it
    // again brings the log back.
    let preferred = CONSOLE_LOG_DEFAULT;
    const apply = () => {
      const height = consoleLogHeight(preferred, 0, room());
      document.documentElement.style.setProperty('--console-log', `${height}px`);
      handle.setAttribute('aria-valuenow', String(height));
      handle.setAttribute('aria-valuemax', String(Math.round(Math.max(CONSOLE_LOG_MIN, room()))));
      return height;
    };
    const remember = (height) => {
      preferred = Math.max(CONSOLE_LOG_MIN, height);
      const shown = apply();
      try { localStorage.setItem(CONSOLE_LOG_KEY, String(preferred)); } catch { /* see below */ }
      return shown;
    };

    try {
      const saved = Number(localStorage.getItem(CONSOLE_LOG_KEY));
      if (saved >= CONSOLE_LOG_MIN) preferred = saved;
    } catch { /* a private window with storage blocked keeps the default */ }
    apply();
    window.addEventListener('resize', () => apply());

    const drag = (startY, startH) => {
      document.body.classList.add('is-resizing-console');
      const at = (clientY) => consoleLogHeight(startH, startY - clientY, Number.POSITIVE_INFINITY);
      const move = (event) => { preferred = at(event.clientY); apply(); };
      const stop = (event) => {
        document.body.classList.remove('is-resizing-console');
        handle.removeEventListener('pointermove', move);
        handle.removeEventListener('pointerup', stop);
        handle.removeEventListener('pointercancel', stop);
        remember(at(event.clientY));
      };
      handle.addEventListener('pointermove', move);
      handle.addEventListener('pointerup', stop);
      handle.addEventListener('pointercancel', stop);
    };
    handle.addEventListener('pointerdown', (event) => {
      if (event.button !== 0) return;
      event.preventDefault();
      try { handle.setPointerCapture(event.pointerId); } catch { /* drag still follows the pointer */ }
      drag(event.clientY, log.getBoundingClientRect().height);
    });
    handle.addEventListener('dblclick', () => remember(CONSOLE_LOG_DEFAULT));
    handle.addEventListener('keydown', (event) => {
      const step = event.shiftKey ? 48 : 16;
      if (event.key === 'ArrowUp') remember(consoleLogStep(preferred, step, room()));
      else if (event.key === 'ArrowDown') remember(consoleLogStep(preferred, -step, room()));
      else return;
      event.preventDefault();
    });
  }

  async execute(line) {
    const verb = line.split(/\s+/)[0].toLowerCase();

    // Page-local verbs never reach the box. Help is recognized in every
    // form a user reaches for: `help trigger` and `trigger --help`.
    const topic = grammar.helpTopic(line);
    if (topic !== null) return this.printHelp(topic);
    if (verb === 'clear') return this.console.clear();
    if (verb === 'connect') return this.connect();
    if (verb === 'disconnect') return this.disconnect();

    let parsed;
    try {
      parsed = grammar.parse(line);
    } catch (e) {
      this.console.error(e.message);
      return undefined;
    }
    if (parsed.local) return this.verticalPositionCommand(parsed);
    // Through the same clamp and note as the sidebar field. Sent as typed,
    // `hpos 100ms` at 1 ms/div was stored a hundred divisions off screen,
    // with nothing said. A bare `hpos` is a read and goes to the box below.
    if (parsed.action === 'set_time_offset') {
      return { value: await this.applyTimePosition(parsed.params.offset) };
    }
    // Refused here rather than sent: the daemon would refuse it anyway, and
    // the page can say where the control that does work is.
    if (parsed.action === 'set_offset' && parsed.params.offset !== 0
        && this.capabilities && this.capabilities.analog_offset === false) {
      this.console.error(`The ${this.capabilities.model || 'scope'} has no analog offset, `
        + 'so it cannot shift the signal in hardware. To move a trace on screen, '
        + 'use "vpos <channel> <volts>" or the channel\'s Position field.');
      return undefined;
    }
    // Only once the strips exist: before then the page cannot tell "every
    // channel is off" from "no channel is known yet".
    if (isMeasurement(parsed.action) && !parsed.channel
        && this.channelState && this.channelState.size
        && !firstMeasurableChannel(this.channelState)) {
      this.console.error('No channel is on. Switch one on to measure.');
      return undefined;
    }
    // `enable B` names a channel. The action itself carries no channel: the
    // box turns on whichever net the request is sent to, so the letter has
    // to choose the net or both letters enable the first channel.
    let net;
    let label = null;
    if (parsed.channel) {
      net = this.netForLabel(parsed.channel);
      if (!net) {
        this.console.error(`channel ${parsed.channel} has no net`);
        return undefined;
      }
      label = parsed.channel;
    } else if (isPerChannelAction(parsed.action)) {
      const channel = defaultChannel(
        parsed.action, this.channelState, this.channelNets);
      if (channel) ({ net, label } = channel);
    }
    const body = await this.runCommand(
      parsed.action, parsed.params, parsed.summary, net, label);
    // The box does not know where the page draws its traces, so the page
    // adds that to the box's account of its settings.
    if (body && parsed.action === 'get_state') this.reportVerticalPositions();
    if (body && parsed.channel
        && (parsed.action === 'enable_net' || parsed.action === 'disable_net')) {
      const state = this.channelState && this.channelState.get(parsed.channel);
      if (state) {
        state.enabled = parsed.action === 'enable_net';
        if (state.toggle) state.toggle.checked = state.enabled;
      }
      this.refreshMeasurements();
    }
    return body;
  }

  /** `vpos`: read or move a trace on screen. Nothing is sent to the box. */
  verticalPositionCommand(parsed) {
    let label = parsed.channel;
    if (!label) {
      // As the other per-channel verbs choose: the first channel that is on,
      // else the first that can be reached at all.
      const on = firstMeasurableChannel(this.channelState);
      label = on ? on.label
        : [...this.channelState.keys()].find((l) => this.channelState.get(l).net);
    }
    const state = label && this.channelState.get(label);
    if (!state) {
      this.console.error(label ? `this scope has no channel ${label}` : 'no channel to position');
      return undefined;
    }
    // The strip disables its Position field for an unwired channel; a typed
    // command should not reach what the panel will not.
    if (!state.net) {
      this.console.error(`channel ${label} has no net`);
      return undefined;
    }
    if (parsed.params.volts !== undefined) {
      this.applyVerticalPosition(label, parsed.params.volts);
    }
    this.console.write(`channel ${label}: vertical position ${si(state.positionV, 'V', 3)} (display only)`);
    return { value: state.positionV };
  }

  /** Each channel's vertical position, as `status` reports the rest. */
  reportVerticalPositions() {
    for (const [label, state] of this.channelState.entries()) {
      if (!state.net) continue;
      this.console.write(`channel ${label}: vertical position ${si(state.positionV, 'V', 3)} (display only)`);
    }
  }

  printHelp(topic) {
    if (topic) {
      const row = grammar.helpFor(topic);
      if (!row) {
        this.console.error(`no command "${topic}"; type "help" to list them`);
        return;
      }
      this.console.write(`  ${row[0]}`, 'table');
      this.console.write(`  ${row[1]}`, 'note');
      return;
    }
    const rows = grammar.helpRows();
    const width = Math.max(...rows.map(([usage]) => usage.length));
    for (const [usage, help] of rows) {
      this.console.write(`  ${usage.padEnd(width + 2)}${help}`, 'table');
    }
  }
}

// Exported, and started only in a browser, so the channel/net mapping and the
// volts/div ladder can be exercised under node. The constructor needs a DOM,
// so the tests call the methods on the prototype against a hand-built `this`.
// sampleTraceAt is exported so it can be checked against the box's
// `trace_voltage_at`: the reading in the terminal and the label on the plot
// come from different implementations of the same interpolation.
/** Height of the command log, and the smallest it may be dragged to. */
export const CONSOLE_LOG_DEFAULT = 150;
export const CONSOLE_LOG_MIN = 48;
const CONSOLE_LOG_KEY = 'lager-scope-console-log';

/**
 * Log height after a drag.
 *
 * `offset` is how far the pointer moved upward from the start of the drag:
 * the console sits at the bottom, so pulling its edge up makes the log
 * taller. `room` is what the window has left once the waveform keeps a
 * usable strip, so the log cannot be dragged off the screen.
 */
export function consoleLogHeight(start, offset, room) {
  const base = Number.isFinite(start) ? start : CONSOLE_LOG_DEFAULT;
  const next = Math.max(CONSOLE_LOG_MIN, base + (Number.isFinite(offset) ? offset : 0));
  // A room that has not been measured is not a cap. A measured room always
  // leaves the minimum, even when the window is shorter than that.
  if (!Number.isFinite(room)) return Math.round(next);
  return Math.round(Math.min(Math.max(CONSOLE_LOG_MIN, room), next));
}

/**
 * Log height after an arrow-key step of `step` pixels, upward when positive.
 *
 * Stepped from the height on screen, not the one asked for: a drag past the
 * top of the window asks for more than there is room for, and stepping down
 * from that took a keypress per 16 px of the excess before the log moved.
 */
export function consoleLogStep(preferred, step, room) {
  return consoleLogHeight(consoleLogHeight(preferred, 0, room), step, room);
}

export { ScopeApp, voltsPerDivChoices, timebaseChoices, sampleTraceAt,
         PER_CHANNEL_ACTIONS, isPerChannelAction, channelName,
         pinIndex, colorSlot, sameSetting, si,
         stepPosition, scaleStep, decimalsIn, wirePositionField };

if (typeof document !== 'undefined') {
  const app = new ScopeApp();
  app.init();
}
