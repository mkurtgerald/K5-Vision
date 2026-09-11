# Delivery Gates

K5 Vision uses strict precursor gating to prevent compounded failures and downstream rework.

## Core rule

**Nothing downstream becomes active implementation work until its precursor is complete, accepted, hardened, and green.**

A draft, mock, partial test pass, or locally working subset does not unlock downstream work while the current gate has unmet acceptance or hardening criteria.

## One active critical-path gate

At any moment there is one active critical-path gate. Future gates may exist as planning items, but they remain blocked and must not accumulate implementation work.

Current public sequence:

1. Stage 02 — endpoint compatibility
2. Stage 03 — transport/runtime qualification
3. Stage 04 — session/profile contract

Later stages are intentionally not enumerated in the public repository until they become necessary to execute.

## Mandatory stage hardening

Every active gate must complete the applicable requirements in `docs/HARDENING_STANDARD.md` before it can close.

Hardening is performed during the stage, not deferred to a later stabilization sprint. Each gate must leave its owned boundary more resistant to malformed inputs, dependency faults, timeouts, invalid state, resource pressure, unsafe recovery, hidden failures, security exposure, and regression than it was when the stage began.

Critical-path hardening debt does not move downstream.

## Gate exit requirements

A gate closes only when all of the following are true:
- every functional acceptance criterion is satisfied
- applicable hardening requirements are satisfied with evidence
- CI is green at the exact accepted revision
- required integration or physical validation has passed
- known defects attributable to this layer are fixed or proven unrelated
- meaningful normal and failure modes are tested
- malformed, unsupported, timeout, interruption, and recovery behavior are tested where relevant
- diagnostics are sufficient to localize failures without exposing secrets or protected data
- new external dependencies/artifacts have passed review
- regression coverage exists for defects found during the stage
- no unresolved defect or hardening weakness is being pushed into a dependent layer

## Failure handling

When validation exposes a failure:
1. stop forward critical-path work
2. identify the earliest layer violating its contract
3. reproduce the failure at that layer with the smallest practical test
4. fix the root cause there
5. add or strengthen regression coverage
6. harden the affected boundary against the defect class when practical
7. rerun the current gate from its required acceptance boundary
8. resume downstream work only after the gate is accepted, hardened, and green

Do not patch a downstream consumer merely to hide an upstream defect.

## No compounded failures

If an upstream prerequisite is uncertain, flaky, partial, insufficiently hardened, or failing, dependent work is blocked. Additional layers must not be built on top of known instability.

## Efficiency definition

Efficiency is measured by stable completed capability—not files changed, commits, issue count, or simultaneous workstreams.

Preferred behavior:
- narrow scope
- fast failure detection
- small reproducible tests
- root-cause fixes
- regression coverage
- deliberate hardening
- one-way progression through proven gates

Repeated break/fix cycles for the same defect class indicate that the responsible tests, abstraction, validation, or recovery behavior need strengthening before proceeding.

## Parallel work

Non-critical work may proceed only when it cannot depend on, alter, or mask the current gate. It must not create pressure to waive the active precursor or its hardening requirements.

## Definition of done

"Implemented" means code exists.

"Functionally accepted" means the intended behavior works at the required boundary.

"Hardened" means the boundary has been deliberately tested and strengthened against applicable negative, failure, recovery, security, resource, compatibility, and regression conditions.

"Gate complete" means the capability is functionally accepted, hardened, integrated, observable, commercially acceptable, and proven at its required boundary.

Only **gate complete** unlocks the next critical-path stage.
