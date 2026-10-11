# K5 Vision Donor Ledger

This ledger records third-party source or substantial copied material incorporated into K5 Vision. It is a provenance and compliance record, not a substitute for the governing upstream license.

## Required record for every donor import

For each donor component or copied source block, record:

- Component/project name
- Upstream repository or canonical source
- Exact version, tag, or commit SHA
- Governing license/SPDX identifier
- Upstream copyright holder(s)
- Files or functionality incorporated
- Whether source was copied, adapted, vendored, linked, or used only as a package dependency
- Local modifications
- Required attribution/NOTICE obligations
- Commercial-use conclusion
- Reviewer and review date
- Upstream checksum or other immutable identifier when practical

## Current donor imports

No Analytics business logic is vendored in K5. The following entry records the
separate, exact-source engineering wheel and its supplemental license notice.

Package dependencies declared in `pyproject.toml` are dependencies, not automatically donor-source imports. If source from one of those projects is copied or substantially adapted into this repository, add a ledger entry before merge.

## Entry template

### <component name>

- **Source:** <canonical URL>
- **Version/commit:** <tag or full SHA>
- **License:** <SPDX identifier>
- **Copyright:** <upstream copyright notice>
- **K5 files affected:** <paths>
- **Integration method:** <copied/adapted/vendored/package dependency/etc.>
- **Modifications:** <summary or none>
- **Required notices:** <requirements>
- **Commercial use:** <approved / restricted / rejected>
- **Reviewed by:** <name>
- **Reviewed on:** <YYYY-MM-DD>
- **Integrity reference:** <checksum or immutable reference>

## Rules

1. Never remove an upstream copyright or license notice from donor material.
2. Do not describe donor material as K5-owned code.
3. Permissive donor licenses remain effective for the donor material even when surrounding K5-owned material is distributed under different terms.
4. Copyleft, source-available, non-commercial, research-only, evaluation-only, no-license, or ambiguous material requires explicit review before incorporation.
5. Public availability is not evidence of permission to copy or commercialize code.
6. If provenance cannot be demonstrated, do not merge the material.

## Analytics Lab engineering package wrapper

- **Source:** https://github.com/mkurtgerald/Analytics-lab
- **Version/commit:** `c8b347ae538991a0c0ce38eabc2dc17b566531d3`; distribution
  `k5-analytics-runtime==0.0.0+gc8b347ae538991a0c0ce38eabc2dc17b566531d3`
- **License:** K5 Analytics Lab Source-Available License v1.0; original donor
  material retains MIT and Apache-2.0 terms
- **Copyright:** Analytics project copyright (c) 2026 Kurt Gerald; ByteTrack
  derived slice copyright (c) 2021 Yifu Zhang; Open Model Zoo decoder reference
  copyright (C) 2020-2024 Intel Corporation
- **K5 files affected:** `scripts/build_analytics_runtime_wheel.py`,
  `src/k5vision/analytics_package.py`, `src/k5vision/data/analytics-runtime-*`,
  and `docs/ANALYTICS_RUNTIME_PACKAGING.md`
- **Integration method:** a separately installed, namespace-preserving runtime
  wheel built solely from local immutable input; no Analytics detector, tracker,
  pose or temporal source is copied into the K5 repository
- **Modifications:** none to upstream Analytics sources/notices. K5 owns only the
  packaging/admission wrapper. Upstream package version and inline notices stay
  unchanged; exact revision identity is in distribution metadata and the manifest
- **Required notices:** full exact upstream `LICENSE`, `THIRD_PARTY.md` and
  `third_party/licenses/ByteTrack-MIT.txt`; inline donor attribution retained;
  complete supplemental Apache-2.0 license from Open Model Zoo
  `6697dead54ed1cdd664b0313189c2cb52ee6335e/LICENSE`, Git blob
  `261eeb9e9f8b2b4b0d119366dda99c6fd7d35c64`
- **Commercial use:** restricted; engineering qualification only. This wrapper
  does not grant project redistribution/production rights or close native wheel,
  transitive dependency, model, data or product-release review. Preserve upstream
  source-available restrictions. Separate written commercial authority and
  unresolved release reviews are required before shipping
- **Reviewed by:** automated exact-source and notice identity verification; no
  legal sign-off is claimed
- **Reviewed on:** 2026-10-03
- **Integrity reference:** all 67 Python sources plus three exact Analytics
  notices/registers are checked against upstream Git blob IDs; supplemental OMZ
  license is also Git-blob verified. Full SHA-256/size/Git identities are recorded
  in `src/k5vision/data/analytics-runtime-manifest.json`, whose hash is pinned in
  the admission module
- **Replacement boundary:** change the pinned package only through a reviewed
  manifest/version update and applicable installed-runtime qualification, rather
  than copying business logic into K5 or admitting arbitrary `PYTHONPATH` source

## MediaMTX native continuous-media worker evaluation

- **Source:** https://github.com/bluenviron/mediamtx
- **Version/commit:** `v1.21.2`,
  `914e39535ece709919ac325aaeed98d394ed9093`
- **License:** MIT
- **Copyright:** copyright (c) 2019 aler9
- **K5 files affected:** `src/k5vision/media/mediamtx_provider.py`,
  `src/k5vision/media/mediamtx_config.py`,
  `src/k5vision/data/mediamtx-donor-manifest.json`,
  `tests/unit/test_mediamtx_provider.py`,
  `tests/integration/test_mediamtx_donor_runtime.py`,
  `docs/WINDOWS_DONOR_MEDIA_BOUNDARY.md`, `THIRD_PARTY_NOTICES.md`, and
  `third_party/licenses/MediaMTX-MIT.txt`
- **Integration method:** separate native executable with fixed HTTP read APIs
  and private generated configuration. No upstream implementation source or
  binary is vendored. K5-owned adapter code follows the pinned upstream schema;
  the exact upstream license is copied unchanged for attribution.
- **Modifications:** none to donor source or release binaries. Opt-in K5
  normalization and configuration do not replace the current installed runtime
  or change accepted installer identities.
- **Purpose:** evaluate continuous enrolled-camera RTSP pull, reconnect,
  stream fan-out, fMP4 recording, age-based retention and playback inventory
  behind the canonical Windows media-worker boundary.
- **Runtime/development use:** official Windows amd64 executable is the target;
  the official Linux amd64 executable is used only for portable protocol and
  generated-media regression. No new detector/model or paid service is adopted.
- **Required notices:** preserve the full MIT license and original copyright;
  complete notice retained in `third_party/licenses/MediaMTX-MIT.txt`.
  Review/preserve notices for the exact compiled binary's transitive modules
  before redistribution.
- **Commercial use:** MIT source permits commercial use and redistribution with
  notices preserved. This record approves the source/API evaluation scope;
  it does not claim completed Windows binary/transitive redistribution review
  or native product qualification. No executable is shipped by this change.
- **Reviewed by:** automated immutable-source, upstream release metadata and
  checksum verification; no legal sign-off or physical Windows pass is claimed.
- **Reviewed on:** 2026-10-11
- **Integrity references:** upstream `LICENSE` SHA-256
  `ecae73b0a23185a35a1222edc0aae5245bb9739420bad0125b0c42e935301a80`;
  official Windows archive SHA-256
  `a84224f5a490855684e62ba6fb71e80ea2a5e16aed61883bb58380290e93e467`;
  portable Linux archive SHA-256
  `121be6e00397e473a95d83c75f4fcd3819a6af3b0e1a50889cba43ab4fab6a27`.
  Both archive identities match upstream `checksums.sha256` and the release
  asset digests. URLs and identities are in the donor manifest.
- **Replacement boundary:** fixed K5-owned media state, configuration,
  recording-segment and playback-span contracts. Any upstream upgrade or
  installed-default switch requires a new immutable artifact review and native
  acceptance. Existing framed-RTP recordings retain their existing reader.

## FFmpeg/ffprobe native Windows development fixtures

- **Source:** FFmpeg.org-recommended Gyan builds;
  https://github.com/GyanD/codexffmpeg/releases/tag/9.0.2
- **Version/artifact:** `9.0.2`, `ffmpeg-9.0.2-essentials_build.zip`
- **License/copyright:** GPL-3.0-or-later; FFmpeg upstream developers and
  separately attributed build dependencies. The archive preserves its GPL
  license and README; no upstream license is replaced.
- **K5 files affected:** `.github/workflows/donor-media-windows.yml`,
  `src/k5vision/data/mediamtx-donor-test-tools.json` and development notices.
- **Integration/modifications:** external native development executables,
  invoked only as separate fixture-generation and decoding processes;
  unmodified binaries, no source import or linking.
- **Purpose:** generated H.264 and independent decoded-frame verification in
  the isolated native Windows donor acceptance. Not a product runtime dependency.
- **Review conclusion:** reviewed GPL development execution without
  redistribution. No tool/archive is bundled or uploaded as evidence. This
  scope does not approve incorporation or redistribution in a product installer.
  The build enables GPL/version3/libx264 and has no `--enable-nonfree` setting.
- **Required notices:** retain the supplied archive license/attributions while
  using these external tools; any later redistribution requires a separate
  dependency review and fulfillment of its source/notice obligations.
- **Reviewed by/on:** automated upstream metadata, exact archive and PE amd64
  identity inspection, 2026-10-11; no legal sign-off is claimed.
- **Integrity:** archive SHA-256
  `60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba`,
  matching both GitHub release-asset digest and Gyan's versioned checksum.
  Full source/artifact references are in the test-tool manifest.
- **Replacement:** supply separately admitted fixture tools; this does not
  change the K5/MediaMTX runtime or the normalized provider contract.
