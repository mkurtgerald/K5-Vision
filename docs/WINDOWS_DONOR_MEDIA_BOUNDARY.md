# Architecture decision: native Windows donor media boundary

Status: engineering evaluation; existing application defaults and qualified
installer identities are unchanged. Native Windows integrated acceptance remains
required before making this the installed default.

## Decision

Use MediaMTX `v1.21.2`, commit
`914e39535ece709919ac325aaeed98d394ed9093`, as the independently owned continuous
media worker. Use its official native Windows amd64 executable; Linux, WSL and
Docker are not part of the target installation. The initial Linux execution is a
portable protocol and recording regression only.

K5 already uses MediaMTX to generate an ephemeral alpha source. This evaluation
extends the donor's responsibility to always-on enrolled-camera ingestion,
stream fan-out, keyframe-aware fMP4 recording, age-based retention, recording
inventory and time-based playback. Those mechanisms stay in the donor process.
K5's API does not copy media bytes into its control-plane event loop.

| Responsibility | Owner |
| --- | --- |
| Site, enrolled device, operator identity and authorization | K5 |
| Source credentials and protected configuration | K5 |
| Continuous RTSP/TCP ingestion, reconnect and stream distribution | MediaMTX |
| Recording parts, segments, retention and time-based playback | MediaMTX |
| Reviewed detection and source-attributed overlay results | Existing K5 analytics boundary |
| Native process ownership, restart policy and disk admission | K5 installed supervisor |

This is a service integration, not a source fork. `mediamtx_provider.py` translates
fixed donor read APIs into K5-owned records. `mediamtx_config.py` creates a private
configuration from admitted site/device/source bindings. No donor objects or raw
configuration are returned to operator clients.

## Integration contract

- A deterministic opaque path binds site ID, enrolled device UUID and stream
  token. Source URLs and camera names are absent from path names.
- Configuration contains only selected paths. It has no wildcard publisher,
  anonymous users or `runOn*` commands. Sources are pulled continuously, even
  when the detector or operator view is unavailable.
- API, RTSP, HLS and playback listeners bind numeric loopback addresses. Worker
  credentials authorize internal execution; they are not operator credentials.
  Browser media must pass through a separately admitted K5 authorization bridge.
- Source selection must match the enrolled numeric host. Credential-bearing
  source material belongs only in protected private configuration, never argv,
  logs or public evidence. Configuration generation does not admit filesystem
  ownership or start a child process.
- API reads have bounded time, response size and outstanding requests. Redirects
  and proxy forwarding of internal credentials are disabled. Diagnostic codes
  contain no donor response bodies, URLs or credentials.
- Provider version admission requires the exact evaluated donor version.
  Executable/archive identity and private state ownership are separate installer
  responsibilities. Expected archive hashes are in
  `src/k5vision/data/mediamtx-donor-manifest.json`; binaries are not vendored.
- Recording segments and playable spans have different meanings. Segment
  timestamps identify on-disk pieces; the playback API groups contiguous media
  into spans and preserves interruption gaps. K5 does not infer a continuous
  timeline from segment count.
- Recording filenames include their numeric UTC offset. Restarting in a
  different timezone must preserve absolute segment and playback timestamps.

## Compatibility and limits

Existing `.k5r` and `.k5d` recordings keep their current reader. New donor MP4
recordings are a separate backend; renaming old files or feeding MP4 bytes into
the existing framed-RTP reader is invalid.

MediaMTX does not provide a complete NVR management UI, object detection or a
total recording-byte quota. The remaining integration requires authenticated
operator live/playback controls, timestamp-aligned detections, owned Windows
startup/recovery and disk-pressure admission. Retention is age-based; part-size
limits bound memory, not the total disk footprint.

Playback `/get` takes wall-clock start and duration. Its lack of HTTP byte-range
support means seeking must select a new time interval. Codec and keyframe support
must be tested with the selected cameras and Windows player; this worker does
not transcode an arbitrary camera into a browser-compatible codec.

The first read adapter caps full segment inventories at 2,048 items and response
bodies at 256 KiB. The default one-day retention with one-minute segments fits
this bound under uninterrupted input. Longer retention, shorter segments or many
source interruptions can require windowed inventory access; an oversized result
fails closed while the donor continues recording. A usable long-retention
catalog requires a separate bounded pagination/window contract.

Frigate and Viseron were evaluated as complete donor services but do not meet
the required native Windows runtime. Copying their complete interfaces would
require substantial backend API and platform changes. Their small permissive
components remain optional donor candidates, not dependencies of this boundary.

## Acceptance

The isolated `donor-media-windows.yml` PR workflow executes this acceptance on
the native hosted Windows runner. It verifies the MediaMTX archive identity and
uses separately pinned FFmpeg/ffprobe development fixtures to generate and
decode synthetic media. The uploaded JUnit receipt must report Windows, native
execution and zero skips. This qualifies the tested donor boundary, not an
installed K5 product or a physical camera.

`tests/integration/test_mediamtx_donor_runtime.py` is opt-in. Supply an admitted
MediaMTX executable and development-only FFmpeg/ffprobe tools; the test never
downloads binaries or connects to a real camera. For native Windows:

```powershell
$env:K5_MEDIAMTX_DONOR_EXE = "C:\reviewed-tools\mediamtx.exe"
$env:K5_DONOR_FFMPEG = "C:\reviewed-tools\ffmpeg.exe"
$env:K5_DONOR_FFPROBE = "C:\reviewed-tools\ffprobe.exe"
python -m pytest tests/integration/test_mediamtx_donor_runtime.py --no-cov --junitxml=artifacts/donor-media.xml
```

The Windows archive hash must match the manifest before extraction. The test
checks executable version, uses supplied test codecs, reports the actual
execution platform and always stops/reaps the owned donor and publisher
processes. Its source-free receipt explicitly excludes camera, analytics and
browser qualification.

The portable evaluation exercises actual donor RTSP pull, generated H.264 media,
independently decodable segments, source interruption/reconnection, time-based
playback and restart with retained inventory. It does not qualify real cameras,
person detection, a Windows browser or native Windows process behavior.

The installed target acceptance must use the exact Windows archive and complete
one authenticated camera flow: enroll, live video with person boxes, concurrent
recording, application/service restart, inventory recovery and playback. Exercise
unauthorized requests, source interruption, bounded disk pressure, relaunch and
owned process cleanup. Donor success alone does not accept the complete product.

## Provenance and replacement

MediaMTX source is MIT; retain its original copyright and full notice. The donor
ledger identifies the source revision and evaluation artifacts. Review the exact
Windows binary and its transitive module notices before redistribution. No new
model, hosted service, paid NVR plugin or copied detector source is adopted here.
That binary review includes the embedded hls.js v1.7.3 assets, not only Go
module dependencies.

The Windows acceptance's external FFmpeg/ffprobe fixtures are Gyan release
9.0.2, identified in `mediamtx-donor-test-tools.json`. Their GPL terms are
reviewed for separate-process development execution only. They are not bundled,
uploaded as evidence or used by the product runtime. This review does not
approve their redistribution in a product installer.

The integration is opt-in and leaves existing installation pins unchanged. A
replacement media worker must implement the same normalized K5 read contracts;
upstream upgrades require a new immutable artifact review and target acceptance.

Primary sources: [MediaMTX v1.21.2](https://github.com/bluenviron/mediamtx/releases/tag/v1.21.2),
[pinned configuration](https://github.com/bluenviron/mediamtx/blob/914e39535ece709919ac325aaeed98d394ed9093/mediamtx.yml),
[control API](https://github.com/bluenviron/mediamtx/blob/914e39535ece709919ac325aaeed98d394ed9093/api/openapi.yaml),
[playback](https://mediamtx.org/docs/features/playback).
