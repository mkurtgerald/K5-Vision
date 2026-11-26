"""The kernel probe must exercise unambiguous, unchanged real handle methods."""

from __future__ import annotations

import hashlib
import runpy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PROBE = runpy.run_path(str(ROOT / "scripts/probe_windows_directory_pin.py"))
SOURCE = (ROOT / PROBE["SOURCE"]).read_text(encoding="utf-8")


def test_extracts_actual_handle_methods_and_directory_only_pin_arguments():
    adapter, arguments, binding = PROBE["extract"](SOURCE)
    assert adapter._open_handle.__code__.co_filename == "<actual_native_directory_handle_methods>"
    assert adapter._handle_key.__code__.co_filename == "<actual_native_directory_handle_methods>"
    assert arguments == {"access": 0x20081, "share": 3, "create": 3, "directory": True}
    assert binding["source_sha256"] == hashlib.sha256(SOURCE.encode()).hexdigest()


@pytest.mark.parametrize(
    "failure",
    [
        "duplicate_stage",
        "duplicate_open_method",
        "duplicate_pin_call",
        "dynamic_access",
        "boolean_access",
        "delete_sharing",
    ],
)
def test_ambiguous_or_changed_native_call_is_not_silently_admitted(failure):
    source = SOURCE
    if failure == "duplicate_stage":
        source += "\nclass _WindowsStage: pass\n"
    elif failure == "duplicate_open_method":
        source = source.replace(
            "    def _open_handle(self, path,",
            "    def _open_handle(self): pass\n\n    def _open_handle(self, path,",
        )
    elif failure == "duplicate_pin_call":
        anchor = (
            "handle = self._open_handle(path, access=0x20081, share=3, create=3, directory=True)"
        )
        source = source.replace(anchor, anchor + "\n                " + anchor)
    elif failure == "dynamic_access":
        source = source.replace("access=0x20081", "access=selected_access")
    elif failure == "boolean_access":
        source = source.replace("access=0x20081", "access=True")
    elif failure == "delete_sharing":
        source = source.replace("access=0x20081, share=3", "access=0x20081, share=7")
    with pytest.raises(ValueError):
        PROBE["extract"](source)


def test_probe_source_reader_refuses_symlinks(tmp_path):
    original, linked = tmp_path / "owned.py", tmp_path / "link.py"
    original.write_text(SOURCE)
    linked.symlink_to(original)
    with pytest.raises(ValueError):
        PROBE["_source"](linked)
