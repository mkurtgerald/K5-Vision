# Installed Windows person-analytics dependencies

This record identifies the native Windows inputs for the independent K5 person
detector. The installer includes its Python/native runtime and model; enabling
person events in the graphical application requires no package manager, model
download, command shell, or separately installed Python. Native recording keeps
its existing MediaMTX and Caddy provisioning boundary.

The source-available Analytics Lab engineering wrapper is not included or used
by this detector. Its separate rights and qualification remain unchanged.

## Exact Windows runtime

The additional wheel identities are pinned in `windows/desktop/donors.json` and
the hash-required desktop requirements file:

| Distribution | Exact version | Windows wheel bytes | Governing notices |
| --- | --- | ---: | --- |
| OpenVINO | 2026.3.1 | 75,818,370 | Apache-2.0; included runtime, oneDNN and oneTBB notices |
| NumPy | 2.2.6 | 12,614,190 | BSD-3-Clause; OpenBLAS and GCC runtime notices/exception |
| OpenVINO telemetry | 2025.2.0 | 25,227 | Apache-2.0 |
| OpenCV headless | 5.0.0.93 | 43,825,962 | Packaging MIT, OpenCV Apache-2.0, FFmpeg LGPL-2.1-or-later, and complete supplied third-party notices |

The build verifies the exact wheel byte length and SHA-256, distribution metadata,
and the supplied license files. All wheel notices and `.dist-info` metadata are
retained in the installed runtime and their hashes appear in the inventory. The
OpenVINO wheel includes complete `runtime-third-party-programs.txt`,
`onednn_third-party-programs.txt`, and `onetbb_third-party-programs.txt` records.
NumPy retains its full binary license text, including OpenBLAS and the GCC Runtime
Library Exception. These terms remain effective independently of K5's license.

OpenVINO imports Microsoft `MSVCP140.dll` by its original name. NumPy
supplies the unchanged Microsoft runtime with a wheel-repair hash suffix. The
build verifies that exact source and copies its unchanged bytes app-locally to
`runtime/msvcp140.dll`: 575,056 bytes, SHA-256
`a4c2229bdc2a2a630acdc095b4d86008e5c3e3bc7773174354f3da4f5beb9cde`.
The original NumPy copy is retained, and both paths and their origin are bound
in the inventory. Existing installer Microsoft Distributable Code terms apply;
this does not require a separate user-installed C++ redistributable. CPython
supplies the other required VCRUNTIME140 and VCRUNTIME140_1 DLLs. The modern
Windows Universal CRT and operating-system libraries remain system components.

The installed detector uses the OpenVINO CPU path. GPU hardware support,
accuracy, scene suitability, capacity and endurance require their own measured
qualification. A bundled library's additional capabilities are not a K5 feature
claim. Inference errors and resource limits must remain isolated from recording.

`pip install .[operator-analytics]` is a developer convenience matching this
independent detector. The older `[analytics]` extra preserves the separate
engineering wrapper's OpenCV 4.12 admission; neither command is an end-user step.

## Exact person model

The Intel/Open Model Zoo `person-detection-retail-0013` FP16 model is separately
licensed under Apache-2.0 at source revision
`6697dead54ed1cdd664b0313189c2cb52ee6335e`. The model definition is copyright
(c) 2023-2024 Intel Corporation. Its complete upstream license is installed as
`licenses/OpenModelZoo-person-Apache-2.0.txt`.

The official XML and BIN are 571,233 and 1,445,734 bytes respectively. Their
official source URLs, exact SHA-384 identities, documented tensor interface and
license provenance remain in `operator-person-model-manifest.json`. The build
requires the reviewed complete LF or CRLF manifest and checks both downloaded
artifacts before sealing them in the payload. The installed loader repeats
artifact/runtime admission before constructing the model from verified bytes.

Installed relative root:
`runtime/Lib/site-packages/k5vision/data/person-models/`. The existing manifest's
`person-detection-retail-0013/FP16/` subdirectory is preserved. The product uses
this installed configuration automatically; explicit engineering configuration
keeps its prior validation behavior. No demonstration image or private camera
media forms part of the installed payload.

The upstream model targets upright pedestrians in retail scenes. Its license
does not establish suitability for distant people, night scenes, other poses,
identity recognition, vehicles or other analytic categories. Release evidence
must distinguish actual model execution and event navigation from accuracy.

## Runtime-loaded FFmpeg decoder and corresponding sources

The Windows OpenCV wheel's decoder is the separately loaded
`cv2/opencv_videoio_ffmpeg500_64.dll`, 30,876,160 bytes, SHA-256
`7aabff029d1fa47cf56ede4dbbe05dc62da3208f07cd16471d4c966a33f9aa31`.
Its Git blob `117f46365f6bba0d0553989a96d86e10d2d89d39` exactly matches the
unchanged upstream binary in
[opencv/opencv_3rdparty](https://github.com/opencv/opencv_3rdparty/tree/06dc20cad65dc7fcf784f70c95d46750520889a7),
commit `06dc20cad65dc7fcf784f70c95d46750520889a7`.

The official readme identifies this Windows decoder as LGPL and states that its
build excludes GPL components. The exact binary reports LGPL version 2.1 or later;
its retained configure flags contain neither `--enable-gpl` nor
`--enable-nonfree`. The complete wheel `cv2/LICENSE-3RD-PARTY.txt` is retained
unchanged (183,251 bytes; SHA-256
`c1d60169b55cee56452b227c2fd2a7b9ddda3dd3741065f1c6201072be73fadb`).
That document also preserves supplied permissive-component and Intel IPP terms.
K5 uses decoding of its own local recording files. It does not bundle or fetch
the separate OpenH264 encoder binary.

The upstream build script identifies FFmpeg `n7.1`, libvpx `v1.16.0`, libaom
`v3.14.1` and OpenH264 `v2.5.0` API headers. The decoder's wrapper is from OpenCV
commit `a0a660fcb1e58a295e6caa6aee64ed4d369b0181`, preserved by the upstream
binary repository's submodule identity. The admitted upstream inputs below supply corresponding material under
installed `licenses/sources/`. The complete FFmpeg, libvpx and libaom archives
are retained unchanged. OpenCV build/wrapper and OpenH264 archives retain every
complete non-NUL source/build file in `*-sources.tar.gz`, omitting duplicate
prebuilt binaries and media fixtures. Each derivative has a sealed inventory of
original per-file lengths and SHA-256 values; original upstream archive
identities remain in the dependency inventory:

| Archive | Source revision | SHA-256 |
| --- | --- | --- |
| `FFmpeg-n7.1.tar.gz` | `b08d7969c550a804a59511c7b83f2dd8cc0499b8` | `02fa6d9827da3b6786e4df821218cc036db2b4481e7f48267c2dcda695633afc` |
| `OpenCV-FFmpeg-build.tar.gz` | `06dc20cad65dc7fcf784f70c95d46750520889a7` | `6ed03e7468a3d91b62e24aa5ad2183924f231dd81a3442da221d7ddf315220b8` |
| `OpenCV-FFmpeg-wrapper.tar.gz` | `a0a660fcb1e58a295e6caa6aee64ed4d369b0181` | `5d97bddd4c5d2895dddafe6aff211b9fc1add564887263b7c062f2699660bf1d` |

| `libvpx-v1.16.0.tar.gz` | `1024874c5919305883187e2953de8fcb4c3d7fa6` | `274c00c0b21f03a9152e324b1989f73b74268aa8545b39ef5faccc50fdb56393` |
| `OpenH264-v2.5.0.tar.gz` | `8c7008aeb6335e7d36ab0d9a023a63f82a8eaac0` | `0a12daa5a7fefc3acf5f6485fe21159399351cd4b83af80dde525fa520f17820` |
| `libaom-v3.14.1.tar.gz` | `03087864cf4bea6abb0d28f95cf7843511413d8f` | `5e0bbef3dbf20004ae80e68bb84ae149b7e2c532f2674169ea8d6d42b6b16945` |

The build repository includes the original complete MinGW/Docker build scripts,
source-download revisions, OpenH264 wrapper, toolchains and license. The FFmpeg
archive supplies the complete corresponding FFmpeg source. The retained OpenCV
source archive supplies wrapper source, headers and build configuration.
Full libvpx/libaom sources and OpenH264 source/API headers accompany it. Upstream source URLs and archive
sizes are also recorded in the sealed inventory. The build tools and archives
are source-access material; the installer never executes them.

Recipients retain the applicable LGPL rights to modify the library and to
modify/relink their own application copy for use with a modified library,
including reverse engineering for debugging those modifications. K5's terms do
not prohibit those rights. Installed K5 Python source, the public supervisor
source and build configuration, complete corresponding LGPL sources, and the
original decoder/build notices are available to perform that work. The official
installer checks its reviewed payload before execution; a rebuilt application
can bind a replacement decoder. This source-and-relinking route does not promise
that a modified binary retains the official installer's qualification.

PyAV is not part of this package. Package metadata alone was not used to admit
unreviewed native codec libraries.
