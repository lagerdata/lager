# Lager Documentation

Documentation for the Lager platform, built with [Mintlify](https://mintlify.com/)
and published to [docs.lagerdata.com](https://docs.lagerdata.com).

## Doc Structure

The documentation is organized into tabs, defined in `docs.json`:

1. **Overview** - Getting started guides, architecture, troubleshooting, glossary
2. **CLI Reference** - All `lager` CLI commands (box management, configuration,
   power, measurement, I/O and communication, development, utilities)
3. **Python API** - On-box Python API reference (`from lager import Net, NetType`)
4. **Rust API** - Rust client reference (`lager-net` on crates.io): a page per net
   type, plus the client, errors, async, testing, and authentication
5. **AI Agents (MCP)** - Model Context Protocol server and DUT context
6. **Supported Instruments** - The authoritative hardware compatibility list
7. **Release Notes** - Version history

Source files live in `source/` as `.mdx` (Markdown + JSX). Page paths in
`docs.json` include the `source/` prefix, which is why published URLs look like
`docs.lagerdata.com/source/reference/cli/overview`.

## Development

The CLI is published as [`mint`](https://www.npmjs.com/package/mint) (it was renamed
from `mintlify`). You do not need to install it globally — `npx` fetches it, and
`package.json` pins the version so a local run matches CI:

```bash
cd docs
npm run dev
```

Before opening a pull request, run the two checks CI runs:

```bash
npm run validate        # strict build; fails on MDX parse errors
npm run broken-links    # dangling links and anchors, scoped to source/
```

`broken-links` is the one CI gates (`static-checks.yml`). `validate` is not gated,
so an MDX parse error only shows up if you run it — please do.

There is no local build step. docs.lagerdata.com is built and served by Mintlify's
own hosted platform, which deploys straight from `main` via the Mintlify GitHub app;
nothing in this repository produces the published site.

## Adding a Page

1. Create the `.mdx` file under the appropriate `source/` subdirectory.
2. Add its path (including the `source/` prefix) to the right group in
   `docs.json` — a page not listed in the navigation will not be published.
3. Preview with `npm run dev` before opening a pull request.

## Troubleshooting

- **A page loads as a 404** - make sure you are running from the `docs/` folder
  (where `docs.json` is), and that the page is listed in `docs.json`.
- **Looking for a `build` script?** There isn't one, by design - see above. `mint`
  has no `build` subcommand; `mint export` writes an `export.zip`, which is not
  what a static host expects and is not part of publishing here.
