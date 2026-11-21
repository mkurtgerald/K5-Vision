"""Read-only native ProgramData admission probe for disposable Windows builders.

Loads the exact stdlib-only identity policy source without starting K5. Output
contains fixed principal categories and numeric permission masks, never paths,
account names, source values, or exception text. A policy refusal is an observed
diagnostic outcome, not product qualification or permission to modify an ACL.
"""

from __future__ import annotations

import ctypes
import importlib.util
import json
import os
import sys
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]


def _program_data() -> Path:
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    ole = ctypes.WinDLL("ole32", use_last_error=True)
    shell.SHGetKnownFolderPath.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    shell.SHGetKnownFolderPath.restype = ctypes.c_int32
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole.CoTaskMemFree.restype = None
    identifier = ctypes.create_string_buffer(UUID("62ab5d82-fdc1-4dc3-a9dd-070d1d495d97").bytes_le)
    value = ctypes.c_void_p()
    try:
        if (
            shell.SHGetKnownFolderPath(identifier, 0, None, ctypes.byref(value)) != 0
            or not value.value
        ):
            raise ValueError
        return Path(ctypes.wstring_at(value))
    finally:
        if value.value:
            ole.CoTaskMemFree(value)


def _policy_module():
    source = ROOT / "src/k5vision/windows_identity_security.py"
    name = "k5_machine_admission_policy_probe"
    spec = importlib.util.spec_from_file_location(name, source)
    if spec is None or spec.loader is None:
        raise ValueError
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def observe(module, directory: Path) -> dict[str, object]:
    """Evaluate the real policy against one native descriptor snapshot."""
    native = module._native_security()
    owner, entries = native.descriptor(os.fspath(directory))
    current = native.current_user()
    used_installer = owner == module._TRUSTED_INSTALLER or any(
        entry.sid == module._TRUSTED_INSTALLER for entry in entries
    )
    verified_installer = used_installer and native.trusted_installer()
    kinds = {
        "S-1-5-18": "system",
        "S-1-5-32-544": "administrators",
        "S-1-5-32-545": "users",
        "S-1-5-11": "authenticated_users",
        "S-1-1-0": "everyone",
        "S-1-3-0": "creator_owner",
        "S-1-3-4": "owner_rights",
        module._TRUSTED_INSTALLER: "trusted_installer",
    }
    trusted = {current, module._ADMINISTRATORS, module._SYSTEM}
    if verified_installer:
        trusted.add(module._TRUSTED_INSTALLER)

    def kind(sid):
        return kinds.get(sid, "current_user" if sid == current else "other")

    result: dict[str, object] = {
        "schema_version": 1,
        "probe": "python_program_data_admission",
        "completed": True,
        "owner_kind": kind(owner),
        "trusted_installer_verified": bool(verified_installer),
        "allow_aces": [],
        "aces_capped": len(entries) > 64,
    }
    for entry in entries[:64]:
        if entry.kind != 0:
            continue
        mask = module._expanded_mask(entry.mask)
        principal = owner if entry.sid == module._OWNER_RIGHTS else entry.sid
        ignored = bool(entry.flags & 0x08)
        result["allow_aces"].append(
            {
                "principal_kind": kind(entry.sid),
                "flags": entry.flags,
                "expanded_mask": mask,
                "foreign_forbidden_mask": (
                    mask & module._ANCESTOR_MUTATION
                    if principal not in trusted and not ignored
                    else 0
                ),
            }
        )
    try:
        module._check_policy(
            owner,
            entries,
            current,
            private=False,
            require_file_inheritance=False,
            trusted_installer_verified=bool(verified_installer),
        )
        result["admitted"] = True
    except module.WindowsIdentitySecurityError:
        result["admitted"] = False
    return result


def main() -> int:
    result: dict[str, object] = {
        "schema_version": 1,
        "probe": "python_program_data_admission",
        "completed": False,
    }
    try:
        if os.name != "nt":
            raise ValueError
        result = observe(_policy_module(), _program_data())
    except Exception:
        result["failure"] = "native_observation_unavailable"
    print(json.dumps(result, sort_keys=True))
    return 0 if result["completed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
