"""Admission of the pinned, installer-owned engineering analytics payload.

No network, user source URI, credential, camera frame or arbitrary model path.
The manifest is cross-checked against product-owned identities before native pip
or model access; hashes are rechecked after each copy. Never use for promotion
without separate Analytics Lab and third-party redistribution approval.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import stat
from pathlib import Path

REVISION = "c8b347ae538991a0c0ce38eabc2dc17b566531d3"
PROVIDER = "analytics-lab-omz-person-v1"
VERSIONS = {
    "openvino": "2026.3.1",
    "opencv-python-headless": "4.12.0.88",
    "numpy": "2.2.6",
    "openvino-telemetry": "2025.2.0",
    "k5-analytics-runtime": "0.0.0+g" + REVISION,
}
MODELS = {
    "person-detection-retail-0013/FP16/person-detection-retail-0013.xml": (571233, "99ad3d4580a0123bef05ff77b6f46ccec16de974d1f5699fb94cd842e3242c6aa641f4977f9a5bb2f0fab42fe51cbb63"),
    "person-detection-retail-0013/FP16/person-detection-retail-0013.bin": (1445734, "a67422e3b5ec76057651d2a0237eab862de00e968c7eef1e5f333849ae64f91900bcd30a23e1b7dbaa07313e358759b9"),
    "human-pose-estimation-0001/FP16/human-pose-estimation-0001.xml": (218215, "cffe8df7d053b9cbf858a21faa32e30cb8a645416e9ee4ce3fbcc3106094505477d66a06b7e46d6ddb9c2de4b0cee319"),
    "human-pose-estimation-0001/FP16/human-pose-estimation-0001.bin": (8197354, "dabb7be42c5be008de354c6670aa8291c2d18e59f18a1c138df9b5200929a03c1e323930a1d21ecff69d3f06007de67c"),
}


def _unique(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("Duplicate analytics manifest key")
        result[key] = value
    return result


def _regular(path: Path) -> None:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ValueError("Analytics payload contains a link")
    if not path.is_file():
        raise ValueError("Analytics payload entry must be a file")


def _digest(path: Path, algorithm: str) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, algorithm).hexdigest()


def validate(bundle: Path) -> tuple[tuple[Path, ...], tuple[tuple[str, Path], ...]]:
    if bundle.is_symlink() or not bundle.is_dir():
        raise ValueError("Analytics bundle root is missing or linked")
    manifest = bundle / "manifest.json"
    _regular(manifest)
    if manifest.stat().st_size > 131072:
        raise ValueError("Analytics manifest too large")
    data = json.loads(manifest.read_text("utf-8"), object_pairs_hook=_unique)
    if type(data) is not dict or data.keys() != {
        "schema_version", "revision", "wheels", "models", "release_status"
    } or data["schema_version"] != "k5-owner-analytics-bundle-v1" or data["revision"] != REVISION or not isinstance(data["release_status"], str) or "engineering-only" not in data["release_status"]:
        raise ValueError("Analytics provenance admission failed")
    wheels_dir = bundle / "wheels"
    models_dir = bundle / "models"
    if any(path.is_symlink() or not path.is_dir() for path in (wheels_dir, models_dir)):
        raise ValueError("Analytics asset directories are invalid")
    if type(data["wheels"]) is not list or len(data["wheels"]) != len(VERSIONS):
        raise ValueError("Analytics wheel inventory count mismatch")
    wheel_paths = []
    seen = set()
    for item in data["wheels"]:
        if type(item) is not dict or item.keys() != {"name", "version", "filename", "size", "sha256"}:
            raise ValueError("Analytics wheel record invalid")
        name = item["name"]
        filename = item["filename"]
        size = item["size"]
        sha = item["sha256"]
        if type(name) is not str or name in seen or name not in VERSIONS or item["version"] != VERSIONS[name] or type(filename) is not str or not re.fullmatch(r"[A-Za-z0-9_.+\\-]+\\.whl", filename) or type(size) is not int or not (0 < size <= 250000000) or type(sha) is not str or not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise ValueError("Analytics wheel pin mismatch")
        if filename.split("-")[0].replace("_", "-").lower() != name:
            raise ValueError("Analytics wheel distribution mismatch")
        path = wheels_dir / filename
        _regular(path)
        if path.stat().st_size != size or _digest(path, "sha256") != sha:
            raise ValueError("Analytics wheel digest mismatch")
        wheel_paths.append(path)
        seen.add(name)
    if seen != VERSIONS.keys() or {p.name for p in wheels_dir.iterdir()} != {p.name for p in wheel_paths}:
        raise ValueError("Unexpected analytics wheel payload")
    if type(data["models"]) is not list or len(data["models"]) != len(MODELS):
        raise ValueError("Analytics model inventory count mismatch")
    model_paths = []
    seen_models = set()
    for item in data["models"]:
        if type(item) is not dict or item.keys() != {"path", "size", "sha384", "license"}:
            raise ValueError("Analytics model record invalid")
        name = item["path"]
        if type(name) is not str or name not in MODELS or name in seen_models or (item["size"], item["sha384"]) != MODELS[name] or item["license"] != "Apache-2.0":
            raise ValueError("Analytics model pin mismatch")
        source = models_dir.joinpath(*name.split("/"))
        _regular(source)
        if source.stat().st_size != item["size"] or _digest(source, "sha384") != item["sha384"]:
            raise ValueError("Analytics model digest mismatch")
        model_paths.append((name, source))
        seen_models.add(name)
    if seen_models != MODELS.keys() or {p.relative_to(models_dir).as_posix() for p in models_dir.rglob("*") if p.is_file()} != seen_models:
        raise ValueError("Unexpected analytics model payload")
    for name in ("Analytics-Lab-LICENSE", "Analytics-Lab-THIRD_PARTY.md"):
        file = bundle / name
        _regular(file)
        if not (0 < file.stat().st_size < 262144):
            raise ValueError("Analytics provenance notice missing")
    return tuple(wheel_paths), tuple(model_paths)


def materialize_models(bundle: Path, destination: Path) -> None:
    _, models = validate(bundle)
    root = destination / "analytics-models"
    if root.exists():
        raise ValueError("Analytics staging destination must be fresh")
    root.mkdir()
    for name, source in models:
        target = root.joinpath(*name.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        if target.stat().st_size != MODELS[name][0] or _digest(target, "sha384") != MODELS[name][1]:
            raise ValueError("Analytics model changed while copying")
    config = {
        "schema_version": 1,
        "provider": PROVIDER,
        "source_revision": REVISION,
        "artifact_root": str(root.absolute()),
    }
    (destination / "analytics-config.json").write_text(
        json.dumps(config, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8"
    )
