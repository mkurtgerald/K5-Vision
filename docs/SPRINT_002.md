# Sprint 002 — ONVIF Vertical Slice

Tracking: issue #2.

## Goal

Prove that K5 can interrogate an ONVIF-capable camera, normalize its identity/capabilities/media profiles, and hand stable stream metadata to the future media plane without exposing ONVIF-library-specific objects outside the adapter.

## Increment A — contracts (this branch)

- Canonical stream profile model
- Canonical identity/capability model
- Stable adapter protocol and normalized failure reasons
- Secret-safe ephemeral credential model
- Library-neutral ONVIF extraction snapshot
- Deterministic main/sub/auxiliary profile normalization

No new runtime dependency is required for Increment A.

## Increment B — network probe

Before selecting an ONVIF client dependency:
1. Compare maintained candidate libraries and direct SOAP/WS-Discovery implementation cost.
2. Verify license and transitive dependency suitability.
3. Record selection in an ADR.
4. Implement timeouts, authentication failure mapping, and unreachable-device behavior.
5. Keep credentials out of logs and returned API objects.

## Increment C — hardware validation

Run the opt-in integration harness against at least one real ONVIF camera and capture:
- Identity
- Supported services
- Media profiles
- Stream URI acquisition success (do not persist credential-bearing URIs)
- Probe duration
- Recovery behavior after a deliberate network interruption

## Exit condition

Sprint 002 is complete only when mocked tests and at least one real-camera probe pass. The contracts alone are progress, not a completion claim.
