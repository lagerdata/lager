# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Run the macOS installer on a Mac that is not the one the CLI runs on.

`lager install --ip <a Mac>` copies this file and ``macos_box.py`` to that
Mac and runs this one there over SSH, with whatever Python 3 the Mac has.
It therefore uses the standard library only and must not import the CLI.

The outcome is written as JSON to ``--result``: the SSH session carries a
terminal (``sudo`` may ask for a password), so its output is for the person
watching and is not parsed.

A Linux Box never receives or runs this file.
"""
import argparse
import importlib.util
import json
import os
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
# An SSH session does not get the PATH a terminal on the Mac does.
EXTRA_PATH = ("/opt/homebrew/bin", "/usr/local/bin", "~/.cargo/bin")


class LagerError(Exception):
    """The CLI's error shape, without the CLI."""

    def __init__(self, problem, *, cause=None, fixes=None, **_ignored):
        super().__init__(problem)
        self.problem = problem
        self.cause = cause
        self.fixes = list(fixes) if fixes else []


def load_installer():
    errors = types.ModuleType("_lager_errors")
    errors.LagerError = LagerError
    sys.modules["_lager_errors"] = errors
    spec = importlib.util.spec_from_file_location("lager_macos_box", HERE / "macos_box.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def extend_path(environ=os.environ):
    parts = environ.get("PATH", "").split(os.pathsep)
    for extra in EXTRA_PATH:
        extra = os.path.expanduser(extra)
        if extra not in parts and os.path.isdir(extra):
            parts.append(extra)
    environ["PATH"] = os.pathsep.join(p for p in parts if p)


def run(args, installer=None):
    """Do what ``args`` asks; the JSON-ready outcome."""
    installer = installer or load_installer()
    try:
        if args.command == "install":
            result = installer.install(
                args.version, args.cli_version, bind=args.bind,
                source=Path(args.source) if args.source else None,
                # An update leaves out what the last install on that Mac left
                # out; an install leaves out exactly what it is told to.
                skip=None if (args.reuse_skips and not args.skip) else (args.skip or ()))
            return {"ok": True, "box_version": result.box_version,
                    "image": result.image, "skipped": result.skipped,
                    "ready": result.ready,
                    "python_port": installer.PYTHON_SERVICE_PORT}
        if args.command == "uninstall":
            installer.uninstall(purge_config=args.purge_config,
                                keep_images=args.keep_images)
            return {"ok": True}
        return {"ok": True, "pieces": installer.installed_pieces()}
    except LagerError as exc:
        return {"ok": False, "problem": exc.problem, "cause": exc.cause,
                "fixes": exc.fixes}
    except Exception as exc:  # noqa: BLE001 - reported to the CLI, not lost
        return {"ok": False, "problem": "The installer failed on the Mac.",
                "cause": f"{type(exc).__name__}: {exc}", "fixes": []}


def parse(argv):
    parser = argparse.ArgumentParser(prog="macos_remote")
    parser.add_argument("--result", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    install = commands.add_parser("install")
    install.add_argument("--version", required=True)
    install.add_argument("--cli-version", required=True)
    install.add_argument("--bind", default="0.0.0.0")
    install.add_argument("--source", default=None)
    install.add_argument("--skip", action="append", choices=("jlink", "picoscope"))
    install.add_argument("--reuse-skips", action="store_true")
    uninstall = commands.add_parser("uninstall")
    uninstall.add_argument("--purge-config", action="store_true")
    uninstall.add_argument("--keep-images", action="store_true")
    commands.add_parser("pieces")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse(sys.argv[1:] if argv is None else argv)
    extend_path()
    outcome = run(args)
    with open(args.result, "w", encoding="utf-8") as fh:
        json.dump(outcome, fh)
    return 0 if outcome.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
