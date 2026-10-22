"""Publish only the already-qualified Windows bytes; uses Python's standard library."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

REPO = "mkurtgerald/K5-Vision"
HEAD = "c6dbf70abc9a7bede7a07602e52e5d5a7fb753a0"
RUN = 39173802998
TAG = "k5-windows-beta-c6dbf70"
SETUP_SHA = "49b6f646939a584c70ed872a0db85070d8528f0f11db64af13b434c34b8ddb17"
SETUP_BYTES = 13410082
VM_SHA = "9ad1afe06945536c5ebe0e32c318c37b284f0fb589e6d040db019d199c6972c5"
ARTIFACTS = (
    (
        12153413509,
        "k5-vision-windows-setup-" + HEAD,
        12913458,
        "791e5699eee2df8c9c96cee245d20cd01776e6b58d6807aa7b007abc84461a46",
        {
            "K5VisionSetup.exe",
            "desktop-package.json",
            "payload-manifest.json",
            "dependency-inventory.json",
            "desktop-windows-receipt.json",
        },
    ),
    (
        12153348667,
        "desktop-windows-witness-39173802998",
        2104,
        "c404d2884963effc57662383092bdd41250478dfd0e12c1e7b77c4aa4a34c173",
        {"desktop-windows.xml", "desktop-windows-receipt.json"},
    ),
    (
        12154045759,
        "vm-clock-windows-witness-39173802998",
        4602,
        "8ed3a9377e1529fb4138355053d4a5a5d77d4b5f5cb1fcde3ee632c7c6ce0bc5",
        {
            "vm-clock-windows.xml",
            "vm-clock-windows-receipt.json",
            "vm-clock-input/desktop-package.json",
        },
    ),
)
ASSETS = (
    "K5VisionSetup.exe",
    "desktop-windows-receipt.json",
    "desktop-windows.xml",
    "vm-clock-windows-receipt.json",
    "vm-clock-windows.xml",
    "SHA256SUMS",
)
BODY = (
    "<!-- k5-qualified-c6dbf70-delivery-v1 -->\n"
    "Native Windows ONVIF recording beta. Download K5VisionSetup.exe and open it to install; "
    "no PowerShell, command prompt, or separate Python installation is required.\n\n"
    "Source: " + HEAD + ". This is the exact installer exercised by the graphical Windows "
    "and real VM clock-correction qualification, not a rebuild. It is unsigned; Windows may "
    "show an unknown-publisher warning. First launch needs internet to fetch verified "
    "recording workers.\n\n"
    "The attached finite receipts cover generated cameras and actual guest clock corrections. "
    "Physical-camera compatibility, hardware capacity and endurance remain beta field checks. "
    "SHA256SUMS identifies every attached executable and evidence file.\n"
)


class AdmissionError(RuntimeError):
    pass


def require(value, message):
    if not value:
        raise AdmissionError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        require(
            parsed.scheme == "https" and not parsed.username and not parsed.password,
            "Unsafe download redirect",
        )
        require(
            parsed.port in (None, 443) and parsed.hostname is not None, "Unsafe download redirect"
        )
        allowed = (".githubusercontent.com", ".blob.core.windows.net", ".github.com")
        require(
            parsed.hostname == "api.github.com" or parsed.hostname.endswith(allowed),
            "Unknown download redirect host",
        )
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if parsed.hostname != urllib.parse.urlsplit(req.full_url).hostname:
            redirected.remove_header("Authorization")
            redirected.remove_header("Cookie")
        return redirected


class GitHub:
    def __init__(self):
        self.token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        require(bool(self.token), "Fresh GitHub job token unavailable")
        self.opener = urllib.request.build_opener(SafeRedirect())

    def request(
        self,
        path,
        *,
        method="GET",
        payload=None,
        data=None,
        limit=2 * 1024 * 1024,
        missing=False,
        binary=False,
        upload=False,
    ):
        host = "uploads.github.com" if upload else "api.github.com"
        headers = {
            "Authorization": "Bearer " + self.token,
            "User-Agent": "K5-qualified-delivery",
            "X-GitHub-Api-Version": "2022-11-28",
            "Accept": (
                "application/octet-stream"
                if binary and "/releases/assets/" in path
                else "application/vnd.github+json"
            ),
        }
        if payload is not None:
            data = json.dumps(payload, allow_nan=False).encode()
            headers["Content-Type"] = "application/json"
        elif data is not None:
            headers["Content-Type"] = "application/octet-stream"
        request = urllib.request.Request(
            "https://" + host + path, data=data, headers=headers, method=method
        )
        try:
            with self.opener.open(request, timeout=45) as response:
                result = response.read(limit + 1)
        except urllib.error.HTTPError as error:
            if missing and error.code == 404:
                return None
            raise AdmissionError(
                "GitHub "
                + method
                + " "
                + path.split("?", 1)[0]
                + " failed: HTTP "
                + str(error.code)
            ) from None
        require(len(result) <= limit, "GitHub response exceeded its byte limit")
        return result if binary else json.loads(result)


def unpack(archive, expected):
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        entries = zipped.infolist()
        require(
            len(entries) == len(expected) and set(zipped.namelist()) == expected,
            "Artifact file allowlist mismatch",
        )
        for entry in entries:
            mode = entry.external_attr >> 16
            require(
                not entry.is_dir() and (mode & 0o170000) in (0, 0o100000),
                "Artifact contains a nonordinary file",
            )
            require(
                not entry.flag_bits & 1 and entry.file_size <= 16 * 1024 * 1024,
                "Artifact entry exceeded its byte limit",
            )
        return {entry.filename: zipped.read(entry) for entry in entries}


def parsed(data):
    return json.loads(data.decode("utf-8-sig"))


def admit(api, source, output):
    actual_head = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    require(actual_head == HEAD, "Verifier checkout is not the qualified source")
    run = api.request(f"/repos/{REPO}/actions/runs/{RUN}")
    require(
        run["head_sha"] == HEAD and run["status"] == "completed" and run["conclusion"] == "success",
        "Qualification run identity mismatch",
    )
    bundles = []
    for identifier, name, size, sha, allowed in ARTIFACTS:
        metadata = api.request(f"/repos/{REPO}/actions/artifacts/{identifier}")
        require(
            metadata["id"] == identifier
            and metadata["name"] == name
            and metadata["size_in_bytes"] == size
            and metadata["digest"] == "sha256:" + sha
            and metadata["expired"] is False
            and metadata["workflow_run"]["id"] == RUN
            and metadata["workflow_run"]["head_sha"] == HEAD,
            "Artifact identity mismatch",
        )
        archive = api.request(
            f"/repos/{REPO}/actions/artifacts/{identifier}/zip", binary=True, limit=size
        )
        require(
            len(archive) == size and digest(archive) == sha, "Artifact ZIP byte identity mismatch"
        )
        bundles.append(unpack(archive, allowed))
    setup, normal, vm = bundles
    executable = setup["K5VisionSetup.exe"]
    require(
        len(executable) == SETUP_BYTES
        and executable[:2] == b"MZ"
        and digest(executable) == SETUP_SHA,
        "Qualified installer identity mismatch",
    )
    package = parsed(setup["desktop-package.json"])
    require(
        package["head_sha"] == HEAD
        and package["setup_sha256"] == SETUP_SHA
        and package["setup_bytes"] == SETUP_BYTES
        and package["no_terminal_required"] is True
        and package["onvif_camera_onboarding"] is True
        and package["manual_stream_uri_required"] is False
        and package["workers_bundled"] is False
        and package["first_launch_internet_required"] is True,
        "Package metadata mismatch",
    )
    require(
        package["payload_manifest_sha256"] == digest(setup["payload-manifest.json"]),
        "Payload manifest identity mismatch",
    )
    require(
        parsed(setup["payload-manifest.json"])["head_sha"] == HEAD
        and parsed(setup["dependency-inventory.json"])["head_sha"] == HEAD,
        "Payload source identity mismatch",
    )
    require(
        parsed(normal["desktop-windows-receipt.json"])
        == parsed(setup["desktop-windows-receipt.json"]),
        "Normal receipts differ",
    )
    require(
        parsed(vm["vm-clock-input/desktop-package.json"]) == package,
        "VM exercised a different installer package",
    )
    require(
        len(vm["vm-clock-windows-receipt.json"]) == 6898
        and digest(vm["vm-clock-windows-receipt.json"]) == VM_SHA,
        "Qualified VM receipt byte identity mismatch",
    )
    with tempfile.TemporaryDirectory(prefix="k5-publish-admission-") as directory:
        work = Path(directory)
        environment = {**os.environ, "K5_DESKTOP_HEAD_SHA": HEAD}
        for kind, report, published, extra in (
            (
                "verify_desktop_receipt.py",
                normal["desktop-windows.xml"],
                normal["desktop-windows-receipt.json"],
                [],
            ),
            (
                "verify_windows_vm_clock_receipt.py",
                vm["vm-clock-windows.xml"],
                vm["vm-clock-windows-receipt.json"],
                ["--expected-head", HEAD, "--installer-sha256", SETUP_SHA],
            ),
        ):
            report_path, receipt_path = work / "report.xml", work / "receipt.json"
            report_path.write_bytes(report)
            result = subprocess.run(
                [
                    sys.executable,
                    str(source / "scripts" / kind),
                    str(report_path),
                    str(receipt_path),
                    *extra,
                ],
                env=environment,
                capture_output=True,
                timeout=30,
            )
            require(result.returncode == 0, "Strict qualification receipt admission failed")
            require(
                parsed(receipt_path.read_bytes()) == parsed(published),
                "Published receipt differs from strict verifier output",
            )
    require(not output.exists(), "Admission output already exists")
    output.mkdir(parents=True)
    files = {
        "K5VisionSetup.exe": executable,
        **normal,
        "vm-clock-windows.xml": vm["vm-clock-windows.xml"],
        "vm-clock-windows-receipt.json": vm["vm-clock-windows-receipt.json"],
    }
    checksums = "".join(digest(data) + "  " + name + "\n" for name, data in sorted(files.items()))
    files["SHA256SUMS"] = checksums.encode("ascii")
    require(set(files) == set(ASSETS), "Release asset allowlist mismatch")
    for name, data in files.items():
        (output / name).write_bytes(data)
    print("Admitted exact installer and both strict Windows receipts at " + HEAD)
    return files


def tag_matches(api, *, required=False):
    reference = api.request(f"/repos/{REPO}/git/ref/tags/{TAG}", missing=True)
    if reference is None:
        require(not required, "Published release tag is missing")
        return
    target = reference["object"]
    for _ in range(3):
        if target["type"] == "commit":
            require(target["sha"] == HEAD, "Existing tag targets an unqualified commit")
            return
        require(target["type"] == "tag", "Existing tag type is unsupported")
        target = api.request(f"/repos/{REPO}/git/tags/{target['sha']}")["object"]
    raise AdmissionError("Existing tag chain exceeded its limit")


def check_assets(api, release, files):
    assets = api.request(f"/repos/{REPO}/releases/{release['id']}/assets?per_page=100")
    require(type(assets) is list and len(assets) <= len(files), "Unknown existing release assets")
    known = {}
    for asset in assets:
        name = asset["name"]
        require(
            name in files and name not in known and asset["state"] == "uploaded",
            "Unknown or incomplete existing release asset",
        )
        require(asset["size"] == len(files[name]), "Existing release asset size mismatch")
        content = api.request(
            f"/repos/{REPO}/releases/assets/{asset['id']}", binary=True, limit=len(files[name])
        )
        require(content == files[name], "Existing release asset bytes mismatch")
        known[name] = asset
    return known


def publish(api, files):
    tag_matches(api)
    release = api.request(f"/repos/{REPO}/releases/tags/{TAG}", missing=True)
    if release is None:
        candidates = api.request(f"/repos/{REPO}/releases?per_page=100")
        require(type(candidates) is list, "Release inventory is invalid")
        matches = [item for item in candidates if item.get("tag_name") == TAG]
        require(len(matches) <= 1, "Release tag inventory is ambiguous")
        release = matches[0] if matches else None
    if release is None:
        release = api.request(
            f"/repos/{REPO}/releases",
            method="POST",
            payload={
                "tag_name": TAG,
                "target_commitish": HEAD,
                "name": "K5 Vision Windows beta c6dbf70",
                "body": BODY,
                "draft": True,
                "prerelease": True,
                "make_latest": "false",
            },
        )
    require(
        release["tag_name"] == TAG
        and release["target_commitish"] == HEAD
        and release["body"] == BODY
        and release["prerelease"] is True,
        "Existing release is not this qualified delivery",
    )
    assets = check_assets(api, release, files)
    if release["draft"] is False:
        require(set(assets) == set(files), "Published release asset set is incomplete")
    else:
        require(release["draft"] is True, "Unexpected release draft state")
        for name, content in sorted(files.items()):
            if name not in assets:
                api.request(
                    f"/repos/{REPO}/releases/{release['id']}/assets?name="
                    + urllib.parse.quote(name, safe=""),
                    method="POST",
                    data=content,
                    upload=True,
                )
        require(set(check_assets(api, release, files)) == set(files), "Draft upload is incomplete")
        release = api.request(
            f"/repos/{REPO}/releases/{release['id']}",
            method="PATCH",
            payload={"draft": False, "prerelease": True, "make_latest": "false"},
        )
        require(
            release["draft"] is False and release["prerelease"] is True,
            "Release did not publish as a prerelease",
        )
    tag_matches(api, required=True)
    return "https://github.com/" + REPO + "/releases/tag/" + TAG


def verify_public(files):
    # This opener has no Authorization header or GitHub client token.
    opener = urllib.request.build_opener(SafeRedirect())
    for name, expected in sorted(files.items()):
        url = f"https://github.com/{REPO}/releases/download/{TAG}/{name}"
        for attempt in range(4):
            try:
                request = urllib.request.Request(
                    url, headers={"User-Agent": "K5-public-download-check"}
                )
                with opener.open(request, timeout=45) as response:
                    actual = response.read(len(expected) + 1)
                require(actual == expected, "Anonymous public asset bytes mismatch")
                break
            except urllib.error.HTTPError as error:
                if error.code != 404 or attempt == 3:
                    raise AdmissionError(
                        "Anonymous download failed: HTTP " + str(error.code)
                    ) from None
                time.sleep(2)
        print("Verified anonymous download: " + url)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--verify-public", action="store_true")
    args = parser.parse_args()
    require(not args.verify_public or args.publish, "Public verification requires publication")
    api = GitHub()
    files = admit(api, args.source.resolve(strict=True), args.output)
    if args.publish:
        print("Published qualified prerelease: " + publish(api, files))
    if args.verify_public:
        verify_public(files)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(
            "Qualified delivery stopped: "
            + (str(error) if isinstance(error, AdmissionError) else type(error).__name__),
            file=sys.stderr,
        )
        raise SystemExit(1) from None
