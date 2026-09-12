# Stage 03 Dependency Review

This record covers the external package introduced only to measure bounded candidate-process resource use during the active qualification gate.

## psutil 7.2.2

- Component: `psutil` 7.2.2
- Canonical source: `https://github.com/giampaolo/psutil`
- Package source: `https://pypi.org/project/psutil/7.2.2/`
- Governing license: BSD-3-Clause
- Purpose: cross-platform child-process CPU, resident-memory, and available process-I/O observation for the Stage 03 qualification harness
- Use: runtime library imported by K5-owned measurement code; no psutil source is copied or modified
- Platforms required by this stage: Windows and Linux are supported upstream
- Linking/bundling: normal Python dependency; distribution must retain the applicable BSD notice when the dependency is redistributed
- Commercial-use conclusion: approved for this bounded use under the project dependency policy, subject to preservation of upstream license/notice obligations
- Redistribution conclusion: approved for normal redistribution subject to BSD-3-Clause conditions and release-time dependency/SBOM reconciliation
- Provenance: upstream project and PyPI release metadata identify psutil as the package source; version is pinned for reproducible qualification behavior
- Replacement strategy: isolated behind the K5-owned process measurement adapter; it can be replaced without changing the Stage 03 evidence contracts
- Review date: 2026-09-12

This review covers psutil only. It does not approve any candidate process, candidate binary, codec, plugin, artifact, or its separate distribution terms. Each candidate still requires its own `CandidateReview` before Stage 03 selection.
