# K5 Vision

Native Windows surveillance with ONVIF and direct RTSP cameras, recording,
continuous UTC playback, verified MP4 exports and local person detection.

## Windows beta

Download **K5VisionSetup.exe** from the matching
[public release](https://github.com/mkurtgerald/K5-Vision/releases) and double-click
it. End users do not need PowerShell, Command Prompt, external Python, Docker,
WSL or a separate model installation. Choose **Stop and quit** from the K5 tray
menu before upgrading. Recording runs while the Windows user is signed in;
first launch needs Internet to obtain the verified native media workers.

The workspace provides camera discovery/enrollment, a camera wall, recording
controls, a zoomable UTC timeline, direct seeking, playback controls, normal MP4
downloads, and background person events that open their recorded footage.
Administration manages local users, roles, camera access, activation/reset and
account activity. See the [desktop guide](docs/WINDOWS_DESKTOP_BETA.md).

Expanded administration in this source branch includes storage quota/reserve/
retention settings and verified copy-first directory migration, Windows Time and
ONVIF time controls, comprehensive activity downloads, constrained custom roles,
TOTP MFA, and OIDC sign-in linked to existing accounts. The
[settings guide](docs/ADMIN_SETTINGS_WORKSPACE.md) explains the graphical controls.
Feature availability in an installer is established by its matching release
notes and exact-source Windows receipts; a source change alone is not a
qualified download.

Analytics Lab 0.1 ingestion and K5-Lab v2 observation/response adapters use scoped
machine credentials and durable delivery. The Neural adapter is non-authorizing:
a connected provider is required, and K5-Lab currently supplies no production
shared Neural host. Saving a connection does not establish model execution.

Native release gates exercise the installed GUI, bundled inference, saved MP4s,
restart persistence and actual +/-48-hour host clock corrections. Time
administration has a separate native configuration/restoration witness. Source
configuration, observed offset and successful synchronization are reported
separately; clock-step resilience alone does not certify absolute UTC accuracy.

## Development

Requirements: Python 3.12+

```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"
ruff check src tests
ruff format --check src tests
pytest
```

## Engineering startup

The older [Alpha bootstrap](scripts/windows-alpha/README.md) and source commands
below are engineering paths. The normal Windows product uses the EXE above.


`k5-vision serve` starts the local control plane on loopback by default.
`k5-vision serve --operator` selects the existing Stage-One operator application
factory. It uses the same authenticated application composition and private runtime
configuration; selecting it does not enroll a source, start a media session, or bypass
authorization. Missing private runtime configuration remains fail closed.

## Contributions

See `CONTRIBUTING.md` before opening a pull request.

## Security

See `SECURITY.md`. Do not disclose credentials, protected data, private infrastructure details, or exploitable vulnerability details in public issues.

## Licensing

K5-owned material is governed by the repository license unless a file or directory states otherwise. Third-party material remains governed by its original license and required notices.
