"""The engineering codec probe cannot silently grant product or release approval."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "codec_review", ROOT / "scripts/qualify_owner_software_decoder.py"
)
assert SPEC and SPEC.loader
review = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(review)


def test_review_accepts_explicit_lgpl_only_build_metadata():
    review.review_license("LGPL version 2.1 or later", "--enable-shared --disable-gpl")


@pytest.mark.parametrize(
    ("license_text", "configuration"),
    [
        ("GPL version 2 or later", "--enable-shared"),
        ("LGPL version 3 or later", "--enable-shared"),
        ("unknown", "--enable-shared"),
        ("LGPL version 2.1 or later", "--enable-gpl"),
        ("LGPL version 2.1 or later", "--enable-nonfree"),
        ("LGPL version 2.1 or later", "-Dgpl=true"),
        ("LGPL version 2.1 or later", "nonfree = enabled"),
        ("LGPL version 2.1 or later", "version3=1"),
        ("LGPL version 2.1 or later", ""),
        ("LGPL version 2.1 or later", "x" * 32769),
    ],
)
def test_unknown_or_restricted_metadata_is_rejected(license_text, configuration):
    with pytest.raises(review.ReviewFailure):
        review.review_license(license_text, configuration)


def metadata(plugin):
    return f"  Name libav\n  Filename {plugin}\n  Version 1.28.7\n  License LGPL\n"


def test_plugin_metadata_binds_exact_version_license_and_origin(tmp_path):
    plugin = tmp_path / "gstlibav.dll"
    review.review_metadata(metadata(plugin), plugin)
    for changed in (
        metadata(plugin).replace("1.28.7", "1.28.6"),
        metadata(plugin).replace("License LGPL", "License GPL"),
        metadata(tmp_path / "foreign.dll"),
        metadata(plugin) + "  Name libav\n",
        metadata(plugin).replace("  License LGPL\n", ""),
    ):
        with pytest.raises(review.ReviewFailure):
            review.review_metadata(changed, plugin)


def test_exact_file_cannot_select_ambiguous_dll(tmp_path):
    for name in ("avcodec-61.dll", "libavcodec-61.dll"):
        (tmp_path / name).write_bytes(b"fixture-only")
    with pytest.raises(review.ReviewFailure, match="codec_file"):
        review.exact_file(tmp_path, ("avcodec-61.dll", "libavcodec-61.dll"), "codec_file")


def native_fixture():
    core, app = Mock(), Mock()
    core.gst_init_check.return_value = 1
    core.gst_parse_launch_full.return_value = 10
    core.gst_bin_get_by_name.return_value = 20
    core.gst_element_set_state.return_value = 1
    core.gst_sample_get_buffer.return_value = 30
    core.gst_buffer_get_size.return_value = 320 * 240 * 3
    app.gst_app_sink_try_pull_sample.side_effect = [40] * 8 + [None]
    app.gst_app_sink_is_eos.return_value = 1
    return core, app


def test_synthetic_probe_requires_eight_real_samples_and_strict_parse():
    core, app = native_fixture()
    assert review.decode_synthetic(core, app) == 8
    assert core.gst_parse_launch_full.call_args.args[1:] == (None, 1, None)
    assert core.gst_mini_object_unref.call_count == 8
    assert core.gst_object_unref.call_count == 2
    assert "videotestsrc num-buffers=8" in review.PIPELINE
    assert "avdec_h264" in review.PIPELINE
    assert all(word not in review.PIPELINE for word in ("rtsp", "filesink", "urisourcebin"))


def test_partial_pipeline_and_wrong_frame_shape_never_pass():
    core, app = native_fixture()
    core.gst_parse_launch_full.return_value = None
    with pytest.raises(review.ReviewFailure, match="synthetic_pipeline_parse"):
        review.decode_synthetic(core, app)
    core.gst_element_set_state.assert_not_called()
    core, app = native_fixture()
    core.gst_buffer_get_size.return_value = 1
    with pytest.raises(review.ReviewFailure, match="decoded_frame_shape"):
        review.decode_synthetic(core, app)
    assert core.gst_mini_object_unref.call_count == 1
    assert core.gst_object_unref.call_count == 2


def test_license_precedes_decode_and_product_never_changes():
    source = (ROOT / "scripts/qualify_owner_software_decoder.py").read_text()
    main = source[source.index("def main()") :]
    license_check = main.index("review_license(license_text, configuration)")
    assert license_check < main.index("decode_synthetic(")
    assert "K5_CODEC_REVIEW_PRODUCT_CHANGED=0" in main
    assert "K5_OWNER_PUBLIC_ACCEPTANCE=not_proven" in main
    assert "write_bytes" not in source and "write_text" not in source
    workflow = (ROOT / ".github/workflows/owner-decoder-compatibility.yml").read_text()
    assert review.INSTALLER_SHA256 in workflow
    assert "if: always() && steps.install.outputs.owned_install == 'true'" in workflow
    assert "persist-credentials: false" in workflow
    assert "self-hosted" not in workflow
    assert "K5VisionAlpha-Setup" not in workflow
