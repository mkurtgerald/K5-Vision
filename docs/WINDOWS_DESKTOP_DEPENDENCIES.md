# Native Windows desktop dependency review

Reviewed on 2026-10-13; ONVIF source slice reviewed on 2026-10-18; installed
person-analytics inputs reviewed on 2026-10-28. This record covers the desktop package:
an embedded CPython runtime, an exact minimal Python operator environment,
official first-run native-worker provisioning, and an Inno Setup installer. The user installs
and starts the application through Windows graphical controls. First launch
requires Internet access to the pinned official worker downloads; subsequent
launches reuse the admitted private copies. The independent OpenVINO person
detector, OpenCV decoder, exact model and native notices/sources are bundled;
development tools and the separate Analytics Lab engineering wrapper remain
outside this package. See `WINDOWS_PERSON_ANALYTICS_DEPENDENCIES.md` for the exact
model/native source and relinking record.

## CPython runtime

- **Artifact:** official CPython 3.12.10 Windows amd64 embeddable distribution,
  [`python-3.12.10-embed-amd64.zip`](https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip).
- **Source:** [CPython v3.12.10](https://github.com/python/cpython/tree/0cc81280367df838c4b199f8f0378837165071c2),
  commit `0cc81280367df838c4b199f8f0378837165071c2`.
- **Integrity:** 11,133,606 bytes; SHA-256
  `4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3`.
- **Integration:** unchanged official interpreter, standard-library archive,
  and native libraries are installed in the application directory. The path
  configuration is adjusted to load the bundled application and site packages;
  this does not remove or replace upstream notices.
- **Rights:** Python's PSF and historical licenses allow commercial use and
  redistribution with their notices preserved. Embedded native components have
  their own terms. The official archive's complete `LICENSE.txt` is retained,
  including the Microsoft Distributable Code provisions and bundled library
  notices. Its exact bytes are 36,874 bytes with SHA-256
  `e502c6b880ff58d614901495a9009c136539cd0b1e2a2abb8fc00b934c203419`.
- **Windows restrictions:** Microsoft Distributable Code remains limited to
  Microsoft platforms; preserve its copyright, trademark and patent notices,
  do not imply Microsoft endorsement, and retain the supplied restrictions on
  malicious, deceptive or unlawful programs. Installer acceptance must cover
  these supplied terms. These restrictions apply to Microsoft's code, rather
  than changing the Python software license or ownership of K5 code.
- **Maintenance:** this exact binary is a reproducible package input. Future
  interpreter/security updates require a newly reviewed artifact and native
  desktop acceptance; the package does not automatically upgrade itself.

## Exact Python operator environment

The desktop package installs the K5 wheel with dependency resolution disabled
and separately provisions the following exact operator runtime. This is a
deliberately smaller environment than the general-purpose dependency list in
`pyproject.toml`. It supports ONVIF camera discovery, credentials, H.264 profile
selection and recording. The camera's standard RTSP stream is resolved
automatically; the normal setup flow does not require the user to find or type a
stream URL. The ONVIF connector uses the bundled Python standard library and
adds no distribution to the exact list below.

| Distribution | Version | Declared license |
| --- | --- | --- |
| annotated-doc | 0.0.5 | MIT |
| annotated-types | 0.8.0 | MIT |
| anyio | 4.15.1 | MIT |
| click | 8.5.0 | BSD-3-Clause |
| fastapi | 0.143.0 | MIT |
| h11 | 0.16.0 | MIT |
| idna | 3.20 | BSD-3-Clause |
| opentelemetry-api | 1.45.1 | Apache-2.0 |
| psutil | 7.2.2 | BSD-3-Clause |
| pydantic | 2.14.1 | MIT |
| pydantic-core | 2.50.1 | MIT |
| starlette | 1.7.0 | BSD-3-Clause |
| typing-extensions | 4.16.0 | PSF-2.0 |
| typing-inspection | 0.4.4 | MIT |
| uvicorn | 0.54.0 | BSD-3-Clause |
| OpenVINO | 2026.3.1 | Apache-2.0 and retained native notices |
| NumPy | 2.2.6 | BSD-3-Clause and retained native/runtime notices |
| OpenVINO telemetry | 2025.2.0 | Apache-2.0 |
| OpenCV headless | 5.0.0.93 | MIT/Apache-2.0; FFmpeg LGPL-2.1-or-later and retained native notices |

These declarations were checked against the exact wheel metadata and included
license files. Click 8.5.0 declares no runtime `Requires-Dist`, so colorama is
not required by this exact environment. The native Windows build must preserve
all installed `.dist-info` metadata and license directories, including older
wheel layouts that put `LICENSE` directly in `.dist-info`, and retain package
notices elsewhere in the wheel. Apache `NOTICE` files must remain intact when
provided. K5's license does not supersede any of these third-party terms.

The build inventory should record each distribution name and version, exact
wheel filename and SHA-256, declared license expression/classifiers, and the
relative paths and hashes of every supplied license and notice. Record CPython
and media-worker hashes separately. Review the installed distribution inventory
against this exact list and reject unplanned additions. Preserve native-package
and compiled-component notices; package-level metadata alone does not establish
that every statically linked component has completed a separate release review.

The omitted general dependency subtree needs its own review before it can be
added to a redistributed desktop product:

- `onvif-python==0.3.1` declares MIT for its Python package, but separately ships
  `onvif/wsdl/LICENSE.md`. That document contains custom ONVIF contribution and
  document terms: copying unchanged documents with retained notices is allowed,
  while modification of that document is not granted. This is not equivalent
  to applying the package's MIT declaration to every bundled schema.
- Its requests/zeep dependency subtree brings `certifi`, with MPL-2.0 source
  availability obligations, and the Windows `lxml` wheel. The inspected lxml
  6.1.3 `LICENSES.txt` states that its binary wheels bundle LGPL-2.1 iconv and
  describes some schematron extraction resources as unlicensed. The package's
  BSD declaration does not resolve those separate obligations or missing terms.

These packages and resources are excluded rather than relicensed by K5.

## Selected go2rtc ONVIF source adaptation

- **Source:** [AlexxIT/go2rtc v1.9.14](https://github.com/AlexxIT/go2rtc/tree/b5948cfb25404cc5cb37b166ecaa2dca20b11d4b),
  immutable commit `b5948cfb25404cc5cb37b166ecaa2dca20b11d4b`.
- **Selected files:** `pkg/onvif/client.go`, `pkg/onvif/envelope.go` and
  `pkg/onvif/helpers.go`, limited to camera discovery/scope names, SOAP and
  WS-Security UsernameToken construction, capability/profile lookup and stream
  URI resolution. The unrelated time-zone helper is not adopted.
- **License/copyright:** MIT, Copyright (c) 2022 Alexey Khit. The unchanged
  upstream `LICENSE` is retained in `third_party/licenses/go2rtc-MIT.txt` and
  the installed `licenses` directory; 1,068 bytes, SHA-256
  `b0dcf4855af5a72b4dfbd9117c207b330f4cc35658576a0b5351d6e2becac546`.
  The complete source/blob/hash record is in `DONOR_LEDGER.md`.
- **Integration:** selected source adapted into `operator_onvif.py`, with inline
  attribution and full MIT notice. Python's bundled standard library supplies
  the implementation; no Go executable/runtime, additional Python distribution,
  `onvif-python`, zeep, lxml, requests, or WSDL files are shipped for this
  connector. No ONVIF Device Manager code is copied.
- **Changes:** bounded XML/network handling, numeric camera-address pinning,
  response-size/deadline/concurrency limits, HTTP Digest and Media2 fallback.
  The source slice does not implement ONVIF server behavior, PTZ, imaging or
  camera configuration writes. MediaMTX continues to handle the resolved stream.
- **Commercial-use conclusion:** MIT permits this exact selected-source use,
  modification and redistribution with its copyright/permission notice
  preserved. This bounded review does not admit unrelated go2rtc packages,
  dependencies, models or binaries. K5-owned work retains its existing terms.
- **Qualification:** the ONVIF GUI flow requires its own exact-build Windows
  acceptance. Earlier manual-enrollment desktop evidence does not qualify the
  new onboarding flow or establish compatibility with a physical camera.

The installed person-event detector includes the independently reviewed
OpenVINO/NumPy/OpenCV runtime, Apache-2.0 person model, runtime-loaded LGPL
FFmpeg decoder, unchanged notices and corresponding decoder sources described
in `WINDOWS_PERSON_ANALYTICS_DEPENDENCIES.md`. No Analytics Lab engineering
wrapper, standalone FFmpeg executable, GStreamer, Playwright or browser binary
is included in the product runtime. The earlier view-only candidate review in
`OPERATOR_PERSON_DETECTION_REVIEW.md` remains a historical engineering scope;
installed product qualification requires the actual shipped runtime.

## Inno Setup installer

- **Artifact:** official [Inno Setup 6.4.3 installer](https://github.com/jrsoftware/issrc/releases/download/is-6_4_3/innosetup-6.4.3.exe),
  6,454,616 bytes; SHA-256
  `f3c42116542c4cc57263c5ba6c4feabfc49fe771f2f98a79d2f7628b8762723b`.
  The downloaded bytes match the official GitHub release digest.
- **Source:** tag `is-6_4_3`, commit
  `9254dce05d45fd804f64bc802d6353cd9d44755c` in
  [jrsoftware/issrc](https://github.com/jrsoftware/issrc/tree/9254dce05d45fd804f64bc802d6353cd9d44755c).
- **License:** the exact [Inno Setup License](https://github.com/jrsoftware/issrc/blob/9254dce05d45fd804f64bc802d6353cd9d44755c/license.txt)
  is preserved unchanged in
  `third_party/licenses/InnoSetup-6.4.3.txt`, SHA-256
  `a7f97b166be34fc8bf04e79617b9f56b2aa7903a2808f94155e449c80bdfdcc2`.
  Copyright (C) 1997-2025 Jordan Russell; portions copyright (C) 2000-2025
  Martijn Laan.
- **Integration and conclusion:** the compiler is a build tool, and its
  generated installer/uninstaller engine is distributed with the product.
  The exact 6.4.3 license expressly permits use for any purpose, including
  commercial applications, and redistribution. Retain source copyrights and
  conditions, all binary copyright notices and website addresses, do not
  misrepresent origin, and clearly mark modifications to upstream software.
  K5 supplies its own installer configuration without modifying the Inno engine.
- **Version boundary:** the build uses this exact reviewed compiler. An image's
  preinstalled compiler or a current/latest release is not interchangeable;
  later version terms, artifact hashes, and generated behavior require review.
  A successful compiler signature/hash check does not sign the generated K5
  installer or confer Windows SmartScreen reputation.

## Native Go media workers

The desktop installer does not bundle these worker executables. Its graphical
first launch downloads the same unmodified official Windows amd64 workers
already pinned for the native operator pilot, with finite progress and no shell
commands. Their surrounding K5 interfaces remain the replacement boundary.

| Component | Immutable source | Archive SHA-256 | Executable SHA-256 |
| --- | --- | --- | --- |
| Caddy v2.11.7, Apache-2.0 | `72dd0fb067f6d7826c7f79907670ba4a713bfe37` | `0a1edc0b799512051c57071ce0e798d3f2cf67dc98171366f1d2b326072e3b06` | `5f93a9bd555f3aac9d33af2756ba14b3519d8cca4ffd55256bdaeab444c9db2a` |
| MediaMTX v1.21.2, MIT | `914e39535ece709919ac325aaeed98d394ed9093` | `a84224f5a490855684e62ba6fb71e80ea2a5e16aed61883bb58380290e93e467` | `31bc3cc425a0278c7a5696c7ba309257235060157eb338eed20bb88dbe0faf87` |

Official source, download URLs, executable sizes and archive identities are in
`src/k5vision/data/caddy-donor-manifest.json` and
`src/k5vision/data/mediamtx-donor-manifest.json`. The installed, hash-bound
`workers/donor-downloads.json` selects exactly these two Windows artifacts.
Provisioning verifies each archive SHA-256, executable length/SHA-256 and
unchanged license SHA-256 before using it. The private worker directory retains
the official archive's complete license alongside its executable. Existing
verified copies are reused; modified bytes are refused rather than executed.

Caddy's official release supplies target-specific SBOMs. The separately captured
exact compiled-module inventory and supplied notices help assess future worker
redistribution, but package-level declarations alone do not establish all native
component rights. Earlier pilot records admitted official downloads and left
broader binary redistribution review open. Keeping the same download boundary
does not silently close that review or make K5 the owner of Go dependencies.

This record establishes the package inputs and the bounded desktop scope. It
does not grant rights over restricted analytics material, settle missing terms
in omitted packages, or claim legal sign-off for a general commercial release.
