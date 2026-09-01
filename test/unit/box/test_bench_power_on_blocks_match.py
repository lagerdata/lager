"""The three bench workflows must open with the same power-on shell.

`integration-tests.yml`, `update-regression.yml` and `bench-extended.yml` each
begin their hardware phase with a step named "Power on bench instruments (AC
relays)". The `run:` block is the same procedure in all three: drive both AC
relays high, wait for every relay-powered instrument to enumerate, then wait for
the box's hardware service to answer a real supply read.

Keeping the three in sync was an honour-system comment, and it failed. A change
that added a relay-write retry and put the Keithley 2281S behind
`KEITHLEY_PRESENT` landed in `integration-tests.yml` only. The nightly runs
`update-regression.yml` FIRST, so it still waited for an instrument that is off
the bench, failed at power-on, and skipped the integration job that carried the
fix -- a red nightly against a commit whose diff said the problem was solved.

This pins the three blocks to each other. It reads the workflows rather than a
duplicated copy of the expected shell, so no side can move without the others.

Scope: the `run:` block only. Each step keeps its own leading comment, `env:`
block, `id:`, `if:` and `continue-on-error:` -- those legitimately differ by
caller. What must not differ is the procedure itself.
"""

import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

STEP_NAME = "      - name: Power on bench instruments (AC relays)"
RUN_OPEN = "        run: |"
STEP_KEYS = ("        shell: bash", "      - name: ")

FILES = ("integration-tests.yml", "update-regression.yml", "bench-extended.yml")


def _run_block(filename):
    """Return the `run:` block of the power-on step, or None if absent."""
    path = WORKFLOWS / filename
    lines = path.read_text().splitlines()

    try:
        start = lines.index(STEP_NAME)
    except ValueError:
        pytest.fail(f"{filename}: no step named {STEP_NAME.strip()!r}")

    try:
        run_at = lines.index(RUN_OPEN, start)
    except ValueError:
        pytest.fail(f"{filename}: power-on step has no `run: |` block")

    body = []
    for line in lines[run_at + 1:]:
        if any(line.startswith(k) for k in STEP_KEYS):
            break
        body.append(line)

    assert body, f"{filename}: power-on `run:` block is empty"
    return "\n".join(body)


def test_the_parser_found_something():
    """Guard the guard: a parser matching nothing would pass forever."""
    block = _run_block("integration-tests.yml")
    assert "lager gpo" in block
    assert "expected=(" in block
    assert len(block.splitlines()) > 50


@pytest.mark.parametrize("filename", FILES[1:])
def test_power_on_run_block_matches_the_reference(filename):
    """Every bench workflow runs the same power-on procedure."""
    reference = _run_block(FILES[0])
    actual = _run_block(filename)

    assert actual == reference, (
        f"{filename}'s power-on `run:` block has drifted from "
        f"{FILES[0]}'s. These three run the same procedure and the nightly "
        f"chains two of them, so a fix applied to one and not the others "
        f"leaves the bench red against a commit that looks fixed. Copy the "
        f"block from {FILES[0]} verbatim; per-caller differences belong in "
        f"the step's `env:`/`if:`/`id:`, not in the shell."
    )


@pytest.mark.parametrize("filename", FILES)
def test_the_keithley_is_gated_not_hardcoded(filename):
    """The 2281S is off the bench; no workflow may require it unconditionally."""
    block = _run_block(filename)
    assert 'if [ "${KEITHLEY_PRESENT:-}" = "true" ]' in block, (
        f"{filename}: the Keithley 2281S must be gated on KEITHLEY_PRESENT, "
        f"not listed unconditionally in `expected`. It is off the bench "
        f"(#417) and an unconditional entry fails every run at power-on."
    )
    assert "expected=(Rigol_DP821 Rigol_MSO5204)" in block, (
        f"{filename}: the unconditional inventory should hold only the "
        f"instruments actually on the bench."
    )
