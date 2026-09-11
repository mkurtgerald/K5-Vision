# K5 Vision

K5 Vision is an early-stage systems project developed publicly with commercial use in mind.

Public documentation intentionally exposes only the detail required to implement, test, harden, and review the current delivery stage.

## Delivery discipline

K5 Vision uses strict precursor gating: downstream implementation does not begin until its precursor is complete, accepted, hardened, and green.

See:
- `docs/DELIVERY_GATES.md`
- `docs/HARDENING_STANDARD.md`
- `docs/COMMERCIAL_DEPENDENCY_POLICY.md`
- `docs/DONOR_LEDGER.md`

Hardening is not a final cleanup phase. Every active stage must prove relevant negative-path, recovery, security, resource, compatibility, observability, and regression behavior before the next stage is unlocked.

## Development

Requirements: Python 3.12+

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"
ruff check src tests
ruff format --check src tests
pytest
```

## Contributions

See `CONTRIBUTING.md` before opening a pull request. Contributions must respect the active delivery gate, applicable hardening requirements, project-owned architecture boundaries, and third-party licensing/provenance rules.

## Security

See `SECURITY.md`. Do not disclose credentials, protected data, private infrastructure details, or exploitable vulnerability details in public issues.

## Licensing

K5-owned material is governed by the repository's current K5 Vision Source-Available License unless a file or directory states otherwise. Third-party material remains governed by its original license and must retain required notices and attribution.
