# Security Policy

## Reporting a vulnerability

Please report security issues **privately** — do not open a public issue for a
suspected vulnerability.

- Use GitHub's **[Report a vulnerability](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)**
  (Security → Advisories) on this repository, or
- email the maintainers at the address listed on the repository profile.

We aim to acknowledge reports within a few business days and will coordinate a fix
and disclosure timeline with you.

## Scope

Warden is a runtime for executing AI agents under isolation, permissioning, and
resource governance. Security-relevant areas include:

- **Tenant isolation** — one `(user_id, task_id)` run must not read or write another's
  workspace, credentials, or state.
- **Permission enforcement / HITL** — the deny-baseline, the permission chain, and the
  human-in-the-loop gates must fail closed.
- **Credential handling** — provider credentials are resolved server-side and injected
  by reference; they must never appear in events, logs, or audit output.
- **Safety guardrails** — input/output sanitization and the leak/exfil detectors.

## Handling secrets

- Never commit real credentials. `.env` is gitignored; use `.env.example` as the template.
- Provider credentials are resolved server-side; callers of the Runs API send a
  service token and a user id, never a model API key.
- Telemetry is **off by default** and must never emit credentials.
