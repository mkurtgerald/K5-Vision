# Stage 03 Qualification

This stage uses measured behavior to qualify a replaceable runtime behind project-owned contracts.

## Boundary

`RuntimeSample` schema version `1` records only qualification measurements. External implementation objects, source addresses, and credentials are not retained in the record. Unknown fields are rejected so source or credential data cannot be added accidentally to retained samples.

`QualificationPlan` bounds scored-run count and per-operation timeout. `QualificationResult` requires one candidate identity across every retained sample. The candidate boundary includes both normal measurement and an explicit interruption/re-entry measurement.

`CandidateReview` records the minimum commercial, redistribution, and supported-platform review required before a measured candidate may be ranked. `CandidateScore` is derived only from retained scored measurements. Candidate selection cannot succeed when review evidence is missing, recovery evidence fails, scored runs fail, or the candidate is not approved for the required distribution boundary.

`ResourceProfile` records finite CPU and memory observations at explicit increasing load levels. `Stage03Evidence` requires the same candidate set across qualification, review, and resource evidence, and requires every candidate to use one comparable load ladder. Final Stage 03 selection excludes incomplete resource profiles and uses the highest retained load point as a deterministic resource tie-break after latency.

## Reproducible method

For each candidate under review:

1. use the same validated input and host conditions;
2. use an explicit bounded timeout;
3. perform one warm-up run that is not scored;
4. capture at least five scored runs;
5. record startup time, latency, CPU use, memory use, bytes processed, completion, and recovery outcome;
6. perform and retain one interruption/re-entry case;
7. measure CPU and memory at the same increasing load levels used for every candidate;
8. retain raw samples so the result can be independently re-ranked;
9. attach the candidate's versioned dependency/distribution review before selection.

The project-owned qualification runner enforces the warm-up, scored-run minimum, bounded per-operation timeout, candidate identity consistency, and explicit recovery measurement. It does not retry implicitly after timeout or dependency failure. Timeouts, candidate failures, invalid samples, and incomplete selection evidence are exposed through stable sanitized failure classes.

Only candidates with complete successful scored runs, successful recovery evidence, complete comparable resource evidence, and an approved review are eligible for final Stage 03 selection. Qualification aggregates use median retained measurements. Final ordering uses latency, highest-load CPU use, highest-load memory use, median CPU use, median memory use, startup time, then a stable candidate identifier. This keeps selection tied to captured evidence rather than preference or a single successful run.

## Gate status

The qualification boundary, bounded execution method, evidence-gated selection rule, and comparable resource-evidence contract are defined. No runtime is selected by this document. Stage 03 remains open until real candidate measurements, real increasing-load resource evidence, completed candidate reviews, hardening evidence, and green CI are all recorded at the accepted revision.
