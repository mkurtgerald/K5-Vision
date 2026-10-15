"""Identity-bound H.264 selection for the Windows engineering installer.

Selection happens before pipeline creation, never after a decoding failure.
Only fixed mode messages reach the GUI. No sources, media or credentials enter
this module; component admission is not installed-product acceptance.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import pathlib
import re
import stat

HARDWARE = "d3d11h264dec"
SOFTWARE = "avdec_h264"
REVIEWED_FILES = (
    (
        "lib/gstreamer-1.0",
        ("gstlibav.dll", "libgstlibav.dll"),
        "b386b9f30c99ac6f00164831fbfbbd01c165526c0a3c8a0ac97abedfdb80092b",
    ),
    (
        "bin",
        ("avcodec-61.dll", "libavcodec-61.dll"),
        "7920e5f71dd28f7216e80131ad056d751c32732485e7f0acd34554cc13a2069d",
    ),
    (
        "bin",
        ("gstreamer-1.0-0.dll", "libgstreamer-1.0-0.dll"),
        "9728a996dfeb03c79b20310a43967f32c41f1c3ab8d2dd15b2a6d27ff206300d",
    ),
    (
        "bin",
        ("gstapp-1.0-0.dll", "libgstapp-1.0-0.dll"),
        "18a89a7c409284e72ae1e6ba7964d632c35915cdd131274c9df2020b98917d1e",
    ),
)
_MODE_MESSAGES = {
    HARDWARE: "K5 decoder selected: hardware-h264",
    SOFTWARE: "K5 decoder selected: reviewed-software-h264 (engineering build)",
}


class DecoderSelectionError(RuntimeError):
    """Fixed failure identifiers, never native error text or installed paths."""


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise DecoderSelectionError(reason)


def bind(library: object, symbol: str, args: list, result: object):
    function = getattr(library, symbol)
    function.argtypes, function.restype = args, result
    return function


def _admit_directory(path: pathlib.Path) -> None:
    metadata = path.lstat()
    require(
        stat.S_ISDIR(metadata.st_mode)
        and not path.is_symlink()
        and not getattr(metadata, "st_file_attributes", 0) & 0x400,
        "decoder_identity_rejected",
    )


def exact_files(root: pathlib.Path) -> tuple[pathlib.Path, ...]:
    _admit_directory(root)
    result = []
    for relative, names, expected in REVIEWED_FILES:
        directory = root
        for part in pathlib.PurePosixPath(relative).parts:
            directory /= part
            _admit_directory(directory)
        candidates = [directory / name for name in names if (directory / name).is_file()]
        require(len(candidates) == 1, "decoder_identity_rejected")
        path = candidates[0]
        metadata = path.lstat()
        require(
            stat.S_ISREG(metadata.st_mode)
            and not path.is_symlink()
            and not getattr(metadata, "st_file_attributes", 0) & 0x400
            and 0 < metadata.st_size <= 256 * 1024 * 1024,
            "decoder_identity_rejected",
        )
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            require(
                (opened.st_dev, opened.st_ino, opened.st_size)
                == (metadata.st_dev, metadata.st_ino, metadata.st_size),
                "decoder_identity_rejected",
            )
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        require(digest == expected, "decoder_identity_rejected")
        require(path.resolve().parent == directory.resolve(), "decoder_identity_rejected")
        result.append(path.resolve())
    return tuple(result)


def loaded_origin(library: object) -> pathlib.Path:
    """Check the loaded HMODULE, not just the name requested from the loader."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    filename = bind(
        kernel,
        "GetModuleFileNameW",
        [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32],
        ctypes.c_uint32,
    )
    handle = getattr(library, "_handle", 0)
    require(bool(handle), "decoder_identity_rejected")
    buffer = ctypes.create_unicode_buffer(32768)
    length = filename(handle, buffer, len(buffer))
    require(0 < length < len(buffer), "decoder_identity_rejected")
    return pathlib.Path(buffer.value).resolve()


def check_codec(codec: object) -> None:
    license_text = bind(codec, "avcodec_license", [], ctypes.c_char_p)()
    configuration = bind(codec, "avcodec_configuration", [], ctypes.c_char_p)()
    version = bind(codec, "avcodec_version", [], ctypes.c_uint)()
    require(license_text == b"LGPL version 2.1 or later", "decoder_license_rejected")
    require(
        isinstance(configuration, bytes) and 0 < len(configuration) <= 32768,
        "decoder_license_rejected",
    )
    forbidden = (
        rb"(?:--enable-(?:gpl|nonfree|version3)"
        rb"|(?:gpl|nonfree|version3)\s*=\s*(?:true|enabled|1))"
    )
    require(re.search(forbidden, configuration, re.I) is None, "decoder_license_rejected")
    require(version == 4002660, "decoder_identity_rejected")  # libavcodec 61.19.100


def _selected(element: str) -> str:
    # The installed service's stdout is already projected through the GUI.
    # Do not start a separate unbounded preflight or transmit native diagnostics.
    print(_MODE_MESSAGES[element], flush=True)
    return element


def select_decoder(
    core: object, root: pathlib.Path, *, allow_reviewed_software: bool = False
) -> str:
    """Preserve D3D11; explicitly admit only the reviewed engineering alternative."""
    try:
        pointer, string = ctypes.c_void_p, ctypes.c_char_p
        factory_find = bind(core, "gst_element_factory_find", [string], pointer)
        unref = bind(core, "gst_object_unref", [pointer], None)
        hardware = factory_find(b"d3d11h264dec")
        if hardware:
            unref(hardware)
            return _selected(HARDWARE)
        require(allow_reviewed_software is True, "hardware_decoder_unavailable")
        paths = exact_files(pathlib.Path(root))
        require(loaded_origin(core) == paths[2], "decoder_identity_rejected")
        loader = getattr(ctypes, "WinDLL", None)
        require(loader is not None, "decoder_platform_unavailable")
        codec, app = loader(str(paths[1])), loader(str(paths[3]))
        require(loaded_origin(codec) == paths[1], "decoder_identity_rejected")
        require(loaded_origin(app) == paths[3], "decoder_identity_rejected")
        check_codec(codec)
        software = factory_find(b"avdec_h264")
        require(bool(software), "software_decoder_unavailable")
        plugin = None
        try:
            plugin = bind(core, "gst_plugin_feature_get_plugin", [pointer], pointer)(software)
            require(bool(plugin), "decoder_identity_rejected")
            filename = bind(core, "gst_plugin_get_filename", [pointer], string)(plugin)
            version = bind(core, "gst_plugin_get_version", [pointer], string)(plugin)
            license_text = bind(core, "gst_plugin_get_license", [pointer], string)(plugin)
            require(
                isinstance(filename, bytes) and 0 < len(filename) <= 32768,
                "decoder_identity_rejected",
            )
            require(
                pathlib.Path(filename.decode("utf-8")).resolve() == paths[0],
                "decoder_identity_rejected",
            )
            require(version == b"1.28.7" and license_text == b"LGPL", "decoder_license_rejected")
            loaded_plugin = loader(str(paths[0]))
            require(loaded_origin(loaded_plugin) == paths[0], "decoder_identity_rejected")
            require(exact_files(pathlib.Path(root)) == paths, "decoder_identity_rejected")
            return _selected(SOFTWARE)
        finally:
            if plugin:
                unref(plugin)
            unref(software)
    except DecoderSelectionError:
        raise
    except (AttributeError, OSError, UnicodeError, TypeError, ValueError):
        raise DecoderSelectionError("decoder_selection_failed") from None
