# K5 Vision Alpha — Windows owner test checklist

**Qualification status:** This checklist accompanies an installer candidate. An installer is **not owner-test-ready** until its exact revision passes clean Windows installation, launch, shortcut, dependency, upgrade/reinstall, uninstall, and external-public-RTSP rendered-box acceptance.

The owner workflow is graphical. Do not use PowerShell, Command Prompt, Git, Python, manual dependency installation, or environment-variable edits.

1. **Identify the build and download the installer directly.** The owner handoff must provide a direct `.exe` or `.msi` download associated with the exact approved 40-character source revision and a published SHA-256. An Actions `.zip` archive requiring extraction is an engineering artifact, **not** the owner handoff. Do not install a build labeled `unqualified`.
2. **Install.** On a clean, supported 64-bit Windows machine, double-click `K5VisionAlpha-Setup-<revision>.exe`. Follow the Windows installation dialog. The installer must install its private Python runtime, necessary dependencies, and GStreamer without separate setup.
3. **Launch.** Open **K5 Vision Alpha** from the desktop shortcut, then close and reopen it from the Start Menu. No terminal window or manual preparation should be required. Installation and launch failures must provide useful graphical messages.
4. **Synthetic source sanity check.** With the source field blank, choose **Run test**. This is a local, non-recording smoke test only; it does **not** qualify public RTSP or analytics overlay acceptance.
5. **External-public-RTSP test.** Enter a valid, credential-free, publicly reachable `rtsp://` stream and choose **Run test**. Never paste a private/home camera address, password, token, or credential-bearing URI. Confirm moving video and visible detection/tracking boxes in the operator view and the GUI's analytics/render/operator completion statuses. Any failure means the build is not ready.
6. **Privacy.** Confirm recording remains disabled and that no stream media, decoded frames, raw public source URI, or private camera configuration is retained.
7. **Reinstall or upgrade.** Close K5, run the newer approved setup by double-click, and confirm its GUI and both shortcuts work after an in-place upgrade. A failed upgrade must preserve the previous working runtime.
8. **Uninstall.** Use **Windows Settings → Apps → Installed apps → K5 Vision Alpha → Uninstall**. Confirm both shortcuts disappear and the K5-owned private runtime is removed without deleting unrelated user files.

**Engineering acceptance gate:** One external-public-RTSP run must show decoded frames, analytics submissions and completions, detections and tracked detections, rendered boxes, and operator presentations all greater than zero, with analytics failures zero. Installation and GUI source-status alone do not satisfy this gate.

**Signing:** An unsigned engineering test build may show a Windows publisher warning. This is separate from production code-signing readiness. Only distribute a build once its exact Windows and public-RTSP qualification evidence is linked.
