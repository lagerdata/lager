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
  constructor(action, params, summary) {
    this.action = action;
    this.params = params || {};
    this.summary = summary || action;
  }
}

export class CommandError extends Error {}

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
};

/**
 * The grammar. Each verb maps a token list to a ParsedCommand.
 *
 * `local` verbs are handled by the page (help, clear, connect) and never
 * reach the box; they carry no action.
 */
export const COMMANDS = [
  {
    verb: 'enable',
    usage: 'enable',
    help: 'Enable this net\u2019s channel',
    parse: () => new ParsedCommand('enable_net', {}, 'enable'),
  },
  {
    verb: 'disable',
    usage: 'disable',
    help: 'Disable this net\u2019s channel',
    parse: () => new ParsedCommand('disable_net', {}, 'disable'),
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
    usage: 'scale [<volts-per-div>]',
    help: 'Get or set vertical scale, e.g. "scale 0.5"',
    parse: (args) => (args.length === 0
      ? new ParsedCommand('get_scale', {}, 'scale')
      : new ParsedCommand('set_scale',
        { volts_per_div: requireNumber(args[0], 'volts-per-div') },
        `scale ${args[0]}`)),
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
    usage: 'coupling [dc|ac|gnd]',
    help: 'Get or set input coupling',
    parse: (args) => (args.length === 0
      ? new ParsedCommand('get_coupling', {}, 'coupling')
      : new ParsedCommand('set_coupling', { mode: args[0] }, `coupling ${args[0]}`)),
  },
  {
    verb: 'probe',
    usage: 'probe [<ratio>]',
    help: 'Get or set probe attenuation, e.g. "probe 10"',
    parse: (args) => (args.length === 0
      ? new ParsedCommand('get_probe', {}, 'probe')
      : new ParsedCommand('set_probe',
        { ratio: requireNumber(args[0], 'ratio') }, `probe ${args[0]}`)),
  },
  {
    verb: 'offset',
    usage: 'offset [<volts>]',
    help: 'Get or set vertical offset',
    parse: (args) => (args.length === 0
      ? new ParsedCommand('get_offset', {}, 'offset')
      : new ParsedCommand('set_offset',
        { offset: requireNumber(args[0], 'volts') }, `offset ${args[0]}`)),
  },
  {
    verb: 'measure',
    usage: `measure <${Object.keys(MEASUREMENTS).slice(0, 6).join('|')}|...>`,
    help: 'Measure the live signal; "measure" alone lists the options',
    parse: (args) => {
      if (args.length === 0) {
        throw new CommandError(
          `measure what? one of: ${Object.keys(MEASUREMENTS).join(', ')}`);
      }
      const action = MEASUREMENTS[args[0].toLowerCase()];
      if (!action) {
        throw new CommandError(
          `unknown measurement "${args[0]}"; try one of: `
          + Object.keys(MEASUREMENTS).join(', '));
      }
      return new ParsedCommand(action, {}, `measure ${args[0]}`);
    },
  },
  {
    verb: 'trigger',
    usage: 'trigger [level <v>] [slope rising|falling] [source <ch>] [mode auto|normal|single]',
    help: 'Configure the edge trigger; only the parts you name change',
    parse: (args) => {
      if (args.length === 0) {
        throw new CommandError('trigger what? e.g. "trigger level 1.2 slope rising"');
      }
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
];

/** Verbs the page handles itself, listed so `help` can show them. */
export const LOCAL_COMMANDS = [
  { verb: 'help', usage: 'help', help: 'List commands' },
  { verb: 'clear', usage: 'clear', help: 'Clear the console' },
  { verb: 'connect', usage: 'connect', help: 'Reconnect the capture stream' },
  { verb: 'disconnect', usage: 'disconnect', help: 'Stop the capture stream' },
];

const BY_VERB = new Map(COMMANDS.map((c) => [c.verb, c]));

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

/** Help text as `[usage, help]` rows. */
export function helpRows() {
  return [...COMMANDS, ...LOCAL_COMMANDS].map((c) => [c.usage, c.help]);
}
