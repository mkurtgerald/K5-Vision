# K5 Vision Delivery Gates

K5 Vision uses strict precursor gating to prevent compounded failures and downstream rework.

## Core rule

**Nothing downstream becomes active implementation work until its precursor is complete, accepted, and green.**

A contract draft, mock, partial test pass, or locally working subset does not unlock downstream work when the current gate still has unmet acceptance criteria.

## One active critical-path gate

At any moment there is one active critical-path gate. Other future gates may exist as planning issues, but they remain blocked and must not accumulate implementation work.

For the current sequence:

1. ONVIF discovery/capability gate
2. Native media-plane benchmark/runtime selection
3. Live-view/session contract
4. Recording integrity/retention
5. Playback/timeline
6. Event/health pipeline
7. Search/retrieval
8. AI/CV integration
9. Mapping/telemetry and higher-level autonomous behavior

The sequence may be changed only by an explicit architecture decision that documents why the dependency relationship was wrong. It may not be bypassed to preserve apparent velocity.

## Gate exit requirements

A gate closes only when all of the following are true:

- Every acceptance criterion for the issue is satisfied.
- CI is green at the exact accepted commit.
- Required integration or hardware validation has actually passed.
- Known defects attributable to this layer are fixed or explicitly proven unrelated.
- Failure modes are tested, not only the happy path.
- Logs/diagnostics are sufficient to identify failures at this layer.
- New dependencies have passed commercial/license review.
- No unresolved defect is being deferred into a downstream layer that depends on the broken behavior.

## Failure handling

When a test or integration exposes a failure:

1. Stop forward feature work on the critical path.
2. Identify the earliest layer that violates its contract or acceptance criteria.
3. Reproduce the failure at that layer with the smallest practical test.
4. Fix the root cause there.
5. Add or strengthen a regression test so the same class of failure cannot silently return.
6. Re-run the current gate from its required acceptance boundary.
7. Only after the gate is green may downstream work resume.

Do not patch a downstream consumer to hide an upstream defect. Do not add retries, fallbacks, exception swallowing, special cases, or alternate code paths merely to make a later feature appear to work unless that behavior is itself part of the upstream contract.

## No compounded failures

If an upstream prerequisite is uncertain, flaky, partially implemented, or failing, dependent work is considered blocked. Building additional layers on top of it increases diagnosis cost and is prohibited on the critical path.

Examples:

- Do not build live-view behavior around an ONVIF profile model that has not passed real-camera validation.
- Do not build recording around a media worker whose reconnect/resource behavior has not passed the benchmark gate.
- Do not build playback against recording output until recording integrity and segment/index rules are accepted.
- Do not build AI workflows that depend on an event path that is not itself stable and observable.

## Efficiency definition

K5 measures efficiency by completed, stable capability—not files changed, commits, issue count, or simultaneous workstreams.

Preferred behavior:

- narrow scope;
- fast failure detection;
- small reproducible tests;
- root-cause fixes;
- regression coverage;
- one-way progression through proven gates.

Repeated break/fix cycles for the same defect class are a process failure and should trigger stronger tests or a review of the responsible abstraction before proceeding.

## Parallel work

Non-critical work may proceed only when it cannot depend on, alter, or mask the current gate—for example documentation, independent tooling, or benchmark harness preparation. It must not create implementation pressure that causes the active precursor gate to be waived.

## Definition of done

"Implemented" means code exists.

"Gate complete" means the capability is tested, integrated, observable, commercially acceptable, and proven at the required boundary.

Only **gate complete** unlocks the next critical-path layer.
