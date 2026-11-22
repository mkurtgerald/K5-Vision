from __future__ import annotations

import copy
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from test_legacy_identity_snapshot import _hashes, _prepare, _Protector
from test_legacy_identity_snapshot import legacy as legacy_fixture
from test_legacy_identity_snapshot import private_profile as private_profile_fixture

from k5vision import identity_state
from k5vision import legacy_migration_target as target
from k5vision import operator_identity_security as security_module
from k5vision.domain.devices import DeviceCreate
from k5vision.operator_audit import OperatorAuditStore
from k5vision.operator_events import OperatorEventStore
from k5vision.operator_identity_security import AccountSecurityStore
from k5vision.operator_integrations import OperatorIntegrationStore
from k5vision.operator_product_state import OperatorCameraStore
from k5vision.operator_recording_policy import RecordingPolicyStore
from k5vision.operator_role_policy import OperatorRolePolicy
from k5vision.operator_storage_settings import OperatorStorageSettings
from k5vision.services.device_registry import DeviceRegistry
from k5vision.services.user_registry import UserRegistry

legacy = legacy_fixture
private_profile = private_profile_fixture


class _Admission:
    revoked = False

    def assert_authenticated(self, binding):
        if self.revoked:
            raise RuntimeError("PRIVATE transport credential detail")
        if self.bound != binding:
            raise ValueError


def _metadata(area, path, root):
    info = path.stat()
    return {
        "area": area,
        "relative": path.relative_to(root).as_posix(),
        "size": info.st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "mtime_ns": info.st_mtime_ns,
    }


def _write(path, content):
    path.write_bytes(content)
    path.chmod(0o600)


@pytest.fixture
def prepared(legacy, private_profile, monkeypatch):
    state = identity_state.load_identity_state(legacy.source)
    recordings = private_profile / "old-recordings"
    recordings.mkdir(mode=0o700)
    settings = OperatorStorageSettings(legacy.source, state.site_id, recordings)
    settings.configure(expected_revision=0, quota_bytes=40 * 1024**3, retention_seconds=123456)
    registry = DeviceRegistry(
        database_path=legacy.source / "devices.sqlite3", site_id=state.site_id
    )
    device = registry.register(DeviceCreate(name="Preserved camera", host="192.0.2.10"))
    cameras = OperatorCameraStore(legacy.source, state.site_id, registry=registry)
    source = cameras.upsert(device, "rtsp://camera-user:retained-password@192.0.2.10/live")
    policy = RecordingPolicyStore(legacy.source, state.site_id)
    policy.get(source)
    policy.close()
    cameras.close()
    registry.close()
    for factory in (
        OperatorRolePolicy,
        OperatorAuditStore,
        OperatorEventStore,
        OperatorIntegrationStore,
    ):
        factory(legacy.source, state.site_id).close()
    receipt = _prepare(legacy)
    before = _hashes(legacy.source)
    root = private_profile / "service-data"
    root.mkdir(mode=0o700)
    binding = target.TargetBinding(
        state.site_id, legacy.transfer_id, receipt["source_digest"], "S-1-5-21-1-2-3-1001"
    )
    admission = _Admission()
    admission.bound = binding
    monkeypatch.setattr(target, "ProfileSecretProtector", lambda site: legacy.new)
    monkeypatch.setattr(security_module, "ProfileSecretProtector", lambda site: legacy.new)
    session = target.TargetMigration(root, binding, admission)
    paths = session.begin()
    for path in legacy.destination.iterdir():
        destination = Path(paths["identity"]) / path.name
        shutil.copy2(path, destination)
        session.record_entry(_metadata("identity", destination, destination.parent))
    media_root = Path(paths["recordings"])
    camera_root = media_root / ("k5_" + "1" * 64)
    camera_root.mkdir(mode=0o700)
    segment = camera_root / "2026-11-22_02-00-00-000000_+0530.mp4"
    # Retained segments are copied and hashed byte-for-byte, never transcoded.
    _write(segment, b"retained recording bytes" * 100)
    session.record_entry(_metadata("recordings", segment, media_root))
    session.set_snapshot(receipt)
    fixture = SimpleNamespace(
        legacy=legacy,
        root=root,
        binding=binding,
        admission=admission,
        session=session,
        paths=paths,
        receipt=receipt,
        source_hashes=before,
        device=device,
        segment=segment,
        media_bytes=segment.read_bytes(),
    )
    yield fixture
    session.close()


def _resume(prepared):
    prepared.session.close()
    session = target.TargetMigration(prepared.root, prepared.binding, prepared.admission)
    prepared.session = session
    return session


def test_prepare_is_nonactive_then_explicit_commit_preserves_real_authorities(prepared):
    p = prepared
    result = p.session.prepare_activation()
    assert result["prepared"] is True and result["activated"] is False
    assert not (p.root / "identity").exists()
    assert p.segment.read_bytes() == p.media_bytes
    assert list((p.root / "recordings").iterdir()) == []
    assert target.inspect_target(p.root)["status"] == "pending"
    assert p.session.commit()["activated"] is True
    active = p.root / "identity"
    state = identity_state.load_identity_state(active)
    assert state.site_id == p.binding.site_id
    assert (
        p.root / "recordings" / p.segment.parent.name / p.segment.name
    ).read_bytes() == p.media_bytes
    assert _hashes(p.legacy.source) == p.source_hashes
    with sqlite3.connect(active / "operator-account-security.sqlite3") as connection:
        mfa = connection.execute("SELECT * FROM mfa").fetchone()
        assert not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name=?", (target.snapshot._TABLE,)
        ).fetchall()
    assert p.legacy.new.unprotect(mfa[2], p.legacy.user_id) == p.legacy.secret
    with pytest.raises(InvalidTag):
        p.legacy.old.unprotect(mfa[2], p.legacy.user_id)
    security = AccountSecurityStore(active, state.site_id)
    assert security.status(UUID(p.legacy.user_id))["mfa_enabled"] is True
    security.close()
    users = UserRegistry(database_path=state.database_path, site_id=state.site_id)
    account, _ = users.verify_password_snapshot(
        username="legacy-admin", password="legacy-admin-password"
    )
    assert str(account.id) == p.legacy.user_id
    users.close()
    settings = json.loads((active / "operator-storage.json").read_text())
    assert settings["recording_directory"] == str(p.root / "recordings")
    assert settings["quota_bytes"] == 40 * 1024**3 and settings["retention_seconds"] == 123456
    assert not (active / target.snapshot._MARKER).exists()
    assert not (active / target.snapshot._STAGED_MANIFEST).exists()
    assert target.inspect_target(p.root)["status"] == "committed"
    with pytest.raises(target.TargetMigrationError):
        p.session.rollback()


def test_live_admission_is_required_again_after_long_prepare(prepared):
    p = prepared
    p.session.prepare_activation()
    p.admission.revoked = True
    with pytest.raises(
        target.TargetMigrationError, match="^Migration target was unavailable or refused$"
    ) as error:
        p.session.commit()
    assert error.value.__context__ is None
    assert not (p.root / "identity").exists()
    assert p.segment.exists()
    assert _hashes(p.legacy.source) == p.source_hashes


@pytest.mark.parametrize("admission", [True, False, None, lambda: None])
def test_boolean_or_unbound_approval_is_not_service_admission(prepared, admission):
    with pytest.raises(target.TargetMigrationError):
        target.TargetMigration(prepared.root, prepared.binding, admission)


def test_same_binding_resumes_ledger_once_and_duplicate_registration_is_idempotent(prepared):
    p = prepared
    before = p.session.status()
    session = _resume(p)
    assert session.begin()["registered_files"] == before["registered_files"]
    entry = _metadata("recordings", p.segment, Path(p.paths["recordings"]))
    assert session.record_entry(entry)["registered_files"] == before["registered_files"]
    assert session.activate()["activated"] is True


def test_another_target_instance_cannot_own_same_data_root(prepared):
    other = target.TargetMigration(prepared.root, prepared.binding, prepared.admission)
    try:
        with pytest.raises(target.TargetMigrationError):
            other.begin()
        assert prepared.session.status()["phase"] == "copying"
    finally:
        other.close()


def test_wrong_binding_cannot_resume_existing_private_stage(prepared):
    p = prepared
    p.session.close()
    binding = target.TargetBinding(
        p.binding.site_id, p.binding.transfer_id, "0" * 64, p.binding.source_owner_sid
    )
    admission = _Admission()
    admission.bound = binding
    with target.TargetMigration(p.root, binding, admission) as other:
        with pytest.raises(target.TargetMigrationError):
            other.begin()
    assert not (p.root / "identity").exists()


@pytest.mark.parametrize("mutation", ["content", "extra_file", "extra_stage", "link", "directory"])
def test_altered_or_unknown_staged_content_refuses_without_source_changes(prepared, mutation):
    p = prepared
    if mutation == "content":
        _write(p.segment, b"changed retained bytes")
    elif mutation == "extra_file":
        _write(p.segment.parent / "2026-11-22_02-01-00-000000.mp4", b"unregistered")
    elif mutation == "extra_stage":
        _write(p.session.stage / "unexpected", b"unknown private state")
    elif mutation == "link":
        original = p.segment.with_suffix(".saved")
        p.segment.rename(original)
        p.segment.symlink_to(original)
    else:
        old = Path(p.paths["recordings"])
        old.rename(p.session.stage / "foreign-recordings")
        old.mkdir(mode=0o700)
    with pytest.raises(target.TargetMigrationError):
        p.session.prepare_activation()
    assert not (p.root / "identity").exists()
    assert _hashes(p.legacy.source) == p.source_hashes


@pytest.mark.parametrize("where", ["raw", "clone"])
def test_last_progress_callback_cannot_change_already_verified_identity(prepared, where):
    p = prepared

    def progress(note):
        if note["phase"] == "prepared":
            directory = p.session.stage / ("identity" if where == "raw" else "validated-identity")
            with sqlite3.connect(directory / "operator-account-security.sqlite3") as connection:
                connection.execute("UPDATE mfa SET last_counter=last_counter+1")

    with pytest.raises(target.TargetMigrationError):
        p.session.prepare_activation(progress=progress)
    assert not (p.root / "identity").exists()
    assert p.segment.exists()
    assert _hashes(p.legacy.source) == p.source_hashes


def test_prepared_clone_tamper_is_refused_at_explicit_commit(prepared):
    p = prepared
    p.session.prepare_activation()
    with sqlite3.connect(p.session.stage / "validated-identity" / "users.sqlite3") as connection:
        connection.execute("UPDATE users SET enabled=0")
    with pytest.raises(target.TargetMigrationError):
        p.session.commit()
    assert not (p.root / "identity").exists()
    assert p.segment.exists()


def test_status_and_cancel_remain_responsive_during_long_verification(prepared):
    p = prepared
    entered, release = threading.Event(), threading.Event()
    errors = []

    def progress(note):
        if not entered.is_set():
            entered.set()
            assert release.wait(3)

    def run():
        try:
            p.session.prepare_activation(progress=progress)
        except target.TargetMigrationError as error:
            errors.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    assert entered.wait(3)
    try:
        assert p.session.status()["phase"] == "verifying"
        p.session.cancel()
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive() and len(errors) == 1
    assert not (p.root / "identity").exists()
    assert p.segment.exists()


def test_prepare_restart_requires_recovery_and_fresh_prepare_before_commit(prepared):
    p = prepared
    p.session.prepare_activation()
    session = _resume(p)
    assert session.begin()["phase"] == "prepared"
    with pytest.raises(target.TargetMigrationError):
        session.commit()
    assert session.recover()["phase"] == "rolled_back"
    assert not (p.root / "identity").exists()
    session = _resume(p)
    assert session.begin()["phase"] == "copying"
    assert session.activate()["activated"] is True


@pytest.mark.parametrize("mutation", ["tail", "truncated", "state", "ledger_replaced"])
def test_resume_refuses_ledger_or_state_tamper(prepared, mutation):
    p = prepared
    stage = p.session.stage
    p.session.close()
    ledger = stage / target._LEDGER
    if mutation == "tail":
        with ledger.open("ab") as stream:
            stream.write(b"{}\n")
    elif mutation == "truncated":
        ledger.write_bytes(ledger.read_bytes()[:-1])
    elif mutation == "state":
        document = json.loads((stage / target._STATE).read_text())
        document["revision"] = True
        (stage / target._STATE).write_text(json.dumps(document))
    else:
        content = ledger.read_bytes()
        ledger.rename(stage / "replaced-ledger")
        _write(ledger, content)
    session = _resume(p)
    with pytest.raises(target.TargetMigrationError):
        session.begin()
    assert not (p.root / "identity").exists()


def test_failure_between_recordings_and_identity_publication_restores_owned_stage(
    prepared, monkeypatch
):
    p = prepared
    rename = target._rename_new

    def fail_identity(source, destination):
        if destination == p.root / "identity":
            raise OSError("private native rename failure")
        return rename(source, destination)

    p.session.prepare_activation()
    monkeypatch.setattr(target, "_rename_new", fail_identity)
    with pytest.raises(target.TargetMigrationError):
        p.session.commit()
    assert p.segment.read_bytes() == p.media_bytes
    assert list((p.root / "recordings").iterdir()) == []
    assert not (p.root / "identity").exists()
    assert p.session.status()["phase"] == "rolled_back"
    assert _hashes(p.legacy.source) == p.source_hashes


def test_foreign_global_identity_is_never_overwritten_or_removed(prepared):
    p = prepared
    p.session.prepare_activation()
    foreign = p.root / "identity"
    foreign.mkdir(mode=0o700)
    _write(foreign / "keep", b"existing unrelated site")
    with pytest.raises(target.TargetMigrationError):
        p.session.commit()
    p.session.rollback()
    assert (foreign / "keep").read_bytes() == b"existing unrelated site"
    assert p.segment.exists()


def test_completed_export_bytes_and_metadata_publish_under_new_identity(prepared):
    if not Path("/usr/bin/ffmpeg").exists():
        pytest.skip("requires actual MP4 generation, exercised by native Windows gate")
    p = prepared
    exports = Path(p.paths["exports"])
    identifier = str(uuid4())
    media = exports / (identifier + ".mp4")
    subprocess.run(
        [
            "/usr/bin/ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x120:rate=10",
            "-t",
            "2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(media),
        ],
        check=True,
        capture_output=True,
        timeout=20,
    )
    media.chmod(0o600)
    value = {
        "schema_version": 1,
        "site_id": p.binding.site_id,
        "id": identifier,
        "owner": p.legacy.user_id,
        "camera": str(p.device.id),
        "start": "2026-11-22T02:00:00+05:30",
        "duration": 2,
        "created": "2026-11-22T02:02:00Z",
        "bytes": media.stat().st_size,
        "sha256": hashlib.sha256(media.read_bytes()).hexdigest(),
    }
    metadata = exports / (identifier + ".json")
    _write(metadata, json.dumps(value).encode())
    for path in (media, metadata):
        p.session.record_entry(_metadata("exports", path, exports))
    content = media.read_bytes()
    assert p.session.activate()["activated"] is True
    assert (p.root / "identity" / "exports" / media.name).read_bytes() == content
    assert json.loads((p.root / "identity" / "exports" / metadata.name).read_text()) == value


class _PowerLoss(BaseException):
    pass


@pytest.mark.parametrize("phase", ["recordings_published", "identity_published"])
def test_process_crash_after_directory_rename_stays_pending_and_recovers(
    prepared, monkeypatch, phase
):
    p = prepared
    p.session.prepare_activation()
    save = p.session._save

    def crash(**changes):
        if changes.get("phase") == phase:
            raise _PowerLoss
        return save(**changes)

    monkeypatch.setattr(p.session, "_save", crash)
    with pytest.raises(_PowerLoss):
        p.session.commit()
    assert target.inspect_target(p.root)["status"] == "pending"
    session = _resume(p)
    assert session.begin()["status"] == "pending"
    assert session.recover()["phase"] == "rolled_back"
    assert not (p.root / "identity").exists()
    assert p.segment.read_bytes() == p.media_bytes
    assert list((p.root / "recordings").iterdir()) == []
    assert _hashes(p.legacy.source) == p.source_hashes


def test_new_authenticated_attempt_can_recover_old_fingerprint_after_mfa_change(
    prepared, monkeypatch
):
    p = prepared
    p.session.prepare_activation()
    save = p.session._save

    def crash(**changes):
        if changes.get("phase") == "identity_published":
            raise _PowerLoss
        return save(**changes)

    monkeypatch.setattr(p.session, "_save", crash)
    with pytest.raises(_PowerLoss):
        p.session.commit()
    p.session.close()
    binding = target.TargetBinding(
        p.binding.site_id, str(uuid4()), "2" * 64, p.binding.source_owner_sid
    )
    admission = _Admission()
    admission.bound = binding
    assert target.recover_pending(p.root, binding, admission) == {
        "status": "recovered",
        "recovered_transfers": 1,
    }
    assert not (p.root / "identity").exists()
    assert p.segment.read_bytes() == p.media_bytes
    assert _hashes(p.legacy.source) == p.source_hashes
    with target.TargetMigration(p.root, binding, admission) as session:
        assert session.begin()["phase"] == "copying"
    assert (p.session.stage / "identity" / "identity.json.snapshot").exists()


@pytest.mark.parametrize("foreign", ["site", "owner", "committed", "revoked"])
def test_recovery_never_broadens_site_owner_or_committed_authority(prepared, foreign):
    p = prepared
    if foreign == "committed":
        p.session.activate()
    p.session.close()
    binding = target.TargetBinding(
        "install-" + uuid4().hex if foreign == "site" else p.binding.site_id,
        str(uuid4()),
        "2" * 64,
        "S-1-5-21-1-2-3-1002" if foreign == "owner" else p.binding.source_owner_sid,
    )
    admission = _Admission()
    admission.bound = binding
    admission.revoked = foreign == "revoked"
    before = (p.session.stage / target._STATE).read_bytes()
    with pytest.raises(target.TargetMigrationError):
        target.recover_pending(p.root, binding, admission)
    assert (p.session.stage / target._STATE).read_bytes() == before
    assert _hashes(p.legacy.source) == p.source_hashes


def test_service_secret_boundary_requires_valid_purpose_and_verification(prepared):
    p = prepared
    protected = p.session.protect(p.legacy.user_id, p.legacy.secret)
    assert protected.startswith(b"dpapi-v1:")
    p.session.verify(p.legacy.user_id, p.legacy.secret, protected)
    with pytest.raises(InvalidTag):
        p.legacy.old.unprotect(protected, p.legacy.user_id)
    for purpose, secret in (
        ("arbitrary", p.legacy.secret),
        (p.legacy.user_id, b"short"),
        ("oidc-client-secret", b""),
    ):
        with pytest.raises(target.TargetMigrationError):
            p.session.protect(purpose, secret)
    with pytest.raises(target.TargetMigrationError):
        p.session.verify(
            p.legacy.user_id, p.legacy.secret, b"private-profile-v1:" + p.legacy.secret
        )
    assert p.legacy.secret not in (p.session.stage / target._STATE).read_bytes()


def test_failure_to_begin_releases_service_root_lock(prepared):
    p = prepared
    p.session.close()
    binding = target.TargetBinding(
        p.binding.site_id, p.binding.transfer_id, "3" * 64, p.binding.source_owner_sid
    )
    admission = _Admission()
    admission.bound = binding
    refused = target.TargetMigration(p.root, binding, admission)
    with pytest.raises(target.TargetMigrationError):
        refused.begin()
    # A failed peer binding must not retain an otherwise invisible service lock.
    session = _resume(p)
    assert session.begin()["phase"] == "copying"


@pytest.mark.parametrize("stop", [lambda: True, lambda: None, lambda: 1])
def test_non_false_stop_result_refuses_before_publication(prepared, stop):
    with pytest.raises(target.TargetMigrationError):
        prepared.session.prepare_activation(stop=stop)
    assert not (prepared.root / "identity").exists()
    assert prepared.segment.exists()


def _minimal_target(legacy, profile, monkeypatch):
    receipt = _prepare(legacy)
    root = profile / ("target-" + uuid4().hex)
    root.mkdir(mode=0o700)
    binding = target.TargetBinding(
        receipt["site_id"], legacy.transfer_id, receipt["source_digest"], "S-1-5-21-1-2-3-1001"
    )
    admission = _Admission()
    admission.bound = binding
    monkeypatch.setattr(target, "ProfileSecretProtector", lambda site: legacy.new)
    monkeypatch.setattr(security_module, "ProfileSecretProtector", lambda site: legacy.new)
    session = target.TargetMigration(root, binding, admission)
    paths = session.begin()
    for path in legacy.destination.iterdir():
        destination = Path(paths["identity"]) / path.name
        shutil.copy2(path, destination)
        session.record_entry(_metadata("identity", destination, destination.parent))
    session.set_snapshot(receipt)
    return session


@pytest.mark.parametrize("component", ["roles", "storage", "policy"])
def test_missing_anchored_source_authority_cannot_be_silently_initialized(
    legacy, private_profile, monkeypatch, component
):
    site = identity_state.load_identity_state(legacy.source).site_id
    if component == "roles":
        OperatorRolePolicy(legacy.source, site).close()
        missing = legacy.source / "role-profiles.json"
    elif component == "storage":
        media = private_profile / "original-recordings"
        media.mkdir(mode=0o700)
        OperatorStorageSettings(legacy.source, site, media).configure(
            expected_revision=0, retention_seconds=200
        )
        missing = legacy.source / "operator-storage.json"
    else:
        RecordingPolicyStore(legacy.source, site).close()
        missing = legacy.source / "operator-recording-policy.json"
    missing.unlink()
    session = _minimal_target(legacy, private_profile, monkeypatch)
    before = _hashes(legacy.source)
    try:
        with pytest.raises(target.TargetMigrationError):
            session.prepare_activation()
        assert not (session.root / "identity").exists()
        assert not missing.exists() and _hashes(legacy.source) == before
    finally:
        session.close()


def test_service_cannot_activate_another_principals_ciphertext(prepared, monkeypatch):
    p = prepared
    wrong = _Protector(p.binding.site_id, AESGCM.generate_key(128))
    monkeypatch.setattr(security_module, "ProfileSecretProtector", lambda site: wrong)
    with pytest.raises(target.TargetMigrationError):
        p.session.prepare_activation()
    assert not (p.root / "identity").exists()
    assert _hashes(p.legacy.source) == p.source_hashes


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("native_activation_required", 1),
        ("activation_allowed", 0),
        ("omitted_source_entries", ["../outside"]),
    ],
)
def test_receipt_requires_exact_finite_native_snapshot_contract(prepared, field, value):
    receipt = copy.deepcopy(prepared.receipt)
    receipt[field] = value
    with pytest.raises(target.TargetMigrationError):
        prepared.session.set_snapshot(receipt)
    assert prepared.session.status()["phase"] == "copying"


@pytest.mark.parametrize(
    "area,relative",
    [
        ("identity", "identity.json"),
        ("identity", "../users.sqlite3"),
        ("recordings", "../escape.mp4"),
        ("recordings", "camera/a.mp4"),
        ("recordings", "k5_" + "1" * 64 + "/2026-02-31_01-00-00-000000.mp4"),
        ("exports", "../job.mp4"),
        ("exports", str(uuid4()) + ".part.mp4"),
    ],
)
def test_registration_rejects_noncanonical_or_activation_paths(prepared, area, relative):
    with pytest.raises(target.TargetMigrationError):
        prepared.session.record_entry(
            {"area": area, "relative": relative, "size": 1, "sha256": "0" * 64, "mtime_ns": 0}
        )


@pytest.mark.parametrize("tail", ["absent", "prefix", "complete", "corrupt"])
def test_durable_pending_registration_resumes_only_its_exact_append(prepared, monkeypatch, tail):
    p = prepared
    segment = p.segment.parent / "2026-11-22_02-01-00-000000.mp4"
    _write(segment, b"another retained segment" * 100)
    entry = _metadata("recordings", segment, Path(p.paths["recordings"]))
    previous_count = p.session.status()["registered_files"]
    save = p.session._save

    def crash(**changes):
        if tail == "complete" and changes.get("pending_entry", "absent") is None:
            raise _PowerLoss
        result = save(**changes)
        if tail != "complete" and changes.get("pending_entry") is not None:
            raise _PowerLoss
        return result

    monkeypatch.setattr(p.session, "_save", crash)
    with pytest.raises(_PowerLoss):
        p.session.record_entry(entry)
    state = json.loads((p.session.stage / target._STATE).read_text())
    ledger = p.session.stage / target._LEDGER
    if tail in {"prefix", "corrupt"}:
        pending = state["pending_entry"]
        head = hashlib.sha256(bytes.fromhex(state["head"]) + target._canonical(pending)).hexdigest()
        raw = target._canonical({"entry": pending, "previous": state["head"], "hash": head}) + b"\n"
        with ledger.open("ab") as stream:
            stream.write(raw[: len(raw) // 2] if tail == "prefix" else b"foreign append")
    session = _resume(p)
    if tail == "corrupt":
        before = ledger.read_bytes()
        with pytest.raises(target.TargetMigrationError):
            session.begin()
        assert ledger.read_bytes() == before
    else:
        assert session.begin()["registered_files"] == previous_count + 1
        assert session.record_entry(entry)["registered_files"] == previous_count + 1
        assert json.loads((session.stage / target._STATE).read_text())["pending_entry"] is None
        assert session.activate()["activated"] is True


@pytest.mark.parametrize("after_move", [False, True])
def test_private_clone_retirement_has_recoverable_before_and_after_move_journal(
    prepared, monkeypatch, after_move
):
    p = prepared
    p.session.prepare_activation()
    p.session.rollback()
    session = _resume(p)
    session.begin()
    rename = target._rename_new

    def crash(source, destination):
        if destination.name.startswith("retired-identity-"):
            if after_move:
                rename(source, destination)
            raise _PowerLoss
        return rename(source, destination)

    monkeypatch.setattr(target, "_rename_new", crash)
    with pytest.raises(_PowerLoss):
        session.prepare_activation()
    monkeypatch.setattr(target, "_rename_new", rename)
    session = _resume(p)
    session.begin()
    session.recover()
    session = _resume(p)
    session.begin()
    assert session.activate()["activated"] is True
    assert len(session._state["retired_clone_ids"]) == 1


def test_postwrite_commit_error_cannot_rollback_visible_committed_site(prepared, monkeypatch):
    p = prepared
    p.session.prepare_activation()
    atomic_json = target._atomic_json
    persisted = []

    def persisted_then_error(path, value):
        atomic_json(path, value)
        if path.name == target._STATE and value.get("phase") == "committed":
            persisted.append(True)
            raise OSError("private durability acknowledgement failed")

    monkeypatch.setattr(target, "_atomic_json", persisted_then_error)
    with pytest.raises(target.TargetMigrationError):
        p.session.commit()
    state = target._read_json(p.session.stage / target._STATE)
    assert persisted and state["phase"] == "committed"
    assert p.session.status()["status"] == "committed"
    assert identity_state.load_identity_state(p.root / "identity").site_id == p.binding.site_id
    assert (
        p.root / "recordings" / p.segment.parent.name / p.segment.name
    ).read_bytes() == p.media_bytes
    assert _hashes(p.legacy.source) == p.source_hashes
    with pytest.raises(target.TargetMigrationError):
        p.session.rollback()
    assert target.inspect_target(p.root)["status"] == "committed"


@pytest.mark.parametrize("tail", ["empty", "partial", "complete"])
def test_actual_process_exit_leaves_inert_atomic_temp_and_new_authorized_attempt_recovers(
    prepared, tail
):
    p = prepared
    p.session.prepare_activation()
    state_path = p.session.stage / target._STATE
    state_before = state_path.read_bytes()
    program = """
import json
import os
import sys
from pathlib import Path
from k5vision import legacy_migration_target as target
# Apply only the same managed-container ancestor fixture as the parent test.
if os.name == 'posix':
    lstat = Path.lstat
    def admitted_system_ancestors(path, *arguments, **keywords):
        info = lstat(path, *arguments, **keywords)
        if path in (Path('/'), Path('/tmp')) and info.st_uid == 65534:
            fields = list(info)
            fields[4] = 0
            return os.stat_result(fields)
        return info
    Path.lstat = admitted_system_ancestors
def crash(*arguments, **keywords):
    os._exit(73)
path = Path(sys.argv[1])
state = json.loads(path.read_bytes())
state['revision'] += 1
if sys.argv[2] == 'complete':
    if os.name == 'nt':
        target._windows_move = crash
    else:
        target.os.replace = crash
else:
    fdopen = target.os.fdopen
    def crash_write(descriptor, *arguments, **keywords):
        output = fdopen(descriptor, *arguments, **keywords)
        payload = target._canonical(state)
        output.write(payload[:len(payload) // 2] if sys.argv[2] == 'partial' else b'')
        output.flush()
        os.fsync(output.fileno())
        os._exit(73)
    target.os.fdopen = crash_write
target._atomic_json(path, state)
"""
    process = subprocess.run(
        [sys.executable, "-I", "-c", program, str(state_path), tail],
        capture_output=True,
        timeout=10,
    )
    assert process.returncode == 73
    assert state_path.read_bytes() == state_before
    orphans = list(p.session.stage.rglob(".target-*.tmp"))
    assert len(orphans) == 1
    orphan = orphans[0]
    identity = target._id(orphan.stat())
    content = orphan.read_bytes()
    assert content != state_before and bool(content) is (tail != "empty")
    p.session.close()
    binding = target.TargetBinding(
        p.binding.site_id, str(uuid4()), "3" * 64, p.binding.source_owner_sid
    )
    admission = _Admission()
    admission.bound = binding
    assert target.recover_pending(p.root, binding, admission)["status"] == "recovered"
    assert orphan.read_bytes() == content and target._id(orphan.stat()) == identity
    assert not (p.root / "identity").exists()
    assert p.segment.read_bytes() == p.media_bytes
    assert _hashes(p.legacy.source) == p.source_hashes


@pytest.mark.parametrize("unsafe", ["top_level", "name", "link", "large", "replaced_directory"])
def test_atomic_temp_custody_does_not_admit_foreign_files_or_directory_replacement(
    prepared, unsafe
):
    p = prepared
    atomic = p.session.stage / target._ATOMIC
    if unsafe == "replaced_directory":
        atomic.rename(p.session.stage / "foreign-atomic")
        atomic.mkdir(mode=0o700)
    else:
        path = (p.session.stage if unsafe == "top_level" else atomic) / (
            ".target-" + uuid4().hex + ".tmp"
        )
        if unsafe == "name":
            path = atomic / "unrelated"
        if unsafe == "link":
            path.symlink_to(p.segment)
        else:
            _write(path, b"x" * (32769 if unsafe == "large" else 10))
    with pytest.raises(target.TargetMigrationError):
        p.session.prepare_activation()
    assert not (p.root / "identity").exists()
    assert p.segment.read_bytes() == p.media_bytes
    assert _hashes(p.legacy.source) == p.source_hashes
