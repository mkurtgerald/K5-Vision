# Stage 03 Qualification

This stage uses measured behavior to qualify a replaceable runtime behind project-owned contracts.

## Boundary

`RuntimeSample` schema version `1` records only qualification measurements. External implementation objects, source addresses, and credentials are not retained in the record. Unknown fields are rejected so source or credential data cannot be added accidentally to retained samples.

`QualificationPlan` bounds scored-run count and per-operation timeout. `QualificationResult` requires one candidate identity across every retained sample. The candidate boundary includes both normal measurement and an explicit interruption/re-entry measurement.

`CandidateReview` records the minimum commercial, redistribution, and supported-platform review required before a measured candidate may be ranked. `CandidateScore` is derived only from retained scored measurements. Candidate selection cannot succeed when review evidence is missing, recovery evidence fails, scored runs fail, or the candidate is not approved for the required distribution boundary.

`ResourceProfile` records finite CPU and memory observations at explicit increasing load levels. `Stage03Evidence` schema version `2` requires a `QualificationContext`, the same candidate set across qualification, review, and resource evidence, and one comparable load ladder. The context retains only SHA-256 input and host fingerprints, platform/architecture, and the bounded qualification plan. Source addresses, credentials, and descriptive host details are not part of the retained context.

The retained plan is authoritative for the evidence bundle: every candidate result must contain exactly the configured number of scored runs. This prevents evidence captured with different run counts or timeout assumptions from being treated as one comparable qualification set.

`Stage03SelectionRecord` schema version `1` binds the deterministic winning candidate to SHA-256 digests of the complete retained evidence and the derived ordered ranking. Candidate collections are normalized by stable identifier before evidence hashing so equivalent bundles do not acquire different identities merely from list ordering. Any retained measurement or review change produces a different evidence identity.

Final selection also requires comparative evidence from at least two candidates. A single candidate may be retained and inspected while evidence is being assembled, but it cannot produce a final selection or selection record by itself.

## Reproducible method

For each candidate under review:

1. use the same validated input and host conditions;
2. retain safe SHA-256 fingerprints for that input and host configuration;
3. use the same explicit bounded qualification plan;
4. perform one warm-up run that is not scored;
5. capture at least five scored runs;
6. record startup time, latency, CPU use, memory use, bytes processed, completion, and recovery outcome;
7. perform and retain one interruption/re-entry case;
8. measure CPU and memory at the same increasing load levels used for every candidate;
9. retain raw samples so the result can be independently re-ranked;
10. attach the candidate's versioned dependency/distribution review before selection;
11. retain the final selection record alongside the exact evidence bundle it identifies.

The project-owned qualification runner enforces the warm-up, scored-run minimum, bounded per-operation timeout, candidate identity consistency, and explicit recovery measurement. The evidence contract additionally binds all candidate results to one retained plan and safe host/input fingerprints. It does not retry implicitly after timeout or dependency failure. Timeouts, candidate failures, invalid samples, and incomplete selection evidence are exposed through stable sanitized failure classes.

Only candidates with complete successful scored runs, successful recovery evidence, complete comparable resource evidence, and an approved review are eligible for final Stage 03 selection. Qualification aggregates use median retained measurements. Final ordering uses latency, highest-load CPU use, highest-load memory use, median CPU use, median memory use, startup time, then a stable candidate identifier. This keeps selection tied to captured evidence rather than preference or a single successful run.

The final selection record is derived only after eligible comparative evidence exists. Its evidence digest and ranking digest make the acceptance decision independently re-checkable against the retained record rather than relying on a mutable summary or prose claim.

## Gate status

The qualification boundary, bounded execution method, evidence-gated selection rule, comparable resource-evidence contract, reproducibility context, comparative-selection guard, and tamper-evident final-selection record are defined. No runtime is selected by this document. Stage 03 remains open until real candidate measurements, real increasing-load resource evidence, completed candidate reviews, hardening evidence, and green CI are all recorded at the accepted revision.
