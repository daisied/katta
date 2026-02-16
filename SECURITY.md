# Security Policy

## Supported Versions

The `main` branch is the only supported version.

## Reporting a Vulnerability

Please do not open public issues for sensitive vulnerabilities.

- Open a private GitHub Security Advisory:
  `https://github.com/daisied/katta/security/advisories/new`
- Include reproduction steps, impact, and affected configuration.

We will acknowledge reports within 72 hours and provide status updates until resolved.

## Threat Model Notes

Katta is a self-hosted agent with optional shell/tool execution. Treat it as a trusted-admin tool, not a multi-tenant hardened service.

- By design, admin users can execute commands inside the container.
- Run only in trusted environments.
- Never expose this bot to untrusted users or public networks without additional isolation.
