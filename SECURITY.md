# Security Policy

## Supported Versions

Lager ships frequently from `main`. Rather than pin a table that goes stale each
release, the policy is:

| Release | Support |
| --- | --- |
| Latest minor (see [Releases](https://github.com/lagerdata/lager/releases/latest)) | Full support - bug fixes and security fixes |
| Previous minor | Security fixes only |
| Older | Not supported - please upgrade |

Upgrade the CLI with `pip install --upgrade lager-cli`, and a box with
`lager update --box <name>`.

## Reporting a Vulnerability

We take security seriously. If you discover a vulnerability in Lager, please
report it responsibly.

### How to Report

1. **Do not** open a public GitHub issue for security vulnerabilities.
2. Email **hello@lagerdata.com**.
3. Include as much detail as you can:
   - Description of the vulnerability
   - Steps to reproduce
   - Potential impact
   - Affected version (`lager --version`)
   - Any suggested fix (optional)

### What to Expect

- **Acknowledgment** — within 48 hours.
- **Assessment** — we confirm the issue and determine severity.
- **Updates** — we keep you informed of progress.
- **Resolution** — we aim to resolve critical issues within 30 days.
- **Credit** — with your permission, we credit you in the release notes.

### Scope

This policy applies to:

- The Lager CLI (`cli/`)
- Box-side code and services (`box/`)
- Deployment and provisioning scripts (`cli/deployment/`)

### Out of Scope

- Third-party dependencies — report to their upstream maintainers.
- Issues in user-provided scripts or box configurations.
- Physical security of bench hardware.
- Findings that require an attacker to already have shell access to the box.

## Security Model

A Lager box has direct physical control over instruments and targets, and by
design exposes that control over the network. Treat a box as trusted
infrastructure on a trusted network:

- **Never expose a box directly to the internet.** Reach it over a VPN
  (Tailscale is what we deploy) or a trusted LAN.
- **Use SSH key authentication**, not passwords, and give each box unique
  credentials.
- **Keep boxes updated** with `lager update` — box software and the CLI are
  version-matched.
- **Review the firewall.** Deployment configures UFW to restrict access to VPN
  interfaces; verify with `sudo ufw status verbose`.
- **Rotate VPN auth keys** periodically.

For deployments that need authenticated access, Lager supports placing an
authenticating gateway in front of a box; the CLI discovers it and prompts for
`lager login`.

## Acknowledgments

We thank the security researchers who responsibly disclose vulnerabilities to us.
