#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Fail on a `try` whose broad handler can swallow its own `ctx.exit()`.

`click.exceptions.Exit` and `Abort` subclass RuntimeError, so a bare
`except Exception` catches every deliberate `ctx.exit(N)` raised inside its own
try block. The exception is then reported as though it were a crash, and the
handler's own `ctx.exit(1)` overwrites the code the command asked for.

That is invisible in review: the handler looks like ordinary error handling and
the `ctx.exit()` looks like ordinary control flow. Only running the command
shows the traceback. It had reached eleven try blocks across seven files before
anyone noticed, including:

  * `lager update --check` exited 1 where it asked for 2, so the integration
    workflow could not tell "in sync" from "never reached the box" and had to
    parse the `Code:` line out of stdout instead.
  * `lager binaries remove <nonexistent>` printed "binary not found", had its
    `ctx.exit(1)` cancelled by `except Exception: pass`, and carried on into
    the removal.
  * `lager uart` retried an entire session because the `ctx.exit()` that ends
    it read as a connection error, then rewrote the session's code to 1. A
    `str(last_error) != "0"` comparison against `str(Exit(0))` had been added
    to paper over the clean-disconnect case.

The fix at each site is one handler, ordered before the broad one:

    except (Exit, Abort):
        raise

Ordering is what matters -- `except Exception` after it never sees control
flow. This check understands that ordering and only reports try blocks with no
such handler ahead of the broad one.
"""
import argparse
import ast
import pathlib
import sys

CONTROL_FLOW = {'Exit', 'Abort'}
DEFAULT_ROOTS = ['cli/commands']


def _names(node):
    """Exception names a handler catches, ignoring the module it came from."""
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, ast.Attribute):
        return [node.attr]
    if isinstance(node, ast.Tuple):
        return [n for e in node.elts for n in _names(e)]
    return []


def catches_control_flow(handler):
    return bool(CONTROL_FLOW & set(_names(handler.type))) if handler.type else False


def is_broad(handler):
    return handler.type is None or _names(handler.type) == ['Exception']


def control_flow_lines(body):
    """Lines in `body` that raise click control flow: ctx.exit() / raise Abort()."""
    module = ast.Module(body=body, type_ignores=[])
    lines = set()
    for node in ast.walk(module):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'exit'
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == 'ctx'):
            lines.add(node.lineno)
        elif (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)
                and _names(node.exc.func) in (['Abort'], ['Exit'])):
            lines.add(node.lineno)
    return sorted(lines)


def findings(roots):
    out = []
    for root in roots:
        for path in sorted(pathlib.Path(root).rglob('*.py')):
            try:
                tree = ast.parse(path.read_text(encoding='utf-8'))
            except (SyntaxError, UnicodeDecodeError):
                continue  # compileall in the same job owns syntax
            for node in ast.walk(tree):
                if not isinstance(node, ast.Try):
                    continue
                broad = next((i for i, h in enumerate(node.handlers) if is_broad(h)), None)
                if broad is None:
                    continue
                if any(catches_control_flow(h) for h in node.handlers[:broad]):
                    continue
                lines = control_flow_lines(node.body)
                if lines:
                    out.append((str(path), node.lineno,
                                node.handlers[broad].lineno, lines))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument('roots', nargs='*', default=DEFAULT_ROOTS,
                    help=f'directories to scan (default: {" ".join(DEFAULT_ROOTS)})')
    args = ap.parse_args()

    found = findings(args.roots)
    if not found:
        print(f'OK: no broad handler swallows its own control flow '
              f'({", ".join(args.roots)})')
        return 0

    print('FAIL: a broad `except` can swallow control flow raised in its own '
          'try block.\n', file=sys.stderr)
    for path, tryline, handler, lines in found:
        pretty = ', '.join(f'{path}:{n}' for n in lines)
        print(f'  {path}:{tryline}: try -- `except Exception` at line {handler}\n'
              f'      swallows: {pretty}', file=sys.stderr)
    print('\n  Add this ahead of the broad handler (see cli/commands/box/ssh.py):\n'
          '\n      except (Exit, Abort):\n          raise\n'
          '\n  with `from click.exceptions import Abort, Exit` at the top of the '
          'file.\n  Order matters: a handler placed after `except Exception` '
          'never runs.', file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
