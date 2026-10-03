# Explicit installed person-overlay composition

This engineering integration makes the already-qualified Analytics-lab person
detector/tracker reachable through the normal installed K5 operator application.
It is disabled unless the installation owner explicitly selects the local config
below. It does not grant production deployment or commercial redistribution
clearance. See [the packaging/provenance boundary](ANALYTICS_RUNTIME_PACKAGING.md).

## Installed inputs

- Install the K5 wheel and its optional `analytics` dependency extra. The four
  selected versions are OpenVINO `2026.3.1`, OpenCV headless `4.12.0.88`, NumPy
  `2.2.6` and OpenVINO telemetry `2025.2.0`; these match the existing Windows
  physical witness's resolved set. No telemetry API is called by this adapter.
- Separately install the locally built, exact-revision Analytics runtime wheel
  described in the packaging document. A source checkout or `PYTHONPATH` entry
  is not an installed payload and is rejected. K5 contains no copied detector
  or tracker implementation.
- Provision the existing four reviewed OMZ FP16 XML/BIN files separately, under
  their existing artifact-relative names. The unchanged Analytics artifact
  manifest binds exact sizes and SHA-384 values. No source, model or video is
  downloaded by app startup, provider construction, or inference.

Set `K5_ANALYTICS_CONFIG` in the process environment to an absolute local JSON
file containing exactly:

```json
{
  "schema_version": 1,
  "provider": "analytics-lab-omz-person-v1",
  "source_revision": "c8b347ae538991a0c0ce38eabc2dc17b566531d3",
  "artifact_root": "C:\\absolute\\reviewed-omz-artifacts"
}
```

Use an existing local directory. Relative paths, network paths, links/junctions,
unknown fields, duplicate JSON keys, unreviewed providers/revisions, and changed
package/model bytes fail admission. Windows paths also use the existing fixed
local-volume check; nothing changes ownership, permissions or system settings.
Unset the environment variable to disable this option. An empty or invalid
explicit setting is an error, never a silent fallback to disabled analytics.

The existing `k5-vision serve --operator` route and inherited launcher environment
reach this setting without new CLI flags. Package/dependency/model admission is
read-only and occurs before importing the eager default control-plane app or
creating configured application databases. Ordinary `serve` does not opt in.
The existing Alpha bootstrap still installs its pinned donor revision, and its
ball-pattern launcher is not a positive person-detection acceptance fixture.
This change does not claim that the Alpha shortcut already ships this candidate.

## Runtime and authority boundary

Normal human-session authentication, role/device admission, source resolution and
the existing live API remain in charge. The analytics provider receives only a
bounded transient decoded BGRX frame. It never receives a URI, camera identifier,
credential, user, recording destination or execution authority. Tracking IDs are
session-local association, not identity or re-identification.

Every live launch constructs its own detector and tracker after authorization.
Only the existing CPU OMZ configuration (`max_people=4`) is admitted. No module
names, URLs, thresholds, models or runtime plugins can be supplied by the config.
The existing overlay remains single-flight and fail-open for an individual slow
or failed inference, returning aggregate outcome counters through the existing
version-2 operator receipt. Explicit initialization failure fails that launch.

The adapter owns at most four native workers across concurrent camera launches.
Each worker owns at most one 16 MiB transient frame copy. Native construction has
a 15-second waiting bound; close waits at most two seconds. Python cannot forcibly
terminate an in-process native call: an unfinished call remains quarantined in
its capacity slot until it actually ends, and cleanup failure is not reported as
success. Reentry never queues more work on an interrupted session. Native errors
are converted inside the worker to scalar failure results so a retained Future
cannot keep raw error tracebacks and image locals alive. No frame, clip, source
identity or raw native error is written to files or receipts.

No source/recording policy changes here. Synthetic/public test launches remain
non-recording and disposable; this adapter adds no recording or retention to
private operation. Durable recovery identity remains a separate explicit path,
and its refusal of Alpha test sources remains intact.

## Acceptance still required

Portable tests cover default-disabled behavior, malformed/dependency/model
admission, real API authentication rejection, multi-camera isolation,
stop/relaunch, failed native worker cleanup, payload bounds and package identity.
They do not establish visible native presentation.

For exact installed-candidate acceptance, use the existing serialized physical
lane, its idle-host admission and reviewed loopback RTSP clip. Install both wheels
into a clean interpreter, clear checkout injection, configure local verified
artifacts, start the installed ordinary CLI/app, enroll/authenticate through the
normal APIs and launch live presentation. Require a same-execution receipt with
positive frames, Windows presentations, analytics completions and rendered boxes,
`analytics_enabled=true`, zero analytics failures, a second successful launch and
complete owned cleanup. Bind the exact K5 candidate, Analytics source manifest,
wrapper wheel and model/runtime identities in source-free scalar evidence.

Do not substitute the former test-only injected provider or combine unrelated
decode and detector receipts. Keep the existing scalar artifact contract and
privacy validation; no screenshots, recordings, source URI, credentials or user
paths are acceptance artifacts. Transactional upgrade/rollback, persistent
graphical setup, final installer delivery, accuracy and commercial shipping
remain separate gates.
