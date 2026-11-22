"""Exercise the actual native legacy marker validation without opening a site.

Only public checkout source, generated temporary C# and a finite JSON receipt
are used. Filesystem/ACL admission and installed migration remain separate gates.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SOURCE_FILES = (
    "windows/desktop/K5Vision.cs",
    "windows/desktop/K5RecorderHost.cs",
    "windows/desktop/K5ServiceInstallation.cs",
)
_TOKENS = re.compile(
    r'//[^\n]*|/\*.*?\*/|@"(?:""|[^"])*"|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[{}]',
    re.S,
)


def _digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _extract(text, anchor):
    if text.count(anchor) != 1:
        raise ValueError("Native source anchor is ambiguous")
    start = text.index(anchor)
    opening = text.index("{", start)
    depth = 0
    for token in _TOKENS.finditer(text, opening):
        if token.group() == "{":
            depth += 1
        elif token.group() == "}":
            depth -= 1
            if depth == 0:
                return text[start : token.end()]
    raise ValueError("Native source block is incomplete")


def materialize(root, *, bridge_source=None):
    """Compile current source bodies; never reproduce their admission policy."""
    sources = {}
    for name in _SOURCE_FILES:
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("Native source must be an ordinary checkout file")
        sources[name] = path.read_text(encoding="utf-8")
    parser = _extract(sources[_SOURCE_FILES[0]], "internal static class Json")
    owners = _extract(
        sources[_SOURCE_FILES[1]], "private static HashSet<string> Owners(string data)"
    )
    mark = _extract(sources[_SOURCE_FILES[2]], "private static void MarkLegacyProfiles()")
    bridge_source = sources[_SOURCE_FILES[0]] if bridge_source is None else bridge_source
    bridge = _extract(bridge_source, "internal sealed class Bridge : IDisposable")
    constructor = _extract(bridge, "internal Bridge(")
    if constructor.count("ProcessStartInfo start =") != 1:
        raise ValueError("Native bridge constructor context is ambiguous")
    fields = bridge[bridge.index("{") + 1 : bridge.index("internal Bridge(")].strip()
    context = constructor[
        constructor.index("{") + 1 : constructor.index("ProcessStartInfo start =")
    ].strip()
    receive = _extract(bridge, "private void Receive(object sender, DataReceivedEventArgs args)")
    fixture = (
        _BRIDGE.replace("@@FIELDS@@", fields)
        .replace("@@CONTEXT@@", context)
        .replace("@@RECEIVE@@", receive)
    )
    anchor = (
        "Dictionary<string, object> value = Json.Parse(File.ReadAllText(marker, Encoding.UTF8));"
    )
    if owners.count(anchor) != 1:
        raise ValueError("Native marker validator is ambiguous")
    validation = owners[owners.index(anchor) : owners.rindex("}")].rstrip()
    # The fixture supplies text in memory. Actual filesystem admission remains
    # before this unchanged validation tail in the product's Owners method.
    validation = validation.replace("File.ReadAllText(marker, Encoding.UTF8)", "text")
    traversal = re.findall(
        r'foreach \(object sid in \((?:object\[\]|IList)\)previous\["profiles"\]\)'
        r"\s+if \(!found\.Contains\(\(string\)sid\)\) found\.Add\(\(string\)sid\);",
        mark,
    )
    if len(traversal) != 1:
        raise ValueError("Native existing-marker traversal is ambiguous")
    generated = (
        _CS.replace("@@JSON@@", parser)
        .replace("@@BRIDGE@@", fixture)
        .replace("@@VALIDATION@@", validation)
        .replace("@@TRAVERSAL@@", traversal[0])
    )
    if "@@" in generated:
        raise ValueError("Native probe substitution is incomplete")
    return generated, {
        "normalized_source_sha256": {name: _digest(value) for name, value in sources.items()},
        "json_class_sha256": _digest(parser),
        "owners_method_sha256": _digest(owners),
        "mark_method_sha256": _digest(mark),
        "validation_sha256": _digest(validation),
        "traversal_sha256": _digest(traversal[0]),
        "compiled_probe_sha256": _digest(generated),
        "bridge_source_sha256": _digest(bridge_source),
        "bridge_fields_sha256": _digest(fields),
        "bridge_context_sha256": _digest(context),
        "bridge_receive_sha256": _digest(receive),
        "bridge_process_launch_exercised": False,
        "filesystem_and_acl_admission_exercised": False,
        "product_modified": False,
        "profile_or_service_written": False,
    }


_BRIDGE = r"""
internal sealed class BridgeFixture {
    @@FIELDS@@
    internal BridgeFixture(string root, Dictionary<string, object> request, object job,
        bool persistent, bool serviceWorker = false) {
        @@CONTEXT@@
        Process = null; // The fixture never launches or owns a product process.
    }
    @@RECEIVE@@
    internal void Feed(string text) {
        ConstructorInfo constructor = typeof(DataReceivedEventArgs).GetConstructor(
            BindingFlags.Instance | BindingFlags.NonPublic, null, new[] { typeof(string) }, null);
        if (constructor == null) throw new InvalidOperationException();
        Receive(null, (DataReceivedEventArgs)constructor.Invoke(new object[] { text }));
    }
    internal bool Admitted { get { return !InvalidOutput && outcome != null; } }
}
internal static class BridgeCases {
    private const string Migration = "{\"schema_version\":1,\"status\":\"migration_required\"," +
        "\"reason\":\"legacy_upgrade_required\"}";
    private static Dictionary<string, object> Request(string operation) {
        return new Dictionary<string, object> { {"schema_version", 1}, {"operation", operation} };
    }
    private static string Response(string status) {
        return Json.Serializer.Serialize(new Dictionary<string, object> {
            {"schema_version", 1}, {"status", status} });
    }
    private static string Progress(int value) {
        return "{\"schema_version\":1,\"status\":\"preparing_recordings\",\"progress\":" +
            value + "}";
    }
    private static bool Admits(bool service, bool persistent, string operation, string frame,
        bool running = false, Dictionary<string, object> request = null) {
        BridgeFixture bridge = new BridgeFixture("", request ?? Request(operation), null,
            persistent, service);
        try {
            bridge.Feed(frame);
            return bridge.Admitted && bridge.Running.WaitOne(0) == running;
        } finally { bridge.Running.Dispose(); }
    }
    private static bool InvalidSequence(string[] frames, bool persistent = true) {
        BridgeFixture bridge = new BridgeFixture("", Request("serve"), null, persistent, true);
        try {
            foreach (string frame in frames) bridge.Feed(frame);
            return bridge.InvalidOutput;
        } finally { bridge.Running.Dispose(); }
    }
    private static bool ProgressPreserved() {
        BridgeFixture bridge = new BridgeFixture("", Request("serve"), null, true, true);
        try {
            bridge.Feed(Progress(0)); bridge.Feed(Progress(10)); bridge.Feed(Progress(100));
            bridge.Feed(Response("running"));
            return bridge.Admitted && bridge.PreparingRecordings &&
                bridge.RecordingsProgress == 100 && bridge.Running.WaitOne(0);
        } finally { bridge.Running.Dispose(); }
    }
    private static bool TwoRepliesPreserved() {
        BridgeFixture bridge = new BridgeFixture("", Request("serve"), null, true, true);
        try {
            bridge.Feed(Response("running")); bridge.Feed(Response("stopped"));
            return bridge.Admitted && bridge.Running.WaitOne(0);
        } finally { bridge.Running.Dispose(); }
    }
    internal static void Exercise(Dictionary<string, object> proof) {
        proof["service_bridge_stage"] = "status_reply";
        bool valid = Admits(true, false, "status", Migration);
        proof["service_migration_reply_admitted"] = valid;
        if (!valid) throw new InvalidOperationException();
        proof["service_bridge_stage"] = "context_controls";
        bool contexts = !Admits(false, false, "status", Migration) &&
            !Admits(true, true, "status", Migration);
        foreach (string operation in new[] { "initialize", "serve", "stop", "other" })
            contexts &= !Admits(true, false, operation, Migration);
        Dictionary<string, object> wrongSchema = Request("status");
        wrongSchema["schema_version"] = true;
        contexts &= !Admits(true, false, "status", Migration, false, wrongSchema);
        Dictionary<string, object> extraRequest = Request("status");
        extraRequest["extra"] = true;
        contexts &= !Admits(true, false, "status", Migration, false, extraRequest);
        Dictionary<string, object> wrongOperation = Request("status");
        wrongOperation["operation"] = 1;
        contexts &= !Admits(true, false, "status", Migration, false, wrongOperation);
        proof["service_wrong_context_cases"] = 9;
        proof["service_wrong_contexts_refused"] = contexts;
        if (!contexts) throw new InvalidOperationException();
        proof["service_bridge_stage"] = "frame_controls";
        string[] malformed = new[] {
            Migration.Replace("legacy_upgrade_required", "other"),
            "{\"schema_version\":1,\"status\":\"migration_required\"}",
            Migration.Replace("}", ",\"extra\":true}"),
            Migration.Replace("\"schema_version\":1", "\"schema_version\":true"),
            Migration.Replace("\"schema_version\":1", "\"schema_version\":\"1\""),
            Migration.Replace("\"schema_version\":1", "\"schema_version\":1.0"),
            Migration.Replace("\"schema_version\":1", "\"schema_version\":2"),
            Migration.Replace("\"schema_version\":1,", ""),
            Migration.Replace("\"status\":\"migration_required\",", ""),
            Migration.Replace("\"migration_required\"", "[]"),
            Migration.Replace("\"legacy_upgrade_required\"", "[]"),
            Migration.Replace("migration_required", "other"),
            "null", "{", new string('x', 1025)
        };
        bool frames = true;
        foreach (string frame in malformed) frames &= !Admits(true, false, "status", frame);
        proof["service_malformed_frame_cases"] = malformed.Length;
        proof["service_malformed_frames_refused"] = frames;
        if (!frames) throw new InvalidOperationException();
        proof["service_bridge_stage"] = "legacy_controls";
        bool legacy = true;
        foreach (string status in new[] { "ready", "new", "refused", "stopped" })
            legacy &= Admits(false, false, "status", Response(status));
        foreach (string status in new[] { "ready", "new", "refused" })
            legacy &= Admits(true, false, "status", Response(status));
        legacy &= Admits(false, true, "serve", Response("running"), true) &&
            Admits(true, true, "serve", Response("running"), true);
        proof["unrelated_status_cases"] = 9;
        proof["unrelated_statuses_preserved"] = legacy;
        if (!legacy) throw new InvalidOperationException();
        string[] manyProgress = Enumerable.Repeat(Progress(0), 102).ToArray();
        bool limits = ProgressPreserved() && TwoRepliesPreserved() &&
            InvalidSequence(new[] { Progress(1) }, false) &&
            InvalidSequence(new[] { Progress(10), Progress(9) }) &&
            InvalidSequence(new[] { Progress(-1) }) &&
            InvalidSequence(new[] { Progress(101) }) && InvalidSequence(manyProgress) &&
            InvalidSequence(new[] { Response("running"), Progress(1) }) &&
            InvalidSequence(new[] { Response("ready"), Response("ready"), Response("ready") });
        proof["legacy_progress_and_line_limit_cases"] = 9;
        proof["legacy_progress_and_line_limits_preserved"] = limits;
        if (!limits) throw new InvalidOperationException();
        proof["service_bridge_stage"] = "complete";
    }
}
"""


_CS = r"""
using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Linq;
using System.Reflection;
using System.Security.Principal;
using System.Threading;
using System.Web.Script.Serialization;
namespace K5VisionDesktop {
@@JSON@@
@@BRIDGE@@
internal static class LegacyProfilesProbe {
    private static HashSet<string> Current(string text) { @@VALIDATION@@ }
    private static int Existing(string text) {
        Dictionary<string, object> previous = Json.Parse(text);
        List<string> found = new List<string>();
        @@TRAVERSAL@@
        return found.Count;
    }
    private static bool Refuses(Func<string, HashSet<string>> admission, string text) {
        try { admission(text); return false; } catch (Exception) { return true; }
    }
    private static string Marker(int count) {
        List<string> values = new List<string>();
        for (int i = 0; i < count; i++) values.Add("S-1-5-21-100-200-300-" + (1000 + i));
        return Json.Serializer.Serialize(new Dictionary<string, object> {
            {"schema_version", 1}, {"profiles", values.ToArray()} });
    }
    public static int Main() {
        Dictionary<string, object> proof = new Dictionary<string, object>();
        int exit = 1;
        try {
            string valid = Marker(1);
            object array = Json.Parse(valid)["profiles"];
            proof["native_framework_v4"] = Environment.Version.Major == 4;
            proof["profiles_runtime_arraylist"] = array.GetType() == typeof(ArrayList);
            proof["profiles_runtime_object_array"] = array is object[];
            proof["profiles_runtime_ilist"] = array is IList;
            if (!(Environment.Version.Major == 4 && array is IList))
                throw new InvalidOperationException();
            string sid = "S-1-5-21-100-200-300-1000";
            string profiles = "\"profiles\":[\"" + sid + "\"]";
            bool single = Current(valid).SetEquals(new[] { sid });
            bool cap = Current(Marker(256)).Count == 256;
            bool mark = Existing(valid) == 1 && Existing(Marker(256)) == 256;
            List<string> negatives = new List<string> {
                Marker(0), Marker(257),
                "{\"schema_version\":1,\"profiles\":[\"" + sid + "\",\"" + sid + "\"]}",
                "{\"schema_version\":1,\"profiles\":[1]}",
                "{\"schema_version\":1,\"profiles\":[null]}",
                "{\"schema_version\":1,\"profiles\":[\"not-a-sid\"]}",
                "{\"schema_version\":1,\"profiles\":[\"s-1-5-21-100-200-300-1000\"]}",
                "{\"schema_version\":1,\"profiles\":\"" + sid + "\"}",
                "{\"schema_version\":1,\"profiles\":{}}",
                "{\"schema_version\":1,\"profiles\":null}",
                "{\"schema_version\":1}", "{\"profiles\":[]}",
                "{\"schema_version\":1," + profiles + ",\"extra\":true}",
                "{\"schema_version\":1.0," + profiles + "}",
                "{\"schema_version\":true," + profiles + "}",
                "{\"schema_version\":2," + profiles + "}", "{"
            };
            bool refused = true;
            foreach (string text in negatives) refused &= Refuses(Current, text);
            proof["current_valid_single_admitted"] = single;
            proof["current_valid_256_admitted"] = cap;
            proof["current_existing_marker_traversal_preserved"] = mark;
            proof["current_negative_case_count"] = negatives.Count;
            proof["current_all_negatives_refused"] = refused;
            if (!(single && cap && mark && refused)) throw new InvalidOperationException();
            BridgeCases.Exercise(proof);
            proof["status"] = "observed";
            exit = 0;
        } catch (Exception) { proof["status"] = "refused"; }
        Console.WriteLine(Json.Serializer.Serialize(proof));
        return exit;
    }
}
}
"""


def _head(root):
    return (
        subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, timeout=10, check=True
        )
        .stdout.decode("ascii")
        .strip()
    )


def _native_variant(compiler, directory, name, generated, proof):
    source, output = directory / (name + ".cs"), directory / (name + ".exe")
    source.write_text(generated, encoding="utf-8", newline="\n")
    proof["phase"] = name + "_compile"
    result = subprocess.run(
        [
            str(compiler),
            "/nologo",
            "/target:exe",
            "/platform:x64",
            "/optimize+",
            "/warn:4",
            "/warnaserror+",
            "/codepage:65001",
            "/reference:System.dll",
            "/reference:System.Core.dll",
            "/reference:System.Web.Extensions.dll",
            "/out:" + str(output),
            str(source),
        ],
        capture_output=True,
        timeout=30,
        check=False,
    )
    if result.returncode:
        raise ValueError("Native source probe compilation failed")
    proof["phase"] = name + "_execution"
    result = subprocess.run([str(output)], capture_output=True, timeout=15, check=False)
    if len(result.stdout) > 65536 or result.stderr:
        raise ValueError("Native source receipt is unavailable")
    actual = json.loads(
        result.stdout.decode("utf-8"),
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
    )
    if type(actual) is not dict or any(
        type(value) not in (bool, int, str) for value in actual.values()
    ):
        raise ValueError("Native source receipt is invalid")
    return result.returncode, actual


def main():
    proof = {"schema_version": 1, "status": "refused", "phase": "environment"}
    temporary_path = None
    status = 1
    try:
        if (
            sys.platform != "win32"
            or os.environ.get("GITHUB_ACTIONS") != "true"
            or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted"
        ):
            raise ValueError("Requires the disposable native Windows workflow")
        head = os.environ["K5_NATIVE_PROBE_HEAD_SHA"]
        if re.fullmatch("[a-f0-9]{40}", head) is None:
            raise ValueError("Missing exact native source head")
        if _head(ROOT) != head:
            raise ValueError("Native checkout and required source head differ")
        proof["head_sha"] = head
        proof["phase"] = "source_binding"
        generated, binding = materialize(ROOT)
        proof.update(binding)
        baseline = ROOT / "native-bridge-baseline"
        baseline_head = "0bc10f5aacaa45903b33d9f2dfb23c9d8d197868"
        old_path = baseline / _SOURCE_FILES[0]
        if old_path.is_symlink() or not old_path.is_file() or _head(baseline) != baseline_head:
            raise ValueError("Missing exact historical bridge source")
        old = old_path.read_text(encoding="utf-8")
        if _digest(old) != "a4d81d0094eeccdd05df705c450ab6842f3729df063407b4e1037446975e3a7a":
            raise ValueError("Historical bridge source changed")
        baseline_generated, baseline_binding = materialize(ROOT, bridge_source=old)
        proof["baseline_head_sha"] = baseline_head
        proof["baseline_bridge_source_sha256"] = baseline_binding["bridge_source_sha256"]
        proof["baseline_bridge_receive_sha256"] = baseline_binding["bridge_receive_sha256"]
        proof["baseline_bridge_context_sha256"] = baseline_binding["bridge_context_sha256"]
        proof["baseline_compiled_probe_sha256"] = baseline_binding["compiled_probe_sha256"]
        compiler = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
        with tempfile.TemporaryDirectory(
            prefix="k5-legacy-profiles-", dir=os.environ["RUNNER_TEMP"]
        ) as temporary:
            directory = Path(temporary)
            temporary_path = directory
            before_code, before = _native_variant(
                compiler, directory, "baseline", baseline_generated, proof
            )
            if (
                before_code != 1
                or before.get("status") != "refused"
                or before.get("service_bridge_stage") != "status_reply"
                or before.get("service_migration_reply_admitted") is not False
                or before.get("current_all_negatives_refused") is not True
            ):
                raise ValueError("Historical bridge did not reproduce its named runtime refusal")
            proof["old_bridge_named_runtime_refusal_reproduced"] = True
            after_code, actual = _native_variant(compiler, directory, "candidate", generated, proof)
            proof.update(actual)
            if after_code or proof.get("status") != "observed":
                raise ValueError("Actual native marker or service protocol validation failed")
        proof["phase"] = "complete"
        status = 0
    except Exception as error:
        proof["status"] = "refused"
        kind = type(error).__name__
        proof["failure_type"] = (
            kind if kind in {"ValueError", "TimeoutExpired", "OSError"} else "Other"
        )
    proof["owned_cleanup"] = temporary_path is None or not temporary_path.exists()
    if not proof["owned_cleanup"]:
        proof["status"] = "refused"
        status = 1
    (ROOT / "native-legacy-profiles.json").write_text(
        json.dumps(proof, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8"
    )
    print(json.dumps({"status": proof["status"], "phase": proof["phase"]}))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
