# Lager Documentation

Documentation for the Lager platform, built with [Mintlify](https://mintlify.com/)
and published to [docs.lagerdata.com](https://docs.lagerdata.com).

## Doc Structure

The documentation is organized into tabs, defined in `docs.json`:

1. **Overview** - Getting started guides, architecture, troubleshooting, glossary
2. **CLI Reference** - All `lager` CLI commands (box management, configuration,
   power, measurement, I/O and communication, development, utilities)
3. **Python API** - On-box Python API reference (`from lager import Net, NetType`)
4. **Rust API** - On-box Rust API reference (net types, testing, debug and UART, auth)
5. **AI Agents (MCP)** - Model Context Protocol server and DUT context
6. **Supported Instruments** - The authoritative hardware compatibility list
7. **Release Notes** - Version history

Source files live in `source/` as `.mdx` (Markdown + JSX). Page paths in
`docs.json` include the `source/` prefix, which is why published URLs look like
`docs.lagerdata.com/source/reference/cli/overview`.

## Development

The CLI is published as [`mint`](https://www.npmjs.com/package/mint) (it was renamed
from `mintlify`). You do not need to install it globally — `npx` fetches it:

```bash
cd docs
npx mint@latest dev
```

Before opening a pull request, run the two checks CI should agree with:

```bash
npx mint@latest validate       # strict build; fails on MDX parse errors
npx mint@latest broken-links   # fails on dangling internal links
```

## Adding a Page

1. Create the `.mdx` file under the appropriate `source/` subdirectory.
2. Add its path (including the `source/` prefix) to the right group in
   `docs.json` — a page not listed in the navigation will not be published.
3. Preview with `mintlify dev` before opening a pull request.

## Troubleshooting

- **A page loads as a 404** - make sure you are running from the `docs/` folder
  (where `docs.json` is), and that the page is listed in `docs.json`.
- **`Unknown command: build`** - the CLI no longer has a `build` subcommand. Use
  `npx mint@latest validate` to check a build, or `npx mint@latest export` to
  produce a static site.
