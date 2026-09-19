# K5 Vision

Early-stage systems project.

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

## Contributions

See `CONTRIBUTING.md` before opening a pull request.

## Security

See `SECURITY.md`. Do not disclose credentials, protected data, private infrastructure details, or exploitable vulnerability details in public issues.

## Licensing

K5-owned material is governed by the repository license unless a file or directory states otherwise. Third-party material remains governed by its original license and required notices.
