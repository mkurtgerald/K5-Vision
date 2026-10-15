# Windows decoder compatibility — bounded engineering review

The October 11 installed-product diagnosis at controller `50c58fb5cf4c7fe551137c34f0336ca867dcc99b`
ran the unmodified installer product `70b24135a085c8bb1dca23253a3c25fef9e18164`.
Run `38141253745` observed `d3d11h264dec=0`, the other four inspected pipeline
elements present, and installed launcher line 709 returning HTTP 503 with a
sanitized decoder/presentation execution failure. This establishes an unsupported
hosted graphics/decoder combination, not the owner's physical graphics capabilities.

The GStreamer D3D11 plugin registers codec elements according to underlying hardware
support: https://gstreamer.freedesktop.org/documentation/d3d11/index.html .
Do not rerun the same absent-decoder path or silently use an unreviewed fallback.

## Candidate review, not product enablement

The next engineering probe inspects only the exact GStreamer MSVC x64 runtime
already used by the installer: 1.28.7, installer SHA-256
`032fc6062b8539838fc8da22589cb9b24c5d820baa7f8cc160af9ea08395badf`.
It does not install or modify K5, accept a source URI, or use physical cameras.
No software decoder is enabled in the product by this review.

Upstream Cerbero 1.28.7 resolves to `e0e7007e210dab3e2f3e898939e2cfe1fd02f123`:
https://github.com/GStreamer/cerbero/tree/e0e7007e210dab3e2f3e898939e2cfe1fd02f123 .
The exact `recipes/ffmpeg.recipe` blob `aab487cdeba26eafefde0ce69712a73f14f45dfa`
selects FFmpeg 7.1, LGPL-2.1-or-later, upstream archive SHA-256
`40973d44970dbc83ef302b0609f2e74982be2d85916dd2ee7472d30678a7abe6`, with nonfree
and version3 disabled. The `recipes/gst-libav-1.0.recipe` blob
`47ec55e054fc50c7765729335d50a5e5d8b62113` declares LGPL-2.0-or-later and the
FFmpeg dependency. Recipes alone do not establish the shipped binary license.

The probe therefore requires matching installed plugin version, plugin origin and
LGPL metadata; FFmpeg's actual exported license must be LGPL version 2.1 or later,
and its configuration must not enable GPL, nonfree or version3 options. It records
exact SHA-256 identities of plugin/codec/core/app DLLs and checks them again after
execution. Only after those checks does a camera-free synthetic pipeline encode
and decode exactly eight generated frames in memory, without saving media. A
successful component probe is not public RTSP, product, performance or beta acceptance.

## Preserved shipping boundaries

There is no copied third-party source or codec modification here. The probe uses
separate upstream shared libraries through their public ABI for internal engineering
qualification. Runtime replacement, notices, exact transitive binary inventory,
source-offer/relinking obligations, patents and commercial distribution require
separate review before product enablement or release. See
https://ffmpeg.org/legal.html and `COMMERCIAL_DEPENDENCY_POLICY.md`.
No legal signoff, patent clearance or production redistribution approval is claimed.
