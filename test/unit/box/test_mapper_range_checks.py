# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
A range check written as `4 > bits > 32` rejects nothing.

Python chains that into `4 > bits and bits > 32`, and no number is both below 4
and above 32, so the branch is dead and the `raise` under it is unreachable.
Seven validations across two mapper modules were written that way, with the
bounds inverted, and every one accepted any value at all while its message
promised a range.

That is worse than no check: the message states a guarantee the code does not
provide, so a caller who reads it has been told the value was validated. What
the instrument then does with an out-of-range width is not established -- the
write may be clamped, rejected silently, or accepted into a state the caller
did not ask for.

Two kinds of test here, failing for different reasons:

  the tree-wide scan   catches the SHAPE anywhere in the shipping code,
                       including in a validation nobody has written yet
  the per-site tests   catch the BEHAVIOUR -- that each of the seven rejects
                       now. Every one fails on the code as it was, at both
                       ends, and each also pins an in-range value so a
                       corrected bound cannot reject what is valid

`rigol_mso5000.py`'s cursor-position checks were corrected to
`not (0 <= x <= 479)` in an earlier pass; this brings the rest of the file to
the same form.
"""

import ast
import pathlib

import pytest

from lager.nets.mappers import rigol_mso5000
from lager.nets.mappers.keithley import KeithleyBatteryFunctionMapper

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]

#: Shipping code only. A dead branch in a test is a broken test, which its own
#: suite catches; this scan is about a guarantee made to a caller.
SCAN_ROOTS = ("box", "cli")

#: Below this the walk is not seeing the tree and every result is vacuous.
#: box/ + cli/ carried 65 chained comparisons when this was written.
MIN_CHAINED_COMPARISONS = 50


def _chained_comparisons():
    """Every `a OP b OP c` in the shipping code, as (relpath, lineno, node)."""
    found = []
    for root in SCAN_ROOTS:
        for path in sorted((REPO_ROOT / root).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            try:
                tree = ast.parse(path.read_text())
            except SyntaxError:  # not this test's business
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Compare) and len(node.ops) >= 2:
                    found.append((path.relative_to(REPO_ROOT), node.lineno, node))
    return found


def _is_unsatisfiable(node):
    """True for a two-sided numeric chain whose bounds cannot both hold.

    Only the literal-bounded form is decidable without running anything:
    `LO > x > HI` where LO < HI, and `LO < x < HI` where LO > HI. A chain with
    a name for either bound may be fine at runtime and is left alone.
    """
    if len(node.ops) != 2:
        return False
    lo, hi = node.left, node.comparators[1]
    if not all(isinstance(v, ast.Constant) and isinstance(v.value, (int, float))
               for v in (lo, hi)):
        return False
    first, second = (type(op).__name__ for op in node.ops)
    if first in ("Gt", "GtE") and second in ("Gt", "GtE"):
        return lo.value < hi.value
    if first in ("Lt", "LtE") and second in ("Lt", "LtE"):
        return lo.value > hi.value
    return False


def test_the_scan_sees_the_tree():
    """Guard the guard: a walk matching nothing would pass forever."""
    found = _chained_comparisons()
    assert len(found) >= MIN_CHAINED_COMPARISONS, (
        f"the walk found only {len(found)} chained comparisons under "
        f"{SCAN_ROOTS}. It is not reading the tree, so the check below is "
        f"asserting nothing."
    )


def test_no_range_check_is_unsatisfiable():
    offenders = [f"{path}:{lineno}  {ast.unparse(node)}"
                 for path, lineno, node in _chained_comparisons()
                 if _is_unsatisfiable(node)]
    assert not offenders, (
        "these comparisons can never be true, so the branch under each is "
        "dead:\n  " + "\n  ".join(offenders) +
        "\nThe bounds are inverted. Write it as `not (LO <= x <= HI)`, the "
        "form the cursor-position checks in rigol_mso5000.py already use."
    )


class FakeDevice:
    """Records forwarded calls and answers every query with 8.

    Each mapper defines `__getattr__` to forward an unknown name to the device
    proxy, which is what makes these validations reachable in a unit test: the
    check runs before the forward, so nothing has to exist on the driver.
    """

    def __init__(self):
        self.calls = []
        self.forwarded = []

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append(name)
            self.forwarded.append((name, args, kwargs))
            return 8
        return record


def _mapper(cls):
    """Build a mapper without running `__init__`.

    The Bus subclasses take keyword channel objects and drive the instrument
    from `setup()` inside `__init__`, none of which the method under test
    needs. Assigning `device` first matters: `__getattr__` forwards to it, so
    an instance without one recurses on the first attribute miss.
    """
    obj = cls.__new__(cls)
    obj.device = FakeDevice()
    obj.net = None
    return obj


#: One row per site fixed: (label, call, too_low, in_range, too_high).
SITES = [
    ("TriggerSettingsUART.set_uart_params bits",
     lambda m, v: m.set_uart_params(bits=v),
     rigol_mso5000.TriggerSettingsUART_RigolMSO5000FunctionMapper, 4, 8, 9),

    ("TriggerSettingsI2C.set_trigger_on_address bits",
     lambda m, v: m.set_trigger_on_address(bits=v),
     rigol_mso5000.TriggerSettingsI2C_RigolMSO5000FunctionMapper, 6, 7, 11),

    ("TriggerSettingsI2C.set_trigger_on_data width",
     lambda m, v: m.set_trigger_on_data(width=v),
     rigol_mso5000.TriggerSettingsI2C_RigolMSO5000FunctionMapper, 0, 5, 6),

    ("TriggerSettingsSPI.set_trigger_data bits",
     lambda m, v: m.set_trigger_data(bits=v),
     rigol_mso5000.TriggerSettingsSPI_RigolMSO5000FunctionMapper, 2, 8, 64),

    ("BusUART.set_data_bits",
     lambda m, v: m.set_data_bits(v),
     rigol_mso5000.BusUART_RigolMSO5000FunctionMapper, 4, 8, 10),

    ("BusSPI.set_data_width",
     lambda m, v: m.set_data_width(v),
     rigol_mso5000.BusSPI_RigolMSO5000FunctionMapper, 2, 8, 64),

    ("KeithleyBattery.setup_battery soc",
     lambda m, v: m.setup_battery(soc=v),
     KeithleyBatteryFunctionMapper, -1, 50, 101),
]


@pytest.mark.parametrize("call, cls, low, ok, high",
                         [s[1:] for s in SITES], ids=[s[0] for s in SITES])
def test_the_range_is_enforced_at_both_ends(call, cls, low, ok, high):
    """Both ends reject, and the value between them still passes.

    All three assertions fail on the pre-fix code: the dead branch let `low`
    and `high` straight through, and `ok` only passes once the corrected bound
    is the right way round.
    """
    with pytest.raises(ValueError):
        call(_mapper(cls), low)
    with pytest.raises(ValueError):
        call(_mapper(cls), high)
    call(_mapper(cls), ok)


def test_a_state_of_charge_of_zero_reaches_the_instrument():
    """0 is a value, not an absence.

    `setup_battery` guarded `soc` with `!= None` and then again with a bare
    truthiness test. The second test is falsy for 0, so `setup_battery(soc=0)`
    fell through both the range check above and `set_soc` below it -- no
    exception, no log line, no return value, and the simulation kept whatever
    state of charge it already had.

    0 is the interesting end of the range for a discharge test, and it is
    inside the range the neighbouring `ValueError` advertises, so the message
    said 0 was acceptable while the code discarded it. Every other parameter
    on this method is guarded by `!= None` alone.

    The parametrized site above cannot catch this: it tests -1, 50 and 101,
    and a value that is silently dropped raises nothing at either end.
    """
    mapper = _mapper(KeithleyBatteryFunctionMapper)

    mapper.setup_battery(soc=0)

    forwarded = [(name, args) for name, args, _ in mapper.device.forwarded]
    assert ("set_soc", (0,)) in forwarded, (
        f"setup_battery(soc=0) did not call set_soc(0); forwarded {forwarded}"
    )

