# Native Windows beta sprint dependency review

Reviewed 2026-10-13 for engineering qualification under
[External Dependency and Artifact Policy](COMMERCIAL_DEPENDENCY_POLICY.md).
This records development execution and the existing official-download pilot
scope. It does not approve redistribution of platform binaries, browser assets,
native wheels or models in a commercial installer.

## Playwright browser acceptance

- **Component/pin:** Python `playwright==1.58.0`, installed only in the disposable
  browser/person acceptance environment. It is not a K5 runtime dependency.
- **Official sources:** [Python wrapper at v1.58.0](https://github.com/microsoft/playwright-python/tree/v1.58.0)
  and [Playwright driver at v1.58.0](https://github.com/microsoft/playwright/tree/v1.58.0).
  The exact package is obtained from its [PyPI release](https://pypi.org/project/playwright/1.58.0/).
- **License/attribution:** Apache-2.0; copyright Microsoft Corporation. The
  official [Python LICENSE](https://github.com/microsoft/playwright-python/blob/v1.58.0/LICENSE)
  has SHA-256 `7fab1461b41970ff376f1c9303a637076bfaaeb71cd12dd3a1c44aaf59a1a2b9`.
  The [driver LICENSE](https://github.com/microsoft/playwright/blob/v1.58.0/LICENSE)
  is also Apache-2.0. Retain supplied license/third-party notices in the installed
  development packages; no donor source is copied or adapted into K5.
- **Use:** launch the Windows machine's installed Microsoft Edge through
  `channel="msedge"` and exercise real operator/media behavior. Edge remains a
  separately installed Microsoft product governed by its own terms. The Windows
  gate does not download, bundle or redistribute a Playwright browser build or
  Edge. The receipt records the actual browser version; pinning Playwright does
  not pin that browser version.
- **Transitive development tools:** the Playwright wheel supplies its Node.js
  driver runtime. Node.js source has MIT terms plus separately attributed
  bundled components; see its [official license register](https://github.com/nodejs/node/blob/main/LICENSE).
  Python release metadata declares `pyee>=13,<14` and `greenlet>=3.1.1,<4.0.0`.
  [pyee](https://github.com/jfhbrook/pyee/blob/main/LICENSE) is MIT, copyright Josh
  Holbrook; [greenlet](https://github.com/python-greenlet/greenlet/blob/master/LICENSE)
  uses MIT for its own files and PSF terms for the identified Stackless-derived
  platform files. Preserve the packages' supplied notices. These ranges are
  upstream dependencies, not a claim of frozen transitive platform artifacts.
- **Review conclusion/replacement:** Apache-licensed browser automation is
  admitted for these engineering tests with unmodified external development
  packages. Node, pyee and greenlet are neither product dependencies nor included
  in the Windows operator package. Any later bundling requires exact transitive
  artifact, vulnerability and notice review. Replacing this driver changes the
  test harness and requires new native browser acceptance; it does not change
  the installed operator architecture.

## Reused media fixtures and native product workers

FFmpeg/ffprobe reuse the already reviewed Gyan `9.0.2` development artifact,
`ffmpeg-9.0.2-essentials_build.zip`, SHA-256
`60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba`.
The [exact manifest](../src/k5vision/data/mediamtx-donor-test-tools.json) and
[existing donor review](DONOR_LEDGER.md#ffmpegffprobe-native-windows-development-fixtures)
record official source/checksum links and GPL-3.0-or-later terms. These unmodified
separate processes generate synthetic H.264 streams and independently decode
test clips. Preserve the archive notices during development use. No FFmpeg
binary, archive or test media is distributed with the operator; this
review grants no product bundling or redistribution scope.

MediaMTX `v1.21.2` and Caddy `v2.11.7` remain under the existing pinned manifests
and [donor ledger](DONOR_LEDGER.md). MIT and Apache-2.0 notices remain in the
operator package. Their native executables are obtained from pinned official
releases at first launch, rather than bundled in the package. This sprint adds
no native-worker binary redistribution approval and does not change their
artifact identities or accepted download scope.

## Independent optional person inference

OpenVINO, NumPy, the official OMZ person model and its evaluation input have a
separate [exact-source/model review](OPERATOR_PERSON_DETECTION_REVIEW.md) and
manifest. Browser-driver or generated-media acceptance does not qualify model
rights, native/transitive wheel redistribution, training-data provenance,
accuracy, live-camera suitability or unattended analytics. Those boundaries
remain independent of this development-tool review.

## Evidence and cleanup

The [native beta workflow](../.github/workflows/operator-beta-windows.yml) retains
only allowlisted JUnit reports and finite validated JSON as qualification
evidence. Successful fixture receipts require owned browser, driver, product and
generated-source process cleanup. The workflow deletes its owned native-tool
directory even after failure; installed development packages remain only in the
disposable runner environment. No tool archive, driver/browser binary, model,
image, video, camera credential or private state is uploaded as evidence. The
separate qualified product package has its own explicit file allowlist and
contains no development tools or model weights.
