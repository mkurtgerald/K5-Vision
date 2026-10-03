# Pinned Analytics engineering runtime

K5 orchestrates the separately owned Analytics Lab implementation through an
installed package. Detector, tracking, pose and temporal business logic stays in
`mkurtgerald/Analytics-lab`; it is not copied into the K5 source tree. This wrapper
closes package identity and offline installation boundaries for engineering
qualification. It does **not** approve production, commercial distribution,
model redistribution, native binary redistribution or detector accuracy.

## Immutable input and ownership

- Source: <https://github.com/mkurtgerald/Analytics-lab/tree/c8b347ae538991a0c0ce38eabc2dc17b566531d3>
- Full revision: `c8b347ae538991a0c0ce38eabc2dc17b566531d3`
- Installed distribution: `k5-analytics-runtime`
- Distribution version: `0.0.0+gc8b347ae538991a0c0ce38eabc2dc17b566531d3`
- Import namespace: the original `analytics_lab`, unchanged
- Governing project terms: K5 Analytics Lab Source-Available License v1.0,
  copyright (c) 2026 Kurt Gerald; original donor rights remain effective

`src/k5vision/data/analytics-runtime-manifest.json` binds every packaged source
and notice file by exact byte size, SHA-256 and upstream Git blob SHA-1. The 67
Python files are the complete `analytics_lab/` source directory at this revision.
No source modification, model, dataset, media, native library or compiled code
is put in the wheel. Upstream `__version__` remains unchanged; admission uses
installed distribution metadata and the complete payload, never that string.

The original root `LICENSE`, full `THIRD_PARTY.md`, and ByteTrack MIT license
are preserved byte-for-byte. Inline Intel Apache-2.0 and ByteTrack attribution
remain in the unchanged Python sources. The full Apache-2.0 license is supplied
from Open Model Zoo revision `6697dead54ed1cdd664b0313189c2cb52ee6335e`, `LICENSE`
Git blob `261eeb9e9f8b2b4b0d119366dda99c6fd7d35c64`. Its supplemental local copy is
also manifest-bound. Open Model Zoo's root tree has no NOTICE file; its separate
third-party register covers components not copied by this wrapper.

The wheel retains all these notices under its `.dist-info/licenses/` directory,
alongside a copy of the entire admission manifest. The donor ledger and
`THIRD_PARTY_NOTICES.md` describe the separation and remaining release review.

## Offline build

Use Python 3.12+ and a previously acquired, authorized local checkout or source
archive of the exact pinned Analytics source. The builder itself does not fetch
source, call Git, install packages, execute Analytics, resolve dependencies, or
load models. A source archive is sufficient because every used byte is checked
against K5's pre-reviewed manifest; a claimed revision or checkout directory name
alone is never trusted.

```sh
python scripts/build_analytics_runtime_wheel.py \
  --source-root /absolute/path/to/Analytics-lab \
  --output-dir /absolute/path/to/wheels
```

The stdlib-only builder rejects missing, changed, additional or symlinked package
source and mismatched notices before producing an artifact. Use a clean source
directory without Python caches or automatic newline conversion (a Git archive
export preserves the exact bytes on Windows too). It writes a sorted, uncompressed ZIP wheel
with fixed timestamps, fixed file permissions and a complete hashed `RECORD`.
No environment timestamps, source paths, build backend or network state enter
the bytes. Rebuilding from the same admitted inputs yields an identical wheel.
Output replacement is atomic; interrupted writes cannot publish a partial wheel.

The wheel has no automatic native dependency resolution. For engineering
qualification, install the generated wheel into the same environment as the K5
wheel using the explicit local artifact, for example:

```sh
python -m pip install --no-index --no-deps /absolute/path/to/wheels/k5_analytics_runtime-0.0.0+gc8b347ae538991a0c0ce38eabc2dc17b566531d3-py3-none-any.whl
```

Provision the separately pinned K5 `analytics` optional dependencies only under
the approved runtime qualification procedure. This wrapper does not download
or package those wheels. Normal K5 installation keeps analytics optional, and
no nonexistent PyPI Analytics package is declared as a dependency. Production
packaging remains blocked on the exact native/transitive notices and model
rights review recorded in the upstream register.

## Installed-package admission contract

`k5vision.analytics_package.ANALYTICS_REVISION` is the expected full source SHA.
`validate_installed_analytics()` returns the verified `analytics_lab` directory
or raises `AnalyticsPackageError`, a `RuntimeError` subclass. Both module import
and validation are stdlib-only and import neither Analytics nor native runtimes.
Call admission **before** the first Analytics import. It checks:

1. Exactly one installed distribution with the required name and full version
2. Exact `METADATA`, `WHEEL`, top-level namespace and full installed manifest
3. Size and SHA-256 of every source and notice, anchored in K5's expected manifest
4. A complete hashed installation `RECORD`, rejecting missing or unexpected entries
5. No editable installation, missing source, path traversal, symlink or extra code
6. Every present current-interpreter `.pyc` body against code compiled from the
   verified source, including optimization levels 0, 1 and 2; stale/foreign
   caches and external `PYTHONPYCACHEPREFIX` locations are rejected, rather than
   allowed to override verified source
7. Import resolution to that exact package root, so a checkout or `PYTHONPATH`
   shadow is rejected without executing it; pre-imported unadmitted modules are
   rejected, and repeat admission also checks loaded module origins/search paths

Restart after correcting a pre-imported/shadowed package. Repair an invalid
installation from the exact wheel rather than editing files or bypassing the
manifest. No network retry or permissive fallback occurs. This is an installation
integrity boundary in an otherwise trusted process, not a sandbox against an
attacker controlling the interpreter, import hooks or concurrent filesystem
writes. Installations must not be modified while the application uses them.

## Evidence and remaining gates

`tests/test_analytics_package.py` covers deterministic wheel creation and local
synthetic installed distributions without native dependencies. It exercises
valid/repeated admission, all optimization cache variants, absent/duplicate/wrong
packages, metadata/source/manifest corruption, extra code, bytecode tampering,
checkout shadowing, pre-imported modules, changed origins/search paths, unsafe
paths, malformed records, editable installs and altered build inputs.

Engineering evidence must separately establish a clean installed K5 wheel,
installed Analytics wheel, optional native dependency identities, application
startup, readiness, and enabled/disabled behavior. Unit tests and a deterministic
wheel do not establish live-video inference, hardware acceptance or commercial
rights. No timeout/retry against an external service applies to this offline
builder/admission step; native runtime and physical qualification remain distinct
checks, with shared runners and unrelated work preserved.
