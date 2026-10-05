# Installed Alpha Test/Run facade engineering witness

## Boundary and current evidence

`scripts/installed_alpha_facade_witness.py` is a separate, bounded Windows x64
engineering controller. It prepares an owned offline installed layout using the
existing Start witness's admission/materialization helpers and executes:

1. The unmodified installed `Test-K5VisionAlpha.ps1` with invalid analytics config
2. The same actual Test script with the admitted valid analytics config
3. The unmodified installed `Run-K5VisionAlpha.ps1` twice, each invoking the actual
   installed Start script for one bounded generated-ball analytics launch

The product scripts, Start-v1 controller/receipt, PR319 wheel-provenance consumer,
and native workflows are unchanged. Hosted Windows Smoke adds only trigger paths,
portable contracts and the owned-Python observer test after runtime admission.
Test retains its exact qualified local
`Invoke-K5NativeProbe`; this controller introduces no product sibling dependency.
There is no copied facade implementation, dot-sourced replacement, AST-extracted
facade, product monkeypatch, public source URI, or real camera/storage input.

**Native qualification is pending.** Portable synthetic regression records check
validation logic only. The process inventories and event counts for successful
actual Test and Run have not been established by those tests. The finite limits
(32 process births, 256 TEMP events) are fail-closed resource ceilings, not claimed
observed success profiles. If an allocated native run exceeds a limit, review its
source-free failure at this boundary; do not silently expand acceptance.

This is not installation, upgrade, desktop shortcut, beta, public-RTSP, or
person-box acceptance. The generated ball proves provider/launcher continuity,
not person detections. No current delivery gate is unlocked by portable tests or
by this implementation alone.

## Admission and immutable inputs

The input expectations use all independently admitted Start-v1 identities plus
`facade_controller_sha256`. The controller retains their names and semantics:
revision and fresh run nonce; raw Git source tree; K5 package payload and wheel;
Analytics manifest and wheel; model/seed identities; installed runtime RECORD
identity; wheelhouse; owned native cache; separate raw Start, Test and Run SHA256s.
The installed runtime identity is added to the admitted expectations only after
the existing source-isolated installed probe returns it, then rebound on every
later probe. Wheel admission still delegates to the unchanged accepted helper.

Before materialization, a fresh raw Git export must match the independently
admitted source tree. The new controller and all three reused helper files must
match that export byte for byte. This binding is repeated at the end. The existing
materializer independently repeats its original checks. It builds/copies into an
explicit empty work root outside the repository, installs only admitted offline
wheels, and does not run the product installer or change access/ACL settings.
Provisioning and any permitted offline package-install execution belong to the
later allocated native run; neither was executed during local implementation.

The generated installed-probe driver is rebound to the exact candidate helper;
its input JSON and valid analytics config are independently reconstructed from
admitted source and input paths. Their exact bytes, the invalid config, and exact
probe argv/config-environment selection are checked before initial use and around
every subsequent probe and facade. A facade cannot weaken its own post-check by
rewriting those private authority files. The materialized raw source tree is also
rechecked. Stderr collectors are retained even if process construction fails, and
both stdout and stderr readers participate in the final quiescence gate.

Each facade boundary checks raw installed product scripts/revision/version,
original and owned cache, wheel and wheelhouse, and the controller. The existing
installed probe revalidates payload/runtime/model/seed before and after every
facade invocation. The tiny generated calling envelope is compared to its exact
controller-derived bytes before and after each call. Each invocation uses a new
PowerShell process and owned kill-on-close Windows Job. The working directory and
TEMP/profile/cache are owned and independent of the exported source tree.

## Observations and refusal

The accepted invalid-Start five-birth observer is unchanged. The new facade
observer reuses its completion-port, least-privilege process-handle, TEMP I/O,
cancellation-race draining, and guarded-assignment machinery with a separate
image policy:

- Invalid Test: admitted base/venv Python, PowerShell and console-host images
- Valid Test: those images plus the admitted GStreamer launch/version executable
- Run: those images plus GStreamer launch, inspect, plugin scanner and MediaMTX

Every image is bound by exact path and raw SHA256, then checked again after
execution. Every Job-reported birth must be observed and reconciled, including
short-lived children. Image inspection follows proof that the exact process is
owned by this Job; PID/name scans are never used. Unknown image, unavailable
birth, denied read, count overflow, or incomplete coverage aborts only that owned
Job and refuses acceptance. A process that exited before inspection is never
inferred from totals or silently classified. No permission escalation occurs.

The receipt retains observed class counts rather than asserting a guessed exact
success inventory. Valid Test genuinely runs analytics admission, GStreamer
version and multiple CLI probes. It makes no "zero native process" claim.

All TEMP events are consumed and bounded. The existing finite one-pair/two-pair
PowerShell policy-probe lifecycle must complete, with no ignored policy events.
Test refuses any alpha-session or unknown TEMP event. Run admits events only in
the known owned alpha-session subtree plus policy probes. The observer does not
attribute filesystem writes to particular processes. Acceptance requires a
complete post-Job TEMP drain and empty TEMP, not just an empty final directory.

The invalid Test receipt's no-session/no-media assertions depend on complete
process/TEMP observations plus the immutable actual Test/Start route and exact
config-refusal output/exit. They are never inferred from the product's printed
no-camera sentence. Valid Test's receipt claims observed no-session, not a broad
zero-state/no-media property. Run captures bounded existing Start counters and
requires 225 frames, at least 225 presentations, positive submissions/completions,
zero analytics failures, and clean exit on both launches.

The output collector never persists raw child output. Test stdout uses a strict
full fixed-line grammar; Run reuses the accepted bounded Start counter parser
and adds an exact returned-facade marker. Fresh gate exit evidence, source-free
failure enums, bounded collectors, zero active children before forced cleanup,
reader quiescence, and closed owned Jobs are required. Forced termination cannot
turn active survivors into successful evidence. Primary and cleanup diagnostics
are preserved separately if both fail. A live reader or incompletely closed Job
prevents deletion of its work root.

## Receipt and invocation

Success writes only `installed-alpha-facades-witness.json`, with schema
`installed-alpha-facades-v1` and scope
`owned-installed-test-run-facades-engineering-only`. It contains exact scalar
identities/flags/counters and four strictly typed source-free observation records.
No process IDs, paths, TEMP names, native streams, URIs, credentials, frames,
package listings or media are published. No success receipt is written on failure.
The admitted expectations are a separate file named
`installed-alpha-facades-expectations.json`.

Use the same CLI input paths as `installed_alpha_launcher_witness.py`, a fresh
separate nonce/work root/output, and the added independently generated controller
hash. Invoke with the admitted base runtime's `-I -B -S` flags. Independent receipt
validation is read-only:

```text
python -I -B -S scripts/installed_alpha_facade_witness.py --validate-receipt --expectations installed-alpha-facades-expectations.json --output installed-alpha-facades-witness.json
```

The expectation and receipt must be from the same exact reviewed candidate.
A source-tree change requires regenerating all candidate expectations; no current
Start-v1 expectation or receipt should be rewritten/reinterpreted as facade proof.

## Tests and deferred workflow wiring

- `tests/test_installed_alpha_facade_witness.py`: portable contract, negative,
  identity, forged receipt, event lifecycle, ownership, missing-birth, bound,
  source-free diagnostic, cleanup, raw controller-binding and sequencing tests
- `tests/test_windows_alpha_facade_witness.py::test_windows_facade_process_observer_real_owned_births`:
  hosted Windows low-level native observer check using admitted Python only;
  this is not Test/Run qualification and needs the existing base-runtime binding
- `tests/test_windows_alpha_facade_witness.py::test_windows_actual_installed_test_and_two_run_facades`:
  opt-in end-to-end real Test/Run test, requiring an explicitly allocated native
  lane and `K5_FACADE_NATIVE_INPUTS`, a private JSON object whose exact keys are
  `expectations`, `admitted_expectations`, `output`, `repo`, `analytics_source`,
  `k5_wheel`, `wheelhouse`, `evidence_root`, `local_appdata`, `git`, `temp_root`,
  and `work_root`; values are the already admitted existing CLI input paths

Both Windows cases remain unrun in the local Linux implementation environment.
The full native test may perform the controller's offline installation inside its
owned root; merely selecting hosted tests must never implicitly provision it.

Minimal native wiring, deliberately **not implemented here**:

1. Root reviews exact candidate, hosted selections and postmerge evidence, and
   allocates the shared native runner without canceling another owner
2. Preserve Start-v1 acceptance and PR319 provenance consumers unchanged
3. After existing Start receipt validation, prepare separate facade expectations,
   nonce and owned work root from the same independently admitted raw candidate
4. Recheck exact head and idle host, invoke the facade controller, then recheck
   owned-host cleanup and independently validate its distinct receipt
5. Upload only the validated source-free facade receipt; clean only its owned
   roots, preserving any roots blocked by live resources for bounded diagnosis

Current-main installed-candidate workflow does not include PR319's qualified
hosted-before-native admission split. Reconciling that orchestration, branch/path
admission, and runner allocation is separate root-reviewed work. Do not alter
labels, permissions, concurrency, or infer native authorization from hosted
selection. Facade success must not depend solely on the currently separate
provenance storage-ACL blocker. The included hosted-only Smoke selection does not
run the full installed facade witness or allocate the physical runner.

## Hardening applicability

Malformed inputs, strict types, source/provenance drift, unknown processes,
short-lived/missing births, denied ownership, output floods, timeout, duplicate
markers, unexpected TEMP activity, canceled drains, active survivors, cleanup
failure and repeated entry are covered by portable contracts and reused boundary
regressions. The new native profile still needs actual Windows qualification.
No new dependency, external source, dataset or model is introduced. Network,
installer/upgrade/shortcut, public input, real camera and storage acceptance are
outside this owned generated-fixture boundary and remain separate gates.
