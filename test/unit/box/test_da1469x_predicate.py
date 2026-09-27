# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""The box asks "is this a DA1469x?" in one place, plus one pinned copy.

``lager.debug.probes.is_da1469x`` answers it. Several sites act on the answer:
the OpenOCD flash dispatch, the J-Link flash path's post-flash reset, the GDB
reset variant. Each used to spell the test out inline as
``'DA1469' in device.upper()``. Inline copies agree until one of them is
edited, which is how the debug service and the Net API came to flash a
DA1469x differently.

``debug/jlink.py`` keeps its own copy, ``_is_da1469x``, because three tests
load that module standalone and it cannot import ``.probes`` (see
test_debug_script_root.py). This file pins the copy to the original, and fails
if an inline spelling of the test appears anywhere else under ``box/lager``
that is not listed, with its reason, in ``ALLOWED``.
"""

import ast
import importlib.util
import pathlib
import sys
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
BOX_LAGER = REPO_ROOT / 'box' / 'lager'

#: (path under box/lager, enclosing function) -> why it may spell the test out.
ALLOWED = {
    ('debug/probes.py', 'is_da1469x'): 'the predicate itself',
    ('debug/jlink.py', '_is_da1469x'): (
        'the standalone copy: jlink.py is loaded by path and cannot import probes'
    ),
    ('debug/gdb.py', 'get_arch'): (
        'a part-number-prefix to CPU-architecture table that groups DA1469x with '
        'DA1458x and DA1468x as Cortex-M4; it is not the DA1469x special case'
    ),
}

#: (path under box/lager, function) -> the predicate that function must call.
CALLERS = {
    ('debug/api.py', 'flash_device'): 'is_da1469x',
    ('debug/gdb.py', '_jlink_monitor_reset'): 'is_da1469x',
    ('debug/gdb.py', 'reset'): 'is_da1469x',
    ('debug/jlink.py', '_is_da1469'): '_is_da1469x',
}

DEVICES = [
    None, '', 'DA14695', 'da14699@1', 'DA14691_A',
    'nRF52840_xxAA', 'STM32F4x', 'DA14585',
]


def _names_the_family(node):
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and 'DA1469' in node.value.upper()
    )


class _InlineTests(ast.NodeVisitor):
    """(line, enclosing function) for each inline test for the family.

    An inline test is a membership check against a string that names the
    family (``'DA1469' in x``, ``'DA1469' not in x``), or a string method
    called with one (``x.startswith('DA1469')``). Docstrings and log messages
    name the family constantly; only a comparison or a call is a test, which is
    why this walks the AST rather than grepping.
    """

    STRING_METHODS = frozenset({
        'startswith', 'endswith', 'find', 'rfind', 'index', 'count', '__contains__',
    })

    def __init__(self):
        self.functions = []
        self.found = []

    def _function(self, node):
        self.functions.append(node.name)
        self.generic_visit(node)
        self.functions.pop()

    visit_FunctionDef = _function
    visit_AsyncFunctionDef = _function

    def _record(self, node):
        self.found.append(
            (node.lineno, self.functions[-1] if self.functions else '<module>'))

    def visit_Compare(self, node):
        membership = any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops)
        operands = [node.left, *node.comparators]
        if membership and any(_names_the_family(o) for o in operands):
            self._record(node)
        self.generic_visit(node)

    def visit_Call(self, node):
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr in self.STRING_METHODS
            and any(_names_the_family(a) for a in node.args)
        ):
            self._record(node)
        self.generic_visit(node)


def _inline_tests(source):
    visitor = _InlineTests()
    visitor.visit(ast.parse(source))
    return visitor.found


def _calls_inside(path, function):
    """Every name called inside the function named *function* in *path*."""
    tree = ast.parse(path.read_text(encoding='utf-8'))
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function:
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call):
                    if isinstance(inner.func, ast.Attribute):
                        called.add(inner.func.attr)
                    elif isinstance(inner.func, ast.Name):
                        called.add(inner.func.id)
    return called


def _load_jlink_standalone():
    """Load debug/jlink.py by path, with no parent package, as its tests do."""
    try:
        import pexpect  # noqa: F401
        from pexpect import replwrap  # noqa: F401
    except ImportError:  # the box image has pexpect; a bare unit env may not
        from unittest.mock import MagicMock
        for name in ('pexpect', 'pexpect.replwrap'):
            sys.modules.setdefault(name, MagicMock())
    spec = importlib.util.spec_from_file_location(
        'jlink_da1469x_copy', BOX_LAGER / 'debug' / 'jlink.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class OnePredicateInTheTree(unittest.TestCase):
    def test_no_inline_family_test_outside_the_predicate_and_its_copy(self):
        offenders = []
        for path in sorted(BOX_LAGER.rglob('*.py')):
            rel = path.relative_to(BOX_LAGER).as_posix()
            for lineno, function in _inline_tests(path.read_text(encoding='utf-8')):
                if (rel, function) not in ALLOWED:
                    offenders.append(f'box/lager/{rel}:{lineno} in {function}()')
        self.assertEqual(
            offenders, [],
            '\n\nThese test for the DA1469x family inline. Call '
            'lager.debug.probes.is_da1469x instead, or jlink._is_da1469x inside '
            'debug/jlink.py, which cannot import probes.',
        )

    def test_the_scan_still_finds_the_allowed_sites(self):
        # A scanner that silently matches nothing would pass forever.
        found = set()
        for rel, _ in ALLOWED:
            source = (BOX_LAGER / rel).read_text(encoding='utf-8')
            found |= {(rel, function) for _, function in _inline_tests(source)}
        self.assertEqual(found, set(ALLOWED))

    def test_the_scan_sees_each_inline_shape_and_ignores_prose(self):
        source = (
            'def f(device):\n'
            '    """Treat a DA1469x specially: \'DA1469\' in device.upper()."""\n'
            '    logger.info("DA1469x: resetting target")\n'
            "    a = 'DA1469' in device.upper()\n"
            "    b = 'DA1469' not in (device or '')\n"
            "    c = device.upper().startswith('DA1469')\n"
            '    return a or b or c\n'
        )
        self.assertEqual(_inline_tests(source), [(4, 'f'), (5, 'f'), (6, 'f')])


class TheJLinkCopyAgrees(unittest.TestCase):
    def test_jlink_copy_matches_probes(self):
        from lager.debug.probes import is_da1469x

        jlink = _load_jlink_standalone()
        for device in DEVICES:
            with self.subTest(device=device):
                self.assertEqual(jlink._is_da1469x(device), is_da1469x(device))


class DecisionSitesAskAPredicate(unittest.TestCase):
    def test_each_decision_site_calls_a_predicate(self):
        for (rel, function), predicate in sorted(CALLERS.items()):
            with self.subTest(site=f'{rel}:{function}'):
                self.assertIn(predicate, _calls_inside(BOX_LAGER / rel, function))


if __name__ == '__main__':
    unittest.main()
