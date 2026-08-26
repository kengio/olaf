# OLAF brand system — Ink & Ice

Ink & Ice is the single visual and verbal direction for OLAF. It balances the
clarity of snow and water with the confidence expected from access-control
software.

**Scope note — this is a request, not a license term.** The repository —
code, documentation, and the mascot artwork alike — is licensed under
[MIT](../LICENSE), and MIT's grant covers everything here without carve-outs:
anyone may use, copy, modify, and redistribute it, mascot included. Nothing
below narrows that grant or adds a condition MIT does not already impose. The
rules that follow describe the project's preferred house style and what we
ask of a fork or derivative — most centrally, that a materially modified or
rebranded distribution use its own name and artwork rather than OLAF's,
so it reads as its own project rather than implying endorsement by, or
affiliation with, this one. Treat everything below as norms we'd appreciate
you following, not restrictions the license enforces.

## Quick reference

- **Primary message:** Review every access change. Apply only what you approved.
- **Product description:** Independent community Preview tooling for reviewing Microsoft Fabric OneLake data access role changes.
- **Primary gradient:** `#7DD3FC → #1E3A8A` at `105deg`
- **Ink:** `#071A2B`
- **Snow:** `#FFFFFF`
- **Accent:** `#FB923C`, reserved for the approval lock
- **Type:** Inter / system sans; JetBrains Mono / system mono for code
- **Voice:** calm, precise, protective, practical

## Brand idea

OLAF is the vigilant reviewer at the gate. The arctic owl — whose facial disc
forms the O of OLAF inside an ice shield — makes access control approachable;
the shield, layered feathers, and closed approval lock make the operating
model explicit:

1. inspect the proposed change;
2. validate the request;
3. review the exact diff;
4. apply only the approved state;
5. preserve an audit trail.

The mascot is friendly. The system underneath is exact.

## Messaging

### Primary

> Review every access change. Apply only what you approved.

### Supporting messages

- **Preview before permission.** `plan` shows the exact additions and removals.
- **Catch risky configuration early.** Validation blocks common RLS, CLS,
  predicate, casing, and multi-role exposure mistakes before deployment.
- **One runtime, multiple deployments.** Keep deployment-specific intent in the
  Excel-authored config and runtime parameters.
- **A history you can query.** Link plan to apply and log who changed what and
  when.

### Short product description

OLAF is an independent community Preview that brings a plan → review → apply
workflow to Microsoft Fabric OneLake data access roles in one self-contained
Fabric notebook. It is not production-ready while its bulk DAR mutation
dependency remains officially Preview:
[Microsoft REST reference](https://learn.microsoft.com/en-us/rest/api/fabric/core/onelake-data-access-security/create-or-update-data-access-roles).

### Voice

| We are | We are not |
|---|---|
| Precise and evidence-led | Alarmist or absolute |
| Friendly and direct | Cute at the expense of clarity |
| Operationally realistic | “One-click secure” marketing |
| Clear about guards and limits | Vague about what is enforced |

Prefer “blocks deployment until…” over “guarantees security.” Prefer
“review the exact diff” over “effortless governance.”

## Color

| Token | Hex | Use |
|---|---|---|
| Ice 300 | `#7DD3FC` | Light gradient edge, highlights |
| Ice 500 | `#38BDF8` | Links and supporting information |
| Trust 700 | `#1E3A8A` | Dark gradient edge, strong UI |
| Ink 900 | `#071A2B` | Primary text and dark surfaces |
| Ink 800 | `#102A43` | Secondary dark surface |
| Snow | `#FFFFFF` | Owl facial disc and primary light surface |
| Frost | `#EFF8FF` | Light page background |
| Accent | `#FB923C` | Approval lock, sparing CTA accent |

Never use the orange accent as a large background. Do not communicate status
through color alone; pair status colors with text or an icon.

## Typography

- Headings: Inter or the platform system sans, weight 650–800.
- Body: Inter or system sans, weight 400–500.
- Code and operational labels: JetBrains Mono or system mono.
- Body copy: 16px minimum, line-height 1.5–1.65, maximum width 72 characters.
- Avoid novelty, handwritten, ice, or holiday fonts.

## Logo family

| Asset | Use |
|---|---|
| `olaf-logo.png` | Primary mark — avatars, docs headers, badges ≥ 64px |
| `olaf-mascot.png` | Mascot artwork — README hero, launch and community material |
| `olaf-banner-light.png` / `olaf-banner-dark.png` | Masthead banners per color scheme |
| `olaf-social-preview.png` | Repository social preview — OLAF mark only, without third-party logos or product icons |

### Clear space

Use at least one brow-height of clear space on every side. Do not place copy,
badges, or partner marks inside that area.

### Minimum size

- Primary mark: 32px minimum (the mark stays readable when scaled down).
- Full mascot illustration: 200px wide minimum.

### Motion

- Keep the primary mascot static in GitHub, documentation, and product UI.
- Do not animate checklist state, approval status, or the lock in a way that
  could imply a real runtime state.

### Do not

- recolor individual mascot parts;
- remove the shield, brow, or approval lock from the marks;
- stretch, rotate, shadow, glow, or place the mark on a busy image;
- combine OLAF artwork with a Microsoft logo, product icon, partner badge, or
  certification mark;
- use “official,” “certified,” “endorsed,” or similar wording about Microsoft.

See [Legal separation](#legal-separation) below for the identity/affiliation rule.

## Illustration and icon style

- Flat geometry, solid white snow, Ink & Ice gradient outlines.
- Rounded ends, consistent strokes, no shadows or simulated material.
- One orange approval accent per composition.
- Icons use a 24px grid, 2px stroke, round caps, and no decorative detail.
- Product diagrams use white/frost surfaces, Ink text, Ice connectors, and
  orange only for a reviewed apply/approval event.

## README usage

1. Lead with the masthead banner (`assets/brand/olaf-banner-light.png` /
   `olaf-banner-dark.png` via a `<picture>` element).
2. Follow with one concrete product sentence.
3. Show the plan → review → apply promise before implementation detail.
4. Keep the quick start within the first two screenfuls.
5. Use emoji only as section wayfinding, not in every bullet.
6. Prefer repository-relative image assets so forks and offline copies render.

## Legal separation

OLAF is used as the acronym for OneLake Access Framework. The brand must not
reference, imitate, or imply affiliation with any existing animated character
or media franchise, including a character that shares the name Olaf.

OLAF is an independent community project and is not affiliated with, endorsed
by, sponsored by, or supported by Microsoft. Microsoft, Microsoft Fabric, and
OneLake are trademarks of the Microsoft group of companies. Use Microsoft
product names only as plain-text, less-prominent compatibility references. Do
not use Microsoft logos, product icons, or partner/certification treatments.
Microsoft's [Trademark and Brand Guidelines](https://www.microsoft.com/en-us/legal/intellectualproperty/trademarks)
are the governing external reference.

The project owner's decision to retain “OneLake Access Framework” carries a
trademark/confusion risk because Microsoft's general guidelines restrict use of
Microsoft brand assets in third-party product names. The disclaimer reduces
confusion but is not permission or a trademark license. Do not describe the
name as legally cleared.

## Release checklist

- [ ] Approved Ink & Ice asset used
- [ ] Accent orange reserved for approval
- [ ] Light and dark backgrounds checked
- [ ] SVG accessible names (`role="img"`, `aria-label`, embedded `<title>`) and image alt text present
- [ ] Minimum size and clear space met
- [ ] Claims describe OLAF’s actual controls
- [ ] Community Preview status and independent-project disclaimer are prominent
- [ ] No Microsoft logo, product icon, certification mark, or endorsement wording
- [ ] README links and local asset paths pass tests
