# Stage 19 Native Decoder ABI Review

Status: **APPROVED FOR EXACT-REVISION QUALIFICATION.**

Stage 19 adds no third-party Python package, codec library, or runtime. It qualifies a narrow `ctypes` bridge to the C ABI already shipped inside the Stage-18 accepted isolated GStreamer `1.28.7` runtime.

## Rationale

GStreamer explicitly exposes application-facing `appsrc` and `appsink` C APIs. The accepted Stage-18 runtime already physically qualified the H.264 decode element chain and its effective LGPL metadata. Using Python standard-library `ctypes` against the existing runtime avoids adding a second Python binding/package surface solely to cross the application boundary.

The gate does **not** yet execute media through the ABI. It proves only that the exact isolated runtime exposes the small function surface needed by the subsequent adapter gate.

## Qualified libraries and exports

Core GStreamer runtime library:

- `gst_init_check`
- `gst_parse_launch`
- `gst_bin_get_by_name`
- `gst_element_set_state`
- `gst_object_unref`
- `gst_buffer_new_allocate`
- `gst_buffer_fill`
- `gst_buffer_get_size`
- `gst_buffer_map`
- `gst_buffer_unmap`
- `gst_sample_get_buffer`
- `gst_sample_get_caps`
- `gst_caps_get_structure`
- `gst_structure_get_int`
- `gst_mini_object_unref`

GstApp runtime library:

- `gst_app_src_push_buffer`
- `gst_app_src_end_of_stream`
- `gst_app_sink_try_pull_sample`
- `gst_app_sink_is_eos`

Only the DLL basenames and this fixed export allowlist may be retained. Absolute DLL/runtime paths are excluded from evidence.

## Upstream basis

GStreamer documents `appsrc` push mode as an application-controlled data injection boundary and `appsink` as the application-facing sample retrieval API. It also documents `gst_app_src_push_buffer()` as the type-safe application API for appsrc and `gst_app_sink_try_pull_sample()` as the bounded sample retrieval API.

Upstream references:

- https://gstreamer.freedesktop.org/documentation/applib/gstappsrc.html
- https://gstreamer.freedesktop.org/documentation/applib/gstappsink.html
- https://gstreamer.freedesktop.org/documentation/tutorials/basic/short-cutting-the-pipeline.html
- https://gstreamer.freedesktop.org/documentation/installing/on-windows.html

## Commercial/provenance conclusion

No new externally distributed component is adopted by Stage 19. The governing external runtime remains the exact Stage-18 reviewed GStreamer `1.28.7` surface. Python `ctypes` is part of the Python standard library. The gate must reproduce Stage-18 runtime qualification before ABI evidence is accepted.

Any later expansion of native symbols, GStreamer elements, Python bindings, codec runtimes, or plugin surface requires a separate review before merge.

This is an engineering dependency review, not legal advice.
