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


def test_user_models_reject_unbounded_or_empty_administration_requests() -> None:
    with pytest.raises(ValueError):
        UserCreate(username="bad name", display_name="Bad")
    with pytest.raises(ValueError):
        UserCreate(username="valid.user", display_name="x" * 129)
    with pytest.raises(ValueError):
        UserPatch()
