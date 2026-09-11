# Stage Hardening Standard

K5 Vision treats hardening as part of implementation, not as a later stabilization phase.

Every active precursor gate must include a hardening pass before it can close. Downstream implementation remains blocked until the current stage is both functionally accepted and hardened at its boundary.

## Core rule

**A stage that works only on the happy path is not complete.**

Each stage must prove that it behaves safely, predictably, observably, and recoverably when inputs, dependencies, timing, resources, and external systems do not behave as expected.

## Required hardening dimensions

Apply each dimension that is relevant to the active stage. If a dimension is not applicable, record why.

### 1. Input and boundary hardening

- validate externally supplied and cross-layer inputs
- reject malformed, missing, oversized, unsupported, or contradictory values explicitly
- avoid implicit coercion where it can hide faults
- keep third-party implementation objects behind project-owned boundaries
- define stable error behavior at public/internal contracts

### 2. Failure-mode hardening

Test realistic negative paths, including where applicable:
- authentication or authorization failure
- unreachable dependency or endpoint
- timeout and cancellation
- malformed or incomplete response
- unsupported capability or version
- interrupted operation
- duplicate/replayed request
- partial success
- invalid state transition
- dependency exception
- resource exhaustion or bounded-capacity condition

A failure must produce an explicit, diagnosable outcome rather than silent corruption, undefined state, uncontrolled retry, or hidden fallback.

### 3. Retry, timeout, and recovery hardening

- retries must be bounded and intentional
- timeouts must be explicit at external boundaries
- retry behavior must not amplify load or create duplicate side effects
- recovery after transient failure must be tested where recovery is expected
- permanent failures must terminate cleanly
- restart/re-entry behavior must not depend on undefined in-memory state

### 4. State and concurrency hardening

Where state or concurrency exists:
- validate legal state transitions
- reject or safely handle duplicate operations
- protect shared mutable state
- test cancellation and interruption boundaries
- prevent stale state from being treated as current truth
- verify idempotency where repeated requests are plausible

### 5. Resource hardening

Where relevant:
- bound queues, buffers, caches, retries, retained objects, and worker growth
- close/release external resources deterministically
- avoid unbounded memory or thread/task growth
- test repeated operation for leaks or degradation
- define behavior when configured limits are reached

### 6. Security hardening

At each stage:
- do not expose secrets in source, exceptions, diagnostics, or ordinary logs
- minimize sensitive data retention
- apply least-privilege assumptions to external access
- validate trust boundaries explicitly
- do not treat external metadata as trusted merely because transport succeeded
- review new dependencies and artifacts before acceptance
- fix known exploitable behavior at the earliest responsible layer

### 7. Observability hardening

A failure must be localizable without guesswork.

Where applicable, diagnostics should make it possible to distinguish:
- configuration error
- authentication/authorization error
- transport failure
- timeout
- malformed external behavior
- unsupported capability
- internal contract violation
- resource/capacity failure

Diagnostics must not leak credentials, tokens, protected data, or unnecessary implementation detail.

### 8. Compatibility hardening

Where interfaces or external systems are involved:
- reject unsupported versions/capabilities clearly
- avoid relying on undocumented implementation quirks
- preserve project-owned contracts across adapter implementations
- test representative variations and absence of optional capabilities
- ensure compatibility fallbacks are explicit and testable rather than accidental

### 9. Dependency and supply-chain hardening

For every new or changed dependency/artifact:
- verify governing license/terms and commercial suitability
- pin or constrain versions appropriately
- record provenance when required by project policy
- avoid unnecessary dependencies
- isolate dependency-specific behavior behind project-owned interfaces where practical
- verify that dependency failure does not corrupt project state

### 10. Regression hardening

Every defect found during a stage must improve the system:
- reproduce the defect at the earliest responsible layer
- add a regression test or equivalent deterministic validation
- fix the root cause rather than mask the symptom downstream
- rerun the gate acceptance boundary

Repeated failures of the same class require strengthening the responsible abstraction, test strategy, or validation boundary before forward work resumes.

## Hardening evidence required for gate exit

Before an active stage closes, its issue/PR or associated acceptance record must show:
- normal-path acceptance
- meaningful negative-path coverage
- timeout/recovery behavior where applicable
- explicit validation of malformed/unsupported inputs where applicable
- diagnostic behavior sufficient to localize expected failures
- dependency/provenance review for new external material
- regression coverage for defects discovered during the stage
- relevant physical/integration validation
- green CI at the exact accepted revision
- no known unresolved defect being transferred to the next layer

## Hardening depth increases with maturity

Hardening is cumulative. Later stages do not replace earlier guarantees; they depend on them.

As the codebase matures, each stage should increase pressure through broader variation, longer repeated runs, higher load, dependency faults, restart/recovery testing, and stricter acceptance thresholds where those tests are meaningful.

## No hardening debt on the critical path

Critical-path hardening may not be deferred as generic technical debt merely to unlock the next stage.

If a weakness can reasonably cause downstream instability, data corruption, unsafe behavior, security exposure, or repeated integration failure, it belongs to the current gate and must be resolved before progression.
