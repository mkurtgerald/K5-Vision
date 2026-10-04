# Installed Alpha Start-script engineering witness

`scripts/installed_alpha_launcher_witness.py` qualifies an **owned installed
layout running the actual candidate Start script** on Windows x64. It does not
run the installer, modify a real installation, exercise upgrade/shortcut behavior,
or establish production/distribution acceptance. It does not replace
`installed_analytics_witness.py` or its separately retained person-clip evidence.
The only shared-helper changes are explicit base-runtime/no-site relay admission,
a typed Win32 Job accounting structure, and a fixed dependency-download diagnostic
label; the original app command, provider, clip, receipt schema and media gates
remain unchanged.

The fixture is the launcher's existing generated ball. Success proves selected
Analytics admission and positive provider submissions/completions through two
real installed Start runs. It makes **no person-box claim**. Neither detections
nor rendered-box counters are acceptance requirements for this fixture.

## Prerequisites and ownership

The caller must coordinate the shared Windows lane and supply these already
prepared, immutable local inputs:

- Exact candidate Git revision and repository, with this controller and its
  candidate-matched common helper exported using `git -c core.autocrlf=false -c core.eol=lf
  archive`. Invoke the controller from that LF export, not a CRLF-converted copy
- Base CPython 3.12 x64, invoked with `-I -B`. A venv controller is refused. The
  caller independently binds `K5_WITNESS_BASE_PYTHON` and
  `K5_WITNESS_BASE_PYTHON_SHA256` to the admitted setup runtime, and every shared
  Job launch rechecks both against the current base interpreter and its bytes.
  Only the admitted base starts the relay, with `-I -B -S`, so neither a venv
  redirector nor site hooks can run before Job assignment
- The current candidate K5 wheel, whose complete `k5vision/` payload must exactly
  match the candidate's Git-exported `src/k5vision/` inventory
- Local Analytics source at `c8b347ae538991a0c0ce38eabc2dc17b566531d3`
- A prepopulated offline wheelhouse containing the exact reviewed Alpha runtime
  requirements plus `openvino==2026.3.1`,
  `opencv-python-headless==4.12.0.88`, `numpy==2.2.6`,
  `openvino-telemetry==2025.2.0`, `colorama==0.4.6`, and `pyreadline3==3.5.6`
- The reviewed local model/seed evidence cache required by the existing isolated
  installed probe. Model and seed identities must match independent admission
  values. The seed is admitted but is not this witness's transmitted media
- An existing LocalAppData cache with GStreamer 1.28.7 and MediaMTX 1.21.1 under
  `K5RunnerTools`. Both native trees, the pinned installer/archive markers, the
  executable marker, core DLL, launch/inspect executables, versions and required
  plugins are admitted. Missing or changed cache contents fail; this controller
  neither repairs nor provisions shared caches
- An admitted local Git executable, an existing temporary parent and an existing
  empty exclusively owned child directory outside the candidate checkout
- Output paths outside that owned child, and a fresh 32-hex run nonce

Temporary qualification provisioning, scheduling and any publication belong to
the caller. This controller never downloads packages, native code, models or
media. Installation uses only binary wheels with `--no-index --no-deps`, exact
versions and the admitted wheelhouse. The deterministic Analytics wrapper is
built twice from the exact donor archive with the existing reviewed builder.

The native trees are copied into **owned LocalAppData**, at the paths the
unmodified Start script actually uses. Complete inventory hashes and both
MediaMTX markers are checked before each valid launch, so its download/repair
branch must not be taken. The original cache is never deleted or modified.
Inherited credentials, real profile directories, proxy settings, application
state and source Python paths are not passed into any child. All profile,
registry, session and installed-layout outputs are disposable. No execution
policy, security setting, persistent real account or real database is changed.

## Input identity contract

Execution takes a strict scalar `--expectations` JSON object. It must have exactly
`revision` (40 lowercase hexadecimal characters), `run_nonce` (fresh 32 lowercase
hexadecimal characters) and these 12 nonzero SHA-256 fields:

- `source_tree_sha256`: `common.digest(tree_manifest(exact_candidate_export))`
- `k5_payload_sha256`: `common.digest(source_payload(exact_candidate_export))`
- `k5_wheel_sha256`: exact current K5 wheel bytes
- `analytics_manifest_sha256`: candidate
  `src/k5vision/data/analytics-runtime-manifest.json` bytes
- `analytics_wheel_sha256`: deterministic pinned wrapper wheel bytes
- `model_identity_sha256` and `seed_identity_sha256`: existing reviewed installed
  probe contracts, independently admitted before this proof
- `native_cache_sha256`: `common.digest(native_manifest(original_local_appdata))`
- `wheelhouse_sha256`: `common.digest(tree_manifest(offline_wheelhouse))`
- `start_script_sha256`, `test_script_sha256`, `run_script_sha256`: exact raw Git
  bytes of candidate `scripts/windows-alpha/{Start,Test,Run}-K5VisionAlpha.ps1`

The controller exposes `INPUT_IDENTITIES`, `native_manifest`, `tree_manifest` and
`source_payload` for caller preparation. `tree_manifest` is a relative POSIX-path
to content-SHA-256 map over every regular file, including unexpected additions.
Aliases/reparse points are rejected; file counts and total bytes are bounded.
`common.digest` is SHA-256 over sorted-key compact JSON. None of the input values
should be manufactured from a success receipt being validated.

Installed runtime RECORD identities may depend on the disposable venv's path.
The controller therefore writes a second, separate scalar expectation object to
`--admitted-expectations` immediately after the installed, isolated package/model
probe and **before** Start runs. This object adds `runtime_identity_sha256`, the
existing probe's verified installed-RECORD digest, to the original 14 fields.
The same installed probe must produce the same identities after both launches.

Keep the admitted expectation file under the caller's evidence custody. The
validator requires it explicitly and never uses the receipt as its own identity
baseline. These are auditable local engineering records, not cryptographic remote
attestation: a party able to replace both the receipt and its trusted expectation
file can fabricate evidence. The caller must also bind both files to the exact
candidate job and independently supplied nonce; reusing a nonce defeats replay
protection.

## Invocation

Use the admitted base interpreter and the LF-exported controller. The following
PowerShell variables denote caller-owned local paths, not machine defaults:

```powershell
& $BasePython -I -B $Controller `
  --expectations $AdmissionJson `
  --admitted-expectations $AdmittedExpectationsJson `
  --output $ReceiptJson `
  --repo $CandidateRepository `
  --analytics-source $PinnedAnalyticsRepository `
  --k5-wheel $CurrentCandidateWheel `
  --wheelhouse $OfflineWheelhouse `
  --evidence-root $ReviewedEvidenceRoot `
  --local-appdata $ExistingLocalAppData `
  --git $AdmittedGit `
  --temp-root $TemporaryParent `
  --work-root $ExclusiveEmptyWorkRoot
```

The required output basenames are:

- `installed-alpha-start-script-witness.json`
- `installed-alpha-start-script-expectations.json`

The work root is adopted only after ownership/path checks and is then removed,
including source exports, installed venv, temporary credentials/databases,
configuration, native copies and transient logs. The success receipt is written
only after complete cleanup. A failed retry clears earlier success output and
emits only fixed bounded diagnostics; it does not emit a partially successful
receipt. Failed preparation may leave the empty, not-yet-adopted root for its
caller to remove.

Validate separately:

```powershell
& $BasePython -I -B $Controller --validate-receipt `
  --expectations $AdmittedExpectationsJson --output $ReceiptJson
```

Validation is portable and requires no Windows/native execution. It rejects
extra/nested/duplicate fields, nonfinite values, oversized JSON, coercions,
zero/wrong identities, wrong candidate or nonce, missing repeated-run evidence,
any cleanup failure and any person-box/full-installer assertion. Do not substitute
this command for the native proof.

## Actual launch and negative boundary

Only a small owned PowerShell invocation envelope is generated. It invokes
`& $Start -Port $Port -ExitAfterPublicTest`; `$Start` is always the installed,
raw-byte-verified `Start-K5VisionAlpha.ps1`. It does not dot-source, modify,
intercept or reimplement Start. Test and Run are copied and verified as installed
files; this witness does not claim to execute those entry points or a shortcut.
No `PublicRtspSource` or `AnalyticsPreflightOnly` override is passed.

1. Run actual installed Start with an explicit invalid selected Analytics config
2. Require the launcher's exact fixed refusal and exit 23 from the envelope
3. A Windows directory-change notification must prove no transient session/file
   creation, including create-then-delete, under the owned session TEMP root
4. Kernel Job accounting must show exactly four total processes and zero active
   processes: base relay Python, PowerShell, the installed preflight venv
   redirector and its base interpreter. This deliberately CPython-3.12-specific
   negative-path contract excludes native probes, media and application children
5. Run the same actual installed Start twice with the explicit admitted config,
   generated ball and bounded exit. Pre-admit TCP control/8554 and UDP
   18000/18001; never stop another owner's listener or process
6. For each launch, require one admitted/synthetic/Analytics/operator/exit marker,
   exactly 225 delivered frames, at least 225 presentations, positive provider
   submissions/completions, completions no greater than submissions, zero
   Analytics failures, zero active owned children and no leftover session state
7. Verify package/model/seed identities, installed script/record bytes and native
   cache identity again, then remove the complete owned layout

Only fixed scalar markers are retained from bounded streaming stdout. Stderr is
classified using the existing bounded diagnostic collector, never printed or
stored. Child deadlines are finite. The shared Job helper assigns a base-Python
relay before opening its execution gate and owns all descendants. Cleanup is by
owned handles and owned directories, never process-name/path/port searches.

## Receipt scope and testing

The receipt has schema `installed-alpha-start-script-v1`,
`acceptance_scope=installed-layout-start-script-engineering-only`,
`fixture=generated-ball` and `person_box_acceptance=false`. It records the exact
candidate, Analytics revision, nonce, immutable identities, invalid-config
boundary booleans, both launches' scalar counters and cleanup. Its distinct file
and schema must remain separate from the existing Alpha and person-box baselines.

Portable tests cover forged/stale/oversized receipts, raw script identity,
source/package/cache/model mismatches, offline package command selection,
credential/source isolation, actual Start selection, invalid-first/repeated-run
ordering, Job structure offsets/descendants, transient directory notifications,
wrong refusal/exit states, collector limits and cleanup failures. Mocked Windows
and orchestration tests establish harness contracts only. Real Windows x64,
PowerShell, CPython redirector accounting, native cache/plugin admission and both
225-frame launches still require coordinated native execution at the exact final
candidate. Linux test success is not Windows acceptance.
