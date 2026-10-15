# Windows engineering decoder integration

This change addresses the actual installed-host failure in run 38141253745,
job 114475416526: the installed application required d3d11h264dec, which was
unavailable on that hosted Windows worker. This is not evidence about the
owner's physical graphics hardware.

The existing native playback and direct RTSP builders now use the same selector
before building a pipeline. Registered D3D11 remains preferred. If unavailable,
the internal engineering candidate may use only the exact reviewed GStreamer
1.28.7 gst-libav/FFmpeg 7.1 shared libraries, as qualified by run 38143310437,
job 114481421423. There is no fallback retry after pipeline execution fails.
Fatal-error parsing and every positive analytics, tracking, overlay and operator
acceptance condition remain unchanged.

Software admission verifies SHA-256 file identities, rejects links/reparse points
inside the runtime, verifies actual loaded HMODULE paths (not requested names),
checks plugin origin/version/license and the codec's actual exported LGPL-2.1-
or-later license, libavcodec 61.19.100 version, and absence of enabling GPL,
nonfree or version3 configuration. File identities are checked again after
loading. Only fixed selected-mode messages are emitted, without source or media.
The existing bounded installed launch/controller remains responsible for runtime
execution limits. No additional shell, owner setup or unbounded standalone
preflight process is introduced by this change.

No third-party source is copied or modified. The existing separately loaded
upstream shared libraries remain subject to their original terms. Exact notices,
transitive inventory, source/relinking obligations, patent review and commercial
redistribution approval remain separate open release gates. This is internal
engineering enablement only. See WINDOWS_DECODER_COMPATIBILITY.md,
COMMERCIAL_DEPENDENCY_POLICY.md and THIRD_PARTY_NOTICES.md.

The old installer artifacts remain unchanged. A source commit, unit regressions
or the earlier eight-frame component probe does not qualify this integration.
A new exact-source installer must pass actual installed GUI video, external
public RTSP analytics/tracking/rendered boxes/presentation with zero analytics
failures and zero retained public media. Installer lifecycle, physical visual
signoff and the remaining Issue 199 admin/user/record/playback requirements remain
independent gates. PR 319 remains draft; no beta or owner-test-ready claim.
