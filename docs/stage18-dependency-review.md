# Stage 18 Decoder Runtime Dependency Review

Status: **CONDITIONALLY APPROVED PENDING EXACT-REVISION PHYSICAL INVENTORY.**

Stage 18 narrows the concrete Windows H.264 playback runtime to the existing isolated GStreamer `1.28.7` distribution and selects `d3d11h264dec` as the decoder candidate. It intentionally avoids adding PyAV/FFmpeg bindings, OpenH264, or another separately distributed codec runtime at this gate.

## Selected surface

The exact surface to be physically inventoried is:

- `appsrc` — application packet-source boundary — expected `gst-plugins-base`
- `rtph264depay` — H.264 RTP depayloader — expected `gst-plugins-good`
- `h264parse` — H.264 parser — expected `gst-plugins-bad`
- `d3d11h264dec` — Direct3D 11 / DXVA H.264 decoder — expected `gst-plugins-bad`
- `videoconvert` — raw-video conversion boundary — expected `gst-plugins-base`
- `appsink` — application frame-sink boundary — expected `gst-plugins-base`

Every element must report version `1.28.7`, effective license `LGPL`, and the expected source module on the exact accepted revision. Qualification fails closed on any missing element or metadata mismatch.

## Why this decoder path

GStreamer documents `d3d11h264dec` as a Direct3D11/DXVA H.264 hardware decoder in the `d3d11` plugin from GStreamer Bad Plug-ins. That keeps Stage 18 inside the already pinned GStreamer distribution while using the Windows platform decode API rather than adding a separate FFmpeg/libav or OpenH264 runtime to the K5 distribution surface.

Upstream references:

- `https://gstreamer.freedesktop.org/documentation/d3d11/d3d11h264dec.html`
- `https://gstreamer.freedesktop.org/documentation/rtp/rtph264depay.html`
- `https://gstreamer.freedesktop.org/documentation/app/index.html`
- `https://gstreamer.freedesktop.org/documentation/frequently-asked-questions/licensing.html`
- `https://gstreamer.freedesktop.org/documentation/plugin-development/appendix/licensing-advisory.html`

GStreamer describes its framework and official plugin code as LGPL-oriented and intended to support applications under licenses of their choice. As with the earlier Stage-03 review, that general upstream posture is not treated as blanket approval: Stage-18 acceptance requires `gst-inspect-1.0` on the isolated physical Windows runtime to prove the effective license, version, plugin identity, and source module for every newly exercised element.

## Evidence/privacy contract

The qualification process executes `gst-inspect-1.0` with argv-only process invocation and suppresses stderr. Raw inspection output is parsed in memory and is never retained. The artifact contains only:

- exact Git revision
- pinned GStreamer version
- installer SHA-256 when available
- selected decoder name
- normalized element role, plugin, source module, version, and effective license

Executable paths, plugin filenames, host paths, runner identity, camera addresses, credentials, RTSP URIs, RTP payloads, frames, and clips are excluded from retained evidence.

## Acceptance boundary

This review approves the Stage-18 selection only if the exact-revision physical inventory matches the expected surface above and normal CI plus required Stage-04/05/06 physical regressions are green. A mismatch reopens selection rather than silently falling back to another codec implementation.

Concrete packet-to-frame adapter integration remains blocked until Stage 18 closes. This is an engineering dependency review, not legal advice.
