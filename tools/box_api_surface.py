#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
List the box HTTP API a client can call, and diff it between two releases.

The Rust crate (lagerdata/lager-rs) is a client of this API. Nothing told it
when a release added to the API, so it fell sixteen releases behind (box
0.44.0 to 0.59.0) before anyone noticed. The `Release: lager-rs API check`
workflow runs this on every release tag, against the previous release, and
opens a lager-rs issue when the two differ.

WHAT IS READ
------------
The source, parsed with `ast`; nothing is imported or run, so this works on
any checkout without the box's dependencies.

  routes          every Flask route under box/lager: "<METHOD> <path>"
  route actions   for a route whose handler compares `action` with names,
                  those names (POST /supply/command -> voltage, current, ...)
  net/command     the roles in ROLE_ACTIONS, and each role's action names
  socketio        Socket.IO events, by namespace
  debug service   the :8765 service's paths and its SERVICE_FEATURES
  capabilities    the keys of the `capabilities` object /status returns

An action name is any string the handler compares `action` with (`==`, `!=`,
`in`, `not in`), including the strings in a module-level table it compares against
or indexes with `action`, and those of a module function it hands `action` to.

WHAT IS NOT
-----------
Parameters and response shapes. A release that adds a parameter to an existing
action, or changes what an action returns, produces no diff here. The issue
says so, so nobody reads "no issue" as "nothing to do".

USAGE
-----
  box_api_surface.py extract <checkout> [-o surface.json]
  box_api_surface.py diff <old.json> <new.json> --old-label v0.59.0 \\
      --new-label v0.60.0 [-o report.md]

`diff` prints "changed" or "unchanged", and writes the report only when the
surface changed.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple, Union

BOX_ROOT = Path("box") / "lager"
NET_COMMAND = BOX_ROOT / "http_handlers" / "net_command.py"
DEBUG_SERVICE = BOX_ROOT / "debug" / "service.py"
STATUS_SERVER = BOX_ROOT / "box_http_server.py"

SECTIONS = ("routes", "route_actions", "net_command", "socketio", "debug_service",
            "capabilities")


def _parse(path: Path) -> Optional[ast.Module]:
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return None


def _const_str(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _strings(node: ast.AST) -> Set[str]:
    """The string elements of a tuple/list/set literal, the string keys of a
    dict literal, or of a frozenset()/set()/tuple() call wrapping one."""
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return {s for s in map(_const_str, node.elts) if s is not None}
    if isinstance(node, ast.Dict):
        return {s for s in map(_const_str, (k for k in node.keys if k is not None))
                if s is not None}
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id in ("frozenset", "set", "tuple") and node.args):
        return _strings(node.args[0])
    return set()


def _module_tables(tree: ast.Module) -> Dict[str, Set[str]]:
    """Module-level NAME = <collection of strings>."""
    tables: Dict[str, Set[str]] = {}
    for stmt in tree.body:
        targets: Sequence[ast.expr] = []
        value = None
        if isinstance(stmt, ast.Assign):
            targets, value = stmt.targets, stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            targets, value = [stmt.target], stmt.value
        for target in targets:
            if isinstance(target, ast.Name) and value is not None:
                found = _strings(value)
                if found:
                    tables[target.id] = found
    return tables


def _module_functions(tree: ast.Module) -> Dict[str, ast.FunctionDef]:
    return {s.name: s for s in tree.body if isinstance(s, ast.FunctionDef)}


def _is_action(node: ast.AST) -> bool:
    return isinstance(node, ast.Name) and node.id == "action"


def _action_names(func: ast.FunctionDef, tables: Dict[str, Set[str]],
                  functions: Dict[str, ast.FunctionDef],
                  seen: Optional[Set[str]] = None) -> Set[str]:
    """Every action name `func` dispatches on. See the module docstring."""
    seen = set() if seen is None else seen
    seen.add(func.name)
    names: Set[str] = set()

    def collection(node: ast.AST) -> Set[str]:
        if isinstance(node, ast.Name):
            return tables.get(node.id, set())
        return _strings(node)

    for node in ast.walk(func):
        if isinstance(node, ast.Compare) and _is_action(node.left):
            for op, right in zip(node.ops, node.comparators):
                if isinstance(op, (ast.Eq, ast.NotEq)):
                    s = _const_str(right)
                    if s is not None:
                        names.add(s)
                elif isinstance(op, (ast.In, ast.NotIn)):
                    names |= collection(right)
        elif isinstance(node, ast.Subscript) and _is_action(node.slice):
            names |= collection(node.value)
        elif isinstance(node, ast.Call):
            callee = node.func
            # TABLE.get(action)
            if (isinstance(callee, ast.Attribute) and callee.attr == "get"
                    and node.args and _is_action(node.args[0])):
                names |= collection(callee.value)
            # helper(..., action, ...) defined in the same module
            elif (isinstance(callee, ast.Name) and callee.id in functions
                    and callee.id not in seen
                    and any(_is_action(a) for a in node.args)):
                names |= _action_names(functions[callee.id], tables, functions, seen)
    return names


def _route_decorations(
        func: ast.FunctionDef) -> Iterator[Tuple[str, str, Union[List[str], str]]]:
    """(kind, path_or_event, methods_or_namespace) for each route decorator."""
    for dec in func.decorator_list:
        if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)):
            continue
        first = _const_str(dec.args[0]) if dec.args else None
        if first is None:
            continue
        kwargs = {k.arg: k.value for k in dec.keywords if k.arg}
        if dec.func.attr == "route":
            methods = sorted(_strings(kwargs["methods"])) if "methods" in kwargs else ["GET"]
            yield "route", first, methods
        elif dec.func.attr == "on" and "namespace" in kwargs:
            namespace = _const_str(kwargs["namespace"])
            if namespace is not None:
                yield "socketio", first, namespace


def _box_python_files(root: Path) -> Iterable[Path]:
    for path in sorted((root / BOX_ROOT).rglob("*.py")):
        parts = set(path.relative_to(root).parts)
        if parts & {"test", "tests", "__pycache__"}:
            continue
        yield path


def _routes_and_events(root: Path):
    routes: Set[str] = set()
    route_actions: Dict[str, Set[str]] = {}
    socketio: Dict[str, Set[str]] = {}
    for path in _box_python_files(root):
        tree = _parse(path)
        if tree is None:
            continue
        tables = _module_tables(tree)
        functions = _module_functions(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            for kind, name, extra in _route_decorations(node):
                if isinstance(extra, str):
                    socketio.setdefault(extra, set()).add(name)
                    continue
                actions = _action_names(node, tables, functions)
                for method in extra:
                    key = f"{method} {name}"
                    routes.add(key)
                    if actions:
                        route_actions.setdefault(key, set()).update(actions)
    return routes, route_actions, socketio


def _net_command(root: Path) -> Dict[str, Set[str]]:
    tree = _parse(root / NET_COMMAND)
    if tree is None:
        return {}
    tables = _module_tables(tree)
    functions = _module_functions(tree)
    roles: Dict[str, Set[str]] = {}
    for stmt in tree.body:
        if not (isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Dict)
                and any(isinstance(t, ast.Name) and t.id == "ROLE_ACTIONS"
                        for t in stmt.targets)):
            continue
        for key, value in zip(stmt.value.keys, stmt.value.values):
            role = _const_str(key) if key is not None else None
            if role is None:
                continue
            handler = functions.get(value.id) if isinstance(value, ast.Name) else None
            roles[role] = _action_names(handler, tables, functions) if handler else set()
    return roles


def _debug_service(root: Path) -> Dict[str, Set[str]]:
    tree = _parse(root / DEBUG_SERVICE)
    if tree is None:
        return {"routes": set(), "features": set()}
    tables = _module_tables(tree)
    routes: Set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name.startswith("do_")):
            continue
        method = node.name[3:].upper()
        for sub in ast.walk(node):
            # self.path == '/x'
            if (isinstance(sub, ast.Compare) and isinstance(sub.left, ast.Attribute)
                    and sub.left.attr == "path"):
                for right in sub.comparators:
                    s = _const_str(right)
                    if s is not None:
                        routes.add(f"{method} {s}")
            # TABLE.get(self.path)
            elif (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr == "get" and sub.args
                    and isinstance(sub.args[0], ast.Attribute)
                    and sub.args[0].attr == "path"
                    and isinstance(sub.func.value, ast.Name)):
                for s in tables.get(sub.func.value.id, set()):
                    routes.add(f"{method} {s}")
    return {"routes": routes, "features": tables.get("SERVICE_FEATURES", set())}


def _capabilities(root: Path) -> Set[str]:
    tree = _parse(root / STATUS_SERVER)
    if tree is None:
        return set()
    keys: Set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if key is not None and _const_str(key) == "capabilities" and isinstance(value, ast.Dict):
                keys |= _strings(value)
    return keys


def extract(root: Path) -> dict:
    """The API surface of the checkout at `root`, as sorted JSON-ready data."""
    routes, route_actions, socketio = _routes_and_events(root)
    debug = _debug_service(root)
    return {
        "routes": sorted(routes),
        "route_actions": {k: sorted(v) for k, v in sorted(route_actions.items())},
        "net_command": {k: sorted(v) for k, v in sorted(_net_command(root).items())},
        "socketio": {k: sorted(v) for k, v in sorted(socketio.items())},
        "debug_service": {"routes": sorted(debug["routes"]),
                          "features": sorted(debug["features"])},
        "capabilities": sorted(_capabilities(root)),
    }


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------

def _code(items: Iterable[str]) -> str:
    return ", ".join(f"`{i}`" for i in items)


def _set_lines(title: str, old: Iterable[str], new: Iterable[str]) -> List[str]:
    old_s, new_s = set(old), set(new)
    added, removed = sorted(new_s - old_s), sorted(old_s - new_s)
    lines: List[str] = []
    if added or removed:
        lines.append(f"### {title}")
        lines.append("")
        if added:
            lines.append(f"- Added: {_code(added)}")
        if removed:
            lines.append(f"- Removed: {_code(removed)}")
        lines.append("")
    return lines


def _keyed_lines(title: str, noun: str, old: Dict[str, List[str]],
                 new: Dict[str, List[str]]) -> List[str]:
    rows: List[str] = []
    for key in sorted(set(old) | set(new)):
        if key not in old:
            rows.append(f"- New {noun} `{key}`: {_code(new[key]) or '(no names found)'}")
        elif key not in new:
            rows.append(f"- Removed {noun} `{key}`")
        else:
            added = sorted(set(new[key]) - set(old[key]))
            removed = sorted(set(old[key]) - set(new[key]))
            parts = []
            if added:
                parts.append(f"added {_code(added)}")
            if removed:
                parts.append(f"removed {_code(removed)}")
            if parts:
                rows.append(f"- `{key}`: " + "; ".join(parts))
    return [f"### {title}", "", *rows, ""] if rows else []


def diff(old: dict, new: dict, old_label: str, new_label: str) -> Optional[str]:
    """A Markdown report of what changed, or None when nothing did."""
    body: List[str] = []
    body += _set_lines("HTTP routes", old.get("routes", []), new.get("routes", []))
    body += _keyed_lines("Actions on command routes", "route",
                         old.get("route_actions", {}), new.get("route_actions", {}))
    body += _keyed_lines("`/net/command` roles and actions", "role",
                         old.get("net_command", {}), new.get("net_command", {}))
    body += _keyed_lines("Socket.IO events", "namespace",
                         old.get("socketio", {}), new.get("socketio", {}))
    old_debug = old.get("debug_service", {})
    new_debug = new.get("debug_service", {})
    body += _set_lines("Debug service (:8765) routes", old_debug.get("routes", []),
                       new_debug.get("routes", []))
    body += _set_lines("Debug service features (`/health`)", old_debug.get("features", []),
                       new_debug.get("features", []))
    body += _set_lines("`/status` capabilities", old.get("capabilities", []),
                       new.get("capabilities", []))
    if not body:
        return None
    head = [
        f"lager {new_label} changed the box HTTP API since {old_label}. Check whether",
        "the crate should pick any of it up; `MISSING_ENDPOINTS.md` records what is in",
        "scope.",
        "",
    ]
    tail = [
        "This list comes from route and action names in the box source "
        "(`tools/box_api_surface.py` in lagerdata/lager). A new parameter on an "
        "existing action, or a changed response shape, does not show up here; the "
        f"{new_label} CHANGELOG covers those.",
    ]
    return "\n".join(head + body + tail) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0].strip())
    sub = parser.add_subparsers(dest="command", required=True)

    p_extract = sub.add_parser("extract", help="write a checkout's API surface as JSON")
    p_extract.add_argument("checkout", type=Path)
    p_extract.add_argument("-o", "--output", type=Path)

    p_diff = sub.add_parser("diff", help="report what changed between two surfaces")
    p_diff.add_argument("old", type=Path)
    p_diff.add_argument("new", type=Path)
    p_diff.add_argument("--old-label", required=True)
    p_diff.add_argument("--new-label", required=True)
    p_diff.add_argument("-o", "--output", type=Path)

    args = parser.parse_args(argv)
    if args.command == "extract":
        if not (args.checkout / NET_COMMAND).is_file():
            parser.error(f"{args.checkout} has no {NET_COMMAND}; is it a lager checkout?")
        text = json.dumps(extract(args.checkout), indent=2) + "\n"
        if args.output:
            args.output.write_text(text, encoding="utf-8")
        else:
            sys.stdout.write(text)
        return 0

    old = json.loads(args.old.read_text(encoding="utf-8"))
    new = json.loads(args.new.read_text(encoding="utf-8"))
    report = diff(old, new, args.old_label, args.new_label)
    if report is None:
        print("unchanged")
        return 0
    if args.output:
        args.output.write_text(report, encoding="utf-8")
    else:
        sys.stdout.write(report)
    print("changed", file=sys.stderr if not args.output else sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
