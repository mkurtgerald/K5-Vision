"""Review the exact installed GStreamer software decoder; synthetic memory only.

This is a hosted engineering probe, never a K5 product fallback or public witness.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import pathlib
import re
import subprocess
import sys
import threading
import time

VERSION = "1.28.7"
INSTALLER_SHA256 = "032fc6062b8539838fc8da22589cb9b24c5d820baa7f8cc160af9ea08395badf"
PIPELINE = (
    "videotestsrc num-buffers=8 ! video/x-raw,width=320,height=240,framerate=10/1 "
    "! videoconvert ! x264enc tune=zerolatency ! video/x-h264,profile=baseline "
    "! h264parse ! avdec_h264 ! videoconvert ! video/x-raw,format=BGR "
    "! appsink name=k5review sync=false max-buffers=8 drop=false"
)


class ReviewFailure(RuntimeError):
    pass


def require(condition: bool, code: str) -> None:
    if not condition:
        raise ReviewFailure(code)


def review_license(license_text: str, configuration: str) -> None:
    require(license_text == "LGPL version 2.1 or later", "license_not_admitted")
    require(0 < len(configuration) <= 32768, "configuration_not_admitted")
    forbidden = r"(?:--enable-(?:gpl|nonfree|version3)|(?:gpl|nonfree|version3)\s*=\s*(?:true|enabled|1))"
    require(re.search(forbidden, configuration, re.IGNORECASE) is None, "configuration_not_admitted")


def review_metadata(text: str, plugin: pathlib.Path) -> None:
    require(len(text.encode("utf-8")) <= 65536, "plugin_metadata_not_admitted")
    fields = {}
    for name in ("Name", "Filename", "Version", "License"):
        values = re.findall(r"^  " + name + r"\s+(.+?)\s*$", text, re.MULTILINE)
        require(len(values) == 1, "plugin_metadata_not_admitted")
        fields[name] = values[0]
    require(fields["Name"] == "libav" and fields["Version"] == VERSION, "plugin_metadata_not_admitted")
    require(fields["License"] == "LGPL", "plugin_license_not_admitted")
    require(pathlib.Path(fields["Filename"]).resolve() == plugin.resolve(), "plugin_origin_not_admitted")


def exact_file(directory: pathlib.Path, names: tuple[str, ...], code: str) -> pathlib.Path:
    candidates = [directory / name for name in names if (directory / name).is_file()]
    require(len(candidates) == 1, code)
    path = candidates[0]
    require(not path.is_symlink() and path.resolve().parent == directory.resolve(), code)
    return path


def digest(path: pathlib.Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def inspect(executable: pathlib.Path, environment: dict[str, str]) -> str:
    process = subprocess.Popen(
        [str(executable), "avdec_h264"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    buffers = [bytearray(), bytearray()]
    overflow = threading.Event()

    def drain(stream, target):
        while block := stream.read(4096):
            if len(target) + len(block) > 65536:
                overflow.set()
            else:
                target.extend(block)

    threads = [
        threading.Thread(target=drain, args=(stream, target), daemon=True)
        for stream, target in zip((process.stdout, process.stderr), buffers)
    ]
    for thread in threads:
        thread.start()
    try:
        code = process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
        raise ReviewFailure("plugin_inspection_timeout") from None
    finally:
        for thread in threads:
            thread.join(timeout=3)
    require(not overflow.is_set() and all(not t.is_alive() for t in threads), "plugin_output_bound")
    require(code == 0, "software_decoder_unavailable")
    return buffers[0].decode("utf-8", errors="strict")


def bind(library, symbol, arguments, result):
    function = getattr(library, symbol)
    function.argtypes, function.restype = arguments, result
    return function


def decode_synthetic(core, app) -> int:
    p, i, u, s = ctypes.c_void_p, ctypes.c_int, ctypes.c_uint64, ctypes.c_char_p
    init = bind(core, "gst_init_check", [p, p, p], i)
    parse = bind(core, "gst_parse_launch_full", [s, p, i, p], p)
    lookup = bind(core, "gst_bin_get_by_name", [p, s], p)
    state = bind(core, "gst_element_set_state", [p, i], i)
    unref = bind(core, "gst_object_unref", [p], None)
    mini_unref = bind(core, "gst_mini_object_unref", [p], None)
    sample_buffer = bind(core, "gst_sample_get_buffer", [p], p)
    size = bind(core, "gst_buffer_get_size", [p], ctypes.c_size_t)
    pull = bind(app, "gst_app_sink_try_pull_sample", [p, u], p)
    eos = bind(app, "gst_app_sink_is_eos", [p], i)
    require(bool(init(None, None, None)), "native_initialization")
    pipeline, sink, frames = None, None, 0
    try:
        pipeline = parse(PIPELINE.encode("ascii"), None, 1, None)
        require(bool(pipeline), "synthetic_pipeline_parse")
        sink = lookup(pipeline, b"k5review")
        require(bool(sink), "synthetic_sink")
        require(state(pipeline, 4) != 0, "synthetic_pipeline_start")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            sample = pull(sink, 500_000_000)
            if sample:
                try:
                    buffer = sample_buffer(sample)
                    require(bool(buffer) and size(buffer) == 320 * 240 * 3, "decoded_frame_shape")
                    frames += 1
                    require(frames <= 8, "decoded_frame_bound")
                finally:
                    mini_unref(sample)
            elif eos(sink):
                break
        require(frames == 8 and bool(eos(sink)), "decoded_frame_count")
        return frames
    finally:
        if pipeline:
            state(pipeline, 1)
        if sink:
            unref(sink)
        if pipeline:
            unref(pipeline)


def main() -> None:
    require(os.name == "nt" and os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted", "host_admission")
    require(os.environ.get("GITHUB_REPOSITORY") == "mkurtgerald/K5-Vision", "repository_admission")
    require(sys.version_info[:3] == (3, 12, 10), "python_admission")
    require(os.environ.get("K5_CODEC_INSTALLER_SHA256") == INSTALLER_SHA256, "installer_admission")
    root = pathlib.Path(os.environ["RUNNER_TEMP"]) / "k5-decoder-compatibility"
    require(root.is_dir() and not root.is_symlink(), "runtime_admission")
    binary = root / "bin"
    plugin = exact_file(root / "lib/gstreamer-1.0", ("gstlibav.dll", "libgstlibav.dll"), "plugin_file")
    codec = exact_file(binary, ("avcodec-61.dll", "libavcodec-61.dll"), "codec_file")
    core_path = exact_file(binary, ("gstreamer-1.0-0.dll", "libgstreamer-1.0-0.dll"), "core_file")
    app_path = exact_file(binary, ("gstapp-1.0-0.dll", "libgstapp-1.0-0.dll"), "app_file")
    paths = {"PLUGIN": plugin, "AVCODEC": codec, "CORE": core_path, "APP": app_path}
    before = {name: digest(path) for name, path in paths.items()}
    environment = os.environ.copy()
    environment["PATH"] = str(binary) + os.pathsep + environment.get("PATH", "")
    environment["GST_REGISTRY_1_0"] = str(root / "review-registry.bin")
    environment["GST_PLUGIN_PATH_1_0"] = ""
    environment["GST_PLUGIN_SYSTEM_PATH_1_0"] = str(plugin.parent)
    for name in tuple(environment):
        if name.startswith("K5_"):
            environment.pop(name)
    metadata = inspect(binary / "gst-inspect-1.0.exe", environment)
    review_metadata(metadata, plugin)
    os.environ.update({key: environment[key] for key in ("PATH", "GST_REGISTRY_1_0", "GST_PLUGIN_PATH_1_0", "GST_PLUGIN_SYSTEM_PATH_1_0")})
    with os.add_dll_directory(str(binary)):
        library = ctypes.CDLL(str(codec))
        license_text = bind(library, "avcodec_license", [], ctypes.c_char_p)().decode("ascii")
        configuration = bind(library, "avcodec_configuration", [], ctypes.c_char_p)().decode("ascii")
        version = int(bind(library, "avcodec_version", [], ctypes.c_uint)())
        review_license(license_text, configuration)
        require(version >> 16 == 61, "codec_version")
        frames = decode_synthetic(ctypes.CDLL(str(core_path)), ctypes.CDLL(str(app_path)))
    require(all(digest(path) == before[name] for name, path in paths.items()), "runtime_changed")
    print("K5_CODEC_REVIEW_LICENSE=LGPL_2_1_OR_LATER")
    print("K5_CODEC_REVIEW_AVCODEC_VERSION=" + str(version))
    for name, value in before.items():
        print("K5_CODEC_REVIEW_" + name + "_SHA256=" + value)
    print("K5_CODEC_REVIEW_SYNTHETIC_FRAMES=" + str(frames))
    print("K5_CODEC_REVIEW_RETAINED_MEDIA=0")
    print("K5_CODEC_REVIEW_PRODUCT_CHANGED=0")
    print("K5_OWNER_PUBLIC_ACCEPTANCE=not_proven")
    print("K5_CODEC_REVIEW=passed")


if __name__ == "__main__":
    try:
        main()
    except ReviewFailure as error:
        code = str(error)
        print("K5_CODEC_REVIEW_FAILURE=" + (code if re.fullmatch("[a-z_]{1,48}", code) else "refused"))
        raise SystemExit(1) from None
    except Exception:
        print("K5_CODEC_REVIEW_FAILURE=unexpected")
        raise SystemExit(1) from None
