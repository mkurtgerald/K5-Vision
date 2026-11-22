"""Diagnostic-only timing of one immutable installer; never a qualification gate."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path, PureWindowsPath

import psutil

HEAD = "7cc4e227fca152ebd04c5a7eae728e02fd4143aa"
SETUP_SHA = "427c8faa7e8aa647dbff5746f7620f9b0c3738c911ca58edd06bfebbccefa37b"
SETUP_BYTES = 286579963
OPERATIONS = {
    "--installation-identity": "guard_identity",
    "--installation-machine-extraction-check": "guard_extraction",
    "--installation-payload-check": "guard_payload",
    "--installation-service-legacy-check": "guard_legacy_check",
    "--installation-service-stop": "guard_stop",
    "--installation-service-prepare": "guard_prepare",
    "--installation-service-seal": "guard_seal",
    "--installation-service-install": "guard_install",
    "--installation-service-start": "guard_start",
    "--installation-service-remove": "guard_remove",
}

HASH_BENCHMARK = r"""
using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Security.Cryptography;
using System.Web.Script.Serialization;
internal static class HashProfile {
  private sealed class Totals { internal long Ticks; internal double Cpu; internal string Type; }
  private static byte[] Hash(FileStream stream, bool cng, Totals totals, Process process) {
    double cpu = process.TotalProcessorTime.TotalSeconds;
    long start = Stopwatch.GetTimestamp();
    byte[] result;
    using (SHA256 hash = cng ? new SHA256Cng() : SHA256.Create()) {
      string name = hash.GetType().Name;
      totals.Type = name == "SHA256Managed" || name == "SHA256Cng" ||
        name == "SHA256CryptoServiceProvider" ? name : "other";
      result = hash.ComputeHash(stream);
    }
    totals.Ticks += Stopwatch.GetTimestamp() - start;
    totals.Cpu += process.TotalProcessorTime.TotalSeconds - cpu;
    return result;
  }
  private static string Hex(byte[] value) {
    return BitConverter.ToString(value).Replace("-", "").ToLowerInvariant();
  }
  internal static int Main(string[] args) {
    try {
      if (args.Length != 3) return 1;
      byte[] raw = File.ReadAllBytes(args[1]);
      using (SHA256 hash = SHA256.Create())
        if (Hex(hash.ComputeHash(raw)) !=
          "a564b6b038da136a5378180e9e8d1164050c4a01eee010e9a5de884325c7e959") return 2;
      JavaScriptSerializer json = new JavaScriptSerializer { MaxJsonLength = 4 * 1024 * 1024 };
      var manifest = json.Deserialize<Dictionary<string, object>>(
        System.Text.Encoding.UTF8.GetString(raw));
      string root = Path.GetFullPath(args[0]);
      var first = new Totals(); var second = new Totals();
      int files = 0; long bytes = 0;
      using (Process process = Process.GetCurrentProcess()) {
        foreach (object entry in (IEnumerable)manifest["files"]) {
          var row = (Dictionary<string, object>)entry;
          string relative = (string)row["path"];
          if (relative.Length == 0 || relative.Contains("\\") || relative.Contains(":") ||
              relative.StartsWith("/")) return 3;
          foreach (string part in relative.Split('/'))
            if (part.Length == 0 || part == "." || part == "..") return 3;
          string target = Path.Combine(root, relative.Replace('/', '\\'));
          for (string current = target; current.Length >= root.Length;
               current = Path.GetDirectoryName(current))
            if ((File.GetAttributes(current) & FileAttributes.ReparsePoint) != 0) return 4;
          using (FileStream stream = new FileStream(
            target, FileMode.Open, FileAccess.Read, FileShare.Read)) {
            long length = Convert.ToInt64(row["bytes"]);
            if (stream.Length != length) return 5;
            byte[] a, b;
            if ((files & 1) == 0) {
              a = Hash(stream, false, first, process); stream.Position = 0;
              b = Hash(stream, true, second, process);
            }
            else {
              b = Hash(stream, true, second, process); stream.Position = 0;
              a = Hash(stream, false, first, process);
            }
            if (Hex(a) != (string)row["sha256"] || Hex(b) != (string)row["sha256"] ||
                stream.Length != length) return 6;
            bytes += length; files++;
          }
        }
      }
      var result = new Dictionary<string, object> {
        { "schema_version", 1 }, { "diagnostic_only", true }, { "all_digests_match", true },
        { "paired_read_locked_streams", true }, { "alternating_algorithm_order", true },
        { "files", files }, { "payload_bytes", bytes },
        { "default_algorithm_type", first.Type }, { "cng_algorithm_type", second.Type },
        { "default_wall_seconds", (double)first.Ticks / Stopwatch.Frequency },
        { "default_cpu_seconds", first.Cpu },
        { "cng_wall_seconds", (double)second.Ticks / Stopwatch.Frequency },
        { "cng_cpu_seconds", second.Cpu }
      };
      File.WriteAllText(args[2], json.Serialize(result));
      return 0;
    } catch { return 7; }
  }
}
"""


class ProfileFailure(RuntimeError):
    pass


class FatalCleanupFailure(ProfileFailure):
    pass


def require(value, code):
    if not value:
        raise ProfileFailure(code)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def category(image, arguments):
    name = PureWindowsPath(image).name.casefold()
    if name in {"k5vision.exe", "k5visioninstallguard.exe", "k5visionuninstallguard.exe"}:
        if len(arguments) in {2, 3} and arguments[1] in OPERATIONS:
            expected = 3 if arguments[1] == "--installation-identity" else 2
            if len(arguments) == expected:
                return OPERATIONS[arguments[1]]
        return "service_host_or_client"
    return {
        "k5visionsetup.exe": "setup",
        "k5visionrecorder.exe": "service_wrapper",
        "python.exe": "python",
        "pythonw.exe": "python",
        "unins000.exe": "uninstaller",
        "conhost.exe": "console_host",
    }.get(name, "inno_clone" if name.endswith(".tmp") else "other")


def log_summary(path):
    """Keep fixed counts/spans only; never retain arbitrary log lines or paths."""
    if not path.exists():
        return {"available": False}
    info = path.lstat()
    require(
        stat.S_ISREG(info.st_mode)
        and info.st_nlink == 1
        and not getattr(info, "st_file_attributes", 0) & 0x400
        and info.st_size <= 16 * 1024**2,
        "private_log_refused",
    )
    counts = {"file_entries": 0, "installing_files": 0, "installed_files": 0, "sha256_skips": 0}
    first = last = None
    expression = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\s+(.*)$")
    with path.open("r", encoding="utf-8-sig", errors="replace") as stream:
        for line in stream:
            require(len(line) <= 65536, "private_log_line_refused")
            match = expression.match(line)
            if match is None:
                continue
            moment, message = match.groups()
            if message == "-- File entry --":
                counts["file_entries"] += 1
            elif message == "Installing the file.":
                counts["installing_files"] += 1
                first = first or datetime.fromisoformat(moment)
            elif message == "Successfully installed the file.":
                counts["installed_files"] += 1
                last = datetime.fromisoformat(moment)
            elif message == "Existing file's SHA-256 hash matches our file. Skipping.":
                counts["sha256_skips"] += 1
    span = None if first is None or last is None else (last - first).total_seconds()
    return {
        "available": True,
        **counts,
        # This includes file callbacks and is not pure decompression time.
        "file_entries_wall_span_seconds": round(span, 3)
        if span is not None and 0 <= span <= 180
        else None,
    }


def tree_summary(root, manifest):
    """Classify leftovers read-only; unknown names and paths never leave here."""
    result = {"root_present": None, "snapshot_complete": False}
    try:
        info = root.lstat()
    except FileNotFoundError:
        return {"root_present": False, "snapshot_complete": True}
    except OSError:
        result["inspection_unavailable"] = True
        return result
    result["root_present"] = True
    root_reparse = stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & 0x400
    )
    result["root_kind"] = (
        "reparse"
        if root_reparse
        else (
            "directory"
            if stat.S_ISDIR(info.st_mode)
            else "file"
            if stat.S_ISREG(info.st_mode)
            else "other"
        )
    )
    bootstrap = {
        "k5vision.exe",
        "payload-manifest.json",
        "k5visionrecorder.exe",
        "k5visionrecorder.xml",
        "unins000.exe",
        "unins000.dat",
        "unins000.msg",
    }
    known = {item["path"].casefold() for item in manifest["files"]}
    known_directories = set()
    for name in known:
        parts = name.split("/")
        known_directories.update("/".join(parts[:index]) for index in range(1, len(parts)))
    result.update(
        dict.fromkeys(
            (
                "entries",
                "regular_files",
                "directories",
                "reparse_entries",
                "other_entries",
                "known_payload_files",
                "known_bootstrap_files",
                "unlisted_entries",
                "empty_directories",
            ),
            0,
        )
    )
    result["known_root_names"] = []
    result["root_empty"] = None
    deadline = time.monotonic() + 3
    try:
        require(
            stat.S_ISDIR(info.st_mode) and not root_reparse,
            "leftover_root_refused",
        )
        stack = [(root, "", (info.st_dev, info.st_ino))]
        while stack:
            require(time.monotonic() < deadline, "leftover_budget_exhausted")
            directory, relative, expected = stack.pop()
            current = directory.lstat()
            require(
                stat.S_ISDIR(current.st_mode)
                and not getattr(current, "st_file_attributes", 0) & 0x400
                and (current.st_dev, current.st_ino) == expected,
                "leftover_directory_changed",
            )
            empty = True
            with os.scandir(directory) as entries:
                for entry in entries:
                    require(
                        result["entries"] < 4096 and time.monotonic() < deadline,
                        "leftover_budget_exhausted",
                    )
                    empty = False
                    result["entries"] += 1
                    name = (relative + "/" if relative else "") + entry.name.casefold()
                    item = entry.stat(follow_symlinks=False)
                    if not relative and entry.name.casefold() in bootstrap:
                        result["known_root_names"].append(entry.name.casefold())
                    if stat.S_ISLNK(item.st_mode) or getattr(item, "st_file_attributes", 0) & 0x400:
                        result["reparse_entries"] += 1
                        continue
                    if stat.S_ISDIR(item.st_mode):
                        result["directories"] += 1
                        if name not in known_directories:
                            result["unlisted_entries"] += 1
                        stack.append((Path(entry.path), name, (item.st_dev, item.st_ino)))
                    elif stat.S_ISREG(item.st_mode):
                        result["regular_files"] += 1
                        if name in known:
                            result["known_payload_files"] += 1
                        elif not relative and name in bootstrap:
                            result["known_bootstrap_files"] += 1
                        else:
                            result["unlisted_entries"] += 1
                    else:
                        result["other_entries"] += 1
            after = directory.lstat()
            require((after.st_dev, after.st_ino) == expected, "leftover_directory_changed")
            if empty:
                result["empty_directories"] += 1
            if not relative:
                result["root_empty"] = empty
        result["snapshot_complete"] = True
    except (OSError, RuntimeError):
        result["inspection_unavailable"] = True
    result["known_root_names"].sort()
    return result


class Pin:
    def __init__(self, native, identity, now, image, arguments, origin):
        self.native, self.identity = native, identity
        self.handle = native.api.OpenProcess(0x00101000, False, identity[0])
        require(bool(self.handle), "process_pin_unavailable")
        self.value = {
            "pid": identity[0],
            "created_100ns": identity[1],
            "category": category(image, arguments),
            "origin": origin,
            "first_seen_seconds": round(now, 3),
            "last_seen_seconds": round(now, 3),
            "cpu_seconds": 0,
            "exit_observed_seconds": None,
            "exit_code": None,
        }
        try:
            self.sample(now)
        except BaseException:
            self.close()
            raise

    def sample(self, now):
        filetime = self.native.api.GetProcessTimes.argtypes[1]._type_
        created, exited, kernel, user = (filetime() for _ in range(4))
        require(
            self.native.api.GetProcessTimes(
                self.handle,
                ctypes.byref(created),
                ctypes.byref(exited),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ),
            "process_times_unavailable",
        )
        require((created.high << 32) | created.low == self.identity[1], "process_identity_changed")
        cpu = ((kernel.high << 32) | kernel.low) + ((user.high << 32) | user.low)
        self.value.update(last_seen_seconds=round(now, 3), cpu_seconds=round(cpu / 10**7, 6))
        state = self.native.api.WaitForSingleObject(self.handle, 0)
        require(state in {0, 258}, "process_wait_unavailable")
        if state == 0 and self.value["exit_observed_seconds"] is None:
            code = wintypes.DWORD()
            require(
                self.native.api.GetExitCodeProcess(self.handle, ctypes.byref(code)),
                "process_exit_unavailable",
            )
            self.value.update(exit_observed_seconds=round(now, 3), exit_code=code.value)

    def close(self):
        if self.handle:
            if not self.native.api.CloseHandle(self.handle):
                raise FatalCleanupFailure("process_pin_close_failed")
            self.handle = None


class Profiler:
    def __init__(self, source, installed):
        sys.path[:0] = [str(source / "src"), str(source / "tests" / "integration")]
        from windows_process_identity import NativeProcessTree
        from windows_service_acceptance import InstalledService

        from k5vision.operator_native_process import OwnedNativeProcess

        self.process_factory = OwnedNativeProcess
        self.service_factory = InstalledService
        self.native = NativeProcessTree()
        self.installed = installed
        self.service = None
        self.phases = []
        self.total_pins = 0

    def active(self, owned):
        class Accounting(ctypes.Structure):
            _fields_ = [
                ("user", ctypes.c_int64),
                ("kernel", ctypes.c_int64),
                ("period_user", ctypes.c_int64),
                ("period_kernel", ctypes.c_int64),
                ("faults", wintypes.DWORD),
                ("total", wintypes.DWORD),
                ("active", wintypes.DWORD),
                ("terminated", wintypes.DWORD),
            ]

        query = owned._job._api.QueryInformationJobObject
        query.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_void_p,
        ]
        query.restype = wintypes.BOOL
        result = Accounting()
        require(
            query(owned._job._handle, 1, ctypes.byref(result), ctypes.sizeof(result), None),
            "job_query_failed",
        )
        return result.active

    def observe(self, roots, pins, now, record):
        for root, origin in roots:
            try:
                identities = [root, *self.native.descendants(root)]
                for identity in identities:
                    if identity in pins:
                        continue
                    require(len(pins) < 512, "process_inventory_limit")
                    detail = self.native.details(identity)
                    try:
                        arguments = psutil.Process(identity[0]).cmdline()
                    except psutil.Error:
                        arguments = []
                    pins[identity] = Pin(
                        self.native, identity, now, detail["image"], arguments, origin
                    )
            except FatalCleanupFailure:
                record["untransferred_query_cleanup_failed"] = True
                raise
            except (OSError, RuntimeError, psutil.Error):
                record["discovery_unavailable_samples"] += 1
        for pin in pins.values():
            try:
                pin.sample(now)
            except (OSError, RuntimeError):
                record["query_unavailable_samples"] += 1

    def service_root(self):
        if not self.installed.exists():
            return None
        if self.service is None:
            self.service = self.service_factory(self.installed)
        if self.service.absent():
            return None
        configuration = self.service.configuration()
        require(configuration["installed_executable_verified"], "service_image_refused")
        require(
            configuration["account"].casefold() == r"NT SERVICE\K5VisionRecorder".casefold(),
            "service_account_refused",
        )
        status = self.service.status()
        if status["pid"]:
            process = psutil.Process(status["pid"])
            identity = process.pid, self.service.native_birth(process)
            facts = self.native.details(identity)
            require(facts["session"] == 0, "service_session_refused")
            require(facts["sid"] == self.native.service_sid(), "service_token_refused")
            require(
                Path(facts["image"]).resolve() == self.installed / "K5VisionRecorder.exe",
                "service_runtime_image_refused",
            )
            require(self.service.status()["pid"] == identity[0], "service_instance_changed")
            return identity
        return None

    def run(self, label, arguments, *, timeout, log=None):
        started = time.monotonic()
        record = {
            "phase": label,
            "discovery_unavailable_samples": 0,
            "query_unavailable_samples": 0,
            "discovery_is_best_effort": True,
        }
        self.phases.append(record)
        pins, owned = {}, None
        try:
            owned = self.process_factory(arguments, cwd=self.installed.parent)
            root = owned.pid, self.native.birth(owned.pid)
            require(root[1] is not None, "controller_birth_unavailable")
            last_service = 0.0
            service_root = None
            while True:
                now = time.monotonic() - started
                if now - last_service >= 0.2:
                    try:
                        service_root = self.service_root()
                    except (OSError, RuntimeError, psutil.Error):
                        record["discovery_unavailable_samples"] += 1
                    last_service = now
                roots = [(root, "command_job")]
                if service_root:
                    roots.append((service_root, "scm"))
                self.observe(roots, pins, now, record)
                code = owned.poll()
                if code is not None and self.active(owned) == 0:
                    record["exit_code"] = code
                    require(code == 0, "owned_command_failed")
                    break
                require(now < timeout, "diagnostic_phase_timeout")
                time.sleep(0.05)
        finally:
            record["elapsed_seconds"] = round(time.monotonic() - started, 3)
            try:
                if owned is not None:
                    owned.stop()
            finally:
                cleanup_failed = False
                for pin in pins.values():
                    try:
                        pin.sample(time.monotonic() - started)
                    except (OSError, RuntimeError):
                        record["query_unavailable_samples"] += 1
                    finally:
                        try:
                            pin.close()
                        except (OSError, RuntimeError):
                            cleanup_failed = True
                record["owned_job_closed"] = owned is None or owned._job is None
                record["processes"] = [pin.value for pin in pins.values()]
                self.total_pins += len(pins)
                record["all_query_handles_closed"] = not record.get(
                    "untransferred_query_cleanup_failed", False
                ) and all(pin.handle is None for pin in pins.values())
                if log is not None:
                    record["inno_log"] = log_summary(log)
                require(not cleanup_failed, "query_handle_cleanup_incomplete")


def hash_benchmark(profiler, scratch, installed, manifest_path, manifest):
    source = scratch / "HashProfile.cs"
    executable = scratch / "HashProfile.exe"
    output = scratch / "hash-profile.json"
    source.write_text(HASH_BENCHMARK, encoding="utf-8")
    compiler = Path(os.environ["SYSTEMROOT"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    profiler.run(
        "hash_benchmark_compile",
        [
            str(compiler),
            "/nologo",
            "/target:exe",
            "/platform:x64",
            "/optimize+",
            "/reference:System.dll",
            "/reference:System.Core.dll",
            "/reference:System.Web.Extensions.dll",
            "/out:" + str(executable),
            str(source),
        ],
        timeout=30,
    )
    profiler.run(
        "hash_benchmark",
        [str(executable), str(installed), str(manifest_path.resolve()), str(output)],
        timeout=45,
    )
    require(output.stat().st_size <= 16384, "hash_benchmark_output_refused")
    value = json.loads(output.read_bytes())
    require(
        set(value)
        == {
            "schema_version",
            "diagnostic_only",
            "all_digests_match",
            "paired_read_locked_streams",
            "alternating_algorithm_order",
            "files",
            "payload_bytes",
            "default_algorithm_type",
            "cng_algorithm_type",
            "default_wall_seconds",
            "default_cpu_seconds",
            "cng_wall_seconds",
            "cng_cpu_seconds",
        }
        and value["schema_version"] == 1
        and all(
            value[name] is True
            for name in (
                "diagnostic_only",
                "all_digests_match",
                "paired_read_locked_streams",
                "alternating_algorithm_order",
            )
        )
        and value["files"] == len(manifest["files"])
        and value["payload_bytes"] == sum(row["bytes"] for row in manifest["files"])
        and value["default_algorithm_type"]
        in {"SHA256Managed", "SHA256Cng", "SHA256CryptoServiceProvider", "other"}
        and value["cng_algorithm_type"] == "SHA256Cng",
        "hash_benchmark_output_refused",
    )
    for name in (
        "default_wall_seconds",
        "default_cpu_seconds",
        "cng_wall_seconds",
        "cng_cpu_seconds",
    ):
        require(
            type(value[name]) in {int, float} and 0 <= value[name] <= 90,
            "hash_benchmark_timing_refused",
        )
    return value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = {
        "schema_version": 1,
        "diagnostic_only": True,
        "product_qualification": False,
        "installer_head": HEAD,
        "setup_sha256": SETUP_SHA,
        "source_run_id": 44987532116,
        "source_run_attempt": 1,
        "producer_job_id": 137101275650,
        "controller_sha256": digest(Path(__file__)),
        "sample_interval_ms": 50,
        "artifact_id": 14740417355,
        "native_windows": os.name == "nt",
        "camera_acceptance": False,
        "recording_acceptance": False,
        "normal_gate_or_deadline_changed": False,
    }
    profiler = None
    failure = None
    owned = False
    try:
        harness = os.environ.get("K5_INSTALL_PROFILE_HARNESS_SHA", "")
        require(re.fullmatch(r"[0-9a-f]{40}", harness) is not None, "harness_identity_refused")
        result["harness_head"] = harness
        require(os.name == "nt", "native_windows_required")
        setup = (args.input / "K5VisionSetup.exe").resolve(strict=True)
        require(
            setup.stat().st_size == SETUP_BYTES and digest(setup) == SETUP_SHA,
            "setup_bytes_refused",
        )
        manifest_path = args.input / "payload-manifest.json"
        require(
            manifest_path.stat().st_size <= 4 * 1024**2
            and digest(manifest_path)
            == "a564b6b038da136a5378180e9e8d1164050c4a01eee010e9a5de884325c7e959",
            "manifest_bytes_refused",
        )
        manifest = json.loads(manifest_path.read_bytes())
        installed = Path(os.environ.get("ProgramW6432", os.environ["ProgramFiles"])) / "K5 Vision"
        profile = Path(os.environ["PROGRAMDATA"]) / "K5Vision"
        require(not installed.exists() and not profile.exists(), "fresh_runner_required")
        require(
            not any(s.name().casefold() == "k5visionrecorder" for s in psutil.win_service_iter()),
            "fresh_runner_required",
        )
        profiler = Profiler(args.source.resolve(strict=True), installed)
        owned = True
        with tempfile.TemporaryDirectory(prefix="k5-install-profile-") as scratch:
            scratch = Path(scratch)
            options = [
                "/SP-",
                "/VERYSILENT",
                "/SUPPRESSMSGBOXES",
                "/NORESTART",
                "/DIR=" + str(installed),
            ]
            try:
                for label in ("first_install", "identical_reinstall"):
                    log = scratch / (label + ".log")
                    profiler.run(
                        label, [str(setup), *options, "/LOG=" + str(log)], timeout=90, log=log
                    )
                    require(
                        profiler.service is not None and profiler.service.status()["state"] == 4,
                        "service_not_running",
                    )
                    require(not (profile / "identity").exists(), "unexpected_site_initialized")
                    if label == "first_install":
                        result["hash_benchmark"] = hash_benchmark(
                            profiler, scratch, installed, manifest_path, manifest
                        )
                        profiler.run(
                            "graceful_service_stop",
                            [str(installed / "K5Vision.exe"), "--installation-service-stop"],
                            timeout=70,
                        )
                        require(profiler.service.status()["state"] == 1, "service_not_stopped")
                log = scratch / "uninstall.log"
                profiler.run(
                    "uninstall",
                    [
                        str(installed / "unins000.exe"),
                        "/VERYSILENT",
                        "/SUPPRESSMSGBOXES",
                        "/NORESTART",
                        "/LOG=" + str(log),
                    ],
                    timeout=45,
                    log=log,
                )
            finally:
                try:
                    if installed.exists() and (installed / "unins000.exe").exists():
                        profiler.run(
                            "cleanup_uninstall",
                            [
                                str(installed / "unins000.exe"),
                                "/VERYSILENT",
                                "/SUPPRESSMSGBOXES",
                                "/NORESTART",
                            ],
                            timeout=45,
                        )
                finally:
                    result["installed_tree_after_uninstall"] = tree_summary(installed, manifest)
                    try:
                        result["service_absent_after_uninstall"] = (
                            profiler.service is not None and profiler.service.absent()
                        )
                    except (OSError, RuntimeError):
                        result["service_absent_after_uninstall"] = None
            require(
                result["installed_tree_after_uninstall"].get("root_present") is False,
                "installed_tree_cleanup_incomplete",
            )
            require(result["service_absent_after_uninstall"], "service_cleanup_incomplete")
            result["owned_cleanup"] = True
            result["site_data_preserved_without_account"] = (
                profile.exists() and not (profile / "identity").exists()
            )
    except Exception as error:
        failure = str(error) if isinstance(error, ProfileFailure) else "controller_failure"
        result["failure"] = failure
        result["owned_cleanup"] = False
    finally:
        result["owned_installation_started"] = owned
        if profiler is not None:
            result["phases"] = profiler.phases
            result["observed_process_instances"] = profiler.total_pins
        args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return 1 if failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
