"""Generated, camera-free integrity tests for the packaged owner analytics payload."""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "scripts/windows-alpha/owner_analytics_bundle.py"
SPEC = importlib.util.spec_from_file_location("owner_analytics_bundle", SOURCE)
assert SPEC is not None and SPEC.loader is not None
bundle = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bundle)


def make_bundle(tmp_path, monkeypatch):
    wheel = b"synthetic wheel bytes, never executed"
    model = b"synthetic model bytes, never executed"
    monkeypatch.setattr(bundle, "VERSIONS", {"example": "1.2.3"})
    monkeypatch.setattr(
        bundle,
        "MODELS",
        {"example/FP16/sample.bin": (len(model), hashlib.sha384(model).hexdigest())},
    )
    root = tmp_path / "bundle"
    (root / "wheels").mkdir(parents=True)
    (root / "models/example/FP16").mkdir(parents=True)
    (root / "wheels/example-1.2.3-py3-none-any.whl").write_bytes(wheel)
    (root / "models/example/FP16/sample.bin").write_bytes(model)
    (root / "Analytics-Lab-LICENSE").write_text("Engineering only")
    (root / "Analytics-Lab-THIRD_PARTY.md").write_text("Third-party notices")
    record = {
        "schema_version": "k5-owner-analytics-bundle-v1",
        "revision": bundle.REVISION,
        "release_status": "engineering-only; release approval pending",
        "wheels": [
            {
                "name": "example",
                "version": "1.2.3",
                "filename": "example-1.2.3-py3-none-any.whl",
                "size": len(wheel),
                "sha256": hashlib.sha256(wheel).hexdigest(),
            }
        ],
        "models": [
            {
                "path": "example/FP16/sample.bin",
                "size": len(model),
                "sha384": hashlib.sha384(model).hexdigest(),
                "license": "Apache-2.0",
            }
        ],
    }
    (root / "manifest.json").write_text(json.dumps(record), encoding="utf-8")
    return root


def test_bundle_admits_exact_pins_and_materializes_config(tmp_path, monkeypatch):
    root = make_bundle(tmp_path, monkeypatch)
    wheels, models = bundle.validate(root)
    assert len(wheels) == len(models) == 1
    destination = tmp_path / "staged"
    destination.mkdir()
    bundle.materialize_models(root, destination)
    assert (destination / "analytics-models/example/FP16/sample.bin").is_file()
    config = json.loads((destination / "analytics-config.json").read_text("utf-8"))
    assert config == {
        "schema_version": 1,
        "provider": bundle.PROVIDER,
        "source_revision": bundle.REVISION,
        "artifact_root": str((destination / "analytics-models").absolute()),
    }


@pytest.mark.parametrize("target", ["wheels", "models"])
def test_bundle_rejects_extra_payload_files(tmp_path, monkeypatch, target):
    root = make_bundle(tmp_path, monkeypatch)
    (root / target / "unreviewed.bin").write_bytes(b"unapproved")
    with pytest.raises(ValueError):
        bundle.validate(root)


def test_bundle_rejects_modified_wheel_and_model(tmp_path, monkeypatch):
    root = make_bundle(tmp_path, monkeypatch)
    wheel = root / "wheels/example-1.2.3-py3-none-any.whl"
    wheel.write_bytes(wheel.read_bytes() + b"x")
    with pytest.raises(ValueError):
        bundle.validate(root)
    wheel.write_bytes(b"synthetic wheel bytes, never executed")
    model = root / "models/example/FP16/sample.bin"
    model.write_bytes(b"changed model")
    with pytest.raises(ValueError):
        bundle.validate(root)


def test_bundle_rejects_unreviewed_model_path(tmp_path, monkeypatch):
    root = make_bundle(tmp_path, monkeypatch)
    manifest = root / "manifest.json"
    record = json.loads(manifest.read_text("utf-8"))
    record["models"][0]["path"] = "../private-camera"
    manifest.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ValueError):
        bundle.validate(root)
