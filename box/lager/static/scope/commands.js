// Copyright 2024-2026 Lager Data
// SPDX-License-Identifier: Apache-2.0

/**
 * Command grammar for the in-browser scope CLI.
 *
 * Every entry parses a typed line into the `{action, params}` pair that
 * `POST /net/command` takes -- the same action vocabulary the terminal
 * `lager scope` CLI and the Python driver use. There is deliberately no
 * browser-only path to the hardware: a command typed here and the equivalent
 * terminal command reach the same handler with the same arguments, so the two
 * cannot drift in behavior.
 *
 * That is why the table below names actions like `set_scale` rather than
 * inventing UI-friendly ones. The nicer spelling belongs in `usage`.
 */

/** A parsed command, ready to POST. */
export class ParsedCommand {
  constructor(action, params, summary, channel) {
    this.action = action;
    this.params = params || {};
    this.summary = summary || action;
    // Which channel the verb named, when it named one. Not a parameter of
    // the action: the box addresses a channel by which net the request is
    // sent to, and a letter here would be an argument the handler does not
    // read.
    this.channel = channel || null;
    // Handled by the page, with no action to send.
    this.local = false;
  }
}

export class CommandError extends Error {}

/** A channel letter, or null when the command named none.
 *
 * "1" is A and "2" is B, the same numbering the channel nets use for their
 * pins. Anything else is a typo, not a channel the scope can have.
 */
function channelToken(token) {
  if (token === undefined) return null;
  const name = String(token).trim().toUpperCase();
  if (/^[A-D]$/.test(name)) return name;
  if (/^[1-4]$/.test(name)) return String.fromCharCode(64 + Number(name));
  throw new CommandError(`channel must be A-D, got "${token}"`);
}

/** A channel named ahead of a setting's value, and the tokens after it.
 *
 * A letter only. These verbs take a number next, and channelToken() reads
 * "2" as channel B, so "scale 2" would be ambiguous: here a digit is always
 * the value. A single letter past D is still a channel, and a wrong one.
 */
function leadingChannel(args) {
  if (args.length && /^[A-Za-z]$/.test(args[0])) {
    return [channelToken(args[0]), args.slice(1)];
  }
  return [null, args];
}

/** The value of a channel setting, refusing anything after it.
 *
 * "scale 0.5 B" is the channel typed in the wrong place. Taking the 0.5 and
 * dropping the B would set the wrong channel and report success.
 */
function settingValue(verb, rest) {
  if (rest.length > 1) {
    throw new CommandError(
      `${verb} takes a channel first, then one value, e.g. "${verb} B ${rest[0]}"`);
  }
  return rest[0];
}

/** Summary text for a channel setting, with the channel the user typed. */
function settingSummary(verb, channel, rest) {
  return [verb, channel, ...rest].filter(Boolean).join(' ');
}

function requireNumber(token, what) {
  if (token === undefined) {
    throw new CommandError(`${what} is required`);
  }
  // Accept engineering notation (1e-3, 500m is not accepted -- explicit is
  // better than clever when a wrong scale can saturate an input).
  const value = Number(token);
  if (!Number.isFinite(value)) {
    throw new CommandError(`${what} must be a number, got "${token}"`);
  }
  return value;
}

// SI prefixes a typed quantity may carry. Lower-case only: "M" is mega, and
// a 1000x error in the other direction is not one to guess at.
const PREFIXES = { n: 1e-9, u: 1e-6, '\u00b5': 1e-6, m: 1e-3 };

/** A number in `unit`, bare or with the unit: "0.1", "1e-3" and "100ms" are
 * all seconds.
 *
 * A prefix without the unit ("100m") is still refused, as requireNumber()
 * refuses it.
 */
function requireQuantity(token, unit, what) {
  if (token === undefined) {
    throw new CommandError(`${what} is required`);
  }
  const text = String(token).trim();
  const match = new RegExp(`^(.*?)([num\u00b5]?)${unit}$`, 'i').exec(text);
  if (match && match[1] !== '' && Number.isFinite(Number(match[1]))) {
    const prefix = match[2];
    if (prefix && !(prefix in PREFIXES)) {
      throw new CommandError(`${what} must be a number, got "${token}"`);
    }
    return Number(match[1]) * (prefix ? PREFIXES[prefix] : 1);
  }
  return requireNumber(text, what);
}

const MEASUREMENTS = {
  vpp: 'measure_vpp',
  vmax: 'measure_vmax',
  vmin: 'measure_vmin',
  vrms: 'measure_vrms',
  vavg: 'measure_vavg',
  period: 'measure_period',
  freq: 'measure_freq',
  frequency: 'measure_freq',
  'duty-pos': 'measure_dc_pos',
  'duty-neg': 'measure_dc_neg',
  'width-pos': 'measure_pulse_width_pos',
  'width-neg': 'measure_pulse_width_neg',
  rise: 'measure_rise_time',
  fall: 'measure_fall_time',
  overshoot: 'measure_overshoot',
  // One capture, every quantity, and a set that agrees with itself. The
  // others take a capture each, so reading several in a row samples a live
  // signal at different moments.
  all: 'measure_all',
};

/**
 * The grammar. Each verb maps a token list to a ParsedCommand.
 *
 * `local` verbs are handled by the page (help, clear, connect) and never
 * reach the box; they carry no action. `vpos` is one too, but is parsed here
 * because it names a channel the way the settings below do.
 */
export const COMMANDS = [
  {
    verb: 'enable',
    usage: 'enable [<channel>]',
    help: 'Enable a channel, e.g. "enable B". With no channel, the first one',
    parse: (args) => {
      const channel = channelToken(args[0]);
      return new ParsedCommand(
        'enable_net', {}, channel ? `enable ${channel}` : 'enable', channel);
    },
  },
  {
    verb: 'disable',
    usage: 'disable [<channel>]',
    help: 'Disable a channel, e.g. "disable B". With no channel, the first one',
    parse: (args) => {
      const channel = channelToken(args[0]);
      return new ParsedCommand(
        'disable_net', {}, channel ? `disable ${channel}` : 'disable', channel);
    },
  },
  {
    verb: 'start',
    usage: 'start [single]',
    help: 'Start acquisition; "single" arms one capture',
    parse: (args) => (args[0] === 'single'
      ? new ParsedCommand('start_single', {}, 'start single')
      : new ParsedCommand('start_capture', {}, 'start')),
  },
  {
    verb: 'stop',
    usage: 'stop',
    help: 'Stop acquisition',
    parse: () => new ParsedCommand('stop_capture', {}, 'stop'),
  },
  {
    verb: 'force',
    usage: 'force',
    help: 'Trigger now instead of waiting for the condition',
    parse: () => new ParsedCommand('force_trigger', {}, 'force'),
  },
  {
    verb: 'scale',
    usage: 'scale [<channel>] [<volts-per-div>]',
    help: 'Get or set vertical scale, e.g. "scale B 0.5"; with no channel, the first one that is on',
    parse: (args) => {
      const [channel, rest] = leadingChannel(args);
      settingValue('scale', rest);
      const summary = settingSummary('scale', channel, rest);
      return rest.length === 0
        ? new ParsedCommand('get_scale', {}, summary, channel)
        : new ParsedCommand('set_scale',
          { volts_per_div: requireNumber(rest[0], 'volts-per-div') }, summary, channel);
    },
  },
  {
    verb: 'timebase',
    usage: 'timebase [<seconds-per-div>]',
    help: 'Get or set horizontal scale, e.g. "timebase 1e-3"',
    parse: (args) => (args.length === 0
      ? new ParsedCommand('get_timebase', {}, 'timebase')
      : new ParsedCommand('set_timebase',
        { seconds_per_div: requireNumber(args[0], 'seconds-per-div') },
        `timebase ${args[0]}`)),
  },
  {
    verb: 'coupling',
    usage: 'coupling [<channel>] [dc|ac|gnd]',
    help: 'Get or set input coupling, e.g. "coupling B ac"; with no channel, the first one that is on',
    parse: (args) => {
      const [channel, rest] = leadingChannel(args);
      settingValue('coupling', rest);
      const summary = settingSummary('coupling', channel, rest);
      return rest.length === 0
        ? new ParsedCommand('get_coupling', {}, summary, channel)
        : new ParsedCommand('set_coupling', { mode: rest[0] }, summary, channel);
    },
  },
  {
    verb: 'probe',
    usage: 'probe [<channel>] [<ratio>]',
    help: 'Get or set probe attenuation, e.g. "probe B 10"; with no channel, the first one that is on',
    parse: (args) => {
      const [channel, rest] = leadingChannel(args);
      settingValue('probe', rest);
      const summary = settingSummary('probe', channel, rest);
      return rest.length === 0
        ? new ParsedCommand('get_probe', {}, summary, channel)
        : new ParsedCommand('set_probe',
          { ratio: requireNumber(rest[0], 'ratio') }, summary, channel);
    },
  },
  {
    verb: 'offset',
    usage: 'offset [<channel>] [<volts>]',
    help: 'Get or set analog offset, e.g. "offset B 0.1". Shifts the signal in hardware, '
      + 'so measurements change with it and a smaller range can be used; not on every scope '
      + '(to move only the drawing, see vpos)',
    parse: (args) => {
      const [channel, rest] = leadingChannel(args);
      settingValue('offset', rest);
      const summary = settingSummary('offset', channel, rest);
      return rest.length === 0
        ? new ParsedCommand('get_offset', {}, summary, channel)
        : new ParsedCommand('set_offset',
          { offset: requireNumber(rest[0], 'volts') }, summary, channel);
    },
  },
  {
    // Seconds rather than divisions, so the verb does not depend on how many
    // divisions this screen happens to draw. Positive looks forward, to
    // signal later than the trigger; negative looks back before it. The page
    // holds a set to the screen's divisions before sending it, as it does for
    // the sidebar field (ScopeApp.execute).
    verb: 'hpos',
    aliases: ['position'],
    usage: 'hpos [<seconds>]',
    help: 'Get or set horizontal position from the trigger, e.g. "hpos 2e-3" or "hpos 100ms"',
    parse: (args) => (args.length === 0
      ? new ParsedCommand('get_time_offset', {}, 'hpos')
      : new ParsedCommand('set_time_offset',
        { offset: requireQuantity(args[0], 's', 'seconds') }, `hpos ${args[0]}`)),
  },
  {
    // A verb that changes nothing on the box: the page draws the trace higher
    // or lower. The analog offset would move Vmax, Vmin and Vavg too.
    verb: 'vpos',
    usage: 'vpos [<channel>] [<volts>]',
    help: 'Get or set where a trace is drawn, e.g. "vpos B -0.5" or "vpos B 200mV". '
      + 'Display only: the capture and every measurement are unchanged, and it '
      + 'resets when the page reloads (to shift the signal itself, see offset)',
    parse: (args) => {
      const [channel, rest] = leadingChannel(args);
      settingValue('vpos', rest);
      const params = rest.length === 0
        ? {} : { volts: requireQuantity(rest[0], 'V', 'volts') };
      const parsed = new ParsedCommand(
        null, params, settingSummary('vpos', channel, rest), channel);
      parsed.local = true;
      return parsed;
    },
  },
  {
    verb: 'measure',
    usage: `measure [<channel>] <${Object.keys(MEASUREMENTS).slice(0, 6).join('|')}|...>`,
    help: 'Measure the live signal, e.g. "measure B vpp"; with no channel, the first one that is on',
    parse: (args) => {
      // The channel first, as the net comes first in the terminal's
      // `lager scope <net> measure vpp`. A digit is a channel here too: no
      // measurement is a number, so "measure 2 freq" can only mean B.
      const lone = (token) => token !== undefined && /^[A-Za-z0-9]$/.test(token);
      const channel = lone(args[0]) ? channelToken(args[0]) : null;
      const rest = channel ? args.slice(1) : args;
      if (rest.length === 0) {
        throw new CommandError(
          `measure what? one of: ${Object.keys(MEASUREMENTS).join(', ')}`);
      }
      if (lone(rest[0])) {
        throw new CommandError(
          `measure takes one channel, then one measurement, got "${args.join(' ')}"`);
      }
      const name = rest[0].toLowerCase();
      const action = Object.prototype.hasOwnProperty.call(MEASUREMENTS, name)
        ? MEASUREMENTS[name] : null;
      if (!action) {
        throw new CommandError(
          `unknown measurement "${rest[0]}"; try one of: `
          + Object.keys(MEASUREMENTS).join(', '));
      }
      if (rest.length > 1) {
        // "measure vpp B" names the channel last. It is refused, not read
        // either way, so every verb that names a channel has one order.
        const named = channel || (lone(rest[1]) ? channelToken(rest[1]) : 'B');
        throw new CommandError(
          `measure takes a channel first, then one measurement, e.g. "measure ${named} ${rest[0]}"`);
      }
      return new ParsedCommand(action, {},
        channel ? `measure ${channel} ${rest[0]}` : `measure ${rest[0]}`, channel);
    },
  },
  {
    verb: 'trigger',
    usage: 'trigger [level <v>] [slope rising|falling] [source <ch>] [mode auto|normal|single]',
    help: 'Configure the edge trigger; only the parts you name change; alone, show it',
    parse: (args) => {
      // Reading it back, as `lager scope <net> trigger` does.
      if (args.length === 0) return new ParsedCommand('get_trigger', {}, 'trigger');
      // `trigger edge ...` is accepted because that is how the terminal CLI
      // spells it (`lager scope trigger edge`).
      const tokens = args[0] === 'edge' ? args.slice(1) : args.slice();
      const params = {};
      while (tokens.length) {
        const key = tokens.shift().toLowerCase();
        const value = tokens.shift();
        if (value === undefined) {
          throw new CommandError(`"${key}" needs a value`);
        }
        if (key === 'level') params.level = requireNumber(value, 'level');
        else if (key === 'slope') params.slope = value;
        else if (key === 'source') params.source = value;
        else if (key === 'coupling') params.coupling = value;
        else if (key === 'mode') params.mode = value;
        else throw new CommandError(`unknown trigger setting "${key}"`);
      }
      if (Object.keys(params).length === 0) {
        throw new CommandError('trigger needs at least one setting');
      }
      return new ParsedCommand('trigger_edge', params, `trigger ${args.join(' ')}`);
    },
  },
  {
    // Cursors are typed only -- there is no handle on the plot to drag and no
    // field in the sidebar. They are kept on the box, not in this page, so
    // that a pair placed from the terminal `lager scope ... cursor` is the
    // pair drawn here.
    verb: 'cursor',
    usage: 'cursor [time <t1> <t2> | volts <v1> <v2> | off]',
    help: 'Place cursors and read the deltas; "cursor" alone reads them',
    parse: (args) => {
      if (args.length === 0) {
        return new ParsedCommand('measure_cursor', {}, 'cursor');
      }
      const kind = args[0].toLowerCase();
      if (kind === 'off') {
        return new ParsedCommand('clear_cursor', {}, 'cursor off');
      }
      if (kind !== 'time' && kind !== 'volts') {
        throw new CommandError(
          `unknown cursor "${args[0]}"; try "cursor time <t1> <t2>", `
          + '"cursor volts <v1> <v2>", or "cursor off"');
      }
      // Both at once, because one cursor of a pair reads nothing: the
      // quantity wanted is the difference between them.
      const what = kind === 'time' ? 'seconds' : 'volts';
      const pair = [
        requireNumber(args[1], `first cursor in ${what}`),
        requireNumber(args[2], `second cursor in ${what}`),
      ];
      return new ParsedCommand('set_cursor', { [kind]: pair },
        `cursor ${args.slice(0, 3).join(' ')}`);
    },
  },
  {
    verb: 'holdoff',
    usage: 'holdoff [<seconds>]',
    help: 'Get or set trigger holdoff, e.g. "holdoff 1e-3"',
    parse: (args) => (args.length === 0
      ? new ParsedCommand('get_trigger_holdoff', {}, 'holdoff')
      : new ParsedCommand('set_trigger_holdoff',
        { seconds: requireNumber(args[0], 'seconds') }, `holdoff ${args[0]}`)),
  },
  {
    verb: 'acquire',
    usage: 'acquire [normal | average [<count>] | peak]',
    help: 'Get or set how captures combine, e.g. "acquire average 64"',
    parse: (args) => {
      if (args.length === 0) return new ParsedCommand('get_acquire', {}, 'acquire');
      const mode = args[0].toLowerCase();
      if (!['normal', 'average', 'peak'].includes(mode)) {
        throw new CommandError(`unknown acquisition mode "${args[0]}"; try normal, average or peak`);
      }
      const params = { mode };
      if (args[1] !== undefined) {
        if (mode !== 'average') throw new CommandError('only average takes a count');
        params.count = requireNumber(args[1], 'count');
      }
      return new ParsedCommand('set_acquire', params, `acquire ${args.join(' ')}`);
    },
  },
  {
    verb: 'roll',
    usage: 'roll [auto|on|off]',
    help: 'Get or set roll mode for slow timebases',
    parse: (args) => (args.length === 0
      ? new ParsedCommand('get_roll', {}, 'roll')
      : new ParsedCommand('set_roll', { mode: args[0].toLowerCase() }, `roll ${args[0]}`)),
  },
  {
    verb: 'status',
    usage: 'status',
    help: 'Show every setting: channels, timebase, trigger, acquisition',
    parse: () => new ParsedCommand('get_state', {}, 'status'),
  },
  {
    verb: 'display',
    usage: 'display',
    help: 'Show the display settings below',
    parse: () => new ParsedCommand('get_display', {}, 'display'),
  },
  {
    verb: 'persistence',
    usage: 'persistence <seconds|infinite|off>',
    help: 'Let traces linger and fade, e.g. "persistence 2"',
    parse: (args) => {
      if (args[0] === undefined) throw new CommandError('persistence needs seconds, infinite or off');
      const value = args[0].toLowerCase();
      const persistence = (value === 'off' || value === 'infinite')
        ? value : requireNumber(args[0], 'seconds');
      return new ParsedCommand('set_display', { persistence }, `persistence ${args[0]}`);
    },
  },
  {
    verb: 'xy',
    usage: 'xy on|off',
    help: 'Plot channel B against channel A',
    parse: (args) => {
      const value = (args[0] || '').toLowerCase();
      if (value !== 'on' && value !== 'off') throw new CommandError('xy takes on or off');
      return new ParsedCommand('set_display', { xy: value }, `xy ${value}`);
    },
  },
  {
    verb: 'zoom',
    usage: 'zoom <factor|off> [<center-seconds>]',
    help: 'Magnify around a time from the trigger, e.g. "zoom 8 1e-3"',
    parse: (args) => {
      if (args[0] === undefined) throw new CommandError('zoom needs a factor or off');
      if (args[0].toLowerCase() === 'off') {
        return new ParsedCommand('set_display', { zoom: 'off' }, 'zoom off');
      }
      const zoom = { factor: requireNumber(args[0], 'factor') };
      if (args[1] !== undefined) zoom.center = requireNumber(args[1], 'center');
      return new ParsedCommand('set_display', { zoom }, `zoom ${args.join(' ')}`);
    },
  },
  {
    verb: 'math',
    usage: 'math <a+b|a-b|a*b|off>',
    help: 'Draw a trace computed from two channels',
    parse: (args) => {
      if (args[0] === undefined) throw new CommandError('math needs an expression like a-b, or off');
      return new ParsedCommand('set_display', { math: args.join('') }, `math ${args.join(' ')}`);
    },
  },
  {
    verb: 'fft',
    usage: 'fft <channel|off> [hann|hamming|blackman|flattop|rectangular]',
    help: 'Show a spectrum pane under the trace',
    parse: (args) => {
      if (args[0] === undefined) throw new CommandError('fft needs a channel, or off');
      const fft = args[0].toLowerCase() === 'off'
        ? 'off' : { channel: args[0].toUpperCase(), window: (args[1] || 'hann').toLowerCase() };
      return new ParsedCommand('set_display', { fft }, `fft ${args.join(' ')}`);
    },
  },
  {
    verb: 'spectrum',
    usage: 'spectrum [<channel>] [<peaks>]',
    help: 'List the strongest frequency components, computed on the box',
    parse: (args) => {
      const params = {};
      if (args[0] !== undefined) params.channel = args[0].toUpperCase();
      if (args[1] !== undefined) params.peaks = requireNumber(args[1], 'peaks');
      return new ParsedCommand('fft', params, `spectrum ${args.join(' ')}`.trim());
    },
  },
  {
    verb: 'capabilities',
    usage: 'capabilities',
    help: 'Show what the attached scope supports',
    parse: () => new ParsedCommand('capabilities', {}, 'capabilities'),
  },
  {
    verb: 'autoscale',
    usage: 'autoscale',
    help: 'Autoscale (Rigol only; PicoScope reports that it has none)',
    parse: () => new ParsedCommand('autoscale', {}, 'autoscale'),
  },
  {
    // Page chrome. The trace and the console keep the width the sidebar had.
    verb: 'sidebar',
    usage: 'sidebar [on|off|show|hide]',
    help: 'Hide or show the controls sidebar, e.g. "sidebar off". '
      + 'With no argument, say whether it is hidden. The page keeps the choice',
    parse: (args) => {
      if (args.length > 1) {
        throw new CommandError('sidebar takes on, off, show or hide');
      }
      const word = args.length ? args[0].toLowerCase() : '';
      const shown = { on: true, show: true, off: false, hide: false }[word];
      if (word && shown === undefined) {
        throw new CommandError(
          `sidebar takes on, off, show or hide, got "${args[0]}"`);
      }
      const parsed = new ParsedCommand(
        null, word ? { shown } : {}, word ? `sidebar ${word}` : 'sidebar');
      parsed.local = true;
      return parsed;
    },
  },
];

/** Verbs the page handles itself, listed so `help` can show them. */
export const LOCAL_COMMANDS = [
  { verb: 'help', usage: 'help [<command>]',
    help: 'List commands, or one of them: "help trigger" or "trigger --help"' },
  { verb: 'clear', usage: 'clear', help: 'Clear the console' },
  { verb: 'connect', usage: 'connect', help: 'Reconnect the capture stream' },
  { verb: 'disconnect', usage: 'disconnect', help: 'Stop the capture stream' },
];

// Old spellings, still parsed but not offered: `position` became `hpos` when
// `vpos` arrived beside it, and a bare "position" no longer said which way.
const BY_VERB = new Map(COMMANDS.flatMap(
  (c) => [c.verb, ...(c.aliases || [])].map((verb) => [verb, c])));

/**
 * Split a command line, honoring double quotes so a value may contain spaces.
 */
export function tokenize(line) {
  const tokens = [];
  const pattern = /"([^"]*)"|(\S+)/g;
  let match = pattern.exec(line);
  while (match !== null) {
    tokens.push(match[1] !== undefined ? match[1] : match[2]);
    match = pattern.exec(line);
  }
  return tokens;
}

/**
 * Parse a line into a ParsedCommand, or throw CommandError with a message
 * meant to be shown to the user verbatim.
 */
export function parse(line) {
  const tokens = tokenize(line.trim());
  if (tokens.length === 0) {
    throw new CommandError('empty command');
  }
  const verb = tokens[0].toLowerCase();
  const entry = BY_VERB.get(verb);
  if (!entry) {
    throw new CommandError(
      `unknown command "${verb}"; type "help" to see what is available`);
  }
  return entry.parse(tokens.slice(1));
}

/** Verbs matching a prefix, for tab completion. */
export function complete(prefix) {
  const lowered = prefix.toLowerCase();
  const all = [...COMMANDS.map((c) => c.verb), ...LOCAL_COMMANDS.map((c) => c.verb)];
  return all.filter((verb) => verb.startsWith(lowered)).sort();
}

const HELP_ENTRIES = [...COMMANDS, ...LOCAL_COMMANDS];

/** Help text as `[usage, help]` rows. */
export function helpRows() {
  return HELP_ENTRIES.map((c) => [c.usage, c.help]);
}

/** One command's `[usage, help]`, or null when `name` is not a verb. */
export function helpFor(name) {
  const verb = String(name || '').trim().toLowerCase();
  const entry = HELP_ENTRIES.find(
    (c) => c.verb === verb || (c.aliases || []).includes(verb));
  return entry ? [entry.usage, entry.help] : null;
}

/**
 * The command a line is asking about, when the line is a help request.
 *
 * `help` alone asks for the whole list (`topic` is ''). `help trigger` and
 * `trigger --help` ask for that one command. A bare word `help` is not a
 * request: `trigger help` looks like a trigger setting, so it is left for
 * the command to reject. A line that is not asking for help returns null.
 */
export function helpTopic(line) {
  const tokens = tokenize(String(line || '').trim());
  if (tokens.length === 0) return null;
  const verb = tokens[0].toLowerCase();
  if (verb === 'help') return tokens[1] ? tokens[1].toLowerCase() : '';
  if (tokens.slice(1).some((token) => token === '--help')) return verb;
  return null;
}
