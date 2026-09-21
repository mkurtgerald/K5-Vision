# K5 Integration Foundation

This document defines the minimum infrastructure required before the next major
Neural / Virtual Guard sprint.

## Canonical flow

```text
sources -> K5 adapters -> NormalizedEvent -> event transport -> Neural/Analytics consumers
                                      |
                                      +-> policy/authority -> scoped execution
```

## Required foundation

### Canonical event

All analytics and sensor adapters normalize into `NormalizedEvent` before
entering the shared integration boundary. Vendor-specific payloads stay behind
adapters.

The event records observation, ingestion, and processing timestamps separately
for forensic integrity.

### Delivery identity

Transport delivery is explicitly separated from event identity through
`DeliveryEnvelope`.

Consumers must use `message_id`/event identity for idempotency. A retry or
replay is not a new event merely because it has a new delivery attempt.

Transport implementations may use in-process calls, IPC, HTTP, gRPC,
NATS/JetStream, or another reviewed fabric without changing event semantics.

### Contract negotiation

Services advertise supported versions and capabilities with `ContractOffer`.
K5 explicitly returns a `ContractSelection`. Unknown/incompatible versions
fail closed rather than silently changing semantics.

## Next infrastructure increments

The next implementation layer should add:

1. deterministic policy evaluation for Auto/Emergency mode;
2. single-use execution-grant consumption and replay protection;
3. receipt persistence/audit linkage;
4. an Analytics-Lab adapter mapping its normalized analytic schema into the K5
   canonical event without coupling the repositories;
5. transport implementation only after the local contract behavior is proven.

The transport is intentionally not the architecture. The contracts are.
