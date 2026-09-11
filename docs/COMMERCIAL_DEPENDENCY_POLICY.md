# Commercial Dependency Policy

K5 Vision is developed publicly with eventual commercial distribution as an explicit requirement. Dependency review therefore happens before adoption, not before launch.

## Default allowlist

Dependencies are normally acceptable when their governing license is permissive and commercial redistribution is compatible with K5's intended packaging, subject to notice/attribution requirements. Common examples include MIT, BSD-2-Clause, BSD-3-Clause, and Apache-2.0.

## Requires explicit review

Do not merge a new dependency without explicit documented review when it uses or includes:
- GPL family licenses
- AGPL
- LGPL where linking/distribution details matter
- MPL/EPL/CDDL or other file-level reciprocal terms
- SSPL/BSL/source-available licenses
- Non-commercial, research-only, evaluation-only, field-of-use, or custom terms
- Model weights/data with separate or unclear use restrictions
- SDKs requiring a commercial agreement

"Open source" is not treated as a synonym for "commercially safe for our distribution model."

## Contribution requirement

Every PR that adds or changes a dependency must state:
1. Package/component and version range
2. Upstream project URL
3. License identifier
4. Why it is needed
5. Runtime vs development-only use
6. Required notices/attribution
7. Whether it is linked, bundled, modified, or invoked externally

## Sprint 001 baseline

The initial runtime deliberately stays small: FastAPI, Pydantic, and Uvicorn. These were selected as permissively licensed foundations and should still be re-verified when versions are pinned for a release. Test/lint tooling is development-only and remains subject to the same review process.

## Release rule

Before any commercial release:
- Freeze dependencies
- Generate an SBOM
- Run automated license/vulnerability scans
- Review transitive dependencies
- Produce third-party notices
- Resolve every unknown/custom/non-commercial finding

This document is engineering policy, not legal advice. Material licensing questions should receive qualified legal review before commercial distribution.
