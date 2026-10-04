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
