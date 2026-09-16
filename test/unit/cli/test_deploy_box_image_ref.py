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

The deploy script as a whole is far too large to run here, but its image and
container handoff is not. TestImageHandoffRuns extracts that block between its
sentinels and runs it with ssh stubbed, recording every remote command in
order, and TestPrePullCommandRuns runs the generated pull command against a
fake docker.
"""

import os
import pathlib
import shutil
import subprocess

import pytest

from cli.commands.utility.update import (
    _box_image_ref_for_version,
    resolve_version_ref,
)

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
    # Commit SHAs (#326): a third arm that resolves to itself and, like a
    # branch, has no published image. The near-misses must stay branches.
    "5d84c68612384eed2854638c1e0941a4ff8b7893",
    "5D84C68612384EED2854638C1E0941A4FF8B7893",
    "5d84c68",
    "5d84c68612384eed2854638c1e0941a4ff8b7893a",
    "z5d84c68612384eed2854638c1e0941a4ff8b789",
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
    def test_the_two_resolvers_agree_on_the_git_ref_too(self, version):
        # Agreeing about images is not enough: the shell and the client must
        # also agree about what to check the box out to. Before the SHA arm
        # existed these could not disagree, because both had the same two
        # branches; with three arms that is a property to assert, not assume.
        git_ref, _ = _bash_resolve(version)
        _, python_reset, _ = resolve_version_ref(version)
        assert git_ref == python_reset, (
            f"{version!r}: bash resolves to {git_ref!r}, "
            f"resolve_version_ref resolves to {python_reset!r}"
        )

    @pytest.mark.parametrize("version", VERSIONS)
    def test_an_image_ref_is_set_exactly_when_the_ref_is_a_tag(self, version):
        # The stronger statement: the image ref is not merely equal to the
        # client's, it is set in the same arm that resolves to a release TAG.
        #
        # Tag-ness is taken from the client's own answer (a tag is the arm that
        # produces a refs/tags/ refspec) rather than inferred from the shape of
        # git_ref. "does not start with origin/" USED to mean "is a tag", and
        # the SHA arm broke that equivalence -- a SHA resolves to a bare object
        # id and has no image, so the old inference would have demanded one.
        git_ref, image_ref = _bash_resolve(version)
        _, _, python_fetch = resolve_version_ref(version)
        is_tag = python_fetch.startswith("refs/tags/")
        assert bool(image_ref) == is_tag, (
            f"{version!r}: git_ref={git_ref!r} image_ref={image_ref!r} "
            f"is_tag={is_tag}"
        )

    def test_a_commit_sha_resolves_to_itself_and_has_no_image(self):
        # The #326 case stated on its own, so a regression names the reason.
        sha = "5d84c68612384eed2854638c1e0941a4ff8b7893"
        git_ref, image_ref = _bash_resolve(sha)
        assert git_ref == sha, f"a SHA must not become origin/<sha>: {git_ref!r}"
        assert image_ref == "", "a commit is not a release and has no image"

    def test_an_uppercase_sha_is_normalised_like_the_client_does(self):
        upper = "5D84C68612384EED2854638C1E0941A4FF8B7893"
        git_ref, _ = _bash_resolve(upper)
        assert git_ref == upper.lower()

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
        # is opt-in cannot apply here. What the variable resolves to is pinned
        # by TestBoxImagePullVocabulary, which runs the block rather than
        # matching its text: a string assertion pins spelling, and the bug it
        # missed was two commands giving one spelling opposite meanings.
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

    def test_timeout_option_exists(self):
        from cli.commands.utility.install import install
        names = {p.name for p in install.params}
        assert "timeout" in names

    def test_timeout_rejects_a_negative(self):
        from click.testing import CliRunner
        from cli.commands.utility.install import install

        result = CliRunner().invoke(install, ["--ip", "10.0.0.1", "--timeout", "-5"])
        assert result.exit_code != 0
        assert "not in the range" in result.output

    def test_the_deploy_budget_is_no_longer_a_literal(self):
        # Issue #316: `timeout=1800` appeared twice -- the subprocess call and
        # the message -- so there was no way to raise it without editing the
        # source. Both are now derived from the resolved value.
        source = (ROOT / "cli" / "commands" / "utility" / "install.py").read_text()
        assert "timeout=1800" not in source
        assert "timed out after 30 minutes" not in source
        assert "timeout=deploy_timeout or None" in source

    def test_the_timeout_message_names_the_way_out(self):
        # The timeout fires during the build, after the old container is gone.
        # An operator seeing only "timed out" has no way to know the build was
        # healthy, that an override exists, or that a re-run is cheap.
        source = (ROOT / "cli" / "commands" / "utility" / "install.py").read_text()
        assert "--timeout" in source
        assert "LAGER_INSTALL_TIMEOUT" in source
        assert "Re-running is safe" in source


class TestPrePullBeforeTeardown:
    """A release-tag install pulls its image while the old containers serve.

    The deployment used to remove the containers and prune the build cache
    first, and only then resolve and pull, so the box was down for the whole
    download and any miss became a fully cold build.
    """

    def _prepull_cmd(self, ref):
        body = _extract(DEPLOY_SCRIPT, "image pre-pull")
        return subprocess.run(
            ["bash", "-c", body + f"\nbox_image_prepull_cmd {ref!r}\n"],
            capture_output=True, text=True, check=True,
        ).stdout

    def test_the_prepull_command_has_the_clients_pull_shape(self):
        from cli.commands.utility.update import _docker_pull_cmd

        digest = "sha256:" + "c" * 64
        cmd = self._prepull_cmd(f"{REGISTRY}@{digest}")
        assert subprocess.run(["bash", "-n", "-c", cmd]).returncode == 0
        assert f"{REGISTRY}@{digest}" in cmd
        assert "timeout 300 docker" in cmd
        assert cmd.index("rc=$?") < cmd.index('rm -rf "$cfg"')
        assert cmd.rstrip().endswith("exit $rc")
        client = _docker_pull_cmd(f"{REGISTRY}:v0.46.2", digest)
        for shared in (
            'cfg=$(mktemp -d)',
            '--config "$cfg"',
            '--platform "linux/$(dpkg --print-architecture 2>/dev/null || uname -m)"',
            'rm -rf "$cfg"',
        ):
            assert shared in cmd, shared
            assert shared in client, shared


_HANDOFF_STUBS = r'''
set -e
LOG="$1"
SSH_OPTS=""; BOX_USER=benchuser; BOX_IP=192.0.2.10; VPN_INTERFACE=""
BOX_IMAGE_REGISTRY=ghcr.io/lagerdata/lager-box
print_info()    { echo "INFO: $*"; }
print_success() { echo "OK: $*"; }
print_warning() { echo "WARN: $*"; }
print_error()   { echo "ERR: $*"; }
resolve_box_image_digest() {
    if [ "${RESOLVE_RC:-0}" = 0 ]; then printf '%s\n' "$DIGEST"; return 0; fi
    echo "registry unreachable" >&2
    return 1
}
ssh() {
    eval "remote=\"\${$#}\""
    printf '%s' "$remote" | tr '\n' ' ' >> "$LOG"
    printf '\n' >> "$LOG"
    case "$remote" in
        *"docker info"*) return "${DOCKER_INFO_RC:-0}" ;;
        *"pull --platform"*)
            if [ "${PULL_RC:-0}" != 0 ]; then echo "manifest unknown" >&2; fi
            return "${PULL_RC:-0}" ;;
    esac
    return 0
}
'''

_HANDOFF_REPORT = '\necho "HANDOFF=[${LAGER_BOX_IMAGE_ENV}] PREPULLED=${BOX_IMAGE_PREPULLED}"\n'

_DIGEST = "sha256:" + "a" * 64
_TAG = {"BOX_IMAGE_PULL": "1", "BOX_IMAGE_TAG_REF": f"{REGISTRY}:v0.46.2",
        "GIT_VERSION": "v0.46.2", "DIGEST": _DIGEST}


def _step(remote):
    """Name the deploy step a remote command belongs to."""
    remote = remote.strip()
    for marker, name in (
        ("docker info", "daemon-check"),
        ("systemctl show docker", "daemon-diagnosis"),
        ("docker image prune -f", "image-prune"),
        ("pull --platform", "pre-pull"),
        ("for c in lager pigpio controller", "teardown"),
        ("docker builder prune -af", "build-cache-prune"),
        ("df -h /", "disk-check"),
        ("LAGER_WG_IFACE", "vpn"),
        ("./start_box.sh", "start"),
    ):
        if marker in remote:
            return name
    return f"unexpected: {remote[:60]}"


class TestImageHandoffRuns:
    """The deploy's image-and-container handoff, run for real with ssh stubbed.

    The block is extracted between its sentinels and executed under `set -e`,
    as the script runs it, with every remote command recorded in order. What
    it proves, per branch: the image is pulled while the old containers still
    serve, the containers stop only afterwards, the build cache is cleared only
    when the pull succeeded, and start_box.sh receives the image only then.
    """

    def _run(self, tmp_path, **env):
        block = _extract(DEPLOY_SCRIPT, "image and container handoff")
        log = tmp_path / "remote.log"
        log.write_text("")
        run_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **env}
        proc = subprocess.run(
            ["bash", "-c", _HANDOFF_STUBS + block + _HANDOFF_REPORT, "handoff", str(log)],
            env=run_env, capture_output=True, text=True, cwd=str(tmp_path),
        )
        remotes = [line for line in log.read_text().splitlines() if line.strip()]
        return proc, [_step(r) for r in remotes], remotes

    def test_a_pulled_image_is_on_the_box_before_the_containers_stop(self, tmp_path):
        proc, steps, remotes = self._run(tmp_path, **_TAG, RESOLVE_RC="0", PULL_RC="0")
        assert proc.returncode == 0, proc.stderr
        assert steps == ["daemon-check", "image-prune", "pre-pull", "teardown",
                         "build-cache-prune", "disk-check", "start", "image-prune"]
        assert f"LAGER_BOX_IMAGE={REGISTRY}@{_DIGEST}" in remotes[-2]
        assert "LAGER_BOX_IMAGE_VERSION=v0.46.2" in remotes[-2]
        assert "PREPULLED=1" in proc.stdout

    def test_a_failed_pull_builds_and_keeps_the_cache(self, tmp_path):
        proc, steps, remotes = self._run(tmp_path, **_TAG, RESOLVE_RC="0", PULL_RC="1")
        assert proc.returncode == 0, proc.stderr
        assert steps == ["daemon-check", "image-prune", "pre-pull", "teardown",
                         "disk-check", "start", "image-prune"]
        assert "LAGER_BOX_IMAGE" not in remotes[-2]
        assert "manifest unknown" in proc.stdout
        assert "PREPULLED=0" in proc.stdout

    @pytest.mark.parametrize("env, says", [
        ({**_TAG, "RESOLVE_RC": "1"}, "registry unreachable"),
        ({**_TAG, "RESOLVE_RC": "0", "DIGEST": "sha256:abc;touch INJECTED"}, "unexpected form"),
        ({**_TAG, "BOX_IMAGE_PULL": "0"}, "Building and starting containers"),
        ({"BOX_IMAGE_PULL": "1", "BOX_IMAGE_TAG_REF": "", "GIT_VERSION": "main"},
         "only release tags are published"),
    ], ids=["registry-unreachable", "malformed-digest", "no-pull", "branch-target"])
    def test_no_image_means_a_build_with_the_cache_kept(self, tmp_path, env, says):
        proc, steps, remotes = self._run(tmp_path, **env)
        assert proc.returncode == 0, proc.stderr
        assert steps == ["daemon-check", "image-prune", "teardown", "disk-check",
                         "start", "image-prune"]
        assert "LAGER_BOX_IMAGE" not in remotes[-2]
        assert says in proc.stdout
        assert not (tmp_path / "INJECTED").exists()

    def test_a_stopped_daemon_ends_the_deploy_before_anything_is_touched(self, tmp_path):
        proc, steps, _ = self._run(tmp_path, **_TAG, DOCKER_INFO_RC="1")
        assert proc.returncode == 1
        assert steps == ["daemon-check", "daemon-diagnosis"]

    def test_the_replaced_image_is_reclaimed_after_the_container_runs(self, tmp_path):
        # The prune that runs before the pre-pull cannot reach the image this
        # deploy replaces. start_box.sh moves the `lager` tag onto the new
        # image at the very end, and only at that moment does the old one go
        # dangling -- so without a second prune it sits on the box, about 3 GB,
        # until the next install. The first prune must NOT move to cover this:
        # it has to stay ahead of the pre-pull, or it reaches the image this
        # deploy just downloaded, which carries no tag until start_box.sh runs.
        proc, steps, _ = self._run(tmp_path, **_TAG, RESOLVE_RC="0", PULL_RC="0")
        assert proc.returncode == 0, proc.stderr
        assert steps.count("image-prune") == 2
        assert steps[-1] == "image-prune"
        assert steps.index("image-prune") < steps.index("pre-pull")


class TestBoxImagePullVocabulary:
    """`LAGER_BOX_IMAGE_PULL` means the same thing here as in `lager update`.

    update reads it as an opt-IN: 1, true or yes in any letter case turn its
    pull on, and every other value leaves it off. This script starts from on
    and applies that same rule in reverse. Before the two agreed, `=false`
    left the install pull ON while turning the update pull OFF -- one spelling
    with opposite meanings, which is invisible to anyone who exports it once
    for a whole shell and then runs both commands.

    These run the block rather than matching its text. The previous test
    asserted the literal assignment line, which pins spelling and cannot see a
    disagreement about meaning.
    """

    @staticmethod
    def _resolve(env_value):
        block = _extract(DEPLOY_SCRIPT, "image pull vocabulary")
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        if env_value is not None:
            env["LAGER_BOX_IMAGE_PULL"] = env_value
        proc = subprocess.run(
            ["bash", "-c", block + '\necho "PULL=$BOX_IMAGE_PULL"'],
            env=env, capture_output=True, text=True,
        )
        assert proc.returncode == 0, proc.stderr
        return proc.stdout.strip().rsplit("PULL=", 1)[-1].strip()

    def test_unset_leaves_the_install_default_on(self):
        # The one difference from update that is deliberate: install pulls by
        # default, update does not.
        assert self._resolve(None) == "1"

    @pytest.mark.parametrize("value", ["1", "true", "yes", "TRUE", "Yes"])
    def test_the_words_update_accepts_turn_it_on_here_too(self, value):
        assert self._resolve(value) == "1"

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "FALSE", "No"])
    def test_a_value_update_reads_as_off_turns_it_off_here_too(self, value):
        # `false` is the spelling that mattered: it used to leave this pull on.
        assert self._resolve(value) == "0"

    def test_an_unrecognized_value_turns_it_off_exactly_as_update_does(self):
        # update's rule is "anything that is not 1/true/yes leaves it off", so
        # a typo costs a slower install, never a disagreement between the two.
        assert self._resolve("ture") == "0"


class TestPrePullCommandRuns:
    """The generated pre-pull command, executed against a fake docker."""

    FAKE_DOCKER = """#!/bin/bash
echo "$*" >> "$DOCKER_LOG"
prev=""; for a in "$@"; do [ "$prev" = "--config" ] && cfg="$a"; prev="$a"; done
echo "$cfg" > "$CFG_RECORD"
[ -d "$cfg" ] && echo "config-dir-present" >> "$DOCKER_LOG"
exit "${FAKE_PULL_RC:-0}"
"""
    FAKE_DPKG = '#!/bin/bash\n[ "$1" = "--print-architecture" ] && echo amd64\n'
    FAKE_TIMEOUT = '#!/bin/bash\necho "timeout $1" >> "$DOCKER_LOG"\nshift\nexec "$@"\n'

    def _bin(self, tmp_path, with_timeout):
        # A PATH holding only what the command needs, so whether `timeout`
        # exists is decided here rather than by the machine running the test.
        bindir = tmp_path / "bin"
        bindir.mkdir()
        for tool in ("mktemp", "rm", "uname"):
            found = shutil.which(tool)
            assert found, f"{tool} is not on PATH"
            os.symlink(found, bindir / tool)
        scripts = {"docker": self.FAKE_DOCKER, "dpkg": self.FAKE_DPKG}
        if with_timeout:
            scripts["timeout"] = self.FAKE_TIMEOUT
        for name, body in scripts.items():
            path = bindir / name
            path.write_text(body)
            path.chmod(0o755)
        return bindir

    @pytest.mark.parametrize("with_timeout", [True, False], ids=["timeout", "no-timeout"])
    @pytest.mark.parametrize("pull_rc", [0, 7])
    def test_pulls_by_digest_passes_the_exit_status_and_cleans_up(self, tmp_path, with_timeout, pull_rc):
        ref = f"{REGISTRY}@{_DIGEST}"
        body = _extract(DEPLOY_SCRIPT, "image pre-pull")
        cmd = subprocess.run(
            ["bash", "-c", body + f"\nbox_image_prepull_cmd {ref!r}\n"],
            capture_output=True, text=True, check=True,
        ).stdout
        log, record = tmp_path / "docker.log", tmp_path / "cfg"
        proc = subprocess.run(
            ["/bin/bash", "-c", cmd],
            env={"PATH": str(self._bin(tmp_path, with_timeout)),
                 "DOCKER_LOG": str(log), "CFG_RECORD": str(record),
                 "FAKE_PULL_RC": str(pull_rc), "TMPDIR": str(tmp_path)},
            capture_output=True, text=True,
        )
        assert proc.returncode == pull_rc
        lines = log.read_text().splitlines()
        if with_timeout:
            assert lines[0] == "timeout 300"
            lines = lines[1:]
        cfg = record.read_text().strip()
        assert lines[0] == f"--config {cfg} pull --platform linux/amd64 {ref}"
        assert lines[1] == "config-dir-present"
        assert cfg and not os.path.exists(cfg)
