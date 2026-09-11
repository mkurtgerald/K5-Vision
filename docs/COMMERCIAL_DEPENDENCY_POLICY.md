# External Dependency and Artifact Policy

K5 Vision is developed publicly with commercial use as a design requirement. Review happens before adoption, not at release time.

This policy applies to source packages, donor source, runtimes, SDKs, binaries, models/artifacts, indexes, datasets, firmware-facing components, hosted services, and other externally sourced material that can affect distribution rights or operational dependence.

## Normally acceptable

Permissively licensed components may be acceptable when commercial use and redistribution are compatible with the intended packaging and all notice/attribution obligations are understood. Examples commonly include MIT, BSD-2-Clause, BSD-3-Clause, ISC, and Apache-2.0.

A permissive source-code license does not automatically clear separately licensed assets, binaries, data, artifacts, model weights, or service terms.

Permissive donor material retains its original copyright and license. Incorporating donor material does not convert it into K5-owned code and does not place surrounding K5-owned work under the donor license unless the governing donor terms require that result.

## Requires explicit review

Do not adopt material with unreviewed:
- reciprocal/copyleft obligations
- source-available or custom terms
- non-commercial, research-only, evaluation-only, or field-of-use restrictions
- separately licensed assets/artifacts
- unclear data or source provenance
- redistribution restrictions
- mandatory royalties, usage reporting, device/channel fees, or hosted-service dependence
- terms that make replacement or migration impractical
- missing or ambiguous license terms

Public availability is not treated as evidence of commercial suitability or permission to copy.

## Donor-source requirement

Any PR that copies, adapts, vendors, or substantially incorporates third-party source must update `docs/DONOR_LEDGER.md` before merge.

The donor record must identify the canonical source, immutable version or commit where available, governing license, upstream copyright, K5 files affected, integration method, modifications, notice obligations, commercial-use conclusion, reviewer, review date, and an integrity reference when practical.

Required copyright, attribution, patent, and license notices must be preserved. Distribution-facing notices belong in `THIRD_PARTY_NOTICES.md` or a clearly identified third-party notice file.

## Contribution requirement

A PR that adds or changes an external dependency/artifact must record, as applicable:
1. component/artifact and version or exact identifier
2. upstream/source location
3. governing license/terms
4. purpose
5. runtime vs development/test use
6. required notices/attribution
7. how it is linked, bundled, modified, redistributed, downloaded, or invoked
8. commercial-use/redistribution conclusion
9. known provenance considerations
10. replacement/exit strategy when material to architecture

## Release rule

Before commercial release:
- freeze dependency and artifact versions
- generate an SBOM where applicable
- maintain an external-artifact inventory
- run license and vulnerability review
- review transitive dependencies
- produce required third-party notices
- reconcile the donor ledger against the shipped source and artifacts
- resolve unknown, restricted, or commercially unsuitable findings

This is an engineering policy, not legal advice. Material licensing questions should receive qualified legal review before commercial distribution.
