# K5 Vision Windows Alpha Bootstrap

This bootstrap provides a bounded Windows test path for the reviewed K5 Vision alpha without requiring a local source checkout after installation.

## Reviewed runtime revision

`2b2ef1a6d64bbc9df14271c95d2d0d14a19b7077`

## Run

1. On a Windows x64 K5 test host with Python 3.12 available through `py.exe`, run `Install-K5VisionAlpha.ps1` from this directory.
2. The installer provisions reviewed GStreamer, installs the pinned K5 revision into an isolated user-local environment, runs a camera-free preflight, and creates the **K5 Vision Alpha** desktop shortcut.
3. Launch the shortcut. It starts only on `127.0.0.1`, waits for `/api/v1/health`, then opens the local API surface.

## Safety boundary

- This bootstrap does not enroll or contact a camera.
- Recording is explicitly disabled by removing `K5_STAGE_ONE_RECORDING_ROOT`.
- Device/user databases and the GStreamer registry are temporary per-session files under `%TEMP%` and are deleted when the launcher exits.
- The control plane binds only to `127.0.0.1`.
- EdgeVMS results are not K5 acceptance evidence.

Physical live-camera acceptance remains a separate gate and must not reuse EdgeVMS evidence.

This remains an alpha bootstrap, not a signed MSI/EXE installer.
