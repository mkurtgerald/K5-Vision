# Exact native media worker review

Reviewed on 2026-10-13 for the graphical Windows recording candidate. The
installer bundles the reviewed Python application and interpreter. It does not
bundle MediaMTX or Caddy. The desktop application's first launch obtains the
unchanged, pinned official Windows archives through the graphical startup flow;
the user does not enter commands or install a development toolchain. Subsequent
launches use the verified private cache. An initial Internet connection is
therefore required for these workers.

The executable identities remain the existing donor manifest boundary. The
graphical provisioner must verify the complete archive and extracted executable
against these identities and preserve the upstream archive license documents.
This boundary does not imply that K5 owns the donor implementations or their
dependencies, or that a complete commercial binary redistribution review is
finished.

| Worker | Exact source commit | Windows executable SHA-256 | Embedded Go runtime | Build-module inventory |
| --- | --- | --- | --- | --- |
| MediaMTX v1.21.2 | `914e39535ece709919ac325aaeed98d394ed9093` | `31bc3cc425a0278c7a5696c7ba309257235060157eb338eed20bb88dbe0faf87` | go1.27.2 | 85 dependencies and the MediaMTX root module |
| Caddy v2.11.7 | `72dd0fb067f6d7826c7f79907670ba4a713bfe37` | `5f93a9bd555f3aac9d33af2756ba14b3519d8cca4ffd55256bdaeab444c9db2a` | go1.26.8 | 148 dependencies; upstream synthetic `caddy (devel)` build module |

The reviewed bytes are the official artifacts identified by
`src/k5vision/data/mediamtx-donor-manifest.json` and
`src/k5vision/data/caddy-donor-manifest.json`. Their executable sizes are
57,024,512 and 53,159,936 bytes, respectively. Both executable build records
report Windows amd64, `CGO_ENABLED=0`, the exact source commit above, and an
unmodified source tree. These records establish the compiled module inventory;
they do not establish that every file in each module source archive was linked.

## What was verified

Every dependency module source archive was obtained at its exact embedded
version. Its Go directory `h1` checksum was independently calculated and matched
the executable's embedded checksum. Archive SHA-256, original supplied legal
documents, and verbatim source copyright/license comment blocks were retained
in a separate offline engineering audit. The MediaMTX root archive was also
reviewed at its exact version. Official Go source archives were matched against
the Go release metadata, including both byte counts and SHA-256 values:

| Source archive | Bytes | SHA-256 |
| --- | --- | --- |
| `go1.26.8.src.tar.gz` | 34,150,120 | `4e39b98e42f946fa05ac8bc5b71877df97dbdb7cbb1a777b541667ad7117fd2e` |
| `go1.27.2.src.tar.gz` | 35,134,614 | `03495da2ba64894d40f5c4992e49454fa78b50690604ff92b6afff5081b76e62` |

The runtime preservation includes the original Go BSD license and patent grant,
vendored legal files, and source legal comments. The latter capture notices
that a top-level BSD license alone misses, including Lucent/Vita Nuova runtime
code, Sun math code, and Fiat crypto code. Preserving legal material from an
entire source archive is deliberately broader than the Windows link graph;
notices for another target or a source-only development tool do not imply that
the associated implementation was shipped in the product.

Caddy's exact Go proxy metadata supplies full source-origin commits for 130 of
its 148 dependency modules. The remaining 18 older records lack that metadata.
Their exact module versions and verified embedded `h1` identities are recorded;
full origin commits are not invented. Pseudoversions retain the commit prefixes
encoded by their upstream version. Source-archive hashes identify every
reviewed archive independently of this metadata limitation.

## Findings that require exact linkage and scope

- **MediaMTX's `github.com/benburkert/openpgp` fork:** the exact source archive at
  `v0.0.0-20160410205803-c2471f86866c` has no root license file. Some fork-added
  files have no explicit terms. The reviewed Windows executable's file/function
  tables identify only `aes/keywrap/keywrap.go`, `Wrap`, and `Unwrap` from this
  module. That file includes complete BSD-2-Clause terms, copyright 2014 Matthew
  Endsley, and has SHA-256
  `1003ae68bf74ac039e624a5e100a2b89723e06d00b473b52251024d34abeb3c4`.
  This supports the terms for the observed linked code; it does not clear the
  rest of the fork for arbitrary reuse.
- **MediaMTX's `github.com/cyphar/filepath-securejoin@v0.6.1`:** its source archive
  includes BSD and MPL material. The Windows file table identifies root
  `join.go` and `vfs.go`, whose source declares BSD-3-Clause. No `pathrs-lite`
  marker was found; the MPL implementation belongs to Linux/test portions of
  this source archive. The broader module license collection is retained as
  evidence without labeling the Windows worker wholly MPL or wholly BSD.
- **Caddy's `github.com/slackhq/nebula@v1.11.2`:** its source archive includes
  Wintun DLLs under a custom prebuilt-binary license with a non-transferable
  grant and a conditional permitted-API redistribution exception. The official
  Caddy Windows archive contains only `caddy.exe`, `LICENSE`, and `README.md`.
  The reviewed module has no DLL embedding directives, and Caddy's build
  inventory has no `golang.zx2c4.com/wintun` module. The source-only DLLs are not
  copied into the K5 product. Their presence in a dependency archive is not
  evidence that the reviewed Caddy executable contains them.
- **Caddy's `github.com/alecthomas/chroma/v2@v2.27.0`:** its complete `COPYING`
  includes MIT source terms and the SIL Open Font License 1.1 for Liberation
  Mono. Reserved font names, the prohibition on selling the font alone, and
  retained font notices remain applicable to the relevant font material; the
  module cannot be summarized as only MIT.
- **Caddy's generated fallback roots:**
  `golang.org/x/crypto/x509roots/fallback@v0.0.0-20260929172509-b39ff6d641ec`
  supplies a Go BSD license and embeds `bundle/bundle.der`. Its source explicitly
  says the roots come from the NSS trust store. The exact generator in
  `golang/crypto` commit `b39ff6d641ecc5bb4b241737cb0c6b646f3e12a9` defaults to a
  Mozilla `certdata.txt` tip URL rather than recording the exact NSS input
  revision in the generated bundle. A Go BSD declaration does not establish
  the separate inherited certificate-data terms or that original input's
  revision. Exact source/notice reconciliation for this data remains open
  before K5 bundles this binary in a redistributed installer.

## Distribution decision

The current graphical installer retains the previously established official
download boundary for both workers. The dependency audit is engineering
evidence, kept separately from the installer and repository license tree;
copying hundreds of collected source notices into the repository would not
close the remaining review or establish binary redistribution clearance.

A later package that bundles either worker must reconcile its exact link graph,
embedded assets, complete notice obligations, and applicable source-availability
requirements. A donor/runtime upgrade changes the input and requires a new
review. The K5 source-available license continues to apply to K5-owned code;
third-party rights remain with their respective owners.
