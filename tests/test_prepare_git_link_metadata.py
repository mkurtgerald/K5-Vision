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
COMMIT = "10c2d4e64f494eef37b7c969c94ea1de5420bc77"
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
        18150, "2cadebc723f48a7602dd6172ecad2a58211e93ef",
        "f5b51a6ba796bf8b6e0acbbc31cfd976fbd87760a870821f4b35194c74144062",
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
    ("tests/test_observe_installed_git_links_wrapper.py", "df719627b95af3695bb494562e5c43935013641a18039acc3330aa47a7fa689f"),
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
    assert "$_" not in TEXT.split("$failure = 'context_binding'", 1)[1]
    assert "hard compiler disk quota" in TEXT


def test_source_checks_explicitly_do_not_claim_windows_or_native_proof():
    assert "not a PowerShell parser or Windows execution proof" in __doc__
    assert "No PowerShell, C#, CLI, Git, network, sockets, compilation, or runtime setup" in __doc__
