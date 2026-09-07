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
 * Rendering decodes on the socket and draws on a frame callback rather than
 * drawing per capture: captures arrive at ~100/s while a display only needs
 * ~60, so drawing each one would burn CPU on frames nobody sees.
 */

import { decode, FLAG_TRIGGERED } from './lscp.js';
import * as grammar from './commands.js';

const CHANNEL_COLORS = ['--ch-a', '--ch-b', '--ch-c', '--ch-d'];

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

/** The 1-2-5 time/div settings this unit can reach at `memoryDepth`.
 *
 * The list used to be a fixed 1 us to 100 ms regardless of what was attached,
 * so it offered settings the hardware cannot reach: a 2204A samples no faster
 * than 10 ns, and a block is at least 8000 samples, which puts its fastest
 * screen at 8 us/div. Choosing 1 us/div silently got you eight times that.
 *
 * Only the fast end can be worked out from the capabilities -- nothing there
 * bounds the slowest interval -- so the slow end stays as offered, and the
 * readback corrects the display if the unit lands somewhere else.
 */
function timebaseChoices(caps, memoryDepth) {
  const rate = Number(caps && caps.max_sample_rate_hz);
  const depth = Number(memoryDepth);
  if (!(rate > 0) || !(depth > 0)) return TIMEBASES.slice();

  const fastest = depth / (rate * HORIZONTAL_DIVISIONS);
  // Just under, so a step the unit can hit exactly is not excluded by
  // floating-point noise in the division above.
  const within = TIMEBASES.filter((t) => t >= fastest * 0.999);
  return within.length ? within : TIMEBASES.slice(-1);
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
]);

function isPerChannelAction(action) {
  // Cursors are the scope's, and measure_cursor reads them against the
  // channel they were placed on rather than against a net's own.
  if (action === 'measure_cursor') return false;
  if (action.startsWith('measure_')) return true;
  return PER_CHANNEL_ACTIONS.has(action);
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
      return `${(value / factor).toPrecision(digits)} ${prefix}${unit}`;
    }
  }
  return `${value.toPrecision(digits)} ${unit}`;
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
    this.socket = null;
    this.capabilities = null;
    this.latest = null;
    this.dirty = false;
    this.channelState = new Map();
    // Trigger level and position drawn on the plot. On by default: a level
    // you cannot see is one you cannot set with any confidence.
    this.showTriggerMarkers = true;
    // Divisions the capture window is shifted from the trigger.
    this.timePositionDiv = 0;
    // Cursors, as the box last reported them: `{cursors, readings}` or null.
    // Held rather than derived, since the box owns them and the console is
    // the only thing that moves them.
    this.cursors = null;
    // Block depth the timebase list was last built for. Null until a capture
    // reports one, which is when the unreachable fast steps can be dropped.
    this.timebaseDepth = null;
    // Measurement polling, so the readouts follow the signal rather than
    // describing whatever was on screen when Start was pressed.
    this.measureTimer = null;
    this.measureInFlight = false;

    this.captureCount = 0;
    this.lastRateAt = performance.now();
    this.rate = 0;
    this.latencyMs = null;

    this.console = new Console(el('console-output'));
    this.canvas = el('scope-canvas');
    this.ctx = this.canvas.getContext('2d');

    this.wireControls();
    this.wireConsole();
    this.observeCanvas();

    requestAnimationFrame(() => this.tick());
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
      const nets = (body.nets || body || []).filter(
        (n) => n.role === 'scope' || n.role === 'scope-channel'
          || n.role === 'analog');

      select.replaceChildren();
      if (nets.length === 0) {
        select.append(new Option('no scope nets', ''));
        this.console.error(
          'No scope nets on this box. Create one with "lager net add".');
        return;
      }

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
   * the trigger, acquisition and the cursors. The six settings that do belong
   * to a channel go to that channel's net, because the scope net has no
   * channel and the box refuses to guess one.
   */
  netForAction(action, explicit) {
    if (explicit) return explicit;
    if (!isPerChannelAction(action)) return this.net;
    const channel = (this.channelNets || [])[0];
    return channel ? channel.name : this.net;
  }

  async loadCapabilities() {
    if (!this.net) return;
    try {
      const response = await fetch(`/scope/${encodeURIComponent(this.net)}/stream`);
      const body = await response.json();
      if (!response.ok) {
        this.console.error(body.error || `Ticket request failed (${response.status})`);
        return;
      }
      this.ticket = body;
      this.capabilities = body.capabilities;
      if (body.capability_error) {
        this.console.note(`Capabilities unavailable: ${body.capability_error}`);
      }
      this.applyCapabilities();
    } catch (e) {
      this.console.error(`Could not reach the scope: ${e.message}`);
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
        // Divisions this trace is shifted from centre, for viewing only.
        positionDiv: 0,
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

    this.showCapabilityNotes(caps);
  }

  buildChannelStrip(label, index, caps) {
    const color = `var(${CHANNEL_COLORS[index % CHANNEL_COLORS.length]})`;
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
        `Channel ${label} ${toggle.checked ? 'on' : 'off'}`, state.net);
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
        state.net);
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
    positionCaption.textContent = 'Position (div)';
    const positionRow = document.createElement('div');
    positionRow.className = 'field-row';
    const positionInput = document.createElement('input');
    positionInput.type = 'number';
    positionInput.step = '0.5';
    positionInput.min = String(-VERTICAL_LIMIT);
    positionInput.max = String(VERTICAL_LIMIT);
    positionInput.value = '0';
    positionInput.setAttribute(
      'aria-label', `Channel ${label} vertical position in divisions`);
    const positionReset = document.createElement('button');
    positionReset.type = 'button';
    positionReset.className = 'btn btn--small';
    positionReset.textContent = '0';
    positionReset.title = `Centre channel ${label}`;
    positionRow.append(positionInput, positionReset);
    position.append(positionCaption, positionRow);
    state.positionInput = positionInput;
    state.positionReset = positionReset;

    const applyPosition = (divisions) => {
      state.positionDiv = clamp(divisions, -VERTICAL_LIMIT, VERTICAL_LIMIT);
      positionInput.value = String(state.positionDiv);
      this.requestRedraw();
    };
    // On input rather than change: nothing is sent to the scope, so the trace
    // can follow the spinner as it is held down.
    positionInput.addEventListener('input', () => {
      const value = Number(positionInput.value);
      // Blank part-way through typing a minus sign, or cleared outright.
      // Leaving the trace where it is beats snapping it to centre and back.
      if (positionInput.value === '' || !Number.isFinite(value)) return;
      applyPosition(value);
    });
    positionReset.addEventListener('click', () => applyPosition(0));

    strip.append(head, field, custom, pair, position);

    // No net means no way to address this channel: the box has capabilities
    // reporting it, but nothing wired to it. Disable rather than let the
    // controls fall back to the selected net, which is how channel B's
    // switch came to operate channel A.
    if (!this.channelState.get(label).net) {
      const why = `No scope net is wired to channel ${label}. `
        + 'Add one with "lager net add" to control it here.';
      // The position field goes with them: it needs no net, but a channel
      // that can never be switched on has no trace to move.
      for (const control of [toggle, select, customInput, couplingSelect,
        probeSelect, positionInput, positionReset]) {
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
    // The scale is applied to the drawing as well as the hardware, so the
    // picture has to be redrawn even if the command fails or the scope is
    // stopped.
    this.requestRedraw();

    if (push) {
      this.runCommand('set_scale', { volts_per_div: voltsPerDiv },
        `Channel ${label} ${si(voltsPerDiv, 'V', 2)}/div`, state.net);
    }
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
      await this.runCommand('set_timebase', { seconds_per_div: seconds },
        `timebase ${si(seconds, 's', 2)}/div`);
      try {
        const body = await this.send('get_timebase', {});
        const achieved = Number(body.value);
        if (Number.isFinite(achieved) && achieved > 0) this.showTimebase(achieved);
      } catch { /* older box: leave the request showing */ }
    }

    // The window is held in divisions, so a new time/div moves it in seconds.
    // Re-send it against the achieved value now on the control, or a shift of
    // two divisions set at 1 ms/div stays two divisions of the old scale.
    if (this.timePositionDiv) await this.applyTimePosition(this.timePositionDiv);
  }

  /** Make the timebase dropdown display `seconds`, offered or not. */
  showTimebase(seconds) {
    const select = el('timebase');
    if (!select) return;

    const asOption = String(seconds);
    if (![...select.options].some((o) => o.value === asOption)) {
      // Off the ladder, because the hardware rounded to an interval that is
      // not a round number of seconds. Inserted in order so the list stays
      // monotonic.
      const next = [...select.options].find((o) => Number(o.value) > seconds);
      select.add(new Option(`${si(seconds, 's', 3)}/div`, asOption), next || null);
    }
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
        `Channel ${label} ${ratio}x probe`, state.net);
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
   * Scope nets carry a 1-based pin that is the channel number, so channel A
   * is the net on pin 1. Position in the list is only a fallback: nets come
   * back in whatever order the box lists them, and a box needn't define one
   * net per channel -- with only `scope2` defined, index 0 must not silently
   * become channel B's net.
   */
  netForChannel(index) {
    const nets = this.channelNets || this.scopeNets || [];
    const pinOf = (n) => Number(n.pin);
    const byPin = nets.find((n) => pinOf(n) === index + 1);
    if (byPin) return byPin.name;
    // Position is a fallback only where no net declares a usable pin. If any
    // does, an unmatched channel genuinely has no net: with just `scope2`
    // (pin 2) defined, channel A must come back unwired rather than picking
    // up channel B's net and driving the wrong channel.
    const anyPinned = nets.some((n) => Number.isFinite(pinOf(n)) && pinOf(n) > 0);
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

    // Before the offset, which is held in divisions and converted with
    // whatever time/div the control shows: reading the offset first would
    // convert it against a stale one.
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
      const perDiv = Number(el('timebase').value);
      if (Number.isFinite(seconds) && perDiv > 0) {
        // push: false -- the hardware is already there.
        this.applyTimePosition(seconds / perDiv, { push: false });
      }
    } catch { /* older box: leave it centred */ }

    // Cursors outlive the page the same way: the box holds them, so a pair
    // placed from the terminal before this page was opened is drawn on it.
    await this.refreshCursors(this.net);
  }

  /** Move the capture window earlier or later than the trigger.
   *
   * Unlike the vertical position this cannot be done in the renderer. The
   * capture already fills the screen, so signal past either edge was never
   * sampled -- there is nothing on hand to pan to. Seeing it means asking the
   * scope for a window in a different place, which is the pre/post-trigger
   * split, and that means a fresh capture. Positive divisions look forward,
   * to signal later than the trigger; negative look back before it.
   */
  async applyTimePosition(divisions, { push = true } = {}) {
    const value = clamp(divisions, -HORIZONTAL_LIMIT, HORIZONTAL_LIMIT);
    this.timePositionDiv = value;
    el('time-position').value = String(value);
    this.requestRedraw();
    if (!push) return;
    // Divisions here, seconds on the wire: the daemon's offset is a time, and
    // stays correct if this UI ever draws a different number of divisions.
    const perDiv = Number(el('timebase').value) || 0;
    await this.runCommand('set_time_offset', { offset: value * perDiv },
      `position ${value} div`);
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
  async connect() {
    if (this.socket) return;
    if (!this.net) {
      this.console.error('No scope net selected.');
      return;
    }

    // Tickets expire, so fetch a fresh one per connection rather than
    // reusing the one from page load.
    await this.loadCapabilities();
    if (!this.ticket) return;

    const url = new URL(this.ticket.ws_path, window.location.href);
    url.protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';

    this.setLink('connecting', 'link--down');
    const socket = new WebSocket(url);
    socket.binaryType = 'arraybuffer';

    socket.addEventListener('open', () => {
      this.socket = socket;
      this.setLink('connected', 'link--up');
      el('connect').textContent = 'Disconnect';
      this.console.write('Capture stream connected.', 'note');
      // Captures are off until asked for, so that a control-only client is
      // not sent the stream. This is the client that wants it.
      socket.send(JSON.stringify({ command: 'Subscribe' }));
    });

    socket.addEventListener('message', (event) => {
      if (typeof event.data === 'string') {
        this.onControlMessage(event.data);
      } else {
        this.onCapture(event.data);
      }
    });

    socket.addEventListener('error', () => {
      this.setLink('error', 'link--error');
    });

    socket.addEventListener('close', () => {
      this.socket = null;
      this.setLink('disconnected', 'link--down');
      el('connect').textContent = 'Connect';
      el('plot-empty').hidden = false;
    });
  }

  disconnect() {
    // Before the early return: the timer outlives the socket otherwise, and
    // goes on taking a capture every half second against a scope nobody is
    // watching.
    this.stopMeasurementPolling();
    if (!this.socket) return;
    try {
      this.socket.send(JSON.stringify({ command: 'Unsubscribe' }));
    } catch { /* closing anyway */ }
    this.socket.close();
    this.socket = null;
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
    if (response.response === 'Error') {
      // The daemon reports dropped captures this way; it is a warning about
      // the display, not a failed command.
      const kind = /dropped \d+ captures/.test(response.message || '') ? 'note' : 'error';
      this.console.write(response.message, kind);
    }
  }

  onCapture(buffer) {
    let frame;
    try {
      frame = decode(buffer);
    } catch (e) {
      this.console.error(`Bad capture frame: ${e.message}`);
      return;
    }

    this.latest = frame;
    // Coalesce: the newest frame wins and is drawn on the next animation
    // frame. Drawing every capture would render frames the display never
    // shows.
    this.dirty = true;
    this.captureCount += 1;

    const now = performance.now();
    if (now - this.lastRateAt >= 500) {
      this.rate = (this.captureCount * 1000) / (now - this.lastRateAt);
      this.captureCount = 0;
      this.lastRateAt = now;
      this.updateStats(frame);
    }
  }

  updateStats(frame) {
    el('stat-rate').textContent = `${this.rate.toFixed(0)} cap/s`;
    const rate = 1e9 / frame.sampleIntervalNs;
    el('stat-rate-samples').textContent = si(rate, 'S/s', 3);
    el('stat-latency').textContent = `${frame.samplesPerChannel.toLocaleString()} pts`;
    // The capture says how deep a block is, which with the unit's fastest
    // interval is what bounds the reachable timebases. Only known once one
    // has arrived, so the list is trimmed here rather than at connect.
    this.rebuildTimebaseChoices(frame.samplesPerChannel);
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

  async runCommand(action, params, summary, net) {
    try {
      const body = await this.send(action, params, net);
      this.console.write(body.message || `${summary || action}: ok`);
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

  /** The channel the measurements panel reads, or null if none is on.
   *
   * The first enabled one that has a net. Channels with no net of their own
   * cannot be measured through this endpoint, so they are passed over rather
   * than reported as the answer.
   */
  measuredChannel() {
    for (const [label, state] of this.channelState.entries()) {
      if (state && state.enabled && state.net) return { label, net: state.net };
    }
    return null;
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

  tick() {
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
    requestAnimationFrame(() => this.tick());
  }

  draw(frame) {
    const ctx = this.ctx;
    const ratio = window.devicePixelRatio || 1;
    const width = this.canvas.width / ratio;
    const height = this.canvas.height / ratio;

    ctx.clearRect(0, 0, width, height);
    el('plot-empty').hidden = true;

    this.drawGraticule(ctx, width, height);

    const styles = getComputedStyle(document.documentElement);
    let overflowed = [];

    frame.channels.forEach((descriptor, index) => {
      const volts = frame.volts(index);
      if (!volts || volts.length === 0) return;

      const label = descriptor.channel;
      const state = this.channelState.get(label);
      const voltsPerDiv = (state && state.voltsPerDiv) || 1;
      const fullScale = voltsPerDiv * 4; // 8 divisions, centre at zero.
      // The channel's vertical position, converted from divisions to the
      // units the plot works in: half the screen is four divisions, so a
      // division is a quarter of it.
      const shift = ((state && state.positionDiv) || 0) / 4;

      ctx.strokeStyle = styles
        .getPropertyValue(CHANNEL_COLORS[index % CHANNEL_COLORS.length]).trim();
      ctx.lineWidth = 1.25;
      ctx.beginPath();

      // One vertical span per horizontal pixel: with 8000+ samples across
      // ~1000 px, plotting every sample would draw the same column many
      // times and lose the peaks. Min/max per column keeps the envelope,
      // which is what makes narrow glitches visible at all.
      const columns = Math.max(1, Math.floor(width));
      const perColumn = volts.length / columns;
      for (let column = 0; column < columns; column += 1) {
        const start = Math.floor(column * perColumn);
        const end = Math.min(volts.length, Math.floor((column + 1) * perColumn) + 1);
        if (start >= end) continue;

        let min = volts[start];
        let max = volts[start];
        for (let i = start + 1; i < end; i += 1) {
          const v = volts[i];
          if (v < min) min = v;
          if (v > max) max = v;
        }

        const yMin = height / 2 - (min / fullScale + shift) * (height / 2);
        const yMax = height / 2 - (max / fullScale + shift) * (height / 2);
        if (column === 0) ctx.moveTo(column, yMax);
        ctx.lineTo(column, yMax);
        ctx.lineTo(column, yMin);
      }
      ctx.stroke();

      if (frame.overflowed && frame.overflowed(index)) overflowed.push(label);
    });

    // Clipping silently distorts every measurement taken from the capture,
    // so it has to be visible rather than inferred from a flat top.
    const warning = el('overflow-warning');
    if (overflowed.length) {
      warning.textContent = `Channel ${overflowed.join(', ')} clipped \u2014 `
        + 'increase volts/div';
      warning.hidden = false;
    } else {
      warning.hidden = true;
    }

    if (this.showTriggerMarkers) {
      // The time marker needs a triggered capture to mean anything -- in auto
      // mode an untriggered one has no trigger point to mark -- but the level
      // is a setting rather than a property of the capture, so it is drawn
      // either way. That is the case that matters: when nothing is
      // triggering, the level is exactly what you want to see.
      if (frame.flags & FLAG_TRIGGERED) {
        this.drawTriggerMarker(ctx, frame, width, height);
      }
      this.drawTriggerLevel(ctx, width, height);
    }

    // Last, so the readout box sits over the trace rather than under it.
    if (this.cursors) this.drawCursors(ctx, frame, width, height);
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
      // means a different height on a channel at a different volts/div.
      const state = this.channelState.get(channel)
        || this.channelState.values().next().value;
      const fullScale = ((state && state.voltsPerDiv) || 1) * 4;
      const shift = ((state && state.positionDiv) || 0) / 4;
      volts.forEach((v, i) => {
        const y = height / 2 - (v / fullScale + shift) * (height / 2);
        this.drawCursorLine(ctx, false, clamp(y, 1, height - 1), width, height,
          `v${i + 1}`);
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
  drawCursorLine(ctx, vertical, at, width, height, name) {
    const position = Math.round(at) + 0.5;
    ctx.strokeStyle = '#8b98a5';
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
    ctx.fillStyle = '#8b98a5';
    ctx.textBaseline = 'top';
    if (vertical) {
      ctx.fillText(name, Math.min(width - 14, position + 2), height - 14);
    } else {
      ctx.fillText(name, 2, Math.min(height - 14, position + 2));
    }
  }

  /** Where a time relative to the trigger falls, in pixels across the plot. */
  timeToX(frame, seconds, width) {
    const total = frame.samplesPerChannel;
    const index = frame.preTriggerSamples
      + (seconds * 1e9) / frame.sampleIntervalNs;
    return (index / total) * width;
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
    const shift = (state.positionDiv || 0) / 4;
    const exact = height / 2 - (level / fullScale + shift) * (height / 2);
    // A level beyond the top or bottom is pinned to that edge rather than
    // dropped. "Off screen, that way" is the useful thing to know, and a line
    // drawn outside the canvas looks identical to no trigger at all -- which
    // is the state someone reads as the feature being broken.
    const y = Math.min(height - 1, Math.max(1, exact));
    const offBy = exact < 1 ? '\u2191' : (exact > height - 1 ? '\u2193' : '');

    const styles = getComputedStyle(document.documentElement);
    const index = Math.max(0, [...this.channelState.keys()].indexOf(source));
    const colour = styles
      .getPropertyValue(CHANNEL_COLORS[index % CHANNEL_COLORS.length]).trim();

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
    const x = (frame.preTriggerSamples / total) * width;
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
      if (this.socket) this.disconnect(); else this.connect();
    });

    el('net-select').addEventListener('change', async (event) => {
      this.disconnect();
      this.net = event.target.value || null;
      this.adoptChannelNets();
      await this.loadCapabilities();
    });

    el('btn-start').addEventListener('click', async () => {
      await this.runCommand('start_capture', {}, 'start');
      this.startMeasurementPolling();
    });
    // Single takes one capture and stops, so one reading is the whole of what
    // there is to show; polling would keep re-arming a scope the user stopped.
    el('btn-single').addEventListener('click', async () => {
      await this.runCommand('start_single', {}, 'single');
      this.stopMeasurementPolling();
      this.refreshMeasurements();
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

    // Horizontal position. On change rather than input: unlike the vertical
    // position this re-arms the scope, so it should not fire per keystroke
    // while a value is being typed.
    el('time-position').addEventListener('change', (event) => {
      const value = Number(event.target.value);
      if (event.target.value === '' || !Number.isFinite(value)) return;
      this.applyTimePosition(value);
    });
    el('time-position-reset').addEventListener('click', () => this.applyTimePosition(0));

    const applyTrigger = () => {
      this.runCommand('trigger_edge', {
        level: Number(el('trigger-level').value),
        slope: el('trigger-slope').value,
        source: el('trigger-source').value,
        mode: el('trigger-mode').value,
      }, 'trigger');
    };
    for (const id of ['trigger-slope', 'trigger-mode']) {
      el(id).addEventListener('change', applyTrigger);
    }
    // Source also decides which channel's scale the level line is drawn
    // against, so the plot changes with it and not only the hardware.
    el('trigger-source').addEventListener('change', () => {
      applyTrigger();
      this.requestRedraw();
    });
    // On the number input, react to committed edits rather than each
    // keystroke, which would send a command per digit.
    el('trigger-level').addEventListener('change', () => {
      // Redraw as well as send: the level line moves with this field, and
      // waiting for the next capture to show where the trigger went defeats
      // the point of drawing it.
      applyTrigger();
      this.requestRedraw();
    });

    el('trigger-markers').addEventListener('change', (event) => {
      this.showTriggerMarkers = event.target.checked;
      this.requestRedraw();
    });

    el('btn-clear').addEventListener('click', () => this.console.clear());
  }

  wireConsole() {
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

  async execute(line) {
    const verb = line.split(/\s+/)[0].toLowerCase();

    // Page-local verbs never reach the box.
    if (verb === 'help') return this.printHelp();
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
    return this.runCommand(parsed.action, parsed.params, parsed.summary);
  }

  printHelp() {
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
export { ScopeApp, voltsPerDivChoices, timebaseChoices, sampleTraceAt,
         PER_CHANNEL_ACTIONS, isPerChannelAction };

if (typeof document !== 'undefined') {
  const app = new ScopeApp();
  app.init();
}
