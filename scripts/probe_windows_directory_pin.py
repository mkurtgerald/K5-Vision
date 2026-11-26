"""Prove the actual directory-pin access participates in Win32 sharing checks.

The fixture never constructs a migration controller or opens an installation.
It extracts the real handle methods, structs and directory-pin arguments, then
uses them only on one new, empty, private temporary directory.
"""

from __future__ import annotations

import ast
import ctypes
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "src/k5vision/legacy_migration_copy.py"
BASELINE = "f96c828f4fdeb63596e2b6a27da9b29487387160"
BASELINE_SHA = "d83258baa584a460e2c8069c5e52db6fba77e6aac65143682bcae81ade5a3d5b"


def _digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _one(nodes, kind, name):
    values = [node for node in nodes if isinstance(node, kind) and node.name == name]
    if len(values) != 1:
        raise ValueError("Native source selection is ambiguous")
    return values[0]


def extract(source):
    """Load unchanged real methods; derive access from the actual pin call."""
    tree = ast.parse(source)
    stage = _one(tree.body, ast.ClassDef, "_WindowsStage")
    methods = [_one(stage.body, ast.FunctionDef, name) for name in ("_open_handle", "_handle_key")]
    pin = _one(stage.body, ast.FunctionDef, "pin")
    recheck = _one(stage.body, ast.FunctionDef, "recheck")
    structs = [_one(tree.body, ast.ClassDef, name) for name in ("_FileID", "_FileAttributes")]
    calls = [
        node
        for node in ast.walk(pin)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "self"
        and node.func.attr == "_open_handle"
    ]
    if len(calls) != 1:
        raise ValueError("Native directory pin call is ambiguous")
    call = calls[0]
    if len(call.args) != 1 or not isinstance(call.args[0], ast.Name) or call.args[0].id != "path":
        raise ValueError("Native directory pin path is unsupported")
    arguments = {item.arg: ast.literal_eval(item.value) for item in call.keywords}
    if (
        len(call.keywords) != 4
        or set(arguments) != {"access", "share", "create", "directory"}
        or any(type(arguments[name]) is not int for name in ("access", "share", "create"))
        or arguments["share"] != 3
        or arguments["create"] != 3
        or arguments["directory"] is not True
    ):
        raise ValueError("Native directory pin arguments changed")
    adapter = ast.ClassDef(
        name="Adapter", bases=[], keywords=[], body=methods, decorator_list=[], type_params=[]
    )
    selected = ast.fix_missing_locations(ast.Module(body=[*structs, adapter], type_ignores=[]))
    namespace = {"ctypes": ctypes}
    exec(compile(selected, "<actual_native_directory_handle_methods>", "exec"), namespace)
    return (
        namespace["Adapter"],
        arguments,
        {
            "source_sha256": _digest(source),
            "handle_methods_sha256": _digest(
                "\n".join(ast.get_source_segment(source, n) for n in methods)
            ),
            "structs_sha256": _digest(
                "\n".join(ast.get_source_segment(source, n) for n in structs)
            ),
            "pin_method_sha256": _digest(ast.get_source_segment(source, pin)),
            "recheck_method_sha256": _digest(ast.get_source_segment(source, recheck)),
        },
    )


def _head(root):
    return (
        subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, timeout=10, check=True
        )
        .stdout.decode("ascii")
        .strip()
    )


def _source(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError("Native source must be an ordinary checkout file")
    return path.read_text(encoding="utf-8")


def _kernel():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer, dword = ctypes.c_void_p, ctypes.c_uint32
    kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, dword, dword, pointer, dword, dword, pointer]
    kernel.CreateFileW.restype = pointer
    kernel.CloseHandle.argtypes, kernel.CloseHandle.restype = [pointer], ctypes.c_int32
    kernel.GetFileInformationByHandleEx.argtypes = [pointer, ctypes.c_int32, pointer, dword]
    kernel.GetFileInformationByHandleEx.restype = ctypes.c_int32
    return kernel


def _policy():
    path = ROOT / "src/k5vision/windows_identity_security.py"
    text = _source(path)
    spec = importlib.util.spec_from_file_location("directory_pin_private_policy", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module, _digest(text)


def main():
    proof = {"schema_version": 1, "status": "refused", "phase": "environment"}
    root = root_id = kernel = None
    handles = set()
    status = 1

    def opened(adapter, access, *, share=7):
        handle = adapter._open_handle(root, access=access, share=share, create=3, directory=True)
        handles.add(handle)
        return handle

    def close(handle):
        if not kernel.CloseHandle(handle):
            raise ValueError("Native handle cleanup failed")
        handles.remove(handle)

    def key(adapter):
        handle = opened(adapter, 0x80)
        try:
            return adapter._handle_key(handle, directory=True)
        finally:
            close(handle)

    try:
        if (
            sys.platform != "win32"
            or sys.version_info[:3] != (3, 12, 10)
            or ctypes.sizeof(ctypes.c_void_p) != 8
            or os.environ.get("GITHUB_ACTIONS") != "true"
            or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted"
        ):
            raise ValueError("Requires the disposable native Windows workflow")
        head = os.environ["K5_NATIVE_PROBE_HEAD_SHA"]
        if re.fullmatch("[a-f0-9]{40}", head) is None or _head(ROOT) != head:
            raise ValueError("Native checkout and required source head differ")
        proof["head_sha"] = head
        proof["phase"] = "source_binding"
        current_source = _source(ROOT / SOURCE)
        baseline_root = ROOT / "native-copy-baseline"
        old_source = _source(baseline_root / SOURCE)
        if _head(baseline_root) != BASELINE or _digest(old_source) != BASELINE_SHA:
            raise ValueError("Historical directory pin source changed")
        current_type, current_args, current_binding = extract(current_source)
        old_type, old_args, old_binding = extract(old_source)
        if (
            old_args["access"] != 0x20080
            or current_args["access"] != 0x20081
            or current_binding["handle_methods_sha256"] != old_binding["handle_methods_sha256"]
            or current_binding["structs_sha256"] != old_binding["structs_sha256"]
            or current_binding["recheck_method_sha256"] != old_binding["recheck_method_sha256"]
        ):
            raise ValueError("Native pin change exceeds the directory read bit")
        proof.update(current_binding)
        proof["baseline_source_sha256"] = old_binding["source_sha256"]
        proof["baseline_head_sha"] = BASELINE
        proof["old_access"] = old_args["access"]
        proof["current_access"] = current_args["access"]
        proof["revalidation_unchanged"] = True
        proof["pin_loop_constructor_service_and_full_copy_exercised"] = False
        proof["mocked_native_api"] = False
        proof["profile_or_service_written"] = False
        kernel = _kernel()
        current, old = current_type(), old_type()
        current.kernel = old.kernel = kernel
        policy, policy_sha = _policy()
        proof["private_policy_sha256"] = policy_sha
        proof["phase"] = "owned_directory"
        root = Path(tempfile.mkdtemp(prefix="k5-directory-pin-", dir=os.environ["RUNNER_TEMP"]))
        initial = root.lstat()
        root_id = initial.st_dev, initial.st_ino
        policy.check_windows_volume(root)
        policy.check_windows_security(root, private=True, require_file_inheritance=True)
        proof["owned_private_directory_admitted"] = True
        original = key(current)
        proof["phase"] = "historical_metadata_pin"
        handle = opened(old, old_args["access"], share=old_args["share"])
        try:
            probe = opened(old, 0x10000)
            close(probe)
            proof["old_metadata_pin_allows_delete_access"] = True
        finally:
            close(handle)
        proof["phase"] = "current_directory_pin"
        handle = opened(current, current_args["access"], share=current_args["share"])
        try:
            if current._handle_key(handle, directory=True) != original:
                raise ValueError("Pinned directory identity changed")
            try:
                probe = opened(current, 0x10000)
            except OSError as error:
                if error.errno != 32:
                    raise ValueError("Unexpected native delete refusal") from None
                proof["current_pin_delete_win32"] = 32
                proof["current_directory_pin_denies_delete_access"] = True
            else:
                close(probe)
                raise ValueError("Current directory pin did not enforce sharing")
            if key(current) != original:
                raise ValueError("Pinned directory identity changed")
        finally:
            close(handle)
        proof["phase"] = "post_close"
        probe = opened(current, 0x10000)
        close(probe)
        proof["after_close_delete_access_succeeds"] = True
        proof["directory_identity_unchanged"] = key(current) == original
        if not proof["directory_identity_unchanged"]:
            raise ValueError("Directory identity changed")
        proof["status"] = "observed"
        proof["phase"] = "complete"
        status = 0
    except Exception as error:
        proof["status"] = "refused"
        kind = type(error).__name__
        proof["failure_type"] = (
            kind if kind in {"ValueError", "OSError", "TimeoutExpired"} else "Other"
        )
    finally:
        cleaned = False
        try:
            for handle in tuple(handles):
                close(handle)
            if root is not None:
                current = root.lstat()
                if (
                    root_id != (current.st_dev, current.st_ino)
                    or getattr(current, "st_file_attributes", 0) & 0x400
                    or next(root.iterdir(), None) is not None
                ):
                    raise ValueError("Refusing cleanup of a changed directory")
                root.rmdir()
            cleaned = True
        except Exception:
            proof["status"] = "refused"
            status = 1
        proof["owned_cleanup"] = cleaned
    (ROOT / "native-directory-pin.json").write_text(
        json.dumps(proof, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8"
    )
    print(json.dumps({"status": proof["status"], "phase": proof["phase"]}))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
