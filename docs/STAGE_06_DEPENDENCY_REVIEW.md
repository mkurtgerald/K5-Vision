# Stage 06 Dependency Review

Status: **APPROVED FOR THE CURRENT STAGE 06 LIVE-VIEW LIFECYCLE SURFACE.**

The current Stage 06 slice introduces no new third-party runtime, codec, media, UI, storage, or analytics dependency. The K5-owned live-view lease boundary is implemented with Python standard-library concurrency/UUID primitives, existing Pydantic models, and the already accepted Stage-04/05 `MediaSession` boundary.

## Exercised third-party surface

The underlying media session continues to use the previously reviewed surface only:

- GStreamer `1.28.7`
- `rtspsrc` from `gst-plugins-good`, effective license `LGPL`
- `queue` from core GStreamer, effective license `LGPL`
- `fakesink` from core GStreamer, effective license `LGPL`
- psutil `7.2.2`, BSD-3-Clause, for bounded process-tree cleanup
- Pydantic, already present in the project dependency set, for source-free contract validation

The governing media/runtime review remains `docs/STAGE_04_DEPENDENCY_REVIEW.md`, with upstream integrity and licensing references in `docs/STAGE_03_GSTREAMER_REVIEW.md`, Stage-05 continuity recorded in `docs/STAGE_05_DEPENDENCY_REVIEW.md`, and distribution notices in `THIRD_PARTY_NOTICES.md`.

## Stage 06 posture

The current live-view boundary adds no decoder, depayloader, parser, codec library, transcoder, recorder, persistence layer, browser transport, analytics runtime, or UI toolkit. It only controls bounded ephemeral consumer leases over one accepted media session, with deterministic sharing, final-consumer stop, timeout handling, recovery, cleanup, and source-free observable state.

No camera media, frames, clips, RTSP URIs, camera addresses, credentials, or raw runner identity are part of the retained Stage-06 contract or test evidence.

Any Stage-06 change that begins delivering decoded/encoded payloads to a consumer, adds GStreamer elements beyond the reviewed surface, changes dependency versions, adds a network delivery protocol, or changes the distribution model requires this review to be updated before acceptance.

This is an engineering dependency review, not legal advice.
