# Neural Docking Contract

Status: architecture boundary for K5 Vision.

## Decision

K5 Vision owns production authority, identity, permissions, policy, device
control, recording, evidence handling, and operator-facing execution.

Reasoning, learning, inference, correlation, simulation, and LLM subsystems are
replaceable K5 subsystems. They may run embedded, as a local service, or as a
remote service. Runtime placement must not change the application contract.

No reasoning subsystem may directly acquire production device authority merely
because it produced a recommendation or proposal.

## Stable seam

The boundary is versioned around five message families:

1. **ObservationEnvelope** -- normalized observations entering reasoning.
2. **ActionProposal** -- a requested action plus evidence, confidence, expiry,
   constraints, and requested authority.
3. **AuthorityDecision** -- K5 policy/authority decision: allow, deny, or modify.
4. **ActionReceipt** -- execution/result feedback closing the loop.
5. **CapabilityHandshake** -- supported contract versions, capabilities, runtime
   mode, and producer version.

The Python reference models live in
`src/k5vision/neural_contracts.py`.

## Invariants

- Contracts are transport-neutral. In-process calls, IPC, HTTP, gRPC, NATS, or
  another event fabric must preserve the same semantics.
- Device/vendor specifics do not belong in the neural boundary.
- Observations are normalized before they cross the boundary.
- A proposal is not permission to execute.
- Production execution requires a K5 authority decision.
- Every authorized execution produces a receipt.
- Messages carry explicit schema versions and timezone-aware timestamps.
- Unknown schema versions fail closed until an adapter is deliberately added.
- Proprietary ontology extensions, response policies, tuning, learned weights,
  customer data, and protected adapters remain outside public generic contracts.

## Compatibility rule

New capabilities may add optional fields or negotiate a new schema version.
Breaking semantic changes require a new contract version. K5 Vision must retain
an adapter or explicit incompatibility response rather than silently changing
meaning.

## Deployment model

The same contract must support:

- embedded/lightweight reasoning for small systems;
- a local offline container/service;
- a dedicated compute node or clustered service.

This preserves a single K5 integration seam while allowing compute placement to
change with product tier, site scale, hardware, or connectivity.

## Authority model

Requested authority is descriptive input to policy, not a grant. Current levels:

`observe -> recommend -> confirm -> bounded -> high -> emergency`

The K5 authority/policy layer determines what each level means for a deployment,
user, role, action class, device, site, and current system state.

## Change-control rule

Feature work on either side of this seam must not bypass these contracts.
If a new capability cannot fit this boundary, update the boundary deliberately
before coupling production code to a one-off interface.


## Autonomous operation

K5 Vision supports autonomous security operation as a normal deployment mode.
Human confirmation is not required for every action.

Autonomy modes are:

- **manual** -- operator drives execution;
- **assisted** -- the system recommends and prepares actions;
- **auto** -- pre-authorized policies may execute without per-action operator approval;
- **emergency** -- narrowly defined emergency policies may execute immediately.

Autonomous behavior is governed by policy, not by unrestricted model privilege.
A reasoning subsystem may propose an action; K5 policy may approve it
automatically when the current autonomy mode, action class, target, confidence,
site policy, user/site configuration, and current system state satisfy the
configured rule set.

An approved autonomous action receives a short-lived, single-use
`ExecutionGrant` bound to the exact proposal, action type, target, and policy
version. The execution layer consumes that grant and emits an
`ActionReceipt`.

This permits K5 Neural / Virtual Guard to function as an autonomous security
guard while keeping device authority explicit, auditable, revocable, and
bounded.

### Example

```text
Observation
   ↓
Neural / Virtual Guard
   ↓
ActionProposal
   ↓
K5 policy evaluates current mode
   ├─ manual  → request operator
   ├─ assisted → recommend
   └─ auto/emergency + policy match
               ↓
         ExecutionGrant
               ↓
          Device action
               ↓
         ActionReceipt
```

Switching to Auto mode does not bypass policy. It changes which policy-approved
actions may proceed without a human confirmation step.
