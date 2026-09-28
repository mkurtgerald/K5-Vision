# K5 Vision Windows Alpha Bootstrap

This bootstrap provides a bounded Windows test path for the reviewed K5 Vision alpha without requiring a local source checkout after installation.

## Run

1. On a Windows x64 K5 test host with Python 3.12 available through `py.exe`, run `Install-K5VisionAlpha.ps1` from this directory.
2. The installer provisions reviewed GStreamer, installs the exact pinned K5 revision into an isolated user-local environment, runs a camera-free preflight, and creates the **K5 Vision Alpha** desktop shortcut.
3. Launch the shortcut. K5 binds only to `127.0.0.1`, validates a credential-free public RTSP test source, creates ephemeral device/user state, authenticates an ephemeral human operator, and launches the Windows live-operator path automatically.
4. To override the current public test stream, run `Run-K5VisionAlpha.ps1 -PublicRtspSource "<rtsp-url>"`. Public-test mode rejects embedded credentials and any source that resolves to non-public address space.

## Safety boundary

- Public-test recording is explicitly disabled; `K5_STAGE_ONE_RECORDING_ROOT` is removed before startup.
- Public-test RTSP URIs may not contain credentials.
- The test hostname must resolve only to globally routable addresses. K5 pins the selected public IP into the runtime URI before media delivery.
- Device/user databases and the GStreamer registry are temporary per-session files under `%TEMP%` and are deleted when the launcher exits.
- The public test uses the normal device enrollment, user bootstrap/login, and human-session-only live operator APIs; it does not bypass authorization.
- The control plane binds only to `127.0.0.1`.
- No public-stream frames or recordings are retained.
- EdgeVMS results are not K5 acceptance evidence.

Physical private-camera acceptance remains a separate gate and does not reuse public-test evidence.

This remains an alpha bootstrap, not a signed MSI/EXE installer.
