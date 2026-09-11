# Release Process for lager-cli

This document is for **maintainers** who publish releases of the `lager-cli` package to PyPI.

To contribute code, see [CONTRIBUTING.md](CONTRIBUTING.md).

## Prerequisites

- Python 3.10+
- [`twine`](https://twine.readthedocs.io/) (`pip install twine`)
- A [PyPI API token](https://pypi.org/help/#apitoken) with upload access to the `lager-cli` project
- Push access to `lagerdata/lager` on GitHub, with your `origin` remote pointing at it

## Pre-release Checklist

Before you start a release, confirm each item:

- [ ] Every pull request for the release is merged to `main`, and CI on `main` is green.
- [ ] Tests pass against the target hardware boxes.
- [ ] `CHANGELOG.md` has an entry under `## [Unreleased]` for every user-facing change.
- [ ] `## [Unreleased]` holds each `###` heading at most once. Two branches that each add a
      `### Fixed` block both merge cleanly. Merge the blocks in Keep a Changelog order: Added,
      Changed, Deprecated, Removed, Fixed, Security. Keep every bullet.
- [ ] No open security issue blocks the release.

This command counts the `###` headings under `## [Unreleased]`:

```bash
awk '/^## \[Unreleased\]/{f=1;next} /^## \[/{f=0} f' CHANGELOG.md | grep '^### ' | sort | uniq -c
```

## Release Steps

Throughout this guide, replace `X.Y.Z` with the version number, for example `0.47.1`.

### 1. Create a Release Branch and Update the Version

Create the branch from the latest `main`:

```bash
git fetch origin
git switch -c release/vX.Y.Z origin/main
```

Then set the version in `cli/__init__.py`:

```python
__version__ = 'X.Y.Z'
```

### 2. Update CHANGELOG.md

Keep the `## [Unreleased]` heading and the comment below it. Add the version heading directly
under that comment, above the entries that ship in this release:

```markdown
## [Unreleased]

<!-- Keep this heading. ... -->

## [X.Y.Z] - YYYY-MM-DD
```

A branch that merges after the release files its entry under `[Unreleased]`. Without that
heading, the entry lands inside the released section, and no merge conflict catches it.

**Rollover.** At the first release of a new ten-minor block, such as `0.50.0`, move the previous
block into `docs/changelog/`. For `0.50.0`, that block is `0.40.0` through `0.49.x`. Move the text
unchanged, and link the new file from the header of `CHANGELOG.md`. GitHub stops rendering a
Markdown file at about 512 KB, so `tools/check_docs.py` fails when `CHANGELOG.md` grows past
400 KB. The same check reads `docs/changelog/`, so every archived version still needs its
release-notes page.

### 3. Write the Release Notes

Copy `docs/source/release-notes/_template.mdx` to `docs/source/release-notes/vX.Y.Z.mdx`. Fill in
the sections that have entries, and delete the rest. `tools/check_ste.py` does not check release
notes, so follow the style rules in the template's comment by hand.

### 4. Update Navigation

In `docs/docs.json`, under the **Release Notes** tab, add the new page at the top of the group for
its version range:

```json
{
  "group": "0.40 and later",
  "pages": [
    "source/release-notes/vX.Y.Z",
    "source/release-notes/v0.47.0",
    ...
  ]
}
```

At the first release of a new ten-minor block, rename `0.40 and later` to `0.40 – 0.49`, and add a
new `0.50 and later` group above it.

Then run the docs check. It fails unless the CHANGELOG heading, the release-notes page, and the
navigation entry all exist:

```bash
python tools/check_docs.py
```

### 5. Open a Pull Request Against `main`

Commit the four files, push the branch to `origin` by name, and open a pull request:

```bash
git add cli/__init__.py CHANGELOG.md docs/source/release-notes/vX.Y.Z.mdx docs/docs.json
git commit -m "vX.Y.Z"
git push -u origin release/vX.Y.Z
gh pr create --repo lagerdata/lager --base main --head release/vX.Y.Z --title "vX.Y.Z"
```

Name the branch in `git push`. A branch that you create from `origin/main` tracks `main` until its
first push.

The `main` branch accepts changes only through a pull request. After a code owner approves, merge
with **Rebase and merge**. The branch ruleset allows no other merge method.

### 6. Tag the Release

After the merge, create an annotated tag on the release commit and push it. Tag `origin/main`
directly, so the state of your local checkout does not matter:

```bash
git fetch origin
git tag -a vX.Y.Z origin/main -m "vX.Y.Z"
git push origin vX.Y.Z
```

Before you tag, confirm that `origin/main` is the release commit. If another pull request merged
after yours, tag the release commit by its SHA instead.

### 7. Wait for Tag Validation, Then Download the Artifact

Pushing the tag triggers two workflows in parallel:

- **Release: Validate Tag** (`.github/workflows/release-validation.yml`) works from
  the tagged commit and runs these steps:
  - builds the sdist and the wheel;
  - runs `twine check`;
  - installs each one into a clean venv;
  - asserts that the installed `lager --version` equals the tag;
  - import-walks the installed package against `tools/packaging_import_baseline.txt`;
  - uploads the result as a workflow artifact named `dist-vX.Y.Z`, kept 90 days.
- **Release: Publish Box Image** (`.github/workflows/box-image-publish.yml`) builds
  `box/lager/docker/box.Dockerfile`. It pushes
  `ghcr.io/lagerdata/lager-box:vX.Y.Z` (and `:X.Y.Z`), labeled with the tag and the
  commit it was built from. `lager update --pull --version vX.Y.Z` fetches that
  image by digest instead of building on the box; without `--pull` (the default
  while this soaks) nothing consumes it. A box can pull the package anonymously
  only when the package is **public**. Set that once in the GitHub UI after the
  first successful publish (Packages → lager-box → Package settings).

  The `org.opencontainers.image.version` label is what the client checks before
  deploying a pulled image. If a change to this workflow stops the label matching
  the tag, every box silently falls back to building. That is slower, but it is
  never wrong.

Wait for Validate Tag to go green, then download the artifact:

```bash
gh run list --repo lagerdata/lager --workflow release-validation.yml --limit 1
gh run download <run-id> --repo lagerdata/lager --name dist-vX.Y.Z --dir dist
```

Do **not** rebuild locally. The artifact is the set of bytes the validation proved; a local
rebuild is a different, unproven build. (If the workflow is red, the tag has a real problem
-- fix it before anything reaches PyPI.)

A red box-image publish does **not** block the PyPI upload, because no box depends
on that image yet. Fix it before you cut the next release.

### 8. Upload to PyPI

Upload the downloaded artifact:

```bash
twine upload dist/*
```

When prompted, use `__token__` as the username and your PyPI API token as the password.

### 9. Create the GitHub Release

The GitHub Release body is the release's section of `CHANGELOG.md`, without its heading:

```bash
awk '/^## \[X.Y.Z\]/{f=1;next} /^## \[/{f=0} f' CHANGELOG.md > release-notes.md
gh release create vX.Y.Z --repo lagerdata/lager --title "vX.Y.Z" --notes-file release-notes.md
```

> **Pinning:** Boxes pin to a release via its **tag** — `lager update --version vX.Y.Z`. The CLI also accepts the bare form `X.Y.Z` and resolves it to the `vX.Y.Z` tag. Do **not** create a per-version branch; tags are the single source of truth for pinned versions.

## Post-release

Confirm that the release is live:

```bash
pip install lager-cli==X.Y.Z
lager --version
```

## Releasing a Commit That Is Not the Head of `main`

To exclude work that is already on `main`, create the release branch from the last commit to
ship instead of from `origin/main`. `release-validation.yml` compares the built version with the
tag, so the tagged commit must carry the new version. Put the version bump, the CHANGELOG
heading, and the release notes on that branch, and tag the branch's commit. After you publish,
open a second pull request that brings the same three changes to `main`.

## Versioning Policy

This project follows [Semantic Versioning](https://semver.org/):

- **MAJOR** (`X`): Breaking changes to CLI commands or Python API
- **MINOR** (`Y`): New features, new device/instrument support
- **PATCH** (`Z`): Bug fixes, documentation, minor improvements

> **Note:** Each release is a **tag** (`vX.Y.Z`). Older releases also had a matching `X.Y.Z` branch for box pinning. Those branches are retired: `lager update` and `lager install` resolve an `X.Y.Z` pin to the `vX.Y.Z` tag.

## Troubleshooting

**`python -m build` fails:** Install `build` (`pip install build`), and run the build from the
`cli/` directory. Releases use the CI artifact from step 7, so you need a local build only to
debug a problem.

**`twine check` fails on an artifact that CI passed:** Your local `packaging` library is too old
to read the wheel's metadata. Run `twine` from a fresh virtual environment with current versions
of `twine` and `packaging`.

**`twine upload` fails:** Check that your PyPI API token is valid, and that the version is not
already on PyPI.

**Validate Tag is red:** The tagged commit has a real problem. Do not upload it. While the version
is not yet on PyPI, delete the tag, fix the problem on `main`, and tag again:

```bash
git tag -d vX.Y.Z
git push origin :refs/tags/vX.Y.Z
```
