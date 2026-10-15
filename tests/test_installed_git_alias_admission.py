"""Pure source contracts and independent failure models; NEVER compile/run native code.

These tests do not prove native ABI, actual sharing/ACL/namespace behavior, compiler
behavior, timer scheduling or a process-image/cross-step lease. Physical execution
of this new derivative and independent final post-admission remain necessary.
"""

from __future__ import annotations

import hashlib
import re
import struct
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path

import pytest


def canonical_checkout_bytes(raw):
    """Match the hosted source reader's sole allowed checkout transformation."""
    text = raw.decode("utf-8", errors="strict").replace("\r\n", "\n")
    if "\r" in text:
        raise ValueError("source_encoding")
    return text.encode("utf-8", errors="strict")


ROOT = Path(__file__).resolve().parents[1]
SOURCE_BYTES = canonical_checkout_bytes(
    (ROOT / "scripts/admit_installed_git_alias.cs").read_bytes()
)
SOURCE = SOURCE_BYTES.decode("utf-8")
WRAPPER = (ROOT / "scripts/assert-installed-git-alias.ps1").read_text(encoding="utf-8")
ROOT_ID, CMD_ID = 101, 202
SIZE = 43352
MAX_TOTAL = SIZE * 8
EXPECTED_HASH = "78211c7ed73988da93a6d8a33d47ec6187f464d7ea2a9a00c182bbd7a1ecf30f"
EXPECTED_ACL = "994780ca62efb849031b3dc79e20f98b3c344a9f82e66e5b9eb9fd2185b3596d"


def section(start, end, text=SOURCE):
    return text.split(start, 1)[1].split(end, 1)[0]


def component(name):
    if not 1 <= len(name.encode("utf-16-le", errors="surrogatepass")) // 2 <= 1024:
        raise ValueError("name_bound")
    if name in {".", ".."} or name.endswith((" ", ".")):
        raise ValueError("path_shape")
    if any(unicodedata.category(c) == "Cc" or c in '<>:"/\\|?*' for c in name):
        raise ValueError("path_shape")
    stem = name.split(".", 1)[0].rstrip(" ").upper()
    if stem in {"CON", "PRN", "AUX", "NUL", "CLOCK$", "CONIN$", "CONOUT$"}:
        raise ValueError("path_shape")
    if re.fullmatch(r"(?:COM|LPT)[0-9¹²³]", stem):
        raise ValueError("path_shape")
    try:
        name.encode("utf-16-le", errors="strict")
    except UnicodeError as error:
        raise ValueError("path_shape") from error


def wire(entries=((CMD_ID, "git.exe"), (CMD_ID, "git-lfs.exe")), *, tail_padding=False):
    data = bytearray(8)
    for index, (parent, name) in enumerate(entries):
        encoded = name.encode("utf-16-le", errors="surrogatepass")
        size = 20 + len(encoded)
        padded = (size + 7) & ~7
        last = index == len(entries) - 1
        entry = bytearray(padded if not last or tail_padding else size)
        struct.pack_into("<I", entry, 0, 0 if last else padded)
        struct.pack_into("<QI", entry, 8, parent, len(encoded) // 2)
        entry[20 : 20 + len(encoded)] = encoded
        data.extend(entry)
    struct.pack_into("<II", data, 0, len(data), len(entries))
    return data


def parse(data, *, links=2, status=0, io_status=0, completed=None):
    used = len(data) if completed is None else completed
    if status != 0 or io_status != 0 or not 0 < used <= 65536 or used > len(data):
        raise ValueError("enumeration")
    data = data[:used]
    if len(data) < 30:
        raise ValueError("enumeration")
    needed, count = struct.unpack_from("<II", data)
    if needed != len(data):
        raise ValueError("enumeration")
    if count != 2 or count != links:
        raise ValueError("alias_count")
    position, names, result = 8, set(), []
    for index in range(count):
        if position % 8 or position > len(data) - 20:
            raise ValueError("enumeration")
        next_offset = struct.unpack_from("<I", data, position)[0]
        parent, units = struct.unpack_from("<QI", data, position + 8)
        if parent == 0:
            raise ValueError("identity_mismatch")
        if not 1 <= units <= 1024:
            raise ValueError("name_bound")
        end = position + 20 + units * 2
        if end > len(data):
            raise ValueError("enumeration")
        try:
            name = bytes(data[position + 20 : end]).decode("utf-16-le", errors="strict")
        except UnicodeError as error:
            raise ValueError("path_shape") from error
        component(name)
        if parent != CMD_ID:
            raise ValueError("alias_outside_root")
        if name not in ("git.exe", "git-lfs.exe"):
            raise ValueError("exact_pair")
        relative = "cmd/" + name
        if relative.upper() in names:
            raise ValueError("alias_duplicate")
        names.add(relative.upper())
        result.append((parent, relative))
        if index + 1 < count:
            minimum = (20 + units * 2 + 7) & ~7
            if next_offset < minimum or next_offset % 8 or next_offset > len(data) - position - 20:
                raise ValueError("enumeration")
            position += next_offset
        elif next_offset != 0 or not end <= len(data) <= (end + 7) & ~7:
            raise ValueError("enumeration")
    if "CMD/GIT.EXE" not in names:
        raise ValueError("alias_missing_primary")
    result.sort(key=lambda item: item[1])
    if [item[1] for item in result] != ["cmd/git-lfs.exe", "cmd/git.exe"]:
        raise ValueError("exact_pair")
    return result


def changed_u32(data, offset, value):
    result = bytearray(data)
    struct.pack_into("<I", result, offset, value)
    return result


def test_exact_literal_policy_and_derivation():
    for token in (
        "eeb76812034a45604fbe43171cecbffcc413af8c31fca611e0f2a75bf202059a",
        "run 37784286812",
        "private const int MaxLinks = 2;",
        "private const int MaxHandles = 7;",
        "private const long MaxFileBytes = 43352;",
        "private const uint ExpectedVolume = 0xd0d710f0;",
        "private const ulong ExpectedId = 0x00de000000069fdd;",
        EXPECTED_HASH,
        EXPECTED_ACL,
        "private const long MaxTotalBytes = MaxFileBytes * 8;",
        "private const long MaxElapsedMs = 8000;",
    ):
        assert token in SOURCE
    assert "Environment.GetEnvironmentVariable" not in SOURCE
    assert "Dictionary<string, object>" not in SOURCE
    assert "public static Session Begin()" in SOURCE
    assert "public string Complete()" in SOURCE
    assert "Session(Observation value)" in SOURCE
    assert "public Session(" not in SOURCE
    assert "OS/device-map anchor" in SOURCE
    assert "not a perfect process-image or cross-step lease" in SOURCE


def test_single_component_relative_open_and_minimum_read_sharing():
    anchor = section("private Held OpenAnchor()", "private Held OpenRelative")
    relative = section("private Held OpenRelative", "// Every query")
    assert SOURCE.count("Native.CreateFileW(") == 1
    assert '@"\\\\?\\C:\\"' in anchor
    assert "IntPtr.Zero, 3," in anchor
    for token in (
        "ShareRead = 0x00000001",
        "ObjDontReparse = 0x00001000",
        "attributes.RootDirectory = parent.Handle;",
        "attributes.Attributes = ObjCaseInsensitive | ObjDontReparse;",
        "ReadAttributes | ReadControl | Synchronize | (directory ? Traverse : ReadData)",
        "FileOpenReparsePoint | FileSynchronousIoNonalert",
        "status == 0 && io.Status == 0",
        "Own(handle);",
    ):
        assert token in SOURCE
    assert relative.index("ValidateComponent(component)") < relative.index("Native.NtOpenFile(")
    for token in (
        'OpenRelative(anchor, "Program Files", true)',
        'OpenRelative(programFiles, "Git", true)',
        'OpenRelative(git, "cmd", true)',
        'OpenRelative(cmd, "git.exe", false)',
    ):
        assert token in SOURCE


def test_unchanged_native_handle_only_identity_acl_and_abi_contract():
    for token in (
        "IntPtr.Size == 8 && BitConverter.IsLittleEndian",
        'Offset(typeof(LinkEntryLayout), "ParentFileId") == 8',
        'Offset(typeof(LinkEntryLayout), "NameUnits") == 16',
        'Offset(typeof(LinkEntryLayout), "FirstNameUnit") == 20',
        'Offset(typeof(LinksLayout), "Entry") == 8',
        "FileHardLinkInformation = 46",
        "LinkBufferBytes = 65536",
        "status == 0 && io.Status == 0 && used > 0",
        "needed == (uint)bytes.Length",
        "int nameBytes = checked((int)nameUnits * 2)",
        "new UnicodeEncoding(false, false, true)",
        'String.Equals(filesystem.ToString(), "NTFS", StringComparison.Ordinal)',
        "U64(Query(handle, 6, 8, true), 0)",
        "identifier != 0 && identifier == byHandleId",
        "serial == info.VolumeSerial",
        "Query(primary.Handle, FileHardLinkInformation",
        'Require(parentId == cmd.Initial.Id, "alias_outside_root")',
        'String.Equals(name, "git.exe", StringComparison.Ordinal)',
        'String.Equals(name, "git-lfs.exe", StringComparison.Ordinal)',
        "Native.GetSecurityInfo(handle, 1, 0x00000005",
        "raw.Owner, null, null, raw.DiscretionaryAcl",
        "new RawSecurityDescriptor(retained",
        "stable.GetBinaryForm(normalized, 0)",
        "Native.LocalFree(descriptor)",
        'Require(dacl != IntPtr.Zero, "acl_null")',
        "item.Initial.Same(ReadMetadata(item.Handle, item.Directory))",
        "String.Equals(item.Acl, ReadAcl(item.Handle), StringComparison.Ordinal)",
        "alias.Initial.Same(primary.Initial)",
        "(info.Attributes & AttributeReparse) == 0",
    ):
        assert token in SOURCE
    closure = section("private List<Link> Closure", "private string Digest")
    assert "count == MaxLinks && count == primary.Initial.Links" in closure
    assert "parentId == git.Initial.Id" not in closure
    assert 'result[0].Relative == "cmd/git-lfs.exe"' in closure
    assert 'result[1].Relative == "cmd/git.exe"' in closure


def test_fixed_system_dlls_only_no_execution_search_mutation_network():
    exports = set(re.findall(r"internal static extern \w+ (\w+)\(", SOURCE))
    assert exports == {
        "NtOpenFile",
        "NtQueryInformationFile",
        "CreateFileW",
        "GetFileInformationByHandle",
        "GetVolumeInformationByHandleW",
        "GetFileType",
        "ReadFile",
        "SetFilePointerEx",
        "CloseHandle",
        "LocalFree",
        "GetSecurityInfo",
        "IsValidSecurityDescriptor",
        "GetSecurityDescriptorControl",
        "GetSecurityDescriptorLength",
    }
    assert SOURCE.count("DefaultDllImportSearchPaths(DllImportSearchPath.System32)") == len(exports)
    assert set(re.findall(r'\[DllImport\("([^"]+)"', SOURCE)) == {
        "ntdll.dll",
        "kernel32.dll",
        "advapi32.dll",
    }
    for token in (
        "Process.Start",
        "Directory.Get",
        "Directory.Enumerate",
        "OpenFileById",
        "FindFirstFile",
        "CreateHardLink",
        "WriteFile(",
        "SetSecurityInfo",
        "AdjustTokenPrivileges",
        "File.Write",
        "WebClient",
        "HttpClient",
        "LoadLibrary",
        "GetProcAddress",
        "Process.GetProcesses",
        "Kill(",
    ):
        assert token not in SOURCE


def test_fresh_session_full_revalidation_and_single_use_cleanup():
    acquire = section("internal void Acquire()", "internal void Revalidate()")
    revalidate = section("internal void Revalidate()", "internal bool CloseAll()")
    opened = section("internal static Session Open()", "public string Complete()")
    complete = section("public string Complete()", "private sealed class Refusal")
    assert opened.index("pending.Acquire()") < opened.index("return result;")
    assert "if (failure != null) pending.CloseAll()" in opened
    assert "Interlocked.Exchange(ref observation, null)" in complete
    assert 'if (current == null) return "session_closed"' in complete
    assert complete.index("current.Revalidate()") < complete.index("current.CloseAll()")
    assert 'failure == "none"' in complete
    for body in (acquire, revalidate):
        assert body.count("Closure(primary, git, cmd)") == 2
        assert body.count("AssertDigest(primary)") == 2
        assert "AssertDigest(alias)" in body
        assert "AssertHeldUnchanged()" in body
    assert "watch.Stop()" in acquire and "watch.Restart()" in revalidate
    assert "delegate(object state) { Environment.Exit(124); }, null, 20000" in SOURCE
    assert "for (int i = owned.Count - 1; i >= 0; i--)" in SOURCE
    assert "owned.Clear()" in SOURCE


def test_exact_before_read_budget_and_noncontent_refusals_precede_hash():
    digest = section("private string Digest(Held file)", "private void AssertPinned")
    assert digest.index("BytesRead <= MaxTotalBytes - request") < digest.index("Native.ReadFile(")
    pinned = section("private void AssertDigest", "private void AssertUnchangedClosure")
    assert pinned.index("AssertPinned(file)") < pinned.index("Digest(file)")
    assert "primary.Initial.Size <= MaxTotalBytes / (2L * (primary.Initial.Links + 2L))" in SOURCE
    assert "BytesRead == MaxFileBytes * 4" in SOURCE and "BytesRead == MaxTotalBytes" in SOURCE
    assert 'Require(info.Links == MaxLinks, "alias_count")' in SOURCE
    assert 'Require(size == (ulong)MaxFileBytes, "file_size")' in SOURCE


def test_wrapper_pin_byte_clone_strict_decode_and_no_git_source_bootstrap():
    assert "$sourceSha256 = '" + hashlib.sha256(SOURCE_BYTES).hexdigest() + "'" in WRAPPER
    assert "$SourceBytes.Length -ne " + str(len(SOURCE_BYTES)) in WRAPPER
    assert WRAPPER.index("$SourceBytes.Clone()") < WRAPPER.index("ComputeHash($privateBytes)")
    assert WRAPPER.index("ComputeHash($privateBytes)") < WRAPPER.index("GetString($privateBytes)")
    assert "[Text.UTF8Encoding]::new($false, $true)" in WRAPPER
    assert "Add-Type -TypeDefinition $sourceText" in WRAPPER
    assert "-ReferencedAssemblies $systemCorePath" in WRAPPER
    for token in (
        "Get-Content",
        "git show",
        "git.exe show",
        "-Path $source",
        "-OutputAssembly",
        "Start-Process",
        "Invoke-Expression",
    ):
        assert token not in WRAPPER
    assert "PSEdition -cne 'Desktop'" in WRAPPER
    assert "PSVersion.Major -ne 5" in WRAPPER and "PSVersion.Minor -ne 1" in WRAPPER
    assert r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319" in WRAPPER
    assert "'csc.exe', 'System.Core.dll'" in WRAPPER
    assert "('K5ExactGitAlias' -as [type])" in WRAPPER


def test_wrapper_buffers_success_until_reproof_cleanup_boundary_and_preserves_primary():
    invoker = WRAPPER.split("$invoker = {", 1)[1]
    assert invoker.index("& $AssertBoundary") < invoker.index("$aliasType::Begin()")
    assert invoker.index("$aliasType::Begin()") < invoker.index("& $Operation")
    assert invoker.index("& $Operation") < invoker.index("$session.Complete()")
    assert invoker.index("$session.Complete()") < invoker.rindex("& $AssertBoundary")
    assert invoker.rindex("& $AssertBoundary") < invoker.index("Write-Output -NoEnumerate")
    assert invoker.index("if ($null -ne $failure)") < invoker.index("Write-Output -NoEnumerate")
    assert "$failureError = $_ # Keep first underlying error private" in invoker
    assert "$failure = $phase" in invoker
    assert invoker.count("if ($null -eq $failure)") >= 2
    assert "2>$null 3>$null 4>$null 5>$null 6>$null" in invoker
    assert r"C:\Program Files\Git\cmd\git.exe" in invoker
    assert ".GetNewClosure()" in invoker
    for token in (
        "Write-Host",
        "Write-Error",
        "Write-Warning",
        "Write-Information",
        "Exception.Message",
        "ScriptStackTrace",
        "Format-List",
    ):
        assert token not in WRAPPER
    assert "'fixed_git_alias_' + $failure" in invoker


def test_wrapper_retains_artifacts_and_requires_independent_post_after_process_exit():
    assert "after this PowerShell process exits" in WRAPPER
    assert "independently pinned same-host guard/compiler-idle/file-only artifact" in WRAPPER
    assert "Final artifact admission MUST run later" in WRAPPER
    assert "compiler_temp_occupied" in WRAPPER
    assert "SetEnvironmentVariable('TEMP', $previousTemp, 'Process')" in WRAPPER
    assert "SetEnvironmentVariable('TMP', $previousTmp, 'Process')" in WRAPPER
    assert "$null -ne $restoreError -and $null -eq $compileError" in WRAPPER
    for token in (
        "Remove-Item",
        "Directory.Delete",
        "File.Delete",
        "-Recurse",
        "Set-Acl",
        "SetAccessControl",
        "Stop-Process",
    ):
        assert token not in WRAPPER


@pytest.mark.parametrize("padding", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
def test_wire_model_exact_pair_only_success(padding, reverse):
    entries = [(CMD_ID, "git.exe"), (CMD_ID, "git-lfs.exe")]
    if reverse:
        entries.reverse()
    assert parse(wire(entries, tail_padding=padding)) == [
        (CMD_ID, "cmd/git-lfs.exe"),
        (CMD_ID, "cmd/git.exe"),
    ]


@pytest.mark.parametrize("status, io_status", [(1, 0), (0, 1), (0x80000005, 0), (-1, -1)])
def test_wire_model_exact_native_success_only(status, io_status):
    with pytest.raises(ValueError, match="enumeration"):
        parse(wire(), status=status, io_status=io_status)


@pytest.mark.parametrize("completed", [0, 7, 29, 33, 65537])
def test_wire_model_completed_span_bound(completed):
    with pytest.raises(ValueError, match="enumeration"):
        parse(wire(), completed=completed)


@pytest.mark.parametrize(
    "offset, value, code",
    [
        (0, 65537, "enumeration"),
        (0, 8, "enumeration"),
        (4, 0, "alias_count"),
        (4, 1, "alias_count"),
        (4, 3, "alias_count"),
        (4, 17, "alias_count"),
        (24, 0, "name_bound"),
        (24, 1025, "name_bound"),
        (24, 0xFFFFFFFF, "name_bound"),
        (24, 100, "enumeration"),
    ],
)
def test_wire_model_malformed_fields(offset, value, code):
    with pytest.raises(ValueError, match=code):
        parse(changed_u32(wire(), offset, value))


@pytest.mark.parametrize("next_offset", [0, 8, 20, 31, 33, 0xFFFFFFF8])
def test_wire_model_early_terminator_overlap_alignment_overflow(next_offset):
    with pytest.raises(ValueError, match="enumeration"):
        parse(changed_u32(wire(), 8, next_offset))


@pytest.mark.parametrize(
    "name",
    [
        "",
        ".",
        "..",
        "git.exe.",
        "git.exe ",
        "git.exe:stream",
        "x/y",
        "x\\y",
        "CON",
        "con.exe",
        "CON .exe",
        "PRN.log",
        "AUX",
        "NUL",
        "CLOCK$",
        "CONIN$",
        "CONOUT$",
        "COM0",
        "COM1.exe",
        "COM9",
        "LPT0",
        "LPT9.log",
        "COM¹.exe",
        "LPT²",
        "COM³",
        "x\x00y",
        "x\x1fy",
        "x\x7fy",
        "x\x85y",
        "x\ud800y",
        "x\udc00y",
    ],
)
def test_wire_model_component_refusals(name):
    with pytest.raises(ValueError, match="name_bound|path_shape|enumeration"):
        parse(wire(((CMD_ID, name), (CMD_ID, "git-lfs.exe"))))


@pytest.mark.parametrize(
    "parent, name, code",
    [
        (0, "git.exe", "identity_mismatch"),
        (999, "git.exe", "alias_outside_root"),
        (ROOT_ID, "git.exe", "alias_outside_root"),
        (CMD_ID, "GIT.EXE", "exact_pair"),
        (CMD_ID, "copy.exe", "exact_pair"),
        (CMD_ID, "git-lfs.exe", "alias_duplicate"),
        (CMD_ID, "gıt.exe", "exact_pair"),
        (CMD_ID, "git-😀.exe", "exact_pair"),
    ],
)
def test_wire_model_outside_case_wrong_alias_duplicate(parent, name, code):
    with pytest.raises(ValueError, match=code):
        parse(wire(((parent, name), (CMD_ID, "git-lfs.exe"))))


@pytest.mark.parametrize("links", [0, 1, 3, 16, 0xFFFFFFFF])
def test_wire_model_metadata_link_count_mismatch(links):
    with pytest.raises(ValueError, match="alias_count"):
        parse(wire(), links=links)


def test_wire_model_missing_extra_alias_and_trailing_native_data():
    for entries in (
        [(CMD_ID, "git.exe")],
        [(CMD_ID, "git.exe"), (CMD_ID, "git-lfs.exe"), (CMD_ID, "extra.exe")],
    ):
        with pytest.raises(ValueError, match="alias_count"):
            parse(wire(entries), links=len(entries))
    data = wire()
    data += b"\0" * 8
    struct.pack_into("<I", data, 0, len(data))
    with pytest.raises(ValueError, match="enumeration"):
        parse(data)
    last = 8 + struct.unpack_from("<I", wire(), 8)[0]
    with pytest.raises(ValueError, match="enumeration"):
        parse(changed_u32(wire(), last, 24))


@dataclass(frozen=True)
class Proof:
    volume: int = 0xD0D710F0
    file_id: int = 0x00DE000000069FDD
    links: int = 2
    size: int = SIZE
    digest: str = EXPECTED_HASH
    acl: str = EXPECTED_ACL
    filesystem: str = "NTFS"
    reparse: bool = False
    ancestors_ordinary: bool = True
    metadata_changed: bool = False
    readable_acl: bool = True
    cleanup: bool = True
    proof_elapsed_ms: int = 57


def admit_model(proof):
    if (
        proof != Proof(proof_elapsed_ms=proof.proof_elapsed_ms)
        or not 0 <= proof.proof_elapsed_ms <= 8000
    ):
        raise ValueError("refused")


@pytest.mark.parametrize(
    "field, bad",
    [
        ("volume", 0),
        ("volume", 0xD0D710F1),
        ("file_id", 0),
        ("file_id", 0x00DE000000069FDE),
        ("links", 0),
        ("links", 1),
        ("links", 3),
        ("size", 0),
        ("size", SIZE - 1),
        ("size", SIZE + 1),
        ("digest", "0" * 64),
        ("digest", EXPECTED_HASH.upper()),
        ("acl", "0" * 64),
        ("filesystem", "ReFS"),
        ("filesystem", "ntfs"),
        ("reparse", True),
        ("ancestors_ordinary", False),
        ("metadata_changed", True),
        ("readable_acl", False),
        ("cleanup", False),
        ("proof_elapsed_ms", 8001),
        ("proof_elapsed_ms", -1),
    ],
)
def test_exact_proof_model_rejects_every_field_drift(field, bad):
    with pytest.raises(ValueError, match="refused"):
        admit_model(replace(Proof(), **{field: bad}))


@pytest.mark.parametrize("value", [0, 57, 8000])
def test_exact_proof_model_elapsed_bound(value):
    admit_model(Proof(proof_elapsed_ms=value))


def transaction_model(failures=frozenset()):
    events, opened, failure = [], False, None

    def step(name):
        events.append(name)
        if name in failures:
            raise ValueError(name)

    try:
        step("pre_boundary")
        step("compiler_idle_before")
        step("acquire")
        opened = True
        step("operation")
        step("buffer_output")
    except ValueError as error:
        failure = str(error)
    finally:
        if opened:
            for name in ("revalidate", "close"):
                try:
                    step(name)
                except ValueError as error:
                    failure = failure or str(error)
        for name in ("post_boundary", "compiler_idle_after"):
            try:
                step(name)
            except ValueError as error:
                failure = failure or str(error)
    if failure is None:
        step("publish_output")
    return events, failure


@pytest.mark.parametrize(
    "failure",
    [
        "pre_boundary",
        "compiler_idle_before",
        "acquire",
        "operation",
        "buffer_output",
        "revalidate",
        "close",
        "post_boundary",
        "compiler_idle_after",
    ],
)
def test_session_model_never_publishes_partial_failure(failure):
    events, actual = transaction_model({failure})
    assert actual == failure
    assert "publish_output" not in events
    if "operation" in events:
        assert "revalidate" in events and "close" in events
    assert "post_boundary" in events and "compiler_idle_after" in events


def test_session_model_keeps_primary_failures_and_new_proof_every_call():
    events, error = transaction_model({"operation", "revalidate", "close", "post_boundary"})
    assert error == "operation"
    assert events.index("revalidate") < events.index("close") < events.index("post_boundary")
    for _ in range(3):
        events, error = transaction_model()
        assert error is None
        assert events == [
            "pre_boundary",
            "compiler_idle_before",
            "acquire",
            "operation",
            "buffer_output",
            "revalidate",
            "close",
            "post_boundary",
            "compiler_idle_after",
            "publish_output",
        ]


def test_model_read_caps_checked_before_transfer_and_exact_total():
    calls = []

    def bounded_read(total, request):
        if request <= 0 or total > MAX_TOTAL - request:
            raise ValueError("bytes_bound")
        calls.append(request)
        return total + request

    assert bounded_read(MAX_TOTAL - SIZE, SIZE) == MAX_TOTAL
    with pytest.raises(ValueError, match="bytes_bound"):
        bounded_read(MAX_TOTAL - SIZE + 1, SIZE)
    with pytest.raises(ValueError, match="bytes_bound"):
        bounded_read(0, 0)
    assert calls == [SIZE]
    assert SIZE == MAX_TOTAL // (2 * (2 + 2))


def test_compiler_temp_is_derived_from_validated_run_attempt_action():
    assert "$CompilerTemp -cne $expectedTemp" in WRAPPER
    assert "'k5-installed-git-alias-'" in WRAPPER
    assert (
        "$env:GITHUB_RUN_ID + '-' + $env:GITHUB_RUN_ATTEMPT + '-' + $env:GITHUB_ACTION" in WRAPPER
    )
    assert "$temporary.Name -cne '_temp'" in WRAPPER
    assert "$temporary.Parent.FullName -cne $workspace.Parent.FullName" in WRAPPER
    assert WRAPPER.index("$CompilerTemp -cne $expectedTemp") < WRAPPER.index(
        "EnumerateFileSystemEntries($CompilerTemp)"
    )


def test_primary_acl_failure_survives_descriptor_cleanup_failure():
    acl = section("private string ReadAcl", "private List<Link> Closure")
    assert "catch (Exception) { failed = true; throw; }" in acl
    assert 'if (!released && !failed) throw new Refusal("cleanup_failed")' in acl
    opened = section("internal static Session Open()", "public string Complete()")
    assert opened.index("Session result = new Session(pending)") < opened.index("pending.Acquire()")


@pytest.mark.parametrize("ancestor", range(4))
@pytest.mark.parametrize(
    "defect", ["reparse", "volume", "id", "acl", "changed_time", "type", "share_conflict"]
)
def test_handle_chain_model_rejects_each_ancestor_drift(ancestor, defect):
    # Four held ancestors followed by the primary and two reopened aliases.
    before = [
        dict(
            reparse=False,
            volume=0xD0D710F0,
            id=i + 1,
            acl="owner+dacl",
            changed_time=1,
            type="directory",
            share_conflict=False,
        )
        for i in range(4)
    ]
    after = [dict(item) for item in before]
    after[ancestor][defect] = {
        "reparse": True,
        "volume": 1,
        "id": 9,
        "acl": "other",
        "changed_time": 2,
        "type": "file",
        "share_conflict": True,
    }[defect]
    with pytest.raises(ValueError, match="changed"):
        for expected, actual in zip(before, after, strict=False):
            if expected != actual:
                raise ValueError("changed")


@pytest.mark.parametrize("alias_index", [0, 1])
@pytest.mark.parametrize(
    "field,bad",
    [
        ("volume", 1),
        ("file_id", 1),
        ("links", 1),
        ("size", SIZE + 1),
        ("digest", "0" * 64),
        ("acl", "0" * 64),
        ("reparse", True),
        ("filesystem", "ReFS"),
        ("metadata_changed", True),
        ("readable_acl", False),
    ],
)
def test_exact_pair_model_rejects_either_alias_drift(alias_index, field, bad):
    aliases = [Proof(), Proof()]
    aliases[alias_index] = replace(aliases[alias_index], **{field: bad})
    with pytest.raises(ValueError, match="refused"):
        for alias in aliases:
            admit_model(alias)


@pytest.mark.parametrize("failed_handle", range(7))
@pytest.mark.parametrize("throws", [False, True])
def test_cleanup_model_attempts_every_owned_handle_in_reverse_on_failure(failed_handle, throws):
    attempted, successful = [], True
    for handle in range(6, -1, -1):
        attempted.append(handle)
        try:
            if handle == failed_handle:
                if throws:
                    raise ValueError("close")
                successful = False
        except ValueError:
            successful = False
    assert attempted == [6, 5, 4, 3, 2, 1, 0]
    assert not successful
    close = section("internal bool CloseAll()", "private static void ValidateComponent")
    assert "catch (Exception) { success = false; }" in close
    assert close.index("Native.CloseHandle") < close.index("owned.Clear()")


def test_session_model_is_single_use_even_after_failed_revalidation():
    pending = object()

    def complete(failed=False):
        nonlocal pending
        current, pending = pending, None
        if current is None:
            return "session_closed"
        return "changed" if failed else "none"

    assert complete(failed=True) == "changed"
    assert complete() == "session_closed"


@pytest.mark.parametrize(
    "receipt", [None, True, {"status": "observed"}, {"storage_admission": True}]
)
def test_receipt_model_never_substitutes_for_current_physical_proof(receipt):
    # Receipt is deliberately not an argument to the admission function.
    with pytest.raises(ValueError, match="refused"):
        admit_model(replace(Proof(), digest="0" * 64))
    assert "Environment.GetEnvironmentVariable" not in SOURCE
    assert "public static Session Begin()" in SOURCE
    assert "$session = $aliasType::Begin()" in WRAPPER


def test_wrapper_preserves_first_error_records_and_first_restore_inner_privately():
    initializer, invoker = WRAPPER.split("$invoker = {", 1)
    for body in (initializer, invoker):
        assert "$failureError = $null" in body
        assert "$failureError = $_" in body
        assert body.count("if ($null -eq $failureError) { $failureError = $_ }") >= 2
    assert "RuntimeException]::new('compiler_environment', $restoreError.Exception)" in initializer
    assert initializer.index(
        "$null -ne $restoreError -and $null -eq $compileError"
    ) < initializer.index("RuntimeException]::new('compiler_environment'")
    assert "if ($null -eq $restoreError) { $restoreError = $_ }" in initializer
    assert "$failure = $phase" in initializer and "$failure = $phase" in invoker
    assert "throw ('fixed_git_alias_' + $failure + ':' + $nativeCode)" in invoker
    assert "Write-Output $failureError" not in WRAPPER
    assert "throw $failureError" not in WRAPPER
    assert "throw $restoreError" not in WRAPPER


def test_native_refusal_projection_is_closed_exact_csharp_allowlist_and_typed():
    projection = section("function Get-K5AliasNativeRefusal", "$failure = $null", WRAPPER)
    csharp_codes = set(re.findall(r'"([a-z_]+)"', section("HashSet<string> Codes", "// No path,")))
    powershell_codes = set(re.findall(r"'([a-z_]+)'", section("$codes = @(", ")\n", projection)))
    assert powershell_codes == csharp_codes
    assert "$Value -is [Management.Automation.ErrorRecord]" in projection
    assert (
        "$exception.GetType() -eq [Management.Automation.MethodInvocationException]" in projection
    )
    assert "$exception.GetType() -eq [InvalidOperationException]" in projection
    assert "$null -eq $exception.InnerException" in projection
    assert "$candidate -cin $codes" in projection
    assert "return 'unknown'" in projection
    assert "while (" not in projection
    assert WRAPPER.count("$exception.Message") == 1
    invoker = WRAPPER.split("$invoker = {", 1)[1]
    assert "if ($phase -ceq 'proof_begin')" in invoker
    assert "$nativeCode = & $nativeProjection $completion" in invoker
    assert "$nativeCode = & $nativeProjection $failureError" in invoker


NATIVE_CODES = set(re.findall(r'"([a-z_]+)"', section("HashSet<string> Codes", "// No path,")))


def project_native_model(kind, text, *, wrapper="method", inner=False):
    if kind == "complete":
        candidate = text
    elif kind == "InvalidOperationException" and wrapper in ("method", "direct") and not inner:
        candidate = text
    else:
        candidate = None
    return candidate if isinstance(candidate, str) and candidate in NATIVE_CODES else "unknown"


@pytest.mark.parametrize("code", sorted(NATIVE_CODES))
@pytest.mark.parametrize("kind", ["complete", "InvalidOperationException"])
def test_native_refusal_model_preserves_each_fixed_category(code, kind):
    assert project_native_model(kind, code) == code


@pytest.mark.parametrize(
    "kind,text,wrapper,inner",
    [
        ("Exception", "identity_mismatch", "method", False),
        ("RuntimeException", "hash_mismatch", "method", False),
        ("InvalidOperationException", "identity_mismatch", "unknown", False),
        ("InvalidOperationException", "identity_mismatch", "method", True),
        ("InvalidOperationException", "IDENTITY_MISMATCH", "method", False),
        ("InvalidOperationException", "identity_mismatch\nsecret", "method", False),
        ("InvalidOperationException", r"C:\secret\token.txt", "method", False),
        ("complete", "Unexpected secret", "method", False),
        ("complete", None, "method", False),
        ("complete", 1, "method", False),
    ],
)
def test_native_refusal_model_drops_every_untyped_or_unlisted_detail(kind, text, wrapper, inner):
    assert project_native_model(kind, text, wrapper=wrapper, inner=inner) == "unknown"


def test_first_private_error_model_survives_secondary_restore_cleanup_and_boundary_errors():
    original, restoration, cleanup, boundary = (object() for _ in range(4))
    first_error, phase = original, "operation"
    for later in (restoration, cleanup, boundary):
        if first_error is None:
            first_error = later
    assert first_error is original and phase == "operation"
    restore_error = None
    for later in (restoration, cleanup):
        if restore_error is None:
            restore_error = later
    assert restore_error is restoration
    public = "fixed_git_alias_" + phase + ":unknown"
    assert public == "fixed_git_alias_operation:unknown"


def test_initial_directory_enumerator_cleanup_does_not_replace_first_failure():
    assert "catch { $entryError = $_; throw }" in WRAPPER
    assert "try { $entries.Dispose() } catch { if ($null -eq $entryError) { throw } }" in WRAPPER


# Hosted qualification contracts below are source/model checks only. They never
# start PowerShell, a compiler, a subprocess, or the native alias admission.
SMOKE = (ROOT / ".github/workflows/windows-alpha-script-smoke.yml").read_text(encoding="utf-8")
COMPILE_STEP_NAME = "Parse and compile exact Git alias sources only"
POST_STEP_NAME = "Independently admit Git alias compiler artifacts"


def smoke_step(name):
    return SMOKE.split("      - name: " + name + "\n", 1)[1].split("\n      - name:", 1)[0]


def test_hosted_alias_qualification_keeps_existing_route_job_and_permissions():
    assert re.findall(r"(?m)^  ([a-z][a-z0-9_-]*):$", SMOKE.split("\njobs:\n", 1)[1]) == [
        "windows-alpha-script"
    ]
    assert "    runs-on: windows-latest\n" in SMOKE
    assert "    timeout-minutes: 15\n" in SMOKE
    assert "permissions:\n  contents: read\n" in SMOKE
    assert "workflow_dispatch" not in SMOKE and "workflow_call" not in SMOKE
    assert "self-hosted" not in SMOKE and "continue-on-error" not in SMOKE
    assert SMOKE.index("name: Checkout") < SMOKE.index("name: Parse Windows alpha PowerShell")
    assert SMOKE.index("name: Parse Windows alpha PowerShell") < SMOKE.index(
        "name: " + COMPILE_STEP_NAME
    )
    assert SMOKE.index("name: " + COMPILE_STEP_NAME) < SMOKE.index("name: " + POST_STEP_NAME)
    assert SMOKE.index("name: " + POST_STEP_NAME) < SMOKE.index("name: Set up Python")


def test_hosted_alias_source_pins_match_final_helper_and_csharp_bytes():
    compile_step = smoke_step(COMPILE_STEP_NAME)
    for name in ("assert-installed-git-alias.ps1", "admit_installed_git_alias.cs"):
        data = (ROOT / "scripts" / name).read_bytes().replace(b"\r\n", b"\n")
        pin = f"Read-PinnedCheckoutSource '{name}' {len(data)} '{hashlib.sha256(data).hexdigest()}'"
        assert compile_step.count(pin) == 1
    reader = section("function Read-PinnedCheckoutSource", "\n          try {", compile_step)
    assert reader.count("[IO.File]::Open(") == 1
    assert "[IO.FileAccess]::Read, [IO.FileShare]::Read" in reader
    assert reader.index("$stream.Length -gt 2 * $Size") < reader.index("[byte[]]::new(")
    assert "$stream.ReadByte() -ne -1" in reader
    assert "[Text.UTF8Encoding]::new($false, $true)" in reader
    assert '$utf8.GetString($raw).Replace("`r`n", "`n")' in reader
    assert '$text.Contains("`r")' in reader
    assert reader.index("$bytes.Length -ne $Size") < reader.index("$sha256.ComputeHash($bytes)")
    assert reader.index("$actual -cne $Digest") < reader.index("return $utf8.GetString($bytes)")
    for forbidden in (
        "ReadAllText",
        "ReadAllBytes",
        "Get-Content",
        "Get-FileHash",
        "ParseFile",
        "Trim(",
    ):
        assert forbidden not in compile_step


def canonical_checkout_model(raw, expected):
    """Only the known Windows checkout newline conversion precedes exact pins."""
    if not len(expected) <= len(raw) <= 2 * len(expected):
        raise ValueError("source_size")
    text = raw.decode("utf-8", errors="strict").replace("\r\n", "\n")
    if "\r" in text:
        raise ValueError("source_encoding")
    data = text.encode("utf-8", errors="strict")
    if len(data) != len(expected):
        raise ValueError("source_size")
    if hashlib.sha256(data).digest() != hashlib.sha256(expected).digest():
        raise ValueError("source_hash")
    return data


@pytest.mark.parametrize("name", ["assert-installed-git-alias.ps1", "admit_installed_git_alias.cs"])
def test_hosted_checkout_model_accepts_only_verified_lf_bytes_after_crlf_normalization(name):
    expected = (ROOT / "scripts" / name).read_bytes().replace(b"\r\n", b"\n")
    assert b"\r" not in expected
    assert canonical_checkout_model(expected, expected) == expected
    assert canonical_checkout_model(expected.replace(b"\n", b"\r\n"), expected) == expected
    assert canonical_checkout_model(expected.replace(b"\n", b"\r\n", 1), expected) == expected
    variants = [
        expected.replace(b"\n", b"\r", 1),
        expected.replace(b"\n", b"\r\r\n", 1),
        expected + b" ",
        expected[:-1],
        b"\xef\xbb\xbf" + expected,
        b"!" + expected[1:],
        b"\xff" + expected[1:],
        expected * 3,
    ]
    for changed in variants:
        with pytest.raises((ValueError, UnicodeError)):
            canonical_checkout_model(changed, expected)


def test_hosted_alias_only_parses_helper_and_compiles_csharp_with_existing_framework():
    body = smoke_step(COMPILE_STEP_NAME)
    assert "timeout-minutes: 2\n" in body and "shell: powershell\n" in body
    assert "$env:RUNNER_ENVIRONMENT -cne 'github-hosted'" in body
    assert "$env:RUNNER_OS -cne 'Windows'" in body
    assert "$PSVersionTable.PSEdition -cne 'Desktop'" in body
    assert "$PSVersionTable.PSVersion.Major -ne 5" in body
    assert "$PSVersionTable.PSVersion.Minor -ne 1" in body
    assert "-not [Environment]::Is64BitProcess" in body
    assert "-not [Environment]::Is64BitOperatingSystem" in body
    assert "$runtime -cne 'C:\\Windows\\Microsoft.NET\\Framework64\\v4.0.30319'" in body
    assert "foreach ($name in @('csc.exe', 'System.Core.dll'))" in body
    assert "$systemCorePath = Join-Path $runtime 'System.Core.dll'" in body
    assert "('K5ExactGitAlias' -as [type])" in body
    assert "Parser]::ParseInput($helper, [ref]$tokens, [ref]$parseErrors)" in body
    assert "$parseErrors.Count -ne 0" in body
    assert body.count("Add-Type ") == 1
    assert (
        "Add-Type -TypeDefinition $source -Language CSharp -PassThru "
        "-ReferencedAssemblies $systemCorePath" in body
    )
    assert "-WarningAction SilentlyContinue -Verbose:$false -Debug:$false" in body
    assert "$_.FullName -ceq 'K5ExactGitAlias'" in body
    assert body.index("Parser]::ParseInput") < body.index("Add-Type ")


def test_hosted_alias_compiler_private_temp_restored_without_hiding_first_failure():
    body = smoke_step(COMPILE_STEP_NAME)
    assert "$env:GITHUB_RUN_ID -cnotmatch '\\A[1-9][0-9]{0,19}\\z'" in body
    assert "$env:GITHUB_RUN_ATTEMPT -cnotmatch '\\A[1-9][0-9]{0,5}\\z'" in body
    assert "'k5-git-alias-smoke-' + $env:GITHUB_RUN_ID + '-' + $env:GITHUB_RUN_ATTEMPT" in body
    assert body.index("Test-Path -LiteralPath $compilerTemp") < body.index(
        "New-Item -Path $compilerTemp"
    )
    assert "New-Item -Path $compilerTemp -ItemType Directory -ErrorAction Stop" in body
    for variable in ("TEMP", "TMP"):
        assert body.index(
            f"SetEnvironmentVariable('{variable}', $compilerTemp, 'Process')"
        ) < body.index("Add-Type ")
    assert "SetEnvironmentVariable('TEMP', $previousTemp, 'Process')" in body
    assert "SetEnvironmentVariable('TMP', $previousTmp, 'Process')" in body
    assert "catch { $compileFailure = $_; throw }" in body
    assert "if ($null -eq $restoreError) { $restoreError = $_ }" in body
    assert "$null -ne $restoreError -and $null -eq $compileFailure" in body


def test_hosted_alias_post_is_independent_always_run_after_process_exit():
    compile_step, post = smoke_step(COMPILE_STEP_NAME), smoke_step(POST_STEP_NAME)
    assert "if: ${{ always() }}" in post
    assert "timeout-minutes: 1\n" in post and "shell: powershell\n" in post
    assert "EnumerateFileSystemEntries" not in compile_step
    assert "Add-Type" not in post and "Read-PinnedCheckoutSource" not in post
    assert "steps.git_alias_compile" not in post and "GITHUB_OUTPUT" not in compile_step + post
    for body in (compile_step, post):
        assert "'k5-git-alias-smoke-' + $env:GITHUB_RUN_ID + '-' + $env:GITHUB_RUN_ATTEMPT" in body
        assert (
            "Get-CimInstance -ClassName Win32_Process -Property Name -OperationTimeoutSec 10"
            in body
        )
        assert "@('csc.exe', 'cvtres.exe', 'vbc.exe', 'VBCSCompiler.exe')" in body
        assert "$rows.Count -lt 1 -or $rows.Count -gt 32768" in body
        assert "$row.Name.Length -gt 260" in body
        assert "$env:RUNNER_ENVIRONMENT -cne 'github-hosted'" in body
    assert post.index("Get-CimInstance") < post.index("EnumerateFileSystemEntries")


def test_hosted_alias_post_keeps_strict_shallow_file_only_artifact_bounds():
    post = smoke_step(POST_STEP_NAME)
    for token in (
        "Assert-OrdinaryPath $compilerTemp $true",
        "$file = Assert-OrdinaryPath $entry $false",
        "[bool]$item.PSIsContainer -ne $Directory",
        "[IO.FileAttributes]::ReparsePoint",
        "-not [string]::IsNullOrEmpty($item.LinkType)",
        "$count -gt 32",
        "[IO.Directory]::EnumerateFileSystemEntries($compilerTemp).GetEnumerator()",
        "$count -gt 64",
        "[long]$bytes = 0",
        "$file.Length -lt 0",
        "$file.Length -gt 16777216 - $bytes",
        "$bytes += $file.Length",
        "$entries.Dispose()",
    ):
        assert token in post
    assert post.index("$count -gt 64") < post.index("$file = Assert-OrdinaryPath")
    assert post.index("$file.Length -gt 16777216 - $bytes") < post.index("$bytes += $file.Length")
    assert "-Recurse" not in post and "SearchOption" not in post


@pytest.mark.parametrize("name", [COMPILE_STEP_NAME, POST_STEP_NAME])
def test_hosted_alias_steps_have_no_native_invocation_download_cleanup_or_raw_output(name):
    body = smoke_step(name)
    for forbidden in (
        "::Begin(",
        "::Complete(",
        ".Begin(",
        ".Complete(",
        "& $helper",
        "[scriptblock]::Create",
        "Remove-Item",
        "[IO.File]::Delete",
        "[IO.Directory]::Delete",
        "Stop-Process",
        "Start-Process",
        "Invoke-WebRequest",
        "HttpWebRequest",
        "Invoke-RestMethod",
        "Invoke-Expression",
        "pip install",
        "winget",
        "choco",
        "dotnet",
        "Start-Job",
        "& git",
        "CommandLine",
        "Write-Error",
        "$_.Exception",
        "$_.Message",
        "Write-Output $",
        "throw $_",
        "throw $compileFailure",
    ):
        assert forbidden not in body
    assert "[Console]::Error.WriteLine('" in body and "exit 1" in body


def artifact_post_model(entries, *, compiler_idle=True, present=True):
    if not compiler_idle:
        raise ValueError("compiler_occupied")
    total = 0
    if present:
        for count, (kind, reparse, hardlink, length) in enumerate(entries, 1):
            if count > 64 or kind != "file" or reparse or hardlink:
                raise ValueError("compiler_artifacts")
            if length < 0 or length > 16777216 - total:
                raise ValueError("compiler_artifacts")
            total += length
    return total


def test_artifact_post_model_accepts_only_bounded_shallow_ordinary_files():
    ordinary = ("file", False, False, 0)
    assert artifact_post_model([]) == 0
    assert artifact_post_model([ordinary] * 64) == 0
    assert artifact_post_model([("file", False, False, 16777216)]) == 16777216
    bad_sets = [
        [ordinary] * 65,
        [("directory", False, False, 0)],
        [("file", True, False, 0)],
        [("file", False, True, 0)],
        [("file", False, False, -1)],
        [("file", False, False, 16777217)],
        [("file", False, False, 16777216), ("file", False, False, 1)],
    ]
    for entries in bad_sets:
        with pytest.raises(ValueError, match="compiler_artifacts"):
            artifact_post_model(entries)
    with pytest.raises(ValueError, match="compiler_occupied"):
        artifact_post_model([], compiler_idle=False, present=False)


@pytest.mark.parametrize("compile_success", [False, True])
@pytest.mark.parametrize("post_success", [False, True])
def test_two_stage_model_requires_compile_and_post_success_without_early_artifact_admission(
    compile_success, post_success
):
    events = [
        "parse",
        "compile",
        "powershell_exit",
        "independent_compiler_idle",
        "retained_artifacts",
    ]
    assert events.index("powershell_exit") < events.index("retained_artifacts")
    accepted = compile_success and post_success
    assert accepted is (compile_success is True and post_success is True)
    if not compile_success:
        assert not accepted  # A missing temp accepted by post never rescues compilation.


def test_hosted_compile_failure_projection_is_closed_typed_and_keeps_private_error():
    body = smoke_step(COMPILE_STEP_NAME)
    projection = section("function Get-CompileFailureProjection", "\n          $phase =", body)
    stages = set(re.findall(r"'([a-z_]+)'", section("$Phase -cin @(", "))", projection)))
    assigned = set(re.findall(r"\$phase = '([a-z_]+)'", body))
    assert assigned == stages
    reasons = set(re.findall(r"\$reason = '([a-z_]+)'", projection))
    assert reasons == {
        "unknown",
        "runtime",
        "context",
        "path",
        "compiler_inventory",
        "compiler_occupied",
        "source_size",
        "source_read",
        "source_encoding",
        "source_hash",
        "source_parse",
        "type_reuse",
        "compiler_temp_exists",
        "compiler_temp",
        "compiled_type",
        "compiler_environment",
        "compiler_error",
    }
    assert "$failureError = $_" in body
    assert "Get-CompileFailureProjection $failureError $phase" in body
    assert "switch -CaseSensitive ($FailureRecord.Exception.Message)" in projection
    assert "$stage -ceq 'compile'" in projection
    assert "$FailureRecord.FullyQualifiedErrorId -cin @(" in projection
    assert "'SOURCE_CODE_ERROR,Microsoft.PowerShell.Commands.AddTypeCommand'" in projection
    assert "'COMPILER_ERRORS,Microsoft.PowerShell.Commands.AddTypeCommand'" in projection
    assert "$FailureRecord.TargetObject -is [System.CodeDom.Compiler.CompilerError]" in projection
    assert "$FailureRecord.TargetObject.ErrorNumber -cmatch '\\ACS[0-9]{4}\\z'" in projection
    assert (
        "return @{ stage = $stage; reason = $reason; compiler_code = $compilerCode }" in projection
    )
    output = body.split("[Console]::Error.WriteLine(", 1)[1].split("\n", 1)[0]
    assert set(re.findall(r"\$([a-zA-Z][a-zA-Z0-9_.]*)", output)) == {
        "projection.stage",
        "projection.reason",
        "projection.compiler_code",
    }
    for forbidden in ("ErrorText", "Line", "Column", "FileName", "ToString", "InnerException"):
        assert forbidden not in projection


@pytest.mark.parametrize(
    "stage,identifier,kind,code,expected",
    [
        ("compile", "SOURCE_CODE_ERROR", "CompilerError", "CS0246", "CS0246"),
        ("compile", "COMPILER_ERRORS", "CompilerError", "CS0001", "CS0001"),
        ("compiled_type", "SOURCE_CODE_ERROR", "CompilerError", "CS0246", "unknown"),
        ("compile", "OTHER", "CompilerError", "CS0246", "unknown"),
        ("compile", "SOURCE_CODE_ERROR", "Exception", "CS0246", "unknown"),
        ("compile", "SOURCE_CODE_ERROR", "CompilerError", "cs0246", "unknown"),
        ("compile", "SOURCE_CODE_ERROR", "CompilerError", "CS0246\nsecret", "unknown"),
        ("compile", "SOURCE_CODE_ERROR", "CompilerError", r"C:\secret.cs", "unknown"),
        ("compile", "SOURCE_CODE_ERROR", "CompilerError", "CS12345", "unknown"),
    ],
)
def test_hosted_compile_diagnostic_model_never_leaks_untyped_or_noncanonical_code(
    stage, identifier, kind, code, expected
):
    result = "unknown"
    if (
        stage == "compile"
        and identifier in {"SOURCE_CODE_ERROR", "COMPILER_ERRORS"}
        and kind == "CompilerError"
        and re.fullmatch(r"CS[0-9]{4}", code)
    ):
        result = code
    assert result == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        (b"first\nsecond\n", b"first\nsecond\n"),
        (b"first\r\nsecond\r\n", b"first\nsecond\n"),
        (b"first\r\nsecond\n", b"first\nsecond\n"),
        (b"\xef\xbb\xbffirst\r\n", b"\xef\xbb\xbffirst\n"),
    ],
)
def test_source_pin_reader_only_canonicalizes_checkout_crlf(raw, expected):
    # BOMs and all other content remain byte-visible to the unchanged exact pins.
    assert canonical_checkout_bytes(raw) == expected


@pytest.mark.parametrize("raw", [b"first\rsecond\n", b"first\r\r\n", b"first\n\xff"])
def test_source_pin_reader_rejects_bare_cr_and_invalid_utf8(raw):
    with pytest.raises(ValueError):
        canonical_checkout_bytes(raw)
