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

## Threat Model

Some behavior that looks like a weakness is the product working as intended.
This section records which. Judge a report against what we claim here. It also
gives our accepted code-scanning findings a written reason.

**Running user code is the feature.** `POST /python` executes a script the
caller supplies, and the interactive breakpoint console evaluates expressions in
a paused script's namespace. Both exist to let an engineer drive hardware from
their laptop. Neither is a sandbox escape; there is no sandbox. A report that
observes code execution through them describes the product.

**Error strings are the diagnostic surface, and are not sanitized.** A box
failure is usually a hardware failure, and the message is what tells the user
which cable, instrument or target is at fault. The CLI renders it directly
(`cli/core/net_helpers.py`). We deliberately do not genericize these, and
static analysis flags them as information exposure in bulk; we accept that
trade rather than lose the diagnosis. What we do not do is return a stack
trace: tracebacks go to the box log only.

**Paths built from client-supplied names are contained where they are built.**
A net name, a probe serial, a job id, a binary name and a device-lock key each
name a file. Each arrives over the wire. Each is reduced to a safe form, and
each join under a root is checked against that root. That check is
repeated at the point of use rather than shared. See
`box/lager/util/paths.py` for why the repetition is deliberate.

Where a function instead *receives* an already-built path, the same check runs
on entry. Static analysis cannot credit that: nothing local can rebuild the
path. Those findings are accepted, not unfixed. Changing those signatures to
take a name instead pushes net-awareness into modules that correctly know
nothing about nets.

**The box is trusted-network infrastructure.** The services it exposes are
unauthenticated by design, on the assumption stated in the Security Model below.
For deployments that need authenticated access, put the gateway in front of it.

## Security Model

A Lager Box has direct physical control over instruments and targets, and by
design exposes that control over the network. Treat a box as trusted
infrastructure on a trusted network:

- **Never expose a box directly to the internet.** Reach it over a VPN
  (Tailscale is what we deploy) or a trusted LAN.
- **Use SSH key authentication**, not passwords, and give each box unique
  credentials.
- **Keep boxes updated** with `lager update` — box software and the CLI are
  version-matched.
- **Treat network reachability as the boundary, not the host firewall.**
  Deployment configures UFW, and it governs traffic to the host itself. It does
  **not** filter the ports the box's containers publish. Docker installs its own
  forwarding rules ahead of the host chain. A published service port is
  therefore reachable from anywhere that can route to the box, whatever
  `ufw status` reports. Put the box on a VPN or an isolated LAN, and rely on
  that.
- **Rotate VPN auth keys** periodically.

For deployments that need authenticated access, Lager supports placing an
authenticating gateway in front of a box; the CLI discovers it and prompts for
`lager login`.

## Acknowledgments

We thank the security researchers who responsibly disclose vulnerabilities to us.
