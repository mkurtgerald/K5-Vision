# Analytics Integration Framework

K5 Vision treats analytics as isolated capabilities that integrate through a versioned platform contract. Analytics Lab may evolve independently; K5 core should not import model code or ML dependency trees directly.

## Ownership boundary

K5 core owns:

- camera/source identity and authenticated stream access;
- frame/stream taps exposed to approved analytics workers;
- analytic registration, enable/disable state, health, and resource policy;
- normalized event ingestion;
- event correlation, rules/Virtual Guard workflows, alerting, and operator UX;
- evidence bookmarking/retention and chain-of-custody controls;
- audit logging and authorization.

An analytic worker owns:

- model/runtime dependencies;
- inference and analytic-specific preprocessing/postprocessing;
- analytic-specific configuration and thresholds;
- emitting events that conform to `k5.analytics/v1`.

Analytics must not directly own camera credentials, K5 databases, evidence retention, user authorization, or alert delivery.

## Integration contract

Every module supplies an `AnalyticManifest` containing a stable analytic ID, semantic version, supported event types, contract version, model hash, source revision, license provenance, and resource requirements.

Workers emit `AnalyticEvent` objects. Events include source identity, time, confidence, optional normalized bounding box, track/zone/correlation identifiers, extensible attributes, and K5-managed evidence references.

The first contract version is `k5.analytics/v1`. Breaking changes require a new contract version; compatible module upgrades require an explicit upgrade operation rather than silently replacing a registered version.

## Runtime isolation

The K5 service layer depends on the `AnalyticRuntime` protocol, not model packages. Concrete transports may run analytics as a subprocess, container, local HTTP service, Unix socket, gRPC service, GPU worker, or remote inference service.

A worker failure must degrade or disable that analytic without interrupting live video, recording, playback, or unrelated analytics.

## Lifecycle

1. `staged` — package discovered and manifest accepted.
2. `validated` — compatibility, provenance, model hash, event schema, resource budget, and analytic acceptance tests pass.
3. `enabled` — analytic receives approved source taps and its events enter K5's event pipeline.
4. `degraded` — worker is unhealthy or exceeding policy; K5 preserves core VMS behavior and surfaces health state.
5. `disabled` — no inference is performed and no source tap is granted.

## Analytics Lab handoff

A module is ready to enter K5 when it can provide:

- an `AnalyticManifest` compatible with the current K5 analytics API;
- a reproducible build/runtime package;
- source revision and model checksum;
- license/provenance records for donor code, models, and datasets;
- deterministic contract tests with representative positive/negative fixtures;
- documented configuration fields and confidence defaults;
- measured CPU/GPU/VRAM requirements;
- normalized event output only—no direct K5 database or credential access.

The K5 adapter for a module should remain thin: runtime launch/connect, health translation, configuration translation, source-tap negotiation, and event normalization.

## Alpha scope

The contract, registry, lifecycle, and runtime boundary belong in the platform now. Model-specific integration, GPU scheduling, frame-sharing transport, package signing, marketplace/distribution, and advanced policy orchestration can be layered on after the core VMS gates are stable. This keeps the architecture ready for Analytics Lab without making individual analytics a prerequisite for K5 alpha.
