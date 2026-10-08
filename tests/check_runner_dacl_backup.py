"""Source contracts and independent pure models, not Windows/native proof.

Run with python -I. No third-party imports, product code, process/network/native/DB.
"""

import copy
import hashlib
import json
import re
import struct
import sys
import unittest
from pathlib import Path


def guard(event, args):
    if event in {"subprocess.Popen", "os.system", "socket.connect", "socket.bind", "ctypes.dlopen"}:
        raise RuntimeError("prohibited " + event)


sys.addaudithook(guard)
ROOT = Path(__file__).resolve().parents[1]
CS = (ROOT / "scripts/observe_installed_git_links.cs").read_text()
PS = (ROOT / "scripts/observe-installed-git-links.ps1").read_text()
PRE = (ROOT / "scripts/prepare-git-link-metadata.ps1").read_text()
WF = (ROOT / ".github/workflows/installed-git-link-metadata.yml").read_text()
SID = "S-1-5-21-283315059-370827648-873861665-1000"
TRUSTED = {SID, "S-1-5-18", "S-1-5-32-544"}


def section(source, first, last):
    return source.split(first, 1)[1].split(last, 1)[0]


def parse_name(payload, status=0, io_status=0, used=None):
    used = len(payload) if used is None else used
    if status == 0x80000006:
        if io_status != status or used != 0:
            raise ValueError("enumeration")
        return None
    if status != 0 or io_status != 0 or not 14 <= used <= 4096 or used > len(payload):
        raise ValueError("enumeration")
    next_entry, _, length = struct.unpack_from("<III", payload)
    if next_entry != 0 or not 2 <= length <= 2048 or length % 2:
        raise ValueError("enumeration")
    if not 12 + length <= used <= (12 + length + 7) & ~7:
        raise ValueError("enumeration")
    return payload[12:12 + length].decode("utf-16-le", "strict")


def wire(name):
    data = name.encode("utf-16-le")
    return struct.pack("<III", 0, 0, len(data)) + data


def custody(owner, aces, ancestor=False):
    if owner not in TRUSTED:
        return False
    for ace in aces:
        if ace["type"] not in (0, 1) or ace.get("callback", False):
            return False
        if ace["type"] == 0 and not ace["flags"] & 8 and ace["mask"]:
            if (not ancestor or ace["mask"] & 0x100D0040) and ace["sid"] not in TRUSTED:
                return False
    return True


def freeze(records):
    return json.dumps(records, sort_keys=True, separators=(",", ":")).encode()


class SourceContracts(unittest.TestCase):
    def test_no_acl_setter_or_service_control(self):
        for text in (CS, PS):
            for forbidden in ("SetSecurityInfo", "SetNamedSecurityInfo", "SetFileSecurity", "Set-Acl",
                              "AdjustTokenPrivileges", "Start-Service", "Stop-Service", "Set-Service"):
                self.assertNotIn(forbidden, text)
        self.assertNotIn("Win32_Service", PS)
        self.assertNotIn("Win32_UserAccount", PS)
        self.assertNotIn('OpenRelative(runner, ".service"', CS)

    def test_native_traversal_never_reopens_directory_paths(self):
        self.assertEqual(CS.count("Native.CreateFileW("), 1)
        self.assertNotIn("Directory.Enumerate", CS)
        self.assertIn("attributes.RootDirectory = parent.Handle;", CS)
        self.assertIn("ObjCaseInsensitive | ObjDontReparse", CS)
        self.assertIn("Native.NtQueryDirectoryFile(directory.Handle", CS)
        self.assertIn("(uint)buffer.Length, 12, true, IntPtr.Zero, restart", CS)
        self.assertEqual(CS.count("[MarshalAs(UnmanagedType.U1)] bool"), 2)

    def test_inventory_bounds_and_links(self):
        for token in ("MaxObjects = 50000", "MaxDaclBytes = 64L * 1024 * 1024",
                      "MaxPathUnits = 8L * 1024 * 1024", "depth <= 32", "path.Length <= 4096",
                      "MaxHandles = 64", "++discoveredCount < MaxObjects", "enumeratedUnits <= MaxPathUnits",
                      'Require(info.Links == 1, "alias_count")', "identities.Add(objectId)"):
            self.assertIn(token, CS)

    def test_two_snapshots_are_exact_dacl_identity_comparisons(self):
        body = section(CS, "internal Dictionary<string, object> RunInventory()", "internal Dictionary<string, object> SaveBackup")
        self.assertEqual(body.count("Snapshot(root)"), 2)
        self.assertIn("first.Count == second.Count", body)
        self.assertIn("Object.Equals(first[i][key], second[i][key])", body)
        for key in ("path", "object_id", "parent_id", "object_kind", "control", "dacl_base64", "attributes"):
            self.assertIn('"' + key + '"', body)

    def test_live_log_content_changes_are_not_acl_changes(self):
        body = section(CS, "private static bool SameObject", "private void Release")
        for excluded in ("Modified", "Created", "Changed", "Size"):
            self.assertNotIn(excluded, body)
        self.assertNotIn("U64(basic, 16) == info.Modified.Value", CS)
        self.assertIn("ShareRead = 0x00000003", CS)

    def test_dacl_inventory_does_not_require_sacl_or_group(self):
        self.assertIn("checkBackup != 0 ? 0x00000005U : 0x00000004U", CS)
        self.assertIn("new RawSecurityDescriptor(flags, null, null, null,", CS)
        self.assertNotIn("ACCESS_SYSTEM_SECURITY", CS)
        self.assertNotIn("SeSecurityPrivilege", CS)
        self.assertIn("ControlFlags.DiscretionaryAclProtected", CS)
        self.assertIn("ControlFlags.DiscretionaryAclAutoInherited", CS)

    def test_backup_is_outside_root_create_new_and_handle_relative(self):
        body = section(CS, "internal Dictionary<string, object> SaveBackup", "internal bool CloseAll()")
        for token in ('parts.Length == 5', 'parts[1] == "Users"', 'parts[3] == "AppData"',
                      'parts[4] == "Local"', 'attributes.RootDirectory = local.Handle;',
                      '0x00000080, 1, 2, FileNonDirectoryFile', 'io.Information.ToUInt64() == 2',
                      'stream.Flush(true)', 'Hex(hash.ComputeHash(stream)) == digest'):
            self.assertIn(token, body)
        self.assertNotIn("CreateDirectory", body)
        self.assertNotIn("File.WriteAll", body)
        self.assertLess(body.index("ReadAclRecord(handle, 1)"), body.index("stream.Write(bytes"))
        self.assertLess(body.index("ReadAclRecord(parent.Handle, 2)"), body.index("Native.NtCreateFile"))

    def test_backup_custody_and_fresh_revalidation(self):
        for token in ("trusted.Contains(raw.Owner.Value)", "!common.IsCallback", "0x100d0040U",
                      '"backup_parent_ids", parentIds', '"requires_fresh_custody_revalidation", true'):
            self.assertIn(token, CS)
        self.assertIn("requires_fresh_backup_identity_hash_and_custody_revalidation=$true", PS)
        self.assertIn("rollback_status='not_implemented'", PS)

    def test_same_profile_is_verified_on_actual_worker_listener_ancestry(self):
        body = section(PS, "function Assert-K5SameProfileRunner", "function Save-K5DaclInventory")
        for token in (SID, "$principal.IsInRole($au)", "-MethodName GetOwnerSid", "$owner.Sid -cne $expectedSid",
                      "C:\\K5PhysicalRunner\\bin\\Runner.Worker.exe", "C:\\K5PhysicalRunner\\bin\\Runner.Listener.exe",
                      "$node.CreationDate.ToUniversalTime().Ticks", "$workers -ne 1", "$depth -lt 32"):
            self.assertIn(token, body)
        self.assertIn("$after.identity_fingerprint -cne $Writer.identity_fingerprint", PS)
        self.assertEqual(PS.count("= Assert-K5SameProfileRunner"), 2)

    def test_aggregate_deadline_and_job_margins(self):
        self.assertIn("null, 150000, System.Threading.Timeout.Infinite", CS)
        self.assertIn("MaxElapsedMs = 75000", CS)
        self.assertIn("}, null, 90000,", CS)
        self.assertIn("}, null, 30000,", CS)
        start = PS.index("$deadline = [K5FixedGitObservation]::StartObservationDeadline()")
        self.assertLess(start, PS.index("$writer = Assert-K5SameProfileRunner"))
        self.assertLess(PS.index("$result = Save-K5DaclInventory"), PS.index("$deadline.Dispose()"))
        self.assertIn("id: observe\n        timeout-minutes: 3", WF)
        self.assertIn("timeout-minutes: 6", WF)
        self.assertIn("timeout-minutes: 5", WF)
        self.assertIn("id: post\n        if: ${{ always() }}\n        timeout-minutes: 2", WF)

    def test_no_backup_or_readiness_success_after_refusal(self):
        self.assertIn('result["readback_verified"] = false', CS)
        self.assertIn("backup_verified=$false; backup_may_exist=$true", PS)
        self.assertIn("repair_ready = $false; backup_verified = $false", PS)
        self.assertIn("$result.status -cne 'observed' -or -not $result.backup_verified", PS)
        self.assertNotIn("repair_ready=$true", PS)
        self.assertNotIn('{ "repair_ready", true }', CS)

    def test_original_admission_guard_unchanged(self):
        digest = hashlib.sha256((ROOT / "scripts/assert-stage-one-physical-admission.ps1").read_bytes()).hexdigest()
        self.assertEqual(digest, "d7a38b5278802d9ba768d9987b4582a219d490923b0cc4da0c297d29a250b45d")
        self.assertIn("Invoke-PhysicalPost", PRE)
        self.assertEqual(WF.count("if: ${{ always() }}"), 2)

    def test_all_source_and_transport_pins(self):
        digest = hashlib.sha256((ROOT / "scripts/observe_installed_git_links.cs").read_bytes()).hexdigest()
        self.assertIn("$collectorSha256 = '" + digest + "'", PS)
        for name in ("assert-stage-one-physical-admission.ps1", "observe_installed_git_links.cs", "observe-installed-git-links.ps1"):
            data = (ROOT / "scripts" / name).read_bytes()
            blob = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
            sha = hashlib.sha256(data).hexdigest()
            self.assertIn(f"name = '{name}'; size = {len(data)}; blob = '{blob}'; sha256 = '{sha}'", PRE)
        data = (ROOT / "scripts/prepare-git-link-metadata.ps1").read_bytes()
        blob = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
        self.assertEqual(WF.count("$expectedBlob = '" + blob + "'"), 4)
        self.assertEqual(WF.count("$expectedDigest = '" + hashlib.sha256(data).hexdigest() + "'"), 4)


class IndependentModels(unittest.TestCase):
    def test_directory_names_use_byte_lengths_and_unicode(self):
        for name in (".", "..", "a.txt", "name-😀", "x~file"):
            self.assertEqual(parse_name(wire(name)), name)

    def test_directory_terminal_status_must_be_complete(self):
        self.assertIsNone(parse_name(b"", 0x80000006, 0x80000006, 0))
        for io_status, used in ((0, 0), (0x80000006, 1)):
            with self.subTest(io_status=io_status, used=used), self.assertRaises(ValueError):
                parse_name(b"", 0x80000006, io_status, used)

    def test_directory_partial_or_warning_status_refuses(self):
        for status, io_status in ((0x80000005, 0), (0, 1), (1, 0), (-1, -1)):
            with self.subTest(status=status), self.assertRaises(ValueError):
                parse_name(wire("name"), status, io_status)

    def test_directory_malformed_offsets_and_lengths_refuse(self):
        for next_entry, length in ((4, 2), (0, 0), (0, 1), (0, 2049), (0, 4096)):
            payload = struct.pack("<III", next_entry, 0, length) + b"a\0"
            with self.subTest(next_entry=next_entry, length=length), self.assertRaises(ValueError):
                parse_name(payload)

    def test_directory_truncation_and_excess_tail_refuse(self):
        payload = wire("name")
        for changed in (payload[:-1], payload + b"\0" * 8):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                parse_name(changed)

    def test_directory_invalid_utf16_refuses(self):
        with self.assertRaises(UnicodeError):
            parse_name(struct.pack("<III", 0, 0, 2) + b"\0\xd8")

    def test_backup_file_custody_trusted_only(self):
        for principal in TRUSTED:
            self.assertTrue(custody(SID, [{"type": 0, "flags": 0, "mask": 0x1F01FF, "sid": principal}]))
        self.assertFalse(custody("S-1-5-11", []))
        for mask in (1, 0x1200A9, 0x1F01FF):
            self.assertFalse(custody(SID, [{"type": 0, "flags": 0, "mask": mask, "sid": "S-1-5-11"}]))

    def test_backup_ancestor_rejects_effective_takeover_and_delete(self):
        for mask in (0x40, 0x10000, 0x40000, 0x80000, 0x10000000):
            self.assertFalse(custody(SID, [{"type": 0, "flags": 0, "mask": mask, "sid": "S-1-5-11"}], True))
        for mask in (2, 4, 0x1200A9):
            self.assertTrue(custody(SID, [{"type": 0, "flags": 0, "mask": mask, "sid": "S-1-5-11"}], True))

    def test_backup_inherit_only_and_deny_are_not_effective_allows(self):
        self.assertTrue(custody(SID, [{"type": 0, "flags": 8, "mask": 0x1F01FF, "sid": "S-1-3-0"}]))
        self.assertTrue(custody(SID, [{"type": 1, "flags": 0, "mask": 0x1F01FF, "sid": "S-1-1-0"}]))
        self.assertFalse(custody(SID, [{"type": 5, "flags": 0, "mask": 1, "sid": SID}]))
        self.assertFalse(custody(SID, [{"type": 0, "flags": 0, "mask": 1, "sid": SID, "callback": True}]))

    def test_inventory_identity_dacl_inheritance_changes_break_comparison(self):
        original = [{"object_id": "01234567:0000000000000001", "path": "C:\\K5PhysicalRunner", "control": 0x8404,
                     "dacl_base64": "AAAA", "parent_id": "outside-approved-root", "object_kind": "directory"}]
        for field, value in (("object_id", "other"), ("control", 0x9404), ("dacl_base64", "AAAB"),
                             ("path", "C:\\Other"), ("parent_id", "other")):
            changed = copy.deepcopy(original)
            changed[0][field] = value
            self.assertNotEqual(freeze(changed), freeze(original))

    def test_readback_and_membership_changes_break_backup_hash(self):
        expected = freeze([{"object_id": "a", "control": 0x8404}])
        digest = hashlib.sha256(expected).hexdigest()
        self.assertEqual(hashlib.sha256(bytes(expected)).hexdigest(), digest)
        for corrupted in (expected[:-1], expected + b"\0", freeze([])):
            self.assertNotEqual(hashlib.sha256(corrupted).hexdigest(), digest)


if __name__ == "__main__":
    unittest.main(verbosity=2)
