# Stage 17 Dependency Review

Status: **APPROVED FOR THE ACTIVE GATE.**

Stage 17 introduces no new third-party runtime dependency and does not expand the reviewed GStreamer surface.

The playback decoder/frame-consumer boundary is project-owned Python built over the accepted Stage-16 paced playback pump. A concrete decoder is intentionally injected behind the project-owned `PlaybackDecoder` protocol; no FFmpeg, GStreamer decode element, codec library, rendering runtime, or UI dependency is selected or distributed by this gate.

The gate is limited to bounded packet-to-decoder handoff, bounded decoded-frame validation/delivery, explicit lifecycle and cancellation behavior, deterministic decoder cleanup, sanitized failure mapping, source-free observability, and regression coverage. Concrete decoder selection/qualification, pixel-format normalization, rendering, UI, analytics, export, and later product scope remain blocked.

Any future concrete decoder/runtime selection requires separate provenance, license/commercial-distribution, security, and physical qualification review before acceptance.

This is an engineering dependency review, not legal advice.
