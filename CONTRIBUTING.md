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

## External dependencies, donor code, and artifacts

Any PR that adds or changes an external dependency, donor source, or artifact must follow `docs/COMMERCIAL_DEPENDENCY_POLICY.md` and update `docs/DONOR_LEDGER.md` when source or substantial copied material is incorporated.

Preserve all upstream copyright, attribution, patent, and license notices. Do not relicense third-party material or remove rights granted by its original license.

Record only what is needed for review: component/version or commit, source, governing terms, files or functionality used, modifications, purpose, runtime/development use, notice obligations, distribution method, and commercial-use conclusion.

## Contribution rights

Do not submit code, documentation, media, models, datasets, or other material unless you have the legal right to contribute it.

Unless a separate written contributor agreement applies, contributors retain copyright in their own original contributions and grant the K5 Vision project owner a perpetual, worldwide, non-exclusive, royalty-free license to use, reproduce, modify, distribute, sublicense, and commercialize those contributions as part of K5 Vision.

Third-party material remains governed by its original license and must be identified as such. Submission of third-party material does not transfer its copyright to K5 Vision.

By opening a pull request, you represent that the contribution is either your own original work or properly identified third-party material that may lawfully be contributed under its governing terms.

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

K5-owned material is governed by the repository's current K5 Vision Source-Available License unless a file or directory states otherwise. Third-party material remains under its original license.
