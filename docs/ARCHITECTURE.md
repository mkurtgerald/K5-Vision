# Architecture Notes

## Intent

K5 Vision uses project-owned contracts and replaceable implementation boundaries so external integrations and high-throughput components can evolve without forcing changes through the rest of the system.

## Logical boundaries

### 1. Orchestration layer

Responsibilities:
- canonical state and configuration
- API orchestration
- health aggregation
- jobs and command dispatch
- stable external contracts

The orchestration layer should remain lightweight and should not become the sustained high-throughput data path.

### 2. Adapter layer

Implementation-specific behavior remains behind canonical project interfaces. External-library or vendor-specific objects must not leak into higher layers.

Typical responsibilities:
- discovery/probing
- authentication/session handling
- capability normalization
- profile/connection metadata
- implementation-specific configuration

### 3. Transport/worker layer

High-throughput work belongs behind a replaceable worker boundary. Runtime selection is benchmark-driven and must account for reconnect behavior, latency, resource use, packaging, isolation, and distribution suitability.

### 4. Event/state layer

Normalized state changes and events remain source-attributed, timestamped, and observable. Higher layers consume project-owned event contracts rather than bypassing the platform.

### 5. Optional external-processing boundary

Optional external processing is implemented behind replaceable provider/runtime interfaces. Its availability must not become a hidden dependency of baseline platform operation.

Externally sourced artifacts, runtimes, SDKs, datasets, binaries, or hosted services must pass the project dependency/provenance policy before adoption.

### 6. Persistence

Durable stores are introduced behind repository interfaces. Different data classes may use different storage technologies when justified by measured requirements.

## Non-negotiable boundaries

1. Third-party implementation types do not leak into project-wide contracts.
2. The orchestration layer does not become the sustained high-throughput hot path.
3. Optional external processing cannot compromise baseline availability or data integrity.
4. External providers remain replaceable.
5. Every external dependency/artifact must pass project review before merge/use.
6. Secrets never enter the public repository or ordinary logs.
7. Lasting architecture decisions require an ADR before they become difficult to reverse.
8. Hardware-dependent acceptance requires physical validation.

## Current staged path

Only the active gate requires public implementation detail. Its immediate successor remains blocked and later capability detail is intentionally omitted until required for execution.
