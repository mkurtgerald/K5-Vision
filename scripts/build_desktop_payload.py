"""Build the offline, operator-only Windows desktop payload from pinned donors.

This is a development/CI tool, never an end-user installation step. It retains
upstream runtime notices, admits only the exact binary wheels in donors.json,
and emits the manifest identity compiled into the graphical supervisor.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from email.parser import Parser
from pathlib import Path, PurePosixPath
from urllib.request import urlopen
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
DONORS = ROOT / "windows" / "desktop" / "donors.json"
ONVIF_NOTICE = "third_party/licenses/go2rtc-MIT.txt"
ONVIF_NOTICE_SHA256 = "b0dcf4855af5a72b4dfbd9117c207b330f4cc35658576a0b5351d6e2becac546"
ONVIF_MODULE_PATH = "runtime/Lib/site-packages/k5vision/operator_onvif.py"
ONVIF_NOTICE_PATH = "licenses/go2rtc-MIT.txt"


def onvif_source_adaptation(payload: Path) -> dict[str, object]:
    """Require the installed connector and unchanged upstream permission notice.

    Git may check out a text license with LF or CRLF on Windows. Admit only
    those exact complete representations of the pinned upstream text; retain
    the actual installed bytes and hash in the sealed payload inventory.
    """
    module, notice = payload / ONVIF_MODULE_PATH, payload / ONVIF_NOTICE_PATH
    for path in (module, notice):
        if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
            raise ValueError("Required ONVIF package material missing")
    raw_notice = notice.read_bytes()
    canonical = raw_notice.replace(b"\r\n", b"\n")
    if hashlib.sha256(canonical).hexdigest() != ONVIF_NOTICE_SHA256 or raw_notice not in (
        canonical,
        canonical.replace(b"\n", b"\r\n"),
    ):
        raise ValueError("ONVIF upstream notice identity mismatch")
    return {
        "component": "go2rtc ONVIF selected-source adaptation",
        "version": "v1.9.14",
        "source": "https://github.com/AlexxIT/go2rtc",
        "source_commit": "b5948cfb25404cc5cb37b166ecaa2dca20b11d4b",
        "license": "MIT",
        "integration": "Python stdlib camera-onboarding connector; no Go runtime",
        "module": ONVIF_MODULE_PATH,
        "retained_notice": {"path": ONVIF_NOTICE_PATH, "sha256": digest(notice)},
    }


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def admit(path: Path, identity: dict[str, object]) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Donor file is not an ordinary file")
    if path.stat().st_size != identity["bytes"] or digest(path) != identity["sha256"]:
        raise ValueError("Donor content identity mismatch")


def download(identity: dict[str, object], destination: Path) -> None:
    expected = int(identity["bytes"])
    url = str(identity["url"])
    if not url.startswith("https://") or expected <= 0:
        raise ValueError("Donor download identity invalid")
    with urlopen(url, timeout=180) as response, destination.open("xb") as stream:
        copied = 0
        while block := response.read(min(1024 * 1024, expected + 1 - copied)):
            copied += len(block)
            if copied > expected:
                raise ValueError("Donor size exceeded")
            stream.write(block)
    admit(destination, identity)


def extract_python(archive: Path, runtime: Path) -> None:
    # The admitted official embeddable distribution contains only flat files.
    with ZipFile(archive) as zipped:
        for entry in zipped.infolist():
            if (
                not entry.filename
                or PurePosixPath(entry.filename).name != entry.filename
                or entry.is_dir()
                or entry.file_size > 25 * 1024 * 1024
                or entry.external_attr >> 16 & 0o170000 == 0o120000
            ):
                raise ValueError("Unexpected embedded Python archive layout")
        zipped.extractall(runtime)
    (runtime / "python312._pth").write_bytes(
        b"python312.zip\r\n.\r\nLib/site-packages\r\nimport site\r\n"
    )


def run(arguments: list[str]) -> None:
    subprocess.run(arguments, cwd=ROOT, check=True)


def inventory_wheels(wheels: Path, pins: list[dict[str, object]]) -> list[dict[str, object]]:
    expected = {str(pin["filename"]): pin for pin in pins}
    actual = {path.name: path for path in wheels.iterdir()}
    if set(actual) != set(expected):
        raise ValueError("Unexpected dependency wheel inventory")
    inventory: list[dict[str, object]] = []
    for filename, pin in sorted(expected.items()):
        wheel = actual[filename]
        admit(wheel, pin)
        with ZipFile(wheel) as zipped:
            metadata_names = [
                name for name in zipped.namelist() if name.endswith(".dist-info/METADATA")
            ]
            if len(metadata_names) != 1:
                raise ValueError("Dependency metadata missing")
            metadata = Parser().parsestr(zipped.read(metadata_names[0]).decode("utf-8"))
            if metadata["Name"] != pin["name"] or metadata["Version"] != pin["version"]:
                raise ValueError("Dependency metadata identity mismatch")
            license_names = [
                name
                for name in zipped.namelist()
                if any(marker in name.lower() for marker in ("license", "copying", "notice"))
                and not name.endswith("/")
            ]
            if not license_names:
                raise ValueError("Dependency wheel has no retained license")
            inventory.append(
                {
                    **pin,
                    "source": metadata.get_all("Project-URL", []),
                    "retained_notices": [
                        {
                            "path": "runtime/Lib/site-packages/" + name,
                            "sha256": hashlib.sha256(zipped.read(name)).hexdigest(),
                        }
                        for name in sorted(license_names)
                    ],
                }
            )
    return inventory


def worker_download(source: Path, name: str) -> dict[str, object]:
    manifest = json.loads((ROOT / "src/k5vision/data" / f"{name}-donor-manifest.json").read_text())
    artifacts = [item for item in manifest["artifacts"] if item["platform"] == "windows_amd64"]
    if len(artifacts) != 1:
        raise ValueError("Worker platform identity invalid")
    artifact = artifacts[0]
    admit(source, {"bytes": artifact["executable_bytes"], "sha256": artifact["executable_sha256"]})
    return {
        "component": manifest["component"],
        "version": manifest["version"],
        "url": artifact["url"],
        "sha256": artifact["sha256"],
        "bytes": {"mediamtx": 28583662, "caddy": 18479498}[name],
        "executable": artifact["executable"],
        "executable_sha256": artifact["executable_sha256"],
        "executable_bytes": artifact["executable_bytes"],
        "license_sha256": manifest["license_sha256"],
    }


def write_manifest(payload: Path, head_sha: str, generated: Path) -> str:
    files = []
    for path in sorted(payload.rglob("*")):
        if path.is_symlink():
            raise ValueError("Symbolic payload entry refused")
        if not path.is_file():
            continue
        relative = path.relative_to(payload).as_posix()
        if relative in ("payload-manifest.json", "K5Vision.exe"):
            continue
        files.append({"path": relative, "sha256": digest(path), "bytes": path.stat().st_size})
    manifest = payload / "payload-manifest.json"
    manifest.write_text(
        json.dumps({"schema_version": 1, "head_sha": head_sha, "files": files}, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    identity = digest(manifest)
    generated.write_text(
        "namespace K5VisionDesktop { internal static class PayloadIdentity {\n"
        f'  internal const string ManifestSha256 = "{identity}";\n'
        f'  internal const string HeadSha = "{head_sha}";\n'
        "} }\n",
        encoding="utf-8",
    )
    return identity


def build(args: argparse.Namespace) -> None:
    if sys.platform != "win32" or sys.version_info[:2] != (3, 12):
        raise ValueError("Payload compilation requires a native Windows Python 3.12 builder")
    if not re.fullmatch(r"[0-9a-f]{40}", args.head_sha):
        raise ValueError("Exact source revision required")
    output = args.output.absolute()
    if output.exists():
        raise ValueError("Build output must be new; existing contents are never removed")
    output.mkdir(parents=True)
    payload, wheels = output / "payload", output / "wheels"
    runtime = payload / "runtime"
    runtime.mkdir(parents=True)
    wheels.mkdir()
    donors = json.loads(DONORS.read_text(encoding="utf-8"))
    archive = args.python_archive
    if archive is None:
        archive = output / "python-3.12.10-embed-amd64.zip"
        download(donors["python"], archive)
    else:
        admit(archive, donors["python"])
    extract_python(archive, runtime)
    requirements = ROOT / "windows/desktop/runtime-requirements.txt"
    run(
        [
            sys.executable,
            "-m",
            "pip",
            "download",
            "--only-binary=:all:",
            "--no-deps",
            "--require-hashes",
            "--dest",
            str(wheels),
            "-r",
            str(requirements),
        ]
    )
    wheel_inventory = inventory_wheels(wheels, donors["wheels"])
    site_packages = runtime / "Lib/site-packages"
    run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            "--no-compile",
            "--require-hashes",
            "--find-links",
            str(wheels),
            "--target",
            str(site_packages),
            "-r",
            str(requirements),
        ]
    )
    product_wheels = output / "product-wheel"
    run(
        [sys.executable, "-m", "pip", "wheel", ".", "--no-deps", "--wheel-dir", str(product_wheels)]
    )
    project = list(product_wheels.glob("*.whl"))
    if len(project) != 1:
        raise ValueError("Expected exactly one project wheel")
    run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            "--no-compile",
            "--target",
            str(site_packages),
            str(project[0]),
        ]
    )
    # Console script entrypoints are outside this graphical operator runtime.
    for name in ("bin", "Scripts"):
        scripts = site_packages / name
        if scripts.exists():
            shutil.rmtree(scripts)
    workers = [
        worker_download(args.media_exe, "mediamtx"),
        worker_download(args.gateway_exe, "caddy"),
    ]
    (payload / "workers").mkdir()
    (payload / "workers/donor-downloads.json").write_text(
        json.dumps({"schema_version": 1, "artifacts": workers}, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    licenses = payload / "licenses"
    licenses.mkdir()
    for source in (
        "LICENSE",
        "THIRD_PARTY_NOTICES.md",
        "docs/WINDOWS_DESKTOP_DEPENDENCIES.md",
        "third_party/licenses/MediaMTX-MIT.txt",
        "third_party/licenses/Caddy-Apache-2.0.txt",
        "third_party/licenses/InnoSetup-6.4.3.txt",
        ONVIF_NOTICE,
    ):
        shutil.copyfile(ROOT / source, licenses / Path(source).name)
    shutil.copyfile(runtime / "LICENSE.txt", licenses / "CPython-3.12.10-LICENSE.txt")
    onvif_adaptation = onvif_source_adaptation(payload)
    # The official Windows interpreter explicitly requires external end users
    # to agree to terms protecting its Microsoft Distributable Code. Present
    # these terms in the normal installer license page, with upstream text
    # retained unchanged rather than silently treating every component as MIT.
    (licenses / "INSTALLATION-LICENSE.txt").write_bytes(
        b"K5 Vision installation terms\r\n\r\n"
        b"By accepting, you agree to the included licenses and, for Microsoft "
        b"Distributable Code only, to preserve copyright, trademark and patent notices; "
        b"use it only on Microsoft platforms; avoid implying Microsoft endorsement; "
        b"and avoid incorporating it into malicious, deceptive or unlawful programs. "
        b"These conditions apply to Microsoft's code and do not replace K5's or "
        b"Python's separate licenses.\r\n\r\n"
        + (ROOT / "LICENSE").read_bytes()
        + b"\r\n\r\nComplete included Python and Windows binary-component terms:\r\n\r\n"
        + (runtime / "LICENSE.txt").read_bytes()
    )
    (payload / "dependency-inventory.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "head_sha": args.head_sha,
                "scope": "ONVIF camera-onboarding recording operator",
                "onvif_camera_onboarding": True,
                "direct_rtsp_enrollment": True,
                "manual_stream_uri_required": False,
                "python": donors["python"],
                "worker_downloads": workers,
                "workers_bundled": False,
                "wheels": wheel_inventory,
                "source_adaptations": [onvif_adaptation],
                "project_wheel_sha256": digest(project[0]),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    # Exercise the isolated embedded import path before it is admitted. Actual
    # GUI account creation/serving/recording is a separate mandatory native gate.
    run(
        [
            str(runtime / "python.exe"),
            "-I",
            "-B",
            "-c",
            "import fastapi,pydantic,psutil,uvicorn; import k5vision.desktop_setup; "
            "import k5vision.operator_product; import k5vision.operator_product_api; "
            "import k5vision.operator_onvif; import k5vision.operator_time; "
            "import k5vision.operator_rtsp; import k5vision.operator_rtsp_api; "
            "import importlib.util; "
            "assert importlib.util.find_spec('onvif') is None; "
            "assert importlib.util.find_spec('zeep') is None; "
            "assert importlib.util.find_spec('lxml') is None; "
            "assert importlib.util.find_spec('pip') is None",
        ]
    )
    generated = output / "PayloadIdentity.generated.cs"
    manifest_sha = write_manifest(payload, args.head_sha, generated)
    print(
        json.dumps(
            {
                "head_sha": args.head_sha,
                "manifest_sha256": manifest_sha,
                "dependency_wheels": len(wheel_inventory),
                "payload_files": len(
                    json.loads((payload / "payload-manifest.json").read_text())["files"]
                ),
            }
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--media-exe", type=Path, required=True)
    parser.add_argument("--gateway-exe", type=Path, required=True)
    parser.add_argument("--python-archive", type=Path)
    args = parser.parse_args()
    try:
        build(args)
    except Exception:
        # Do not expose remote redirect query strings or local private values.
        print(
            "Desktop payload build refused; inspect the preceding bounded build stage.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
