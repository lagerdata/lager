# Security Policy

## Supported Versions

| Version         | Supported              |
| --------------- | ---------------------- |
| 0.16.x (latest) | ✅ Supported           |
| 0.15.x          | ⚠️ Security fixes only |
| < 0.15          | ❌ Not supported       |

## Reporting a Vulnerability

We take security seriously. If you discover a security vulnerability in Lager, please report it responsibly.

### How to Report

1. **Do NOT** open a public GitHub issue for security vulnerabilities
2. Email security concerns to: **hello@lagerdata.com**
3. Include as much detail as possible:
   - Description of the vulnerability
   - Steps to reproduce
   - Potential impact
   - Any suggested fixes (optional)

### What to Expect

- **Acknowledgment**: We will acknowledge receipt within 48 hours
- **Assessment**: We will assess the vulnerability and determine its severity
- **Updates**: We will keep you informed of our progress
- **Resolution**: We aim to resolve critical issues within 30 days
- **Credit**: With your permission, we will credit you in the release notes

### Scope

This security policy applies to:
- The Lager CLI (`cli/`)
- Box-side code (`box/`)
- Deployment scripts (`cli/deployment/`)

### Out of Scope

- Third-party dependencies (report to upstream maintainers)
- Issues in user-provided scripts or configurations
- Physical security of hardware

## Security Best Practices

When using Lager:

1. **Network Security**
   - Always use VPN (Tailscale recommended) for remote box access
   - Never expose boxes directly to the internet
   - Review firewall rules regularly

2. **Credentials**
   - Generate unique passwords for each box
   - Rotate Tailscale auth keys periodically
   - Use SSH key authentication instead of passwords

3. **Box Security**
   - Keep box software updated (`lager update`)
   - Review UFW firewall status (`sudo ufw status verbose`)
   - Monitor `/var/log/ufw.log` for suspicious activity

## Security Features

Lager includes several security features:

- **UFW Firewall**: Automatic firewall configuration restricts access to VPN interfaces
- **SSH Key Auth**: Passwordless SSH using key-based authentication
- **No Cloud Dependencies**: Direct connections eliminate third-party exposure
- **Read-Only Mounts**: Customer binaries are mounted read-only in containers

## Acknowledgments

We thank all security researchers who responsibly disclose vulnerabilities.
