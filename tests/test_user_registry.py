import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest

import k5vision.services.user_registry as registry_module
from k5vision.auth_sessions import UserSessionManager
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


def test_camera_grants_are_atomic_private_site_scoped_and_persistent(tmp_path):
    path = tmp_path / "users.sqlite3"
    registry = UserRegistry(database_path=path, site_id="site-a")
    one, two = uuid4(), uuid4()
    user, temporary, _ = registry.create_with_bootstrap(
        _user(), actor="admin", camera_access={"mode": "selected", "camera_ids": [one, two]}
    )
    assert registry.get_camera_access(user.id) == {
        "mode": "selected",
        "camera_ids": sorted([one, two], key=str),
    }
    assert registry.credential_status(user.id) == "pending"
    before = registry.audit_events()
    registry.set_camera_access(user.id, mode="selected", camera_ids=[two], actor="admin")
    assert registry.audit_events()[-1].changed_fields == ("camera_access",)
    count = len(registry.audit_events())
    registry.set_camera_access(user.id, mode="selected", camera_ids=[two], actor="admin")
    assert len(registry.audit_events()) == count
    assert temporary not in str(before)
    registry.close()
    reopened = UserRegistry(database_path=path, site_id="site-a")
    assert reopened.get_camera_access(user.id) == {"mode": "selected", "camera_ids": [two]}
    other = UserRegistry(database_path=path, site_id="site-b")
    assert other.get_camera_access(user.id) is None
    assert other.set_camera_access(user.id, mode="all", camera_ids=[], actor="admin") is None
    other.close()
    reopened.close()


def test_invalid_initial_grants_roll_back_user_credential_and_audit(tmp_path):
    registry = UserRegistry(database_path=tmp_path / "users.sqlite3")
    request = _user().model_copy(update={"role": UserRole.ADMINISTRATOR})
    with pytest.raises(ValueError):
        registry.create_with_bootstrap(
            request, actor="admin", camera_access={"mode": "selected", "camera_ids": []}
        )
    assert registry.list() == []
    assert registry.audit_events() == []
    assert registry._connection.execute("SELECT COUNT(*) FROM user_credentials").fetchone()[0] == 0
    registry.close()


def test_invalid_combined_update_preserves_role_grants_and_audit(tmp_path):
    registry = UserRegistry(database_path=tmp_path / "users.sqlite3")
    user, _, _ = registry.create_with_bootstrap(
        _user(), actor="admin", camera_access={"mode": "selected", "camera_ids": []}
    )
    audit = registry.audit_events()
    with pytest.raises(ValueError):
        registry.update(
            user.id,
            UserPatch(role=UserRole.ADMINISTRATOR),
            actor="admin",
            camera_access={"mode": "selected", "camera_ids": []},
        )
    assert registry.get(user.id) == user
    assert registry.get_camera_access(user.id) == {"mode": "selected", "camera_ids": []}
    assert registry.audit_events() == audit
    registry.update(user.id, UserPatch(role=UserRole.ADMINISTRATOR), actor="admin")
    assert registry.get_camera_access(user.id) == {"mode": "all", "camera_ids": []}
    registry.close()


@pytest.mark.parametrize(
    "selection",
    [
        {"mode": "selected", "camera_ids": ["invalid"]},
        {"mode": "all", "camera_ids": [uuid4()]},
        {"mode": "selected", "camera_ids": [uuid4() for _ in range(65)]},
        {"mode": "invalid", "camera_ids": []},
    ],
)
def test_camera_grants_reject_invalid_or_unbounded_selection(tmp_path, selection):
    registry = UserRegistry(database_path=tmp_path / "users.sqlite3")
    user = registry.create(_user(), actor="admin")
    before = registry.audit_events()
    with pytest.raises(ValueError):
        registry.set_camera_access(user.id, actor="admin", **selection)
    assert registry.get_camera_access(user.id) == {"mode": "all", "camera_ids": []}
    assert registry.audit_events() == before
    registry.close()


def test_reset_replaces_password_and_old_temporary_credentials_without_disclosure(tmp_path):
    path = tmp_path / "users.sqlite3"
    registry = UserRegistry(database_path=path)
    user, first, _ = registry.create_with_bootstrap(_user(), actor="admin")
    registry.activate_bootstrap_password(
        username=user.username,
        temporary_credential=first,
        new_password="first initialized password",
        actor="bootstrap-user",
    )
    assert registry.credential_status(user.id) == "active"
    same, second, _ = registry.reset_bootstrap(user.id, actor="admin")
    assert same == user and first != second
    assert registry.credential_status(user.id) == "pending"
    assert (
        registry.verify_password(username=user.username, password="first initialized password")
        is None
    )
    assert (
        registry.activate_bootstrap_password(
            username=user.username,
            temporary_credential=first,
            new_password="replacement initialized password",
            actor="bootstrap-user",
        )
        is None
    )
    assert (
        registry.activate_bootstrap_password(
            username=user.username,
            temporary_credential=second,
            new_password="replacement initialized password",
            actor="bootstrap-user",
        )
        == user
    )
    assert registry.credential_status(user.id) == "active"
    assert (
        registry.verify_password(
            username=user.username, password="replacement initialized password"
        )
        == user
    )
    assert registry.audit_events()[-2].action == "password-reset"
    assert first.encode() not in path.read_bytes() and second.encode() not in path.read_bytes()
    registry.close()


def test_last_initialized_administrator_cannot_be_reset(tmp_path):
    registry = UserRegistry(database_path=tmp_path / "users.sqlite3")
    admin = _initialized_administrator(registry, "primary.admin")
    audit = registry.audit_events()
    with pytest.raises(UserRegistryLastAdministratorError):
        registry.reset_bootstrap(admin.id, actor="primary.admin")
    assert registry.audit_events() == audit
    assert (
        registry.verify_password(
            username=admin.username, password="local administrator test password"
        )
        == admin
    )
    registry.close()


def test_concurrent_admin_resets_leave_one_initialized_administrator(tmp_path):
    path = tmp_path / "users.sqlite3"
    registry = UserRegistry(database_path=path)
    one = _initialized_administrator(registry, "primary.admin")
    two = _initialized_administrator(registry, "other.admin")
    registry.close()
    barrier = Barrier(2)

    def reset(user):
        current = UserRegistry(database_path=path)
        try:
            barrier.wait()
            current.reset_bootstrap(user.id, actor="admin")
            return True
        except UserRegistryLastAdministratorError:
            return False
        finally:
            current.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(reset, [one, two]))
    assert sorted(results) == [False, True]
    reopened = UserRegistry(database_path=path)
    assert sorted([reopened.credential_status(one.id), reopened.credential_status(two.id)]) == [
        "active",
        "pending",
    ]
    reopened.close()


def test_verified_old_password_snapshot_cannot_issue_session_after_reset_and_activation(tmp_path):
    registry = UserRegistry(database_path=tmp_path / "users.sqlite3")
    user, temporary, _ = registry.create_with_bootstrap(_user(), actor="admin")
    registry.activate_bootstrap_password(
        username=user.username,
        temporary_credential=temporary,
        new_password="first initialized password",
        actor="bootstrap-user",
    )
    old_account, old_version = registry.verify_password_snapshot(
        username=user.username, password="first initialized password"
    )
    _, reset, _ = registry.reset_bootstrap(user.id, actor="admin")
    registry.activate_bootstrap_password(
        username=user.username,
        temporary_credential=reset,
        new_password="new initialized password",
        actor="bootstrap-user",
    )

    async def run():
        sessions = UserSessionManager(resolve_account=registry.resolve_session)
        stale, _ = await sessions.issue(old_account, credential_version=old_version)
        assert await sessions.resolve(stale) is None
        current, version = registry.verify_password_snapshot(
            username=user.username, password="new initialized password"
        )
        valid, _ = await sessions.issue(current, credential_version=version)
        assert await sessions.resolve(valid) == user
        registry.update(user.id, UserPatch(enabled=False), actor="admin")
        assert await sessions.resolve(valid) is None

    asyncio.run(run())
    registry.close()


@pytest.mark.parametrize("expiry_method", ["utc", "elapsed", "restart-rollback"])
def test_expired_bootstrap_never_revives_after_clock_rollback_or_restart(
    tmp_path, monkeypatch, expiry_method
):
    state = {"utc": datetime(2035, 1, 1, tzinfo=UTC), "tick": 100.0}

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return state["utc"]

    monkeypatch.setattr(registry_module, "datetime", Clock)
    monkeypatch.setattr(registry_module, "monotonic", lambda: state["tick"])
    path = tmp_path / "users.sqlite3"
    registry = UserRegistry(database_path=path)
    user, temporary, _ = registry.create_with_bootstrap(_user(), actor="admin", ttl_seconds=60)
    if expiry_method == "utc":
        state["utc"] += timedelta(seconds=120)
        registry.close()
        registry = UserRegistry(database_path=path)
    elif expiry_method == "elapsed":
        state["tick"] += 61
    else:
        state["utc"] += timedelta(seconds=30)
        assert registry.credential_status(user.id) == "pending"
        registry.close()
        registry = UserRegistry(database_path=path)
        state["utc"] -= timedelta(seconds=20)
    assert registry.credential_status(user.id) == "expired"
    state["utc"] -= timedelta(days=2)
    registry.close()
    reopened = UserRegistry(database_path=path)
    assert reopened.credential_status(user.id) == "expired"
    assert (
        reopened.activate_bootstrap_password(
            username=user.username,
            temporary_credential=temporary,
            new_password="a replacement password",
            actor="bootstrap-user",
        )
        is None
    )
    reopened.close()


def test_latest_audit_page_uses_event_identity_not_adjustable_wall_time(tmp_path):
    registry = UserRegistry(database_path=tmp_path / "users.sqlite3")
    user = registry.create(_user(), actor="admin")
    registry.update(user.id, UserPatch(display_name="Renamed user"), actor="admin")
    assert [event.action for event in registry.audit_events(newest_first=True, limit=1)] == [
        "updated"
    ]
    registry.close()


def test_valid_bootstrap_survives_small_ntp_backstep_with_trusted_elapsed_budget(
    tmp_path, monkeypatch
):
    state = {"utc": datetime(2035, 1, 1, tzinfo=UTC), "tick": 100.0}

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return state["utc"]

    monkeypatch.setattr(registry_module, "datetime", Clock)
    monkeypatch.setattr(registry_module, "monotonic", lambda: state["tick"])
    registry = UserRegistry(database_path=tmp_path / "users.sqlite3")
    user, temporary, _ = registry.create_with_bootstrap(_user(), actor="admin", ttl_seconds=60)
    state["utc"] += timedelta(seconds=10)
    state["tick"] += 10
    assert registry.credential_status(user.id) == "pending"
    state["utc"] -= timedelta(seconds=5)
    state["tick"] += 1
    assert (
        registry.activate_bootstrap_password(
            username=user.username,
            temporary_credential=temporary,
            new_password="replacement user password",
            actor="bootstrap-user",
        )
        == user
    )
    registry.close()


@pytest.mark.parametrize("offset_seconds", [172800, -172800])
def test_valid_bootstrap_uses_elapsed_lifetime_through_realistic_clock_corrections(
    tmp_path, monkeypatch, offset_seconds
):
    state = {"utc": datetime(2035, 1, 1, tzinfo=UTC), "tick": 100.0}

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return state["utc"]

    monkeypatch.setattr(registry_module, "datetime", Clock)
    monkeypatch.setattr(registry_module, "monotonic", lambda: state["tick"])
    registry = UserRegistry(database_path=tmp_path / "users.sqlite3")
    user, temporary, _ = registry.create_with_bootstrap(_user(), actor="admin", ttl_seconds=60)
    state["utc"] += timedelta(seconds=offset_seconds)
    state["tick"] += 15
    assert registry.credential_status(user.id) == "pending"
    assert (
        registry.activate_bootstrap_password(
            username=user.username,
            temporary_credential=temporary,
            new_password="replacement user password",
            actor="bootstrap-user",
        )
        == user
    )
    registry.close()


def test_elapsed_bootstrap_expiry_is_permanent_through_clock_corrections_and_restart(
    tmp_path, monkeypatch
):
    state = {"utc": datetime(2035, 1, 1, tzinfo=UTC), "tick": 100.0}

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return state["utc"]

    monkeypatch.setattr(registry_module, "datetime", Clock)
    monkeypatch.setattr(registry_module, "monotonic", lambda: state["tick"])
    path = tmp_path / "users.sqlite3"
    registry = UserRegistry(database_path=path)
    user, temporary, _ = registry.create_with_bootstrap(_user(), actor="admin", ttl_seconds=60)
    state["utc"] += timedelta(days=2)
    state["tick"] = 120
    assert registry.credential_status(user.id) == "pending"
    state["utc"] -= timedelta(days=4)
    state["tick"] = 159
    assert registry.credential_status(user.id) == "pending"
    state["tick"] = 160
    assert registry.credential_status(user.id) == "expired"
    registry.close()
    reopened = UserRegistry(database_path=path)
    assert reopened.credential_status(user.id) == "expired"
    assert (
        reopened.activate_bootstrap_password(
            username=user.username,
            temporary_credential=temporary,
            new_password="replacement user password",
            actor="bootstrap-user",
        )
        is None
    )
    reopened.close()


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
