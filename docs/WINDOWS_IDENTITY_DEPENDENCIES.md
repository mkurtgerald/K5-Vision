# Native Windows identity dependencies

The offline desktop runtime adds four exactly admitted wheels to its existing
19-wheel recorder/person-detection runtime. No first-use package, model, OIDC
library or native crypto download is required by an installed application.

| Component | Version / Windows wheel | License | Wheel SHA-256 |
| --- | --- | --- | --- |
| PyJWT | 2.15.1, `py3-none-any` | MIT | `42d59d631f7768a1028a64c7ff581a9bf7519804daf91fc5b6c56e30eec5e193` |
| cryptography | 50.0.2, `cp311-abi3-win_amd64` | Apache-2.0 OR BSD-3-Clause | `7afa5a6602a9f29af1f3a2965f831bae7c9d5d597b7cbb716d41ab3b7d89879c` |
| cffi | 2.1.1, `cp312-cp312-win_amd64` | MIT-0; libffi retains its own notices | `f53e442b08449d42821fa4a4fba000095af9f62742a500f978a9f557ec44339a` |
| pycparser | 3.12, `py3-none-any` | BSD-3-Clause | `a076f3f489b39b43ed13563a1dafe42447445bec2e294154ec5c57fcdda4c85e` |

`runtime-requirements.txt` requires all 23 exact wheel hashes. `donors.json`
also pins each filename and byte count. CPython 3.12's compatible tag selection
chooses the cryptography CPython-3.11 stable-ABI wheel above, rather than a newer
Python-only or free-threading build. The project declares exact PyJWT and
cryptography dependencies for source/CI installations; the existing optional
Analytics Lab extra is unchanged.

PyJWT handles explicit RS256 verification and JWK parsing for configured identity
providers. K5's own admission rules remain responsible for issuer, audience,
algorithm, key selection, request state, nonce, PKCE and account permissions.
TOTP is implemented in K5 with standard-library primitives and needs no separate
authenticator package. Runtime inclusion alone does not establish provider
interoperability or production authentication acceptance.

## Native library and notice closure

The admitted Windows `_rust.pyd` reports OpenSSL **4.0.3** and contains Rust
standard-library source references at commit
`48a229ceaefd4985c50990b14116b6d856af0985` (Rust **1.98.1**). Its 9,897,472 bytes
have SHA-256 `1d4332a447cab13dec8e3a533ad579e0e562b79e40822c931f33769758fc7156`.
The cffi extension's 181,248 bytes have SHA-256
`9af5e4f68a41f26b726f2e9424f9d58a4f5666189f6a48be95f406b257102aec`.
Both exact native-module identities are checked after wheel installation.

PE import inspection found CPython `python3.dll` / `python312.dll`, the already
included `VCRUNTIME140.dll`, Windows UCRT API sets and system DLLs. No additional
app-local redistributable is introduced. OpenSSL is statically incorporated by
the admitted cryptography wheel; there is no separate OpenSSL DLL search or
runtime download.

Every wheel's original `.dist-info/licenses` material remains installed and is
hash-bound in the dependency inventory, including PyJWT authors and the complete
PyCA Apache/BSD texts. That alone does not describe the static native closure.
The installed `licenses/Identity-native-NOTICES.txt` additionally retains original
Rust copyright/license metadata, applicable in-tree license texts and full notice
files from all 61 distinct registry packages in the exact cryptography and Rust
standard-library Cargo lockfiles. Its accompanying JSON inventory records
upstream crate URLs, Cargo checksums, declared license expressions and every
retained source-file byte count/hash.
The readable collection normalizes only CRLF line endings to LF when combining
upstream texts; the provenance inventory retains each original input's byte
count and hash. No copyright, permission, disclaimer or license clause is removed.

The Rust lockfile is deliberately an audit superset: other-target, test and build
dependencies' notices are retained for completeness. Fortanix SGX's MPL terms,
for example, do not mean SGX code is part of this Windows runtime. Rust metadata
also describes compiler, documentation and GCC files outside the distributed
standard library; those implementations are not included by this change.
Disjunctively licensed crate material retains its complete supplied texts rather
than being relabeled as K5's license.

The installation also retains the admitted cryptography source archive and
OpenSSL 4.0.3 upstream source at commit
`af1775b60dfa141a4ad762585052cabeb9f37e9e`, under `licenses/sources/`. The CFFI
source derivative retains every complete text source/build file, including the
original libffi copyright/permission comments. Unneeded prebuilt ARM64 libraries
and object files in its source distribution are omitted. Its original archive
checksum, derivative checksum and per-file inventory remain bound to the payload.
No source archive is executed by the installer.

Both complete LF and Windows CRLF representations of the reviewed notice text
are admitted; any other byte change is refused. Actual installed bytes are
sealed. The isolated embedded-runtime smoke generates an RSA key, signs and
verifies an RS256 token through a public JWK, checks the native OpenSSL version,
and exercises the RFC 6238 TOTP vector before the payload can be packaged.
Actual installed Windows identity workflows remain a separate mandatory gate.
