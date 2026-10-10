#!/usr/bin/env python3
"""Build the pinned, source-verified owner Analytics Lab wheel and model assets.

Engineering package preparation only. No media, camera source, or owner data.
No redistribution/production rights are inferred from an engineering pass.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

REVISION = "c8b347ae538991a0c0ce38eabc2dc17b566531d3"
MODEL_BASE = "https://storage.openvinotoolkit.org/repositories/open_model_zoo/2023.0/models_bin/1/"
MODELS = (
    ("person-detection-retail-0013/FP16/person-detection-retail-0013.xml", 571233, "99ad3d4580a0123bef05ff77b6f46ccec16de974d1f5699fb94cd842e3242c6aa641f4977f9a5bb2f0fab42fe51cbb63"),
    ("person-detection-retail-0013/FP16/person-detection-retail-0013.bin", 1445734, "a67422e3b5ec76057651d2a0237eab862de00e968c7eef1e5f333849ae64f91900bcd30a23e1b7dbaa07313e358759b9"),
    ("human-pose-estimation-0001/FP16/human-pose-estimation-0001.xml", 218215, "cffe8df7d053b9cbf858a21faa32e30cb8a645416e9ee4ce3fbcc3106094505477d66a06b7e46d6ddb9c2de4b0cee319"),
    ("human-pose-estimation-0001/FP16/human-pose-estimation-0001.bin", 8197354, "dabb7be42c5be008de354c6670aa8291c2d18e59f18a1c138df9b5200929a03c1e323930a1d21ecff69d3f06007de67c"),
)
WHEELS = {
    "openvino": "2026.3.1",
    "opencv-python-headless": "4.12.0.88",
    "numpy": "2.2.6",
    "openvino-telemetry": "2025.2.0",
    "k5-analytics-runtime": "0.0.0+g" + REVISION,
}


def verify_model(path: Path, size: int, sha384: str) -> None:
    if path.is_symlink() or not path.is_file() or path.stat().st_size != size:
        raise RuntimeError("Pinned model size/path mismatch")
    with path.open("rb") as stream:
        if hashlib.file_digest(stream, "sha384").hexdigest() != sha384:
            raise RuntimeError("Pinned model SHA384 mismatch")


def fetch_model(relative: str, size: int, sha384: str, root: Path) -> None:
    if ".." in Path(relative).parts or re.fullmatch(r"[a-z0-9_./-]+", relative) is None:
        raise RuntimeError("Unexpected model path")
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise RuntimeError("Model destination must be fresh")
    partial = target.with_suffix(target.suffix + ".partial")
    try:
        request = urllib.request.Request(MODEL_BASE + relative, headers={"User-Agent": "K5OwnerCandidate/1"})
        with urllib.request.urlopen(request, timeout=40) as response, partial.open("xb") as stream:
            remaining = size + 1
            while remaining:
                chunk = response.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                stream.write(chunk)
                remaining -= len(chunk)
            if remaining == 0:
                raise RuntimeError("Model download exceeded pinned size")
        verify_model(partial, size, sha384)
        partial.rename(target)
    finally:
        if partial.exists():
            partial.unlink()


def wheel_inventory(directory: Path) -> list[dict]:
    found = {}
    for path in directory.iterdir():
        if path.is_symlink() or not path.is_file() or path.suffix != ".whl":
            raise RuntimeError("Unexpected wheelhouse entry")
        parts = path.name[:-4].split("-")
        if len(parts) not in (5, 6):
            raise RuntimeError("Invalid wheel filename")
        name = re.sub(r"[-_.]+", "-", parts[0]).lower()
        if name not in WHEELS or parts[1] != WHEELS[name] or name in found:
            raise RuntimeError("Unexpected wheel identity")
        if not 0 < path.stat().st_size <= 250_000_000:
            raise RuntimeError("Wheel size is out of bounds")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        found[name] = {"name": name, "version": parts[1], "filename": path.name, "size": path.stat().st_size, "sha256": digest}
    if found.keys() != WHEELS.keys():
        raise RuntimeError("Analytics wheel inventory incomplete")
    return [found[name] for name in sorted(found)]


def build(analytics_source: Path, output: Path) -> None:
    if output.exists():
        raise RuntimeError("Bundle output must be fresh")
    if not (analytics_source / "LICENSE").is_file() or not (analytics_source / "THIRD_PARTY.md").is_file():
        raise RuntimeError("Analytics proprietary license and donor notices are missing")
    builder = Path(__file__).resolve().parent / "build_analytics_runtime_wheel.py"
    if not builder.is_file():
        raise RuntimeError("Reviewed donor wheel builder unavailable")
    output.mkdir(parents=True)
    wheels, models = output / "wheels", output / "models"
    wheels.mkdir()
    models.mkdir()
    try:
        subprocess.run([sys.executable, "-I", "-B", str(builder), "--source-root", str(analytics_source), "--output-dir", str(wheels)], check=True, timeout=180)
        requirements = [f"{name}=={version}" for name, version in sorted(WHEELS.items()) if name != "k5-analytics-runtime"]
        subprocess.run([sys.executable, "-I", "-B", "-m", "pip", "download", "--no-deps", "--only-binary=:all:", "--disable-pip-version-check", "--dest", str(wheels), *requirements], check=True, timeout=300)
        inventory = wheel_inventory(wheels)
        for relative, size, sha384 in MODELS:
            fetch_model(relative, size, sha384, models)
        shutil.copyfile(analytics_source / "LICENSE", output / "Analytics-Lab-LICENSE")
        shutil.copyfile(analytics_source / "THIRD_PARTY.md", output / "Analytics-Lab-THIRD_PARTY.md")
        record = {"schema_version": "k5-owner-analytics-bundle-v1", "revision": REVISION, "wheels": inventory, "models": [{"path": path, "size": size, "sha384": digest, "license": "Apache-2.0"} for path, size, digest in MODELS], "release_status": "engineering-only; downstream license/provenance approval pending"}
        (output / "manifest.json").write_text(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n", encoding="ascii")
    except BaseException:
        shutil.rmtree(output)
        raise


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--analytics-source", required=True, type=Path)
    p.add_argument("--output-dir", required=True, type=Path)
    a = p.parse_args()
    build(a.analytics_source.resolve(strict=True), a.output_dir.absolute())
    print("K5_OWNER_ANALYTICS_ASSETS=verified_engineering_candidate")
