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


def materialize(root):
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
        "filesystem_and_acl_admission_exercised": False,
        "product_modified": False,
        "profile_or_service_written": False,
    }


_CS = r"""
using System;
using System.Collections;
using System.Collections.Generic;
using System.Security.Principal;
using System.Web.Script.Serialization;
namespace K5VisionDesktop {
@@JSON@@
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
            proof["status"] = "observed";
            exit = 0;
        } catch (Exception) { proof["status"] = "refused"; }
        Console.WriteLine(Json.Serializer.Serialize(proof));
        return exit;
    }
}
}
"""


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
        observed = (
            subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, timeout=10, check=True
            )
            .stdout.decode("ascii")
            .strip()
        )
        if observed != head:
            raise ValueError("Native checkout and required source head differ")
        proof["head_sha"] = head
        proof["phase"] = "source_binding"
        generated, binding = materialize(ROOT)
        proof.update(binding)
        compiler = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
        with tempfile.TemporaryDirectory(
            prefix="k5-legacy-profiles-", dir=os.environ["RUNNER_TEMP"]
        ) as temporary:
            directory = Path(temporary)
            temporary_path = directory
            source, output = directory / "probe.cs", directory / "probe.exe"
            source.write_text(generated, encoding="utf-8", newline="\n")
            proof["phase"] = "native_compile"
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
                timeout=60,
                check=False,
            )
            if result.returncode:
                raise ValueError("Native marker probe compilation failed")
            proof["phase"] = "native_execution"
            result = subprocess.run([str(output)], capture_output=True, timeout=15, check=False)
            if len(result.stdout) > 65536 or result.stderr:
                raise ValueError("Native marker receipt is unavailable")
            actual = json.loads(
                result.stdout.decode("utf-8"),
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
            )
            if type(actual) is not dict or any(
                type(value) not in (bool, int, str) for value in actual.values()
            ):
                raise ValueError("Native marker receipt is invalid")
            proof.update(actual)
            if result.returncode or proof.get("status") != "observed":
                raise ValueError("Actual native marker validation failed")
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
