# Stage 02 — Endpoint Compatibility

Tracking: issue #2.

## Goal

Prove that K5 can interrogate a standards-compatible physical endpoint, normalize its identity/capabilities/profiles, and hand stable connection metadata to the next stage without exposing external-library-specific objects outside the adapter boundary.

## Increment A — contracts

- canonical profile model
- canonical identity/capability model
- stable adapter protocol and normalized failure reasons
- secret-safe ephemeral credential model
- library-neutral extraction snapshot
- deterministic normalization rules

No new runtime dependency is required for Increment A.

## Increment B — integration

Before selecting an external integration dependency:
1. compare maintained candidates and direct implementation cost
2. verify license and transitive dependency suitability
3. record any lasting selection in an ADR
4. implement timeout, authentication-failure, and unreachable-state mapping
5. keep credentials out of logs and returned API objects

## Increment C — physical validation

Run the opt-in integration harness against at least one compatible physical endpoint and capture only the evidence required to prove the stage acceptance criteria.

## Exit condition

Stage 02 is complete only when mocked tests and physical integration validation pass. Contract work alone is progress, not a completion claim.
