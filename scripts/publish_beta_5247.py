"""One-off, fixed-input beta publisher. Only run in the reviewed Actions workflow."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = "mkurtgerald/K5-Vision"
SOURCE = "5247fd96b827129c010bfcf1e924824535f4d1c2"
TREE = "6aa426c8f7899227a74ab4a4ab8c4de3e7ecd43e"
TAG = "k5-windows-beta-5247fd9"
SETUP_SHA = "68690ee4f6a18321af7bde22ae679c196d562608c1e3b468f6a78dc2480dc01f"
SETUP_BYTES = 286590924
QUALIFIED_ARTIFACT = 14950059406
QUALIFIED_BYTES = 285172299
QUALIFIED_SHA = "b24ef4840899f56a7a112754322d5597ae14b8d1f757a16d299ed85819f426d7"
GUI_JOB = 138667612998
GUI_RECEIPT_SHA = "2f9ec885af559a2abbaa212bc3a952481cb182a002b6deadb0230b3e0480e12f"
GUIDE_SHA = "8143b7299545665eb961079bd85e372128598b9ca6b93326b38becac2428de98"
MEMBERS = {
    "K5VisionSetup.exe": (SETUP_BYTES, SETUP_SHA),
    "desktop-package.json": (
        1688,
        "329c98c0513be938c2e84883a0df79afafbdd9301d7edf45658e9e40b6d2cce7",
    ),
    "payload-manifest.json": (
        605819,
        "d40ab7e9c4dba36a85004914ffbd5012176d38479baa3a2022a0ef5249e1b9eb",
    ),
    "desktop-windows-receipt.json": (8649, GUI_RECEIPT_SHA),
    "dependency-inventory.json": (
        89727,
        "247d9ef520a5431aa3536b9e3e215c37256ba8c0caeeb8e9bd8699db6e23c1e1",
    ),
}
# Exact full planned guest run independently admitted in proof3598da3e.
GUEST_RUN = 45459461934
GUEST_HEAD = "bc30f708601cf5718ac2db8a9bc56be7e8633ca9"
GUEST_JOB = 138677743651
GUEST_ARTIFACT = 14952917035
GUEST_BYTES = 4325
GUEST_SHA = "32db46c1fff227e8926dc3444ca690a97aa24dc424afefacc610d0d2b8b435e8"
GUEST_RECEIPT_SHA = "82b0bdbc275afb16a3019e728bb6f8481d45515133f6b49d648205f80d3e7422"
GUEST_PROVENANCE_SHA = "2da939c3654142df09a7752f3d4e103ae7610de65260cbebf1bcda776b38a91e"
GUEST_SOURCES = {
    "probe_windows_guest_recording.ps1": (
        "ab0152dfd13ad5ce4093e3d7692f16ce6ba6c78e2b846e722ed5d4bea00a83f1"
    ),
    "windows_guest_witness.ps1": "0c95c4a387964a76f4aee9a26b991646b187bc0d02b39c776c94ca02909129a6",
    "windows_guest_bootstrap.cs": (
        "b8ef1a885d876d0a66833b3350221bd66794f972843e418815628278b9163909"
    ),
    "windows_guest_bootstrap_controls.cs": (
        "4757e92ab04d387d9d04f0572949c1224dbfa2f25222cb5680290577ec30c837"
    ),
    "windows_guest_boot_sequence_controls.ps1": (
        "84204b182165214c3cf46558ff812c87892b6213a0529d7641a2ea3606cb48c0"
    ),
    "windows_guest_logon_identity_controls.ps1": (
        "7cdaac46bfb21e52a0fe717683adc52f3192ccf860d7cc2561acfbb89482f1fb"
    ),
}
RUNS = {
    45456600775: (
        "operator-desktop-windows.yml",
        {
            138667612998,
            138667613486,
            138667613514,
            138667613667,
            138673765033,
            138673765153,
            138673765194,
            138673765316,
            138673765320,
            138673765365,
            138673765680,
        },
    ),
    45456600704: (
        "operator-beta-windows.yml",
        {
            138667613891,
            138667614400,
            138667614490,
            138667614520,
            138667614562,
            138667614611,
            138672834317,
        },
    ),
    45456600720: ("ci.yml", {138667612769}),
    45456600677: (
        "windows-service-compile.yml",
        {
            138667612199,
            138667612574,
            138667612670,
            138667612696,
            138667612731,
            138667612783,
            138667612800,
        },
    ),
    45456600732: ("operator-product-windows.yml", None),
}
ROOT = Path(__file__).resolve().parents[1]


def require(condition: bool, label: str) -> None:
    if not condition:
        raise RuntimeError(label)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def guest_constants_ready() -> None:
    for value in (GUEST_JOB, GUEST_ARTIFACT, GUEST_BYTES):
        require(type(value) is int and value > 0, "Guest admission is PENDING")
    for value in (GUEST_SHA, GUEST_RECEIPT_SHA, GUEST_PROVENANCE_SHA):
        require(
            isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None,
            "Guest admission is PENDING",
        )


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHub:
    def __init__(self, token: str):
        require(bool(token), "Missing ephemeral workflow token")
        self.token = token
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(
        self,
        route: str,
        *,
        method: str = "GET",
        data: bytes | None = None,
        content_type: str = "application/json",
    ):
        require(route.startswith("/") and not route.startswith("//"), "Invalid API route")
        request = urllib.request.Request(
            "https://api.github.com" + route,
            data=data,
            method=method,
            headers={
                "Authorization": "Bearer " + self.token,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": content_type,
                "User-Agent": "K5-fixed-beta-publisher",
            },
        )
        return self.opener.open(request, timeout=120)

    def json(self, route: str, *, method: str = "GET", body=None, missing_ok: bool = False):
        try:
            with self.request(
                route, method=method, data=None if body is None else json.dumps(body).encode()
            ) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 404 and missing_ok:
                return None
            raise RuntimeError("GitHub API request failed: " + str(error.code)) from None

    def download(self, route: str, path: Path, maximum: int) -> None:
        # Artifact/log redirects are signed URLs. Never forward the API bearer token.
        try:
            response = self.request(route)
        except urllib.error.HTTPError as error:
            require(error.code in (301, 302, 303, 307, 308), "Expected artifact redirect")
            location = error.headers.get("Location", "")
            require(urllib.parse.urlsplit(location).scheme == "https", "Non-HTTPS download")
            response = urllib.request.urlopen(
                urllib.request.Request(location, headers={"User-Agent": "K5-fixed-beta-publisher"}),
                timeout=120,
            )
        with response, path.open("wb") as output:
            total = 0
            while block := response.read(1024 * 1024):
                total += len(block)
                require(total <= maximum, "Download exceeds fixed size bound")
                output.write(block)

    def upload(self, release: dict, path: Path) -> dict:
        prefix = f"https://uploads.github.com/repos/{REPO}/releases/{release['id']}/assets"
        require(release.get("upload_url", "").split("{")[0] == prefix, "Unexpected upload endpoint")
        request = urllib.request.Request(
            prefix + "?" + urllib.parse.urlencode({"name": path.name}),
            data=path.read_bytes(),
            method="POST",
            headers={
                "Authorization": "Bearer " + self.token,
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/octet-stream",
                "User-Agent": "K5-fixed-beta-publisher",
            },
        )
        with self.opener.open(request, timeout=240) as response:
            return json.load(response)


def api_path(suffix: str) -> str:
    return f"/repos/{REPO}/{suffix}"


def admit_run(api: GitHub, run_id: int, head: str, workflow: str) -> dict:
    run = api.json(api_path(f"actions/runs/{run_id}"))
    require(
        run.get("id") == run_id and run.get("run_attempt") == 1 and run.get("head_sha") == head,
        "Run source mismatch",
    )
    require(
        run.get("path") == ".github/workflows/" + workflow
        and run.get("status") == "completed"
        and run.get("conclusion") == "success",
        "Run has not passed",
    )
    require(run.get("repository", {}).get("full_name") == REPO, "Run repository mismatch")
    jobs = api.json(api_path(f"actions/runs/{run_id}/attempts/1/jobs?per_page=100"))
    rows = jobs.get("jobs", [])
    require(jobs.get("total_count") == len(rows) and len(rows) <= 100, "Incomplete job census")
    return {"run": run, "jobs": rows}


def admit_normal_runs(api: GitHub, work: Path) -> list[dict]:
    records = []
    for run_id, (workflow, expected_ids) in RUNS.items():
        admitted = admit_run(api, run_id, SOURCE, workflow)
        jobs = admitted["jobs"]
        if expected_ids is None:
            require(len(jobs) == 1, "Product job census mismatch")
        else:
            require(
                {job.get("id") for job in jobs} == expected_ids and len(jobs) == len(expected_ids),
                "Normal job census mismatch",
            )
        require(
            all(
                job.get("status") == "completed" and job.get("conclusion") == "success"
                for job in jobs
            ),
            "Normal job has not passed",
        )
        records.append(
            {
                "run_id": run_id,
                "attempt": 1,
                "workflow": workflow,
                "head_sha": SOURCE,
                "jobs": [
                    {"id": row["id"], "name": row["name"], "conclusion": row["conclusion"]}
                    for row in jobs
                ],
            }
        )
    commit = api.json(api_path(f"git/commits/{SOURCE}"))
    require(
        commit.get("sha") == SOURCE and commit.get("tree", {}).get("sha") == TREE,
        "Product source tree mismatch",
    )
    log = work / "ci.log"
    api.download(api_path("actions/jobs/138667612769/logs"), log, 16 * 1024 * 1024)
    text = log.read_text(encoding="utf-8", errors="replace")
    require(
        re.search(r"\b8823 passed, 369 skipped,", text) is not None
        and "Total coverage: 83.38%" in text,
        "Actual CI counts mismatch",
    )
    log.unlink()
    return records


def admit_artifact(
    api: GitHub, artifact_id: int, run_id: int, head: str, size: int, sha: str
) -> None:
    artifact = api.json(api_path(f"actions/artifacts/{artifact_id}"))
    require(
        artifact.get("id") == artifact_id
        and artifact.get("expired") is False
        and artifact.get("size_in_bytes") == size,
        "Artifact identity mismatch",
    )
    require(
        artifact.get("workflow_run", {}).get("id") == run_id
        and artifact.get("workflow_run", {}).get("head_sha") == head,
        "Artifact source mismatch",
    )
    require(artifact.get("digest") == "sha256:" + sha, "Artifact API digest mismatch")


def read_fixed_directory(directory: Path, members: dict[str, tuple[int, str]]) -> dict[str, bytes]:
    require(
        directory.is_dir() and {item.name for item in directory.iterdir()} == set(members),
        "Expanded input members mismatch",
    )
    result = {}
    for name, (size, sha) in members.items():
        item = directory / name
        require(
            item.is_file()
            and not item.is_symlink()
            and item.stat().st_size == size
            and hash_file(item) == sha,
            "Expanded input byte mismatch",
        )
        if name != "K5VisionSetup.exe":
            result[name] = item.read_bytes()
    return result


def admit_package(api: GitHub, directory: Path, assets: Path) -> dict:
    admit_artifact(api, QUALIFIED_ARTIFACT, 45456600775, SOURCE, QUALIFIED_BYTES, QUALIFIED_SHA)
    members = read_fixed_directory(directory, MEMBERS)
    package = json.loads(members["desktop-package.json"])
    require(
        package.get("head_sha") == SOURCE
        and package.get("setup_sha256") == SETUP_SHA
        and package.get("setup_bytes") == SETUP_BYTES
        and package.get("payload_manifest_sha256") == MEMBERS["payload-manifest.json"][1],
        "Package binding mismatch",
    )
    wrapper = json.loads(members["desktop-windows-receipt.json"])
    require(
        wrapper.get("head_sha") == SOURCE
        and wrapper.get("lane") == "desktop"
        and wrapper.get("receipt", {}).get("native_windows_execution") is True
        and wrapper.get("receipt", {}).get("owned_cleanup") is True,
        "Graphical receipt mismatch",
    )
    shutil.copyfile(directory / "K5VisionSetup.exe", assets / "K5VisionSetup.exe")
    return {
        "artifact_id": QUALIFIED_ARTIFACT,
        "archive_bytes": QUALIFIED_BYTES,
        "archive_sha256": QUALIFIED_SHA,
        "gui_job_id": GUI_JOB,
        "gui_receipt_sha256": GUI_RECEIPT_SHA,
        "gui_duration_seconds": 355.33,
        "payload_manifest_sha256": MEMBERS["payload-manifest.json"][1],
    }


def admit_guest(api: GitHub, directory: Path) -> dict:
    admitted = admit_run(api, GUEST_RUN, GUEST_HEAD, "windows-guest-boot.yml")
    jobs = [row for row in admitted["jobs"] if row.get("id") == GUEST_JOB]
    require(
        len(jobs) == 1
        and jobs[0].get("status") == "completed"
        and jobs[0].get("conclusion") == "success",
        "Planned guest job has not passed",
    )
    admit_artifact(api, GUEST_ARTIFACT, GUEST_RUN, GUEST_HEAD, GUEST_BYTES, GUEST_SHA)
    members = read_fixed_directory(
        directory,
        {
            "windows-guest-recording.json": (10706, GUEST_RECEIPT_SHA),
            "windows-guest-recording-provenance.json": (2696, GUEST_PROVENANCE_SHA),
        },
    )
    receipt = json.loads(members["windows-guest-recording.json"])
    provenance = json.loads(members["windows-guest-recording-provenance.json"])
    for row in (receipt, provenance):
        require(
            row.get("product_head") == SOURCE and row.get("installer_sha256") == SETUP_SHA,
            "Guest product binding mismatch",
        )
        require(
            row.get("diagnostic_only") is True and row.get("product_qualification") is False,
            "Guest observation scope mismatch",
        )
        require(row.get("reboot_operation") == "planned-shutdown-start", "Guest operation mismatch")
    for flag in (
        "cleanup_complete",
        "actual_guest_boot",
        "recording_after_reboot_before_login",
        "recording_after_actual_signout",
        "retained_media_decoded",
        "postboot_prelogin_media_decoded",
        "reboot_operation_executed",
        "planned_shutdown_off_observed",
        "planned_start_running_observed",
        "planned_native_config_custody_controls_passed",
        "planned_final_off_boundary_verified",
        "planned_boundary_dismounted_before_start",
    ):
        require(receipt.get(flag) is True, "Guest observation incomplete: " + flag)
    require(
        receipt.get("github_credentials_entered_guest") is False
        and receipt.get("retained_decoded_frames") == 10
        and receipt.get("postboot_decoded_frames") == 10,
        "Guest decoded footage mismatch",
    )
    require(
        provenance.get("harness_head") == GUEST_HEAD
        and str(provenance.get("run_id")) == str(GUEST_RUN)
        and str(provenance.get("run_attempt")) == "1",
        "Guest provenance mismatch",
    )
    require(
        all(
            provenance.get("source_hashes", {}).get(name) == sha
            for name, sha in GUEST_SOURCES.items()
        ),
        "Guest source controls mismatch",
    )
    controls = provenance.get("native_logon_identity_controls", {})
    require(
        controls.get("contract_controls_passed") == 165
        and controls.get("hard_reset_exemption_enabled") is False
        and controls.get("human_or_unclassified_interactive_refusals_preserved") is True,
        "Guest logon controls mismatch",
    )
    return {
        "run_id": GUEST_RUN,
        "attempt": 1,
        "job_id": GUEST_JOB,
        "harness_head": GUEST_HEAD,
        "artifact_id": GUEST_ARTIFACT,
        "archive_sha256": GUEST_SHA,
        "receipt_sha256": GUEST_RECEIPT_SHA,
        "provenance_sha256": GUEST_PROVENANCE_SHA,
        "scope": (
            "disposable guest planned shutdown/start and actual sign-out recording observation"
        ),
        "diagnostic_only": True,
        "product_qualification": False,
        "before_login_recording_observed": True,
        "actual_signout_recording_observed": True,
        "decoded_frames_each": 10,
        "cleanup_complete": True,
        "hard_reset_qualified": False,
        "source_controls": 6,
        "logon_identity_controls": 165,
    }


def asset_map(assets: Path) -> dict[str, tuple[int, str]]:
    return {
        path.name: (path.stat().st_size, hash_file(path))
        for path in sorted(assets.iterdir())
        if path.is_file()
    }


def verify_assets(
    rows: list[dict], expected: dict[str, tuple[int, str]], *, complete: bool
) -> None:
    names = [row.get("name") for row in rows]
    require(
        len(names) == len(set(names)) and set(names) <= set(expected), "Unexpected release assets"
    )
    if complete:
        require(set(names) == set(expected), "Release assets incomplete")
    for row in rows:
        size, sha = expected[row["name"]]
        require(
            row.get("size") == size
            and row.get("digest") == "sha256:" + sha
            and row.get("state") == "uploaded",
            "Release asset verification failed",
        )


def admit_tag(api: GitHub, *, required: bool = False) -> None:
    ref = api.json(api_path("git/ref/tags/" + TAG), missing_ok=not required)
    if ref is None:
        return
    obj = ref.get("object", {})
    for _ in range(3):
        if obj.get("type") != "tag":
            break
        obj = api.json(api_path("git/tags/" + obj["sha"])).get("object", {})
    require(
        obj.get("type") == "commit" and obj.get("sha") == SOURCE, "Existing tag target mismatch"
    )


def publish(api: GitHub, assets: Path, notes: str) -> dict:
    expected = asset_map(assets)
    require(
        set(expected)
        == {
            "K5VisionSetup.exe",
            "Windows-Beta-Quick-Start.txt",
            "beta-validation.json",
            "SHA256SUMS",
        },
        "Public asset set mismatch",
    )
    admit_tag(api)
    release = api.json(api_path("releases/tags/" + TAG), missing_ok=True)
    if release is None:
        release = api.json(
            api_path("releases"),
            method="POST",
            body={
                "tag_name": TAG,
                "target_commitish": SOURCE,
                "name": "K5 Vision Windows beta — 5247fd96",
                "body": notes,
                "draft": True,
                "prerelease": True,
                "make_latest": "false",
            },
        )
    require(
        release.get("tag_name") == TAG
        and release.get("target_commitish") == SOURCE
        and release.get("prerelease") is True,
        "Release target mismatch",
    )
    require(release.get("body") == notes, "Release notes mismatch")
    rows = api.json(api_path(f"releases/{release['id']}/assets?per_page=100"))
    verify_assets(rows, expected, complete=release.get("draft") is not True)
    if release.get("draft") is True:
        present = {row["name"] for row in rows}
        for name in expected:
            if name not in present:
                uploaded = api.upload(release, assets / name)
                verify_assets([uploaded], expected, complete=False)
        rows = api.json(api_path(f"releases/{release['id']}/assets?per_page=100"))
        verify_assets(rows, expected, complete=True)
        release = api.json(
            api_path(f"releases/{release['id']}"),
            method="PATCH",
            body={"draft": False, "prerelease": True, "make_latest": "false"},
        )
    require(release.get("draft") is False, "Release not published")
    admit_tag(api, required=True)
    final = api.json(api_path(f"releases/{release['id']}"))
    require(
        final.get("draft") is False
        and final.get("tag_name") == TAG
        and final.get("prerelease") is True,
        "Final release mismatch",
    )
    verify_assets(
        api.json(api_path(f"releases/{release['id']}/assets?per_page=100")), expected, complete=True
    )
    return {
        "release_url": final["html_url"],
        "tag": TAG,
        "source": SOURCE,
        "assets": {name: {"bytes": size, "sha256": sha} for name, (size, sha) in expected.items()},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qualified-dir", type=Path, required=True)
    parser.add_argument("--guest-dir", type=Path, required=True)
    parser.add_argument("--publish", action="store_true")
    options = parser.parse_args()
    guest_constants_ready()  # Must precede token access, downloads and EVERY mutation.
    require(
        os.environ.get("GITHUB_REPOSITORY") == REPO
        and os.environ.get("GITHUB_EVENT_NAME") in ("push", "workflow_dispatch")
        and os.environ.get("GITHUB_REF") == "refs/heads/codex/publish-beta-5247-20261124",
        "Wrong publication context",
    )
    api = GitHub(os.environ.get("GH_TOKEN", ""))
    work = ROOT / "beta-publication-work"
    require(not work.exists(), "Publication workspace already exists")
    work.mkdir()
    assets = work / "assets"
    assets.mkdir()
    admit_normal_runs(api, work)
    admit_package(api, options.qualified_dir, assets)
    admit_guest(api, options.guest_dir)
    guide = ROOT / "release/Windows-Beta-Quick-Start.txt"
    require(
        hash_file(guide) == GUIDE_SHA and "DRAFT" not in guide.read_text(), "Guide is not final"
    )
    shutil.copyfile(guide, assets / guide.name)
    validation_file = ROOT / "release/beta-validation.json"
    notes_file = ROOT / "release/release-notes.txt"
    require(
        hash_file(validation_file)
        == "d7fb5e8298f79fd4dd0021773689d85ac2183836db4f52b9ae99af284a55acaf",
        "Validation asset mismatch",
    )
    require(
        hash_file(notes_file) == "132f8cc5a4a976cbc71f11bbf36f7e59d3edbd6789d9931cf7f22fb80b691319",
        "Release notes mismatch",
    )
    notes = notes_file.read_text()
    require("PENDING" not in notes and "DRAFT" not in notes, "Release notes incomplete")
    shutil.copyfile(validation_file, assets / validation_file.name)
    checksums = "".join(f"{sha}  {name}\n" for name, (_, sha) in asset_map(assets).items())
    (assets / "SHA256SUMS").write_text(checksums)
    result = (
        publish(api, assets, notes)
        if options.publish
        else {
            "validated": True,
            "published": False,
            "source": SOURCE,
            "assets": {
                name: {"bytes": size, "sha256": sha}
                for name, (size, sha) in asset_map(assets).items()
            },
        }
    )
    (ROOT / "publication-evidence.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(
            "Beta publication stopped: "
            + (str(error) if type(error) is RuntimeError else type(error).__name__),
            file=sys.stderr,
        )
        sys.exit(1)
