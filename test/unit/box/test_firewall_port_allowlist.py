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
