"""Camera-free decoder selection and substituted-binary rejection regressions."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from k5vision.media import reviewed_h264_selection as selection


@pytest.fixture
def admitted(tmp_path, monkeypatch):
    records, paths = [], []
    for relative, names, _ in selection.REVIEWED_FILES:
        path = tmp_path / relative / names[0]
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = path.name.encode()
        path.write_bytes(payload)
        records.append((relative, names, hashlib.sha256(payload).hexdigest()))
        paths.append(path.resolve())
    monkeypatch.setattr(selection, "REVIEWED_FILES", tuple(records))
    core = Mock()
    core.gst_element_factory_find.side_effect = lambda name: 22 if name == b"avdec_h264" else 0
    core.gst_plugin_feature_get_plugin.return_value = 33
    core.gst_plugin_get_filename.return_value = str(paths[0]).encode()
    core.gst_plugin_get_version.return_value = b"1.28.7"
    core.gst_plugin_get_license.return_value = b"LGPL"
    codec = Mock()
    codec.avcodec_license.return_value = b"LGPL version 2.1 or later"
    codec.avcodec_configuration.return_value = b"--disable-gpl --disable-nonfree --disable-version3"
    codec.avcodec_version.return_value = 4002660
    app, plugin = Mock(), Mock()
    mapping = {str(paths[0]): plugin, str(paths[1]): codec, str(paths[3]): app}
    origins = {id(core): paths[2], id(codec): paths[1], id(app): paths[3], id(plugin): paths[0]}
    loader = Mock(side_effect=lambda filename: mapping[filename])
    monkeypatch.setattr(selection.ctypes, "WinDLL", loader, raising=False)
    monkeypatch.setattr(selection, "loaded_origin", lambda library: origins[id(library)])
    return SimpleNamespace(
        root=tmp_path,
        paths=paths,
        core=core,
        codec=codec,
        app=app,
        plugin=plugin,
        loader=loader,
        origins=origins,
    )


def test_registered_hardware_preserved_without_software_admission(admitted, capsys):
    admitted.core.gst_element_factory_find.side_effect = lambda name: 11
    assert selection.select_decoder(admitted.core, admitted.root) == selection.HARDWARE
    admitted.loader.assert_not_called()
    admitted.core.gst_object_unref.assert_called_once_with(11)
    assert capsys.readouterr().out == "K5 decoder selected: hardware-h264\n"


def test_software_requires_explicit_engineering_admission(admitted):
    with pytest.raises(selection.DecoderSelectionError, match="hardware_decoder_unavailable"):
        selection.select_decoder(admitted.core, admitted.root)
    admitted.loader.assert_not_called()


@pytest.mark.parametrize("permission", [None, 1, "true", False])
def test_truthy_substitutes_do_not_enable_software(admitted, permission):
    with pytest.raises(selection.DecoderSelectionError):
        selection.select_decoder(admitted.core, admitted.root, allow_reviewed_software=permission)


def test_reviewed_software_selected_and_native_references_released(admitted, capsys):
    result = selection.select_decoder(admitted.core, admitted.root, allow_reviewed_software=True)
    assert result == selection.SOFTWARE
    assert [call.args[0] for call in admitted.core.gst_object_unref.call_args_list] == [33, 22]
    assert capsys.readouterr().out == (
        "K5 decoder selected: reviewed-software-h264 (engineering build)\n"
    )


@pytest.mark.parametrize("index", range(4))
def test_changed_binary_refused_before_loading_software(admitted, index):
    admitted.paths[index].write_bytes(b"changed-private-fixture-marker")
    with pytest.raises(selection.DecoderSelectionError, match="decoder_identity_rejected"):
        selection.select_decoder(admitted.core, admitted.root, allow_reviewed_software=True)
    admitted.loader.assert_not_called()


@pytest.mark.parametrize("index", range(4))
def test_alternative_loaded_module_origin_is_refused(admitted, index, capsys):
    library = (admitted.plugin, admitted.codec, admitted.core, admitted.app)[index]
    admitted.origins[id(library)] = admitted.root / "substituted.dll"
    with pytest.raises(selection.DecoderSelectionError, match="decoder_identity_rejected"):
        selection.select_decoder(admitted.core, admitted.root, allow_reviewed_software=True)
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("flag", [b"--enable-gpl", b"--enable-nonfree", b"--enable-version3"])
def test_unreviewed_codec_configuration_refused(admitted, flag):
    admitted.codec.avcodec_configuration.return_value = flag
    with pytest.raises(selection.DecoderSelectionError, match="decoder_license_rejected"):
        selection.select_decoder(admitted.core, admitted.root, allow_reviewed_software=True)


def test_wrong_license_refused(admitted):
    admitted.codec.avcodec_license.return_value = b"GPL version 3 or later"
    with pytest.raises(selection.DecoderSelectionError, match="decoder_license_rejected"):
        selection.select_decoder(admitted.core, admitted.root, allow_reviewed_software=True)


def test_wrong_codec_version_refused(admitted):
    admitted.codec.avcodec_version.return_value = 4002661
    with pytest.raises(selection.DecoderSelectionError, match="decoder_identity_rejected"):
        selection.select_decoder(admitted.core, admitted.root, allow_reviewed_software=True)


@pytest.mark.parametrize(
    "attribute,value",
    [
        ("gst_plugin_get_filename", b"/substituted/private-fixture-marker.dll"),
        ("gst_plugin_get_version", b"1.28.8"),
        ("gst_plugin_get_license", b"GPL"),
    ],
)
def test_plugin_metadata_refused_and_references_released(admitted, attribute, value, capsys):
    getattr(admitted.core, attribute).return_value = value
    with pytest.raises(selection.DecoderSelectionError) as error:
        selection.select_decoder(admitted.core, admitted.root, allow_reviewed_software=True)
    assert "private-fixture-marker" not in str(error.value)
    assert [call.args[0] for call in admitted.core.gst_object_unref.call_args_list] == [33, 22]
    assert capsys.readouterr().out == ""


def test_missing_software_factory_refused(admitted):
    admitted.core.gst_element_factory_find.side_effect = lambda name: 0
    with pytest.raises(selection.DecoderSelectionError, match="software_decoder_unavailable"):
        selection.select_decoder(admitted.core, admitted.root, allow_reviewed_software=True)


def test_changed_binary_after_plugin_loading_is_refused(admitted):
    def changed_plugin(_):
        admitted.paths[0].write_bytes(b"modified")
        return b"LGPL"

    admitted.core.gst_plugin_get_license.side_effect = changed_plugin
    with pytest.raises(selection.DecoderSelectionError, match="decoder_identity_rejected"):
        selection.select_decoder(admitted.core, admitted.root, allow_reviewed_software=True)


def test_alias_ambiguity_refused(admitted):
    (admitted.root / "bin" / "libavcodec-61.dll").write_bytes(b"unexpected-alias")
    with pytest.raises(selection.DecoderSelectionError, match="decoder_identity_rejected"):
        selection.exact_files(admitted.root)


def test_parent_link_refused(admitted):
    directory = admitted.root / "lib"
    moved = admitted.root / "relocated"
    directory.rename(moved)
    try:
        directory.symlink_to(moved, target_is_directory=True)
    except OSError:
        pytest.skip("Symlink creation unavailable to this unprivileged test worker")
    with pytest.raises(selection.DecoderSelectionError, match="decoder_identity_rejected"):
        selection.exact_files(admitted.root)


def test_native_exception_is_sanitized(admitted, capsys):
    admitted.core.gst_element_factory_find.side_effect = OSError("private-fixture-marker")
    with pytest.raises(selection.DecoderSelectionError, match="decoder_selection_failed") as error:
        selection.select_decoder(admitted.core, admitted.root, allow_reviewed_software=True)
    assert "private-fixture-marker" not in str(error.value)
    assert capsys.readouterr().out == ""


def test_loaded_origin_checks_native_handle_not_requested_name(monkeypatch, tmp_path):
    expected = tmp_path / "actual.dll"

    def module_filename(handle, buffer, length):
        assert handle == 123 and length == 32768
        buffer.value = str(expected)
        return len(str(expected))

    kernel = Mock()
    kernel.GetModuleFileNameW.side_effect = module_filename
    monkeypatch.setattr(selection.ctypes, "WinDLL", Mock(return_value=kernel), raising=False)
    assert selection.loaded_origin(SimpleNamespace(_handle=123, _name="false.dll")) == expected


@pytest.mark.parametrize("length", [0, 32768])
def test_failed_or_truncated_loaded_path_is_refused(monkeypatch, length):
    kernel = Mock()
    kernel.GetModuleFileNameW.return_value = length
    monkeypatch.setattr(selection.ctypes, "WinDLL", Mock(return_value=kernel), raising=False)
    with pytest.raises(selection.DecoderSelectionError, match="decoder_identity_rejected"):
        selection.loaded_origin(SimpleNamespace(_handle=123))
