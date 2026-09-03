"""The Rigol MSO5000 driver's logic-analyzer surface.

`lager logic <net> enable` reached the box and died on `Function not found:
is_la_enabled`: `box/lager/nets/net.py` called seven LA methods on the net's
device and the driver defined none of them. `Device.__getattr__` turns any
unknown name into an HTTP call, so nothing failed until the box looked the
method up -- there is no local point at which a missing driver method is visible.

So this file does two things. It pins the SCPI each method emits, and it walks
the call sites to assert every name they use actually exists on the driver, which
is the check whose absence let the gap ship.
"""

import ast
import pathlib
import re
from unittest import mock

import pytest

from lager.instrument_wrappers.rigol_mso5000_defines import LogicDisplaySize
from lager.measurement.scope import rigol_mso5000
from lager.measurement.scope.rigol_mso5000 import RigolMso5000

BOX = pathlib.Path(__file__).resolve().parents[3] / "box" / "lager"
NET_PY = BOX / "nets" / "net.py"
MAPPER_PY = BOX / "nets" / "mappers" / "rigol_mso5000.py"


class FakeInstrument:
    """Records writes; answers queries from a canned table."""

    def __init__(self, answers=None):
        self.writes = []
        self.queries = []
        self.answers = answers or {}
        self.timeout = 0

    def write(self, cmd):
        self.writes.append(cmd)

    def query(self, cmd):
        self.queries.append(cmd)
        return self.answers.get(cmd, "0")


@pytest.fixture
def scope():
    """A driver on channel 1 with its transport faked out."""
    fake = FakeInstrument()
    with mock.patch.object(rigol_mso5000, "get_instrument", return_value=fake):
        dev = RigolMso5000(address="USB0::FAKE::INSTR", channel=1)
        dev._fake = fake
        yield dev


# --------------------------------------------------------------------------
# The regression guard: every name the call sites use must exist on the driver
# --------------------------------------------------------------------------

def _logic_branch_device_calls():
    """`self.device.<name>` used inside net.py's `NetType.Logic` branches."""
    src = NET_PY.read_text()
    names = set()
    lines = src.splitlines()
    for i, line in enumerate(lines):
        if "NetType.Logic:" not in line or "self.type" not in line:
            continue
        indent = len(line) - len(line.lstrip())
        for follow in lines[i + 1:]:
            if follow.strip() and (len(follow) - len(follow.lstrip())) <= indent:
                break
            names.update(re.findall(r"self\.device\.([a-zA-Z_][a-zA-Z0-9_]*)\(", follow))
    return names


def _mapper_self_calls():
    """`self.<name>` the logic mapper calls but does not define itself."""
    tree = ast.parse(MAPPER_PY.read_text())
    cls = next(n for n in ast.walk(tree)
               if isinstance(n, ast.ClassDef) and n.name == "RigolMSO5000LogicMapper")
    defined = {n.name for n in cls.body if isinstance(n, ast.FunctionDef)}
    called = set()
    for node in ast.walk(cls):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "self"):
            called.add(node.func.attr)
    return {n for n in called - defined if "la" in n or "channel_size" in n}


def test_the_walkers_found_something():
    """Guard the guard: a parser finding nothing would pass forever."""
    assert len(_logic_branch_device_calls()) >= 7, _logic_branch_device_calls()
    assert _mapper_self_calls(), "no LA calls parsed out of the mapper"


@pytest.mark.parametrize("name", sorted(_logic_branch_device_calls()))
def test_net_logic_branch_methods_exist_on_the_driver(name):
    assert hasattr(RigolMso5000, name), (
        f"net.py's Logic branch calls self.device.{name}(), which the driver does "
        f"not define. It will 404 as 'Function not found: {name}'."
    )


@pytest.mark.parametrize("name", sorted(_mapper_self_calls()))
def test_mapper_la_methods_exist_on_the_driver(name):
    assert hasattr(RigolMso5000, name), (
        f"the logic mapper calls self.{name}(), which falls through __getattr__ "
        f"to the driver, which does not define it."
    )


# --------------------------------------------------------------------------
# The same guard, widened to the whole mapper
# --------------------------------------------------------------------------
#
# The walkers above cover the logic surface they were written for. That is too
# narrow: `get_trigger_spi_width` is called by the ANALOG mapper and so was
# invisible to them, and it failed on real hardware as
# "Function not found: get_trigger_spi_width".
#
# Widening the guard to every mapper class finds 115 such names, almost all of
# them across the trigger-settings and bus-decode surfaces. They cannot be
# fixed here, and `unit (box)` is a required context, so they are recorded in a
# baseline file the check compares against exactly.

BASELINE = pathlib.Path(__file__).with_name("mapper_undefined_baseline.txt")


def _baselined_undefined():
    return {line.strip() for line in BASELINE.read_text().splitlines()
            if line.strip() and not line.startswith("#")}


def _mapper_undefined_calls():
    """`Class.name` for every self-call in the mapper the driver lacks."""
    tree = ast.parse(MAPPER_PY.read_text())
    found = set()
    for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
        defined = {n.name for n in cls.body if isinstance(n, ast.FunctionDef)}
        called = set()
        for node in ast.walk(cls):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "self"):
                called.add(node.func.attr)
        found |= {f"{cls.name}.{n}" for n in called - defined
                  if not hasattr(RigolMso5000, n)}
    return found


def _mapper_classes_walked():
    """How many classes the wide walk actually parsed out of the mapper."""
    tree = ast.parse(MAPPER_PY.read_text())
    return [n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]


def test_the_wide_walker_found_something():
    """Guard the guard, again: a parser matching nothing would pass forever.

    This asserts on the number of CLASSES the walk parsed, not on the number of
    undefined names it found. The two come apart. The undefined count is the
    quantity #418 exists to shrink -- it starts at 115 and ratchets toward 0 --
    so a floor under it fails once enough of the driver is implemented, and
    fails saying "the mapper walk parsed almost nothing", which reads as a
    broken parser rather than as progress. The class count is what would
    actually be zero if the parser broke, which is the property meant here.
    """
    walked = _mapper_classes_walked()
    assert len(walked) > 15, (
        f"the mapper walk parsed almost nothing: {walked}"
    )


def test_no_new_undefined_mapper_methods():
    """Two-sided. New undefined names fail; fixed ones must leave the baseline."""
    actual = _mapper_undefined_calls()
    baseline = _baselined_undefined()

    new = sorted(actual - baseline)
    assert not new, (
        f"{len(new)} mapper method(s) call a name no driver defines and are not "
        f"in {BASELINE.name}: {new}. Each will fail at runtime as "
        f"'Function not found'. Implement it on the driver rather than adding it "
        f"to the baseline."
    )

    fixed = sorted(baseline - actual)
    assert not fixed, (
        f"{len(fixed)} baselined name(s) are now defined on the driver: {fixed}. "
        f"Remove them from {BASELINE.name} -- the baseline only ratchets down."
    )


# --------------------------------------------------------------------------
# SCPI
# --------------------------------------------------------------------------

def test_enable_and_disable_la(scope):
    scope.enable_la()
    scope.disable_la()
    assert scope._fake.writes == [":LA:STATe ON", ":LA:STATe OFF"]


@pytest.mark.parametrize("answer,expected", [
    ("1", True), ("0", False), ("ON", True), ("OFF", False), ("", False),
])
def test_is_la_enabled_parses(scope, answer, expected):
    scope._fake.answers[":LA:STATe?"] = answer
    assert scope.is_la_enabled() is expected


def test_channel_display_writes_the_indexed_command(scope):
    scope.enable_la_channel(3)
    scope.disable_la_channel(9)
    assert scope._fake.writes == [
        ":LA:DIGital3:DISPlay ON",
        ":LA:DIGital9:DISPlay OFF",
    ]


def test_d0_is_not_swallowed_by_a_falsy_check(scope):
    """D0 is a real channel and is falsy.

    The analog methods above use `channel or self.channel`, which would send a
    request for D0 to the net's own channel instead. The LA methods must not.
    """
    scope.enable_la_channel(0)
    assert scope._fake.writes == [":LA:DIGital0:DISPlay ON"]


def test_channel_defaults_to_the_nets_own(scope):
    scope.enable_la_channel()
    assert scope._fake.writes == [":LA:DIGital1:DISPlay ON"]


@pytest.mark.parametrize("bad", [16, -1, 99])
def test_out_of_range_channels_are_refused(scope, bad):
    with pytest.raises(ValueError, match="D0-D15"):
        scope.enable_la_channel(bad)


def test_is_la_channel_enabled_reads_the_right_channel(scope):
    scope._fake.answers[":LA:DISPlay? D7"] = "1"
    assert scope.is_la_channel_enabled(7) is True
    assert scope.is_la_channel_enabled(8) is False


def test_channel_state_avoids_the_write_only_subtree(scope):
    """`:LA:DIGital<n>:DISPlay?` is accepted and never answered on real
    hardware, so reading channel state must not go through it. Pinned because
    the unanswerable form is the symmetrical-looking one next to the writes."""
    scope.is_la_channel_enabled(3)
    assert scope._fake.queries == [":LA:DISPlay? D3"]
    assert not any("DIGital" in q for q in scope._fake.queries)


def test_active_channel(scope):
    assert scope.set_la_active_channel(5) == {"active": "D5"}
    assert scope._fake.writes == [":LA:ACTive D5"]


def test_threshold_is_per_pod(scope):
    assert scope.set_la_threshold(1, 1.65) == {"pod": 1, "threshold": 1.65}
    assert scope._fake.writes == [":LA:POD1:THReshold 1.65"]


@pytest.mark.parametrize("bad", [0, 3, "pod1"])
def test_threshold_refuses_a_bad_pod(scope, bad):
    with pytest.raises(ValueError, match="pod must be 1 or 2"):
        scope.set_la_threshold(bad, 1.65)


def test_display_position(scope):
    scope.set_la_display_position(2, 4)
    assert scope._fake.writes == [":LA:DIGital2:POSition 4"]


@pytest.mark.parametrize("given,expected", [
    (LogicDisplaySize.Small, ":LA:SIZE SMALl"),
    (LogicDisplaySize.Medium, ":LA:SIZE MEDium"),
    (LogicDisplaySize.Large, ":LA:SIZE LARGe"),
    ("SMAL", ":LA:SIZE SMALl"),
    ("medium", ":LA:SIZE MEDium"),
    ({"__enum__": {"type": "LogicDisplaySize", "value": "LARG"}}, ":LA:SIZE LARGe"),
])
def test_display_size_accepts_every_form_it_is_sent(scope, given, expected):
    scope.set_enabled_channel_size(given)
    assert scope._fake.writes == [expected]


def test_display_size_refuses_junk(scope):
    with pytest.raises(ValueError, match="SMALl, MEDium or LARGe"):
        scope.set_enabled_channel_size("HUGE")


def test_display_size_enum_is_not_crossed():
    """Medium used to render as LARGe and Large as MEDium.

    `size_medium()` on the mapper therefore made the display large. The enum's
    second element is the abbreviated form the instrument echoes back, so a
    crossed pair also breaks the read path.
    """
    assert LogicDisplaySize.Small.to_cmd() == "SMALl"
    assert LogicDisplaySize.Medium.to_cmd() == "MEDium"
    assert LogicDisplaySize.Large.to_cmd() == "LARGe"
    for member in LogicDisplaySize:
        assert LogicDisplaySize.from_cmd(member.value[1]) is member
