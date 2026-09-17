# Stage 04 Dependency Review

Status: **APPROVED FOR STAGE 04 IMPLEMENTATION AT THE PINNED REVIEWED SURFACE.**

Stage 04 does not introduce a new media framework. It promotes the Stage 03 selected UDP path behind the K5-owned media-session boundary and deliberately narrows the external GStreamer surface used by the runtime adapter.

## Exact third-party surface

The Stage 04 adapter is constrained to:

- GStreamer `1.28.7`
- `rtspsrc` from `gst-plugins-good`, effective license `LGPL`
- `queue` from core GStreamer, effective license `LGPL`
- `fakesink` from core GStreamer, effective license `LGPL`
- psutil `7.2.2` for bounded process-tree cleanup

The adapter declares this reviewed surface in code as version `1.28.7` with the element set `rtspsrc`, `queue`, and `fakesink`. It does not add decoder, codec, demuxer, transcoder, recorder, or storage plugins.

The exact upstream Windows runtime approved during Stage 03 is the official GStreamer 1.28.7 MSVC x86_64 installer with SHA-256:

`032fc6062b8539838fc8da22589cb9b24c5d820baa7f8cc160af9ea08395badf`

That integrity record and the upstream licensing references are maintained in `docs/STAGE_03_GSTREAMER_REVIEW.md`. Stage 04 inherits that review only for the exact narrower surface listed above. A different GStreamer version or additional plugin requires explicit review before use.

## Commercial distribution posture

GStreamer is tracked as `LGPL-2.1-or-later` for engineering dependency purposes. Commercial K5 use is approved subject to the governing LGPL obligations and any applicable third-party component obligations. Redistribution must preserve the applicable notices and release-time source/relinking obligations for the exact binaries actually shipped.

psutil `7.2.2` is BSD-3-Clause and is already covered by the Stage 03 dependency review and distribution notice registry.

This is an engineering dependency review, not legal advice.

## Security and execution boundary

The adapter:

- invokes GStreamer with an argv list and `shell=False`;
- sends stdin, stdout, and stderr to null rather than retaining diagnostics that can contain an RTSP URI or credentials;
- never writes frames, clips, media, source URIs, camera addresses, credentials, or raw runner identity;
- uses the Stage 03 bounded terminate/kill process-tree pattern for cleanup;
- exposes only sanitized K5 lifecycle failures through `MediaSession`;
- serializes lifecycle transitions so concurrent callers cannot create duplicate runtime starts.

## Gate requirement

Stage 04 may not close from this review alone. Closure still requires green normal CI on the exact accepted revision plus source-free physical evidence on the camera-lab runner showing the reviewed GStreamer 1.28.7 UDP receive path and deterministic K5 lifecycle/re-entry behavior.
