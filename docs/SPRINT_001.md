# Stage 01 — Foundation

## Objective

Create a small, executable, testable foundation with stable project-owned contracts and a working quality gate.

## In scope

- typed API/service skeleton
- canonical initial entity model
- replaceable in-memory registry boundary
- health/version endpoint
- basic create/list/get behavior
- CI lint/format/test gate
- minimum coverage gate
- architecture boundaries
- external dependency/artifact policy
- public-repository secret hygiene

## Acceptance criteria

- local development install succeeds on the supported Python version
- lint and format checks pass
- tests meet the repository coverage gate
- health/version behavior works
- canonical registration and retrieval round-trip works
- CI runs for pull requests and pushes to main
- no sustained high-throughput processing is introduced into the orchestration layer

## Explicitly deferred

- external integration specifics
- sustained transport/runtime work
- durable persistence
- authorization features
- UI
- advanced processing

Deferred scope remains intentionally outside the public roadmap until its precursor gates are ready.

## Next stage

Stage 02 proves one standards-based physical endpoint path through project-owned contracts and required integration validation.
