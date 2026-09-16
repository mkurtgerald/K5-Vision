# Stage 03 GStreamer Candidate Review

Status: **APPROVED FOR STAGE 03 SELECTION AT THE PINNED 1.28.7 RUNTIME SURFACE.**

This record covers only the GStreamer surface exercised by the Stage 03 transport/runtime qualification. It does not approve unrelated codecs, demuxers, optional plugins, or the full contents of a general-purpose GStreamer bundle.

## Accepted runtime identity

The accepted source-free runner inventory at revision `7b2ebc413be1d104a7eaaa8d38147ce6101b5015` recorded the K5-isolated reviewed runtime as:

- Windows x86_64 physical runner
- `gst-launch-1.0 version 1.28.7`
- official upstream installer SHA-256 `032fc6062b8539838fc8da22589cb9b24c5d820baa7f8cc160af9ea08395badf`
- `rtspsrc`: present, effective license `LGPL`, source module `gst-plugins-good`, plugin version `1.28.7`
- `queue`: present, effective license `LGPL`, source module `gstreamer`, plugin version `1.28.7`
- `identity`: present, effective license `LGPL`, source module `gstreamer`, plugin version `1.28.7`
- `fakesink`: present, effective license `LGPL`, source module `gstreamer`, plugin version `1.28.7`

The host also retains older machine-wide GStreamer 1.20.4 packages. Those packages are not the reviewed Stage 03 runtime. The qualification workflow prepends and verifies the isolated K5 runtime, requires `gst-launch-1.0` to report 1.28.7, and verifies every required plugin before the physical source is touched.

The runtime inventory is intentionally source-free: it does not read camera credentials, contact the camera, retain an RTSP URI, or retain media.

## Integrity and provenance

The Stage 03 provisioning script downloads the exact GStreamer 1.28.7 MSVC x86_64 runtime installer and its adjacent upstream `.sha256sum` file from:

`https://gstreamer.freedesktop.org/data/pkg/windows/1.28.7/msvc/`

Provisioning rejects a checksum record with an unexpected filename or format and rejects an installer whose calculated SHA-256 does not equal the official upstream value. The verified hash is retained in the isolated runtime provenance record and propagated to final Stage 03 evidence as part of the secret-bound host configuration fingerprint.

## Governing license and commercial suitability

Primary upstream references:

- GStreamer licensing FAQ: `https://gstreamer.freedesktop.org/documentation/frequently-asked-questions/general.html`
- GStreamer licensing advisory: `https://gstreamer.freedesktop.org/documentation/plugin-development/appendix/licensing-advisory.html`
- GStreamer Good Plug-ins module: `https://gstreamer.freedesktop.org/modules/gst-plugins-good.html`

Upstream states that GStreamer and its own plugin code use the GNU LGPL 2.1, and GStreamer documentation describes the framework as intended to support applications under licenses of their choice. The accepted runner inventory independently reports the effective license of every Stage 03 element used here as `LGPL`.

The versioned review records therefore identify the reviewed surface as `LGPL-2.1-or-later` for engineering dependency tracking.

**Commercial-use conclusion:** the Stage 03 GStreamer surface is approved for commercial K5 use subject to the governing LGPL obligations and any applicable third-party component obligations.

**Redistribution conclusion:** redistribution of the required GStreamer surface is approved subject to LGPL compliance and release-time reconciliation of the exact binaries, notices, source/relinking obligations where applicable, and any separately licensed supporting components that are actually shipped. This is not blanket approval to redistribute every plugin installed by a general-purpose GStreamer bundle.

This is an engineering dependency review, not legal advice.

## Platform support

Primary upstream references:

- GStreamer downloads: `https://gstreamer.freedesktop.org/download/`
- Windows installation: `https://gstreamer.freedesktop.org/documentation/installing/on-windows.html`

Upstream provides official Windows binaries and documents Windows support. Linux is a first-class supported platform through distribution packages and upstream source builds. The Stage 03 required surface is therefore accepted for the project requirement to support Windows and Linux, with platform-specific physical qualification still required when a Linux packaging target is implemented.

## Security and update posture

Primary upstream references:

- Current releases: `https://gstreamer.freedesktop.org/releases/`
- GStreamer 1.28 stable-series notes: `https://gstreamer.freedesktop.org/releases/1.28/`

As reviewed on 2026-09-16, upstream identifies 1.28.7 as the current stable release. K5 now provisions that version into an isolated runner-owned path and verifies it against the upstream SHA-256 before use. The earlier 1.20.4 security-baseline blocker is therefore resolved for Stage 03 qualification.

A future runtime update is not implicitly approved by this record. A different GStreamer version or materially different plugin surface requires an explicit review and re-qualification before it can replace this accepted baseline.

## Physical re-qualification result

At revision `7b2ebc413be1d104a7eaaa8d38147ce6101b5015`:

1. CI completed successfully;
2. the source-free runtime inventory completed successfully against isolated GStreamer 1.28.7;
3. physical qualification completed successfully on the accepted camera-lab runner;
4. both versioned transport candidates completed five scored runs, interruption/re-entry recovery, and the common `1,2,3` increasing-load resource ladder;
5. no camera source, username, password, frame, clip, or retained media was added to the evidence artifact.

The versioned `CandidateReview` records for `gstreamer-tcp` and `gstreamer-udp` are maintained in `config/stage03-gstreamer-reviews.json` and apply only to this reviewed runtime surface.

## Remaining Gate 03 closure condition

This dependency review no longer blocks Stage 03. Gate 03 still must not close until the exact accepted revision retains a complete `Stage03Evidence` bundle with safe source/host fingerprints and a deterministic `Stage03SelectionRecord`, and CI plus physical qualification are green for that revision.

No Gate 04 implementation is authorized by this review.
