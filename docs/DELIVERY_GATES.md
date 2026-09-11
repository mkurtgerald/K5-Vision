# Delivery Gates

K5 Vision uses strict precursor gating to prevent compounded failures and downstream rework.

## Core rule

**Nothing downstream becomes active implementation work until its precursor is complete, accepted, and green.**

A draft, mock, partial test pass, or locally working subset does not unlock downstream work while the current gate has unmet acceptance criteria.

## One active critical-path gate

At any moment there is one active critical-path gate. Future gates may exist as planning items, but they remain blocked and must not accumulate implementation work.

Current public sequence:

1. Stage 02 — endpoint compatibility
2. Stage 03 — transport/runtime qualification
3. Stage 04 — session/profile contract

Later stages are intentionally not enumerated in the public repository until they become necessary to execute.

## Gate exit requirements

A gate closes only when all of the following are true:
- every acceptance criterion is satisfied
- CI is green at the exact accepted revision
- required integration or physical validation has passed
- known defects attributable to this layer are fixed or proven unrelated
- meaningful failure modes are tested
- diagnostics are sufficient to localize failures
- new external dependencies/artifacts have passed review
- no unresolved defect is being pushed into a dependent layer

## Failure handling

When validation exposes a failure:
1. stop forward critical-path work
2. identify the earliest layer violating its contract
3. reproduce the failure at that layer with the smallest practical test
4. fix the root cause there
5. add or strengthen regression coverage
6. rerun the current gate from its required acceptance boundary
7. resume downstream work only after the gate is green

Do not patch a downstream consumer merely to hide an upstream defect.

## No compounded failures

If an upstream prerequisite is uncertain, flaky, partial, or failing, dependent work is blocked. Additional layers must not be built on top of known instability.

## Efficiency definition

Efficiency is measured by stable completed capability—not files changed, commits, issue count, or simultaneous workstreams.

Preferred behavior:
- narrow scope
- fast failure detection
- small reproducible tests
- root-cause fixes
- regression coverage
- one-way progression through proven gates

Repeated break/fix cycles for the same defect class indicate that the responsible tests or abstraction need strengthening before proceeding.

## Parallel work

Non-critical work may proceed only when it cannot depend on, alter, or mask the current gate. It must not create pressure to waive the active precursor.

## Definition of done

"Implemented" means code exists.

"Gate complete" means the capability is tested, integrated, observable, commercially acceptable, and proven at its required boundary.

Only **gate complete** unlocks the next critical-path stage.
