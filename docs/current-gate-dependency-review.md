# Current Gate Dependency Review

Status: **APPROVED FOR EXACT-REVISION QUALIFICATION.**

This successor introduces no new third-party package, donor code, model, binary, external service, network surface, platform API, or customer filesystem decision. It adds a project-owned bounded source-free reusable view catalog over the accepted arbitrary-layout contract.

The catalog serializes only logical view identifiers plus already-accepted `ViewportLayout` geometry and sparse logical slots. Canonical ordering, a fixed 64-view capacity, a 262144-byte payload ceiling, strict schema/version validation, duplicate-identifier rejection, exact arbitrary/non-grid geometry preservation, and same-slot-set compatibility checks are enforced before a restored layout is returned. The gate remains storage-transport-neutral: it exposes canonical bytes/text serialization but does not select a private path or perform automatic disk/network persistence.

Retained catalog data excludes camera/source identity, credentials, RTSP/network data, payload/media data, recording identity, private paths, native handles/pointer values, runner identity, analytics, and source assignment. No new third-party license or distribution obligation is introduced. Existing dependency and provenance controls remain governing.

This is an engineering dependency review, not legal advice.
