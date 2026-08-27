---
name: Bug report
about: Something OLAF did that it should not have (or did not do that it should)
title: ""
labels: bug
assignees: ""
---

<!-- Found a SECURITY problem? Stop here — do not include details in a public issue. Use GitHub
     private vulnerability reporting when the repository owner has enabled it (Security →
     Advisories → Report a vulnerability); see SECURITY.md. If the form is unavailable, ask for
     the private channel to be restored without describing the vulnerability. -->

**What happened**

A clear description of the behavior you saw.

**What you expected**

What should have happened instead.

**How to reproduce**

The shortest path you know. The most useful ingredients, sanitized:

- the mode(s) you ran and in what order (e.g. `generate → plan → apply`)
- the result envelope (`status`, `message`, `error`) or the raised message, verbatim
- relevant `onelake_security_log` rows, if any (the `rejected`/`failed` ones especially)
- the shape of the config rows involved — column values matter, **real identifiers do not**

**Environment**

- OLAF version (`__version__`, printed at load): e.g. 1.1.0
- Where it ran: Fabric notebook / pipeline / the pytest suite locally
- Python version, if running the suite locally

**Sanitization check**

- [ ] No real tenant, workspace, lakehouse, group, or user names, and no objectIds, appear
      above (the repo rule — synthetic placeholders like `sg-readers` are what the docs use)
