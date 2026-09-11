# Contributing to K5 Vision

Thank you for contributing to K5 Vision. The project is developed publicly with commercial deployment as an explicit design requirement, so quality, dependency licensing, security hygiene, and stable interfaces are part of the engineering definition of done.

## Code of Conduct

- Be respectful and professional.
- Prefer small, reviewable changes over large mixed-purpose PRs.
- Test before submitting a PR.
- Document public APIs and lasting architecture decisions.
- Do not commit secrets, customer data, production credentials, or proprietary third-party source.

## Development setup

Requirements: Python 3.12+

```bash
git clone https://github.com/YOUR_USERNAME/K5-Vision.git
cd K5-Vision
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"
```

Create a focused branch:

```bash
git checkout -b feature/your-feature-name
```

## Required local quality gate

Run the same checks CI runs:

```bash
ruff check src tests
ruff format --check src tests
pytest
```

To apply Ruff formatting locally:

```bash
ruff format src tests
```

The repository-wide coverage gate is currently 80% minimum. New behavior should include tests for normal paths and meaningful failure states.

## Pull requests

Each PR should:

- Deliver one coherent outcome.
- Link the relevant issue when one exists.
- Keep unrelated refactors out of the change.
- Pass install, lint, format, and test CI checks.
- Document public API/contract changes.
- Complete the commercial/IP checklist in the PR template.

Do not merge forward with a red quality gate.

## Dependencies and commercial compatibility

K5 Vision is open source during its public sprint phase, but the product is intended for commercial distribution. Any PR that adds or changes a dependency must follow `docs/COMMERCIAL_DEPENDENCY_POLICY.md`.

At minimum, record:

1. Component and version range.
2. Upstream project URL.
3. License identifier.
4. Runtime vs development-only use.
5. Why the dependency is needed.
6. Any attribution/notice obligations.
7. Whether the dependency is linked, bundled, modified, or invoked externally.

Do not introduce non-commercial, research-only, field-of-use, unclear/custom, or reciprocal licensing obligations without explicit review.

## Architecture discipline

- Vendor-specific camera objects stay inside adapters.
- The Python control plane must not become the sustained video-frame/transcode hot path.
- Recording integrity must not depend on AI availability.
- Secrets must not enter the repository or logs.
- Lasting architecture choices require an ADR before they become difficult to reverse.
- A sprint is not complete merely because contracts or mocks exist; hardware-dependent acceptance requires hardware validation.

## Sprint workflow

K5 uses short, independently testable increments. Keep active implementation work narrow enough that failures are attributable and reversible. A new feature should not bypass a failing current gate simply to maintain velocity.

## Commit and push

```bash
git add .
git commit -m "feat: describe the outcome"
git push origin feature/your-feature-name
```

## License

By contributing, you agree your contribution will be licensed under the repository's current MIT License unless a formally documented project licensing change states otherwise.
