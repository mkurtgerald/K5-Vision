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

No donor source has been recorded in this ledger yet.

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
