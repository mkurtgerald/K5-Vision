# Third-Party Notices

K5 Vision may incorporate or depend upon third-party software and other materials. Those materials remain subject to their original copyright and license terms.

This file is the distribution-facing notice registry. Detailed source provenance is maintained in `docs/DONOR_LEDGER.md`.

## Incorporated donor source

No incorporated donor source is currently recorded.

## Runtime and development dependencies

Dependencies declared in `pyproject.toml` must be reviewed under `docs/COMMERCIAL_DEPENDENCY_POLICY.md` before commercial distribution. Their inclusion as package dependencies does not transfer their copyrights to K5 Vision and does not change their governing licenses.

### GStreamer

K5 Vision uses a reviewed external GStreamer runtime surface for RTSP/RTP media transport. The Stage 04 approved surface is pinned to GStreamer 1.28.7 and the `rtspsrc`, `queue`, and `fakesink` elements. GStreamer and its own plugin code are tracked under LGPL-2.1-or-later for engineering dependency purposes. Preserve all applicable upstream copyright/license notices and satisfy the governing LGPL source/relinking obligations for the exact binaries distributed with a K5 release. Approval of this narrow surface is not blanket approval to redistribute every plugin present in a general-purpose GStreamer installation. The review records are maintained in `docs/STAGE_03_GSTREAMER_REVIEW.md` and `docs/STAGE_04_DEPENDENCY_REVIEW.md`.

### psutil

K5 Vision depends on psutil for cross-platform process resource observation and bounded process cleanup. psutil is distributed under the BSD-3-Clause license and remains copyright its upstream authors and contributors. Preserve the applicable upstream license and copyright notice when redistributing the dependency. The Stage 03 review record is maintained in `docs/STAGE_03_DEPENDENCY_REVIEW.md`.

## Notice rule

When third-party material requiring attribution or reproduction of license text is incorporated or distributed with K5 Vision, add the required notice here or include the complete upstream notice in a clearly identified file under a third-party notices directory before release.

Do not remove or replace an upstream copyright, patent, attribution, or license notice.
