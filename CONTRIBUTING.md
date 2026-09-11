# Contributing to K5 Vision

K5 Vision is developed publicly with commercial use in mind. Public documentation should contain only the detail needed to implement, test, or review the current stage.

## Conduct

- Be respectful and professional.
- Prefer small, reviewable changes.
- Test before submitting a PR.
- Document public contract changes and lasting architecture decisions.
- Do not commit secrets, customer data, production credentials, or proprietary third-party source.
- Do not add forward-looking product-roadmap detail to public issues, PRs, comments, or docs unless it is required for the current gate.

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

The repository-wide coverage gate is 80% minimum. New behavior should test normal paths and meaningful failure states.

## Pull requests

Each PR should:
- deliver one coherent outcome
- link the relevant current-stage issue when appropriate
- keep unrelated refactors out of the change
- pass install, lint, format, and test CI checks
- document public contract changes
- complete the external dependency/IP checklist
- avoid unnecessary disclosure of future scope or product intent

Do not merge forward with a red quality gate.

## Strict precursor gating

Follow `docs/DELIVERY_GATES.md`.

The critical-path rule is absolute: **nothing downstream becomes active implementation work until its precursor is complete, accepted, and green.**

- keep one active critical-path gate at a time
- keep downstream issues as blocked planning items
- fix failures at the earliest layer that violates its contract
- add regression coverage before resuming forward work
- do not hide upstream defects with downstream special cases
- hardware-dependent gates require physical validation
- a mock or partial implementation does not unlock the next gate

## External dependencies and artifacts

Any PR that adds or changes an external dependency or artifact must follow `docs/COMMERCIAL_DEPENDENCY_POLICY.md`.

Record only what is needed for review: component/version, source, governing terms, purpose, runtime/development use, notice obligations, distribution method, and commercial-use conclusion.

## Architecture discipline

- implementation-specific objects stay behind adapters
- sustained high-throughput work stays out of the orchestration layer
- optional advanced processing cannot become a hidden dependency of baseline operation
- secrets must not enter the repository or ordinary logs
- lasting architecture choices require an ADR
- physical acceptance is required when the gate depends on physical integration

## Sprint workflow

Use short, independently testable increments. Efficiency is measured by stable completed capability—not simultaneous workstreams, files changed, commits, or issue count.

## Commit and push

```bash
git add .
git commit -m "feat: describe the engineering outcome"
git push origin feature/your-feature-name
```

## License

By contributing, you agree your contribution will be licensed under the repository's current MIT License unless a formally documented licensing change states otherwise.
