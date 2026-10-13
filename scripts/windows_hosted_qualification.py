"""Test-only, per-step hosted Windows admission. Never installed with K5.

The GitHub runner, setup-python bootstrap, this supervisor and artifact transport
are outside the owned payload Job. Every payload must drain before another step.
Portable source tests do not establish native resource or ABI qualification.
"""

from __future__ import annotations

import argparse
import ctypes as c
import hashlib
import importlib.util
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROFILE_ROOT = ROOT / "scripts/qualification"
GIB = 1024**3
EVIDENCE_MODE = "sampled-owned-job-v1"
MAX_ASSOCIATIONS = 2**53 - 1  # Exact cross-Job JSON integers; not a resource cap.
ASSOCIATION_FIELDS = (
    "job_total_associations",
    "fully_live_observed_associations",
    "unobserved_or_rejected_associations",
)
PROFILES = {
    "alpha": ".github/workflows/windows-alpha-script-smoke.yml",
    "operator": ".github/workflows/operator-product-windows.yml",
}
POWERSHELL_PREFIX = "$ErrorActionPreference = 'stop'\n"
POWERSHELL_SUFFIX = "\nif ((Test-Path -LiteralPath variable:\\LASTEXITCODE)) { exit $LASTEXITCODE }"


class Refusal(RuntimeError):
    """Fixed, source-free failure category."""


def need(value, category="admission"):
    if not value:
        raise Refusal(category)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def ordinary(path, *, directory=False):
    path = Path(path)
    need(path.is_absolute() and path.exists(), "path")
    for item in (path, *path.parents):
        info = item.lstat()
        need(not item.is_symlink() and not getattr(info, "st_file_attributes", 0) & 0x400, "path")
    need(path.is_dir() if directory else path.is_file(), "path")
    return path.resolve(strict=True)


def load_json(path, limit=2 * 1024**2):
    path = ordinary(path)
    need(path.stat().st_size <= limit, "document_size")

    def pairs(values):
        result = {}
        for key, value in values:
            need(key not in result, "duplicate_field")
            result[key] = value
        return result

    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs)


def canonical_body(value):
    need("\x00" not in value and "\r" not in value.replace("\r\n", ""), "script_encoding")
    return value.replace("\r\n", "\n")


def workflow_steps(text):
    """Read only the reviewed simple run-block form; no general YAML evaluator."""
    result = []
    for chunk in text.split("      - name: ")[1:]:
        lines = chunk.splitlines(keepends=True)
        name = lines[0].strip()
        if "        run: |\n" not in chunk:
            continue
        prefix, body = chunk.split("        run: |\n", 1)
        selected = []
        for line in body.splitlines(keepends=True):
            need(not line.strip() or line.startswith("          "), "workflow_shape")
            selected.append(line[10:] if line.startswith("          ") else line)
        # A literal YAML | scalar keeps one terminal LF and clips blank separators.
        result.append(
            {
                "name": name,
                "body": canonical_body("".join(selected)).rstrip("\n") + "\n",
                "always": "if: always()" in prefix,
                "timeout": 360 if "timeout-minutes: 6" in prefix else 900,
            }
        )
    return result


def admitted_body(profile, step, generated, runner_temp):
    expected = workflow_steps((ROOT / PROFILES[profile]).read_text(encoding="utf-8"))
    need(type(step) is int and 0 <= step < len(expected), "step_identity")
    generated = ordinary(generated)
    need(
        generated.parent == ordinary(runner_temp, directory=True)
        and re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", generated.stem)
        and generated.suffix == ".ps1"
        and generated.stat().st_size <= 1024**2,
        "step_path",
    )
    actual = canonical_body(generated.read_text(encoding="utf-8-sig"))
    need(actual == POWERSHELL_PREFIX + expected[step]["body"] + POWERSHELL_SUFFIX, "step_body")
    need("scripts/windows_hosted_qualification.py" not in actual, "nested_guard")
    return expected[step]


def numeric(value):
    return type(value) in {int, float} and value >= 0 and value < float("inf")


def admit_sample(sample, *, initial=False, previous_cpu=0):
    fields = {
        "logical_cpus",
        "physical_bytes",
        "available_bytes",
        "free_disk_bytes",
        "host_cpu_percent",
        "rss_bytes",
        "active_processes",
        "cpu_seconds",
        "sample_age_seconds",
        "loopback_only",
    }
    need(fields <= sample.keys(), "measurement_missing")
    need(all(numeric(sample[k]) for k in fields - {"loopback_only"}), "measurement_type")
    need(sample["loopback_only"] is True, "endpoint")
    need(sample["sample_age_seconds"] <= 1, "measurement_stale")
    need(sample["logical_cpus"] >= 2 and sample["physical_bytes"] >= 6 * GIB, "host_capacity")
    need(sample["free_disk_bytes"] >= (8 if initial else 2) * GIB, "disk_headroom")
    need(sample["available_bytes"] >= (4 if initial else 2) * GIB, "memory_headroom")
    need(not initial or sample["host_cpu_percent"] <= 50, "cpu_headroom")
    need(sample["rss_bytes"] <= 2 * GIB, "rss_limit")
    need(sample["active_processes"] <= 32, "process_limit")
    need(previous_cpu + sample["cpu_seconds"] <= 900, "cpu_limit")


def validate_wheelhouse(path, records):
    path = ordinary(path, directory=True)
    expected = {r["filename"]: r for r in records}
    need(
        len(expected) == len(records) and {p.name for p in path.iterdir()} == set(expected),
        "wheel_inventory",
    )
    for name, record in expected.items():
        wheel = ordinary(path / name)
        need(
            wheel.stat().st_size == record["size"] and digest(wheel) == record["sha256"],
            "wheel_identity",
        )


def association_evidence(value, *, require_complete=True):
    need(type(value) is dict and value.get("evidence_mode") == EVIDENCE_MODE, "evidence_mode")
    need(type(value.get("association_counts_complete")) is bool, "association_counts")
    counts = [value.get(field) for field in ASSOCIATION_FIELDS]
    need(
        all(type(count) is int and 0 <= count <= MAX_ASSOCIATIONS for count in counts),
        "association_counts",
    )
    need(counts[0] == counts[1] + counts[2], "association_counts")
    need(
        not require_complete or value["association_counts_complete"],
        "association_counts_incomplete",
    )


def accumulate_associations(state, resources):
    association_evidence(state)
    need(resources.association_counts_recorded is False, "association_counts_already_recorded")
    need(resources.association_counts_complete is True, "association_counts_unavailable")
    total, observed = resources.final_total, len(resources.witnessed_births)
    need(type(total) is int and 0 <= observed <= total < 2**32, "association_counts")
    additions = (total, observed, total - observed)
    updated = {
        **state,
        **{
            field: state[field] + count
            for field, count in zip(ASSOCIATION_FIELDS, additions, strict=True)
        },
    }
    # Validate every new aggregate before committing any field. Close never
    # records counts; this explicit once-per-Job path also covers refused stages.
    association_evidence(updated)
    state.update({field: updated[field] for field in ASSOCIATION_FIELDS})
    resources.association_counts_recorded = True


def receipt_valid(receipt):
    required = {
        "source_sha",
        "profile_sha256",
        "workflow_sha256",
        "run_id",
        "attempt",
        "completed",
        "cleanup_complete",
        "owned_active",
        "owned_listeners",
        "peak_rss_bytes",
        "cpu_seconds",
        "stage_count",
    }
    need(type(receipt) is dict and required <= receipt.keys(), "receipt_missing")
    association_evidence(receipt)
    need(receipt["completed"] is True and receipt["cleanup_complete"] is True, "receipt_incomplete")
    need(receipt["owned_active"] == 0 and receipt["owned_listeners"] == 0, "cleanup_incomplete")
    need(
        numeric(receipt["peak_rss_bytes"]) and receipt["peak_rss_bytes"] <= 2 * GIB,
        "receipt_resource",
    )
    need(numeric(receipt["cpu_seconds"]) and receipt["cpu_seconds"] <= 900, "receipt_resource")
    need(type(receipt["stage_count"]) is int and receipt["stage_count"] > 0, "receipt_incomplete")
    for field, length in (("source_sha", 40), ("profile_sha256", 64), ("workflow_sha256", 64)):
        need(
            re.fullmatch("[0-9a-f]{" + str(length) + "}", receipt[field]) is not None,
            "receipt_identity",
        )


def common_module():
    spec = importlib.util.spec_from_file_location(
        "_k5_hosted_common", ROOT / "scripts/installed_analytics_witness.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def context(profile):
    need(os.name == "nt" and os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted", "platform")
    need(profile in PROFILES, "profile")
    run_id, attempt = os.environ.get("GITHUB_RUN_ID", ""), os.environ.get("GITHUB_RUN_ATTEMPT", "")
    source = os.environ.get("K5_QUAL_SOURCE_SHA", "")
    need(
        re.fullmatch(r"[1-9][0-9]{0,19}", run_id)
        and re.fullmatch(r"[1-9][0-9]{0,5}", attempt)
        and re.fullmatch(r"[0-9a-f]{40}", source),
        "context",
    )
    return {
        "run_id": run_id,
        "attempt": attempt,
        "source_sha": source,
        "profile_sha256": digest(PROFILE_ROOT / "windows-hosted-profile.json"),
        "workflow_sha256": digest(ROOT / PROFILES[profile]),
        "profile": profile,
    }


def loaded_python_dll():
    kernel = c.WinDLL("kernel32", use_last_error=True)
    kernel.GetModuleFileNameW.argtypes = [c.c_void_p, c.c_wchar_p, c.c_uint32]
    kernel.GetModuleFileNameW.restype = c.c_uint32
    buffer = c.create_unicode_buffer(32768)
    count = kernel.GetModuleFileNameW(sys.dllhandle, buffer, len(buffer))
    need(0 < count < len(buffer), "bootstrap_loaded_dll")
    return buffer.value


def validate_bootstrap_pe(path):
    with ordinary(path).open("rb") as stream:
        need(stream.read(2) == b"MZ", "bootstrap_pe")
        stream.seek(0x3C)
        offset = int.from_bytes(stream.read(4), "little")
        need(0x40 <= offset <= 1024**2 and offset + 26 <= Path(path).stat().st_size, "bootstrap_pe")
        stream.seek(offset)
        need(stream.read(6) == b"PE\0\0\x64\x86", "bootstrap_pe")
        stream.seek(offset + 24)
        need(stream.read(2) == b"\x0b\x02", "bootstrap_pe")


def validate_bootstrap_cache(cache, executable, exe_hash, dll_hash, *, version, bits, loaded_dll):
    cache = ordinary(cache, directory=True)
    expected = cache / "Python/3.12.10/x64"
    executable = ordinary(executable)
    need(executable == expected / "python.exe", "bootstrap_cache_path")
    need(not re.search(r"[\s\"'{}]", str(executable)), "bootstrap_shell_path")
    ordinary(cache / "Python/3.12.10/x64.complete")
    ordinary(expected / "LICENSE.txt")
    for name in ("pyvenv.cfg", "python._pth", "python312._pth"):
        need(not (expected / name).exists(), "bootstrap_path_hook")
    need(tuple(version) == (3, 12, 10) and bits == 64, "bootstrap_version")
    dll = ordinary(expected / "python312.dll")
    need(ordinary(loaded_dll) == dll, "bootstrap_loaded_dll")
    validate_bootstrap_pe(executable)
    validate_bootstrap_pe(dll)
    need(digest(executable) == exe_hash and digest(dll) == dll_hash, "bootstrap_identity")


def verify_bootstrap_selection():
    validate_bootstrap_cache(
        os.environ["RUNNER_TOOL_CACHE"],
        Path(sys.base_prefix) / "python.exe",
        os.environ["K5_BOOTSTRAP_PYTHON_SHA256"],
        os.environ["K5_BOOTSTRAP_DLL_SHA256"],
        version=sys.version_info[:3],
        bits=c.sizeof(c.c_void_p) * 8,
        loaded_dll=loaded_python_dll(),
    )
    need(
        ordinary(Path(sys.base_prefix) / "python.exe")
        == ordinary(os.environ["K5_BOOTSTRAP_PYTHON"]),
        "bootstrap_selected_path",
    )


def read_state(profile, *, cleanup=False):
    identity = context(profile)
    verify_bootstrap_selection()
    work = ordinary(os.environ["K5_QUAL_ROOT"], directory=True)
    parent = ordinary(
        os.environ.get("K5_QUAL_HOST_TEMP", os.environ["RUNNER_TEMP"]), directory=True
    )
    need(
        work.parent == parent and re.fullmatch(r"k5-hosted-[a-z0-9_]{8,32}", work.name), "work_root"
    )
    state = load_json(work / "state.json")
    association_evidence(state, require_complete=not cleanup)
    need(all(state.get(k) == v for k, v in identity.items()), "state_identity")
    need(
        state["bootstrap_path"] == str(ordinary(Path(sys.base_prefix) / "python.exe"))
        and digest(state["bootstrap_path"]) == state["bootstrap_sha256"],
        "bootstrap_identity",
    )
    need(cleanup or 0 <= time.time() - state["started_at"] <= 900, "job_deadline")
    need(state["wrapper_sha256"] == digest(Path(__file__)), "source_drift")
    need(
        state["common_sha256"] == digest(ROOT / "scripts/installed_analytics_witness.py"),
        "source_drift",
    )
    return work, state


def save_state(work, state):
    association_evidence(state, require_complete=False)
    temporary = work / "state.next.json"
    need(not temporary.exists(), "state_temporary")
    temporary.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    temporary.replace(work / "state.json")


def child_environment(work, state):
    allowed = {
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "SYSTEMDRIVE",
        "OS",
        "PATHEXT",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE",
        "PROCESSOR_IDENTIFIER",
        "PROCESSOR_LEVEL",
        "PROCESSOR_REVISION",
        "USERNAME",
        "USERDOMAIN",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "COMMONPROGRAMFILES",
        "COMMONPROGRAMFILES(X86)",
        "GITHUB_ACTIONS",
        "GITHUB_WORKSPACE",
        "GITHUB_ENV",
        "GITHUB_OUTPUT",
        "GITHUB_STEP_SUMMARY",
        "GITHUB_RUN_ID",
        "GITHUB_RUN_ATTEMPT",
        "GITHUB_SHA",
        "GITHUB_REPOSITORY",
        "RUNNER_TOOL_CACHE",
        "K5_BOOTSTRAP_PYTHON",
        "K5_BOOTSTRAP_PYTHON_SHA256",
        "K5_BOOTSTRAP_DLL_SHA256",
        "RUNNER_OS",
        "RUNNER_ARCH",
        "RUNNER_ENVIRONMENT",
        "K5_QUAL_SOURCE_SHA",
        "K5_QUAL_ROOT",
        "K5_SETUP_PYTHON",
        "K5_DONOR_TOOL_ROOT",
        "K5_MEDIAMTX_DONOR_EXE",
        "K5_CADDY_DONOR_EXE",
        "K5_DONOR_FFMPEG",
        "K5_DONOR_FFPROBE",
        "K5_OPERATOR_WHEEL",
    }
    env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
    env.update(
        PYTHONNOUSERSITE="1",
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONPATH="",
        PYTHONHOME="",
        NO_PROXY="*",
        PIP_CONFIG_FILE=os.devnull,
        PIP_NO_INDEX="1",
        PIP_NO_INPUT="1",
        PIP_NO_COMPILE="1",
        PIP_DISABLE_PIP_VERSION_CHECK="1",
        PIP_REQUIRE_VIRTUALENV="true",
        PIP_FIND_LINKS=str(work / "wheels"),
        PIP_CONSTRAINT=str(work / "constraints.txt"),
        PIP_CACHE_DIR=str(work / "pip-cache"),
        TEMP=str(work / "tmp"),
        TMP=str(work / "tmp"),
        RUNNER_TEMP=str(work / "tmp"),
        LOCALAPPDATA=str(work / "localappdata"),
        APPDATA=str(work / "appdata"),
        K5_QUAL_HOST_TEMP=state["host_temp"],
    )
    env["PATH"] = os.pathsep.join([str(work / "venv/Scripts"), *state["tool_directories"]])
    # The supervisor gate retains its independently admitted setup-python binding.
    # The unchanged test body establishes its separate tested-runtime binding.
    env["K5_WITNESS_BASE_PYTHON"] = state["bootstrap_path"]
    env["K5_WITNESS_BASE_PYTHON_SHA256"] = state["bootstrap_sha256"]
    return env


def run_owned(arguments, *, cwd, env, work, state, seconds):
    association_evidence(state)
    need(state.get("pending_payload") is False, "payload_pending")
    state["pending_payload"] = True
    save_state(work, state)
    common = common_module()
    resources = QualificationResources(common, work)
    owned = None
    original = None
    last = None
    started = time.monotonic()
    try:
        job = resources.job_factory()
        time.sleep(0.25)
        admit_sample(sample_values(resources), initial=True, previous_cpu=state["cpu_seconds"])

        def before_release(_resources):
            time.sleep(0.25)
            admit_sample(sample_values(resources), initial=True, previous_cpu=state["cpu_seconds"])

        resources.before_release = before_release
        owned = common.OwnedProcess(
            arguments,
            cwd=cwd,
            env=env,
            operation="driver_admission",
            stdout=None,
            job_factory=lambda: job,
        )
        while True:
            time.sleep(0.25)
            last = sample_values(resources)
            admit_sample(last, previous_cpu=state["cpu_seconds"])
            state["peak_rss_bytes"] = max(state["peak_rss_bytes"], last["rss_bytes"])
            need(
                time.monotonic() - started <= seconds and time.time() - state["started_at"] <= 900,
                "deadline",
            )
            if owned.process.poll() is not None:
                need(owned.process.returncode == 0, "child_exit")
                break
    except BaseException as error:
        original = error
    finally:
        # A refused or incomplete sample may still have observed a higher RSS.
        # Preserve that evidence even when admission or cleanup subsequently fails.
        state["peak_rss_bytes"] = max(state["peak_rss_bytes"], resources.observed_peak_rss_bytes)
        try:
            if owned is not None:
                owned.close()
            resources.close()
        except BaseException:
            original = Refusal("cleanup_incomplete")
        try:
            accumulate_associations(state, resources)
        except (Refusal, ResourceFailure) as error:
            # Keep recovered earlier aggregates, explicitly marked partial.
            # Missing terminal accounting is never an invented zero or coverage.
            state["association_counts_complete"] = False
            if original is None:
                original = error
        if resources.final_cpu_seconds is not None:
            state["cpu_seconds"] += resources.final_cpu_seconds
        else:
            original = Refusal("final_cpu_unavailable")
        if not resources.cleanup_complete or resources.final_active != 0:
            state["all_owned_cleanup"] = False
            original = Refusal("cleanup_incomplete")
        else:
            state["pending_payload"] = False
        if state["cpu_seconds"] > 900:
            original = Refusal("cpu_limit")
        if original is None and resources.terminal_evidence_failure is not None:
            original = ResourceFailure(resources.terminal_evidence_failure)
    if original is not None:
        raise original


def execute_step(profile, step, generated):
    work, state = read_state(profile)
    selected = admitted_body(profile, step, generated, os.environ["RUNNER_TEMP"])
    need(step not in state["finished_steps"], "step_replay")
    need(not state["failed"] or selected["always"], "failed_prerequisite")
    guarded = [
        i
        for i, row in enumerate(workflow_steps((ROOT / PROFILES[profile]).read_text()))
        if "scripts/windows_hosted_qualification.py" not in row["body"]
    ]
    if not state["failed"]:
        need(step == guarded[len(state["finished_steps"])] and state["prepared"], "step_order")
    specification = load_json(PROFILE_ROOT / "windows-hosted-profile.json")
    validate_wheelhouse(work / "wheels", specification["wheels"])
    directory = Path(tempfile.mkdtemp(prefix="step-", dir=work))
    script = directory / "owned.ps1"
    script.write_bytes(
        (POWERSHELL_PREFIX + selected["body"] + POWERSHELL_SUFFIX)
        .replace("\n", "\r\n")
        .encode("utf-8")
    )
    shell = state["shell_path"]
    need(digest(shell) == state["shell_sha256"], "shell_identity")
    command = [
        state["bootstrap_path"],
        "-I",
        "-B",
        "-S",
        str(Path(__file__).resolve()),
        "--step-child",
        "--profile",
        profile,
        "--step",
        str(step),
        "--owned-script",
        str(script),
    ]
    try:
        run_owned(
            command,
            cwd=ROOT,
            env=child_environment(work, state),
            work=work,
            state=state,
            seconds=selected["timeout"],
        )
        validate_wheelhouse(work / "wheels", specification["wheels"])
        verify_secondary_seals(work, state, initialize=True)
        state["finished_steps"].append(step)
    except BaseException:
        state["failed"] = True
        raise
    finally:
        save_state(work, state)


# Bootstrap, final receipt and the qualification-only Win32 resource adapter are
# defined below. No product launcher/controller source is modified by this file.


def sample_values(resources):
    item = resources.sample()
    resources.assert_fresh(item)
    return {
        "logical_cpus": os.cpu_count(),
        "physical_bytes": item.host_total_bytes,
        "available_bytes": item.host_available_bytes,
        "free_disk_bytes": item.disk_free_bytes,
        "host_cpu_percent": item.host_cpu_percent,
        "rss_bytes": item.rss_bytes,
        "active_processes": item.active_processes,
        "cpu_seconds": item.job_cpu_seconds,
        "sample_age_seconds": time.monotonic() - item.started_at,
        "loopback_only": True,
    }


def pinned_download(record, destination):
    need(not destination.exists(), "download_target")
    allowed = {"github.com", "release-assets.githubusercontent.com", "files.pythonhosted.org"}
    url = urllib.parse.urlsplit(record["url"])
    need(url.scheme == "https" and url.hostname in allowed, "download_origin")

    class Redirects(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            target = urllib.parse.urlsplit(newurl)
            need(
                target.scheme == "https"
                and target.hostname in allowed
                and target.username is None
                and target.password is None,
                "download_redirect",
            )
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    count, h = 0, hashlib.sha256()
    with urllib.request.build_opener(Redirects()).open(record["url"], timeout=30) as response:
        with destination.open("xb") as stream:
            while block := response.read(min(1024**2, record["size"] + 1 - count)):
                count += len(block)
                need(count <= record["size"], "download_size")
                h.update(block)
                stream.write(block)
    need(count == record["size"] and h.hexdigest() == record["sha256"], "download_hash")


def extract_runtime(archive_path, work, specification):
    import tarfile

    expected = specification["runtime_files"]
    seen, total = set(), 0
    with tarfile.open(archive_path, mode="r|gz") as archive:
        for member in archive:
            name = member.name
            need(
                name.startswith("python/")
                and ".." not in Path(name).parts
                and "\\" not in name
                and ":" not in name,
                "archive_path",
            )
            need(member.isdir() or member.isfile(), "archive_type")
            if member.isdir():
                continue
            need(name in expected and name not in seen, "archive_member")
            identity = expected[name]
            need(member.size == identity["bytes"], "archive_size")
            total += member.size
            need(total <= 192 * 1024**2 and member.size <= 32 * 1024**2, "archive_limit")
            target = work / name
            target.parent.mkdir(parents=True, exist_ok=True)
            h = hashlib.sha256()
            count = 0
            with archive.extractfile(member) as source, target.open("xb") as output:
                while block := source.read(65536):
                    count += len(block)
                    h.update(block)
                    output.write(block)
            need(count == member.size and h.hexdigest() == identity["sha256"], "archive_hash")
            seen.add(name)
    need(seen == set(expected), "archive_inventory")
    purge_owned_bytecode(work / "python")
    licenses = work / "python/qualification-retained-licenses"
    licenses.mkdir()
    for name, text in specification["runtime_licenses"].items():
        need(Path(name).name == name and name.startswith("LICENSE."), "license_path")
        (licenses / name).write_text(text, encoding="utf-8")


def bootstrap_child(profile):
    work, state = read_state(profile)
    specification = load_json(PROFILE_ROOT / "windows-hosted-profile.json")
    verify_source(state)
    git = state["tools"]["git"]["path"]
    observed = subprocess.run(
        [git, "-C", str(ROOT), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()
    need(observed == state["source_sha"], "source_revision")
    archive = work / "runtime.tar.gz"
    pinned_download(specification["runtime_archive"], archive)
    extract_runtime(archive, work, specification)
    wheels = work / "wheels"
    wheels.mkdir()
    need(sum(row["size"] for row in specification["wheels"]) <= 40 * 1024**2, "wheel_budget")
    for record in specification["wheels"]:
        pinned_download(record, wheels / record["filename"])
    validate_wheelhouse(wheels, specification["wheels"])
    requirements = "".join(
        f"{r['name']}=={r['version']} --hash=sha256:{r['sha256']}\n"
        for r in specification["wheels"]
    )
    (work / "requirements.txt").write_text(requirements, encoding="utf-8")
    (work / "constraints.txt").write_text(
        "".join(f"{r['name']}=={r['version']}\n" for r in specification["wheels"]), encoding="utf-8"
    )
    runtime = work / "python/python.exe"
    env = child_environment(work, state)
    subprocess.run(
        [str(runtime), "-I", "-B", "-m", "venv", str(work / "venv")],
        check=True,
        env=env,
        timeout=60,
    )
    python = work / "venv/Scripts/python.exe"
    subprocess.run(
        [
            str(python),
            "-I",
            "-B",
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--require-hashes",
            "--requirement",
            str(work / "requirements.txt"),
        ],
        check=True,
        env=env,
        timeout=180,
    )
    verify_environment_process(python, env)


def verify_secondary_seals(work, state, *, initialize=False):
    for name, key in (
        ("runtime-dependencies.json", "runtime_dependency_seal"),
        ("project-launcher.json", "project-launcher.json"),
        ("runtime-project-launcher.json", "runtime-project-launcher.json"),
    ):
        path = work / name
        if key in state:
            need(digest(ordinary(path)) == state[key], "secondary_seal_drift")
        elif initialize and path.exists():
            state[key] = digest(ordinary(path))


def verify_venv_binding(prefix, work):
    prefix = ordinary(prefix, directory=True)
    need(prefix in {work / "venv", work / "tmp/k5-alpha-runtime-probe"}, "venv_scope")
    config = ordinary(prefix / "pyvenv.cfg")
    values = {}
    for line in config.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        need(separator and key.strip() not in values, "venv_config")
        values[key.strip()] = value.strip()
    need(
        ordinary(values["home"], directory=True) == work / "python"
        and values["include-system-site-packages"] == "false"
        and values["version"] == "3.12.15",
        "venv_base",
    )
    specification = load_json(PROFILE_ROOT / "windows-hosted-profile.json")
    for name in ("python.exe", "pythonw.exe"):
        identity = specification["runtime_files"]["python/Lib/venv/scripts/nt/" + name]
        executable = ordinary(prefix / "Scripts" / name)
        need(
            executable.stat().st_size == identity["bytes"]
            and digest(executable) == identity["sha256"],
            "venv_redirector",
        )
    baseline = work / (
        "dependencies.json" if prefix == work / "venv" else "runtime-dependencies.json"
    )
    if baseline.exists():
        need(load_json(baseline).get("pyvenv.cfg") == digest(config), "venv_config_seal")


def verify_environment_process(python, env):
    verify_venv_binding(python.parent.parent, Path(env["K5_QUAL_ROOT"]))
    purge_owned_bytecode(python.parent.parent)
    verify_environment(python.parent.parent)
    subprocess.run([str(python), "-I", "-B", "-m", "pip", "check"], check=True, env=env, timeout=30)


def verify_environment(prefix):
    """Bind installed payload bytes to the admitted wheel ZIPs, never its own RECORD."""
    import importlib.metadata
    import zipfile

    prefix = ordinary(prefix, directory=True)
    work = ordinary(Path(os.environ["K5_QUAL_ROOT"]), directory=True)
    need(
        prefix == work / "venv" or prefix == work / "tmp/k5-alpha-runtime-probe",
        "environment_scope",
    )
    specification = load_json(PROFILE_ROOT / "windows-hosted-profile.json")
    rows = specification["wheels"]
    runtime_only = prefix != work / "venv"
    if runtime_only:
        rows = [
            row
            for row in rows
            if row["group"] == "retained-runtime" or row["name"] in {"pyreadline3", "pip"}
        ]
    expected = {r["name"].lower().replace("_", "-"): r["version"] for r in rows}
    purge_owned_bytecode(prefix)
    site = prefix / "Lib/site-packages"
    seen, project = {}, False
    for distribution in importlib.metadata.distributions(path=[str(site)]):
        name = distribution.metadata["Name"].lower().replace("_", "-")
        need(name not in seen, "dependency_duplicate")
        seen[name] = distribution.version
        if name == "k5-vision":
            need(distribution.version == "0.1.0", "project_version")
            project = True
        else:
            need(expected.get(name) == distribution.version, "dependency_version")
    need({k: v for k, v in seen.items() if k != "k5-vision"} == expected, "dependency_inventory")
    admitted = set()
    mutable_metadata = set()
    for row in rows:
        wheel = ordinary(work / "wheels" / row["filename"])
        need(
            wheel.stat().st_size == row["size"] and digest(wheel) == row["sha256"], "wheel_identity"
        )
        with zipfile.ZipFile(wheel) as archive:
            for member in archive.infolist():
                if member.is_dir():
                    continue
                name = member.filename
                need(
                    ".." not in Path(name).parts
                    and not name.startswith(("/", "\\"))
                    and "\\" not in name
                    and ":" not in name,
                    "wheel_path",
                )
                if ".data/" in name:
                    _, relative = name.split(".data/", 1)
                    need(relative.startswith("scripts/"), "wheel_data_layout")
                    target = prefix / "Scripts" / relative[len("scripts/") :]
                else:
                    target = site / name
                target = ordinary(target)
                need(target.is_relative_to(prefix), "dependency_path")
                admitted.add(target)
                if name.endswith(".pyc"):
                    continue
                if name.endswith(".dist-info/RECORD"):
                    mutable_metadata.update(
                        target.parent / item
                        for item in ("RECORD", "INSTALLER", "REQUESTED", "direct_url.json")
                    )
                    continue
                h = hashlib.sha256()
                with archive.open(member) as stream:
                    while block := stream.read(65536):
                        h.update(block)
                need(
                    target.stat().st_size == member.file_size and digest(target) == h.hexdigest(),
                    "dependency_bytes",
                )
    # All executable package payload and startup hooks are accounted for. pip's
    # installer metadata is sealed immediately after the first authenticated install.
    for path in site.rglob("*"):
        if path.is_dir():
            continue
        path = ordinary(path)
        if path in admitted or path in mutable_metadata:
            continue
        relative = path.relative_to(site).as_posix()
        if project and relative.startswith("k5_vision-0.1.0.dist-info/"):
            if relative == "k5_vision-0.1.0.dist-info/licenses/LICENSE":
                need(digest(path) == digest(ROOT / "LICENSE"), "project_license")
                continue
            need(
                path.parent == site / "k5_vision-0.1.0.dist-info"
                and path.name
                in {
                    "METADATA",
                    "WHEEL",
                    "RECORD",
                    "INSTALLER",
                    "REQUESTED",
                    "direct_url.json",
                    "entry_points.txt",
                },
                "project_metadata",
            )
            continue
        # The admitted editables 0.6 backend emits this exact path-only hook.
        # Other editable names and executable startup hooks remain unadmitted.
        if project and not runtime_only and relative == "_editable_impl_k5_vision.pth":
            need(path.read_text().strip() == str(ROOT / "src"), "project_editable_path")
            continue
        if project and runtime_only and relative.startswith("k5vision/"):
            source = ordinary(ROOT / "src" / relative)
            need(digest(path) == digest(source), "project_payload")
            continue
        raise Refusal("dependency_extra_file")
    launcher = prefix / "Scripts/k5-vision.exe"
    if launcher.exists():
        need(project, "project_launcher_unexpected")
        seal = work / ("runtime-project-launcher.json" if runtime_only else "project-launcher.json")
        observed_launcher = {"sha256": digest(ordinary(launcher))}
        if seal.exists():
            need(load_json(seal) == observed_launcher, "project_launcher_changed")
        else:
            seal.write_text(json.dumps(observed_launcher), encoding="utf-8")
    baseline = work / ("runtime-dependencies.json" if runtime_only else "dependencies.json")
    mutable_metadata.add(prefix / "pyvenv.cfg")
    mutable_metadata.update(
        path
        for path in (prefix / "Scripts").iterdir()
        if path.is_file() and path.name != "k5-vision.exe"
    )
    observed = {
        str(path.relative_to(prefix)): digest(path)
        for path in sorted(mutable_metadata)
        if path.exists()
    }
    if baseline.exists():
        need(load_json(baseline) == observed, "dependency_installer_metadata")
    else:
        baseline.write_text(json.dumps(observed, sort_keys=True), encoding="utf-8")


def prepare(profile):
    identity = context(profile)
    verify_bootstrap_selection()
    bootstrap = ordinary(Path(sys.base_prefix) / "python.exe")
    need(sys.version_info[:3] == (3, 12, 10), "bootstrap_version")
    parent = ordinary(os.environ["RUNNER_TEMP"], directory=True)
    work = Path(tempfile.mkdtemp(prefix="k5-hosted-", dir=parent))
    shell = shutil.which("powershell" if profile == "alpha" else "pwsh")
    need(shell is not None, "shell_missing")
    shell = ordinary(Path(shell))
    tools = {}
    for name in ("git", "node", "powershell", "pwsh"):
        executable = shutil.which(name)
        need(executable is not None, "host_tool_missing")
        executable = ordinary(Path(executable))
        tools[name] = {"path": str(executable), "sha256": digest(executable)}
    directories = sorted(
        {str(Path(value["path"]).parent) for value in tools.values()}
        | {str(Path(os.environ["SYSTEMROOT"]) / "System32")}
    )
    state = {
        **identity,
        "evidence_mode": EVIDENCE_MODE,
        "association_counts_complete": True,
        **dict.fromkeys(ASSOCIATION_FIELDS, 0),
        "bootstrap_path": str(bootstrap),
        "bootstrap_sha256": digest(bootstrap),
        "host_temp": str(parent),
        "tools": tools,
        "tool_directories": directories,
        "all_owned_cleanup": True,
        "pending_payload": False,
        "common_sha256": digest(ROOT / "scripts/installed_analytics_witness.py"),
        "wrapper_sha256": digest(Path(__file__)),
        "shell_path": str(shell),
        "shell_sha256": digest(shell),
        "started_at": time.time(),
        "cpu_seconds": 0,
        "peak_rss_bytes": 0,
        "finished_steps": [],
        "failed": True,
        "prepared": False,
    }
    for name in ("tmp", "localappdata", "appdata", "pip-cache"):
        (work / name).mkdir()
    save_state(work, state)
    # Publish ownership first so an always() finalizer can recover a failed setup.
    with Path(os.environ["GITHUB_ENV"]).open("a", encoding="utf-8") as output:
        output.write(f"K5_QUAL_ROOT={work}\n")
    os.environ["K5_QUAL_ROOT"] = str(work)
    try:
        env = child_environment(work, state)
        run_owned(
            [
                str(bootstrap),
                "-I",
                "-B",
                "-S",
                str(Path(__file__).resolve()),
                "--bootstrap-child",
                "--profile",
                profile,
            ],
            cwd=ROOT,
            env=env,
            work=work,
            state=state,
            seconds=360,
        )
        state["failed"], state["prepared"] = False, True
        state["dependency_seal"] = digest(work / "dependencies.json")
        with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
            output.write(f"python-path={work / 'python/python.exe'}\n")
    finally:
        save_state(work, state)


def step_child(profile, step, owned_script):
    # Environment is already job-isolated, so its temporary parent differs from
    # the supervisor's original RUNNER_TEMP. Read the immutable context explicitly.
    work = ordinary(os.environ["K5_QUAL_ROOT"], directory=True)
    state = load_json(work / "state.json")
    need(all(state.get(k) == v for k, v in context(profile).items()), "state_identity")
    need(digest(work / "dependencies.json") == state["dependency_seal"], "dependency_seal")
    verify_secondary_seals(work, state)
    verify_source(state)
    admit_absent_shell_profiles(state)
    script = ordinary(owned_script)
    need(script.parent.parent == work and script.name == "owned.ps1", "owned_script")
    steps = workflow_steps((ROOT / PROFILES[profile]).read_text())
    need(type(step) is int and 0 <= step < len(steps), "step_identity")
    selected = steps[step]
    need("scripts/windows_hosted_qualification.py" not in selected["body"], "nested_guard")
    expected = POWERSHELL_PREFIX + selected["body"] + POWERSHELL_SUFFIX
    need(script.read_bytes().decode("utf-8").replace("\r\n", "\n") == expected, "owned_body")
    shell = ordinary(state["shell_path"])
    need(digest(shell) == state["shell_sha256"], "shell_identity")
    runtime = work / "python/python.exe"
    specification = load_json(PROFILE_ROOT / "windows-hosted-profile.json")
    need(
        digest(runtime) == specification["runtime_files"]["python/python.exe"]["sha256"],
        "runtime_identity",
    )
    verify_runtime(work, specification)
    verify_environment_process(work / "venv/Scripts/python.exe", dict(os.environ))
    probe = work / "tmp/k5-alpha-runtime-probe/Scripts/python.exe"
    if probe.exists():
        need(
            digest(work / "runtime-dependencies.json") == state.get("runtime_dependency_seal"),
            "runtime_dependency_seal",
        )
        verify_environment_process(probe, dict(os.environ))
    os.environ["K5_WITNESS_BASE_PYTHON"] = str(runtime)
    os.environ["K5_WITNESS_BASE_PYTHON_SHA256"] = digest(runtime)
    result = subprocess.run(
        [str(shell), "-command", ". '" + str(script).replace("'", "''") + "'"],
        cwd=ROOT,
        check=False,
        timeout=selected["timeout"],
    )
    need(result.returncode == 0, "child_exit")
    verify_secondary_seals(work, state)
    verify_source(state)
    verify_runtime(work, specification)
    need(digest(work / "dependencies.json") == state["dependency_seal"], "dependency_seal")
    verify_environment_process(work / "venv/Scripts/python.exe", dict(os.environ))
    probe = work / "tmp/k5-alpha-runtime-probe/Scripts/python.exe"
    if probe.exists():
        verify_environment_process(probe, dict(os.environ))


def purge_owned_bytecode(prefix):
    # Isolated (-I) original tests may create caches despite the environment flag.
    # Caches are disposable: none is admitted as persistent dependency evidence.
    prefix = ordinary(prefix, directory=True)
    for path in prefix.rglob("*.pyc"):
        path = ordinary(path)
        need(path.is_relative_to(prefix), "bytecode_scope")
        if path.parent.name == "__pycache__":
            stem = path.name.split(".cpython-312", 1)
            need(len(stem) == 2 and stem[1] in {".pyc", ".opt-1.pyc", ".opt-2.pyc"}, "bytecode_tag")
            source = path.parent.parent / (stem[0] + ".py")
        else:
            source = path.with_suffix(".py")
        need(ordinary(source).is_relative_to(prefix), "bytecode_source")
        path.unlink()


def verify_runtime(work, specification):
    purge_owned_bytecode(work / "python")
    for name, identity in specification["runtime_files"].items():
        if name.endswith(".pyc"):
            continue
        path = ordinary(work / name)
        need(
            path.stat().st_size == identity["bytes"] and digest(path) == identity["sha256"],
            "runtime_payload",
        )
    expected = set(specification["runtime_files"])
    for path in (work / "python").rglob("*"):
        if path.is_file() and not path.is_relative_to(
            work / "python/qualification-retained-licenses"
        ):
            need(path.relative_to(work).as_posix() in expected, "runtime_extra_file")


def verify_source(state):
    for entry in state["tools"].values():
        need(digest(ordinary(entry["path"])) == entry["sha256"], "host_tool_identity")
    git = state["tools"]["git"]["path"]
    revision = subprocess.run(
        [git, "-C", str(ROOT), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    need(revision.stdout.strip() == state["source_sha"], "source_revision")
    result = subprocess.run(
        [
            git,
            "-C",
            str(ROOT),
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--quiet",
            state["source_sha"],
            "--",
        ],
        check=False,
        timeout=10,
    )
    need(result.returncode == 0, "source_dirty")
    result = subprocess.run(
        [
            git,
            "-C",
            str(ROOT),
            "ls-files",
            "--others",
            "--",
            "src",
            "scripts",
            "tests",
            ".github",
        ],
        check=True,
        capture_output=True,
        timeout=10,
    )
    need(not result.stdout.strip(), "source_untracked")


def admit_absent_shell_profiles(state):
    # Keep original -command behavior; refuse unexpected hooks instead of changing policy.
    user = Path(os.environ["USERPROFILE"]) / "Documents"
    directories = {
        Path(state["shell_path"]).parent,
        user / "WindowsPowerShell",
        user / "PowerShell",
    }
    for directory in directories:
        for name in ("profile.ps1", "Microsoft.PowerShell_profile.ps1"):
            need(not (directory / name).exists(), "shell_profile_unreviewed")


def finish(profile):
    work, state = read_state(profile, cleanup=True)
    association_evidence(state, require_complete=False)
    expected = workflow_steps((ROOT / PROFILES[profile]).read_text())
    guarded = [
        i
        for i, row in enumerate(expected)
        if "scripts/windows_hosted_qualification.py" not in row["body"]
    ]
    success = (
        state["prepared"]
        and state["association_counts_complete"]
        and not state["failed"]
        and state["finished_steps"] == guarded
        and 0 <= time.time() - state["started_at"] <= 900
    )
    # Every completed or refused step has already closed its own Job. No process
    # intentionally spans steps. Runtime/venv binaries are not used by this finalizer.
    need(
        state["all_owned_cleanup"] is True and state["pending_payload"] is False, "cleanup_unproven"
    )
    shutil.rmtree(work)
    need(not work.exists(), "temporary_cleanup")
    receipt = {
        **{
            k: state[k]
            for k in (
                "source_sha",
                "profile_sha256",
                "workflow_sha256",
                "run_id",
                "attempt",
                "peak_rss_bytes",
                "cpu_seconds",
                "evidence_mode",
                "association_counts_complete",
                *ASSOCIATION_FIELDS,
            )
        },
        "completed": bool(success),
        "cleanup_complete": True,
        "owned_active": 0,
        "owned_listeners": 0,
        "stage_count": len(state["finished_steps"]),
        "execution_boundary": (
            "owned step payloads only; read-only selector, PowerShell launcher, "
            "supervisor and transport excluded"
        ),
        "resource_evidence": (
            "sampled RSS and owned endpoints only; between-sample peaks and transient "
            "listeners unproven; association counters cover recovered final Job totals "
            "and are partial when association_counts_complete is false"
        ),
    }
    path = ROOT / f"hosted-{profile}-qualification.json"
    path.write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")
    print("K5_HOSTED_QUALIFICATION_RECEIPT=" + json.dumps(receipt, sort_keys=True))
    receipt_valid(receipt)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    for mode in ("prepare", "finish", "bootstrap-child", "step-child", "verify-environment"):
        modes.add_argument("--" + mode, action="store_true")
    parser.add_argument("--profile", choices=sorted(PROFILES))
    parser.add_argument("--step", type=int)
    parser.add_argument("--owned-script", type=Path)
    parser.add_argument("--environment-root", type=Path)
    parser.add_argument("generated", type=Path, nargs="?")
    args = parser.parse_args()
    try:
        if args.verify_environment:
            verify_environment(args.environment_root)
        elif args.prepare:
            prepare(args.profile)
        elif args.finish:
            finish(args.profile)
        elif args.bootstrap_child:
            bootstrap_child(args.profile)
        elif args.step_child:
            step_child(args.profile, args.step, args.owned_script)
        else:
            execute_step(args.profile, args.step, args.generated)
        return 0
    except BaseException as error:
        category = (
            str(error) if isinstance(error, (Refusal, ResourceFailure)) else "qualification_failed"
        )
        if not re.fullmatch(r"[a-z_]{1,64}", category):
            category = "qualification_failed"
        print("K5_HOSTED_QUALIFICATION_REFUSED=" + category, file=sys.stderr)
        return 1


"""LOCAL PROPOSAL ONLY: qualification Job sampler, not native-qualified.

The supervising Python process is outside payload accounting. No launches here:
pass job_factory to the existing gated OwnedProcess. Only original WindowsJob
assign/close own containment and termination. Polling cannot prove absence of
transient listeners or capture between-sample RSS peaks.
"""


GIB = 1024**3
RSS_LIMIT = 2 * GIB
COMMIT_LIMIT = 3 * GIB
ACTIVE_LIMIT = 32
SAMPLE_INTERVAL = 0.250
STALE_SECONDS = 1.0
MAX_SAMPLE_ATTEMPTS = 3
MAX_RETAINED_BIRTHS = 4096  # Evidence storage bound, not an active-process limit.
U32, U64, SIZE, HANDLE = c.c_uint32, c.c_uint64, c.c_size_t, c.c_void_p


class ResourceFailure(RuntimeError):
    pass


def resource_need(value, code="resource_evidence_unavailable"):
    if not value:
        raise ResourceFailure(code)


class BasicLimits(c.Structure):
    _fields_ = [
        ("process_time", c.c_int64),
        ("job_time", c.c_int64),
        ("flags", U32),
        ("min_ws", SIZE),
        ("max_ws", SIZE),
        ("active", U32),
        ("affinity", SIZE),
        ("priority", U32),
        ("scheduling", U32),
    ]


class ExtendedLimits(c.Structure):
    _fields_ = [
        ("basic", BasicLimits),
        ("io", U64 * 6),
        ("process_memory", SIZE),
        ("job_memory", SIZE),
        ("peak_process", SIZE),
        ("peak_job", SIZE),
    ]


class Accounting(c.Structure):
    _fields_ = [
        ("user", c.c_int64),
        ("kernel", c.c_int64),
        ("period_user", c.c_int64),
        ("period_kernel", c.c_int64),
        ("faults", U32),
        ("total", U32),
        ("active", U32),
        ("terminated", U32),
    ]


class ProcessIds(c.Structure):
    _fields_ = [("assigned", U32), ("count", U32), ("pids", SIZE * ACTIVE_LIMIT)]


class MemoryCounters(c.Structure):
    _fields_ = [
        ("cb", U32),
        ("faults", U32),
        ("peak_ws", SIZE),
        ("working_set", SIZE),
        ("peak_paged", SIZE),
        ("paged", SIZE),
        ("peak_nonpaged", SIZE),
        ("nonpaged", SIZE),
        ("pagefile", SIZE),
        ("peak_pagefile", SIZE),
    ]


class HostMemory(c.Structure):
    _fields_ = [
        ("length", U32),
        ("load", U32),
        ("total", U64),
        ("available", U64),
        ("total_page", U64),
        ("available_page", U64),
        ("total_virtual", U64),
        ("available_virtual", U64),
        ("available_extended", U64),
    ]


class Tcp4(c.Structure):
    _fields_ = [
        ("state", U32),
        ("local", c.c_ubyte * 4),
        ("port", U32),
        ("remote", U32),
        ("remote_port", U32),
        ("pid", U32),
    ]


class Tcp6(c.Structure):
    _fields_ = [
        ("local", c.c_ubyte * 16),
        ("scope", U32),
        ("port", U32),
        ("remote", c.c_ubyte * 16),
        ("remote_scope", U32),
        ("remote_port", U32),
        ("state", U32),
        ("pid", U32),
    ]


class Udp4(c.Structure):
    _fields_ = [("local", c.c_ubyte * 4), ("port", U32), ("pid", U32)]


class Udp6(c.Structure):
    _fields_ = [("local", c.c_ubyte * 16), ("scope", U32), ("port", U32), ("pid", U32)]


@dataclass(frozen=True)
class Snapshot:
    started_at: float
    measured_at: float
    rss_bytes: int
    job_cpu_seconds: float
    active_processes: int
    total_births: int
    host_available_bytes: int
    host_total_bytes: int
    host_cpu_percent: float
    disk_free_bytes: int
    owned_loopback_listeners: int
    # Outer supervisor, CI runner and unrelated processes are intentionally excluded.
    scope: str = "owned-job-payload-only"


class NativeMetrics:
    """Read-only observations except explicit limits on this newly created Job."""

    def __init__(self):
        resource_need(os.name == "nt", "windows_required")
        self.k = c.WinDLL("kernel32", use_last_error=True)
        self.ip = c.WinDLL("iphlpapi", use_last_error=True)
        definitions = {
            "GetCurrentProcess": ([], HANDLE),
            "IsProcessInJob": ([HANDLE, HANDLE, c.POINTER(c.c_int32)], c.c_int32),
            "OpenProcess": ([U32, c.c_int32, U32], HANDLE),
            "GetProcessTimes": ([HANDLE] + [c.c_void_p] * 4, c.c_int32),
            "WaitForSingleObject": ([HANDLE, U32], U32),
            "K32GetProcessMemoryInfo": ([HANDLE, c.c_void_p, U32], c.c_int32),
            "GetSystemTimes": ([c.c_void_p] * 3, c.c_int32),
            "GlobalMemoryStatusEx": ([c.c_void_p], c.c_int32),
            "GetDiskFreeSpaceExW": ([c.c_wchar_p] + [c.c_void_p] * 3, c.c_int32),
            "CloseHandle": ([HANDLE], c.c_int32),
            "GetHandleInformation": ([HANDLE, c.POINTER(U32)], c.c_int32),
            "QueryInformationJobObject": (
                [HANDLE, c.c_int, c.c_void_p, U32, c.c_void_p],
                c.c_int32,
            ),
            "SetInformationJobObject": ([HANDLE, c.c_int, c.c_void_p, U32], c.c_int32),
        }
        for name, (args, result) in definitions.items():
            getattr(self.k, name).argtypes = args
            getattr(self.k, name).restype = result
        self.ip.GetExtendedTcpTable.argtypes = [
            c.c_void_p,
            c.POINTER(U32),
            c.c_int32,
            U32,
            c.c_int,
            U32,
        ]
        self.ip.GetExtendedTcpTable.restype = U32
        self.ip.GetExtendedUdpTable.argtypes = [
            c.c_void_p,
            c.POINTER(U32),
            c.c_int32,
            U32,
            c.c_int,
            U32,
        ]
        self.ip.GetExtendedUdpTable.restype = U32

    def belongs(self, process, job):
        value = c.c_int32()
        resource_need(self.k.IsProcessInJob(process, job.handle, c.byref(value)))
        return bool(value.value)

    def parent_outside(self, job):
        resource_need(
            not self.belongs(self.k.GetCurrentProcess(), job), "supervisor_inside_payload_job"
        )

    def configure(self, job):
        limits = ExtendedLimits()
        # KILL_ON_JOB_CLOSE | JOB_MEMORY | ACTIVE_PROCESS, never breakaway flags.
        limits.basic.flags = 0x2000 | 0x200 | 0x8
        limits.basic.active, limits.job_memory = ACTIVE_LIMIT, COMMIT_LIMIT
        resource_need(
            self.k.SetInformationJobObject(job.handle, 9, c.byref(limits), c.sizeof(limits))
        )
        check = ExtendedLimits()
        resource_need(
            self.k.QueryInformationJobObject(job.handle, 9, c.byref(check), c.sizeof(check), None)
        )
        resource_need(
            check.basic.flags == limits.basic.flags
            and check.basic.active == ACTIVE_LIMIT
            and check.job_memory == COMMIT_LIMIT,
            "job_limits_unproven",
        )
        self.parent_outside(job)

    def accounting(self, job):
        value = Accounting()
        resource_need(
            self.k.QueryInformationJobObject(job.handle, 1, c.byref(value), c.sizeof(value), None)
        )
        resource_need(
            0 <= value.user
            and 0 <= value.kernel
            and value.active <= ACTIVE_LIMIT
            and value.total >= value.active
        )
        return value.total, value.active, (value.user + value.kernel) / 10_000_000

    def members(self, job):
        value = ProcessIds()
        resource_need(
            self.k.QueryInformationJobObject(job.handle, 3, c.byref(value), c.sizeof(value), None)
        )
        resource_need(value.assigned == value.count <= ACTIVE_LIMIT, "membership_incomplete")
        result = tuple(int(value.pids[n]) for n in range(value.count))
        resource_need(len(set(result)) == len(result) and all(0 < pid < 2**32 for pid in result))
        return set(result)

    def open_member(self, pid, job):
        # QUERY_INFORMATION | VM_READ | SYNCHRONIZE, for zero-time state reads only.
        # No terminate, write, injection or all-access rights.
        handle = self.k.OpenProcess(0x0400 | 0x0010 | 0x00100000, False, pid)
        resource_need(handle)
        try:
            resource_need(self.belongs(handle, job), "ownership_unproven")
            birth, _ = self.identity(handle)
            # A proven owned process may exit after the membership read. Keep its
            # handle until Job drain; the sampler must reconcile its departure.
            return handle, birth
        except BaseException:
            self.close_handle(handle)
            raise

    def identity(self, handle):
        birth, exit_time, kernel, user = (U64() for _ in range(4))
        resource_need(
            self.k.GetProcessTimes(
                handle, c.byref(birth), c.byref(exit_time), c.byref(kernel), c.byref(user)
            )
        )
        resource_need(birth.value > 0)
        # GetProcessTimes leaves exit_time undefined while the process is live.
        # Only the retained process object's signal state proves termination.
        state = self.k.WaitForSingleObject(handle, 0)
        resource_need(state in (0, 258), "process_wait_failed")
        return birth.value, state == 0

    def rss(self, handle):
        value = MemoryCounters()
        value.cb = c.sizeof(value)
        resource_need(self.k.K32GetProcessMemoryInfo(handle, c.byref(value), c.sizeof(value)))
        return value.working_set

    def host(self, disk_root):
        mem = HostMemory()
        mem.length = c.sizeof(mem)
        resource_need(self.k.GlobalMemoryStatusEx(c.byref(mem)))
        idle, kernel, user = (U64() for _ in range(3))
        resource_need(self.k.GetSystemTimes(c.byref(idle), c.byref(kernel), c.byref(user)))
        available, total, free = U64(), U64(), U64()
        resource_need(
            self.k.GetDiskFreeSpaceExW(
                str(disk_root), c.byref(available), c.byref(total), c.byref(free)
            )
        )
        resource_need(
            0 < mem.total and 0 <= mem.available <= mem.total and available.value <= total.value
        )
        return mem.available, mem.total, (idle.value, kernel.value, user.value), available.value

    def listeners(self, owned_pids):
        # Windows exposes a global table. Foreign rows are neither returned nor logged.
        result = set()
        tables = (
            ("tcp", 2, Tcp4, self.ip.GetExtendedTcpTable, 3),
            ("tcp", 23, Tcp6, self.ip.GetExtendedTcpTable, 3),
            ("udp", 2, Udp4, self.ip.GetExtendedUdpTable, 1),
            ("udp", 23, Udp6, self.ip.GetExtendedUdpTable, 1),
        )
        for protocol, family, row_type, query, table_class in tables:
            size = U32()
            status = query(None, c.byref(size), False, family, table_class, 0)
            resource_need(status in (0, 122) and 4 <= size.value <= 4 * 1024 * 1024)
            capacity = size.value
            buffer = c.create_string_buffer(capacity)
            resource_need(query(buffer, c.byref(size), False, family, table_class, 0) == 0)
            resource_need(4 <= size.value <= capacity)
            count = U32.from_buffer_copy(buffer.raw[:4]).value
            stride = c.sizeof(row_type)
            resource_need(4 + count * stride <= size.value)
            for n in range(count):
                row = row_type.from_buffer_copy(buffer.raw[4 + n * stride : 4 + (n + 1) * stride])
                if row.pid not in owned_pids:
                    continue
                address = ipaddress.ip_address(bytes(row.local))
                resource_need(
                    (protocol != "tcp" or row.state == 2) and address.is_loopback,
                    "owned_listener_not_loopback",
                )
                port = int.from_bytes(int(row.port).to_bytes(4, "little")[:2], "big")
                resource_need(0 < port < 65536)
                # Preserve TCP record shape; UDP is a bound endpoint, not TCP LISTEN.
                endpoint = (row.pid, family, bytes(row.local), port)
                result.add(endpoint if protocol == "tcp" else (*endpoint, "udp"))
        return result

    def assert_job_handle_closed(self, job):
        flags = U32()
        present = self.k.GetHandleInformation(job.handle, c.byref(flags))
        resource_need(not present and c.get_last_error() == 6, "owned_job_handle_cleanup_failed")

    def close_handle(self, handle):
        resource_need(self.k.CloseHandle(handle), "resource_handle_cleanup_failed")


class QualificationResources:
    """Explicit factory adapter. Caller schedules samples and enforces host policy.

    No background thread is hidden here. The caller schedules 250ms sleeps;
    acquisition time adds to that interval. Call assert_fresh immediately before
    using evidence/gate decisions. The 1s limit is hard staleness, not a claim of
    a hard 250ms start-to-start interval. All measurements are local
    proposal evidence; sampled RSS is not a kernel resident-memory hard cap.
    """

    def __init__(
        self,
        common,
        disk_root,
        *,
        clock=time.monotonic,
        native_factory=NativeMetrics,
        before_release=None,
    ):
        resource_need(before_release is None or callable(before_release), "invalid_before_release")
        self.before_release = before_release
        self.common, self.disk_root, self.clock = common, disk_root, clock
        self.native = native_factory()
        self.job = None
        self.closed = self.failed = False
        self.cleanup_complete = False
        self.observed_peak_rss_bytes = 0
        self.final_cpu_seconds = self.final_active = self.final_total = None
        self.handles = {}  # pid -> (read-only process handle, creation FILETIME)
        # A handle alone is not resource evidence. Only complete live identity,
        # RSS and listener observations can charge a lifetime Job association.
        self.witnessed_births = set()  # (pid, creation FILETIME), retained through drain.
        self.birth_evidence_complete = False
        self.association_counts_complete = self.association_counts_recorded = False
        self.terminal_evidence_failure = None
        self.observed_listeners = set()
        self.last_start = self.last_cpu = self.last_total = None
        self.host_seed_at = clock()
        self.host_seed = self.native.host(disk_root)[2]

    def job_factory(self):
        resource_need(self.job is None and not self.closed and not self.failed, "factory_reused")
        owner, base = self, self.common.WindowsJob

        class QualificationJob(base):
            def __init__(self):
                super().__init__()
                owner.job = self
                try:
                    owner.native.configure(self)
                except BaseException:
                    owner.failed = True
                    owner.close()
                    raise

            def assign(self, process):
                resource_need(not owner.failed and not owner.closed, "resource_guard_failed")
                owner.native.parent_outside(self)
                base.assign(self, process)  # Existing OwnedProcess opens gate afterwards.
                try:
                    owner.native.parent_outside(self)
                    if owner.before_release is not None:
                        owner.before_release(owner)
                    resource_need(not owner.failed and not owner.closed, "resource_guard_failed")
                except BaseException:
                    owner.abort()
                    raise

            def accounting(self):
                # Original close calls this while its Job handle is still valid.
                state = base.accounting(self)
                owner.final_cpu_seconds = (
                    state.total_user_time + state.total_kernel_time
                ) / 10_000_000
                owner.final_active = state.active_processes
                owner.final_total = state.total_processes
                resource_need(
                    owner.final_cpu_seconds >= 0
                    and (owner.last_cpu is None or owner.final_cpu_seconds >= owner.last_cpu)
                    and (owner.last_total is None or owner.final_total >= owner.last_total)
                )
                return state

            def close(self):
                owner.close()

        return QualificationJob()

    def abort(self):
        self.failed = True
        self.close()

    def assert_fresh(self, snapshot):
        try:
            resource_need(not self.failed and not self.closed, "resource_guard_failed")
            age = self.clock() - snapshot.started_at
            resource_need(0 <= age <= STALE_SECONDS, "resource_evidence_stale")
        except BaseException:
            self.abort()
            raise

    def sample(self):
        try:
            return self._sample()
        except BaseException:
            self.abort()
            raise

    def _sample(self):
        resource_need(
            self.job is not None and not self.failed and not self.closed, "resource_guard_failed"
        )
        started = self.clock()
        if self.last_start is not None:
            resource_need(0 < started - self.last_start <= STALE_SECONDS, "resource_evidence_stale")
        resource_need(0 < started - self.host_seed_at <= STALE_SECONDS, "host_cpu_evidence_stale")
        self.native.parent_outside(self.job)

        def fresh():
            resource_need(0 <= self.clock() - started <= STALE_SECONDS, "resource_evidence_stale")

        def accounting():
            observed = self.native.accounting(self.job)
            resource_need(
                (self.last_cpu is None or observed[2] >= self.last_cpu)
                and (self.last_total is None or observed[0] >= self.last_total)
            )
            # Preserve every cumulative observation, including discarded attempts,
            # so final Job drain must also account for the exited children.
            self.last_total, self.last_cpu = observed[0], observed[2]
            return observed

        def exited(pid):
            handle, birth = self.handles[pid]
            resource_need(self.native.belongs(handle, self.job), "ownership_unproven")
            actual, signaled = self.native.identity(handle)
            resource_need(actual == birth, "pid_reuse_or_exit")
            return signaled

        rss, listeners = 0, set()

        def witnessed(pid):
            return pid in self.handles and (pid, self.handles[pid][1]) in self.witnessed_births

        def audit(members, *, prior_rss=0):
            nonlocal rss
            departed, new_births, attempt_rss = set(), set(), prior_rss
            for pid in sorted(members):
                if pid not in self.handles:
                    resource_need(len(self.handles) < MAX_RETAINED_BIRTHS, "birth_evidence_limit")
                    self.handles[pid] = self.native.open_member(pid, self.job)
                known = witnessed(pid)
                signaled = exited(pid)
                resource_need(known or not signaled, "new_birth_already_exited")
                if signaled:
                    departed.add(pid)
                else:
                    # API errors are fatal; CPU/commit cannot replace live RSS.
                    attempt_rss += self.native.rss(self.handles[pid][0])
                    self.observed_peak_rss_bytes = max(self.observed_peak_rss_bytes, attempt_rss)
                    resource_need(attempt_rss <= RSS_LIMIT, "owned_rss_exceeded")
                    if not known:
                        new_births.add(pid)
            rss = max(rss, attempt_rss)
            # All retained PIDs are still bound by their exact process handles.
            # Include departures and preserve evidence from every acquisition pass.
            listeners.update(self.native.listeners(set(self.handles)))
            self.observed_listeners.update(listeners)
            resource_need(len(self.observed_listeners) <= 4096, "listener_evidence_limit")
            completed = set()
            for pid in members:
                signaled = exited(pid)
                resource_need(pid not in departed or signaled, "pid_reuse_or_exit")
                resource_need(
                    pid not in new_births or not signaled, "new_birth_observation_incomplete"
                )
                if signaled:
                    departed.add(pid)
                elif pid in new_births:
                    completed.add((pid, self.handles[pid][1]))
            # Never promote a partially measured or already-exited new handle.
            self.witnessed_births.update(completed)
            return departed, attempt_rss

        remaining_members = None
        initial_allowance = None
        for attempt in range(MAX_SAMPLE_ATTEMPTS):
            fresh()
            before = accounting()
            members = self.native.members(self.job)
            for pid, birth in self.witnessed_births:
                resource_need(pid in self.handles, "departure_handle_missing")
                resource_need(self.handles[pid][1] == birth, "pid_reuse_or_exit")
            resource_need(len(members) <= ACTIVE_LIMIT, "initial_members_exceed_limit")
            if remaining_members is not None:
                # Previously departed PIDs cannot reappear under retained handles.
                resource_need(
                    not (members - remaining_members) & self.handles.keys(),
                    "retry_member_set_expanded",
                )
            for pid in self.handles.keys() - members:
                resource_need(exited(pid), "departure_not_signaled")
            unobserved = {pid for pid in members if not witnessed(pid)}
            if initial_allowance is None:
                # Initial accounting and membership are non-atomic. This covers
                # unobserved-or-rejected associations in that initial census,
                # without claiming they all predate acquisition. Freeze it here:
                # later attempts may never forgive additional unexplained births.
                initial_allowance = max(0, before[0] - len(self.witnessed_births) - len(unobserved))
            # Accounting precedes the non-atomic membership read. Live arrivals
            # may outnumber that older count, but excess above the frozen
            # allowance is never retryable or charged to a future arrival.
            resource_need(
                before[0] - len(self.witnessed_births) - initial_allowance <= len(unobserved),
                "initial_birth_evidence_gap",
            )
            departed, attempt_rss = audit(members)
            after = accounting()
            survivors = self.native.members(self.job)
            newcomers = survivors - members
            resource_need(not survivors & departed, "departed_member_still_listed")
            resource_need(members - departed <= survivors, "final_survivor_missing")
            resource_need(len(survivors) <= ACTIVE_LIMIT, "final_members_exceed_limit")
            resource_need(
                after[0] - len(self.witnessed_births) - initial_allowance <= len(newcomers),
                "final_birth_evidence_gap",
            )
            resource_need(not newcomers & self.handles.keys(), "new_birth_identity_reappeared")
            for pid in survivors - newcomers:
                resource_need(not exited(pid), "final_survivor_signaled")
            if newcomers:
                # Charge every counter increment with full live evidence before
                # retrying. Add to the earlier sum conservatively; a later smaller
                # census cannot erase observed resident memory or listener evidence.
                new_departures, _ = audit(newcomers, prior_rss=attempt_rss)
                resource_need(not new_departures, "new_birth_observation_incomplete")
            fresh()
            counts_match = before[1] == len(members) and after[1] == len(survivors)
            if not departed and not newcomers and counts_match and after[0] == before[0]:
                resource_need(
                    after[0] == len(self.witnessed_births) + initial_allowance,
                    "final_birth_evidence_gap",
                )
                break
            # Re-read non-atomic counts only after every visible live arrival was
            # fully witnessed and every missing retained handle proved exited.
            # Never reset lifetime count gaps or the original acquisition deadline.
            if attempt + 1 == MAX_SAMPLE_ATTEMPTS:
                resource_need(before[1] == len(members), "initial_active_count_mismatch")
                resource_need(after[1] == len(survivors), "final_active_count_mismatch")
                raise ResourceFailure("resource_evidence_unstable")
            remaining_members = survivors
        available, total, times, disk = self.native.host(self.disk_root)
        delta = tuple(b - a for a, b in zip(self.host_seed, times, strict=True))
        elapsed = delta[1] + delta[2]  # GetSystemTimes kernel includes idle.
        resource_need(all(x >= 0 for x in delta) and elapsed > 0 and delta[0] <= elapsed)
        cpu = 100.0 * (elapsed - delta[0]) / elapsed
        measured = self.clock()
        resource_need(0 <= measured - started <= STALE_SECONDS, "resource_evidence_stale")
        resource_need(rss <= RSS_LIMIT, "owned_rss_exceeded")
        snapshot = Snapshot(
            started,
            measured,
            rss,
            after[2],
            len(members),
            after[0],
            available,
            total,
            cpu,
            disk,
            len(listeners),
        )
        self.last_start, self.last_cpu = started, after[2]
        self.host_seed, self.host_seed_at = times, measured
        return snapshot

    def close(self):
        if self.closed:
            resource_need(self.cleanup_complete, "owned_job_cleanup_failed")
            return
        failures = []
        try:
            if self.job is not None:
                try:
                    # Original Job's TerminateJobObject + accounting drain + CloseHandle.
                    self.common.WindowsJob.close(self.job)
                    self.native.assert_job_handle_closed(self.job)
                    resource_need(
                        self.final_active == 0 and self.final_cpu_seconds is not None,
                        "owned_job_drain_unproven",
                    )
                except BaseException:
                    failures.append("owned_job_cleanup_failed")
                try:
                    # Retained process handles bind PIDs through owned Job drain.
                    resource_need(
                        not self.native.listeners(set(self.handles)),
                        "owned_listener_cleanup_failed",
                    )
                except BaseException:
                    failures.append("owned_listener_cleanup_failed")
        finally:
            for handle, _ in self.handles.values():
                try:
                    self.native.close_handle(handle)
                except BaseException:
                    failures.append("resource_handle_cleanup_failed")
            self.handles.clear()
            self.closed = True
            self.cleanup_complete = not failures
        self.association_counts_complete = (
            self.job is not None
            and type(self.final_total) is int
            and len(self.witnessed_births) <= self.final_total < 2**32
            and type(self.final_active) is int
            and self.final_active == 0
            and (self.last_total is None or self.final_total >= self.last_total)
        )
        self.birth_evidence_complete = self.association_counts_complete and self.final_total == len(
            self.witnessed_births
        )
        if self.job is not None and not self.association_counts_complete:
            self.failed = True
            self.terminal_evidence_failure = "association_counts_unavailable"
        if failures:
            self.failed = True
            raise ResourceFailure(failures[0])


if __name__ == "__main__":
    raise SystemExit(main())
