from __future__ import annotations

import copy
import json
import os
import sqlite3
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from k5vision import identity_state
from k5vision.domain.devices import Device
from k5vision.identity_state import initialize_identity_state
from k5vision.media.mediamtx_config import MediaMTXSource
from k5vision.media.mediamtx_provider import MediaMTXBinding
from k5vision.operator_component_state import ComponentStateAnchor
from k5vision.operator_recording_policy import (
    MotionPolicy,
    RecordingPolicyConflict,
    RecordingPolicyError,
    RecordingPolicyStore,
    RecordingSourceChanged,
    validate_motion_policy,
)


@pytest.fixture
def policy(tmp_path, monkeypatch):
    if os.name == "posix":
        original = Path.lstat

        def trusted_ancestor(path, *args, **kwargs):
            value = original(path, *args, **kwargs)
            if path in (Path("/"), Path("/tmp")) and value.st_uid == 65534:
                fields = list(value)
                fields[4] = 0
                return os.stat_result(fields)
            return value

        monkeypatch.setattr(Path, "lstat", trusted_ancestor)
    identity = initialize_identity_state(
        tmp_path / "identity", username="motion-admin", password="motion-test-password"
    )
    camera = Device(name="Motion camera", host="127.0.0.1")
    source = MediaMTXSource(
        MediaMTXBinding(identity.site_id, camera.id),
        camera,
        "rtsp://user:private-password@127.0.0.1/live",
    )
    return RecordingPolicyStore(identity.directory, identity.site_id), source, identity


def _polygon(points=None):
    return {
        "id": str(uuid4()),
        "name": "Entrance",
        "points": points or [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]],
    }


def _motion(store, source):
    value = MotionPolicy(mode="motion").model_dump(mode="json")
    value["zones"] = {
        "reference": {
            "width": 1920,
            "height": 1080,
            "rotation": 0,
            "source_revision": store.source_revision(source),
        },
        "include": [_polygon()],
        "exclude": [],
    }
    return value


def test_policy_and_private_source_key_survive_restart(policy):
    store, source, identity = policy
    before = store.get(source)
    assert before["revision"] == 0 and before["mode"] == "continuous"
    assert before["pre_capture_seconds"] == 0
    saved = store.save(source, 0, _motion(store, source))
    assert saved["revision"] == 1 and saved["mode"] == "motion"
    store.close()
    reopened = RecordingPolicyStore(identity.directory, identity.site_id)
    assert reopened.get(source) == saved
    public = json.dumps(saved)
    assert "private-password" not in public and "rtsp://" not in public
    policy_file = identity.directory / "operator-recording-policy.json"
    if os.name == "posix":
        assert policy_file.stat().st_mode & 0o077 == 0
    # Windows st_mode does not represent DACL privacy. Exercise the native
    # ownership/access admission also used for every private policy read/write.
    identity_state._windows_admission(policy_file, private=True)


@pytest.mark.parametrize("operation", ["read", "save", "reopen"])
def test_native_policy_acl_refusal_blocks_access_without_changing_state(
    policy, monkeypatch, operation
):
    store, source, identity = policy
    saved = store.save(source, 0, _motion(store, source))
    before = store.path.read_bytes()
    native_admission = identity_state._windows_admission
    refused_policy = []

    def refused(path, **options):
        if path == store.path and options.get("private") is True:
            refused_policy.append(path.name)
            raise identity_state.IdentityStateError("native private access refused")
        return native_admission(path, **options)

    monkeypatch.setattr(identity_state, "_windows_admission", refused)
    with pytest.raises(RecordingPolicyError, match="^motion_policy_unavailable$"):
        if operation == "read":
            store.get(source)
        elif operation == "save":
            store.save(source, saved["revision"], MotionPolicy().model_dump(mode="json"))
        else:
            RecordingPolicyStore(identity.directory, identity.site_id)
    assert refused_policy == [store.path.name]
    assert store.path.read_bytes() == before


def test_native_staged_policy_acl_refusal_prevents_atomic_replacement(policy, monkeypatch):
    store, source, _ = policy
    before = store.path.read_bytes()
    native_admission = identity_state._windows_admission
    refused_staging = []

    def refused(path, **options):
        if (
            path.parent == store.root
            and path.name.startswith(".k5-motion-")
            and options.get("private") is True
        ):
            refused_staging.append(path.name)
            raise identity_state.IdentityStateError("native staging access refused")
        return native_admission(path, **options)

    monkeypatch.setattr(identity_state, "_windows_admission", refused)
    with pytest.raises(RecordingPolicyError, match="^motion_policy_unavailable$"):
        store.save(source, 0, _motion(store, source))
    assert refused_staging
    assert store.path.read_bytes() == before
    assert not list(store.root.glob(".k5-motion-*.tmp"))


def test_master_pause_and_labels_do_not_invalidate_source_basis(policy):
    store, source, _ = policy
    changed = replace(
        source,
        recording_enabled=False,
        device=source.device.model_copy(update={"name": "New label", "tags": {"tag"}}),
    )
    assert store.source_revision(changed) == store.source_revision(source)
    assert store.source_revision(
        replace(source, source_uri=source.source_uri + "2")
    ) != store.source_revision(source)


def test_stale_source_cannot_apply_authored_motion_zones(policy):
    store, source, _ = policy
    value = _motion(store, source)
    changed = replace(source, source_uri=source.source_uri + "-different")
    with pytest.raises(RecordingSourceChanged):
        store.save(changed, 0, value)
    assert store.get(source)["revision"] == 0


def test_saved_source_basis_exposes_change_without_erasing_intent(policy):
    store, source, _ = policy
    saved = store.save(source, 0, _motion(store, source))
    changed = replace(source, source_uri=source.source_uri + "-different")
    result = store.get(changed)
    assert result["mode"] == "motion" and result["revision"] == saved["revision"]
    assert result["source_revision"] != result["policy_source_revision"]
    assert result["zones"] == saved["zones"]


def test_stale_revision_never_mutates_disk_or_other_camera(policy):
    store, source, _ = policy
    saved = store.save(source, 0, _motion(store, source))
    original = store.path.read_bytes()
    with pytest.raises(RecordingPolicyConflict):
        store.save(source, 0, MotionPolicy().model_dump(mode="json"))
    assert store.path.read_bytes() == original and store.get(source) == saved
    other = Device(name="Other", host="127.0.0.2")
    second = MediaMTXSource(
        MediaMTXBinding(source.binding.site_id, other.id), other, "rtsp://127.0.0.2/live"
    )
    assert store.get(second)["mode"] == "continuous"


def test_missing_anchored_policy_never_reverts_to_continuous(policy):
    store, source, identity = policy
    store.save(source, 0, _motion(store, source))
    store.path.unlink()
    with pytest.raises(RecordingPolicyError):
        RecordingPolicyStore(identity.directory, identity.site_id)
    with pytest.raises(RecordingPolicyError):
        store.get(source)
    assert not store.path.exists()


@pytest.mark.parametrize("fault", ["key", "site", "version", "unknown", "duplicate", "nan"])
def test_corrupt_anchored_policy_is_refused_without_reinitialization(policy, fault):
    store, source, identity = policy
    store.save(source, 0, _motion(store, source))
    document = json.loads(store.path.read_text())
    if fault == "key":
        document["key"] = "0" * 64
    elif fault == "site":
        document["site_id"] = "foreign-site"
    elif fault == "version":
        document["schema_version"] = True
    elif fault == "unknown":
        document["foreign"] = "value"
    text = json.dumps(document)
    if fault == "duplicate":
        text = '{"site_id":"duplicate",' + text[1:]
    elif fault == "nan":
        text = text.replace('"revision": 2', '"revision": NaN', 1)
    store.path.write_text(text)
    raw = store.path.read_bytes()
    with pytest.raises(RecordingPolicyError):
        RecordingPolicyStore(identity.directory, identity.site_id)
    assert store.path.read_bytes() == raw


def test_dedicated_anchor_does_not_add_unknown_legacy_component(policy):
    _, _, identity = policy
    anchor = ComponentStateAnchor(identity.directory, identity.site_id, "roles", present=False)
    anchor.seal()
    with sqlite3.connect(identity.database_path) as connection:
        names = connection.execute("SELECT component FROM operator_component_anchors").fetchall()
    assert names == [("roles",)]


@pytest.mark.parametrize(
    "field,value",
    [
        ("mode", "person"),
        ("post_capture_seconds", True),
        ("post_capture_seconds", 0),
        ("post_capture_seconds", 121),
        ("sensitivity", 0),
        ("sensitivity", 101),
        ("minimum_area_percent", float("nan")),
        ("minimum_area_percent", float("inf")),
        ("minimum_area_percent", True),
        ("minimum_area_percent", 0.09),
        ("minimum_area_percent", 101),
        ("unknown", "PRIVATE"),
    ],
)
def test_invalid_policy_is_rejected_before_save(policy, field, value):
    store, source, _ = policy
    document = MotionPolicy().model_dump(mode="json")
    document[field] = value
    before = store.path.read_bytes()
    with pytest.raises(RecordingPolicyError):
        store.save(source, 0, document)
    assert store.path.read_bytes() == before


@pytest.mark.parametrize(
    "points",
    [
        [[0, 0], [1, 1], [0, 1], [1, 0]],
        [[0, 0], [0.5, 0.5], [1, 1]],
        [[0, 0], [1, 0], [1, 1], [0, 0]],
        [[-0.1, 0], [1, 0], [1, 1]],
        [[True, 0], [1, 0], [1, 1]],
        [[float("nan"), 0], [1, 0], [1, 1]],
        [[0, 0], [1, 0]],
        [[i / 17, (i % 2) / 2] for i in range(17)],
    ],
)
def test_invalid_polygon_is_rejected(policy, points):
    store, source, _ = policy
    value = _motion(store, source)
    value["zones"]["include"] = [_polygon(points)]
    with pytest.raises(RecordingPolicyError):
        validate_motion_policy(value)


def test_shared_zone_id_and_missing_reference_are_rejected(policy):
    store, source, _ = policy
    value = _motion(store, source)
    value["zones"]["exclude"] = copy.deepcopy(value["zones"]["include"])
    with pytest.raises(RecordingPolicyError):
        validate_motion_policy(value)
    value["zones"]["exclude"] = []
    value["zones"]["reference"] = None
    with pytest.raises(RecordingPolicyError):
        validate_motion_policy(value)


@pytest.mark.parametrize(
    "field,value", [("rotation", False), ("rotation", 0.0), ("width", 8193), ("height", True)]
)
def test_native_reference_requires_exact_supported_geometry(policy, field, value):
    store, source, _ = policy
    document = _motion(store, source)
    document["zones"]["reference"][field] = value
    with pytest.raises(RecordingPolicyError):
        validate_motion_policy(document)


def test_eight_total_polygons_not_eight_per_kind(policy):
    store, source, _ = policy
    value = _motion(store, source)
    value["zones"]["include"] = [_polygon() for _ in range(5)]
    value["zones"]["exclude"] = [_polygon() for _ in range(4)]
    with pytest.raises(RecordingPolicyError):
        validate_motion_policy(value)


def test_failed_atomic_replace_preserves_previous_policy_and_removes_temp(policy, monkeypatch):
    store, source, _ = policy
    before = store.path.read_bytes()
    monkeypatch.setattr(os, "replace", lambda *_: (_ for _ in ()).throw(OSError("PRIVATE")))
    with pytest.raises(RecordingPolicyError, match="motion_policy_unavailable"):
        store.save(source, 0, _motion(store, source))
    assert store.path.read_bytes() == before
    assert not list(store.root.glob(".k5-motion-*.tmp"))


def test_private_profile_change_invalidates_same_stream_uri(policy):
    store, source, _ = policy
    assert store.source_revision(replace(source, profile_key="a" * 64)) != store.source_revision(
        source
    )


def test_committed_camera_removal_frees_policy_without_touching_other_intent(policy):
    store, source, _ = policy
    store.save(source, 0, _motion(store, source))
    camera = Device(name="Retained camera", host="127.0.0.2")
    other = MediaMTXSource(
        MediaMTXBinding(source.binding.site_id, camera.id), camera, "rtsp://127.0.0.2/live"
    )
    saved = store.save(other, 0, MotionPolicy().model_dump(mode="json"))
    assert store.remove(source.binding.device_id) is True
    assert store.remove(source.binding.device_id) is False
    assert store.get(other) == saved and store.get(source)["revision"] == 0


def test_startup_prune_uses_canonical_ids_and_keeps_current_policy_basis(policy):
    store, source, _ = policy
    saved = store.save(source, 0, _motion(store, source))
    raw = store.path.read_bytes()
    assert store.prune_existing((source.binding.device_id,)) == 0
    assert store.path.read_bytes() == raw and store.get(source) == saved
    assert store.prune_existing(()) == 1
    assert store.get(source)["revision"] == 0


@pytest.mark.parametrize("identifiers", [[True], ["not-a-camera"], [uuid4()] * 2])
def test_invalid_prune_snapshot_is_refused_before_mutating_state(policy, identifiers):
    store, _, _ = policy
    raw = store.path.read_bytes()
    with pytest.raises(RecordingPolicyError, match="motion_invalid_request"):
        store.prune_existing(identifiers)
    assert store.path.read_bytes() == raw


def test_original_authority_denial_is_preserved_at_final_atomic_fence(policy):
    from fastapi import HTTPException

    store, source, _ = policy
    original = store.path.read_bytes()
    denied = HTTPException(403, "Access revoked")

    def reject():
        raise denied

    with pytest.raises(HTTPException) as error:
        store.save(source, 0, _motion(store, source), before_write=reject)
    assert error.value is denied and store.path.read_bytes() == original
    assert not list(store.root.glob(".k5-motion-*.tmp"))
