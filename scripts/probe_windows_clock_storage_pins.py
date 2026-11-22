"""Exercise source-derived clock/storage directory opens on an owned empty directory.

This kernel-sharing probe never starts a product, changes the clock, constructs
an authorization controller, or substitutes for full path/admission-loop tests.
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
BASELINE = "23a24e57679534d77a5ea3881902f24a2d8e8e5f"
CLOCK = "windows/desktop/K5Vision.cs"
STORAGE = "src/k5vision/operator_storage_directory.py"
BASELINE_HASHES = {
    CLOCK: "0ad41d10efeb027fab6456050c32418a5ae689793b2ec17f66054add78dadfe8",
    STORAGE: "5f2606754a20be60bf1226b385fafd6cabde5d3a7c5ac0475a2ebcd5dd85257c",
}


def _digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _helper(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def extract_clock(source):
    """Select the actual call, FileIdentity and P/Invoke declarations verbatim."""
    block = _helper("probe_windows_legacy_profiles")._extract
    clock = block(source, "internal static class TimeAdministration")
    pins = block(clock, "private sealed class DirectoryPins : IDisposable")
    acquire = block(pins, "internal void Acquire(")
    calls = re.findall(
        r"SafeFileHandle handle = (CreateFile\(ancestor, ([01]), 3, IntPtr.Zero,\s*"
        r"3, 0x02000000u \| 0x00200000u, IntPtr.Zero\));",
        acquire,
    )
    if len(calls) != 1 or acquire.count("CreateFile(") != 1:
        raise ValueError("Clock directory pin call changed")
    identity_anchor = "[StructLayout(LayoutKind.Sequential)]\n        private struct FileIdentity"
    identity = block(clock, identity_anchor)
    if (
        clock.count("[StructLayout(LayoutKind.Sequential)]\n        private struct FileIdentity")
        != 1
    ):
        raise ValueError("Clock native identity layout changed")
    declarations = []
    for signature in (
        r"SafeFileHandle CreateFile\(string path, uint access, uint share,\s*"
        r"IntPtr security, uint creation, uint flags, IntPtr template\)",
        r"bool GetFileInformationByHandle\(SafeFileHandle file, out FileIdentity info\)",
    ):
        matches = re.findall(
            r'(\[DllImport\("kernel32.dll", [^\n]+\)\]\s*private static extern '
            + signature
            + r";)",
            clock,
        )
        if len(matches) != 1:
            raise ValueError("Clock native declaration changed")
        declarations.extend(matches)
    selected = identity + "\n" + "\n".join(declarations)
    return (
        calls[0][0],
        int(calls[0][1]),
        selected,
        {
            "source_sha256": _digest(source),
            "native_declarations_sha256": _digest(selected),
            "acquire_sha256": _digest(acquire),
            "call_sha256": _digest(calls[0][0]),
        },
    )


def extract_storage(source):
    """Compile only the actual ctypes declarations and directory-open expression."""
    tree = ast.parse(source)
    functions = [
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_pin_ancestors"
    ]
    if len(functions) != 1:
        raise ValueError("Storage pin function is ambiguous")
    function = functions[0]
    assignments = [n for n in function.body if isinstance(n, ast.Assign)]
    if len(assignments) != 5:
        raise ValueError("Storage native declarations changed")
    declarations = assignments[:4]
    calls = [
        n
        for n in ast.walk(function)
        if isinstance(n, ast.Call) and ast.unparse(n.func) == "kernel.CreateFileW"
    ]
    if len(calls) != 1:
        raise ValueError("Storage directory pin call is ambiguous")
    call = calls[0]
    if (
        len(call.args) != 7
        or call.keywords
        or ast.unparse(call.args[0]) != "str(directory)"
        or type(ast.literal_eval(call.args[1])) is not int
        or ast.literal_eval(call.args[1]) not in (0, 1)
        or [ast.unparse(n) for n in call.args[2:]]
        != ["3", "None", "3", "33554432 | 2097152", "None"]
    ):
        raise ValueError("Storage directory pin arguments changed")
    adapter = ast.FunctionDef(
        name="opened",
        args=ast.arguments(
            posonlyargs=[],
            args=[ast.arg(arg="directory")],
            kwonlyargs=[],
            kw_defaults=[],
            defaults=[],
        ),
        body=[*declarations, ast.Return(call)],
        decorator_list=[],
        type_params=[],
    )
    selected = ast.fix_missing_locations(ast.Module(body=[adapter], type_ignores=[]))
    return (
        selected,
        ast.literal_eval(call.args[1]),
        {
            "source_sha256": _digest(source),
            "native_declarations_sha256": _digest(
                "\n".join(ast.get_source_segment(source, n) for n in declarations)
            ),
            "pin_function_sha256": _digest(ast.get_source_segment(source, function)),
            "call_sha256": _digest(ast.get_source_segment(source, call)),
        },
    )


def materialize(old_clock, current_clock, old_storage, current_storage):
    old_call, old_access, old_decl, old_binding = extract_clock(old_clock)
    call, access, declarations, binding = extract_clock(current_clock)
    old_ast, old_storage_access, old_storage_binding = extract_storage(old_storage)
    current_ast, storage_access, storage_binding = extract_storage(current_storage)
    if (
        (old_access, access, old_storage_access, storage_access) != (0, 1, 0, 1)
        or old_decl != declarations
        or old_clock.replace(
            "CreateFile(ancestor, 0, 3, IntPtr.Zero,", "CreateFile(ancestor, 1, 3, IntPtr.Zero,"
        )
        != current_clock
        or old_storage.replace(
            "str(directory), 0, 3, None, 3, 0x02000000 | 0x00200000, None",
            "str(directory), 1, 3, None, 3, 0x02000000 | 0x00200000, None",
        )
        != current_storage
    ):
        raise ValueError("Product change exceeds two directory read-access bits")
    generated = (
        _CS.replace("@@DECLARATIONS@@", declarations)
        .replace("@@OLD@@", old_call)
        .replace("@@CURRENT@@", call)
    )
    return (
        generated,
        old_ast,
        current_ast,
        {
            "clock": binding,
            "old_clock": old_binding,
            "storage": storage_binding,
            "old_storage": old_storage_binding,
            "generated_csharp_sha256": _digest(generated),
        },
    )


_CS = r"""using System;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
internal static class Probe {
@@DECLARATIONS@@
private static SafeFileHandle Old(string ancestor) { return @@OLD@@; }
private static SafeFileHandle Current(string ancestor) { return @@CURRENT@@; }
private static string Key(SafeFileHandle handle) {
    FileIdentity info;
    if (handle.IsInvalid || !GetFileInformationByHandle(handle, out info) ||
        (info.Attributes & 0x400u) != 0 || (info.Attributes & 0x10u) == 0)
        throw new InvalidOperationException("Identity unavailable");
    return info.Volume.ToString() + ":" + info.IndexHigh.ToString() +
        ":" + info.IndexLow.ToString();
}
private static void DeleteOpen(string path, bool expected, int error) {
    using (SafeFileHandle handle = CreateFile(path, 0x10000u, 7, IntPtr.Zero,
            3, 0x02000000u | 0x00200000u, IntPtr.Zero)) {
        int observed = Marshal.GetLastWin32Error();
        if ((!handle.IsInvalid) != expected || (!expected && observed != error))
            throw new InvalidOperationException("Unexpected delete sharing result");
    }
}
private static int Main(string[] args) {
    try {
        if (args.Length != 1) return 2;
        string identity;
        using (SafeFileHandle handle = Old(args[0])) {
            identity = Key(handle);
            DeleteOpen(args[0], true, 0);
        }
        using (SafeFileHandle handle = Current(args[0])) {
            if (Key(handle) != identity) throw new InvalidOperationException("Identity changed");
            DeleteOpen(args[0], false, 32);
            if (Key(handle) != identity) throw new InvalidOperationException("Identity changed");
        }
        DeleteOpen(args[0], true, 0);
        using (SafeFileHandle handle = Old(args[0])) {
            if (Key(handle) != identity) throw new InvalidOperationException("Identity changed");
        }
        Console.WriteLine("{\"old_zero_allows_delete\":true,\"current_one_refuses_delete_error32\":true,\"after_close_allows_delete\":true,\"identity_unchanged\":true}");
        return 0;
    } catch { Console.WriteLine("{\"native_sharing_refused\":true}"); return 1; }
}
}"""


def _python_probe(old_ast, current_ast, directory, helpers):
    from ctypes import wintypes

    namespace = {"ctypes": ctypes, "wintypes": wintypes}
    exec(compile(old_ast, "<actual_old_storage_open>", "exec"), namespace)
    old_open = namespace["opened"]
    exec(compile(current_ast, "<actual_current_storage_open>", "exec"), namespace)
    current_open = namespace["opened"]
    kernel = helpers._kernel()
    adapter_type, _, _ = helpers.extract(helpers._source(ROOT / helpers.SOURCE))
    adapter = adapter_type()
    adapter.kernel = kernel
    handles = set()

    def close(handle):
        if not kernel.CloseHandle(handle):
            raise ValueError("Native cleanup failed")
        handles.remove(handle)

    def opened(function):
        handle = function(directory)
        if not handle or handle == ctypes.c_void_p(-1).value:
            raise ValueError("Source-derived native pin refused")
        handles.add(handle)
        return handle

    def key():
        handle = adapter._open_handle(directory, access=0x80, share=7, create=3, directory=True)
        handles.add(handle)
        try:
            return adapter._handle_key(handle, directory=True)
        finally:
            close(handle)

    def control(expected):
        handle = kernel.CreateFileW(str(directory), 0x10000, 7, None, 3, 0x02200000, None)
        error = ctypes.get_last_error()
        valid = bool(handle) and handle != ctypes.c_void_p(-1).value
        if valid:
            handles.add(handle)
            close(handle)
        if valid is not expected or not expected and error != 32:
            raise ValueError("Unexpected native delete sharing result")

    try:
        handle = opened(old_open)
        identity = key()
        control(True)
        close(handle)
        handle = opened(current_open)
        if key() != identity:
            raise ValueError("Native identity changed")
        control(False)
        if key() != identity:
            raise ValueError("Native identity changed")
        close(handle)
        control(True)
        handle = opened(old_open)
        if key() != identity:
            raise ValueError("Native identity changed")
        close(handle)
        return dict.fromkeys(
            (
                "old_zero_allows_delete",
                "current_one_refuses_delete_error32",
                "after_close_allows_delete",
                "identity_unchanged",
            ),
            True,
        )
    finally:
        for handle in list(handles):
            close(handle)


def main():
    proof = {"schema_version": 1, "status": "refused", "phase": "environment"}
    helpers = _helper("probe_windows_directory_pin")
    owned = []
    result = 1
    try:
        if (
            sys.platform != "win32"
            or sys.version_info[:3] != (3, 12, 10)
            or ctypes.sizeof(ctypes.c_void_p) != 8
            or os.environ.get("GITHUB_ACTIONS") != "true"
            or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted"
        ):
            raise ValueError("Disposable native workflow required")
        head = os.environ["K5_NATIVE_PROBE_HEAD_SHA"]
        if re.fullmatch("[a-f0-9]{40}", head) is None or helpers._head(ROOT) != head:
            raise ValueError("Source checkout mismatch")
        proof["head_sha"] = head
        proof["phase"] = "source_binding"
        baseline = ROOT / "native-clock-storage-baseline"
        if helpers._head(baseline) != BASELINE:
            raise ValueError("Historical source checkout mismatch")
        old = {name: helpers._source(baseline / name) for name in (CLOCK, STORAGE)}
        if any(_digest(old[name]) != digest for name, digest in BASELINE_HASHES.items()):
            raise ValueError("Historical source hash mismatch")
        generated, old_ast, current_ast, bindings = materialize(
            old[CLOCK], helpers._source(ROOT / CLOCK), old[STORAGE], helpers._source(ROOT / STORAGE)
        )
        proof.update(bindings)
        proof["probe_source_sha256"] = _digest(helpers._source(Path(__file__)))
        proof["extraction_helper_sha256"] = _digest(
            helpers._source(ROOT / "scripts/probe_windows_legacy_profiles.py")
        )
        _, _, identity_binding = helpers.extract(helpers._source(ROOT / helpers.SOURCE))
        proof["identity_adapter_binding"] = identity_binding
        proof["baseline_head_sha"] = BASELINE
        policy, policy_sha = helpers._policy()
        proof["private_policy_sha256"] = policy_sha
        for _ in range(2):
            directory = Path(
                tempfile.mkdtemp(prefix="k5-owned-pin-", dir=os.environ["RUNNER_TEMP"])
            )
            os.chmod(directory, 0o700)
            identity = (directory.stat().st_dev, directory.stat().st_ino)
            owned.append((directory, identity))
            policy.check_windows_volume(directory)
            policy.check_windows_security(directory, private=True, require_file_inheritance=True)
        directory, _ = owned[0]
        scratch, _ = owned[1]
        source, executable = scratch / "Probe.cs", scratch / "Probe.exe"
        source.write_text(generated, encoding="utf-8")
        compiler = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
        proof["phase"] = "compile"
        compiled = subprocess.run(
            [
                str(compiler),
                "/nologo",
                "/warn:4",
                "/warnaserror+",
                "/platform:x64",
                "/out:" + str(executable),
                str(source),
            ],
            capture_output=True,
            timeout=30,
        )
        if compiled.returncode != 0 or not executable.is_file():
            raise ValueError("Source-derived native compilation refused")
        proof["compile_exit_code"] = 0
        proof["phase"] = "native_clock_sharing"
        tested = subprocess.run([str(executable), str(directory)], capture_output=True, timeout=15)
        clock_facts = json.loads(tested.stdout.decode("utf-8"))
        keys = {
            "old_zero_allows_delete",
            "current_one_refuses_delete_error32",
            "after_close_allows_delete",
            "identity_unchanged",
        }
        if (
            tested.returncode != 0
            or set(clock_facts) != keys
            or any(v is not True for v in clock_facts.values())
        ):
            raise ValueError("Actual clock directory sharing refused")
        proof["clock_native_facts"] = clock_facts
        proof["phase"] = "native_storage_sharing"
        proof["storage_native_facts"] = _python_probe(old_ast, current_ast, directory, helpers)
        policy.check_windows_volume(directory)
        policy.check_windows_security(directory, private=True, require_file_inheritance=True)
        proof["private_custody_preserved"] = True
        proof["mocked_native_api"] = False
        proof["profile_service_or_clock_written"] = False
        proof["full_acquire_admission_issuer_auth_nonce_clock_and_storage_loops_exercised"] = False
        result = 0
    except Exception as error:
        proof["error_type"] = type(error).__name__
    finally:
        clean = True
        for directory, identity in reversed(owned):
            try:
                if (
                    directory.is_symlink()
                    or (directory.stat().st_dev, directory.stat().st_ino) != identity
                ):
                    raise ValueError("Owned cleanup identity changed")
                if directory.name.startswith("k5-owned-pin-"):
                    for child in directory.iterdir():
                        if (
                            child.name not in {"Probe.cs", "Probe.exe"}
                            or child.is_symlink()
                            or not child.is_file()
                        ):
                            raise ValueError("Unexpected owned probe child")
                        child.unlink()
                    directory.rmdir()
                else:
                    raise ValueError("Owned cleanup scope changed")
            except Exception:
                clean = False
        proof["cleanup_clean"] = clean
        proof["status"] = "passed" if result == 0 and clean else "refused"
        Path("native-clock-storage-pins.json").write_text(
            json.dumps(proof, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8"
        )
    return 0 if proof["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
