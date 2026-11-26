"""Source-derived sharing probes cannot silently substitute a relaxed pin."""

from __future__ import annotations

import hashlib
import runpy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PROBE = runpy.run_path(str(ROOT / "scripts/probe_windows_clock_storage_pins.py"))
CLOCK = (ROOT / PROBE["CLOCK"]).read_text(encoding="utf-8")
STORAGE = (ROOT / PROBE["STORAGE"]).read_text(encoding="utf-8")
OLD_CLOCK = CLOCK.replace(
    "CreateFile(ancestor, 1, 3, IntPtr.Zero,", "CreateFile(ancestor, 0, 3, IntPtr.Zero,"
)
OLD_STORAGE = STORAGE.replace(
    "str(directory), 1, 3, None, 3, 0x02000000 | 0x00200000, None",
    "str(directory), 0, 3, None, 3, 0x02000000 | 0x00200000, None",
)


def test_actual_two_source_calls_and_native_declarations_bind_causal_probe():
    generated, old_ast, current_ast, binding = PROBE["materialize"](
        OLD_CLOCK, CLOCK, OLD_STORAGE, STORAGE
    )
    assert (
        hashlib.sha256(OLD_CLOCK.encode()).hexdigest() == PROBE["BASELINE_HASHES"][PROBE["CLOCK"]]
    )
    assert (
        hashlib.sha256(OLD_STORAGE.encode()).hexdigest()
        == PROBE["BASELINE_HASHES"][PROBE["STORAGE"]]
    )
    old_call, old_access, old_declarations, _ = PROBE["extract_clock"](OLD_CLOCK)
    current_call, access, declarations, _ = PROBE["extract_clock"](CLOCK)
    assert (old_access, access) == (0, 1)
    assert old_declarations == declarations
    assert old_call in generated and current_call in generated
    assert declarations in generated
    assert "DeleteOpen(args[0], false, 32);" in generated
    assert "@@" not in generated
    assert binding["generated_csharp_sha256"] == hashlib.sha256(generated.encode()).hexdigest()
    compile(old_ast, "<actual_old_storage_open>", "exec")
    compile(current_ast, "<actual_current_storage_open>", "exec")


@pytest.mark.parametrize(
    "mutation",
    [
        "other_clock_change",
        "other_storage_change",
        "clock_share",
        "storage_share",
        "clock_access",
        "storage_access",
        "duplicate_clock_call",
        "duplicate_storage_call",
        "clock_signature",
        "clock_layout",
        "storage_declarations",
    ],
)
def test_mutated_or_ambiguous_sources_fail_closed(mutation):
    clock, storage = CLOCK, STORAGE
    if mutation == "other_clock_change":
        clock = clock.replace(
            "private sealed class DirectoryPins", "private sealed class ChangedPins"
        )
    elif mutation == "other_storage_change":
        storage = storage.replace('"Storage parent must already exist"', '"Changed"')
    elif mutation == "clock_share":
        clock = clock.replace("CreateFile(ancestor, 1, 3,", "CreateFile(ancestor, 1, 7,")
    elif mutation == "storage_share":
        storage = storage.replace("str(directory), 1, 3,", "str(directory), 1, 7,")
    elif mutation == "clock_access":
        clock = clock.replace("CreateFile(ancestor, 1,", "CreateFile(ancestor, 0,")
    elif mutation == "storage_access":
        storage = storage.replace("str(directory), 1,", "str(directory), True,")
    elif mutation == "duplicate_clock_call":
        clock = clock.replace(
            "SafeFileHandle handle = CreateFile(ancestor",
            "SafeFileHandle extra = CreateFile(ancestor, 1, 3, IntPtr.Zero, "
            "3, 0x02000000u | 0x00200000u, IntPtr.Zero);\n"
            "                    SafeFileHandle handle = CreateFile(ancestor",
        )
    elif mutation == "duplicate_storage_call":
        storage = storage.replace(
            "            handles.append(handle)",
            "            extra = kernel.CreateFileW(str(directory), 1, 3, None, "
            "3, 0x02000000 | 0x00200000, None)\n            handles.append(handle)",
        )
    elif mutation == "clock_signature":
        clock = clock.replace(
            "CreateFile(string path, uint access", "CreateFile(string path, int access"
        )
    elif mutation == "clock_layout":
        clock = clock.replace("private struct FileIdentity", "private struct ChangedIdentity")
    else:
        storage = storage.replace(
            "    kernel.CloseHandle.argtypes = [wintypes.HANDLE]",
            "    kernel.CloseHandle.argtypes = [ctypes.c_uint32]",
        )
    with pytest.raises(ValueError):
        PROBE["materialize"](OLD_CLOCK, clock, OLD_STORAGE, storage)


def test_extracted_python_call_executes_actual_pointer_declarations_and_access(monkeypatch):
    import ctypes
    from ctypes import wintypes
    from types import SimpleNamespace

    observed = []

    class Call:
        def __call__(self, *arguments):
            observed.append(arguments)
            return 0x123456789ABC

    kernel = SimpleNamespace(CreateFileW=Call(), CloseHandle=Call())
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: kernel, raising=False)
    for source, access in ((OLD_STORAGE, 0), (STORAGE, 1)):
        selected, actual_access, _ = PROBE["extract_storage"](source)
        namespace = {"ctypes": ctypes, "wintypes": wintypes}
        exec(compile(selected, "<actual_storage_open>", "exec"), namespace)
        assert namespace["opened"](Path("owned")) == 0x123456789ABC
        assert observed[-1] == ("owned", access, 3, None, 3, 0x02200000, None)
        assert actual_access == access
        assert kernel.CreateFileW.argtypes[0] is wintypes.LPCWSTR
        assert kernel.CreateFileW.restype is wintypes.HANDLE
        assert kernel.CloseHandle.argtypes == [wintypes.HANDLE]
