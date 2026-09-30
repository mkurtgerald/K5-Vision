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
