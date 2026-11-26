import io
import json
from pathlib import Path
from uuid import uuid4

import pytest
from test_legacy_identity_snapshot import legacy as legacy_fixture
from test_legacy_migration_target import prepared as prepared_fixture
from test_operator_time_admin import private_profile as private_profile_fixture

from k5vision import identity_state, service_setup
from k5vision import legacy_migration_target as target
from k5vision.windows_identity_security import AccessEntry, _check_policy

private_profile = private_profile_fixture
legacy = legacy_fixture
prepared = prepared_fixture
SERVICE = "S-1-5-80-1-2-3-4-5"


def _model_public_parent(monkeypatch, public):
    original = identity_state._windows_admission
    calls = []

    def admit(path, *, private, directory=False):
        calls.append((Path(path), private))
        if Path(path) == public:
            _check_policy(
                "S-1-5-18",
                (AccessEntry(0, 0, 0x116, "S-1-5-32-545"),),
                SERVICE,
                private=private,
                require_file_inheritance=False,
                trusted_installer_verified=False,
                recorder_program_data=True,
            )
        else:
            original(path, private=private, directory=directory)

    monkeypatch.setattr(identity_state, "_windows_admission", admit)
    return calls


@pytest.fixture
def boundary(private_profile, monkeypatch):
    public = private_profile / "KnownProgramData"
    public.mkdir(mode=0o700)
    root = public / "K5Vision"
    root.mkdir(mode=0o700)
    calls = _model_public_parent(monkeypatch, public)
    return root, calls


def test_fresh_service_root_does_not_make_public_programdata_a_private_boundary(boundary):
    root, calls = boundary
    assert not service_setup._migration_required(service_setup._Layout(root, root))
    assert (root.parent, False) in calls
    assert (root, True) in calls


def test_target_startup_inspection_keeps_private_boundary_at_service_root(boundary):
    root, calls = boundary
    assert target.inspect_target(root) == {"status": "none"}
    assert (root.parent, False) in calls and (root, True) in calls


def test_target_transaction_admits_service_root_without_trusting_programdata(boundary):
    root, calls = boundary
    binding = target.TargetBinding(
        "install-" + uuid4().hex, str(uuid4()), "a" * 64, "S-1-5-21-1-2-3-1001"
    )

    class Admission:
        def assert_authenticated(self, candidate):
            assert candidate == binding

    with target.TargetMigration(root, binding, Admission()) as transaction:
        transaction.begin()
        assert transaction.status()["phase"] == "copying"
    assert (root.parent, False) in calls and (root, True) in calls


def test_first_service_status_and_initializer_keep_the_same_private_root_boundary(
    boundary, monkeypatch
):
    root, _ = boundary
    monkeypatch.setattr(service_setup, "_layout", lambda: service_setup._Layout(root, root))

    def call(request):
        output = io.StringIO()
        code = service_setup.main(
            [], input_stream=io.BytesIO(json.dumps(request).encode()), output_stream=output
        )
        return code, json.loads(output.getvalue())

    assert call({"schema_version": 1, "operation": "status"}) == (
        0,
        {"schema_version": 1, "status": "new", "reason": "account_required"},
    )
    code, value = call(
        {
            "schema_version": 1,
            "operation": "initialize",
            "username": "new-service-admin",
            "password": "new-service-password",
            "confirmation": "new-service-password",
        }
    )
    assert code == 0 and value["status"] == "ready"
    assert identity_state.load_identity_state(root / "identity").site_id.startswith("install-")


def test_empty_target_recovery_keeps_programdata_outside_private_boundary(boundary):
    root, calls = boundary
    binding = target.TargetBinding(
        "install-" + uuid4().hex, str(uuid4()), "a" * 64, "S-1-5-21-1-2-3-1001"
    )

    class Admission:
        def assert_authenticated(self, candidate):
            assert candidate == binding

    assert target.recover_pending(root, binding, Admission()) == {
        "status": "none",
        "recovered_transfers": 0,
    }
    assert (root.parent, False) in calls and (root, True) in calls


@pytest.mark.parametrize(
    "violation",
    ["private_root_foreign_read", "public_parent_namespace_write", "foreign_parent_owner"],
)
def test_service_root_fix_never_admits_foreign_root_or_mutable_public_ancestor(
    boundary, monkeypatch, violation
):
    root, _ = boundary
    original = identity_state._windows_admission

    def deny(path, *, private, directory=False):
        if violation == "private_root_foreign_read" and Path(path) == root:
            _check_policy(
                "S-1-5-32-544",
                (AccessEntry(0, 3, 0x1F01FF, SERVICE), AccessEntry(0, 0, 1, "S-1-5-32-545")),
                SERVICE,
                private=private,
                require_file_inheritance=directory,
                trusted_installer_verified=False,
            )
        elif Path(path) == root.parent and violation != "private_root_foreign_read":
            _check_policy(
                "S-1-5-21-99-99-99-99" if violation == "foreign_parent_owner" else "S-1-5-18",
                (AccessEntry(0, 0, 0x40, "S-1-5-32-545"),),
                SERVICE,
                private=private,
                require_file_inheritance=False,
                trusted_installer_verified=False,
                recorder_program_data=True,
            )
        else:
            original(path, private=private, directory=directory)

    monkeypatch.setattr(identity_state, "_windows_admission", deny)
    with pytest.raises(target.TargetMigrationError):
        target.inspect_target(root)
    assert list(root.iterdir()) == []


def test_service_root_reparse_or_link_is_refused_without_following(boundary):
    root, _ = boundary
    target_root = root.with_name("foreign-root")
    target_root.mkdir(mode=0o700)
    root.rmdir()
    root.symlink_to(target_root, target_is_directory=True)
    with pytest.raises(target.TargetMigrationError):
        target.inspect_target(root)
    assert list(target_root.iterdir()) == []


def test_full_activation_publishes_into_private_service_root_below_public_container(
    prepared, monkeypatch
):
    calls = _model_public_parent(monkeypatch, prepared.root.parent)
    prepared.session.prepare_activation()
    assert not (prepared.root / "identity").exists()
    assert prepared.session.commit()["activated"] is True
    assert (
        identity_state.load_identity_state(prepared.root / "identity").site_id
        == prepared.binding.site_id
    )
    assert (prepared.root.parent, True) not in calls
    assert (prepared.root, True) in calls


def test_failed_identity_publication_restores_recordings_with_same_private_root_boundary(
    prepared, monkeypatch
):
    calls = _model_public_parent(monkeypatch, prepared.root.parent)
    original = target._rename_new

    def refuse_identity(source, destination):
        if destination == prepared.root / "identity":
            raise OSError("injected identity publication failure")
        return original(source, destination)

    prepared.session.prepare_activation()
    monkeypatch.setattr(target, "_rename_new", refuse_identity)
    with pytest.raises(target.TargetMigrationError):
        prepared.session.commit()
    assert prepared.session.status()["phase"] == "rolled_back"
    assert not (prepared.root / "identity").exists()
    assert not tuple((prepared.root / "recordings").iterdir())
    assert tuple((prepared.session.stage / "recordings").rglob("*.mp4"))
    assert (prepared.root.parent, True) not in calls
