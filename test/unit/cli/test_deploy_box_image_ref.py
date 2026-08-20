# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""The install path must agree with the client about what has a published image.

`lager install` gets the pre-built box image the same way `lager update` does,
but it has to decide in bash: install's whole deploy runs through
setup_and_deploy_box.sh, and the box has no docker until part-way through that
script, so the choice cannot be made in Python before it starts.

That leaves two implementations of one question -- "is this version a release
tag?" -- and the risk that they drift. The script does NOT re-derive it: the
image ref is computed inside the SAME arm of the SAME conditional that already
resolves a semver pin to a tag ref. These tests pin that agreement by running a
table of versions through both the extracted bash and
``_box_image_ref_for_version`` and requiring identical verdicts, the same way
cli/tests/test_update_gate.py pins the client against ``resolve_version_ref``.

The digest resolver is the other half. It is a third implementation of the GHCR
protocol (after update.py's ``_resolve_image_digest``), so it is tested against
a fake curl for shape and, in the hardware pass, against the real registry for
agreement -- both returned byte-identical digests for v0.39.1 and v0.38.0.

Nothing here executes the deploy script itself; it is far too large to run.
"""

import pathlib
import subprocess

import pytest

from cli.commands.utility.update import _box_image_ref_for_version

ROOT = pathlib.Path(__file__).resolve().parents[3]
DEPLOY_SCRIPT = ROOT / "cli" / "deployment" / "scripts" / "setup_and_deploy_box.sh"
START_BOX = ROOT / "box" / "start_box.sh"

REGISTRY = "ghcr.io/lagerdata/lager-box"


def _extract(path, topic):
    """Return the shell between the BEGIN/END sentinels naming `topic`."""
    begin, end = f"# --- BEGIN {topic}", f"# --- END {topic}"
    body, inside, seen = [], False, False
    for line in path.read_text().splitlines():
        if line.startswith(begin):
            inside, seen = True, True
            continue
        if line.startswith(end):
            inside = False
            continue
        if inside:
            body.append(line)
    assert seen, f"sentinel {begin!r} not found in {path}"
    assert body, f"no shell extracted for {topic!r}"
    return "\n".join(body)


def _uncommented(text):
    """Shell minus comment lines -- the comments here discuss the very awk
    dialect these tests assert is absent, so matching them would pass on prose.
    Same guard as test_docker_start_limit.py."""
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


VERSION_RESOLUTION_SH = _extract(DEPLOY_SCRIPT, "version resolution")
RESOLVER_SH = _extract(DEPLOY_SCRIPT, "image digest resolution")
RESOLVER_CODE = _uncommented(RESOLVER_SH)


def _bash_resolve(version):
    """Run the script's own version resolution. Returns (git_ref, image_ref)."""
    script = (
        f'BOX_IMAGE_REGISTRY={REGISTRY}\n'
        'BOX_IMAGE_TAG_REF=""\n'
        f'GIT_VERSION={version!r}\n'
        + VERSION_RESOLUTION_SH
        + '\nprintf "%s\\n%s\\n" "$GIT_REF" "$BOX_IMAGE_TAG_REF"\n'
    )
    out = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    out += ["", ""]
    return out[0], out[1]


# Both a tag and a non-tag of every shape the two implementations must agree on.
VERSIONS = [
    "v0.39.1", "0.39.1", "v1.2.3", "1.2.3", "v10.20.30",
    "v1.2.3-rc1", "1.2.3-rc", "v1.2.3-alpha2", "v1.2.3-beta", "v1.2.3-preview9",
    "main", "staging", "de/install-pull", "v1.2", "1.2.3.4", "v1.2.3-nightly",
    "release/v1.2.3", "vv1.2.3", "", "HEAD",
]


class TestRegexAgreement:
    """The anti-drift property: what has an image == what has a tag."""

    @pytest.mark.parametrize("version", VERSIONS)
    def test_bash_and_python_agree_on_what_has_an_image(self, version):
        _, bash_ref = _bash_resolve(version)
        python_ref = _box_image_ref_for_version(version) or ""
        assert bash_ref == python_ref, (
            f"{version!r}: bash says {bash_ref!r}, "
            f"_box_image_ref_for_version says {python_ref!r}"
        )

    @pytest.mark.parametrize("version", VERSIONS)
    def test_an_image_ref_is_set_exactly_when_the_ref_is_a_tag(self, version):
        # The stronger statement: the image ref is not merely equal to the
        # client's, it is set in the same arm that resolves to a TAG rather
        # than to origin/<branch>. A branch can never have a published image.
        git_ref, image_ref = _bash_resolve(version)
        is_tag = not git_ref.startswith("origin/")
        assert bool(image_ref) == is_tag, (
            f"{version!r}: git_ref={git_ref!r} image_ref={image_ref!r}"
        )

    def test_the_image_ref_carries_the_normalised_v_prefix(self):
        # The publisher stamps org.opencontainers.image.version with the
        # v-prefixed tag, and start_box.sh compares the label against
        # LAGER_BOX_IMAGE_VERSION verbatim. A bare `0.39.1` reaching the box
        # would fail that comparison on every single pull.
        for version in ("0.39.1", "v0.39.1"):
            _, image_ref = _bash_resolve(version)
            assert image_ref == f"{REGISTRY}:v0.39.1"

    def test_the_two_regexes_are_computed_in_one_place(self):
        # If a future edit moves the image ref out of the conditional, the
        # agreement above becomes a coincidence rather than a structural fact.
        block = VERSION_RESOLUTION_SH
        assert block.count("BOX_IMAGE_TAG_REF=") == 1
        assert block.index("BOX_IMAGE_TAG_REF=") < block.index("else")


class TestDigestResolver:
    """Shape of the anonymous GHCR resolution, driven by a fake curl."""

    @pytest.fixture
    def resolve(self, tmp_path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log = tmp_path / "curl.log"
        (bin_dir / "curl").write_text(
            "#!/bin/bash\n"
            'printf "%s\\n" "$*" >> "$CURL_LOG"\n'
            'for a in "$@"; do case "$a" in\n'
            '  *"/token?"*) printf \'{"token":"%s","expires_in":300}\\n\' "${FAKE_TOKEN-tok123}"; exit 0 ;;\n'
            '  *"/manifests/"*) [ -n "${FAKE_NO_DIGEST:-}" ] && { printf "HTTP/2 404\\r\\n"; exit 0; }\n'
            '                   printf "HTTP/2 200\\r\\nDocker-Content-Digest: %s\\r\\n" "${FAKE_DIGEST}"; exit 0 ;;\n'
            'esac; done\n'
            'exit 0\n'
        )
        (bin_dir / "curl").chmod(0o755)

        def _resolve(ref=f"{REGISTRY}:v0.39.1", **fakes):
            env = {
                "PATH": f"{bin_dir}:/usr/bin:/bin",
                "CURL_LOG": str(log),
                "FAKE_DIGEST": "sha256:" + "b" * 64,
            }
            env.update({k: str(v) for k, v in fakes.items()})
            proc = subprocess.run(
                ["bash", "-c", RESOLVER_SH + f'\nresolve_box_image_digest {ref!r}\n'],
                env=env, capture_output=True, text=True,
            )
            calls = log.read_text().splitlines() if log.exists() else []
            return proc, calls

        return _resolve

    def test_happy_path_prints_the_digest(self, resolve):
        proc, _ = resolve()
        assert proc.returncode == 0
        assert proc.stdout.strip() == "sha256:" + "b" * 64

    def test_token_scope_is_anonymous_and_pull_only(self, resolve):
        # Authenticating with the operator's own credentials would make a
        # package that boxes cannot read appear to work perfectly in testing.
        _, calls = resolve()
        token_call = next(c for c in calls if "/token?" in c)
        assert "scope=repository:lagerdata/lager-box:pull" in token_call
        assert "Authorization" not in token_call

    def test_manifest_request_sends_the_oci_accept_types(self, resolve):
        # Sending the wrong Accept can make a registry hand back a CONVERTED
        # manifest, whose digest is not the one docker resolves on the box --
        # the pull would then miss on a digest we ourselves published.
        _, calls = resolve()
        manifest_call = next(c for c in calls if "/manifests/" in c)
        for kind in (
            "application/vnd.oci.image.manifest.v1+json",
            "application/vnd.oci.image.index.v1+json",
            "application/vnd.docker.distribution.manifest.v2+json",
            "application/vnd.docker.distribution.manifest.list.v2+json",
        ):
            assert kind in manifest_call
        assert "Authorization: Bearer tok123" in manifest_call

    def test_unpublished_tag_is_a_miss_not_a_crash(self, resolve):
        proc, _ = resolve(FAKE_NO_DIGEST=1)
        assert proc.returncode != 0
        assert proc.stdout.strip() == ""

    def test_missing_token_is_a_miss(self, resolve):
        proc, calls = resolve(FAKE_TOKEN="")
        assert proc.returncode != 0
        assert not any("/manifests/" in c for c in calls), (
            "must not attempt the manifest request without a token"
        )

    def test_timeouts_are_tight(self):
        # These run before anything is touched on the box and double as the
        # reachability test. On a network where ghcr.io black-holes, a generous
        # timeout would be paid on EVERY install before falling back.
        assert "--connect-timeout 3" in RESOLVER_CODE
        assert "--max-time 8" in RESOLVER_CODE

    def test_digest_header_is_matched_case_insensitively(self):
        # gawk's IGNORECASE is unavailable on macOS awk, and the operator's
        # machine is as often a Mac as a Linux runner.
        assert "tolower(" in RESOLVER_CODE
        assert "IGNORECASE" not in RESOLVER_CODE


class TestHandoffToStartBox:
    """The single start_box.sh invocation must carry both variables."""

    def test_invocation_threads_the_image_and_its_expected_version(self):
        text = DEPLOY_SCRIPT.read_text()
        invocations = [
            line for line in text.splitlines()
            if "./start_box.sh" in line and line.lstrip().startswith("ssh ")
        ]
        assert len(invocations) == 1, invocations
        assert "${LAGER_BOX_IMAGE_ENV}./start_box.sh" in invocations[0]
        assert "LAGER_BOX_IMAGE=" in text
        assert "LAGER_BOX_IMAGE_VERSION=" in text

    def test_start_box_reads_both_variables(self):
        text = START_BOX.read_text()
        assert "${LAGER_BOX_IMAGE:-}" in text
        assert "${LAGER_BOX_IMAGE_VERSION:-}" in text

    def test_pull_is_on_by_default_and_no_pull_turns_it_off(self):
        text = DEPLOY_SCRIPT.read_text()
        # An install always has a cold layer cache, so the reason update's pull
        # is opt-in cannot apply here.
        assert 'BOX_IMAGE_PULL="${LAGER_BOX_IMAGE_PULL:-1}"' in text
        assert "--no-pull)" in text
        assert "--pull)" in text

    def test_a_branch_target_never_resolves_an_image(self):
        _, image_ref = _bash_resolve("main")
        assert image_ref == ""


class TestInstallCommandSurface:
    """`lager install`'s flags, and what it forwards to the deploy script."""

    def test_both_flags_exist(self):
        from cli.commands.utility.install import install
        names = {p.name for p in install.params}
        assert {"pull", "no_pull"} <= names

    def test_pull_and_no_pull_are_mutually_exclusive(self):
        from click.testing import CliRunner
        from cli.commands.utility.install import install

        result = CliRunner().invoke(install, ["--ip", "10.0.0.1", "--pull", "--no-pull"])
        assert result.exit_code != 0
        assert "mutually exclusive" in result.output

    def test_only_the_overriding_flag_is_forwarded(self):
        # The script owns what the default IS. Forwarding `--pull` on a plain
        # run would freeze today's default into the CLI, so a later change to
        # the script's default would silently not reach anyone.
        source = (ROOT / "cli" / "commands" / "utility" / "install.py").read_text()
        assert 'deploy_args.append("--no-pull")' in source
        assert 'deploy_args.append("--pull")' in source
        no_pull_at = source.index('deploy_args.append("--no-pull")')
        pull_at = source.index('deploy_args.append("--pull")')
        assert source[:no_pull_at].rstrip().endswith("if no_pull:")
        assert source[:pull_at].rstrip().endswith("elif pull:")

    def test_install_reuses_the_clients_tag_test(self):
        # A third copy of "is this a release tag?" would be a third thing to
        # drift; install imports the client's.
        source = (ROOT / "cli" / "commands" / "utility" / "install.py").read_text()
        assert "from .update import _box_image_ref_for_version" in source
