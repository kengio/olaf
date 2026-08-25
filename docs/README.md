# Documentation map

A one-stop index of everything under `docs/`. Start with the "Start here" row if you're new to
the framework; the rest is reference material you'll come back to.

OLAF v1.0.0 is an independent community Preview for evaluation and development.
Read the platform and control-data boundaries before using real principal data.

## Start here

| Doc | What's in it |
|---|---|
| [platform-contract.md](platform-contract.md) | Official-source-backed boundaries: Preview status, identity/permissions, engine-specific enforcement, ETags, runtime baseline, and evidence status. |
| [control-data-security.md](control-data-security.md) | Mandatory pre-upload review, reserved control paths, DAR snapshot gate, sentinel, the optional (recorded, never enforced) per-run attestation, and the same-lakehouse limitation. |
| [glossary.md](glossary.md) | The words this framework uses in a specific sense — **grant**, scope, member, mapping, drift, out-of-band. Read this first if a doc reads oddly. |
| [fabric-import.md](fabric-import.md) | Getting the notebooks (runtime, master workflow, cookbook) into a Fabric workspace — portal, REST, and git-integration paths. |
| [config-examples.md](config-examples.md) | Worked config → mapping examples — the reference for authoring `onelake_security_config` rows. |
| [runbook.md](runbook.md) | Setup, config authoring, and day-to-day running — the operational how-to for any project, including a known-limitations catalog of platform constraints (§7). |

## Reference

| Doc | What's in it |
|---|---|
| [modes.md](modes.md) | The complete mode manual — every one of the eight modes: when to use it, what it does, its guards, its result keys (the unified envelope), plus the failure catalog. |
| [error-handling.md](error-handling.md) | The error-handling model — collect-all (validate) vs fail-fast (execute), the four-value status, native success/failure for Fabric pipelines, and the two failure channels (the authoritative log row + the compact raised envelope). |
| [data-model.md](data-model.md) | The control tables — table relationships, per-table data dictionary, and audit-query recipes. |
| [api-reference.md](api-reference.md) | The API index — how the classes fit together, headline table, and a method map into [api/](api/) (one file per class). |

## Architecture & quality

| Doc | What's in it |
|---|---|
| [architecture.md](architecture.md) | The framework's internal design — validation catalog, guardrails, and how the pieces are put together. |
| [roadmap.md](roadmap.md) | What is coming, what is deliberately not, and what each item is **waiting on** — the granular role API, rule C5's cross-grant blind spot, scheduled drift detection. |
| [testing.md](testing.md) | The testing guide — how the pytest suite is organized, how to run it, and how to add a scenario. |
| [live-smoke-test.md](live-smoke-test.md) | Optional authorized live-validation protocol. It is not release evidence unless the exact SHA/runtime/target scope and cleanup are recorded. |
| [brand-guidelines.md](brand-guidelines.md) | The Ink & Ice brand system — messaging, color/type, mascot usage rules, and how the MIT license and the project's name/artwork requests relate. |

Back to the [project README](../README.md).
