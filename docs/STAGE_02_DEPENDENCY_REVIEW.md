# Stage 02 Dependency Review

This record covers only the dependency introduced for the active Stage 02 integration gate.

## Direct runtime component

- Component: `onvif-python==0.3.1`
- Source: `nirsimetri/onvif-python`
- License: MIT
- Use: runtime integration adapter only
- Distribution posture: permissive; retain required copyright/license notices
- Modification posture: consumed as an external package behind a project-owned adapter boundary
- Exit strategy: replaceable adapter contract; project domain objects do not depend on package-specific types

## Declared transitive components

The selected package currently declares:

- `zeep>=4.3.0` — primary license MIT; bundled/inspired portions carry permissive BSD-style terms that require notice retention.
- `requests>=2.32.0` — Apache-2.0; retain applicable license/notice obligations.
- `pyreadline3>=3.5.4` on Windows — BSD-type license; retain copyright/license text for redistribution where required.

No research-only, non-commercial, field-of-use, source-available, or reciprocal restriction was identified in this dependency set during this review.

## Engineering constraints

- Third-party objects remain inside the Stage 02 adapter.
- Credentials are supplied ephemerally and are not persisted by the adapter contract.
- XML capture/debugging is disabled by default.
- Connection metadata returned to project-owned models must not contain embedded credentials.
- Package-specific exceptions are normalized before leaving the adapter boundary.
- Physical validation remains required before Stage 02 can close.

## Release note

This is an engineering dependency review, not legal advice. Release preparation still requires a frozen dependency graph, SBOM, transitive-license scan, vulnerability scan, and third-party notices review.
