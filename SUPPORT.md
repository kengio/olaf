# Support

Thanks for using OLAF. Here is where to take each kind of question, and what to
honestly expect back.

OLAF v1.0.0 is an independent community Preview for evaluation and development;
it is not a production support offering. Its mutating DAR endpoint is officially
Preview: [Microsoft REST reference](https://learn.microsoft.com/en-us/rest/api/fabric/core/onelake-data-access-security/create-or-update-data-access-roles).

## Where to ask

**Questions, bugs, and feature requests → [GitHub issues](https://github.com/kengio/olaf/issues).**
Pick the matching template — bug report or feature request — or open a blank
issue when neither fits. Before filing, a quick pass through the existing
material often answers the question outright:

- [`docs/README.md`](docs/README.md) — the documentation index; start here.
- [`docs/runbook.md`](docs/runbook.md) — operating procedures, recovery, and
  the failure playbooks.
- [`docs/modes.md`](docs/modes.md) — per-mode behavior, guards, and the failure
  catalog (most error messages are listed there verbatim, with remedies).
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — the shape of the repo and how to work
  on it.

**Security problems → NOT the issue tracker.** A public report of a
vulnerability in an access-control tool is a disclosure. Use GitHub private
vulnerability reporting when it is enabled, as described in
[`SECURITY.md`](SECURITY.md). GitHub documents that repository owners must enable
the route first: [private reporting](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/report-privately).

## What to expect

OLAF is community-maintained with no response or remediation SLA. Clear,
sanitized reproduction steps (config shape, mode, and the relevant envelope or
log-row shape) make triage possible. Never attach real principal identifiers,
workspace or item values, customer data, tokens, or control artifacts.

What this project does not offer: paid support, private consulting channels, or
help operating your Fabric tenant beyond what OLAF itself does.
