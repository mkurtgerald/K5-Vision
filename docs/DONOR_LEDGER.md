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

The operator's ONVIF camera onboarding adapts the reviewed go2rtc source slice
recorded below. The retained upstream MIT terms continue to cover that donor
material; the surrounding K5 license does not replace them.

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

## go2rtc ONVIF discovery and stream resolution source slice

- **Source:** https://github.com/AlexxIT/go2rtc
- **Version/commit:** `v1.9.14`,
  `b5948cfb25404cc5cb37b166ecaa2dca20b11d4b`
- **License:** MIT, exact upstream root `LICENSE`; no different license was
  identified for the three selected ONVIF source files
- **Copyright:** Copyright (c) 2022 Alexey Khit
- **K5 files affected:** `src/k5vision/operator_onvif.py`, its operator API/UI
  integration and acceptance tests, `third_party/licenses/go2rtc-MIT.txt`, and
  the donor/distribution/desktop dependency records
- **Integration method:** selected Go source behavior and SOAP message structure
  adapted into a bounded Python standard-library connector. No go2rtc executable,
  Go runtime, module dependency tree, WSDL bundle, or ONVIF Device Manager source
  is incorporated.
- **Adapted source scope:**
  - `pkg/onvif/client.go`: device `GetCapabilities`, media `GetProfiles` and
    `GetStreamUri` flow, and inheriting supplied camera credentials when the
    returned stream URI has no user information
  - `pkg/onvif/envelope.go`: SOAP envelope/WS-Security UsernameToken structure,
    nonce, creation time and SHA-1 password digest
  - `pkg/onvif/helpers.go`: multicast WS-Discovery Probe and camera scope/name
    extraction. The unrelated `GetPosixTZ` function, which credits a separate
    upstream project, is outside the adopted slice.
- **Modifications:** Python standard-library transport/XML implementation;
  numeric camera-address pinning, strict XML handling, bounded response bytes,
  deadlines and concurrency; HTTP Digest support and Media2 fallback. The GUI
  offers discovery, camera credentials, an H.264 profile and **Connect**; K5
  resolves the underlying RTSP URI. ONVIF server behavior, PTZ, imaging,
  configuration writes and the donor's regular-expression XML parser are not
  adopted.
- **Purpose/runtime scope:** native Windows camera onboarding for the recording
  operator. The existing MediaMTX recording and Caddy delivery boundaries remain
  in use. No shell, separate Python install, go2rtc process, or additional Python
  distribution is needed by the desktop user.
- **Required notices:** the complete unchanged MIT license/copyright is retained
  in `third_party/licenses/go2rtc-MIT.txt`, the adapted module's inline donor
  notice, the distribution notice register and the desktop installer's licenses.
  Preserve these with copies/substantial portions of the adapted source.
- **Commercial use:** approved for this exact selected-source adaptation under
  MIT with its notices preserved. This does not clear unrelated go2rtc packages,
  assets, dependencies or executables, change K5-owned source terms, or claim
  physical-camera qualification.
- **Reviewed by:** automated exact Git object, selected-file source and notice
  identity inspection under `docs/COMMERCIAL_DEPENDENCY_POLICY.md`; no legal
  sign-off is claimed
- **Reviewed on:** 2026-10-18
- **Replacement boundary:** `operator_onvif.py` discovery, profile selection and
  stream-resolution contract; the recording worker and saved camera model remain
  separate. An upstream/source-scope change needs a new donor review and native
  onboarding acceptance.
- **Integrity references:** the local upstream checkout resolves the tag and
  commit above. Exact source identities are:

| Upstream file | Git blob | SHA-256 |
| --- | --- | --- |
| `LICENSE` (1,068 bytes) | `beb8fca089b8d99cb85586015cdaf68959114cf8` | `b0dcf4855af5a72b4dfbd9117c207b330f4cc35658576a0b5351d6e2becac546` |
| `pkg/onvif/client.go` | `bad103c71f3b88ccb61cc098d0423f9df03e7f43` | `fc523c62bac62cb6a12ff9fde8eb752d41be3721457c25bd3b6463ba6cdd7b0c` |
| `pkg/onvif/envelope.go` | `76a41260a36d22661938e3e8c78ccd9b2ff3129c` | `6de78d351fd43c112dbe180b56690dda8a06cb94ac326e5b6a7f0559921a3bed` |
| `pkg/onvif/helpers.go` | `8fac9ac4cbb8cfd3e359a4a2e4ad59a34e26b27d` | `1fe6e4935027bfaf84ab5f5382808fc63c8feaa1a0809b50f47750ec643b2807` |

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

## Caddy native operator media gateway pilot

- **Source:** https://github.com/caddyserver/caddy
- **Version/commit:** `v2.11.7`,
  `72dd0fb067f6d7826c7f79907670ba4a713bfe37`
- **License:** Apache-2.0
- **Copyright:** copyright 2015 Matthew Holt and The Caddy Authors
- **K5 files affected:** `src/k5vision/data/caddy-donor-manifest.json`,
  `src/k5vision/operator_media_gateway.py`,
  `src/k5vision/operator_product_service.py`, `scripts/Start-K5Operator.ps1`,
  `third_party/licenses/Caddy-Apache-2.0.txt`,
  and this notice register
- **Integration method:** separately downloaded official native executable,
  invoked as the local HTTP data gateway. K5 generates private gateway
  configuration and retains operator authorization. Caddy streams media;
  Python remains the control plane. No Caddy implementation source or binary
  is incorporated into this repository.
- **Modifications:** none to donor source or release binaries
- **Runtime/development use:** official Windows amd64 binary is the per-user
  pilot target; Linux amd64 is portable protocol regression only. No Linux,
  WSL, Docker, hosted service, or custom Caddy plugin is required by the target.
- **Required notices:** preserve the unchanged Apache-2.0 license and copyright
  attribution. No root `NOTICE` file exists at this source revision. Preserve
  applicable module notices when redistributing the binary; official per-target
  SBOMs are published alongside the release.
- **Commercial use:** Apache-2.0 permits commercial source use and redistribution
  with its notices preserved. This review admits the official-download pilot
  scope, not a claim of completed transitive binary redistribution review or
  installed Windows product acceptance. No executable is shipped in the PR.
- **Reviewed by:** automated immutable-source, archive, release-digest and
  upstream-checksum verification; no legal sign-off is claimed
- **Reviewed on:** 2026-10-11
- **Integrity references:** exact source license SHA-256
  `cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30`;
  Windows archive SHA-256
  `0a1edc0b799512051c57071ce0e798d3f2cf67dc98171366f1d2b326072e3b06`;
  Linux archive SHA-256
  `727b91701a392de6ebc5027509f548bf39979e5216340d0faed8fa5e69c84f8b`.
  Both downloaded archives match GitHub's release-asset SHA-256 digests and
  the official release checksum file's SHA-512 values. The manifest records
  both algorithms and executable SHA-256 hashes derived from these archives.
- **Replacement boundary:** fixed local HTTP proxy configuration and K5-owned
  authorization checks. A different gateway or upstream upgrade requires new
  immutable artifact review and target acceptance. Accepted Alpha installer
  pins are not changed by the pilot launcher.

## MediaMTX native continuous-media worker evaluation

- **Source:** https://github.com/bluenviron/mediamtx
- **Version/commit:** `v1.21.2`,
  `914e39535ece709919ac325aaeed98d394ed9093`
- **License:** MIT
- **Copyright:** copyright (c) 2019 aler9
- **K5 files affected:** `src/k5vision/media/mediamtx_provider.py`,
  `src/k5vision/media/mediamtx_config.py`,
  `src/k5vision/operator_product_service.py`,
  `src/k5vision/operator_web/app.js`,
  `src/k5vision/data/mediamtx-donor-manifest.json`,
  `tests/unit/test_mediamtx_provider.py`,
  `tests/integration/test_mediamtx_donor_runtime.py`,
  `docs/WINDOWS_DONOR_MEDIA_BOUNDARY.md`, `THIRD_PARTY_NOTICES.md`, and
  `third_party/licenses/MediaMTX-MIT.txt`
- **Integration method:** separate native executable with fixed HTTP read APIs
  and private generated configuration. No donor binary is vendored. The local
  dashboard's HLS event wiring adapts the pinned MIT HLS page; its attribution
  is retained in `operator_web/app.js`. K5-owned adapter code follows the pinned
  upstream schema; the exact upstream license is copied unchanged for attribution.
- **Modifications:** none to donor source or release binaries. Opt-in K5
  normalization and configuration do not replace the current installed runtime
  or change accepted installer identities.
- **Purpose:** evaluate continuous enrolled-camera RTSP pull, reconnect,
  stream fan-out, fMP4 recording, age-based retention and playback inventory
  behind the canonical Windows media-worker boundary, and compose the separate
  foreground Windows operator pilot.
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

## OpenVINO / Open Model Zoo optional operator person view

- **Source:** https://github.com/openvinotoolkit/open_model_zoo
- **Version/revision:** model `person-detection-retail-0013` FP16 at
  `6697dead54ed1cdd664b0313189c2cb52ee6335e`; OpenVINO API/runtime `2026.3.1`,
  NumPy `2.2.6`, OpenVINO telemetry `2025.2.0`.
- **License/copyright:** model's official `model.yml` expressly links Apache-2.0;
  copyright (c) 2023-2024 Intel Corporation. OpenVINO source is Apache-2.0;
  NumPy source is BSD-3-Clause; exact platform/native/transitive wheel notice
  closure remains separate from the API/model source review.
- **K5 files affected:** `operator_analytics.py`, `operator_analytics_person.py`,
  `data/operator-person-model-manifest.json`, unit/native witness tests, and
  `docs/OPERATOR_PERSON_DETECTION_REVIEW.md`.
- **Integration/modifications:** independently written K5 adapter uses documented
  OpenVINO public APIs and exact local model bytes; no model or native binary is
  committed, bundled or downloaded during product startup. No restricted
  Analytics Lab detector, tracker or pose implementation is copied/imported.
  Existing K5 worker lifecycle is reused without invoking its Analytics Lab
  backend. No donor implementation source is modified.
- **Purpose/runtime:** optional native CPU view-only person boxes; missing config
  leaves inference disabled. MediaMTX recording remains independent. The
  transient view cannot produce source-attested events or unattended analytics.
- **Required notices:** full unchanged OMZ Apache-2.0 license at
  `src/k5vision/data/analytics-runtime-Apache-2.0.txt`, Git blob
  `261eeb9e9f8b2b4b0d119366dda99c6fd7d35c64`; preserve Intel attribution and
  all separately supplied runtime-wheel notices in any later redistribution.
- **Development image:** official Apache OMZ model example at the same revision,
  553,577 bytes, Git blob `de3f62ab7774b00d88d8658a142ea20993780323`, SHA-256
  `2be4dfa4d874e5f7929bebd1baf401e2ee630c4bf36cead7a2f8d5421d583959`.
  Ephemeral functional test only; no private cameras/data or image distribution.
- **Review/commercial conclusion:** Apache permits commercial use of its licensed
  material with required terms/notices. This closes the independent API/model
  artifact identity for an optional engineering candidate, not legal sign-off,
  model training-data provenance/suitability, native/transitive redistribution,
  commercial release or physical-camera/accuracy acceptance.
- **Reviewed by/on:** automated exact upstream model definition, documented
  interface, artifact hashes, source license and example Git identity inspection,
  2026-10-13. Real native execution is separately tested and reported per platform.
- **Integrity:** model definition Git blob `7faf63ff85b3d8692cfe8dc20a98d2e765e0da96`;
  README Git blob `3219ebf6ec2fe7ae954200b5f72a78b78c4cd663`; artifact size/SHA-384
  and immutable source references in the pinned manifest. Only its two exact
  LF/CRLF byte representations are admitted. Native model reads verify bounded
  immutable XML/BIN bytes and do not reopen paths after admission.
- **Replacement:** new model/revision/runtime requires new immutable manifest,
  interface/provenance review and native execution; no unreviewed fallback.
