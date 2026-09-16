# Stage 03 GStreamer Candidate Review

Status: **BLOCKED FOR FINAL SELECTION — refresh and re-qualify the runtime before Gate 03 may close.**

This record covers only the GStreamer surface exercised by the Stage 03 transport/runtime qualification. It does not approve unrelated codecs, demuxers, optional plugins, or the full contents of a general-purpose GStreamer bundle.

## Measured runner identity

The source-free runner inventory at the accepted physical-test host recorded:

- Windows x86_64 physical runner
- `GStreamer 1.0` package version `1.20.4`, publisher `GStreamer Project`
- `GStreamer 1.0 (Development Files)` package version `1.20.4`, publisher `GStreamer Project`
- `gst-launch-1.0 version 1.20.4`
- `rtspsrc`: present, effective license `LGPL`, source module `gst-plugins-good`, plugin version `1.20.4.3`
- `queue`: present, effective license `LGPL`, source module `gstreamer`, plugin version `1.20.4.3`
- `identity`: present, effective license `LGPL`, source module `gstreamer`, plugin version `1.20.4.3`
- `fakesink`: present, effective license `LGPL`, source module `gstreamer`, plugin version `1.20.4.3`

The inventory is intentionally source-free: it does not read camera credentials, contact the camera, retain an RTSP URI, or retain media.

## Governing license and commercial suitability

Primary upstream references:

- GStreamer licensing FAQ: `https://gstreamer.freedesktop.org/documentation/frequently-asked-questions/general.html`
- GStreamer licensing advisory: `https://gstreamer.freedesktop.org/documentation/plugin-development/appendix/licensing-advisory.html`
- GStreamer Good Plug-ins module: `https://gstreamer.freedesktop.org/modules/gst-plugins-good.html`

Upstream states that GStreamer and its own plugin code use the LGPL, with the framework intended to support applications under licenses of their choice. The installed runner itself reports the effective license of every Stage 03 element used here as `LGPL`.

**Commercial-use conclusion:** the Stage 03 GStreamer surface is commercially usable subject to the governing LGPL obligations and any applicable third-party component obligations.

**Redistribution conclusion:** redistribution of the required GStreamer surface is permitted subject to LGPL compliance and release-time reconciliation of the exact binaries, notices, source-offer/relinking obligations where applicable, and any separately licensed supporting components that are actually shipped. K5 must not treat approval of these four elements as blanket approval to redistribute every plugin installed by a full GStreamer bundle.

This is an engineering dependency review, not legal advice.

## Platform support

Primary upstream references:

- GStreamer downloads: `https://gstreamer.freedesktop.org/download/`
- Windows installation: `https://gstreamer.freedesktop.org/documentation/installing/on-windows.html`

Upstream provides official Windows binaries and documents Windows support. Linux is a first-class supported platform through distribution packages and upstream source builds. The Stage 03 required surface is therefore suitable for the project requirement to support Windows and Linux, subject to re-running platform-specific qualification as those packaging targets are implemented.

## Security and update posture

Primary upstream references:

- Current releases: `https://gstreamer.freedesktop.org/releases/`
- GStreamer 1.28 stable-series notes: `https://gstreamer.freedesktop.org/releases/1.28/`

As of 2026-09-16, upstream identifies `1.28.7` as the current stable release. It was released on 2026-09-07 and includes important security fixes. The physical runner is on `1.20.4`, an older stable series.

The Stage 03 hardening standard requires dependency/security posture to be resolved before gate exit. Therefore the physically measured `1.20.4` runtime is **not approved for final K5 runtime selection**, even though its physical transport measurements passed. This avoids turning a successful camera test into an implicit approval of stale runtime binaries.

## Required closure action

Before a `CandidateReview` may be attached as approved and a final `Stage03SelectionRecord` may be generated:

1. provision a K5-isolated, pinned supported GStreamer build at the accepted current stable/security baseline (currently `1.28.7`) or a newer explicitly reviewed stable build;
2. capture the same source-free package/plugin provenance inventory;
3. rerun the complete physical Stage 03 comparative qualification on that exact runtime revision;
4. confirm the required Stage 03 elements still report acceptable effective licenses;
5. retain the source-free evidence and generate the deterministic selection record only after all review fields are approved.

No Gate 04 implementation is authorized by this review. The current blocker is the runtime security baseline, not the camera, credentials, physical runner, candidate configuration, or qualification harness.
