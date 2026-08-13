# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
`lager update --check` must not promise a cached build it cannot deliver.

An observed run previewed `Deps: cache valid (no rebuild)` and
`Estimated: ~90s (cached build)`, then took ten minutes on a clean rebuild.
The box was still on the `box/` subdir layout, which the preview ignored
entirely even though `_rebuild_gate_verdict` treats a pending flatten as a
definite rebuild trigger.

The governing property is asymmetric on purpose: over-estimating a build
costs an operator nothing, under-estimating one sends them away from the
terminal. So these tests pin "never says cached when the gate would
rebuild", not "always agrees with the gate".
"""

import importlib
import itertools

u = importlib.import_module("cli.commands.utility.update")

HASH_A = "a" * 64
HASH_B = "b" * 64


def _preview(new_hash=HASH_A, stored_hash=HASH_A, *, force=False,
             needs_flatten=False, is_rollback=False, commits_ahead=0):
    return u._deps_preview(
        new_hash, stored_hash, force=force, needs_flatten=needs_flatten,
        is_rollback=is_rollback, commits_ahead=commits_ahead,
    )


class TestTheRegression:
    def test_pending_flatten_never_reports_a_valid_cache(self):
        # The exact observed case: hashes agree (or cannot be measured),
        # code is behind, and the box still has a box/ subdir.
        status, certain, _ = _preview(new_hash="", stored_hash=HASH_A,
                                      needs_flatten=True)
        assert certain is True
        assert "cache valid" not in status
        assert "flatten" in status

    def test_flatten_is_certain_not_merely_possible(self):
        # The flatten moves every source file and the hash is over sha256sum
        # output, which includes each path — so the post-flatten hash cannot
        # match. "may rebuild" would understate it.
        status, certain, _ = _preview(needs_flatten=True)
        assert certain is True
        assert "may" not in status.lower()


class TestUnmeasuredIsNotUnchanged:
    def test_missing_new_hash_is_named_not_dressed_up(self):
        status, _, unmeasured = _preview(new_hash="", stored_hash=HASH_A)
        assert unmeasured is True
        assert "cache valid" not in status
        assert "unknown" in status
        assert "box source not where the probe looks" in status

    def test_missing_stored_hash_names_the_other_reason(self):
        status, _, unmeasured = _preview(new_hash=HASH_A, stored_hash="")
        assert unmeasured is True
        assert "no successful build recorded yet" in status

    def test_unmeasured_does_not_predict_a_rebuild(self):
        # `_rebuild_gate_verdict` does not treat an unmeasured hash as a
        # trigger, so neither may the preview. Claiming "will rebuild" here
        # would over-predict exactly as badly as "cache valid" under-predicted.
        _status, certain, unmeasured = _preview(new_hash="", stored_hash="")
        assert unmeasured is True
        assert certain is False

    def test_measured_and_equal_still_says_cache_valid(self):
        # The honest positive case must survive: this is the common path and
        # the whole point of the hash.
        status, certain, unmeasured = _preview(HASH_A, HASH_A)
        assert status == "cache valid (no rebuild)"
        assert certain is False
        assert unmeasured is False

    def test_measured_and_different_says_fresh_build(self):
        status, certain, _ = _preview(HASH_A, HASH_B)
        assert certain is True
        assert "fresh build" in status


class TestAgreementWithTheRealGate:
    """The preview and `_rebuild_gate_verdict` read the same box state. They
    are allowed to differ only in the safe direction."""

    def test_never_promises_a_cached_build_when_the_gate_would_rebuild(self):
        combos = itertools.product(
            [HASH_A, ""],          # new hash (measurable or not)
            [HASH_A, HASH_B, ""],  # stored hash
            [False, True],         # needs_flatten
            [False, True],         # force
            [False, True],         # needs_pull
        )
        for new_hash, stored_hash, needs_flatten, force, needs_pull in combos:
            hash_mismatch = u._build_hash_mismatch(new_hash, stored_hash)
            verdict = u._rebuild_gate_verdict(
                {"LAGER_RUNNING": "1"},
                git_sync_confirmed=True,
                needs_pull=needs_pull,
                needs_flatten=needs_flatten,
                hash_mismatch=hash_mismatch,
                force=force,
            )
            status, _certain, _unmeasured = _preview(
                new_hash, stored_hash, force=force, needs_flatten=needs_flatten,
            )
            if verdict == "rebuild" and not needs_pull:
                # needs_pull is excluded: a pull is reported on the `Code:`
                # line, and the deps line legitimately says nothing about it.
                assert status != "cache valid (no rebuild)", (
                    f"gate rebuilds but preview promises a valid cache: "
                    f"new={new_hash[:4]!r} stored={stored_hash[:4]!r} "
                    f"flatten={needs_flatten} force={force}"
                )

    def test_cache_valid_is_only_claimed_when_the_gate_would_skip(self):
        # The converse, restricted to the inputs the deps line owns: if the
        # preview says the cache is good, nothing in the hash/flatten state
        # may force a rebuild.
        for new_hash, stored_hash, needs_flatten in itertools.product(
            [HASH_A, ""], [HASH_A, HASH_B, ""], [False, True],
        ):
            status, _c, _u = _preview(new_hash, stored_hash,
                                      needs_flatten=needs_flatten)
            if status == "cache valid (no rebuild)":
                assert not needs_flatten
                assert not u._build_hash_mismatch(new_hash, stored_hash)


class TestPrecedence:
    def test_force_outranks_everything(self):
        status, _c, _u = _preview(HASH_A, HASH_B, force=True, needs_flatten=True)
        assert "--force" in status

    def test_a_real_mismatch_outranks_flatten(self):
        # Both are certain rebuilds; naming the content change is more useful
        # than naming the layout move.
        status, certain, _u = _preview(HASH_A, HASH_B, needs_flatten=True)
        assert certain is True
        assert "fresh build" in status

    def test_flatten_outranks_the_rollback_unknown(self):
        # A rollback's rebuild is speculative; a flatten's is not.
        status, certain, _u = _preview(needs_flatten=True, is_rollback=True)
        assert certain is True
        assert "flatten" in status
