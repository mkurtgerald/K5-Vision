# K5 Vision Windows Alpha Bootstrap

This bootstrap provides a bounded Windows test path for the reviewed K5 Vision alpha without requiring a local source checkout after installation.

## Run

1. On a Windows x64 K5 test host with Python 3.12 available, run `Install-K5VisionAlpha.ps1` from this directory.
2. The installer provisions reviewed GStreamer, installs the exact pinned K5 revision into an isolated user-local environment, runs a camera-free preflight, and creates the **K5 Vision Alpha** desktop shortcut.
3. Launch the shortcut. By default K5 provisions a pinned MediaMTX binary, generates a local H.264 test pattern, binds the synthetic RTSP server only to `127.0.0.1:8554`, creates ephemeral device/user state, authenticates an ephemeral human operator, and launches the Windows live-operator path automatically.
4. A credential-free public RTSP stream remains available only as an explicit override with `Run-K5VisionAlpha.ps1 -PublicRtspSource "<rtsp-url>"`. Public-test mode rejects embedded credentials and any source that resolves to non-public address space.

## Safety boundary

- Recording is explicitly disabled for both local-synthetic and public-test modes; `K5_STAGE_ONE_RECORDING_ROOT` is removed before startup.
- Local synthetic mode accepts only credential-free RTSP on a literal loopback address and uses a test-only resolver separate from public/private camera resolution.
- MediaMTX is pinned to a reviewed version and SHA-256 verified before use. Its alpha configuration enables RTSP/TCP only on `127.0.0.1:8554`; RTMP, HLS, WebRTC, SRT, MoQ, API, metrics, pprof, and playback are disabled.
- The synthetic publisher uses a generated GStreamer test pattern. It does not contact a camera and does not read or write camera media.
- Public-test RTSP URIs may not contain credentials. The hostname must resolve only to globally routable addresses, and K5 pins the selected public IP into the runtime URI before media delivery.
- Device/user databases, the GStreamer registry, MediaMTX configuration, and synthetic session state are temporary per-session files under `%TEMP%` and are deleted when the launcher exits.
- The test uses the normal device enrollment, user bootstrap/login, and human-session-only live operator APIs; it does not bypass authorization.
- The K5 control plane binds only to `127.0.0.1`.
- No test-stream frames or recordings are retained.
- EdgeVMS results are not K5 acceptance evidence.

Physical private-camera acceptance remains a separate gate and does not reuse synthetic/public-test evidence.

This remains an alpha bootstrap, not a signed MSI/EXE installer.

## Upgrade and recovery

Close K5 before reinstalling. The installer refuses an active installed Python
runtime or a Python process whose executable cannot be identified; it does not
stop any process. Only one installer may operate on an installation at a time.

A replacement is downloaded into an isolated wheelhouse, installed into a
candidate environment, and checked by the camera-free preflight while the prior
installation remains untouched. Activation backs up only `.venv`, the three
launcher/preflight scripts, the two revision records, and the desktop shortcut
when requested. It recreates the venv at its final path using the verified local
wheels and repeats preflight before committing. Configuration and user data are
not moved or deleted. Existing installations do not reprovision shared GStreamer;
the requested GStreamer version must already be present and pass preflight.

A failed activation restores the previous managed paths and shortcut. A journal
allows a later invocation to finish recovery if the installer is interrupted or
a locked file prevents rollback. Close K5 and rerun the same installer with the
same install root and desktop location; do not delete the reported recovery
workspace. A committed upgrade may retain that workspace until a later retry can
clean it up. This is recoverable activation, not an atomic swap across all files:
do not launch K5 while upgrading. Unattended replacement of a running application
and native Windows upgrade acceptance remain separate qualification concerns.

The source and one-file bootstrap revision pins are unchanged by this repair.
The root one-file bootstrap still downloads the pre-transaction `d531d50` payload
and does **not** deliver this repair. Use of this source installer requires
`install_transaction.py` and the existing scripts from the same reviewed source
payload. The one-file delivery path needs a separate exact installer-payload pin
update after native Windows qualification; no helper is downloaded separately.
This repair does not qualify an installed analytics product or a production
deployment.

## Explicit supplied offline wheelhouse

The source installer also accepts a closed, independently reviewed wheelhouse.
All three arguments must be supplied together, with an explicit exact runtime
revision selected through the existing `-K5Revision` parameter:

```powershell
.\Install-K5VisionAlpha.ps1 -InstallRoot $OwnedInstallRoot `
  -K5Revision $ReviewedRuntimeRevision -SkipDesktopShortcut `
  -Wheelhouse $ReviewedWheelDirectory `
  -WheelhouseManifest $ReviewedManifestPath `
  -WheelhouseManifestSha256 $IndependentlyApprovedManifestSha256
```

These are review inputs, not values to discover from an untrusted neighboring
file. A matching hash authenticates bytes only when the expected digest came
from an independently approved record. Before approving that digest, establish
source provenance, licensing, host compatibility and the complete artifact
inventory. No qualified wheelhouse or manifest is supplied by this change.

The offline path has no index access, dependency resolver, source build, pip
upgrade download or online fallback. It verifies and copies the supplied wheels
into its owned staging directory, verifies the copies, and uses only explicit
local wheels with `--no-index --no-deps`. It never provisions GStreamer, even for
a fresh installation: an already reviewed runtime must be available where the
existing preflight expects it. Absence or mismatch fails staged preflight.
The legacy online command flow is unchanged when none of the offline arguments
is used; its open-ended pip/build inputs are not qualified by this offline contract.
The requirements now explicitly pin the observed Windows-only `pyreadline3`
dependency as described below.

### Manifest contract

Use UTF-8 JSON, at most 1 MiB, with no duplicate or additional keys. The top-level
keys are:

- `schema_version`: exactly `k5-alpha-wheelhouse-v1`
- `installer_revision`: the exact reviewed installer commit, recorded for the
  external provenance review; the helper verifies the complete listed bytes
- `runtime_revision`: the exact K5 commit matching `-K5Revision`
- `python`: exact `implementation`, three-part `version`, `platform`,
  `executable_sha256`, `ensurepip_version`, and `ensurepip_wheel_sha256`
- `installer_payload`: byte records for the seven fixed paths in the helper's
  `PAYLOAD_FILES`, including the wrapper, helper, launcher/preflight files,
  runtime requirements and GStreamer provisioner
- `runtime_payload`: every `k5vision/` file in the K5 wheel, independently compared
  against the exact runtime source; `k5vision/cli.py` is required
- `wheels`: the closed artifact list described below

Payload byte records have exactly `size` and lowercase `sha256` fields. Each
wheel entry has exactly `filename`, normalized `name`, exact `version`, expanded
`tags`, `size` and lowercase `sha256`. The expected digest covers the complete
manifest, binding the source revisions to those exact payload/artifact records.
A source-revision string alone is never sufficient provenance.

This initial contract admits only base CPython 3.12 on Windows x64 and its exact
executable and bundled ensurepip wheel identity. It requires precisely the 28
Windows versions in the hash-bound `runtime-requirements.txt`, K5 `0.1.0`, and
the identical pip wheel bundled with that admitted Python. The original 27 pins
are unchanged; `pyreadline3==3.5.6; sys_platform == "win32"` adds the one explicitly
reviewed Windows dependency. The wheelhouse must contain exactly those 30 wheels
and no other entry. Metadata, filename versions,
compatible Windows/pure-Python tags, paths, archive bounds and source payload
bytes are checked; links, reparse points, duplicate names and unexpected files
are refused. Keep wheelhouse, manifest and installer source outside the install
root. They must remain unchanged while installation runs.

The requirements readers recognize only this literal Windows marker, in addition
to unmarked exact pins; they do not resolve arbitrary transitive packages or
interpret general marker expressions. Offline admission selects the fixed Windows
target even when a portable source test runs on Linux. The callable
`install_transaction.runtime_versions(path, target_platform="win32")` reads the
same hash-bound inventory. An explicit Linux or macOS target omits that pin; such
targets do not extend the installer beyond its existing Windows x64 contract.

The clean Windows runtime install recorded on 2026-10-04 collected
`pyreadline3>=3.5.4` from `onvif-python==0.3.1` and installed `pyreadline3==3.5.6`.
Its successful install list contains the original 27 runtime versions plus that
single addition. The separately reviewed native analytics witness already pins
this version (and `colorama==0.4.6`), so its 33-version inventory is unchanged.
That witness's broader inventory is not the offline installer's wheel inventory.

The wheel inventory is closed before any install state is created. Wheel and
script bytes are checked again when staged and before activation. Dependency
constraints are checked by `pip check` in the staged environment; a separate
installed-distribution check requires exactly the admitted versions with no
extra or duplicate distribution. Those checks are repeated at the final path.
A failure leaves the previous installation unchanged or invokes the existing
journaled rollback. The existing lock, active/unknown Python refusal and
interrupted-recovery rules are unchanged.

This route consumes prebuilt artifacts. If a wheel must be built separately,
its Python, pip, backend and full build-dependency closure need separately
reviewed exact versions and hashes; do not obtain them from unconstrained live
resolution. Missing artifact hashes are a qualification blocker, not permission
to select a newer version or bypass admission.

### Qualification limits

Portable source tests use generated wheel metadata and mocked commands. They
exercise refusal, copy verification, command isolation and transaction recovery;
they do not execute supplied packages or establish native installer acceptance.
The platform regression models the observed onvif dependency with generated
metadata: the former 29-wheel inventory fails Windows closure, the Linux marker
is inactive, and the repaired 30-wheel inventory passes that modeled edge.
The install log is inventory evidence, not a complete audit of actual wheel
metadata, dependency closure, artifact hashes, licenses or redistribution rights.
Actual wheel archives were not available for that audit. Adding the existing
witness pin does not qualify or authorize retrieval or installation of artifacts.
Actual admitted Windows wheel inputs, native process/file/COM behavior and the
full installed upgrade boundary still require independent qualification.

Both default revision pins remain unchanged. The old default `d531d50` runtime
lacks `analytics-preflight`, which the current installed Test/Start scripts
require, so it fails current staged preflight. Use an explicitly qualified exact
runtime for this source-only qualification route. The root one-file bootstrap
still does not deliver this helper or its offline parameters; changing its
payload/runtime selection remains a separate reviewed delivery change.

## Capturing already-qualified wheel inputs

The existing installed-analytics qualification workflow has an explicit route
for the transactional-upgrade branch. Publishing on that route can queue its
existing physical job: coordinate the shared runner first. Admission still
requires the exact trusted repository/branch head and its three applicable
hosted checks, followed by the existing fresh host/port checks. The older route's
additional gates and the single non-cancelling job are preserved.

After both installed receipts validate, and before the existing temporary-input
cleanup, `capture_installer_wheel_provenance.py` reads the original 33 dependency
wheels, K5 and Analytics wheels, and admitted Python's bundled pip wheel. It
cross-checks receipt/source identities, exact archive hashes and metadata, then
checks the full 36-input dependency graph and the separate 30-wheel Windows
installer subset. The runtime package payload is pinned independently to the
136 tracked files at `2fa3133e892e633967e4734d363ee4431a43ac0d`; another source
payload requires a separate reviewed binding change. A compatible version label
or a locally rebuilt wheel is not a substitute for the qualified archive bytes.

The collector never runs pip, ensurepip bootstrap, a resolver, a source build,
package payload or media process. Dependency parsing is a bounded, documented
subset of the packaging specifications and refuses unsupported syntax. Raw
requirement marker strings are not published; source-free output contains exact
metadata hashes, normalized closure results and the bounded target environment.

Exact original wheels stay under a newly owned per-run namespace outside the
checkout and temporary cleanup roots:

`<verified runner work root>/k5-qualification-artifacts/K5-Vision/<run>-<attempt>-<revision>`

The directory is derived from live runner paths, with local-volume, path,
ownership, ACL, capacity and collision checks. No access settings change. Only
source-free JSON is uploaded; wheels, source trees, models, media, credentials
and account identifiers are not uploaded. Copies are verified before atomic
publication. A failure conservatively retains partial or uncertain state and
reports that cleanup is pending instead of deleting a possibly replaced path.
Do not adopt or delete an older or unrecognized bundle.

A local COMPLETE record certifies captured bytes only. Consumption still needs
successful outer workflow/cleanup evidence and fresh verification of every
selected archive. The recorded 30-wheel subset is an allowlist of the retained
originals, not an additional resolved or installed environment. Complete real
artifact provenance and availability remain prerequisites for native installer
qualification. JSON alone, generated test fixtures and hosted source tests do
not establish that the real installation or upgrade has passed.
