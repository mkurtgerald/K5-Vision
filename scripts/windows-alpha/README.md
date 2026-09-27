# K5 Vision Windows Alpha Bootstrap

This bootstrap provides a bounded Windows test path for the current reviewed K5 Vision alpha without requiring a local source checkout.

## Reviewed revision

`0fc10949a105357ff607a21866c5333f4d4be0c7`

The installer requires an exact 40-character Git revision and installs that revision from GitHub into an isolated user-local Python environment.

## Run

1. On a Windows x64 K5 test host with Python 3.12 available through `py.exe`, run `Install-K5VisionAlpha.ps1` from this directory.
2. Launch the created **K5 Vision Alpha** desktop shortcut.
3. Enter an uncredentialed RTSP URI using a literal camera IP address, then provide the camera credentials interactively.
4. The launcher creates an ephemeral K5 administrator/session, enrolls the selected camera, and invokes the existing Stage-One live operator path.

## Safety boundary

- The alpha launcher does not configure a recording root and does not intentionally write camera media to disk.
- Camera credentials are entered interactively and are not written to a credential file.
- Transient K5 device/user metadata and the GStreamer registry are created under `%TEMP%` and deleted when the launcher exits.
- The K5 control plane binds only to `127.0.0.1`.
- EdgeVMS results are not K5 acceptance evidence.

This remains an alpha bootstrap, not a signed MSI/EXE installer.
