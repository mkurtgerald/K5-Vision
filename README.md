# K5 Vision

K5 Vision is an open security-sensor management platform designed to unify video, telemetry, analytics, AI-assisted search, and autonomous decision support across vendor boundaries.

## Product direction

K5 Vision is being developed in public during its early sprint phase with commercial deployment as an explicit design requirement. Public development does not mean "prototype-only": architecture, dependencies, testing, and interfaces should be chosen so the same codebase can mature into a commercially deployable product.

Core goals:

- Open-vendor camera and sensor integration
- ONVIF-oriented device onboarding and capability discovery
- Live and recorded video management
- Main/substream policy for bandwidth-aware viewing
- GPS/telemetry-aware mapping for mobile, body-worn, drone, and fixed sensors
- Computer-vision inference and event generation
- Natural-language investigation and search
- Agentic decision support with human-governed autonomy
- Edge, VM, Windows, Linux, and cloud-capable deployment patterns

## Architecture principle

The control plane is intentionally separated from the media and inference hot paths. Python/FastAPI is used for the initial typed control-plane API and orchestration surface; sustained media transport, recording, transcoding, and inference execution will be implemented behind stable interfaces and benchmarked before a native runtime is selected.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Development

Requirements: Python 3.12+

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"
pytest
uvicorn k5vision.main:app --reload
```

Then open `http://127.0.0.1:8000/docs`.

## Current sprint

Sprint 001 establishes the executable control-plane skeleton, domain model, CI quality gate, commercial dependency policy, and first device-registry API. See [docs/SPRINT_001.md](docs/SPRINT_001.md).

## License

K5 Vision is currently distributed under the MIT License. See [LICENSE](LICENSE).
