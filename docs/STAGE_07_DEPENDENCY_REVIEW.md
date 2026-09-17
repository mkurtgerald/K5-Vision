# Stage 07 Dependency Review

Status: **APPROVED FOR THE CURRENT STAGE 07 BOUNDED RECORDING-INGEST SURFACE.**

Stage 07 adds a K5-owned recording-ingest contract and exercises the already accepted Stage-06 RTP delivery path into that contract. It introduces no new third-party Python package, codec, decoder, transcoder, muxer, container library, database, filesystem abstraction, cloud SDK, or persistent-storage dependency.

## Exercised third-party surface

The live physical path is unchanged from Stage 06:

- GStreamer `1.28.7`
- `rtspsrc` from GStreamer Good Plug-ins, effective license `LGPL`
- `capsfilter` from GStreamer core elements, effective license `LGPL`
- `queue` from GStreamer core elements, effective license `LGPL`
- `udpsink` from GStreamer Good Plug-ins, effective license `LGPL`
- psutil `7.2.2`, BSD-3-Clause, for bounded process-tree cleanup inherited from the accepted runtime path
- Pydantic, already present in the project dependency set, for source-free recording/evidence contracts

No additional GStreamer element is introduced by Stage 07. The physical workflow provisions and verifies the same reviewed GStreamer 1.28.7 runtime before qualification.

The governing baseline reviews remain `docs/STAGE_03_GSTREAMER_REVIEW.md`, `docs/STAGE_04_DEPENDENCY_REVIEW.md`, `docs/STAGE_05_DEPENDENCY_REVIEW.md`, and `docs/STAGE_06_DEPENDENCY_REVIEW.md`, with distribution notices in `THIRD_PARTY_NOTICES.md`.

## Stage 07 boundary posture

The recording-ingest boundary is intentionally storage-implementation neutral:

- validated RTP v2 packets enter through the accepted Stage-06 K5 consumer boundary;
- `BoundedRtpRecorder` serializes sink lifecycle and packet writes with explicit packet, byte, and operation-time bounds;
- sink open/write/finalize/abort failures are mapped to sanitized K5 errors rather than exposing source or payload details;
- production persistence remains behind the project-owned `RecordingSink` protocol, so storage/container selection is not silently coupled into this gate;
- the physical qualification uses a non-retaining sink that records only packet and byte counters, specifically so home-camera RTP payloads are not written to disk or retained in artifacts;
- retained Stage-07 evidence contains revision, generic execution context, fixed reviewed runtime/transport labels, counters, final state, and elapsed time only.

No camera media, frames, clips, RTP payloads, RTSP URIs, camera addresses, credentials, storage secrets, local paths, or raw runner identity are permitted in retained Stage-07 evidence.

A future introduction of a muxer/container, concrete persistent-storage implementation, database, object-store SDK, encryption-at-rest library, filesystem format, or different media/runtime dependency requires its own commercial dependency review before acceptance for that surface.

This is an engineering dependency review, not legal advice.
