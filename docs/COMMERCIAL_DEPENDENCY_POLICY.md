# Commercial Dependency Policy

K5 Vision is developed publicly with eventual commercial distribution as an explicit requirement. Dependency review therefore happens before adoption, not before launch.

This policy applies not only to source-code packages, but also to model weights, inference runtimes, SDKs, vector/search engines, training or evaluation datasets, sample assets, firmware-facing components, and other externally sourced artifacts that could affect commercial distribution rights.

## Default allowlist

Dependencies are normally acceptable when their governing license is permissive and commercial redistribution is compatible with K5's intended packaging, subject to notice/attribution requirements. Common examples include MIT, BSD-2-Clause, BSD-3-Clause, and Apache-2.0.

A permissive source-code license does **not** automatically clear separately licensed model weights, datasets, pretrained checkpoints, SDK binaries, or hosted-service terms.

## Requires explicit review

Do not merge or adopt a new dependency/artifact without explicit documented review when it uses or includes:
- GPL family licenses
- AGPL
- LGPL where linking/distribution details matter
- MPL/EPL/CDDL or other file-level reciprocal terms
- SSPL/BSL/source-available licenses
- Non-commercial, research-only, evaluation-only, field-of-use, or custom terms
- Model weights with separate, unclear, research-only, evaluation-only, or restricted use terms
- Training/evaluation datasets with unclear provenance, redistribution restrictions, or commercial-use restrictions
- SDKs requiring a commercial agreement, seat/runtime royalty, device fee, per-channel fee, or hosted-service agreement
- Biometric/face-recognition components whose code license and model/data license differ
- Components that prohibit redistribution of bundled binaries or weights required for normal operation

"Open source," "publicly downloadable," and "available on GitHub/Hugging Face" are not treated as synonyms for "commercially safe for our distribution model."

## Contribution requirement

Every PR that adds or changes a dependency, model, dataset, SDK, or externally sourced artifact must state:
1. Package/component/artifact and version or exact model/checkpoint identifier
2. Upstream project/source URL
3. License identifier for source code
4. Separate license/terms for model weights, data, binaries, or hosted service where applicable
5. Why it is needed
6. Runtime vs development/test-only use
7. Required notices/attribution
8. Whether it is linked, bundled, modified, redistributed, downloaded at runtime, or invoked externally
9. Commercial-use and redistribution conclusion
10. Replacement/exit strategy if the component becomes commercially unsuitable

For AI/CV and biometric components, additionally record:
- model provenance
- weight/checkpoint provenance
- training/evaluation dataset provenance when known/material
- commercial-use terms for all three
- any usage reporting, cloud dependency, royalties, or device/channel restrictions

## Biometric / face-recognition rule

Face recognition and biometric identity storage are planned commercial K5 capabilities. The shipping implementation must not rely on research-only or ambiguous model/data rights.

Detection, alignment, embedding, matching/indexing, vector search, liveness/quality components if used, and any pretrained weights must each be reviewed independently. A cleared face-recognition library does not clear its pretrained models automatically.

Third-party hosted recognition services may be supported as optional adapters, but K5 must retain a replaceable local/provider boundary and cannot make a single external provider mandatory for core product operation.

## Sprint 001 baseline

The initial runtime deliberately stays small: FastAPI, Pydantic, and Uvicorn. These were selected as permissively licensed foundations and should still be re-verified when versions are pinned for a release. Test/lint tooling is development-only and remains subject to the same review process.

## Release rule

Before any commercial release:
- Freeze dependencies and model/runtime versions
- Generate an SBOM for software components
- Maintain a model/artifact inventory with provenance and license status
- Run automated license/vulnerability scans
- Review transitive dependencies
- Review model-weight, dataset, SDK, binary, and hosted-service terms
- Produce third-party notices
- Resolve every unknown/custom/non-commercial finding

This document is engineering policy, not legal advice. Material licensing questions should receive qualified legal review before commercial distribution.
