"""The firewall allowlist must admit exactly the ports start_box.sh publishes.

`secure_box_firewall.sh` is default-deny: a port absent from `LAGER_PORTS` gets no
allow rule on `lo`, `docker0`, `tailscale0` or the corporate VPN interface. The two
lists have drifted three times, each time because the only thing holding them
together was a comment asking the next reader to keep them in sync.

This pins them to each other instead. It reads the shell rather than a duplicated
Python constant, so neither side can move without the other.

Note the scope. This pins the contents of the allowlist. Whether a given rule then
governs a container-published port is a separate question, tracked separately.
"""

import pathlib
import re

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
START_BOX = REPO_ROOT / "box" / "start_box.sh"
FIREWALL = REPO_ROOT / "cli" / "deployment" / "security" / "secure_box_firewall.sh"

# `-p <host>:<container>`, where either side may be an `a-b` range.
_PUBLISH_RE = re.compile(r"-p\s+(\d+(?:-\d+)?):(\d+(?:-\d+)?)")
_ALLOWLIST_RE = re.compile(r"^LAGER_PORTS=\(([^)]*)\)", re.MULTILINE)


def _extract(topic):
    """Return the shell between the BEGIN/END sentinels naming `topic`."""
    begin, end = f"# --- BEGIN {topic}", f"# --- END {topic}"
    body, inside, seen = [], False, False
    for line in START_BOX.read_text().splitlines():
        if line.startswith(begin):
            inside, seen = True, True
            continue
        if line.startswith(end):
            inside = False
            continue
        if inside:
            body.append(line)
    assert seen, f"sentinel {begin!r} not found in {START_BOX}"
    assert body, f"no shell extracted for {topic!r}"
    return "\n".join(body)


def _ufw_form(host_spec):
    """Render a published host port the way ufw spells it: a-b becomes a:b."""
    return host_spec.replace("-", ":")


def published_host_ports():
    """Every host port start_box.sh can publish, conditional arms included.

    9000 is appended separately when the UART service is enabled, so a naive read
    of the array literal alone would miss it. The firewall has to admit it either
    way -- the allowlist is written once at provisioning time and cannot know which
    arm a later `start_box.sh` run will take.
    """
    block = _extract("port publishing")
    return {_ufw_form(host) for host, _container in _PUBLISH_RE.findall(block)}


def allowlisted_ports():
    text = FIREWALL.read_text()
    match = _ALLOWLIST_RE.search(text)
    assert match, f"LAGER_PORTS array not found in {FIREWALL}"
    return set(match.group(1).split())


def test_the_allowlist_matches_what_start_box_publishes():
    published = published_host_ports()
    allowed = allowlisted_ports()

    missing = sorted(published - allowed)
    extra = sorted(allowed - published)

    assert not missing, (
        f"start_box.sh publishes {missing} but LAGER_PORTS does not admit them. "
        "A port absent from the allowlist gets no allow rule on any interface."
    )
    assert not extra, (
        f"LAGER_PORTS admits {extra}, which start_box.sh never publishes. "
        "Drop them, or publish them."
    )


def test_the_conditional_9000_arm_is_covered():
    """Guards the specific shape the array-literal-only parse would miss."""
    assert "9000" in published_host_ports(), (
        "9000 is appended to PORT_PUBLISH_ARGS outside the array literal; "
        "if this fails the extractor stopped seeing the conditional arm."
    )


def test_the_debug_ranges_are_admitted():
    """The four ranges that went missing three times, named explicitly."""
    allowed = allowlisted_ports()
    for port_range in ("2331:2342", "4444:4447", "6666:6669", "9090:9097"):
        assert port_range in allowed, f"{port_range} is not in LAGER_PORTS"


@pytest.mark.parametrize("stale", ["5001"])
def test_dead_ports_stay_out_of_the_allowlist(stale):
    """5001 sat in the deployed allowlist with nothing serving it."""
    assert stale not in allowlisted_ports()


def test_the_help_text_is_derived_not_restated():
    """The script must print LAGER_PORTS, not a hand-maintained copy of it.

    Both the header comment and the --help text carried their own port lists, and
    both were stale against the array in the same file.
    """
    text = FIREWALL.read_text()
    assert "${LAGER_PORTS[*]}" in text, (
        "--help should render the allowlist from the array so the two cannot drift"
    )


# The tests above pin what is in the allowlist. The ones below pin the shape of
# the ufw command each entry is spent on, which is a separate way to be wrong.
#
# Every entry is passed to ufw verbatim, and a range is spelled `a:b`. ufw's
# extended syntax refuses one unless the rule also names a protocol:
#
#   $ ufw allow in on lo to any port 2331:2342
#   ERROR: Must specify 'tcp' or 'udp' with multiple ports
#
# The allowlist held nothing but single ports when these rules were written, so
# the missing protocol was invisible. Adding the debug ranges made every box
# install fail on the first rule -- the script runs under `set -e`, so there is
# no partial success to notice.

_ALLOW_RULE_RE = re.compile(r"^\s*(ufw allow in on .*)$", re.MULTILINE)
_DENY_RULE_RE = re.compile(r"^\s*(ufw deny .*)$", re.MULTILINE)

_UFW_MULTIPORT_ERROR = "Must specify 'tcp' or 'udp' with multiple ports"

# The interface each allow loop covers, as it is spelled in the script. All four
# are listed because only the first one to run reports the error: a fix derived
# from a failing log alone repairs `lo` and leaves the other three broken for
# whichever box is the first to have that interface.
ALLOW_INTERFACES = ["lo", "docker0", '"$TAILSCALE_IFACE"', '"$CORPORATE_VPN_IFACE"']


TRAP_HANDLER = "restore_minimal_policy"


def _rules(pattern):
    found = pattern.findall(FIREWALL.read_text())
    assert found, f"no rules matching {pattern.pattern!r} in {FIREWALL}"
    return found


def _trap_handler_body():
    """The shell between `<handler>() {` and the closing brace in column zero."""
    text = FIREWALL.read_text()
    opener = f"{TRAP_HANDLER}() {{"
    assert opener in text, f"{TRAP_HANDLER}() not found in {FIREWALL}"
    return text.split(opener, 1)[1].split("\n}", 1)[0]


@pytest.mark.parametrize("iface", ALLOW_INTERFACES)
def test_each_interface_allow_rule_names_a_protocol(iface):
    rules = [
        r for r in _rules(_ALLOW_RULE_RE) if r.startswith(f"ufw allow in on {iface} ")
    ]
    assert len(rules) == 1, (
        f"expected exactly one `ufw allow in on {iface}` rule, found {len(rules)}"
    )
    assert "proto tcp" in rules[0], (
        f"{rules[0]}\n"
        f"names no protocol, so ufw rejects every range in LAGER_PORTS: "
        f"{_UFW_MULTIPORT_ERROR}. Add `proto tcp`."
    )


def test_no_allow_rule_is_left_without_a_protocol():
    """Catches a fifth interface added later without the protocol."""
    bare = [r for r in _rules(_ALLOW_RULE_RE) if "proto tcp" not in r]
    assert not bare, (
        "these rules feed a LAGER_PORTS entry to ufw with no protocol:\n  "
        + "\n  ".join(bare)
        + f"\nufw answers a range there with: {_UFW_MULTIPORT_ERROR}"
    )


def test_the_external_deny_rules_name_a_protocol():
    """These already spell it `$PORT/tcp`; the constraint is the same one."""
    bare = [
        r for r in _rules(_DENY_RULE_RE) if "/tcp" not in r and "proto tcp" not in r
    ]
    assert not bare, "these deny rules name no protocol:\n  " + "\n  ".join(bare)


def test_all_of_tcp_is_the_right_protocol():
    """`proto tcp` is only correct while the box publishes nothing over UDP.

    start_box.sh publishes with a plain `-p`, which docker reads as TCP. A `/udp`
    appearing there would need a second allow rule, not a changed one.
    """
    block = _extract("port publishing")
    assert "/udp" not in block, (
        "start_box.sh publishes a UDP port. The firewall allows `proto tcp` only, "
        "so that port would be admitted by no rule -- add a udp rule alongside."
    )


def test_the_unfirewalled_window_is_covered_by_a_trap():
    """`ufw --force reset` wipes the rules long before `ufw --force enable`.

    A failure in between used to leave the box with the firewall off and no rules,
    reported as nothing more specific than "Deployment failed!". The trap has to be
    armed before the disable and released only after the enable, or the window it
    exists for is not the window it covers.
    """
    # The handler releases the trap and re-runs `ufw --force enable` itself, so
    # searching the whole file finds its copies rather than the main flow's.
    lines = FIREWALL.read_text().replace(_trap_handler_body(), "").splitlines()

    def line_of(predicate, what):
        for i, line in enumerate(lines):
            if predicate(line.strip()):
                return i
        raise AssertionError(f"{what} not found in {FIREWALL}")

    arm = line_of(
        lambda s: (
            s.startswith("trap ")
            and s.endswith(" EXIT")
            and not s.startswith("trap - ")
        ),
        "the EXIT trap",
    )
    disable = line_of(
        lambda s: s.startswith("ufw --force disable"), "`ufw --force disable`"
    )
    enable = line_of(
        lambda s: s.startswith("ufw --force enable"), "`ufw --force enable`"
    )
    release = line_of(lambda s: s == "trap - EXIT", "the trap release")

    assert arm < disable, "the trap is armed after the firewall is already down"
    assert enable < release, "the trap is released before the firewall is back up"


def test_the_trap_restores_ssh_not_just_the_default_policy():
    """Re-enabling a reset ufw without re-adding SSH locks the box out.

    `ufw --force reset` removes the SSH rule too, so a trap that only re-enables
    leaves default-deny with nothing allowed -- worse than the firewall it replaced.
    """
    body = _trap_handler_body()
    assert "allow 22/tcp" in body, (
        "the trap re-enables ufw without restoring the SSH rule that "
        "`ufw --force reset` removed"
    )
    assert "--force enable" in body, "the trap does not bring the firewall back up"
