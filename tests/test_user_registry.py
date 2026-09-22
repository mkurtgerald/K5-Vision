from uuid import uuid4

import pytest

from k5vision.domain.users import UserCreate, UserPatch, UserRole
from k5vision.services.user_registry import (
    UserRegistry,
    UserRegistryCapacityError,
    UserRegistryConflictError,
)


def _user(username: str = "operator.one") -> UserCreate:
    return UserCreate(
        username=username,
        display_name="Operator One",
        role=UserRole.OPERATOR,
        enabled=True,
    )


def test_user_registry_persists_site_scope_and_audit(tmp_path) -> None:
    database = tmp_path / "users.sqlite3"
    registry = UserRegistry(database_path=database, site_id="site-a")
    created = registry.create(_user(), actor="control-plane-write")
    registry.close()

    reopened = UserRegistry(database_path=database, site_id="site-a")
    listed = reopened.list()
    audit = reopened.audit_events()
    reopened.close()

    other_site = UserRegistry(database_path=database, site_id="site-b")
    isolated = other_site.list()
    other = other_site.create(_user(), actor="control-plane-write")
    other_site.close()

    assert listed == [created]
    assert isolated == []
    assert other.id != created.id
    assert len(audit) == 1
    assert audit[0].user_id == created.id
    assert audit[0].action == "created"
    assert audit[0].actor == "control-plane-write"
    assert audit[0].changed_fields == ("username", "display_name", "role", "enabled")


def test_duplicate_username_is_case_insensitive_and_does_not_add_audit(tmp_path) -> None:
    registry = UserRegistry(database_path=tmp_path / "users.sqlite3", site_id="site-a")
    created = registry.create(_user("Operator.One"), actor="control-plane-write")

    with pytest.raises(UserRegistryConflictError):
        registry.create(_user("operator.one"), actor="control-plane-write")

    assert registry.list() == [created]
    assert len(registry.audit_events()) == 1
    registry.close()


def test_user_update_is_bounded_transactional_and_audited(tmp_path) -> None:
    database = tmp_path / "users.sqlite3"
    registry = UserRegistry(database_path=database, site_id="site-a")
    created = registry.create(_user(), actor="control-plane-write")

    updated = registry.update(
        created.id,
        UserPatch(display_name="Shift Supervisor", role=UserRole.ADMINISTRATOR, enabled=False),
        actor="control-plane-write",
    )
    unchanged = registry.update(
        created.id,
        UserPatch(enabled=False),
        actor="control-plane-write",
    )
    missing = registry.update(
        uuid4(),
        UserPatch(enabled=False),
        actor="control-plane-write",
    )
    audit = registry.audit_events()
    registry.close()

    assert updated is not None
    assert updated.display_name == "Shift Supervisor"
    assert updated.role is UserRole.ADMINISTRATOR
    assert updated.enabled is False
    assert unchanged == updated
    assert missing is None
    assert [event.action for event in audit] == ["created", "updated"]
    assert audit[1].changed_fields == ("display_name", "role", "enabled")

    reopened = UserRegistry(database_path=database, site_id="site-a")
    assert reopened.list() == [updated]
    reopened.close()


def test_capacity_rejection_leaves_existing_state_and_audit_unchanged(tmp_path) -> None:
    registry = UserRegistry(
        database_path=tmp_path / "users.sqlite3",
        site_id="site-a",
        capacity=1,
    )
    created = registry.create(_user("first.user"), actor="control-plane-write")

    with pytest.raises(UserRegistryCapacityError):
        registry.create(_user("second.user"), actor="control-plane-write")

    assert registry.list() == [created]
    assert len(registry.audit_events()) == 1
    registry.close()


def test_bootstrap_credential_is_one_time_persisted_only_as_verifier(tmp_path) -> None:
    database = tmp_path / "users.sqlite3"
    registry = UserRegistry(database_path=database, site_id="site-a")
    account, temporary_credential, expires_at = registry.create_with_bootstrap(
        _user(),
        actor="control-plane-admin",
    )
    audit = registry.audit_events()
    registry.close()

    assert len(temporary_credential) >= 32
    assert expires_at.tzinfo is not None
    assert temporary_credential.encode("utf-8") not in database.read_bytes()
    assert [event.action for event in audit] == ["created", "bootstrap-issued"]
    assert all(temporary_credential not in event.model_dump_json() for event in audit)

    reopened = UserRegistry(database_path=database, site_id="site-a")
    assert (
        reopened.verify_password(username=account.username, password=temporary_credential) is None
    )
    activated = reopened.activate_bootstrap_password(
        username=account.username,
        temporary_credential=temporary_credential,
        new_password="correct horse battery staple",
        actor="bootstrap-user",
    )
    replay = reopened.activate_bootstrap_password(
        username=account.username,
        temporary_credential=temporary_credential,
        new_password="another strong password",
        actor="bootstrap-user",
    )
    verified = reopened.verify_password(
        username=account.username,
        password="correct horse battery staple",
    )
    audit_after = reopened.audit_events()
    reopened.close()

    assert activated == account
    assert replay is None
    assert verified == account
    assert [event.action for event in audit_after] == [
        "created",
        "bootstrap-issued",
        "bootstrap-consumed",
    ]
    assert temporary_credential.encode("utf-8") not in database.read_bytes()


def test_wrong_bootstrap_or_disabled_account_does_not_mutate_credential_state(tmp_path) -> None:
    database = tmp_path / "users.sqlite3"
    registry = UserRegistry(database_path=database, site_id="site-a")
    account, temporary_credential, _ = registry.create_with_bootstrap(
        _user(),
        actor="control-plane-admin",
    )

    wrong = registry.activate_bootstrap_password(
        username=account.username,
        temporary_credential="x" * len(temporary_credential),
        new_password="correct horse battery staple",
        actor="bootstrap-user",
    )
    disabled = registry.update(
        account.id,
        UserPatch(enabled=False),
        actor="control-plane-admin",
    )
    blocked = registry.activate_bootstrap_password(
        username=account.username,
        temporary_credential=temporary_credential,
        new_password="correct horse battery staple",
        actor="bootstrap-user",
    )
    audit = registry.audit_events()
    registry.close()

    assert wrong is None
    assert disabled is not None and disabled.enabled is False
    assert blocked is None
    assert [event.action for event in audit] == ["created", "bootstrap-issued", "updated"]


def test_user_models_reject_unbounded_or_empty_administration_requests() -> None:
    with pytest.raises(ValueError):
        UserCreate(username="bad name", display_name="Bad")
    with pytest.raises(ValueError):
        UserCreate(username="valid.user", display_name="x" * 129)
    with pytest.raises(ValueError):
        UserPatch()
