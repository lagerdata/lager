# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""What keeps a published box image cheap to pull.

A box pull downloads only the layers the box does not already hold, so what a
release costs every box comes down to whether the image's unchanged steps
reproduce their previous layers. Three things decide that, and each has
already been wrong once:

  1. ``box-image-publish.yml`` must keep its BuildKit layer cache where the
     NEXT release tag can read it. It used ``type=gha``, which GitHub scopes to
     the ref that wrote it -- no tag can restore another tag's cache -- so every
     release rebuilt ~50 layers cold and published ~900 MB of new layers for a
     ~1 MB change to box source.
  2. ``box.Dockerfile`` must put nothing but box source below the first source
     COPY. Every layer below that line is rebuilt, with a new digest, whenever
     box code changes, and box code changes in every release.
  3. The base image must be pinned by digest. A floating tag moves every layer
     above it whenever Docker Hub rebuilds it; dependabot moves the pin instead.

These are scans of the files as shipped, plus runs of the workflow's own
tag-resolution script. The property under test is what the build is told to
do. The only direct check is a cold image build, which does not belong in a
unit suite.
"""

import os
import pathlib
import re
import subprocess

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[3]
DOCKERFILE = ROOT / "box" / "lager" / "docker" / "box.Dockerfile"
WORKFLOW = ROOT / ".github" / "workflows" / "box-image-publish.yml"
DEPENDABOT = ROOT / ".github" / "dependabot.yml"

# The first instruction that copies box source into the image.
FIRST_SOURCE_COPY = "COPY *.py /app/lager/lager/"
# A fragment unique to the import smoke check: the one RUN allowed below the
# source, because it imports that source.
SMOKE_CHECK_MARK = "api_reference introspection failed"
# Instructions that change image metadata rather than adding file content.
METADATA = {
    "ARG", "CMD", "ENTRYPOINT", "ENV", "EXPOSE", "HEALTHCHECK", "LABEL",
    "ONBUILD", "SHELL", "STOPSIGNAL", "USER", "VOLUME", "WORKDIR",
}

SHA = "0123456789abcdef0123456789abcdef01234567"


# --- Dockerfile ---------------------------------------------------------------

def _instructions(text):
    """Split a Dockerfile into ``(KEYWORD, text)`` pairs, joining continuations.

    Comments and blank lines between instructions are dropped, which also drops
    the ``# syntax=`` directive.
    """
    found, parts = [], []
    for raw in text.splitlines():
        line = raw.rstrip()
        if not parts and (not line.strip() or line.lstrip().startswith("#")):
            continue
        continued = line.endswith("\\")
        parts.append((line[:-1] if continued else line).strip())
        if not continued:
            joined = " ".join(p for p in parts if p)
            found.append((joined.split(None, 1)[0].upper(), joined))
            parts = []
    return found


def _copy_sources(body):
    """The source arguments of a COPY instruction, flags removed."""
    args = [a for a in body.split()[1:] if not a.startswith("--")]
    return args[:-1]


def _below_source_offenders(text):
    """Instructions below the first source COPY that are not box source."""
    instructions = _instructions(text)
    texts = [body for _, body in instructions]
    # A renamed anchor must fail loudly rather than scan nothing.
    assert FIRST_SOURCE_COPY in texts, (
        f"{FIRST_SOURCE_COPY!r} is not in the Dockerfile; update "
        "FIRST_SOURCE_COPY if the source COPY block changed"
    )
    offenders = []
    for keyword, body in instructions[texts.index(FIRST_SOURCE_COPY):]:
        if keyword in METADATA:
            continue
        if keyword == "COPY" and not any(
            src == "docker" or src.startswith("docker/")
            for src in _copy_sources(body)
        ):
            continue
        if keyword == "RUN" and SMOKE_CHECK_MARK in body:
            continue
        offenders.append(body)
    return offenders


class TestDockerfileLayout:
    def test_only_box_source_sits_below_the_first_source_copy(self):
        offenders = _below_source_offenders(DOCKERFILE.read_text())
        assert offenders == [], (
            "box.Dockerfile has steps below the first source COPY that do not "
            "use box source. Each one is rebuilt in every release and gets a "
            "new digest, which every box downloads again. Move them above "
            f"{FIRST_SOURCE_COPY!r}:\n" + "\n".join(offenders)
        )

    def test_the_scan_catches_a_static_step_below_the_source(self):
        # A checker that silently matches nothing passes forever.
        synthetic = "\n".join([
            "# syntax=docker/dockerfile:1.4",
            "FROM python:3.12-slim-bookworm@sha256:" + "0" * 64,
            "RUN apt-get update \\",
            "    && apt-get install -y tini",
            FIRST_SOURCE_COPY,
            "COPY debug /app/lager/lager/debug",
            'RUN cd /app/lager && python3 -c "\\',
            f"print('{SMOKE_CHECK_MARK}')\"",
            "RUN usermod -aG dialout www-data",
            "COPY docker/start-services.sh /usr/local/bin/start-services.sh",
            'ENTRYPOINT ["/usr/bin/tini", "--"]',
            "USER www-data",
        ])
        assert _below_source_offenders(synthetic) == [
            "RUN usermod -aG dialout www-data",
            "COPY docker/start-services.sh /usr/local/bin/start-services.sh",
        ]

    def test_the_moved_steps_are_still_in_the_image(self):
        # Moving the static steps above the source must not drop any of them.
        texts = [body for _, body in _instructions(DOCKERFILE.read_text())]
        above = " ".join(texts[:texts.index(FIRST_SOURCE_COPY)])
        for group in ("dialout", "plugdev", "bluetooth", "lpadmin", "video"):
            assert re.search(
                rf"usermod -aG \S*\b{group}\b\S* www-data", above), group
        for path in ("/var/www", "/etc/lager"):
            assert re.search(rf"mkdir -p [^&;]*{re.escape(path)}", above), path
        assert "chown -R www-data:www-data /var/www /etc/lager" in above
        assert ("COPY docker/start-services.sh "
                "/usr/local/bin/start-services.sh") in above
        assert "chmod +x /usr/local/bin/start-services.sh" in above
        assert ("COPY docker/web_oscilloscope.html "
                "/app/lager/web_oscilloscope.html") in above

    def test_base_image_is_pinned_by_digest(self):
        keyword, body = _instructions(DOCKERFILE.read_text())[0]
        assert keyword == "FROM"
        assert re.fullmatch(r"FROM \S+:\S+@sha256:[0-9a-f]{64}", body), (
            f"base image is not pinned by digest: {body!r}. A floating tag "
            "moves every layer above it whenever the upstream image is rebuilt."
        )


# --- Publish workflow ---------------------------------------------------------

def _workflow():
    return yaml.safe_load(WORKFLOW.read_text())


def _triggers(workflow):
    # YAML 1.1 reads a bare `on:` key as the boolean True.
    return workflow.get("on", workflow.get(True))


def _step(*, name=None, uses_prefix=None):
    for step in _workflow()["jobs"]["publish-box-image"]["steps"]:
        if name is not None and step.get("name") == name:
            return step
        if uses_prefix is not None and str(step.get("uses", "")).startswith(uses_prefix):
            return step
    raise AssertionError(f"no step with name={name!r} uses={uses_prefix!r}")


class TestPublishWorkflowCache:
    def test_layer_cache_is_in_the_registry(self):
        build = _step(uses_prefix="docker/build-push-action@")["with"]
        ref = "type=registry,ref=${{ env.IMAGE_NAME }}:buildcache"
        assert build["cache-from"] == ref
        assert build["cache-to"].startswith(ref + ",")
        assert "mode=max" in build["cache-to"].split(",")

    def test_no_actions_cache_anywhere(self):
        # type=gha is scoped to the tag that wrote it, so no release can read
        # the previous release's cache and every layer gets a new digest.
        # Scans the parsed workflow rather than the file text, because the
        # header comment names the old cache to explain why it is gone.
        assert "type=gha" not in repr(_workflow())

    def test_one_publisher_at_a_time(self):
        concurrency = _workflow()["concurrency"]
        assert concurrency["group"] == "box-image-publish"
        assert concurrency["cancel-in-progress"] is False

    def test_dispatch_inputs_reach_the_build(self):
        inputs = _triggers(_workflow())["workflow_dispatch"]["inputs"]
        assert inputs["tag"]["required"] is False
        for name in ("cache_only", "no_cache"):
            assert inputs[name]["type"] == "boolean"
            assert inputs[name]["default"] is False
        build = _step(uses_prefix="docker/build-push-action@")["with"]
        assert build["push"] == "${{ steps.tag.outputs.push }}"
        assert build["no-cache"] == "${{ inputs.no_cache == true }}"


def _resolve(tmp_path, **env):
    """Run the workflow's own `Resolve tag` script; return (rc, outputs)."""
    script = _step(name="Resolve tag")["run"]
    github_output = tmp_path / "github_output"
    github_output.write_text("")
    run_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "GITHUB_OUTPUT": str(github_output),
        "GITHUB_SHA": SHA,
        "GITHUB_REF_NAME": "",
        "EVENT_NAME": "push",
        "INPUT_TAG": "",
        "CACHE_ONLY": "",
    }
    run_env.update(env)
    proc = subprocess.run(
        ["bash", "-c", script], env=run_env, capture_output=True, text=True,
    )
    outputs = dict(
        line.split("=", 1)
        for line in github_output.read_text().splitlines()
        if "=" in line
    )
    return proc.returncode, outputs


class TestResolveTagScript:
    def test_tag_push_publishes_the_tag(self, tmp_path):
        rc, out = _resolve(tmp_path, EVENT_NAME="push", GITHUB_REF_NAME="v1.2.3")
        assert rc == 0
        assert out == {"ref": "v1.2.3", "tag": "v1.2.3",
                       "version": "1.2.3", "push": "true"}

    def test_a_tag_push_always_publishes(self, tmp_path):
        # `inputs` is empty on a push; a stray value must not skip the push.
        rc, out = _resolve(tmp_path, EVENT_NAME="push",
                           GITHUB_REF_NAME="v1.2.3", CACHE_ONLY="true")
        assert rc == 0
        assert out["push"] == "true"

    def test_dispatch_rebuilds_a_named_tag(self, tmp_path):
        rc, out = _resolve(tmp_path, EVENT_NAME="workflow_dispatch",
                           INPUT_TAG="v0.46.2", CACHE_ONLY="false")
        assert rc == 0
        assert out["ref"] == "v0.46.2"
        assert out["push"] == "true"

    def test_cache_only_builds_the_dispatched_commit_and_pushes_nothing(self, tmp_path):
        rc, out = _resolve(tmp_path, EVENT_NAME="workflow_dispatch",
                           CACHE_ONLY="true")
        assert rc == 0
        assert out["push"] == "false"
        assert out["ref"] == SHA
        assert out["tag"] == f"cache-only-{SHA[:12]}"

    def test_cache_only_with_a_tag_builds_that_tag_without_pushing(self, tmp_path):
        rc, out = _resolve(tmp_path, EVENT_NAME="workflow_dispatch",
                           INPUT_TAG="v0.46.2", CACHE_ONLY="true")
        assert rc == 0
        assert out["ref"] == "v0.46.2"
        assert out["push"] == "false"

    @pytest.mark.parametrize("tag", ["", "main", "0.46.2"])
    def test_a_publishing_dispatch_refuses_anything_but_a_tag(self, tmp_path, tag):
        rc, out = _resolve(tmp_path, EVENT_NAME="workflow_dispatch",
                           INPUT_TAG=tag, CACHE_ONLY="false")
        assert rc != 0
        assert out == {}

    def test_a_crafted_tag_is_data_not_code(self, tmp_path):
        marker = tmp_path / "executed"
        _resolve(tmp_path, EVENT_NAME="workflow_dispatch",
                 INPUT_TAG=f"v1$(touch {marker})", CACHE_ONLY="false")
        assert not marker.exists()


# --- Dependabot ---------------------------------------------------------------

class TestDependabotMovesThePin:
    def test_docker_ecosystem_covers_the_box_dockerfile(self):
        updates = yaml.safe_load(DEPENDABOT.read_text())["updates"]
        directories = {
            u["directory"] for u in updates if u["package-ecosystem"] == "docker"
        }
        assert "/box/lager/docker" in directories
        assert DOCKERFILE.parent == ROOT / "box" / "lager" / "docker"
