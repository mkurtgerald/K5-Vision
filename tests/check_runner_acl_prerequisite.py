"""Pure source contracts and independent models, never Windows/native execution.

Run with python -I. No product import, pytest plugins, subprocess, network or DB.
These checks do not prove PowerShell parsing, C# compilation or Windows behavior.
"""

import copy
import hashlib
import re
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
NAME = "actions.runner.mkurtgerald-K5-Vision.K5-Physical"


def service_name(data, links=1):
    if links != 1 or not 0 < len(data) <= 512:
        raise ValueError("service_metadata")
    text = data.decode("utf-8", "strict")
    if text.startswith("\ufeff"):
        text = text[1:]
    if text.endswith("\r\n"):
        text = text[:-2]
    elif text.endswith("\n"):
        text = text[:-1]
    if not 15 < len(text) <= 256 or not re.fullmatch(r"actions\.runner\.[A-Za-z0-9_.-]+", text):
        raise ValueError("service_metadata")
    return text


def identity_snapshot(snapshot):
    """Independent acceptance model, not a PowerShell interpreter."""
    service, processes = snapshot["service"], snapshot["processes"]
    if service["name"] != NAME or service["state"] != "Running" or service["type"] != "Own Process":
        raise ValueError("service_scope")
    if service["path"] not in {r"C:\K5PhysicalRunner\bin\RunnerService.exe", '"C:\\K5PhysicalRunner\\bin\\RunnerService.exe"'}:
        raise ValueError("service_scope")
    if snapshot["configured_sid"] != snapshot["owner_sid"] or snapshot["token_sid"] != snapshot["configured_sid"]:
        raise ValueError("account_mismatch")
    by_id = {p["pid"]: p for p in processes}
    if not 0 < len(processes) <= 32768 or len(by_id) != len(processes):
        raise ValueError("process_query")
    service_process = by_id.get(service["pid"])
    if service_process is None or service_process["name"] != "RunnerService.exe":
        raise ValueError("service_process")
    cursor, seen, chain, workers = snapshot["current_pid"], set(), [], 0
    for _ in range(32):
        if cursor in seen or cursor not in by_id:
            raise ValueError("owned_ancestry")
        seen.add(cursor)
        node = by_id[cursor]
        if node["created"] is None:
            raise ValueError("owned_ancestry")
        chain.append((cursor, node["created"]))
        workers += node["name"] == "Runner.Worker.exe"
        if cursor == service["pid"]:
            if workers != 1:
                raise ValueError("owned_ancestry")
            return (tuple(chain), tuple(sorted(service.items())), snapshot["configured_sid"])
        parent = by_id.get(node["parent"])
        if parent is None or parent["created"] is None or parent["created"] > node["created"]:
            raise ValueError("owned_ancestry")
        cursor = node["parent"]
    raise ValueError("owned_ancestry")


def good_snapshot():
    return {
        "service": {"name": NAME, "state": "Running", "type": "Own Process", "pid": 10,
                    "path": r"C:\K5PhysicalRunner\bin\RunnerService.exe"},
        "configured_sid": SID, "owner_sid": SID, "token_sid": SID, "current_pid": 40,
        "processes": [
            {"pid": 10, "parent": 1, "name": "RunnerService.exe", "created": 100},
            {"pid": 20, "parent": 10, "name": "Runner.Listener.exe", "created": 110},
            {"pid": 30, "parent": 20, "name": "Runner.Worker.exe", "created": 120},
            {"pid": 40, "parent": 30, "name": "powershell.exe", "created": 130},
        ],
    }


class SourceContracts(unittest.TestCase):
    def test_native_exports_unchanged_and_readonly(self):
        exports = set(re.findall(r"internal static extern \w+ (\w+)\(", CS))
        self.assertEqual(exports, {"NtOpenFile", "NtQueryInformationFile", "CreateFileW",
            "GetFileInformationByHandle", "GetVolumeInformationByHandleW", "GetFileType", "ReadFile",
            "SetFilePointerEx", "CloseHandle", "LocalFree", "GetSecurityInfo", "IsValidSecurityDescriptor",
            "GetSecurityDescriptorControl", "GetSecurityDescriptorLength"})
        for text in (CS, PS):
            for prohibited in ("SetSecurityInfo", "SetFileSecurity", "SetNamedSecurityInfo", "Set-Acl",
                               "AdjustTokenPrivileges", "icacls", "Start-Service", "Stop-Service",
                               "Restart-Service", "Set-Service", "Set-CimInstance"):
                self.assertNotIn(prohibited, text)
            self.assertIsNone(re.search(r"(?<![A-Za-z])sc\.exe\b", text))

    def test_same_handle_framework_and_missing_file_reason(self):
        for token in ("attributes.RootDirectory = parent.Handle;", "ObjCaseInsensitive | ObjDontReparse",
                      "FileOpenReparsePoint | FileSynchronousIoNonalert", "ShareRead", "MaxHandles = 64",
                      "MaxElapsedMs = 8000", 'OpenRelative(runner, ".service", false, true)',
                      "status == unchecked((int)0xc0000034)", 'throw new Refusal("service_metadata_absent")'):
            self.assertIn(token, CS)
        self.assertEqual(CS.count("Native.CreateFileW("), 1)

    def test_bounded_service_file_and_stability(self):
        for token in ("file.Initial.Links == 1 && file.Initial.Size <= 512", "BytesRead <= 1024 - bytes.Length",
                      "new UTF8Encoding(false, true)", "received == bytes.Length", "ReadServiceName(serviceFile) == serviceName",
                      "item.Initial.Same(ReadMetadata(item.Handle, item.Directory))"):
            self.assertIn(token, CS)
        self.assertIn("Assert-K5Integer $Record['bytes_read'] 0 1024", PS)
        for code in ("service_metadata_absent", "service_metadata", "read_failed", "bytes_bound", "alias_count"):
            self.assertIn("'" + code + "'", PS)

    def test_local_readonly_identity_queries_are_bounded(self):
        body = PS.split("function Get-K5ConfiguredRunnerIdentity", 1)[1].split("function Assert-K5DiagnosticRecord", 1)[0]
        self.assertEqual(body.count("Get-CimInstance -ClassName"), 3)
        self.assertEqual(body.count("-OperationTimeoutSec 5"), 4)
        self.assertEqual(body.count("Invoke-CimMethod"), 1)
        self.assertIn("-MethodName GetOwnerSid", body)
        self.assertIn("LocalAccount=True AND Domain='VLR-CYZ4PK3'", body)
        self.assertIn("$pass -lt 2", body)
        self.assertIn("$depth -lt 32", body)
        self.assertIn("$processes.Count -gt 32768", body)
        for token in ("-ComputerName", "-CimSession", ".Translate(", "Get-Credential", "CommandLine"):
            self.assertNotIn(token, body)

    def test_separate_sid_and_ancestry_evidence(self):
        for token in ("$owner.Sid -cne $configuredSid", "$TokenSid -cne $configuredSid",
                      "$node.CreationDate.ToUniversalTime().Ticks", "$workerCount -ne 1",
                      "$fingerprint -cne $previous", "$identity.User.Value -cne $TokenSid"):
            self.assertIn(token, PS)

    def test_no_repair_readiness_or_subtree_backup_claim(self):
        for token in ("$sanitized['repair_ready'] = $false", "$sanitized['required_writers'] = 'unresolved'",
                      "$sanitized['affected_subtree_inventory'] = 'not_collected'",
                      "$sanitized['rollback_backup'] = 'not_created'"):
            self.assertIn(token, PS)
        self.assertIn("$result.service_status -cne 'verified'", PS)

    def test_wrapper_exact_pin(self):
        sha = hashlib.sha256((ROOT / "scripts/observe_installed_git_links.cs").read_bytes()).hexdigest()
        self.assertIn("$collectorSha256 = '" + sha + "'", PS)

    def test_guard_and_transport_safety_unchanged(self):
        guard_hash = hashlib.sha256((ROOT / "scripts/assert-stage-one-physical-admission.ps1").read_bytes()).hexdigest()
        self.assertEqual(guard_hash, "d7a38b5278802d9ba768d9987b4582a219d490923b0cc4da0c297d29a250b45d")
        self.assertIn("timeout-minutes: 5", WF)
        self.assertIn("timeout-minutes: 6", WF)
        self.assertEqual(WF.count("if: ${{ always() }}"), 2)
        self.assertNotIn("workflow_dispatch:", WF)
        self.assertIn("Invoke-PhysicalPost", PRE)
        self.assertIn("Assert-RetainedArtifacts", PRE)


class IndependentModels(unittest.TestCase):
    def test_service_name_expected_encodings(self):
        for suffix in (b"", b"\n", b"\r\n"):
            for prefix in (b"", b"\xef\xbb\xbf"):
                self.assertEqual(service_name(prefix + NAME.encode() + suffix), NAME)

    def test_service_name_rejects_injection_and_malformed_data(self):
        for value in (b"", b"\xff", b"x" * 513, b"actions.runner.", b"actions.runner.x' OR 1=1",
                      b"actions.runner.x\x00", b"actions.runner.x\r", b"actions.runner.x\n\n",
                      b"actions.runner.x/y", b"actions.runner.x\\y", b"actions.runner.\xc3\xa9"):
            with self.subTest(value=value), self.assertRaises((ValueError, UnicodeError)):
                service_name(value)

    def test_service_name_rejects_hardlinks(self):
        for count in (0, 2, 16, 17):
            with self.subTest(count=count), self.assertRaises(ValueError):
                service_name(NAME.encode(), count)

    def test_stable_identity_matches(self):
        snapshot = good_snapshot()
        self.assertEqual(identity_snapshot(snapshot), identity_snapshot(copy.deepcopy(snapshot)))

    def test_distinct_configured_live_and_current_sid(self):
        for key in ("configured_sid", "owner_sid", "token_sid"):
            snapshot = good_snapshot()
            snapshot[key] = "S-1-5-18"
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "account_mismatch"):
                identity_snapshot(snapshot)

    def test_scope_changes_refuse(self):
        for key, value in (("path", r"C:\OtherRunner\bin\RunnerService.exe"), ("state", "Stopped"),
                           ("type", "Share Process"), ("name", "other"), ("pid", 999)):
            snapshot = good_snapshot()
            snapshot["service"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                identity_snapshot(snapshot)

    def test_pid_reuse_changes_fingerprint(self):
        first = good_snapshot()
        second = copy.deepcopy(first)
        second["processes"][0]["created"] += 1
        self.assertNotEqual(identity_snapshot(first), identity_snapshot(second))

    def test_duplicate_missing_cycle_and_time_inversion_refuse(self):
        for kind in ("duplicate", "missing", "cycle", "newer_parent", "missing_time", "no_worker"):
            snapshot = good_snapshot()
            if kind == "duplicate":
                snapshot["processes"].append(copy.deepcopy(snapshot["processes"][0]))
            elif kind == "missing":
                snapshot["processes"][3]["parent"] = 999
            elif kind == "cycle":
                snapshot["processes"][3]["parent"] = 40
            elif kind == "newer_parent":
                snapshot["processes"][2]["created"] = 140
            elif kind == "missing_time":
                snapshot["processes"][2]["created"] = None
            else:
                snapshot["processes"][2]["name"] = "other.exe"
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                identity_snapshot(snapshot)


if __name__ == "__main__":
    unittest.main(verbosity=2)
