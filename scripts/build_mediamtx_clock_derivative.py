#!/usr/bin/env python3
"""Build a separately identified Windows donor derivative; never install it."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD_MANIFEST = ROOT / "third_party/builds/mediamtx-clock-derivative.json"


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run(arguments, *, cwd=None, env=None, timeout=600):
    result = subprocess.run(
        [str(a) for a in arguments],
        cwd=cwd,
        env=env,
        check=False,
        capture_output=True,
        timeout=timeout,
    )
    if result.returncode:
        raise RuntimeError((result.stdout + result.stderr).decode("utf-8", errors="replace"))
    return result.stdout


def admit_checkout(path, expected):
    observed = run(["git", "rev-parse", "HEAD"], cwd=path).decode().strip()
    if observed != expected or run(["git", "status", "--porcelain"], cwd=path).strip():
        raise ValueError("Donor checkout identity or clean state differs")
    # git archive also applies core.autocrlf; explicitly retain committed LF
    # source bytes independently of the Windows host's checkout preferences.
    archive = run(
        [
            "git",
            "-c",
            "core.autocrlf=false",
            "-c",
            "core.eol=lf",
            "archive",
            "--format=tar",
            "HEAD",
        ],
        cwd=path,
    )
    return archive, hashlib.sha256(archive).hexdigest()


def materialize(archive, target):
    target.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as files:
        for entry in files:
            relative = Path(entry.name)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("Source archive path refused")
            destination = target / relative
            if entry.isdir():
                destination.mkdir(parents=True, exist_ok=True)
            elif entry.isfile():
                destination.parent.mkdir(parents=True, exist_ok=True)
                with files.extractfile(entry) as source:
                    destination.write_bytes(source.read())
            else:
                raise ValueError("Non-ordinary donor source archive member refused")


def objects(raw):
    text = raw.decode("utf-8")
    decoder = json.JSONDecoder()
    while text.strip():
        value, end = decoder.raw_decode(text.lstrip())
        yield value
        text = text.lstrip()[end:]


def retain_notices(go, source, environment, output, manifest):
    notice_root = output / "licenses"
    notice_root.mkdir()
    entries = []
    module_json = run([go, "list", "-m", "-json", "all"], cwd=source, env=environment)
    (output / "go-module-graph.jsonl").write_bytes(module_json)
    for module in objects(module_json):
        actual = module.get("Replace", module)
        directory = actual.get("Dir")
        if not directory:
            continue
        directory = Path(directory)
        names = [
            p
            for p in directory.iterdir()
            if p.is_file()
            and p.name.upper().startswith(("LICENSE", "COPYING", "NOTICE", "PATENTS", "AUTHORS"))
        ]
        for path in sorted(names):
            label = module["Path"].replace("/", "__") + "__" + path.name
            destination = notice_root / label
            shutil.copyfile(path, destination)
            entries.append(
                {
                    "module": module["Path"],
                    "version": module.get("Version"),
                    "notice": label,
                    "sha256": digest(destination),
                }
            )
    for name, path in (
        ("Go-LICENSE", go.parent.parent / "LICENSE"),
        ("Go-PATENTS", go.parent.parent / "PATENTS"),
        ("MediaMTX-MIT.txt", source / "LICENSE"),
        ("gortsplib-MIT.txt", source.parent / "gortsplib" / "LICENSE"),
        ("hls.js-LICENSE.txt", ROOT / manifest["hls_js"]["license_path"]),
        ("Apache-2.0.txt", ROOT / manifest["hls_js"]["apache_license_path"]),
    ):
        if path.is_file():
            shutil.copyfile(path, notice_root / name)
            entries.append({"module": name, "notice": name, "sha256": digest(path)})
    (output / "notice-inventory.json").write_text(json.dumps(entries, indent=2) + "\n")
    return entries


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mediamtx-source", type=Path, required=True)
    parser.add_argument("--gortsplib-source", type=Path, required=True)
    parser.add_argument("--go-root", type=Path, required=True)
    parser.add_argument("--go-archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads(BUILD_MANIFEST.read_text())
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    receipt = {
        "schema_version": 1,
        "scope": manifest["scope"],
        "status": "building",
        "product_qualification": False,
        "installer_built": False,
        "derivative_version": manifest["derivative_version"],
        "builds": [],
    }
    stage = "admission"
    try:
        if os.name != "nt":
            raise ValueError("This proof requires a native Windows build host")
        go = args.go_root.resolve() / "bin/go.exe"
        if (
            digest(args.go_archive) != manifest["go"]["archive_sha256"]
            or args.go_archive.stat().st_size != manifest["go"]["archive_bytes"]
        ):
            raise ValueError("Go toolchain archive identity differs")
        environment = dict(
            os.environ,
            GOTOOLCHAIN="local",
            GOWORK="off",
            CGO_ENABLED="0",
            GOOS="windows",
            GOARCH="amd64",
            GOPROXY="https://proxy.golang.org",
            GOSUMDB="sum.golang.org",
            GOFLAGS="",
            GOEXPERIMENT="",
            GOAMD64="v1",
            GOENV="off",
            GOROOT=str(args.go_root.resolve()),
            GOPATH=str(output / "go-workspace"),
            GOMODCACHE=str(output / "go-modules"),
        )
        observed_go = run([go, "version"], env=environment).decode().strip()
        if observed_go != "go version " + manifest["go"]["version"] + " windows/amd64":
            raise ValueError("Go toolchain runtime identity differs")
        media_archive, media_archive_sha = admit_checkout(
            args.mediamtx_source, manifest["mediamtx"]["revision"]
        )
        rtsp_archive, rtsp_archive_sha = admit_checkout(
            args.gortsplib_source, manifest["gortsplib"]["revision"]
        )
        patch = ROOT / manifest["patch"]["path"]
        if digest(patch) != manifest["patch"]["sha256"]:
            raise ValueError("Clock patch identity differs")
        patch_manifest = ROOT / manifest["patch"]["manifest_path"]
        if digest(patch_manifest) != manifest["patch"]["manifest_sha256"]:
            raise ValueError("Reviewed patch source and regression manifest differs")
        peer_pin = json.loads(patch_manifest.read_text())
        if (
            peer_pin["patch_sha256"] != digest(patch)
            or peer_pin["upstream_commit_sha"] != manifest["gortsplib"]["revision"]
            or peer_pin["consumer_commit_sha"] != manifest["mediamtx"]["revision"]
            or digest(ROOT / peer_pin["license"]["path"]) != peer_pin["license"]["sha256"]
        ):
            raise ValueError("Reviewed patch source, consumer or notice identity differs")
        if (
            digest(ROOT / manifest["hls_js"]["apache_license_path"])
            != manifest["hls_js"]["apache_license_sha256"]
        ):
            raise ValueError("Full upstream Apache license identity differs")
        notice = ROOT / manifest["hls_js"]["license_path"]
        if digest(notice) != manifest["hls_js"]["license_sha256"]:
            raise ValueError("HLS notice identity differs")
        stage = "embedded_asset"
        with urllib.request.urlopen(manifest["hls_js"]["url"], timeout=60) as response:
            hls_archive = response.read(10 * 1024 * 1024 + 1)
        if (
            len(hls_archive) != manifest["hls_js"]["archive_bytes"]
            or hashlib.sha256(hls_archive).hexdigest() != manifest["hls_js"]["archive_sha256"]
        ):
            raise ValueError("Pinned upstream HLS asset identity differs")
        with zipfile.ZipFile(io.BytesIO(hls_archive)) as archive:
            hls_js = archive.read("dist/hls.min.js")
        receipt.update(
            source_head=run(["git", "rev-parse", "HEAD"], cwd=ROOT).decode().strip(),
            mediamtx_revision=manifest["mediamtx"]["revision"],
            mediamtx_source_archive_sha256=media_archive_sha,
            gortsplib_revision=manifest["gortsplib"]["revision"],
            gortsplib_source_archive_sha256=rtsp_archive_sha,
            patch_sha256=digest(patch),
            patch_manifest_sha256=digest(patch_manifest),
            patched_source_files=peer_pin["source_files"],
            native_test_files=peer_pin["test_file_sha256"],
            go_version=observed_go,
            go_archive_sha256=digest(args.go_archive),
            go_executable_sha256=digest(go),
            build_manifest_sha256=digest(BUILD_MANIFEST),
            embedded_hls_js_sha256=hashlib.sha256(hls_js).hexdigest(),
        )
        trees = []
        for label in ("first", "second"):
            tree = output / ("source-" + label)
            media, rtsp = tree / "mediamtx", tree / "gortsplib"
            materialize(media_archive, media)
            materialize(rtsp_archive, rtsp)
            if digest(media / "LICENSE") != manifest["mediamtx"]["license_sha256"]:
                raise ValueError("MediaMTX notice identity differs")
            original_sum = (media / "go.sum").read_text()
            for version, total in (
                (manifest["gortsplib"]["version"], manifest["gortsplib"]["module_sum"]),
                (manifest["gortsplib"]["version"] + "/go.mod", manifest["gortsplib"]["go_mod_sum"]),
            ):
                if (
                    manifest["gortsplib"]["module"] + " " + version + " " + total
                    not in original_sum
                ):
                    raise ValueError("Original Go module checksum pin differs")
            for name, pin in peer_pin["source_files"].items():
                if digest(rtsp / name) != pin["upstream_sha256"]:
                    raise ValueError("Original TCP watchdog source identity differs")
            # Source trees live below the K5 checkout but are not Git worktrees.
            # Prevent git apply from discovering that parent and silently
            # skipping source-relative hunks outside its inferred prefix.
            patch_environment = dict(os.environ, GIT_CEILING_DIRECTORIES=str(rtsp.parent))
            git_apply = ["git", "-c", "core.autocrlf=false", "-c", "core.eol=lf", "apply"]
            run([*git_apply, "--check", str(patch)], cwd=rtsp, env=patch_environment)
            run([*git_apply, str(patch)], cwd=rtsp, env=patch_environment)
            for name, pin in peer_pin["source_files"].items():
                if digest(rtsp / name) != pin["patched_sha256"]:
                    raise ValueError("Patched TCP watchdog source identity differs")
            for name, expected in peer_pin["test_file_sha256"].items():
                if digest(rtsp / name) != expected:
                    raise ValueError("Native donor regression source identity differs")
            (media / "internal/core/VERSION").write_text(manifest["derivative_version"])
            (media / "internal/servers/hls/hls.min.js").write_bytes(hls_js)
            env = dict(environment, GOCACHE=str((tree / "go-cache").resolve()))
            run(
                [
                    go,
                    "mod",
                    "edit",
                    "-replace=" + manifest["gortsplib"]["module"] + "=../gortsplib",
                ],
                cwd=media,
                env=env,
            )
            trees.append((label, media, rtsp, env))
        stage = "native_regression"
        regression = manifest["regression"]
        baseline = output / "source-baseline" / "gortsplib"
        materialize(rtsp_archive, baseline)
        # This test uses only original private APIs. A compiler error never
        # qualifies as the expected behavioral baseline failure.
        baseline_test = regression["baseline_test_file"]
        shutil.copyfile(trees[0][2] / baseline_test, baseline / baseline_test)
        baseline_env = dict(environment, GOCACHE=str((baseline.parent / "go-cache").resolve()))
        red = subprocess.run(
            [
                str(go),
                "test",
                "-mod=readonly",
                "-json",
                "-count=1",
                "-run",
                regression["baseline_regex"],
                ".",
            ],
            cwd=baseline,
            env=baseline_env,
            check=False,
            capture_output=True,
            timeout=300,
        )
        (output / "gortsplib-original-native-regression.jsonl").write_bytes(red.stdout)
        (output / "gortsplib-original-native-regression.stderr.txt").write_bytes(red.stderr)
        failed_tests = {
            e["Test"] for e in objects(red.stdout) if e.get("Action") == "fail" and e.get("Test")
        }
        if red.returncode != 1 or not set(regression["baseline_required_failures"]) <= failed_tests:
            raise ValueError("Original source did not reproduce the exact behavioral regressions")
        receipt["original_native_regression_failures"] = sorted(failed_tests)
        first = trees[0]
        test = run(
            [
                go,
                "test",
                "-mod=readonly",
                "-json",
                "-count=1",
                "-run",
                regression["test_regex"],
                ".",
            ],
            cwd=first[2],
            env=first[3],
            timeout=300,
        )
        (output / "gortsplib-native-regression.jsonl").write_bytes(test)
        events = list(objects(test))
        passed = {e["Test"] for e in events if e.get("Action") == "pass" and e.get("Test")}
        if not set(regression["required_tests"]) <= passed:
            raise ValueError("Native donor regressions did not all execute and pass")
        receipt["native_regression_tests"] = sorted(passed)
        stage = "native_build"

        def build(item):
            label, media, _, env = item
            executable = output / ("mediamtx-" + label + ".exe")
            run(
                [go, "build", *manifest["build"]["flags"], "-o", executable, "."],
                cwd=media,
                env=env,
            )
            version = run([executable, "--version"]).decode().strip()
            if version != manifest["derivative_version"]:
                raise ValueError("Derivative executable reported a different version")
            info = run([go, "version", "-m", executable], env=env)
            (output / ("build-info-" + label + ".txt")).write_bytes(info)
            return {
                "label": label,
                "sha256": digest(executable),
                "bytes": executable.stat().st_size,
                "version": version,
                "independent_source_tree_and_compiler_cache": True,
            }

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            receipt["builds"] = list(pool.map(build, trees))
        if receipt["builds"][0]["sha256"] != receipt["builds"][1]["sha256"]:
            raise ValueError("Independent derivative builds differ")
        shutil.copyfile(output / "mediamtx-first.exe", output / "mediamtx.exe")
        receipt.update(
            reproducible_binary=True,
            executable_sha256=receipt["builds"][0]["sha256"],
            executable_bytes=receipt["builds"][0]["bytes"],
        )
        stage = "linux_cross_build"
        linux_environment = dict(
            trees[0][3], GOOS="linux", GOARCH="amd64", GOCACHE=str(output / "linux-go-cache")
        )
        linux_executable = output / "mediamtx"
        run(
            [go, "build", *manifest["build"]["flags"], "-o", linux_executable, "."],
            cwd=trees[0][1],
            env=linux_environment,
        )
        (output / "build-info-linux-amd64.txt").write_bytes(
            run([go, "version", "-m", linux_executable], env=linux_environment)
        )
        receipt["linux_cross_build"] = {
            "sha256": digest(linux_executable),
            "bytes": linux_executable.stat().st_size,
            "goos": "linux",
            "goarch": "amd64",
            "version": manifest["derivative_version"],
            "native_execution_observed": False,
            "reproducibility_claimed": False,
        }
        stage = "notices"
        retain_notices(go, trees[0][1], trees[0][3], output, manifest)
        shutil.copyfile(patch, output / patch.name)
        shutil.copyfile(patch_manifest, output / patch_manifest.name)
        shutil.copyfile(BUILD_MANIFEST, output / BUILD_MANIFEST.name)
        shutil.copyfile(ROOT / "THIRD_PARTY_NOTICES.md", output / "THIRD_PARTY_NOTICES.md")
        receipt["status"] = "passed"
    except BaseException as error:
        receipt.update(status="failed", failure_stage=stage, failure_kind=type(error).__name__)
        raise
    finally:
        (output / "build-provenance.json").write_text(json.dumps(receipt, indent=2) + "\n")
        print(
            json.dumps(
                {
                    k: receipt.get(k)
                    for k in (
                        "status",
                        "failure_stage",
                        "failure_kind",
                        "executable_sha256",
                        "executable_bytes",
                    )
                }
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
