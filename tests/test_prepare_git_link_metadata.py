"""Pure source/model contracts, not a PowerShell parser or Windows execution proof.

No PowerShell, C#, CLI, Git, network, sockets, compilation, or runtime setup.
The approved hosted job separately establishes actual PS5.1 parsing/compilation.
"""

import base64
import hashlib
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEXT = (ROOT / "scripts/prepare-git-link-metadata.ps1").read_text(encoding="utf-8")
COMMIT = "4d6e7bee294cd972c015961959db08baea371d58"
BRANCH = "review/git-link-metadata-20261008"
REF = "refs/heads/" + BRANCH
REPOSITORY = "mkurtgerald/K5-Vision"
HEAD = "a" * 40
PINS = {
    "assert-stage-one-physical-admission.ps1": (
        7643, "9faae324ffaf008a7dc389aaab2d70198c5f4ea1",
        "d7a38b5278802d9ba768d9987b4582a219d490923b0cc4da0c297d29a250b45d",
    ),
    "observe_installed_git_links.cs": (
        34207, "1e5bc986d255210f5cd82770bb472027c590f8bb",
        "eeb76812034a45604fbe43171cecbffcc413af8c31fca611e0f2a75bf202059a",
    ),
    "observe-installed-git-links.ps1": (
        20889, "cb919a3bdf6eb7dc85cab9ebc0420603b972f1c8",
        "dcea700b6055401dafbbb0e7895b36dc1b7b750b125f282f1f8c5020092ab7e9",
    ),
}


def section(start, end):
    return TEXT.split(start, 1)[1].split(end, 1)[0]


def source_model(data, name):
    """Model the transport's exact identity, encoding, size and hash admission."""
    size, blob, digest = PINS[name]
    if (type(data) is not dict or data.get("type") != "file"
            or data.get("name") != name or data.get("path") != "scripts/" + name
            or data.get("sha") != blob or type(data.get("size")) is not int
            or data["size"] != size or data.get("encoding") != "base64"
            or type(data.get("content")) is not str or len(data["content"]) > 65536):
        raise ValueError("source_metadata")
    encoded = data["content"].replace("\r", "").replace("\n", "")
    if not re.fullmatch(r"(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?", encoded):
        raise ValueError("source_encoding")
    raw = base64.b64decode(encoded, validate=True)
    if base64.b64encode(raw).decode("ascii") != encoded:
        raise ValueError("source_encoding")
    if len(raw) != size:
        raise ValueError("source_size")
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("source_hash")
    if hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest() != blob:
        raise ValueError("source_blob")
    raw.decode("utf-8", errors="strict")
    return raw


def source_response(name):
    raw = (ROOT / "scripts" / name).read_bytes()
    return {"type": "file", "name": name, "path": "scripts/" + name,
            "sha": PINS[name][1], "size": len(raw), "encoding": "base64",
            "content": base64.encodebytes(raw).decode("ascii")}


def context_model(context):
    """Small exact-head gate model; it cannot establish Actions trust."""
    required = {
        "os": "Windows_NT", "repository": REPOSITORY, "owner": "mkurtgerald",
        "event_name": "push", "ref": REF, "actor": "mkurtgerald",
        "triggering_actor": "mkurtgerald", "attempt": "1",
        "event_repository": REPOSITORY, "event_owner": "mkurtgerald",
        "event_sender": "mkurtgerald", "event_ref": REF, "live_ref": REF,
        "live_type": "commit",
    }
    if any(context.get(k) != v for k, v in required.items()):
        raise ValueError("context")
    if (not isinstance(context.get("sha"), str)
            or not re.fullmatch("[0-9a-f]{40}", context["sha"])
            or not isinstance(context.get("run"), str)
            or not re.fullmatch("[1-9][0-9]{0,19}", context["run"])):
        raise ValueError("context")
    for key in ("diagnostic_sha", "event_after", "event_head", "live_sha", "commit_sha"):
        if context.get(key) != context["sha"]:
            raise ValueError("head")
    if context.get("parents") != [COMMIT]:
        raise ValueError("parent")
    if context.get("deleted") is not False or context.get("forced") is not False:
        raise ValueError("event")
    mode = context.get("mode")
    if mode in ("HostedQualify", "HostedPost"):
        if context.get("job") != "hosted_qualify" or context.get("runner_environment") != "github-hosted":
            raise ValueError("hosted")
    elif mode in ("PhysicalObserve", "PhysicalPost"):
        if (context.get("job") != "physical_observe" or context.get("runner_environment") != "self-hosted"
                or context.get("runner") != "K5-Physical" or context.get("host") != "VLR-CYZ4PK3"):
            raise ValueError("physical")
    else:
        raise ValueError("mode")


def valid_context(mode="HostedQualify"):
    physical = mode.startswith("Physical")
    return {
        "os": "Windows_NT", "repository": REPOSITORY, "owner": "mkurtgerald",
        "event_name": "push", "ref": REF, "actor": "mkurtgerald", "triggering_actor": "mkurtgerald",
        "attempt": "1", "run": "37123456", "sha": HEAD, "diagnostic_sha": HEAD,
        "event_repository": REPOSITORY, "event_owner": "mkurtgerald", "event_sender": "mkurtgerald",
        "event_ref": REF, "event_after": HEAD, "event_head": HEAD, "deleted": False, "forced": False,
        "live_ref": REF, "live_sha": HEAD, "live_type": "commit", "commit_sha": HEAD, "parents": [COMMIT],
        "mode": mode, "job": "physical_observe" if physical else "hosted_qualify",
        "runner_environment": "self-hosted" if physical else "github-hosted",
        "runner": "K5-Physical" if physical else "hosted", "host": "VLR-CYZ4PK3" if physical else "hosted",
    }


def test_only_mode_parameter_no_mutable_script_or_source_parameters():
    declaration = TEXT.split("$ErrorActionPreference", 1)[0]
    assert "[ValidateSet('HostedQualify', 'HostedPost', 'PhysicalObserve', 'PhysicalPost')]" in declaration
    assert re.findall(r"\[string\]\$(\w+)", declaration) == ["Mode"]
    assert "$sourceCommit = '" + COMMIT + "'" in TEXT
    assert "$branch = '" + BRANCH + "'" in TEXT
    assert "$apiRoot = 'https://api.github.com/repos/mkurtgerald/K5-Vision/'" in TEXT


@pytest.mark.parametrize("name", PINS)
def test_exact_source_bytes_and_pins(name):
    raw = source_model(source_response(name), name)
    size, blob, digest = PINS[name]
    assert f"name = '{name}'; size = {size}; blob = '{blob}'; sha256 = '{digest}'" in TEXT
    assert len(raw) == size


@pytest.mark.parametrize("path,digest", [
    ("tests/test_installed_git_link_csharp_contract.py", "8a9909ffa73db4be2203743ba3bf86df7c4bc40c2dd49513d977f2edbe530057"),
    ("tests/test_observe_installed_git_links_wrapper.py", "0e9d9dfcce4079f9d63ad42c9bb0b19961f821b8802c8009cee781fc3c9ce44a"),
])
def test_frozen_candidate_tests_remain_unchanged(path, digest):
    assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == digest


@pytest.mark.parametrize("key,value", [
    ("type", "symlink"), ("type", "submodule"), ("name", "other.ps1"),
    ("path", "scripts/../guard.ps1"), ("sha", "0" * 40), ("size", True),
    ("size", "7643"), ("size", 7644), ("encoding", "utf-8"), ("content", []),
    ("content", "A" * 65537), ("content", "AAAA AA=="), ("content", "AAAA\tAA=="),
    ("content", "AAAA-AAA"), ("content", ""), ("content", "Zh=="),
])
def test_source_metadata_and_encoding_refuse(key, value):
    name = "assert-stage-one-physical-admission.ps1"
    data = source_response(name)
    data[key] = value
    with pytest.raises(ValueError):
        source_model(data, name)


@pytest.mark.parametrize("name", PINS)
def test_tampered_exact_size_source_refuses(name):
    data = source_response(name)
    raw = bytearray((ROOT / "scripts" / name).read_bytes())
    raw[len(raw) // 2] ^= 1
    data["content"] = base64.b64encode(raw).decode("ascii")
    with pytest.raises(ValueError, match="source_hash"):
        source_model(data, name)


def test_fixed_api_allowlist_bounded_transport_and_no_redirects():
    api = section("function Read-FixedApi", "function Assert-SourceBytes")
    for check in (
        "$Endpoint -cnotin $allowed", "$request.Method = 'GET'", "$request.AllowAutoRedirect = $false",
        "$request.Timeout = 15000", "$request.ReadWriteTimeout = 15000", "$request.MaximumResponseHeadersLength = 16",
        "$request.AutomaticDecompression = [Net.DecompressionMethods]::None", "$response.ResponseUri.AbsoluteUri -cne $uri",
        "$response.ContentLength -gt 131072", "Read-BoundedStream $stream 131072", "$response.ContentEncoding",
        "$response.ContentType -cnotmatch", "[int]$response.StatusCode -ne 200", "$response.Close()", "$stream.Dispose()",
    ):
        assert check in api
    assert "contents/scripts/" in api and "'?ref=' + $sourceCommit" in api
    assert "git/commits/" in api
    assert "download_url" not in TEXT and "raw.githubusercontent.com" not in TEXT
    assert "Invoke-RestMethod" not in TEXT and "Invoke-WebRequest" not in TEXT
    assert TEXT.count("GetResponse()") == 1
    bounded = section("function Read-BoundedStream", "function Read-FixedApi")
    assert "$remaining = $Limit - [int]$memory.Length" in bounded
    assert "$remaining -le 0" in bounded
    assert "$Stream.Read($buffer, 0, [Math]::Min($buffer.Length, $remaining))" in bounded
    assert bounded.index("$remaining -le 0") < bounded.index("$Stream.Read(") < bounded.index("$memory.Write(")
    assert "[Text.UTF8Encoding]::new($false, $true)" in TEXT


@pytest.mark.parametrize("mode", ["HostedQualify", "HostedPost", "PhysicalObserve", "PhysicalPost"])
def test_valid_model_context(mode):
    context_model(valid_context(mode))


@pytest.mark.parametrize("key,value", [
    ("os", "Linux"), ("repository", "mkurtgerald/K5-Lab"), ("owner", "someone"),
    ("event_name", "workflow_dispatch"), ("ref", "refs/heads/main"), ("actor", "bot"),
    ("triggering_actor", "bot"), ("attempt", "2"), ("attempt", "01"), ("attempt", 1),
    ("sha", "A" * 40), ("sha", "a" * 41), ("run", "0"), ("run", "1" * 21),
    ("diagnostic_sha", "b" * 40), ("event_after", "b" * 40), ("event_head", "b" * 40),
    ("live_sha", "b" * 40), ("commit_sha", "b" * 40), ("live_type", "tag"),
    ("parents", []), ("parents", [COMMIT, COMMIT]), ("parents", ["b" * 40]),
    ("deleted", True), ("deleted", "false"), ("deleted", 0), ("forced", True),
    ("job", "physical_observe"), ("runner_environment", "self-hosted"),
    ("mode", "HostedObserve"), ("mode", "hostedqualify"), ("event_sender", "other"),
    ("event_owner", "other"), ("event_repository", "other"), ("event_ref", "refs/heads/main"),
])
def test_context_model_refusals(key, value):
    context = valid_context()
    context[key] = value
    with pytest.raises(ValueError):
        context_model(context)


@pytest.mark.parametrize("key,value", [
    ("job", "hosted_qualify"), ("runner_environment", "github-hosted"),
    ("runner", "K5-Physical-2"), ("host", "another-host"),
])
def test_physical_identity_refusals(key, value):
    context = valid_context("PhysicalPost")
    context[key] = value
    with pytest.raises(ValueError):
        context_model(context)


def test_context_source_binds_event_ref_single_parent_and_attempt():
    context = section("function Assert-Context", "function Assert-InboxCompiler")
    for check in (
        "$env:GITHUB_RUN_ATTEMPT -cne '1'", "$env:GITHUB_EVENT_NAME -cne 'push'",
        "$env:GITHUB_ACTOR -cne 'mkurtgerald'", "$env:GITHUB_TRIGGERING_ACTOR -cne 'mkurtgerald'",
        "$event.head_commit.id -cne $env:GITHUB_SHA", "$event.after -cne $env:GITHUB_SHA",
        "$ref.object.sha -cne $env:GITHUB_SHA", "$commit.sha -cne $env:GITHUB_SHA",
        "$commit.parents.Count -ne 1", "$commit.parents[0].sha -cne $sourceCommit",
        "Read-BoundedStream $eventFile 1048576", "$eventFile.Dispose()",
        "$env:GITHUB_JOB -cne 'hosted_qualify'", "$env:GITHUB_JOB -cne 'physical_observe'",
        "$env:RUNNER_NAME -cne 'K5-Physical'", "[Environment]::MachineName -cne 'VLR-CYZ4PK3'",
    ):
        assert check in context
    dispatch = TEXT.split("$failure = 'context_binding'", 1)[1]
    assert dispatch.index("Assert-Context") < dispatch.index("switch -CaseSensitive ($Mode)")


def test_new_bundle_has_only_fixed_private_path_and_create_new_sources():
    bundle = section("function Get-BundleRoot", "function Assert-CompilerArtifacts")
    assert "'k5-git-link-observation-' + $env:GITHUB_RUN_ID + '-' + $env:GITHUB_RUN_ATTEMPT" in bundle
    assert "Assert-OrdinaryPath $env:RUNNER_TEMP $true" in bundle
    assert "[IO.FileMode]::CreateNew" in bundle
    assert "[IO.FileShare]::None" in bundle
    assert bundle.index("Get-PinnedSource $pin") < bundle.index("[IO.File]::Open(")
    assert "$sources[$pin.name] = $bytes" in bundle
    create = section("function New-PrivateDirectory", "function Get-BundleRoot")
    assert "Test-Path -LiteralPath $Path" in create
    assert "New-Item -Path $Path -ItemType Directory -ErrorAction Stop" in create
    assert "-Force" not in create
    path = section("function Assert-OrdinaryPath", "function Read-BoundedStream")
    for check in ("[IO.FileAttributes]::ReparsePoint", "$item.LinkType", "$count -gt 32", "GetFullPath($Path) -cne $Path"):
        assert check in path


def test_hosted_qualification_parses_and_compiles_without_executing_metadata():
    hosted = section("function Invoke-HostedQualification", "function Invoke-PhysicalPost")
    assert "[Management.Automation.Language.Parser]::ParseInput($scriptText" in hosted
    assert "foreach ($name in @('observe-installed-git-links.ps1', 'assert-stage-one-physical-admission.ps1'))" in hosted
    assert "$errors.Count -ne 0" in hosted
    assert "Add-Type -TypeDefinition $source -Language CSharp -PassThru" in hosted
    assert hosted.count("Assert-CompilerIdle") == 2
    assert hosted.index("Assert-CompilerIdle") < hosted.index("New-PinnedBundle")
    assert hosted.index("ParseInput") < hosted.index("Add-Type") < hosted.rindex("Assert-CompilerIdle")
    for forbidden in ("::Observe", "::ObserveGuarded", "[scriptblock]::Create", "& $guard", "& $wrapper", "Invoke-Expression"):
        assert forbidden not in hosted
    assert "'K5FixedGitObservation'" in hosted
    assert "finally {" in hosted
    for name in ("TEMP", "TMP"):
        assert f"SetEnvironmentVariable('{name}', $compilerTemp, 'Process')" in hosted
    assert "SetEnvironmentVariable('TEMP', $previousTemp, 'Process')" in hosted
    assert "SetEnvironmentVariable('TMP', $previousTmp, 'Process')" in hosted
    runtime = section("function Assert-InboxCompiler", "function Assert-CompilerIdle")
    for check in ("PSEdition -cne 'Desktop'", "PSVersion.Major -ne 5", "PSVersion.Minor -ne 1",
                  "C:\\Windows\\Microsoft.NET\\Framework64\\v4.0.30319", "Is64BitProcess", "'type_reuse'"):
        assert check in runtime


def test_system_compiler_has_no_new_single_link_or_hardlink_attestation():
    runtime = section("function Assert-InboxCompiler", "function Assert-CompilerIdle")
    assert "Assert-OrdinaryPath $runtime $true" in runtime
    assert "$compilerPath = Join-Path $runtime 'csc.exe'" in runtime
    assert "$compiler.FullName -cne $compilerPath -or $compiler.PSIsContainer" in runtime
    assert "$compiler.Attributes -band [IO.FileAttributes]::ReparsePoint" in runtime
    assert "Assert-OrdinaryPath $compilerPath" not in runtime
    assert "Assert-OrdinaryPath (Join-Path" not in runtime
    assert "LinkType" not in runtime


@pytest.mark.parametrize("size,limit,accepted", [(0, 12, False), (11, 12, True), (12, 12, False), (13, 12, False), (4097, 5000, True), (5001, 5000, False)])
def test_read_bound_model_never_requests_beyond_remaining_budget(size, limit, accepted):
    data = b"x" * size
    consumed = 0
    requests = []
    result = bytearray()
    passed = False
    try:
        while True:
            remaining = limit - len(result)
            if remaining <= 0:
                raise ValueError("response_size")
            requested = min(4096, remaining)
            requests.append(requested)
            chunk = data[consumed:consumed + requested]
            consumed += len(chunk)
            if not chunk:
                break
            result.extend(chunk)
        if not result:
            raise ValueError("response_empty")
        passed = True
    except ValueError:
        pass
    assert passed is accepted
    assert consumed <= limit
    assert all(0 < amount <= 4096 for amount in requests)


def test_physical_wrapper_execution_uses_exact_verified_memory_bytes():
    physical = section("        'PhysicalObserve' {", "        'PhysicalPost' {")
    assert "$bundle = New-PinnedBundle" in physical
    assert "$wrapperText = $utf8.GetString($bundle.sources['observe-installed-git-links.ps1'])" in physical
    assert "& ([scriptblock]::Create($wrapperText)) -BundleScripts $bundle.scripts" in physical
    for forbidden in ("Get-Content", "ReadAllText", "& $path", "& $wrapperPath", "Add-Type"):
        assert forbidden not in physical


def test_post_is_fresh_guard_and_idle_without_recompilation_or_prior_bundle_dependence():
    post = section("function Invoke-PhysicalPost", "$failure = 'context_binding'")
    assert "$guardBytes = Get-PinnedSource $pins[0]" in post
    assert "[scriptblock]::Create($utf8.GetString($guardBytes))" in post
    assert "& $guard > $null" in post
    assert "finally {" in post and "Assert-CompilerIdle" in post.split("finally {", 1)[1]
    for forbidden in ("Add-Type", "New-PinnedBundle", "New-PrivateDirectory", "::Observe", "wrapperText"):
        assert forbidden not in post
    hosted = section("        'HostedPost' {", "        'PhysicalObserve' {")
    assert "Assert-CompilerIdle" in hosted and "Assert-RetainedArtifacts" in hosted
    assert "Get-PinnedSource" not in hosted and "Invoke-HostedQualification" not in hosted
    artifacts = section("function Assert-RetainedArtifacts", "function Invoke-HostedQualification")
    assert "Test-Path -LiteralPath $root" in artifacts
    assert "Test-Path -LiteralPath $compilerTemp" in artifacts
    assert "New-" not in artifacts


def test_compiler_inventory_artifacts_and_logs_are_bounded_and_non_destructive():
    idle = section("function Assert-CompilerIdle", "function New-PrivateDirectory")
    assert "-Property Name -OperationTimeoutSec 10" in idle
    assert "$rows.Count -gt 32768" in idle
    assert "@('csc.exe', 'cvtres.exe', 'vbc.exe', 'VBCSCompiler.exe')" in idle
    artifacts = section("function Assert-CompilerArtifacts", "function Assert-RetainedArtifacts")
    for check in ("$count -gt 64", "$bytes -gt 16777216", "$entries.Dispose()", "Assert-OrdinaryPath $entry $false"):
        assert check in artifacts
    forbidden = ("Remove-Item", "Stop-Process", "Start-Process", "Invoke-Expression", "Process]::Start",
                 "Set-Acl", "icacls", "takeown", "git.exe", "setup-python", "pip install", "-Recurse",
                 "WriteAllText", "WriteAllBytes", "FileMode]::Create,", "Get-ChildItem Env:")
    for token in forbidden:
        assert token not in TEXT
    assert TEXT.count("Write-Host") == 1
    assert "throw 'Fixed Git metadata preparation refused. Existing owners were preserved.'" in TEXT
    dispatch = TEXT.split("$failure = 'context_binding'", 1)[1]
    assert "$failureRecord = $_" in dispatch
    record = dispatch.split("$record = [ordered]@{", 1)[1]
    for private in ("$failureRecord", "TargetObject", "Exception", "FullyQualifiedErrorId"):
        assert private not in record
    assert "hard compiler disk quota" in TEXT


def test_source_checks_explicitly_do_not_claim_windows_or_native_proof():
    assert "not a PowerShell parser or Windows execution proof" in __doc__
    assert "No PowerShell, C#, CLI, Git, network, sockets, compilation, or runtime setup" in __doc__


HOSTED_PHASES = {
    "compiler_idle_pre", "bundle_creation", "source_binding", "wrapper_parse", "guard_parse",
    "compiler_temp", "compile", "compiled_type", "compiler_environment", "compiler_idle_post",
    "compiler_artifacts", "unknown",
}
HOSTED_REASONS = {
    "path_lexical", "path_canonical", "path_component", "path_fullname", "path_type",
    "path_depth", "path_reparse", "path_link", "compiler_inventory", "compiler_occupied", "exists", "directory",
    "response_size", "response_empty", "api_request", "api_response", "source_metadata",
    "source_encoding", "source_size", "source_hash", "source_blob", "source_parse",
    "compiled_type", "compiler_artifacts", "compiler_environment",
}
ADD_TYPE_IDS = {
    "SOURCE_CODE_ERROR,Microsoft.PowerShell.Commands.AddTypeCommand",
    "COMPILER_ERRORS,Microsoft.PowerShell.Commands.AddTypeCommand",
}


def hosted_projection_model(phase, message, error_id="", compiler_type=False, error_number=None):
    """Closed projection model, never a PowerShell ErrorRecord/runtime simulation."""
    reason = message if type(message) is str and message in HOSTED_REASONS else "unknown"
    compiler_code = "unknown"
    if phase == "compile" and error_id in ADD_TYPE_IDS:
        reason = "compiler_error"
        if (compiler_type is True and type(error_number) is str
                and re.fullmatch(r"CS[0-9]{4}", error_number)):
            compiler_code = error_number
    return {"phase": phase if phase in HOSTED_PHASES else "unknown",
            "reason": reason, "compiler_code": compiler_code}


def test_repair_inbox_core_reference_is_fixed_checked_and_explicit():
    runtime = section("function Assert-InboxCompiler", "function Assert-CompilerIdle")
    hosted = section("function Invoke-HostedQualification", "function Invoke-PhysicalPost")
    assert "$runtime -cne 'C:\\Windows\\Microsoft.NET\\Framework64\\v4.0.30319'" in runtime
    assert "$script:systemCorePath = Join-Path $runtime 'System.Core.dll'" in runtime
    assert "$systemCore = Get-Item -LiteralPath $script:systemCorePath -Force -ErrorAction Stop" in runtime
    assert "$systemCore.FullName -cne $script:systemCorePath -or $systemCore.PSIsContainer" in runtime
    assert "$systemCore.Attributes -band [IO.FileAttributes]::ReparsePoint" in runtime
    assert "-ReferencedAssemblies @($script:systemCorePath)" in hosted
    assert TEXT.count("-ReferencedAssemblies") == 1
    for forbidden in ("LinkType", "HardLink", "Assert-OrdinaryPath $script:systemCorePath",
                      "Get-ChildItem", "Assembly]::Load", "GAC", "Install", "Download"):
        assert forbidden not in runtime


def test_repair_hosted_phase_precedes_each_fixed_substage():
    hosted = section("function Invoke-HostedQualification", "function Invoke-PhysicalPost")
    for phase, operation in (
        ("compiler_idle_pre", "Assert-CompilerIdle"),
        ("compiler_temp", "$compilerTemp = Join-Path"),
        ("compile", "$types = @(Add-Type"),
        ("compiled_type", "if (@($types | Where-Object"),
        ("compiler_idle_post", "Assert-CompilerIdle\n    $script:qualificationPhase = 'compiler_artifacts'"),
        ("compiler_artifacts", "Assert-CompilerArtifacts $compilerTemp"),
    ):
        assert hosted.index(f"$script:qualificationPhase = '{phase}'") < hosted.index(operation)
    assert "if ($name -ceq 'observe-installed-git-links.ps1') { 'wrapper_parse' } else { 'guard_parse' }" in hosted
    assert hosted.index("'wrapper_parse'") < hosted.index("ParseInput")
    bundle = section("function New-PinnedBundle", "function Assert-CompilerArtifacts")
    for phase, operation in (("bundle_creation", "$root = Get-BundleRoot"),
                             ("source_binding", "$bytes = Get-PinnedSource $pin")):
        assert bundle.index(f"$script:qualificationPhase = '{phase}'") < bundle.index(operation)
    assert bundle.count("if ($Mode -ceq 'HostedQualify')") == 2
    phases = set(re.findall(r"\$script:qualificationPhase = '([^']+)'", TEXT))
    assert phases | {"wrapper_parse", "guard_parse"} == HOSTED_PHASES


def test_repair_hosted_projection_is_closed_and_never_serializes_private_error():
    projection = section("function Get-HostedFailureProjection", "function Invoke-HostedQualification")
    pairs = re.findall(r"'([^']+)' \{ \$reason = '([^']+)' \}", projection)
    assert dict(pairs) == {reason: reason for reason in HOSTED_REASONS}
    assert "switch -CaseSensitive ($FailureRecord.Exception.Message)" in projection
    assert "$script:qualificationPhase -ceq 'compile'" in projection
    for error_id in ADD_TYPE_IDS:
        assert f"'{error_id}'" in projection
    assert "$FailureRecord.FullyQualifiedErrorId -cin @(" in projection
    assert "$FailureRecord.TargetObject -is [System.CodeDom.Compiler.CompilerError]" in projection
    assert "$FailureRecord.TargetObject.ErrorNumber -cmatch '\\ACS[0-9]{4}\\z'" in projection
    assert "$reason = 'unknown'" in projection and "$compilerCode = 'unknown'" in projection
    for forbidden in ("ErrorText", "ToString", "Write-Host", "Write-Output", "ConvertTo-Json",
                      "-match 'CS", ".Split(", ".Substring(", "$reason = $FailureRecord"):
        assert forbidden not in projection
    dispatch = TEXT.split("$failure = 'context_binding'", 1)[1]
    assert "$failureRecord = $_" in dispatch
    assert "$Mode -ceq 'HostedQualify' -and $failure -ceq 'hosted_qualification'" in dispatch
    assert "$projection = Get-HostedFailureProjection $failureRecord" in dispatch
    record = dispatch.split("$record = [ordered]@{", 1)[1]
    assert "phase = " in record and "reason = " in record and "compiler_code = " in record
    assert "$failureRecord" not in record and "TargetObject" not in record


def test_repair_original_error_survives_both_environment_restore_failures():
    hosted = section("function Invoke-HostedQualification", "function Invoke-PhysicalPost")
    assert "$compileFailure = $null" in hosted
    assert "$restoreError = $null" in hosted
    assert "$compileFailure = $_\n        throw" in hosted
    restore = hosted.split("} finally {", 1)[1]
    condition = "if ($null -ne $restoreError -and $null -eq $compileFailure)"
    assert condition in restore
    refusal = "throw [Management.Automation.RuntimeException]::new('compiler_environment', $restoreError.Exception)"
    assert refusal in restore and restore.index(condition) < restore.index(refusal)
    assert restore.count("catch { if ($null -eq $restoreError) { $restoreError = $_ } }") == 2
    assert "$compileFailure =" not in restore


@pytest.mark.parametrize("reason", sorted(HOSTED_REASONS))
def test_repair_known_validation_reason_model(reason):
    assert hosted_projection_model("source_binding", reason) == {
        "phase": "source_binding", "reason": reason, "compiler_code": "unknown"}


@pytest.mark.parametrize("message", [
    "SOURCE_CODE_ERROR", "source_parse token-secret", "SOURCE_PARSE", "source_parse\n",
    "CS0246 C:\\private\\source.cs Bearer secret", "api_response body-secret", "", None,
])
def test_repair_unknown_error_text_model_never_leaks(message):
    assert hosted_projection_model("wrapper_parse", message) == {
        "phase": "wrapper_parse", "reason": "unknown", "compiler_code": "unknown"}


@pytest.mark.parametrize("error_id", sorted(ADD_TYPE_IDS))
@pytest.mark.parametrize("typed,number,expected", [
    (True, "CS0246", "CS0246"), (True, "CS0000", "CS0000"), (False, "CS0246", "unknown"),
    (True, "cs0246", "unknown"), (True, "CS0246\n", "unknown"), (True, "CS02460", "unknown"),
    (True, "CS246", "unknown"), (True, "CS0246 secret", "unknown"), (True, None, "unknown"),
])
def test_repair_compiler_code_requires_known_id_typed_target_and_exact_number(error_id, typed, number, expected):
    result = hosted_projection_model("compile", "private compiler text", error_id, typed, number)
    assert result == {"phase": "compile", "reason": "compiler_error", "compiler_code": expected}


@pytest.mark.parametrize("phase,error_id", [
    ("wrapper_parse", "SOURCE_CODE_ERROR,Microsoft.PowerShell.Commands.AddTypeCommand"),
    ("compile", "SOURCE_CODE_ERROR"), ("compile", "COMPILER_ERRORS"),
    ("compile", "SOURCE_CODE_ERROR,OtherCommand"),
    ("compile", "source_code_error,Microsoft.PowerShell.Commands.AddTypeCommand"),
    ("compile", "SOURCE_CODE_ERROR,Microsoft.PowerShell.Commands.AddTypeCommand\n"),
])
def test_repair_unrecognized_compiler_record_model_is_unknown(phase, error_id):
    assert hosted_projection_model(phase, "private text", error_id, True, "CS0246") == {
        "phase": phase, "reason": "unknown", "compiler_code": "unknown"}


@pytest.mark.parametrize("primary,temp_failed,tmp_failed", [
    (object(), True, True), (object(), True, False), (object(), False, True),
    (object(), False, False), (None, True, True), (None, False, False),
])
def test_repair_restore_precedence_model(primary, temp_failed, tmp_failed):
    original = primary
    temp_error, tmp_error = object(), object()
    restore_errors = [temp_error if temp_failed else None, tmp_error if tmp_failed else None]
    restore_error = None
    attempted = []
    for error in restore_errors:
        attempted.append(error)
        if restore_error is None and error is not None:
            restore_error = error
    result = primary
    if restore_error is not None and primary is None:
        result = ("compiler_environment", restore_error)
    assert attempted == restore_errors
    assert restore_error is (temp_error if temp_failed else tmp_error if tmp_failed else None)
    if original is not None:
        assert result is original
    else:
        assert result == (("compiler_environment", restore_error) if restore_error is not None else None)


PATH_REASONS = {
    "path_lexical", "path_canonical", "path_component", "path_fullname", "path_type",
    "path_depth", "path_reparse", "path_link",
}


def artifact_observation_model(entries, root_failure=None):
    """Shallow, first-refusal model; counts describe checks reached, not a full inventory."""
    observation = {
        "state": "partial_at_refusal", "scope": "root", "observed_type": "unknown",
        "entries_seen": 0, "files_seen": 0, "directories_seen": 0,
        "reparse_seen": 0, "links_seen": 0, "invalid_seen": 0,
    }
    if root_failure:
        return root_failure, observation
    total_bytes = 0
    for entry in entries:
        observation["scope"] = "entry"
        observation["observed_type"] = "unknown"
        observation["entries_seen"] += 1
        if observation["entries_seen"] > 64:
            return "compiler_artifacts", observation
        failure = entry.get("failure")
        if failure in {"path_lexical", "path_canonical", "path_component", "path_fullname", "unknown"}:
            observation["invalid_seen"] += 1
            return failure, observation
        is_directory = entry.get("directory", False)
        observation["observed_type"] = "directory" if is_directory else "file"
        observation["directories_seen" if is_directory else "files_seen"] += 1
        if is_directory:
            failure = "path_type"
        elif entry.get("reparse"):
            observation["reparse_seen"] += 1
            failure = "path_reparse"
        elif entry.get("link"):
            observation["links_seen"] += 1
            failure = "path_link"
        elif failure not in {"path_depth", "path_reparse", "path_link"}:
            failure = None
        if failure:
            observation["invalid_seen"] += 1
            return failure, observation
        size = entry.get("size", 0)
        total_bytes += size
        if size < 0 or total_bytes > 16777216:
            observation["invalid_seen"] += 1
            return "compiler_artifacts", observation
    return None, observation


def test_artifact_projection_path_categories_keep_all_gates_and_order():
    path = section("function Assert-OrdinaryPath", "function Read-BoundedStream")
    assert set(re.findall(r"throw '(path_[a-z]+)'", path)) == PATH_REASONS
    assert "throw 'path'" not in path
    ordered = [
        "throw 'path_lexical'", "GetFullPath($Path) -cne $Path", "throw 'path_canonical'",
        "throw 'path_component'", "Get-Item -LiteralPath $Path -Force -ErrorAction Stop",
        "$item.FullName -cne $Path", "throw 'path_fullname'", "[bool]$item.PSIsContainer",
        "throw 'path_type'", "$count -gt 32", "throw 'path_depth'",
        "$item.Attributes -band [IO.FileAttributes]::ReparsePoint", "throw 'path_reparse'",
        "$item.LinkType", "throw 'path_link'",
    ]
    assert [path.index(token) for token in ordered] == sorted(path.index(token) for token in ordered)
    assert path.count("Get-Item") == 1
    for gate in ("$Path.Length -gt 1024", "$Path.Substring(2).Contains(':')",
                 "$part.Length -eq 0", "$part -in @('.', '..')", "$part.EndsWith('.')",
                 "$part.EndsWith(' ')", "$item.Directory", "$item.Parent"):
        assert gate in path
    # An unexpected metadata/canonicalization exception keeps its original ErrorRecord.
    assert "catch" not in path


def test_artifact_projection_stays_shallow_and_stops_at_first_refused_entry():
    artifacts = section("function Assert-CompilerArtifacts", "function Assert-RetainedArtifacts")
    path = section("function Assert-OrdinaryPath", "function Read-BoundedStream")
    assert "Assert-OrdinaryPath $Path $true $observation" in artifacts
    assert "Assert-OrdinaryPath $entry $false $observation" in artifacts
    assert "$script:compilerArtifactObservation = $observation" in artifacts
    assert "state = 'partial_at_refusal'" in artifacts
    for field in ("entries_seen", "files_seen", "directories_seen", "reparse_seen", "links_seen", "invalid_seen"):
        assert f"{field} = 0" in artifacts
    assert "scope = 'root'; observed_type = 'unknown'" in artifacts
    assert "$observation.scope = 'entry'" in artifacts
    assert "$observation.observed_type = 'unknown'" in artifacts
    assert artifacts.index("$count++") < artifacts.index("$observation.entries_seen = $count")
    assert artifacts.index("$count -gt 64") < artifacts.index("$entry = [string]$entries.Current")
    assert "catch { $observation.invalid_seen++; throw }" in artifacts
    assert artifacts.count("MoveNext()") == 1 and artifacts.count("Get-Item") == 1
    assert artifacts.count("EnumerateFileSystemEntries") == 1
    assert "SearchOption" not in artifacts and "Recurse" not in artifacts
    assert path.count("$Observation.scope -ceq 'entry'") == 3
    assert path.count("$count -eq 1") == 2
    # The existing second metadata read, length bound, and enumerator disposal survive.
    assert "$file = Get-Item -LiteralPath $entry -Force -ErrorAction Stop" in artifacts
    assert "$file.Length -lt 0 -or $bytes -gt 16777216" in artifacts
    assert "finally { $entries.Dispose() }" in artifacts


def test_artifact_projection_output_is_hosted_failure_only_and_has_no_private_values():
    projection = section("function Get-HostedFailureProjection", "function Invoke-HostedQualification")
    assert "$script:qualificationPhase -ceq 'compiler_artifacts'" in projection
    assert "compiler_artifact_observation = $artifactObservation" in projection
    assert "$artifactObservation = $null" in projection
    dispatch = TEXT.split("$failure = 'context_binding'", 1)[1]
    assert "$script:compilerArtifactObservation = $null" in dispatch
    assert "compiler_artifact_observation = $null" in dispatch
    assert "compiler_artifact_observation = $(if ($passed) { $null } else { $projection.compiler_artifact_observation })" in dispatch
    record = dispatch.split("$record = [ordered]@{", 1)[1]
    for private in ("$Path", "$entry", "$file", "$item", "$failureRecord", "Exception", "TargetObject", "FullyQualifiedErrorId"):
        assert private not in record
    observation_source = section("function Assert-CompilerArtifacts", "function Assert-RetainedArtifacts")
    for forbidden in (".FullName", ".Name", "Write-Host", "Write-Output", "ConvertTo-Json"):
        assert forbidden not in observation_source


def test_artifact_projection_ordinary_child_directory_remains_refused_not_admitted():
    def entries():
        yield {"size": 2}
        yield {"directory": True}
        raise AssertionError("first directory refusal must stop further enumeration")

    reason, observation = artifact_observation_model(entries())
    assert reason == "path_type"
    assert observation == {
        "state": "partial_at_refusal", "scope": "entry", "observed_type": "directory",
        "entries_seen": 2, "files_seen": 1, "directories_seen": 1,
        "reparse_seen": 0, "links_seen": 0, "invalid_seen": 1,
    }
    # A high-integrity CodeDOM child is one possible directory, never evidence of this run's exact entry.
    assert "Assert-OrdinaryPath $entry $false $observation" in section(
        "function Assert-CompilerArtifacts", "function Assert-RetainedArtifacts")


@pytest.mark.parametrize("failure", sorted(PATH_REASONS | {"unknown"}))
def test_artifact_projection_root_refusal_has_no_enumerated_entries(failure):
    def entries():
        raise AssertionError("root refusal cannot enumerate children")
        yield

    reason, observation = artifact_observation_model(entries(), root_failure=failure)
    assert reason == failure and observation["scope"] == "root"
    assert all(value == 0 for key, value in observation.items() if key.endswith("_seen"))


@pytest.mark.parametrize("failure", ["path_lexical", "path_canonical", "path_component", "path_fullname", "unknown"])
def test_artifact_projection_early_failure_cannot_claim_entry_type(failure):
    reason, observation = artifact_observation_model([{"failure": failure, "directory": True}])
    assert reason == failure and observation["observed_type"] == "unknown"
    assert observation["entries_seen"] == observation["invalid_seen"] == 1
    assert observation["files_seen"] == observation["directories_seen"] == 0


@pytest.mark.parametrize("entry,expected", [
    ({"reparse": True}, "path_reparse"), ({"link": True}, "path_link"),
    ({"failure": "path_depth"}, "path_depth"),
    ({"failure": "path_reparse"}, "path_reparse"),  # Ancestor flag is not a child flag.
    ({"failure": "path_link"}, "path_link"),
    ({"directory": True, "reparse": True, "link": True}, "path_type"),
    ({"reparse": True, "link": True}, "path_reparse"),
])
def test_artifact_projection_preserves_gate_precedence_and_partial_flag_counts(entry, expected):
    reason, observation = artifact_observation_model([entry])
    assert reason == expected and observation["invalid_seen"] == 1
    assert observation["reparse_seen"] == int(not entry.get("directory") and bool(entry.get("reparse")))
    assert observation["links_seen"] == int(not entry.get("directory") and not entry.get("reparse") and bool(entry.get("link")))


@pytest.mark.parametrize("count", [0, 1, 64, 65, 100])
def test_artifact_projection_count_cap_is_65_and_does_not_read_65th_metadata(count):
    class UnreadableMetadata(dict):
        def get(self, *args):
            raise AssertionError("cap refusal must not inspect the 65th entry")

    entries = [{}] * min(count, 64) + ([UnreadableMetadata()] * max(0, count - 64))
    reason, observation = artifact_observation_model(entries)
    assert reason == ("compiler_artifacts" if count > 64 else None)
    assert observation["entries_seen"] == min(count, 65)
    assert observation["files_seen"] == min(count, 64)
    assert observation["invalid_seen"] == 0
    assert all(0 <= value <= 65 for key, value in observation.items() if key.endswith("_seen"))
    if count > 64:
        assert observation["observed_type"] == "unknown"


@pytest.mark.parametrize("sizes,refused", [([0], False), ([16777216], False), ([16777217], True), ([-1], True), ([16777216, 1], True)])
def test_artifact_projection_preserves_retained_file_size_bounds(sizes, refused):
    reason, observation = artifact_observation_model([{"size": size} for size in sizes])
    assert reason == ("compiler_artifacts" if refused else None)
    assert observation["invalid_seen"] == int(refused)


@pytest.mark.parametrize("message", ["path", "path_type private-name", "path_type\n", "PATH_TYPE", "C:\\private\\file", "Bearer token"])
def test_artifact_projection_unknown_stays_unknown_without_message_scraping(message):
    assert hosted_projection_model("compiler_artifacts", message) == {
        "phase": "compiler_artifacts", "reason": "unknown", "compiler_code": "unknown"}
