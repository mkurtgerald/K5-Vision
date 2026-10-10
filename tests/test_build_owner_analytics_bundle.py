"""Offline, camera-free checks of the pinned owner Analytics bundle builder."""
import hashlib
import importlib.util
from pathlib import Path
import pytest

SOURCE = Path(__file__).resolve().parents[1] / "scripts/build_owner_analytics_bundle.py"
spec = importlib.util.spec_from_file_location("owner_bundle", SOURCE)
assert spec and spec.loader
bundle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bundle)

def test_pinned_inputs():
    assert len(bundle.MODELS) == 4
    assert sum(n for _, n, _ in bundle.MODELS) == 10432536
    assert len(bundle.WHEELS) == 5
    assert bundle.WHEELS["k5-analytics-runtime"] == "0.0.0+g" + bundle.REVISION

def test_model_integrity_refuses_wrong_hash_or_size(tmp_path):
    sample = tmp_path / "artifact.bin"
    sample.write_bytes(b"sample")
    bundle.verify_model(sample, 6, hashlib.sha384(b"sample").hexdigest())
    with pytest.raises(RuntimeError):
        bundle.verify_model(sample, 6, "0" * 96)
    with pytest.raises(RuntimeError):
        bundle.verify_model(sample, 7, hashlib.sha384(b"sample").hexdigest())

def test_model_path_traversal_fails_before_network(tmp_path):
    with pytest.raises(RuntimeError):
        bundle.fetch_model("../../private.txt", 1, "0" * 96, tmp_path)
