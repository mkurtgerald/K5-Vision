# Third-Party Notices

K5 Vision may incorporate or depend upon third-party software and other materials. Those materials remain subject to their original copyright and license terms.

This file is the distribution-facing notice registry. Detailed source provenance is maintained in `docs/DONOR_LEDGER.md`.

## Incorporated donor source

K5 does not vendor Analytics Lab business logic. Its separately built engineering
runtime package is described below; all upstream rights remain effective.

## Runtime and development dependencies

Dependencies declared in `pyproject.toml` must be reviewed under `docs/COMMERCIAL_DEPENDENCY_POLICY.md` before commercial distribution. Their inclusion as package dependencies does not transfer their copyrights to K5 Vision and does not change their governing licenses.

### GStreamer

K5 Vision uses a reviewed external GStreamer runtime surface for RTSP/RTP media transport. The Stage 04 approved surface is pinned to GStreamer 1.28.7 and the `rtspsrc`, `queue`, and `fakesink` elements. GStreamer and its own plugin code are tracked under LGPL-2.1-or-later for engineering dependency purposes. Preserve all applicable upstream copyright/license notices and satisfy the governing LGPL source/relinking obligations for the exact binaries distributed with a K5 release. Approval of this narrow surface is not blanket approval to redistribute every plugin present in a general-purpose GStreamer installation. The review records are maintained in `docs/STAGE_03_GSTREAMER_REVIEW.md` and `docs/STAGE_04_DEPENDENCY_REVIEW.md`.

### psutil

K5 Vision depends on psutil for cross-platform process resource observation and bounded process cleanup. psutil is distributed under the BSD-3-Clause license and remains copyright its upstream authors and contributors. Preserve the applicable upstream license and copyright notice when redistributing the dependency. The Stage 03 review record is maintained in `docs/STAGE_03_DEPENDENCY_REVIEW.md`.

### Caddy native operator media gateway pilot

The per-user native Windows pilot downloads the official Caddy `v2.11.7`
executable from `caddyserver/caddy`, source commit
`72dd0fb067f6d7826c7f79907670ba4a713bfe37`. Caddy is copyright 2015 Matthew
Holt and The Caddy Authors and distributed under Apache-2.0. Its complete
unchanged license is preserved at `third_party/licenses/Caddy-Apache-2.0.txt`.

Caddy implementation source and executables are not bundled by this change.
Preserve applicable upstream/module notices before redistributing a binary;
official target SBOMs are available with the release. K5's surrounding source
license does not restrict the rights granted by Apache-2.0. Download checksums,
executable identities and provenance are in
`src/k5vision/data/caddy-donor-manifest.json`. Native product qualification and
commercial binary redistribution review remain separate from this pilot.

### MediaMTX continuous-media worker evaluation

The opt-in media adapter evaluates MediaMTX `v1.21.2` from
`bluenviron/mediamtx`, commit `914e39535ece709919ac325aaeed98d394ed9093`.
MediaMTX is copyright (c) 2019 aler9 and distributed under the MIT License.
The full unchanged license is preserved at
`third_party/licenses/MediaMTX-MIT.txt`.

No MediaMTX implementation source or executable is bundled by this change.
Preserve this notice and the applicable transitive module notices when shipping
the reviewed native executable. K5's surrounding source license does not
restrict rights granted by the upstream MIT license. Native binary review and
target Windows acceptance remain separate from the source/API evaluation.

### FFmpeg/ffprobe donor acceptance tools

The native Windows donor acceptance job downloads the pinned Gyan FFmpeg 9.0.2
essentials build, under GPL-3.0-or-later, for separate-process synthetic-media
generation and decoding. FFmpeg remains copyright its upstream developers;
retain the licenses provided in the external archive during its use.

These tools are development fixtures, not product runtime dependencies. Their
executables and archives are not incorporated, bundled or uploaded by K5. This
development-use review grants no product redistribution approval. Provenance
and the checksum are in `src/k5vision/data/mediamtx-donor-test-tools.json`.

## Notice rule

When third-party material requiring attribution or reproduction of license text is incorporated or distributed with K5 Vision, add the required notice here or include the complete upstream notice in a clearly identified file under a third-party notices directory before release.

Do not remove or replace an upstream copyright, patent, attribution, or license notice.

## Analytics Lab engineering runtime

The optional `k5-analytics-runtime` engineering wheel packages unchanged
`analytics_lab` sources from `mkurtgerald/Analytics-lab` revision
`c8b347ae538991a0c0ce38eabc2dc17b566531d3`. Original Analytics project material is
copyright (c) 2026 Kurt Gerald and governed by the **K5 Analytics Lab
Source-Available License v1.0**, not an open-source license or the K5 Vision
license. The wrapper does not transfer ownership or grant commercial, production
or redistribution rights.

The wheel preserves the complete upstream `LICENSE` and `THIRD_PARTY.md`, plus
the ByteTrack MIT license (copyright (c) 2021 Yifu Zhang). Analytics' adapted
Open Model Zoo decoder reference retains its copyright (C) 2020-2024 Intel
Corporation and Apache-2.0 attribution; the full Apache license from the exact
Open Model Zoo revision is included. These complete notices are installed in
`k5_analytics_runtime-<version>.dist-info/licenses/` as `Analytics-Lab-LICENSE`,
`THIRD_PARTY.md`, `ByteTrack-MIT.txt`, and `Apache-2.0.txt`. The supplemental Apache
license is also preserved under `src/k5vision/data/analytics-runtime-Apache-2.0.txt`.

This is engineering qualification only. No model, media, dataset, OpenVINO,
OpenCV or other native runtime binary is bundled. Exact native/transitive wheel
notices and redistribution review, separate model/data rights, and commercial
project authority remain release gates. See `docs/ANALYTICS_RUNTIME_PACKAGING.md`
and `docs/DONOR_LEDGER.md`; the upstream register's pending reviews remain pending.

## Independent OpenVINO / Open Model Zoo person view

The optional K5 operator person-view adapter uses the documented public API of
OpenVINO `2026.3.1` and the separately licensed Open Model Zoo
`person-detection-retail-0013` FP16 model at revision
`6697dead54ed1cdd664b0313189c2cb52ee6335e`. The official model definition is
copyright (c) 2023-2024 Intel Corporation and expressly links the Apache-2.0
license. The unchanged exact source-root license remains in
`src/k5vision/data/analytics-runtime-Apache-2.0.txt`. NumPy `2.2.6` is separately
BSD-3-Clause licensed; OpenVINO telemetry `2025.2.0` and supplied platform/native
dependencies retain their own package notices.

K5 wrote this adapter independently; no Analytics Lab detector/tracker source is
copied or imported. That project's separate engineering-wrapper restrictions
remain unchanged. No model, native runtime, dataset or image is bundled in K5.
The official Apache OMZ person example used by the ephemeral native witness
retains Intel/Open Model Zoo attribution and the same license. Do not remove
applicable native/transitive package notices when provisioning separate wheels.
Any product redistribution requires the outstanding artifact/native/transitive
release review. See `docs/OPERATOR_PERSON_DETECTION_REVIEW.md` and the donor ledger
for exact donor identities, view limits, evidence and unresolved release gates.

## Native graphical Windows operator candidate

The separate graphical operator installer bundles the unchanged official
CPython 3.12.10 Windows amd64 embeddable distribution, the exact admitted
MediaMTX v1.21.2 and Caddy v2.11.7 executables, and fifteen pinned binary Python
distributions for the manual-RTSP recording operator. This packaging scope
supersedes the earlier no-binary-bundling statements only for that explicit
desktop installer. It does not bundle FFmpeg, ONVIF, Analytics Lab, OpenVINO,
models, sample images or development/browser tools.

The installed `licenses` directory retains the complete CPython binary-component
terms, MediaMTX MIT and Caddy Apache-2.0 licenses, K5's license, this notice, and
the dedicated `WINDOWS_DESKTOP_DEPENDENCIES.md` review. Every dependency's
`.dist-info` directory and upstream license/notice files remain installed.
`dependency-inventory.json` records exact wheel and retained-notice hashes.
The source `windows/desktop/donors.json` and `runtime-requirements.txt` pin each
artifact; preserving these identities does not replace outstanding transitive
native-binary redistribution review.

The GUI installer is produced with Inno Setup 6.4.3, copyright (C) 1997-2025
Jordan Russell, portions copyright (C) 2000-2025 Martijn Laan. Its unchanged
custom license is retained as `third_party/licenses/InnoSetup-6.4.3.txt` and in
the installed `licenses` directory. Inno Setup's license expressly permits
commercial use and redistribution subject to its retained notices and website
addresses. Sources: https://jrsoftware.org/ and
https://github.com/jrsoftware/issrc/tree/is-6_4_3. The compiler installer itself
is an ephemeral development tool and is not included in the product installer.
