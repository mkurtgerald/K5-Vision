# Native Windows beta sprint

Started: October 13, 2026. Baseline: qualified recording pilot, PR #337,
`b2567976723dfbc097e151560d7d3297a8d3f76f`.

## Product target

Deliver a local Windows camera/recording beta that a user can install, reopen,
operate and recover without developer intervention. The target is H.264 RTSP,
one Windows account, and a small camera installation. Windows stays signed in;
the current supervisor runs in a visible console. A signed EXE/MSI and a boot
service remain separate delivery work. No Docker, WSL or Linux service is part
of the installed product.

The planning target is **five working days**, with **five to ten working days**
as the current range. It requires access to the actual target Windows computer
and representative physical cameras on day one. This is a target, not a beta
completion claim. Revise it from the first field run rather than from the number
of tests or available compute.

Person detection proceeds independently. The existing Analytics Lab wrapper's
evaluation-only license cannot support a general product release. An independent
OpenVINO adapter uses the Apache-licensed OMZ person detector. It remains an
explicitly configured sampled-view candidate: no unattended detection, durable
events, camera attestation, person identity or model-accuracy claim.

## One acceptance gate, parallel implementation

The user requested a rapid, donor-first sprint. This sprint is one product beta
acceptance gate under `DELIVERY_GATES.md`; parallel lanes close independent parts
of that gate. They do not waive physical evidence, provenance or exact-revision
CI. Preserve the prior Alpha artifact identities.

| Lane | Concrete deliverable | Acceptance |
| --- | --- | --- |
| Windows deployment | Per-user immutable installation, Start/Desktop shortcuts, repeat installation, content-correct upgrades, profile-preserving uninstall, optional user logon shortcut | Actual PowerShell 5.1 installed-wheel lifecycle, safe refusals and unchanged state |
| Product reliability | Bounded worker health probes and recovery, recording/storage visibility, camera removal/restore, last usable administrator protection | Failure/recovery tests, concurrent admin guard, real media preservation after removal and restart |
| Browser operation | Real Edge workflow across two separate camera streams | Decoded pixels match each camera live and in recordings, independent outage, history, controls and logout |
| Person candidate | Independent model admission, native inference and bounded transient sampled view | Exact model/runtime identities, positive and blank inference, capacity/cancellation and authorization; field accuracy remains open |
| Field qualification | Representative cameras, measured capacity and endurance | Source-free receipts from the target Windows system; no mocked/short generated-media substitution |

The Windows workflow runs installation, startup, assembled recording, Edge and
native inference on **five independent runners**. It packages only after all
five lanes succeed and each lane proves its declared test executed. New commits
cancel obsolete sprint runs. This spends compute on shorter feedback cycles.

## Daily exit checks

1. **Day 1:** deliver the installable candidate and exact-revision CI evidence;
   validate clean installation on the target Windows computer and enroll the
   representative physical cameras. Record any stream/profile incompatibility.
2. **Day 2:** complete live/record/playback/logout and camera interruption/reconnect
   on those cameras. Correct RTSP/H.264 setup and recovery failures at their root.
3. **Day 3:** measure CPU, memory, disk throughput and recording growth at the
   intended resolution/frame rate and camera count. Verify quota/free-space pauses,
   recovery, repeat installation and the chosen user-logon behavior.
4. **Day 4:** run a 24-hour soak at the qualified capacity, including one source
   outage and one whole-product restart. Check gaps, timestamps and retained MP4
   playback. Extend to 72 hours if failures or drift appear.
5. **Day 5:** close attributable defects, rerun the affected acceptance, publish
   the exact qualified package and known operating limits. If any required field
   result is missing, keep the release a candidate and identify the blocking result.

Detailed field procedures and source-free reporting are in
[`WINDOWS_BETA_FIELD_TEST.md`](WINDOWS_BETA_FIELD_TEST.md). The target capacity is
the measured configuration, not an assumed number of cameras. Available compute
does not replace testing camera firmware or Windows behavior.

## Completion rules

Record status by observed outcome: implemented, locally verified, native Windows
verified, field verified. A green skipped integration test is not acceptance.
The recording beta requires the native workflow and repository-wide CI green
at the package's exact source revision, all required physical/soak receipts,
operating instructions and resolved product failures. Optional detection requires
its own field suitability and final native/transitive rights review before any
broader distribution claim.

Keep work in a reviewable branch stacked on the pilot. Publish the candidate and
its finite qualification evidence; do not merge or declare the beta complete
solely from this sprint's automated tests.
