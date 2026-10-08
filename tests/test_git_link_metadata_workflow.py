"""Pure workflow/source contracts; no Actions, PowerShell, compiler or network calls."""

import base64
import hashlib
import itertools
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / ".github/workflows/installed-git-link-metadata.yml"
TEXT = PATH.read_text(encoding="utf-8")
# A narrowly fixed indentation extractor, not a general YAML parser. Actions is
# still responsible for real workflow parsing during the reviewed hosted run.
def scalar(text, indent, name):
    values = re.findall(r"(?m)^" + " " * indent + re.escape(name) + r": (.+)$", text)
    assert len(values) == 1, (name, values)
    return values[0]


def bracket_list(value):
    assert value.startswith("[") and value.endswith("]")
    return value[1:-1].split(", ")


def fixture_workflow(text):
    on = text.split("\non:\n", 1)[1].split("\npermissions:\n", 1)[0]
    permissions = text.split("\npermissions:\n", 1)[1].split("\nconcurrency:\n", 1)[0]
    concurrency = text.split("\nconcurrency:\n", 1)[1].split("\nenv:\n", 1)[0]
    environment = text.split("\nenv:\n", 1)[1].split("\njobs:\n", 1)[0]
    body = text.split("\njobs:\n", 1)[1]
    names = re.findall(r"(?m)^  ([a-z_]+):$", body)
    assert names == ["hosted_qualify", "physical_observe"]
    jobs = {}
    for index, name in enumerate(names):
        job = body.split("  " + name + ":\n", 1)[1]
        if index + 1 < len(names):
            job = job.split("  " + names[index + 1] + ":\n", 1)[0]
        header, step_text = job.split("    steps:\n", 1)
        condition = re.search(r"(?ms)^    if: >-\n(.*?)(?=^    [a-z])", header).group(1)
        runner = scalar(header, 4, "runs-on")
        item = {"if": condition, "runs-on": bracket_list(runner) if runner.startswith("[") else runner,
                "timeout-minutes": scalar(header, 4, "timeout-minutes"), "steps": []}
        if "    needs:" in header:
            item["needs"] = scalar(header, 4, "needs")
        if "    outputs:" in header:
            item["outputs"] = {key: scalar(header, 6, key) for key in ("qualification", "post_admission")}
        for part in step_text.split("      - name: ")[1:]:
            lead, run = part.split("        run: |\n", 1)
            script = "\n".join(line[10:] for line in run.rstrip().splitlines()) + "\n"
            step = {"id": scalar(lead, 8, "id"), "shell": scalar(lead, 8, "shell"),
                    "timeout-minutes": scalar(lead, 8, "timeout-minutes"), "run": script,
                    "env": {key: scalar(lead, 10, key) for key in ("K5_GITHUB_TOKEN", "K5_METADATA_MODE")}}
            if "        if:" in lead:
                step["if"] = scalar(lead, 8, "if")
            item["steps"].append(step)
        jobs[name] = item
    assert permissions.strip() == "contents: read"
    assert on.lstrip().startswith("push:\n")
    return {"on": {"push": {key: bracket_list(scalar(on, 4, key)) for key in ("branches", "paths")}},
            "permissions": {"contents": scalar(permissions, 2, "contents")},
            "concurrency": {key: scalar(concurrency, 2, key) for key in ("group", "cancel-in-progress")},
            "env": {"K5_DIAGNOSTIC_SHA": scalar(environment, 2, "K5_DIAGNOSTIC_SHA")}, "jobs": jobs}


WORKFLOW = fixture_workflow(TEXT)
HELPER = ROOT / "scripts/prepare-git-link-metadata.ps1"


def all_steps():
    return [step for job in WORKFLOW["jobs"].values() for step in job["steps"]]


def test_only_one_exact_push_route_exists():
    assert WORKFLOW["on"] == {"push": {
        "branches": ["review/git-link-metadata-20261008"],
        "paths": [".github/workflows/installed-git-link-metadata.yml"],
    }}
    assert WORKFLOW["permissions"] == {"contents": "read"}
    assert WORKFLOW["concurrency"] == {
        "group": "k5-fixed-git-readonly-diagnostic", "cancel-in-progress": "false"
    }
    assert set(WORKFLOW["jobs"]) == {"hosted_qualify", "physical_observe"}
    assert WORKFLOW["env"] == {"K5_DIAGNOSTIC_SHA": "${{ github.sha }}"}


@pytest.mark.parametrize("job_name", ["hosted_qualify", "physical_observe"])
def test_exact_repository_event_branch_actor_head_attempt(job_name):
    condition = WORKFLOW["jobs"][job_name]["if"]
    for required in (
        "github.repository == 'mkurtgerald/K5-Vision'",
        "github.event_name == 'push'",
        "github.ref == 'refs/heads/review/git-link-metadata-20261008'",
        "github.actor == github.repository_owner",
        "github.triggering_actor == github.repository_owner",
        "github.run_attempt == 1",
        "github.event.head_commit.id == github.sha",
        "github.event.deleted == false",
        "github.event.forced == false",
    ):
        assert required in condition


def test_physical_requires_all_hosted_successes_with_no_continue_on_error():
    hosted = WORKFLOW["jobs"]["hosted_qualify"]
    physical = WORKFLOW["jobs"]["physical_observe"]
    assert hosted["outputs"] == {
        "qualification": "${{ steps.qualify.outcome }}",
        "post_admission": "${{ steps.post.outcome }}",
    }
    assert physical["needs"] == "hosted_qualify"
    for required in (
        "needs.hosted_qualify.result == 'success'",
        "needs.hosted_qualify.outputs.qualification == 'success'",
        "needs.hosted_qualify.outputs.post_admission == 'success'",
    ):
        assert required in physical["if"]
    assert "continue-on-error" not in TEXT
    assert "always()" not in physical["if"]
    assert "||" not in physical["if"]
    states = ("success", "failure", "cancelled", "skipped", "", None)
    admitted = [row for row in itertools.product(states, repeat=3)
                if all(value == "success" for value in row)]
    assert admitted == [("success", "success", "success")]


def test_same_host_post_steps_are_independent_always_run_and_short():
    hosted = WORKFLOW["jobs"]["hosted_qualify"]
    physical = WORKFLOW["jobs"]["physical_observe"]
    assert hosted["runs-on"] == "windows-latest"
    assert physical["runs-on"] == ["self-hosted", "Windows", "X64", "k5-physical", "camera-lab"]
    assert hosted["timeout-minutes"] == "5"
    assert physical["timeout-minutes"] == "6"
    for job, ids, modes, bounds in (
        (hosted, ["qualify", "post"], ["HostedQualify", "HostedPost"], ["2", "1"]),
        (physical, ["observe", "post"], ["PhysicalObserve", "PhysicalPost"], ["2", "2"]),
    ):
        assert len(job["steps"]) == 2
        assert [step["id"] for step in job["steps"]] == ids
        assert [step["env"]["K5_METADATA_MODE"] for step in job["steps"]] == modes
        assert [step["timeout-minutes"] for step in job["steps"]] == bounds
        assert job["steps"][1]["if"] == "${{ always() }}"
        assert int(job["timeout-minutes"]) > sum(map(int, bounds))
        assert "if" not in job["steps"][0]


def test_all_four_steps_independently_bind_the_same_helper_bytes():
    steps = all_steps()
    assert len({step["run"] for step in steps}) == 1
    raw = HELPER.read_bytes()
    expected_blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
    expected_digest = hashlib.sha256(raw).hexdigest()
    for step in steps:
        assert step["shell"] == "powershell"
        assert step["env"]["K5_GITHUB_TOKEN"] == "${{ github.token }}"
        source = step["run"]
        assert f"$expectedBlob = '{expected_blob}'" in source
        assert f"$expectedDigest = '{expected_digest}'" in source
        assert f"$expectedSize = {len(raw)}" in source
        assert "$script = [scriptblock]::Create($strictUtf8.GetString($bytes))" in source
        assert "& $script -Mode $env:K5_METADATA_MODE" in source
        assert source.index("if ($gitBlob -cne $expectedBlob)") < source.index("[scriptblock]::Create")
        assert source.index("if ($actual -cne $expectedDigest)") < source.index("[scriptblock]::Create")
    assert 0 < len(raw) <= 65536


def test_api_bootstrap_has_fixed_origin_and_true_pre_read_bounds():
    source = all_steps()[0]["run"]
    for token in (
        "$env:GITHUB_API_URL -cne 'https://api.github.com'",
        "https://api.github.com/repos/mkurtgerald/K5-Vision/git/blobs/",
        "$request.AllowAutoRedirect = $false",
        "$request.Timeout = 15000",
        "$request.ReadWriteTimeout = 15000",
        "$request.MaximumResponseHeadersLength = 16",
        "$request.AutomaticDecompression = [Net.DecompressionMethods]::None",
        "$maximumResponse = 131072",
        "$remaining = $maximumResponse - [int]$memory.Length",
        "if ($remaining -le 0) { throw 'response_bound' }",
        "$response.ResponseUri.AbsoluteUri -cne $url",
        "$record.sha -cne $expectedBlob",
        "$bytes.Length -ne $expectedSize",
        "$sha256.ComputeHash($bytes)",
        "$sha1.ComputeHash($gitBytes)",
    ):
        assert token in source
    assert source.index("if ($remaining -le 0)") < source.index("$stream.Read(")
    assert "$response.StatusCode -ne 200" in source
    assert source.count("$request.GetResponse()") == 1
    assert "$record.url" not in source and "$record.download_url" not in source


@pytest.mark.parametrize("forbidden", [
    "actions/checkout", "actions/setup", "uses:", "Start-Process", "git.exe",
    "python", "pip install", "Invoke-Expression", "Invoke-WebRequest",
    "Set-Acl", "Stop-Process", "Remove-Item", "taskkill", "-Recurse",
    "workflow_dispatch", "repository_dispatch", "schedule:", "pull_request:",
    "Out-File", "WriteAll", "Get-Command", "GITHUB_ENV", "GITHUB_PATH",
])
def test_bootstrap_never_uses_an_alternate_execution_or_trigger_route(forbidden):
    assert forbidden not in TEXT


def test_source_binding_model_rejects_changed_bytes_and_wrong_sizes_before_execution():
    raw = HELPER.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()

    def validate(encoded, size, want_digest, want_blob):
        data = base64.b64decode(encoded, validate=True)
        assert len(data) == size
        assert hashlib.sha256(data).hexdigest() == want_digest
        assert hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest() == want_blob
        return data

    assert validate(base64.b64encode(raw), len(raw), digest, blob) == raw
    for data, size, expected_hash, expected_blob in (
        (raw + b"x", len(raw), digest, blob),
        (raw[:-1] + bytes([raw[-1] ^ 1]), len(raw), digest, blob),
        (raw, len(raw), "0" * 64, blob),
        (raw, len(raw), digest, "0" * 40),
    ):
        with pytest.raises(AssertionError):
            validate(base64.b64encode(data), size, expected_hash, expected_blob)
