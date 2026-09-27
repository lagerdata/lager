# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
The license notices the published box image carries, and the manifest of what
else is in it.

The image installs software from three kinds of source. Debian packages keep
their own `copyright` files, and Python distributions are swept up by
`collect_pip_licenses.py`. The third kind -- a vendor download, a source build,
a toolchain installer -- is recorded by nothing unless
`box/lager/docker/licenses/THIRD_PARTY.md` records it.

So the list of what needs a row is not written down here: it is READ from
`box.Dockerfile`. A new `wget` with no row fails this file, and so does a bumped
version whose row still names the old one. A hand-kept list in the test would
only have moved the thing that goes stale.

The manifest records what each upstream declares. It makes no claim that a
component may be redistributed; that review is a separate, open question, and
one test here keeps the file from quietly starting to answer it.
"""
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[3]
DOCKER = REPO / "box" / "lager" / "docker"
DOCKERFILE = DOCKER / "box.Dockerfile"
LICENSES = DOCKER / "licenses"
MANIFEST = LICENSES / "THIRD_PARTY.md"
PUBLISH_WORKFLOW = REPO / ".github" / "workflows" / "box-image-publish.yml"

# Same anchor test_box_image_publish.py uses for "below this, only box source".
FIRST_SOURCE_COPY = "COPY *.py /app/lager/lager/"
UNPINNED = "unpinned (latest at build)"

# PyPI distributions that are a vendor's SDK rather than an ordinary library.
# pip's own metadata covers their declared license; the manifest still names
# them, because the review of vendor terms has to find them.
VENDOR_PIP = ("brainstem",)


def _instructions(text):
    """(KEYWORD, text) per Dockerfile instruction, continuations joined.

    A copy of the helper in test_box_image_publish.py, kept separate so that
    neither file's collection depends on the other.
    """
    out, buf = [], ""
    for raw in text.splitlines():
        stripped = raw.strip()
        if not buf and (not stripped or stripped.startswith("#")):
            continue
        if buf and stripped.startswith("#"):
            continue
        buf += (" " if buf else "") + stripped.rstrip("\\").strip()
        if not stripped.endswith("\\"):
            keyword, _, body = buf.partition(" ")
            out.append((keyword.upper(), body.strip()))
            buf = ""
    return out


def _rows(manifest_text):
    """The component rows of the manifest table."""
    rows, in_table = [], False
    for line in manifest_text.splitlines():
        if line.startswith("## "):
            in_table = line.strip() == "## Components"
            continue
        if in_table and line.startswith("|") and not re.match(r"^\|[\s:|-]+\|$", line):
            rows.append(line)
    return rows[1:] if rows else rows  # drop the header row


_VERSIONED = re.compile(r"\d+\.\d+|\d{4}-\d{2}-\d{2}|\$\{?[A-Z_]*VERSION")


def vendor_downloads(dockerfile_text):
    """What the Dockerfile fetches that is neither a plain apt nor a plain pip install.

    Returns ``[(what, needles, unpinned)]``. A manifest row must contain every
    needle; ``unpinned`` means the row must also say so.
    """
    found = []
    for keyword, body in _instructions(dockerfile_text):
        if keyword == "ENV":
            for name, value in re.findall(r"\b([A-Z_]*VERSION)=(\S+)", body):
                found.append((f"ENV {name}", [f"{name}={value}"], False))
        if keyword != "RUN":
            continue
        vcs_pip = re.findall(r"(git\+https://[^\s'\"@]+)@([0-9a-f]{40})", body)
        for url, sha in vcs_pip:
            found.append((f"pip install from {url}", [url, sha], False))
        for tag, url in re.findall(r"git clone\b[^&|;]*?--branch (\S+) (https://\S+)", body):
            found.append((f"source build of {url}", [url, f"--branch {tag}"], False))
        for match in re.finditer(r"(?<!git\+)https?://[^\s\"'|)>]+", body):
            url = match.group(0)
            if "$" in url:  # a variable: keep the fixed prefix, to a whole segment
                url = url[:url.index("$")]
                url = url[:url.rindex("/") + 1]
            is_clone = any(url == cloned for _tag, cloned in
                           re.findall(r"git clone\b[^&|;]*?--branch (\S+) (https://\S+)", body))
            found.append((f"download {url}", [url],
                          not is_clone and not _VERSIONED.search(match.group(0))))
        for crate in re.findall(r"cargo install (\S+)", body):
            found.append((f"cargo install {crate}", [crate], "@" not in crate))
        for channel in re.findall(r"--default-toolchain (\S+)", body):
            found.append((f"rust toolchain {channel}", [f"--default-toolchain {channel}"],
                          channel in ("stable", "beta", "nightly")))
        for package in re.findall(r"nrfutil install (\S+)", body):
            found.append((f"nrfutil install {package}", [f"nrfutil install {package}"], True))
        for name in VENDOR_PIP:
            for pin in re.findall(rf"['\"]?({re.escape(name)}==[^\s'\"]+)", body):
                found.append((f"vendor SDK from PyPI: {pin}", [pin], False))
        if re.search(r"apt-get install\b.*\bopenocd\b", body):
            found.append(("OpenOCD (named in issue #532)", ["| OpenOCD |"], False))
    return found


def _missing(downloads, rows):
    problems = []
    for what, needles, unpinned in downloads:
        matching = [row for row in rows if all(n in row for n in needles)]
        if not matching:
            problems.append(f"{what}: no row contains {needles}")
        elif unpinned and not any(UNPINNED in row for row in matching):
            problems.append(f"{what}: its row does not say {UNPINNED!r}")
    return problems


class TestEveryVendorDownloadHasARow:
    def test_the_manifest_covers_the_dockerfile(self):
        problems = _missing(vendor_downloads(DOCKERFILE.read_text()), _rows(MANIFEST.read_text()))
        assert problems == [], (
            "box.Dockerfile installs something THIRD_PARTY.md does not record, or a pin "
            "changed and its row did not:\n  " + "\n  ".join(problems))

    def test_the_scan_sees_the_downloads_that_are_there(self):
        # A detector that found nothing would pass the test above on anything.
        found = " ".join(what for what, _n, _u in vendor_downloads(DOCKERFILE.read_text()))
        assert len(vendor_downloads(DOCKERFILE.read_text())) >= 11
        for expected in ("nodejs.org", "labjack", "uldaq", "exodriver", "pykush", "asusrouter",
                         "rustup", "defmt-print", "nrfutil", "brainstem", "phidgets", "OpenOCD"):
            assert expected in found, expected

    @pytest.mark.parametrize("extra, names", [
        ("RUN wget -q https://example.com/tool-1.2.3.tgz && tar xf tool-1.2.3.tgz", "example.com"),
        ("RUN git clone --depth 1 --branch v9 https://example.com/x.git /tmp/x", "example.com/x.git"),
        ("RUN pip3 install 'git+https://example.com/y@" + "a" * 40 + "'", "example.com/y"),
        ("RUN cargo install some-crate@2.0.0 --locked", "some-crate@2.0.0"),
        ("RUN /opt/tools/nrfutil install another-tool", "another-tool"),
    ])
    def test_a_new_download_with_no_row_is_caught(self, extra, names):
        problems = _missing(vendor_downloads(DOCKERFILE.read_text() + "\n" + extra + "\n"),
                            _rows(MANIFEST.read_text()))
        assert any(names in problem for problem in problems), problems

    def test_a_bumped_pin_with_a_stale_row_is_caught(self):
        bumped = DOCKERFILE.read_text().replace("brainstem==2.12.5", "brainstem==9.9.9")
        assert bumped != DOCKERFILE.read_text()
        problems = _missing(vendor_downloads(bumped), _rows(MANIFEST.read_text()))
        assert any("brainstem==9.9.9" in problem for problem in problems), problems

    def test_an_unpinned_download_must_be_recorded_as_unpinned(self):
        rows = [row.replace(UNPINNED, "1.0") for row in _rows(MANIFEST.read_text())]
        problems = _missing(vendor_downloads(DOCKERFILE.read_text()), rows)
        assert any("rust toolchain stable" in p for p in problems), problems
        assert any("nrfutil" in p for p in problems), problems


class TestTheManifestClaimsNothingItHasNotChecked:
    def test_it_says_in_words_that_it_is_not_a_redistribution_review(self):
        text = " ".join(MANIFEST.read_text().split())
        assert "Nothing in this file states that a component can be redistributed" in text
        assert "#532" in text

    @pytest.mark.parametrize("component", ["LabJack LJM", "nrfutil |", "BrainStem", "Phidget22"])
    def test_a_vendors_own_terms_are_marked_not_reviewed(self, component):
        rows = [row for row in _rows(MANIFEST.read_text()) if component in row]
        assert rows, component
        for row in rows:
            assert "Not reviewed" in row, row

    def test_every_row_has_every_cell(self):
        rows = _rows(MANIFEST.read_text())
        assert len(rows) >= 12
        for row in rows:
            cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
            assert len(cells) == 6, row
            assert all(cells), row


class TestTheCopiesAreTheRealFiles:
    @pytest.mark.parametrize("copy, original", [
        (LICENSES / "LICENSE", REPO / "LICENSE"),
        (LICENSES / "NOTICE", REPO / "NOTICE"),
        # Older copies with nothing pinning them. Same text, same risk.
        (REPO / "box" / "LICENSE", REPO / "LICENSE"),
        (REPO / "cli" / "LICENSE", REPO / "LICENSE"),
    ], ids=lambda p: str(p.relative_to(REPO)))
    def test_byte_identical_to_the_repository_root(self, copy, original):
        # The build context is box/lager, so the root files cannot be COPY'd
        # and the image ships a copy. A copy that drifts ships the wrong terms.
        assert copy.read_bytes() == original.read_bytes()


class TestTheDockerfileShipsThem:
    def _index(self, instructions, keyword, fragment):
        matches = [i for i, (k, body) in enumerate(instructions)
                   if k == keyword and fragment in body]
        assert len(matches) == 1, (keyword, fragment, matches)
        return matches[0]

    def test_collector_then_static_files_after_every_pip_install_and_above_the_source(self):
        instructions = _instructions(DOCKERFILE.read_text())
        copy_script = self._index(instructions, "COPY", "docker/collect_pip_licenses.py")
        run_collector = self._index(instructions, "RUN", "collect_pip_licenses.py /usr/share/licenses/lager/pip")
        copy_static = self._index(instructions, "COPY", "docker/licenses/ /usr/share/licenses/lager/")
        first_source = [i for i, (k, body) in enumerate(instructions)
                        if f"{k} {body}" == FIRST_SOURCE_COPY]
        assert len(first_source) == 1
        last_pip = max(i for i, (k, body) in enumerate(instructions)
                       if k == "RUN" and re.search(r"\bpip3? install\b", body))
        # After every pip install, or the notices miss a package; the static
        # files after the RUN, so editing the manifest does not re-run it; and
        # all of it above the source, or every release re-downloads the layer.
        assert last_pip < copy_script < run_collector < copy_static < first_source[0]

    def test_the_collector_does_not_stay_in_the_image(self):
        instructions = _instructions(DOCKERFILE.read_text())
        body = instructions[self._index(instructions, "RUN", "collect_pip_licenses.py /usr/share")][1]
        assert "rm -f /tmp/collect_pip_licenses.py" in body

    def test_the_static_directory_holds_exactly_the_three_files(self):
        assert sorted(p.name for p in LICENSES.iterdir()) == ["LICENSE", "NOTICE", "THIRD_PARTY.md"]

    def test_no_licenses_label_is_asserted_for_the_whole_image(self):
        # org.opencontainers.image.licenses describes ALL the software in an
        # image. Whether every component here may be redistributed is still
        # under review, so neither the Dockerfile nor the publisher claims it.
        labels = [body for keyword, body in _instructions(DOCKERFILE.read_text()) if keyword == "LABEL"]
        assert not any("image.licenses" in body for body in labels)
        assert "image.licenses" not in PUBLISH_WORKFLOW.read_text()
