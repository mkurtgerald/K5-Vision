# K5 Vision

Early-stage systems project.

## Windows Alpha — Start Here

For the current Windows alpha test path, download and run `Install-K5VisionAlpha.ps1` from the repository root.

The bootstrap:
- downloads the exact reviewed K5 alpha revision;
- installs K5 into a user-local isolated environment;
- provisions the reviewed GStreamer runtime;
- creates the **K5 Vision Alpha** desktop shortcut;
- verifies the installed revision;
- removes its temporary bootstrap files.

Launching the desktop shortcut runs the current **non-recording public RTSP operator test** through the normal K5 authentication, device-enrollment, and live-operator APIs. Public test media is not retained.

This is still an alpha bootstrap, not the final signed MSI/EXE installer.

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

## Installed startup

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
