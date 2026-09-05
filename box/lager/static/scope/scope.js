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

const VOLTS_PER_DIV = [0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5];

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
    this.socket = null;
    this.capabilities = null;
    this.latest = null;
    this.dirty = false;
    this.channelState = new Map();

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
        (n) => n.role === 'scope' || n.role === 'analog');

      select.replaceChildren();
      if (nets.length === 0) {
        select.append(new Option('no scope nets', ''));
        this.console.error(
          'No scope nets on this box. Create one with "lager net add".');
        return;
      }
      for (const net of nets) {
        select.append(new Option(net.name, net.name));
      }
      this.net = nets[0].name;
      select.value = this.net;
      await this.loadCapabilities();
    } catch (e) {
      select.replaceChildren(new Option('unavailable', ''));
      this.console.error(`Could not list nets: ${e.message}`);
    }
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
      this.channelState.set(label, { enabled: index === 0, voltsPerDiv: 1 });
      host.appendChild(this.buildChannelStrip(label, index, caps));
    });

    // Trigger sources are exactly the channels that exist.
    const source = el('trigger-source');
    source.replaceChildren();
    labels.forEach((label) => source.append(new Option(label, label)));

    // Timebase choices.
    const timebase = el('timebase');
    timebase.replaceChildren();
    for (const value of TIMEBASES) {
      timebase.append(new Option(si(value, 's', 2), String(value)));
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
    toggle.addEventListener('change', () => {
      this.channelState.get(label).enabled = toggle.checked;
      this.send(toggle.checked ? 'enable_net' : 'disable_net', {});
    });
    toggleLabel.append(toggle, document.createTextNode('on'));
    head.append(swatch, name, toggleLabel);

    // Volts/div, restricted to the ranges this unit reports when it reports
    // any, so the list cannot offer a range the hardware will refuse.
    const field = document.createElement('label');
    field.className = 'field field--stack';
    const caption = document.createElement('span');
    caption.textContent = 'Volts / div';
    const select = document.createElement('select');
    const options = (caps.voltage_ranges && caps.voltage_ranges.length)
      ? caps.voltage_ranges.map((r) => r.full_scale_volts / 4)
      : VOLTS_PER_DIV;
    for (const value of options) {
      select.append(new Option(si(value, 'V', 2), String(value)));
    }
    select.value = String(options[Math.min(options.length - 1, 6)]);
    select.addEventListener('change', () => {
      this.channelState.get(label).voltsPerDiv = Number(select.value);
      this.send('set_scale', { volts_per_div: Number(select.value) });
    });
    field.append(caption, select);

    strip.append(head, field);
    return strip;
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
  async send(action, params) {
    if (!this.net) throw new Error('no scope net selected');
    const response = await fetch('/net/command', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ netname: this.net, action, params: params || {} }),
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

  async runCommand(action, params, summary) {
    try {
      const body = await this.send(action, params);
      this.console.write(body.message || `${summary || action}: ok`);
      if (action === 'capabilities' && body.value) {
        this.capabilities = body.value;
        this.applyCapabilities();
      }
      return body;
    } catch (e) {
      this.console.error(e.message);
      return null;
    }
  }

  // ---------- measurements ----------
  async refreshMeasurements() {
    const host = el('measurements');
    if (!this.net) return;
    try {
      const body = await this.send('measure_vpp', {});
      // The daemon computes the whole set from one capture, but the REST
      // action returns a single value; show the ones the UI cares about with
      // one request each only when a capture is running.
      host.replaceChildren();
      const rows = [['Vpp', body.value, 'V']];
      for (const [label, action, unit] of [
        ['Vmax', 'measure_vmax', 'V'],
        ['Vmin', 'measure_vmin', 'V'],
        ['Vrms', 'measure_vrms', 'V'],
        ['Freq', 'measure_freq', 'Hz'],
      ]) {
        try {
          const r = await this.send(action, {});
          rows.push([label, r.value, unit]);
        } catch {
          rows.push([label, null, unit]);
        }
      }
      for (const [label, value, unit] of rows) {
        const dt = document.createElement('dt');
        dt.textContent = label;
        const dd = document.createElement('dd');
        dd.textContent = si(value, unit, 4);
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
      this.draw(this.latest);
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

        const yMin = height / 2 - (min / fullScale) * (height / 2);
        const yMax = height / 2 - (max / fullScale) * (height / 2);
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

    if (frame.flags & FLAG_TRIGGERED) {
      this.drawTriggerMarker(ctx, frame, width, height);
    }
  }

  drawGraticule(ctx, width, height) {
    ctx.strokeStyle = '#1c2430';
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (let i = 1; i < 10; i += 1) {
      const x = Math.round((width * i) / 10) + 0.5;
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
      await this.loadCapabilities();
    });

    el('btn-start').addEventListener('click', async () => {
      await this.runCommand('start_capture', {}, 'start');
      this.refreshMeasurements();
    });
    el('btn-single').addEventListener('click', () => this.runCommand('start_single', {}, 'single'));
    el('btn-stop').addEventListener('click', () => this.runCommand('stop_capture', {}, 'stop'));
    el('btn-force').addEventListener('click', () => this.runCommand('force_trigger', {}, 'force'));

    el('timebase').addEventListener('change', (event) => {
      this.runCommand('set_timebase', { seconds_per_div: Number(event.target.value) }, 'timebase');
    });

    const applyTrigger = () => {
      this.runCommand('trigger_edge', {
        level: Number(el('trigger-level').value),
        slope: el('trigger-slope').value,
        source: el('trigger-source').value,
        mode: el('trigger-mode').value,
      }, 'trigger');
    };
    for (const id of ['trigger-slope', 'trigger-source', 'trigger-mode']) {
      el(id).addEventListener('change', applyTrigger);
    }
    // On the number input, react to committed edits rather than each
    // keystroke, which would send a command per digit.
    el('trigger-level').addEventListener('change', applyTrigger);

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

const app = new ScopeApp();
app.init();
