# Security Policy

This repository holds the first-party **app bundles** for PersonalClaw. Apps are
installed into a PersonalClaw gateway through a supply-chain pipeline —
quarantine → scan → consent → install — where a `dangerous` scanner verdict is a
terminal, non-overridable refusal. The platform's security architecture lives in
the core repository:
[PersonalClaw/docs/architecture/security.md](https://github.com/PersonalClaw/PersonalClaw/blob/main/docs/architecture/security.md)
and its public threat model at
[PersonalClaw/docs/security/threat-model.md](https://github.com/PersonalClaw/PersonalClaw/blob/main/docs/security/threat-model.md).

## Reporting a vulnerability

**Report security issues privately — do not open a public issue.**

Use GitHub's private vulnerability reporting on this repo: the
[Security tab](https://github.com/PersonalClaw/PersonalClawApps/security) →
**"Report a vulnerability"**.

If the issue is in the platform itself (the scanner, the install pipeline, token
scoping, the sandbox) rather than in a specific app bundle, please report it on
the [core repository](https://github.com/PersonalClaw/PersonalClaw/security/policy)
instead — that is where the enforcing code lives.

### What to expect

The same solo-maintainer expectations as core (not contractual SLAs):

- **Acknowledgement within 7 days.**
- **A fix or remediation plan within 30 days** for confirmed issues.

## Supported versions

App bundles are versioned individually. Only the current published version of
each app receives security fixes; there are no backports to older bundle
versions. The bundles target the latest released PersonalClaw minor.

## Scope

### In scope

- **A shipped app bundle that behaves maliciously or unsafely** — exfiltrating
  data, running destructive commands, or requesting permissions inconsistent with
  its stated function.
- **A bundle crafted to evade the supply-chain scanner** — content that should
  earn a `warning` or `dangerous` verdict but scans `clean`, or that slips
  between scan and install.
- **A manifest that under-declares its permissions or dependencies** in a way that
  misleads the install-consent surface.

### Out of scope

- **The owner installing a `warning`-rated app after explicit consent.** The
  install UI surfaces the verdict and requires confirmation; proceeding anyway is
  an owner choice, not a vulnerability. (A `dangerous` verdict cannot be
  overridden — if one installs anyway, that IS in scope.)
- **Declaration-only permissions documented as such** (e.g. `network`) — see the
  core threat model's limitations section.
- **Vulnerabilities in the platform's enforcing code** — report those on the
  [core repo](https://github.com/PersonalClaw/PersonalClaw/security/policy).
- **Hardening suggestions** — file as a normal issue, not a private advisory.
