# Stage 08 Dependency Review

Status: **APPROVED FOR THE CURRENT STAGE 08 BOUNDED LOCAL-PERSISTENCE SURFACE.**

Stage 08 introduces the first concrete implementation of the project-owned Stage-07 `RecordingSink` contract. The implementation is deliberately Python-standard-library-only and does not add a database, object-store SDK, filesystem abstraction, muxer/container library, codec, decoder, transcoder, encryption library, or other third-party persistence dependency.

## Exercised dependency surface

The live camera delivery path remains the accepted Stage-06/07 surface:

- GStreamer `1.28.7`
- `rtspsrc`, `capsfilter`, `queue`, and `udpsink` from the previously reviewed LGPL GStreamer surface
- psutil `7.2.2`, BSD-3-Clause, inherited for bounded runtime process cleanup
- Pydantic, already present in the project dependency set, for source-free evidence contracts

The concrete local sink uses only Python stdlib filesystem/runtime primitives: `pathlib`, `os`, `asyncio.to_thread`, regular-file descriptors, `fsync`, and same-filesystem `os.replace` finalization.

No new third-party package or GStreamer element is introduced by Stage 08. Existing distribution notices remain governing.

## Persistence boundary posture

- recording identifiers are constrained to a single safe filename token; separators and traversal forms are rejected before filesystem access;
- partial payload is isolated in an exclusive hidden `.part` file under the configured root;
- an existing finalized recording or active same-ID partial writer causes a sanitized conflict instead of overwrite;
- file creation requests restrictive `0600` permissions where supported by the host platform;
- writes are bounded independently at the sink and Stage-07 recorder layers;
- successful finalize flushes, `fsync`s, closes, and promotes the partial file via same-filesystem atomic replace semantics;
- abort and failure cleanup remove partial payload best-effort and never expose path details through the K5 error surface;
- Stage-08 physical qualification may write live camera RTP only inside a temporary qualification directory, which must be deleted before source-free evidence is emitted;
- retained evidence contains revision, generic execution context, fixed reviewed transport/runtime labels, counters, final sink state, cleanup confirmation, and elapsed time only.

No camera URI, camera address, credential, RTP payload, frame, clip, storage secret, unnecessary local path, or raw runner identity may be retained in Stage-08 evidence.

## Deferred dependency decisions

Stage 08 does **not** select a playable media container, database/index, retention engine, encryption-at-rest implementation, distributed storage layer, cloud object store, or playback stack. Any such dependency requires its own commercial/provenance review before acceptance.

This is an engineering dependency review, not legal advice.
