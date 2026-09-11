# K5 Vision Architecture

## Architectural intent

K5 Vision is a vendor-neutral security platform, not a single-camera application. The architecture therefore separates device integration, control-plane state, media transport, analytics/inference, investigation, and user experience so each can scale independently.

## Logical planes

### 1. Control plane

Initial implementation: Python 3.12 + FastAPI + typed Pydantic contracts.

Responsibilities:
- Device inventory and capabilities
- Configuration and policy
- User/API orchestration
- Health/state aggregation
- Jobs and command dispatch
- Stable northbound API contracts

The control plane must not become the sustained video transport or transcoding path.

### 2. Device adapter layer

Vendor-specific behavior is isolated behind canonical K5 interfaces. ONVIF is the primary standards path for compatible cameras; vendor adapters are allowed when standards do not expose required capabilities.

Adapters eventually cover:
- Discovery
- Authentication/session handling
- Capability probing
- Profiles and stream URIs
- PTZ/events where supported
- Device configuration
- Firmware/vendor metadata

### 3. Media plane

Responsibilities:
- RTSP/other ingest
- Main/substream selection
- Live fan-out
- Recording segmentation
- Playback assembly
- Transcode only when required
- Media health metrics

The native implementation language/runtime is intentionally not locked in Sprint 001. Selection requires a benchmark covering sustained streams, reconnect behavior, memory, CPU/GPU path, packaging on Windows/Linux, and license suitability. Stable interfaces come first.

### 4. Event and telemetry plane

Normalizes time-series events from fixed cameras, body-worn cameras, drones, GPS-enabled sources, analytics, access/security sensors, and external systems. Events should remain source-attributed and timestamped so autonomous decisions can be audited.

### 5. Inference plane

Computer-vision models run as replaceable workers/services. NVIDIA acceleration is a target, not a hard dependency for basic platform operation. Model lifecycle, GPU scheduling, batching, and inference results remain separate from recording integrity.

#### Commercial biometric recognition subsystem

Face recognition is a required commercial capability, but it is downstream of the proven media/event/inference foundation and does not bypass precursor gates.

The biometric subsystem must remain modular and replaceable:
- face detection / quality / alignment
- embedding generation
- 1:1 verification and 1:N identification
- configurable decision thresholds with explicit `no_match`
- versioned model/runtime metadata on recognition results
- normalized event output into the event plane
- secure identity enrollment and biometric-template persistence

The identity database must use stable identity identifiers, support multiple enrollment samples/templates, version templates for future model migration/re-embedding, maintain provenance, support watchlist/allowlist/group membership, and provide auditable enrollment/update/delete/search workflows.

Biometric templates and enrollment images are sensitive data. They must not be written to ordinary application logs. Storage and APIs require explicit authorization boundaries, encryption in transit and at rest, configurable retention/deletion behavior, and complete auditability.

No face-recognition model, weights, SDK, vector-search dependency, or training/evaluation dataset may enter the shipping product merely because it is publicly downloadable. Each must pass the commercial dependency policy and provenance review before adoption.

Third-party hosted face-recognition services may be optional adapters, but K5 core operation and its biometric architecture must not depend on a single external provider.

### 6. Investigation / agentic intelligence

Natural-language investigation and autonomous decision support consume authorized K5 APIs and normalized events rather than bypassing the platform. Autonomous actions must be policy-bounded, observable, and auditable.

### 7. Persistence

Sprint 001 uses an in-memory registry solely to establish contracts. Durable storage will be introduced behind repository interfaces. Video/object storage, relational configuration/state, time-series/event data, and biometric identity/template data may use different stores based on measured needs and security requirements.

## Non-negotiable boundaries

1. Camera/vendor code does not leak directly into UI/business logic.
2. Python is not used as the high-bandwidth frame-copy/transcode hot path.
3. Recording continuity is not coupled to AI availability.
4. External AI providers are optional integrations, never required for core VMS operation.
5. Every dependency, model, weight file, SDK, and externally sourced dataset must pass the commercial dependency policy before merge/use.
6. Secrets never enter the public repository.
7. New architecture decisions with lasting impact require an ADR before implementation.
8. Face recognition cannot become a hidden dependency of recording, live view, or basic camera health.

## Near-term vertical slice

Device registration -> ONVIF discovery/capability probe -> RTSP URI acquisition -> media worker ingest -> health/event reporting -> live-view session contract.

That vertical slice is intentionally narrower than the full vision and is the fastest path to proving the architecture with real cameras. Face recognition and the biometric identity database remain planned downstream capabilities until their required precursors are accepted.
