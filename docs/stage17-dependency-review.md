# Stage 17 Dependency Review

Status: **APPROVED FOR THE ACTIVE GATE.**

Stage 17 introduces no concrete decoder package and no new third-party runtime dependency. It does not expand the reviewed GStreamer surface.

The implementation is a project-owned playback decoder/frame-consumer contract layered over the accepted Stage-16 paced playback pump. A future decoder implementation must be injected behind the `PlaybackDecoder` protocol and will require its own provenance, license, distribution, security, and physical qualification before acceptance.

The active surface uses Python `asyncio`, standard-library protocols/dataclasses, and the project's existing Pydantic dependency. Encoded packets and decoded frame buffers are callback-scoped only; retained observability is limited to source-free counters, bounded frame dimensions, lifecycle state, and cleanup status.

Stage-17 acceptance explicitly excludes selection of a concrete decoder runtime, media rendering/display, UI expansion, search/indexing, export, retention, analytics, and new network I/O.

This is an engineering dependency review, not legal advice.
