# Stage 05 Dependency Review

Status: **APPROVED FOR STAGE 05 QUALIFICATION AT THE EXISTING REVIEWED SURFACE.**

Stage 05 introduces no new third-party runtime or media dependency. It qualifies the Stage 04 K5-owned media-session boundary under a bounded readiness workload and reuses the exact dependency surface already approved for Stage 04.

## Exercised third-party surface

- GStreamer `1.28.7`
- `rtspsrc` from `gst-plugins-good`, effective license `LGPL`
- `queue` from core GStreamer, effective license `LGPL`
- `fakesink` from core GStreamer, effective license `LGPL`
- psutil `7.2.2`, BSD-3-Clause, for bounded process-tree cleanup

The governing review remains `docs/STAGE_04_DEPENDENCY_REVIEW.md`, with upstream integrity and licensing references in `docs/STAGE_03_GSTREAMER_REVIEW.md` and distribution notices in `THIRD_PARTY_NOTICES.md`.

## Stage 05 qualification posture

The readiness workload does not add decoder, codec, demuxer, transcoder, recorder, persistence, analytics, UI, or storage dependencies. It exercises repeated start/stop/re-entry, cleanup, failure normalization, bounded increasing concurrency, and source-free evidence generation through project-owned contracts.

No camera media, frames, clips, RTSP URIs, camera addresses, credentials, or raw runner identity are part of the retained readiness evidence contract.

A change to the GStreamer version, plugin surface, psutil version, or distribution model requires a new dependency review before Stage 05 can close.

This is an engineering dependency review, not legal advice.
