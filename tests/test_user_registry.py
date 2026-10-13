from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest

from k5vision.domain.users import UserCreate, UserPatch, UserRole
from k5vision.services.user_registry import (
    UserRegistry,
    UserRegistryCapacityError,
    UserRegistryConflictError,
    UserRegistryLastAdministratorError,
)


def _user(username: str = "operator.one") -> UserCreate:
    return UserCreate(
        username=username,
        display_name="Operator One",
        role=UserRole.OPERATOR,
        enabled=True,
    )


def _initialized_administrator(registry: UserRegistry, username: str):
    request = _user(username).model_copy(update={"role": UserRole.ADMINISTRATOR})
    account, credential, _ = registry.create_with_bootstrap(request, actor="setup-admin")
    assert (
        registry.activate_bootstrap_password(
            username=username,
            temporary_credential=credential,
            new_password="local administrator test password",
            actor="bootstrap-user",
        )
        == account
    )
    return account


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


@pytest.mark.parametrize("durable", [False, True])
@pytest.mark.parametrize(
    "patch",
    [
        UserPatch(enabled=False),
        UserPatch(role=UserRole.OPERATOR),
        UserPatch(display_name="Changed name", enabled=False, role=UserRole.VIEWER),
    ],
)
def test_last_enabled_administrator_rejection_preserves_account_and_audit(
    tmp_path, durable, patch
) -> None:
    database = tmp_path / "users.sqlite3" if durable else ":memory:"
    registry = UserRegistry(database_path=database, site_id="site-a")
    try:
        administrator = _initialized_administrator(registry, "admin.one")
        disabled = _initialized_administrator(registry, "disabled.admin")
        registry.update(
            disabled.id,
            UserPatch(enabled=False),
            actor="current-admin",
        )
        accounts_before = registry.list()
        audit_before = registry.audit_events()
        with pytest.raises(
            UserRegistryLastAdministratorError,
            match="Activate another enabled administrator",
        ):
            registry.update(administrator.id, patch, actor="current-admin")
        assert registry.list() == accounts_before
        assert registry.audit_events() == audit_before

        # A rejected transaction releases its write lock and preserves ordinary
        # administrative updates that do not remove account authority.
        renamed = registry.update(
            administrator.id, UserPatch(display_name="Daily administrator"), actor="current-admin"
        )
        assert renamed is not None and renamed.enabled
        assert renamed.role is UserRole.ADMINISTRATOR
        assert registry.audit_events()[-1].changed_fields == ("display_name",)
    finally:
        registry.close()


def test_administrator_in_another_site_cannot_allow_last_admin_removal(tmp_path) -> None:
    database = tmp_path / "users.sqlite3"
    registry = UserRegistry(database_path=database, site_id="site-a")
    other = UserRegistry(database_path=database, site_id="site-b")
    try:
        administrator = _initialized_administrator(registry, "admin.one")
        _initialized_administrator(other, "admin.one")
        with pytest.raises(UserRegistryLastAdministratorError):
            registry.update(administrator.id, UserPatch(enabled=False), actor="current-admin")
        assert registry.get(administrator.id) == administrator
    finally:
        registry.close()
        other.close()


@pytest.mark.parametrize("durable", [False, True])
def test_concurrent_admin_removals_leave_one_enabled_admin(tmp_path, durable) -> None:
    database = tmp_path / "users.sqlite3" if durable else ":memory:"
    first = UserRegistry(database_path=database, site_id="site-a")
    second = UserRegistry(database_path=database, site_id="site-a") if durable else first
    try:
        administrators = [
            _initialized_administrator(first, username) for username in ("admin.one", "admin.two")
        ]
        barrier = Barrier(2)

        def remove_authority(registry, account, patch):
            barrier.wait(timeout=5)
            try:
                registry.update(account.id, patch, actor="current-admin")
            except UserRegistryLastAdministratorError:
                return "refused"
            return "updated"

        with ThreadPoolExecutor(max_workers=2) as executor:
            operations = [
                executor.submit(
                    remove_authority, first, administrators[0], UserPatch(enabled=False)
                ),
                executor.submit(
                    remove_authority, second, administrators[1], UserPatch(role=UserRole.OPERATOR)
                ),
            ]
            assert sorted(operation.result(timeout=10) for operation in operations) == [
                "refused",
                "updated",
            ]
        enabled_administrators = [
            account
            for account in first.list()
            if account.enabled and account.role is UserRole.ADMINISTRATOR
        ]
        assert len(enabled_administrators) == 1
        assert [event.action for event in first.audit_events()].count("updated") == 1
        if durable:
            reopened = UserRegistry(database_path=database, site_id="site-a")
            try:
                assert reopened.list() == first.list()
            finally:
                reopened.close()
    finally:
        first.close()
        if second is not first:
            second.close()


@pytest.mark.parametrize("durable", [False, True])
def test_pending_administrator_must_activate_before_last_admin_removal(tmp_path, durable) -> None:
    database = tmp_path / "users.sqlite3" if durable else ":memory:"
    registry = UserRegistry(database_path=database, site_id="site-a")
    try:
        first = _initialized_administrator(registry, "admin.one")
        pending, credential, _ = registry.create_with_bootstrap(
            _user("admin.two").model_copy(update={"role": UserRole.ADMINISTRATOR}),
            actor="current-admin",
        )
        audit_before = registry.audit_events()
        with pytest.raises(UserRegistryLastAdministratorError):
            registry.update(first.id, UserPatch(enabled=False), actor="current-admin")
        assert registry.get(first.id) == first
        assert registry.audit_events() == audit_before
        assert (
            registry.activate_bootstrap_password(
                username=pending.username,
                temporary_credential=credential,
                new_password="another administrator test password",
                actor="bootstrap-user",
            )
            == pending
        )
        disabled = registry.update(first.id, UserPatch(enabled=False), actor="current-admin")
        assert disabled is not None and not disabled.enabled
        assert (
            registry.verify_password(
                username=pending.username, password="another administrator test password"
            )
            == pending
        )
    finally:
        registry.close()
