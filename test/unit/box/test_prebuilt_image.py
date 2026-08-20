# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the pre-built-image block in box/start_box.sh.

`lager install` resolves a release tag to an immutable GHCR digest on the
operator's machine and hands it to start_box.sh, which pulls that image instead
of spending ~14 minutes building one. The shell under test is extracted verbatim
between its ``# --- BEGIN pre-built image`` / ``# --- END ...`` sentinels, the
same way test_secret_file_ownership.py and test_authorized_keys_sync.py extract
their blocks, so these exercise the shipped code rather than a transcription.

Four properties make the pull safe, and dropping any one of them makes the
feature worse than not having it:

  1. pull by resolved DIGEST, never by tag -- a tag is mutable, so two boxes
     installed minutes apart could otherwise run different bytes with nothing
     recording which;
  2. assert ``org.opencontainers.image.version`` matches the requested tag, and
     reject an image carrying no label at all -- that label is the only evidence
     a pulled image offers about what is inside it;
  3. pin ``--platform``, so a non-amd64 host gets a clean manifest miss instead
     of an image that dies with `exec format error` at container start;
  4. pull anonymously through a throwaway docker config -- a box holding
     unrelated ghcr.io credentials otherwise gets `denied: denied` on a public
     image, because GHCR evaluates those credentials against *this* repository
     rather than falling back to anonymous.

And the rule that outranks all four: every miss falls back to the local build.
A slow install that works beats a fast one that does not. The last test here is
the one that matters most -- it walks every failure mode and asserts each one
falls through rather than failing the deploy.

Ownership of the real docker is what a unit test cannot have, so a fake `docker`
on PATH records its argv and is driven by env vars: FAKE_PULL_RC, FAKE_LABEL
(``__fail__`` makes inspect exit non-zero) and FAKE_TAG_RC.
"""

import pathlib
import subprocess

import pytest

START_BOX = pathlib.Path(__file__).resolve().parents[3] / "box" / "start_box.sh"

REPO = "ghcr.io/lagerdata/lager-box"
DIGEST = "sha256:" + "a" * 64
DIGEST_REF = f"{REPO}@{DIGEST}"
TAG_REF = f"{REPO}:v0.39.1"
WANT = "v0.39.1"


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


def _uncommented(text):
    """Shell minus comment lines -- the comments here name the very strings
    these tests assert are present in the CODE, so matching the whole block
    would pass on prose. Same guard as test_docker_start_limit.py."""
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


PREBUILT_SH = _extract("pre-built image")
PREBUILT_CODE = _uncommented(PREBUILT_SH)

FAKE_DOCKER = """#!/bin/bash
printf '%s\\n' "$*" >> "$DOCKER_LOG"
if [ "$1 $2" = "image inspect" ]; then
    [ "$FAKE_LABEL" = "__fail__" ] && exit 1
    printf '%s\\n' "$FAKE_LABEL"
    exit 0
fi
case "$1" in
    --config)
        if [ "${FAKE_PULL_RC:-0}" -ne 0 ]; then
            echo "denied: denied" >&2
            exit "${FAKE_PULL_RC}"
        fi
        ;;
    tag) exit "${FAKE_TAG_RC:-0}" ;;
esac
exit 0
"""


class Result:
    def __init__(self, proc, log):
        self.proc = proc
        self.stdout = proc.stdout
        self.returncode = proc.returncode
        self.calls = [c for c in log.read_text().splitlines() if c] if log.exists() else []

    @property
    def used_prebuilt(self):
        return "RESULT=PREBUILT" in self.stdout

    @property
    def fell_back_to_build(self):
        return "RESULT=BUILD" in self.stdout

    def called(self, needle):
        return any(needle in c for c in self.calls)


@pytest.fixture
def run(tmp_path):
    """Run the extracted block under `set -e` with a fake docker on PATH.

    `set -e` is not incidental: start_box.sh sets it, and the caller invokes
    the function as an `elif` condition, which suppresses errexit for the whole
    body. That is exactly what lets a failed pull fall through instead of
    killing the deploy -- so the harness has to reproduce it or the tests would
    pass on shell that aborts in production.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "docker"
    fake.write_text(FAKE_DOCKER)
    fake.chmod(0o755)
    log = tmp_path / "docker.log"

    def _run(*, image=DIGEST_REF, want=WANT, **fakes):
        env = {
            "PATH": f"{bin_dir}:/usr/bin:/bin:/usr/sbin:/sbin",
            "DOCKER_LOG": str(log),
            "HOME": str(tmp_path),
            "FAKE_LABEL": WANT,
        }
        if image is not None:
            env["LAGER_BOX_IMAGE"] = image
        if want is not None:
            env["LAGER_BOX_IMAGE_VERSION"] = want
        env.update({k: str(v) for k, v in fakes.items()})

        script = (
            "set -e\n"
            + PREBUILT_SH
            + '\nif [ -n "${LAGER_BOX_IMAGE:-}" ] && use_prebuilt_box_image; then\n'
            "    echo RESULT=PREBUILT\n"
            "else\n"
            "    echo RESULT=BUILD\n"
            "fi\n"
        )
        proc = subprocess.run(
            ["bash", "-c", script], env=env, capture_output=True, text=True,
        )
        return Result(proc, log)

    return _run


class TestDigestOnly:
    """Property 1: a mutable tag is refused by shape, not by trust."""

    def test_tag_reference_is_refused_before_any_docker_call(self, run):
        res = run(image=TAG_REF)
        assert res.fell_back_to_build
        assert res.calls == [], "a tag ref must not reach docker at all"
        assert "not a digest reference" in res.stdout

    def test_digest_reference_is_accepted(self, run):
        assert run().used_prebuilt


class TestVerification:
    """Property 2: the version label is the only evidence the image offers."""

    def test_missing_expected_version_is_refused_before_pulling(self, run):
        res = run(want=None)
        assert res.fell_back_to_build
        assert res.calls == [], "an unverifiable image must not be pulled"

    @pytest.mark.parametrize("label", ["", "<no value>"])
    def test_unlabelled_image_is_rejected_and_discarded(self, run, label):
        # A Go template indexing a missing key prints the literal `<no value>`.
        # An image published before the labelling workflow landed is
        # indistinguishable from one built from anything at all.
        res = run(FAKE_LABEL=label)
        assert res.fell_back_to_build
        assert res.called(f"rmi {DIGEST_REF}"), "a rejected ~1 GB image must not be left on the box"
        assert not res.called(f"tag {DIGEST_REF} lager")

    def test_inspect_failure_is_treated_as_unlabelled(self, run):
        res = run(FAKE_LABEL="__fail__")
        assert res.fell_back_to_build
        assert res.called(f"rmi {DIGEST_REF}")

    def test_mismatched_label_is_rejected_and_discarded(self, run):
        res = run(FAKE_LABEL="v0.38.0")
        assert res.fell_back_to_build
        assert "claims v0.38.0, expected v0.39.1" in res.stdout
        assert res.called(f"rmi {DIGEST_REF}")
        assert not res.called(f"tag {DIGEST_REF} lager")

    def test_label_read_uses_the_oci_version_key(self, run):
        run()
        assert 'org.opencontainers.image.version' in PREBUILT_CODE


class TestPullShape:
    """Properties 3 and 4, asserted on the emitted command."""

    def test_platform_is_pinned(self, run):
        res = run()
        pull = next(c for c in res.calls if " pull " in c)
        assert "--platform linux/" in pull

    def test_pull_goes_through_a_throwaway_config(self, run):
        res = run()
        pull = next(c for c in res.calls if " pull " in c)
        # NOTE: the command is `docker --config "$cfg" pull`, NOT `docker pull`.
        # A matcher grepping for the literal `docker pull` silently never fires.
        assert pull.startswith("--config "), pull
        cfg = pull.split()[1]
        assert not pathlib.Path(cfg).exists(), "the throwaway config dir must be removed"

    def test_exit_status_is_captured_before_cleanup(self):
        # Ordering matters: `rm -rf` would otherwise overwrite $? and every
        # failed pull would look like a success.
        assert PREBUILT_CODE.index("rc=$?") < PREBUILT_CODE.index('rm -rf "$cfg"')

    def test_platform_falls_back_when_dpkg_is_absent(self):
        assert 'dpkg --print-architecture 2>/dev/null || uname -m' in PREBUILT_CODE


class TestPromotion:
    def test_success_tags_lager_then_drops_the_digest_reference(self, run):
        res = run()
        assert res.used_prebuilt
        tag_at = next(i for i, c in enumerate(res.calls) if c.startswith("tag "))
        rmi_at = next(i for i, c in enumerate(res.calls) if c.startswith("rmi "))
        assert tag_at < rmi_at
        assert res.calls[tag_at] == f"tag {DIGEST_REF} lager"
        # Leaving the digest ref attached is what would stop `docker image
        # prune -f` from EVER reclaiming a superseded ~1 GB image.
        assert res.calls[rmi_at] == f"rmi {DIGEST_REF}"

    def test_failed_tag_discards_and_builds(self, run):
        res = run(FAKE_TAG_RC=1)
        assert res.fell_back_to_build

    def test_the_build_path_clears_stale_registry_provenance(self):
        # Caught on a real box: it pulled once (recording a ghcr: digest), then
        # rebuilt, and the file went on naming a registry image the box was no
        # longer running. A stale provenance record is worse than none.
        start_box = START_BOX.read_text()
        build_branch = start_box[start_box.index("Building Lager Box container"):]
        build_branch = build_branch[:build_branch.index("\nfi\n")]
        assert "rm -f /etc/lager/image-source" in build_branch

    def test_image_source_records_the_digest(self):
        # The write itself is best-effort and guarded on /etc/lager existing,
        # which no test machine has -- it is verified for real on a box. What
        # is pinned here is that the recorded value is the DIGEST and is
        # marked as coming from the registry, because /etc/lager/build-hash
        # cannot answer "where did this image come from": it hashes the box's
        # own tree, which reads identically whether the image was built here
        # or pulled. `lager update` writes the same `ghcr:` prefix.
        assert 'ghcr:%s' in PREBUILT_CODE
        assert '${ref##*@}' in PREBUILT_CODE
        assert '/etc/lager/image-source' in PREBUILT_CODE


class TestEveryMissFallsBack:
    """The rule that outranks the other four properties."""

    @pytest.mark.parametrize(
        "desc,kwargs",
        [
            ("tag instead of digest", {"image": TAG_REF}),
            ("no expected version", {"want": None}),
            ("registry denied", {"FAKE_PULL_RC": 1}),
            ("pull failed, other", {"FAKE_PULL_RC": 125}),
            ("unlabelled image", {"FAKE_LABEL": ""}),
            ("go template no value", {"FAKE_LABEL": "<no value>"}),
            ("inspect failed", {"FAKE_LABEL": "__fail__"}),
            ("label mismatch", {"FAKE_LABEL": "v0.1.92"}),
            ("retag failed", {"FAKE_TAG_RC": 1}),
        ],
    )
    def test_falls_back_rather_than_failing(self, run, desc, kwargs):
        res = run(**kwargs)
        assert res.fell_back_to_build, f"{desc}: must fall back to the local build"
        assert res.returncode == 0, f"{desc}: must not abort the deploy (set -e)"
