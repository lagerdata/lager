# Changelog fragments

Each change that a user can see adds one file here. Do not edit the
`## [Unreleased]` section of `CHANGELOG.md`: that section stays empty, and
the Static Checks job fails if it holds an entry.

Two branches that add different files cannot conflict. When every branch
edited the same section, each merge put every other open branch into conflict.

## Name

`<name>.<type>.md`

- `<name>`: lowercase letters, digits, `.`, `_` and `-`. Use the branch name
  without its prefix, for example `ftdi-usb-cache-flush`.
- `<type>`: `added`, `changed`, `deprecated`, `removed`, `fixed` or `security`.

A change with entries of two types adds two files, for example
`uart-timeout.added.md` and `uart-timeout.fixed.md`.

## Content

One or more Markdown bullets, written as they will appear in `CHANGELOG.md`.
Do not add headings. Write one bullet per change, in one to three sentences:
what changed for a user, and the command or API it affects.

```markdown
- `lager uart` stops at the `--timeout` limit when no data arrives.
```

## Check

```bash
python tools/changelog_fragments.py --check     # what CI runs
python tools/changelog_fragments.py --preview   # the section a release would write now
```

## Release

The release runs `python tools/changelog_fragments.py --assemble X.Y.Z`. It
writes `## [X.Y.Z] - <date>` below the empty Unreleased section, with the
fragments under their type headings, and deletes the fragment files.
