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
