"""Source contracts and an independent wire-format model; never compile/run C#.

These tests do not establish Windows ABI behavior, filesystem race resistance,
ACL behavior, watchdog scheduling, or successful installed-Git observation.
"""

from __future__ import annotations

import re
import struct
import unicodedata
from pathlib import Path

import pytest

SOURCE = (
    Path(__file__).resolve().parents[1] / "scripts" / "observe_installed_git_links.cs"
).read_text(encoding="utf-8")
ROOT_ID, CMD_ID = 101, 202
MAX_TOTAL = 128 * 1024 * 1024


def section(start, end):
    return SOURCE.split(start, 1)[1].split(end, 1)[0]


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


def wire(entries=((CMD_ID, "git.exe"),), *, tail_padding=False):
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


def parse(data, *, links=1, status=0, io_status=0, completed=None):
    used = len(data) if completed is None else completed
    if status != 0 or io_status != 0 or not 0 < used <= 65536 or used > len(data):
        raise ValueError("enumeration")
    data = data[:used]
    if len(data) < 30:
        raise ValueError("enumeration")
    needed, count = struct.unpack_from("<II", data)
    if needed != len(data):
        raise ValueError("enumeration")
    if not 1 <= count <= 16 or count != links:
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
        if parent not in (ROOT_ID, CMD_ID):
            raise ValueError("alias_outside_root")
        relative = ("cmd/" if parent == CMD_ID else "") + name
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
    return sorted(result, key=lambda item: item[1])


def changed_u32(data, offset, value):
    result = bytearray(data)
    struct.pack_into("<I", result, offset, value)
    return result


def test_public_schema_is_fail_closed_and_has_no_partial_refusal_data():
    assert "public static Dictionary<string, object> Observe()" in SOURCE
    base = section("private static Dictionary<string, object> BaseRecord()", "private sealed class Refusal")
    for token in (
        '"fixed-git-hardlink-observation-v2"', '"cmd/git.exe"', '{ "status", "refused" }',
        '{ "closure_complete", false }', '{ "exception_authority", false }',
        '{ "storage_admission", false }', '{ "retry_authority", false }',
    ):
        assert token in base
    assert '"aliases"' not in base and '"primary_sha256"' not in base
    assert 'Codes.Contains(error.Code) ? error.Code : "internal"' in SOURCE
    assert 'result["bytes_read"] = observation.BytesRead;' in SOURCE
    assert 'result["elapsed_ms"] = elapsed;' in SOURCE
    assert 'ToString("x8", CultureInfo.InvariantCulture)' in SOURCE
    assert 'ToString("x16", CultureInfo.InvariantCulture)' in SOURCE


def test_native_surface_is_read_only_and_system_dll_only():
    exports = re.findall(r"internal static extern \w+ (\w+)\(", SOURCE)
    assert set(exports) == {
        "NtOpenFile", "NtQueryInformationFile", "CreateFileW", "GetFileInformationByHandle",
        "GetVolumeInformationByHandleW", "GetFileType", "ReadFile", "SetFilePointerEx",
        "CloseHandle", "LocalFree", "GetSecurityInfo", "IsValidSecurityDescriptor",
        "GetSecurityDescriptorControl", "GetSecurityDescriptorLength",
    }
    assert SOURCE.count("DefaultDllImportSearchPaths(DllImportSearchPath.System32)") == len(exports)
    assert set(re.findall(r'\[DllImport\("([^"]+)"', SOURCE)) == {
        "ntdll.dll", "kernel32.dll", "advapi32.dll"
    }
    for prohibited in (
        "Process.Start", "File.Write", "Directory.Get", "Directory.Enumerate", "GetFiles(",
        "OpenFileById", "FindFirstFile", "FindFirstFileName", "CreateHardLink", "WriteFile(",
        "SetSecurityInfo", "AdjustTokenPrivileges", "Registry.", "WebClient", "HttpClient",
        "LoadLibrary", "GetProcAddress", "Thread.Sleep", "Process.GetProcesses", "Kill(",
    ):
        assert prohibited not in SOURCE


def test_single_component_handle_relative_open_and_synchronous_read_rights():
    anchor = section("private Held OpenAnchor()", "private Held OpenRelative")
    relative = section("private Held OpenRelative", "// Every query")
    assert SOURCE.count("Native.CreateFileW(") == 1
    assert '@"\\\\?\\C:\\"' in anchor
    assert "IntPtr.Zero, 3," in anchor
    assert "private const uint ShareRead = 0x00000001;" in SOURCE
    assert "attributes.RootDirectory = parent.Handle;" in relative
    assert "attributes.Attributes = ObjCaseInsensitive | ObjDontReparse;" in relative
    assert "ReadAttributes | ReadControl | Synchronize | (directory ? Traverse : ReadData)" in relative
    assert "FileOpenReparsePoint | FileSynchronousIoNonalert" in relative
    assert relative.index("ValidateComponent(component)") < relative.index("Native.NtOpenFile(")
    assert "Own(handle);" in relative and "status == 0 && io.Status == 0" in relative
    for token in (
        "ObjDontReparse = 0x00001000", "FileOpenReparsePoint = 0x00200000",
        "FileSynchronousIoNonalert = 0x00000020", "Synchronize = 0x00100000",
        'OpenRelative(anchor, "Program Files", true)', 'OpenRelative(programFiles, "Git", true)',
        'OpenRelative(git, "cmd", true)', 'OpenRelative(cmd, "git.exe", false)',
    ):
        assert token in SOURCE


def test_native_layout_and_completed_length_contracts():
    for token in (
        "IntPtr.Size == 8 && BitConverter.IsLittleEndian", "LayoutKind.Explicit, Size = 48",
        'Offset(typeof(ObjectAttributes), "RootDirectory") == 8',
        'Offset(typeof(IoStatusBlock), "Information") == 8',
        'Offset(typeof(LinkEntryLayout), "ParentFileId") == 8',
        'Offset(typeof(LinkEntryLayout), "NameUnits") == 16',
        'Offset(typeof(LinkEntryLayout), "FirstNameUnit") == 20',
        'Offset(typeof(LinksLayout), "Entry") == 8',
        "FileHardLinkInformation = 46", "LinkBufferBytes = 65536",
        "status == 0 && io.Status == 0 && used > 0", "needed == (uint)bytes.Length",
        "int nameBytes = checked((int)nameUnits * 2)",
        "new UnicodeEncoding(false, false, true)",
    ):
        assert token in SOURCE


def test_ntfs_identity_and_acl_are_handle_only_and_rechecked():
    for token in (
        "Native.GetVolumeInformationByHandleW(handle, IntPtr.Zero, 0",
        'String.Equals(filesystem.ToString(), "NTFS", StringComparison.Ordinal)',
        "U64(Query(handle, 6, 8, true), 0)", "identifier != 0 && identifier == byHandleId",
        "serial == info.VolumeSerial", "Query(primary.Handle, FileHardLinkInformation",
        "parentId == git.Initial.Id", "parentId == cmd.Initial.Id",
        'else throw new Refusal("alias_outside_root")',
        "Native.GetSecurityInfo(handle, 1, 0x00000005",
        "out owner, IntPtr.Zero, out dacl, IntPtr.Zero, out descriptor",
        'Require(dacl != IntPtr.Zero, "acl_null")',
        "new RawSecurityDescriptor(retained", "raw.Owner, null, null, raw.DiscretionaryAcl",
        "stable.GetBinaryForm(normalized, 0)", "Native.LocalFree(descriptor)",
        "alias.Initial.Same(primary.Initial)", "foreach (Held item in held)",
        "item.Initial.Same(ReadMetadata(item.Handle, item.Directory))",
        "String.Equals(item.Acl, ReadAcl(item.Handle), StringComparison.Ordinal)",
    ):
        assert token in SOURCE
    assert SOURCE.count("Closure(primary, git, cmd)") == 2
    assert SOURCE.count("Digest(primary)") == 2


def test_preflight_read_cleanup_and_watchdog_contracts():
    digest = section("private string Digest(Held file)", "internal Dictionary<string, object> Run()")
    assert digest.index("BytesRead <= MaxTotalBytes - request") < digest.index("Native.ReadFile(")
    assert "primary.Initial.Size <= MaxTotalBytes / (primary.Initial.Links + 2L)" in SOURCE
    assert "owned.Count < MaxHandles" in SOURCE and "MaxHandles = 64" in SOURCE
    assert "MaxFileBytes = 32L * 1024 * 1024" in SOURCE and "MaxElapsedMs = 8000" in SOURCE
    assert "for (int i = owned.Count - 1; i >= 0; i--)" in SOURCE
    assert "if (!observation.CloseAll())" in SOURCE
    assert "long elapsed = observation.ElapsedMs;" in SOURCE
    assert "complete != null && elapsed > MaxElapsedMs" in SOURCE
    guard = section("public static Dictionary<string, object> ObserveGuarded()", "public static Dictionary<string, object> Observe()")
    assert "delegate(object state) { Environment.Exit(124); }, null, 20000" in guard
    assert "System.Threading.Timeout.Infinite" in guard
    assert "try { return Observe(); }" in guard and "finally { watchdog.Dispose(); }" in guard


@pytest.mark.parametrize("padding", [False, True])
def test_wire_model_primary_and_allowed_parent_ids(padding):
    payload = wire(((ROOT_ID, "git-copy.exe"), (CMD_ID, "git.exe")), tail_padding=padding)
    assert parse(payload, links=2) == [(CMD_ID, "cmd/git.exe"), (ROOT_ID, "git-copy.exe")]


@pytest.mark.parametrize("status, io_status", [(1, 0), (0, 1), (0x80000005, 0), (-1, -1)])
def test_wire_model_exact_success_only(status, io_status):
    with pytest.raises(ValueError, match="enumeration"):
        parse(wire(), status=status, io_status=io_status)


@pytest.mark.parametrize("completed", [0, 7, 29, 33, 65537])
def test_wire_model_completed_span_is_bounded(completed):
    with pytest.raises(ValueError, match="enumeration"):
        parse(wire(), completed=completed)


@pytest.mark.parametrize("offset, value, code", [
    (0, 65537, "enumeration"), (0, 8, "enumeration"), (4, 0, "alias_count"),
    (4, 17, "alias_count"), (4, 2, "alias_count"), (8, 8, "enumeration"),
    (24, 0, "name_bound"), (24, 1025, "name_bound"), (24, 0xFFFFFFFF, "name_bound"),
    (24, 100, "enumeration"),
])
def test_wire_model_malformed_fields(offset, value, code):
    with pytest.raises(ValueError, match=code):
        parse(changed_u32(wire(), offset, value))


@pytest.mark.parametrize("next_offset", [0, 8, 20, 31, 33, 0xFFFFFFF8])
def test_wire_model_early_terminator_overlap_alignment_overflow(next_offset):
    payload = wire(((CMD_ID, "git.exe"), (ROOT_ID, "copy.exe")))
    with pytest.raises(ValueError, match="enumeration"):
        parse(changed_u32(payload, 8, next_offset), links=2)


@pytest.mark.parametrize("name", [
    "", ".", "..", "git.exe.", "git.exe ", "git.exe:stream", "x/y", "x\\y",
    "CON", "con.exe", "CON .exe", "PRN.log", "AUX", "NUL", "CLOCK$", "CONIN$",
    "CONOUT$", "COM0", "COM1.exe", "COM9", "LPT0", "LPT9.log", "COM¹.exe",
    "LPT²", "COM³", "x\x00y", "x\x1fy", "x\x7fy", "x\x85y", "x\ud800y", "x\udc00y",
])
def test_wire_model_component_refusals(name):
    with pytest.raises(ValueError, match="name_bound|path_shape|enumeration"):
        parse(wire(((CMD_ID, name),)))


def test_wire_model_zero_unknown_duplicate_parent_and_missing_primary():
    for entries, code in (
        (((0, "git.exe"),), "identity_mismatch"),
        (((999, "git.exe"),), "alias_outside_root"),
        (((CMD_ID, "git.exe"), (CMD_ID, "GIT.EXE")), "alias_duplicate"),
        (((ROOT_ID, "git.exe"),), "alias_missing_primary"),
    ):
        with pytest.raises(ValueError, match=code):
            parse(wire(entries), links=len(entries))


def test_wire_model_utf16_code_units_and_tail_bounds():
    entries = ((CMD_ID, "git.exe"), (ROOT_ID, "copy-😀.exe"))
    assert parse(wire(entries), links=2)[1][1] == "copy-😀.exe"
    data = wire()
    data += b"\x00" * 8
    struct.pack_into("<I", data, 0, len(data))
    with pytest.raises(ValueError, match="enumeration"):
        parse(data)


def test_budget_model_bounds_before_read_and_preflight():
    calls = []

    def bounded_read(total, request):
        if request <= 0 or total > MAX_TOTAL - request:
            raise ValueError("bytes_bound")
        calls.append(request)
        return total + request

    assert bounded_read(MAX_TOTAL - 65536, 65536) == MAX_TOTAL
    with pytest.raises(ValueError, match="bytes_bound"):
        bounded_read(MAX_TOTAL - 65535, 65536)
    assert calls == [65536]
    assert 32 * 1024 * 1024 <= MAX_TOTAL // (2 + 2)
    assert not 32 * 1024 * 1024 <= MAX_TOTAL // (3 + 2)
