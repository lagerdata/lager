# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Static checks on box/lager/automation/__init__.py's lazy export table.

That table is a hand-maintained chain of `if name == "...": import; return`
branches, added to by copy-paste every time a driver lands. A pasted branch
whose guard keeps the name it was copied FROM is invisible: the module still
imports, the wrong class answers to the old name, and the new one is simply
absent. That is exactly what happened to PlugableUSBNet -- it was returned
under a duplicated "YKUSHUSBNet" guard, so `automation.YKUSHUSBNet` handed back
the Plugable driver and `automation.PlugableUSBNet` raised AttributeError.

Parsed rather than imported: importing lager.automation drags in the whole box
dependency set, and the defect is a property of the source, not of runtime.
"""

import ast
import os
import unittest

_INIT = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "..", "..",
    "box", "lager", "automation", "__init__.py"))


def _tree():
    with open(_INIT, encoding="utf-8") as f:
        return ast.parse(f.read())


def _getattr_fn(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "__getattr__":
            return node
    raise AssertionError("no __getattr__ in automation/__init__.py")


def _guard_names(test):
    """The string literals a branch condition compares `name` against."""
    out = []
    for cmp_node in ast.walk(test):
        if not isinstance(cmp_node, ast.Compare):
            continue
        for op, comparator in zip(cmp_node.ops, cmp_node.comparators):
            if isinstance(op, ast.Eq) and isinstance(comparator, ast.Constant):
                out.append(comparator.value)
            elif isinstance(op, ast.In) and isinstance(comparator, (ast.Tuple, ast.List, ast.Set)):
                out.extend(e.value for e in comparator.elts
                           if isinstance(e, ast.Constant))
    return [n for n in out if isinstance(n, str)]


def _branches():
    """[(guard_names, returned_identifiers)] for each branch of __getattr__."""
    out = []
    for node in ast.walk(_getattr_fn(_tree())):
        if not isinstance(node, ast.If):
            continue
        returned = []
        for sub in ast.walk(node):
            if isinstance(sub, ast.Return) and isinstance(sub.value, ast.Name):
                returned.append(sub.value.id)
        out.append((_guard_names(node.test), returned))
    return out


def _dunder_all():
    for node in ast.walk(_tree()):
        if (isinstance(node, ast.Assign)
                and any(getattr(t, "id", None) == "__all__" for t in node.targets)):
            return [e.value for e in node.value.elts if isinstance(e, ast.Constant)]
    raise AssertionError("no __all__ in automation/__init__.py")


class TestLazyExportTable(unittest.TestCase):
    def test_no_name_is_guarded_twice(self):
        seen, dupes = set(), []
        for guards, _ in _branches():
            for g in guards:
                if g in seen:
                    dupes.append(g)
                seen.add(g)
        self.assertEqual(dupes, [], f"unreachable duplicate branch(es): {dupes}")

    def test_every_returned_class_answers_to_its_own_name(self):
        # Aliases are fine (AcronameUSB -> AcronameUSBNet); a class that is
        # returned but has no branch of its OWN is not -- it is unreachable.
        guarded = {g for guards, _ in _branches() for g in guards}
        for _, returned in _branches():
            for cls in returned:
                if not cls.endswith(("Net", "Error")):
                    continue
                self.assertIn(cls, guarded,
                              f"{cls} is returned but nothing exports it by name")

    def test_all_exported_names_are_reachable(self):
        guarded = {g for guards, _ in _branches() for g in guards}
        module_level = set()
        for node in _tree().body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module_level.update(a.asname or a.name.split(".")[0]
                                    for a in node.names)
            elif isinstance(node, ast.Assign):
                module_level.update(getattr(t, "id", None) for t in node.targets)
        for name in _dunder_all():
            self.assertTrue(
                name in guarded or name in module_level,
                f"__all__ advertises {name!r} but nothing resolves it")

    def test_plugable_driver_is_exported(self):
        # The specific regression.
        guarded = {g for guards, _ in _branches() for g in guards}
        self.assertIn("PlugableUSBNet", guarded)
        self.assertIn("PlugableUSBNet", _dunder_all())


if __name__ == "__main__":
    unittest.main()
