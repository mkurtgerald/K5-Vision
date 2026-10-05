# Installed Alpha Test/Run facade engineering witness

## Boundary and current evidence

`scripts/installed_alpha_facade_witness.py` is a separate, bounded Windows x64
engineering controller. It prepares an owned offline installed layout using the
existing Start witness's admission/materialization helpers and executes:

1. The unmodified installed `Test-K5VisionAlpha.ps1` with invalid analytics config
2. The same actual Test script with the admitted valid analytics config
3. The unmodified installed `Run-K5VisionAlpha.ps1` twice, each invoking the actual
   installed Start script for one bounded generated-ball analytics launch

The product scripts, Start-v1 controller/receipt and PR319 wheel-provenance
consumer are unchanged. The candidate native workflow adds a separate facade
phase after existing normal-app and Start-v1 validation. Hosted Windows Smoke
selects portable contracts and the owned-Python observer after runtime admission;
it does not execute the installed Test/Run controller.
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
`installed-alpha-facades-expectations.json`. Their exact schema contains only
nonzero lowercase hexadecimal hashes, the exact revision and run nonce. After
independent validation and owned cleanup, the workflow retains this file in a
separate source-free artifact so receipt consumers can validate against the
caller-held authority rather than deriving expectations from the receipt.

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

## Tests and native workflow wiring

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

The hosted owned-Python check is a low-level observer qualification, not actual
Test/Run evidence. The full native Test/Run witness remains pending. Neither
Windows case was run in this local Linux workflow-wiring environment. The full
native controller may perform its offline installation inside its owned root;
merely selecting hosted tests must never implicitly provision that layout.

`.github/workflows/installed-analytics-candidate.yml` now has two jobs:

1. A read-only `windows-latest` hosted admission job, bounded to 10 minutes with
   an 8-minute polling budget inside its 9-minute step. It checks exact trusted
   repository/branch/SHA and successful matching pull-request workflow runs
2. The existing physical job, bounded to 25 minutes, requires that job's success
   and exact `qualified_sha` output. Its first step revalidates the same hosted
   gates and branch once, immediately before any native checkout/provisioning

Only the existing two trusted branches plus
`feat/installed-alpha-facade-witness-20261005` are admitted. The legacy branch
retains all five hosted gates; the launcher and facade branches require CI,
Windows Alpha Script Smoke and PR Run Dedupe. The existing runner labels,
contents/actions read permissions and `stage-one-operator-physical` concurrency
identity with `cancel-in-progress: false` are unchanged. Concurrency now applies
to the physical job so hosted waiting cannot occupy the native lane. This is not
a cross-repository lock; publication still needs a fresh coordinated lane check.
A matching push can automatically queue the native job once hosted checks pass.
There is no new main, manual, reusable or pull-request native trigger.

The facade branch runs the existing normal-app and Start-v1 phases first. After
Start receipt validation, a separate preparation step takes the independently
prepared initial Start input identities, verifies the raw-export tree again,
adds the exact raw-export facade-controller hash and creates a new nonce. It
never reads a success receipt for input authority. The new facade controller
independently admits its own installed runtime identity through the unchanged
source-isolated probe. Initial/admitted facade expectations and output have
separate names and live outside its fresh owned work root.

The workflow rechecks exact head and idle host immediately before the facade
controller. It invokes the controller's full actual Test-invalid/Test-valid/Run-
twice CLI, rechecks owned-host cleanup, binds every admitted identity to the
caller-held initial expectations, and independently validates the distinct
receipt. After successful cleanup it uploads only the validated facade receipt
and, separately, its hash-only admitted expectations, with 14-day retention.
No raw source, logs, paths, credentials or media are included in either artifact.
Normal-app and Start-v1 receipts retain their separate existing validators.

Outer cleanup never deletes a preserved Start or facade controller work root.
A remaining work root, ownership mismatch or denied inspection fails closed and
preserves all owned fixture/controller/input roots for diagnosis. Only the
controller's own successful removal proves its collectors and Jobs drained;
outer `Remove-Item` cannot bypass that guard. A canceled or failed prelaunch step
can conservatively retain even an unused work root. No existing process is
terminated or shared cache removed to force admission.

Workflow regressions cover missing/failed/stale hosted outputs, trusted-branch
admission, exact-head drift, independent hash-only preparation and validation,
retained-work cleanup refusal, exact CLI/ordering and source-free artifact
allowlists. Windows-only harnesses execute the actual inline PowerShell with
mocked API responses and disposable directories, without running native media.
They remain pending on the changed exact candidate until hosted Windows runs.
All actual installed Test/Run qualification remains pending until a separately
coordinated native run succeeds. No installer, upgrade, storage ACL or PR319
provenance step/branch was imported; facade success has no storage-ACL dependency.

## Hardening applicability

Malformed inputs, strict types, source/provenance drift, unknown processes,
short-lived/missing births, denied ownership, output floods, timeout, duplicate
markers, unexpected TEMP activity, canceled drains, active survivors, cleanup
failure and repeated entry are covered by portable contracts and reused boundary
regressions. The new native profile still needs actual Windows qualification.
No new dependency, external source, dataset or model is introduced. Network,
installer/upgrade/shortcut, public input, real camera and storage acceptance are
outside this owned generated-fixture boundary and remain separate gates.
