# Stage 03 Qualification

This stage uses measured behavior to qualify a replaceable runtime behind project-owned contracts.

## Boundary

`RuntimeSample` schema version `1` records only qualification measurements. External implementation objects, source addresses, and credentials are not retained in the record. Unknown fields are rejected so source or credential data cannot be added accidentally to retained samples.

`QualificationPlan` bounds scored-run count and per-operation timeout. `QualificationResult` requires one candidate identity across every retained sample. The candidate boundary includes both normal measurement and an explicit interruption/re-entry measurement.

## Reproducible method

For each candidate under review:

1. use the same validated input and host conditions;
2. use an explicit bounded timeout;
3. perform one warm-up run that is not scored;
4. capture at least five scored runs;
5. record startup time, latency, CPU use, memory use, bytes processed, completion, and recovery outcome;
6. perform and retain one interruption/re-entry case;
7. retain raw samples so the result can be independently re-ranked.

The project-owned qualification runner enforces the warm-up, scored-run minimum, bounded per-operation timeout, candidate identity consistency, and explicit recovery measurement. It does not retry implicitly after timeout or dependency failure. Timeouts, candidate failures, and invalid samples are exposed through stable sanitized failure classes.

Only completed runs that recover cleanly are eligible for ranking. Eligible samples are ordered by captured latency, CPU use, memory use, startup time, then a stable candidate identifier. A runtime is not accepted from preference or a single successful run.

## Gate status

The qualification boundary and bounded execution method are now defined. No runtime is selected by this document. Stage 03 remains open until real candidate measurements, recovery evidence, dependency review, applicable resource testing, hardening evidence, and green CI are all recorded at the accepted revision.
