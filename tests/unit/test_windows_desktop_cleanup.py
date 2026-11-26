"""Preserve real cleanup failures while tolerating a concurrently removed child."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest


def _desktop():
    path = Path(__file__).resolve().parents[1] / "integration/test_windows_desktop_app.py"
    spec = importlib.util.spec_from_file_location("k5_desktop_cleanup_helpers", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_actual_tree_removal_accepts_a_descendant_removed_after_directory_scan(
    tmp_path, monkeypatch
):
    desktop = _desktop()
    root = tmp_path / "owned proof"
    root.mkdir()
    child = root / "vanishing.tmp"
    child.write_bytes(b"generated")
    original = os.unlink
    removed = []

    def concurrent_unlink(path, *args, **kwargs):
        if Path(path).name == child.name and not removed:
            original(path, *args, **kwargs)
            removed.append(True)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", concurrent_unlink)
    desktop._remove_owned_temporary_tree(root)
    assert removed == [True]
    assert not root.exists()


@pytest.mark.parametrize("kind", [PermissionError, OSError])
def test_actual_tree_removal_preserves_other_descendant_failures(tmp_path, monkeypatch, kind):
    desktop = _desktop()
    root = tmp_path / "owned proof"
    root.mkdir()
    child = root / "refused.tmp"
    child.touch()
    error = kind("generated refusal")
    original = os.unlink

    def refused(path, *args, **kwargs):
        if Path(path).name == child.name:
            raise error
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", refused)
    with pytest.raises(kind) as caught:
        desktop._remove_owned_temporary_tree(root)
    assert caught.value is error
    assert child.exists()


@pytest.mark.parametrize("location", ["root", "outside"])
def test_missing_owned_root_or_unrelated_path_is_not_ignored(tmp_path, monkeypatch, location):
    desktop = _desktop()
    root = tmp_path / "owned proof"
    root.mkdir()
    error = FileNotFoundError("generated disappearance")
    target = root if location == "root" else tmp_path / "unrelated"

    def disappearing(_root, *, onexc):
        onexc(os.unlink, str(target), error)

    monkeypatch.setattr(desktop.shutil, "rmtree", disappearing)
    with pytest.raises(FileNotFoundError) as caught:
        desktop._remove_owned_temporary_tree(root)
    assert caught.value is error


def test_success_return_requires_owned_root_to_be_absent(tmp_path, monkeypatch):
    desktop = _desktop()
    root = tmp_path / "owned proof"
    root.mkdir()
    monkeypatch.setattr(desktop.shutil, "rmtree", lambda *_args, **_kwargs: None)
    with pytest.raises(OSError, match="remains after removal"):
        desktop._remove_owned_temporary_tree(root)
