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

Install the [Mintlify CLI](https://www.npmjs.com/package/mintlify):

```bash
npm i -g mintlify
```

Preview the docs locally, from the `docs/` directory where `docs.json` lives:

```bash
cd docs
mintlify dev
```

## Adding a Page

1. Create the `.mdx` file under the appropriate `source/` subdirectory.
2. Add its path (including the `source/` prefix) to the right group in
   `docs.json` — a page not listed in the navigation will not be published.
3. Preview with `mintlify dev` before opening a pull request.

## Troubleshooting

- **`mintlify dev` is not running** - run `mintlify install` to reinstall
  dependencies.
- **A page loads as a 404** - make sure you are running from the `docs/` folder
  (where `docs.json` is), and that the page is listed in `docs.json`.
