from __future__ import annotations

import hashlib
import pickle
import sqlite3
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
import test_operator_time_admin as time_helpers

from k5vision import identity_state
from k5vision import legacy_migration_authorization as authorization
from k5vision.domain.users import UserCreate, UserPatch, UserRole
from k5vision.operator_identity_security import AccountSecurityStore, totp
from k5vision.operator_profile_lock import OperatorProfileLock
from k5vision.services.user_registry import UserRegistry

private_profile = time_helpers.private_profile


def _hashes(root):
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.iterdir()
        if path.is_file()
    }


@pytest.fixture
def legacy(private_profile):
    password = "legacy-authorization-password"
    state = identity_state.initialize_identity_state(
        private_profile / "identity", username="legacy-admin", password=password
    )
    users = UserRegistry(database_path=state.database_path, site_id=state.site_id)
    account = users.verify_password(username="legacy-admin", password=password)
    security = AccountSecurityStore(state.directory, state.site_id)
    security.close()
    users.close()
    lease = OperatorProfileLock(state.directory)
    lease.acquire()
    values = SimpleNamespace(
        state=state,
        source=state.directory,
        password=password,
        account=account,
        lease=lease,
        transfer_id=str(uuid4()),
        owners=[],
    )
    yield values
    for owner in values.owners:
        owner.close()
    lease.close()


def _owner(legacy, **changes):
    arguments = {"lease": legacy.lease, "transfer_id": legacy.transfer_id}
    arguments.update(changes)
    result = authorization.LegacyMigrationAuthorizer(legacy.source, **arguments)
    legacy.owners.append(result)
    return result


def _permit(legacy, owner=None, code=""):
    owner = _owner(legacy) if owner is None else owner
    return owner.authorize("legacy-admin", legacy.password, code)


def _check(permit, **changes):
    binding = {"site_id": permit.site_id, "transfer_id": permit.transfer_id}
    binding.update(changes)
    permit.assert_current(**binding)


def _enable_mfa(legacy):
    secret = b"01234567890123456789"
    counter = int(time.time() // 30)
    security = AccountSecurityStore(legacy.source, legacy.state.site_id)
    try:
        recovery = security.enable(legacy.account.id, secret, counter - 1)
    finally:
        security.close()
    return totp(secret, counter), recovery


def _second_user(legacy, *, role=UserRole.ADMINISTRATOR, active=True):
    users = UserRegistry(database_path=legacy.state.database_path, site_id=legacy.state.site_id)
    try:
        account, credential, _ = users.create_with_bootstrap(
            UserCreate(username="second-user", display_name="Second user", role=role),
            actor="migration-test",
        )
        if active:
            account = users.activate_bootstrap_password(
                username=account.username,
                temporary_credential=credential,
                new_password="second-user-password",
                actor="migration-test",
            )
        return account
    finally:
        users.close()


def test_existing_password_administrator_gets_transfer_bound_live_permit_without_source_writes(
    legacy,
):
    before = _hashes(legacy.source)
    permit = _permit(legacy)
    _check(permit)
    assert permit.administrator_id == legacy.account.id
    assert permit.site_id == legacy.state.site_id and permit.transfer_id == legacy.transfer_id
    assert _hashes(legacy.source) == before
    with pytest.raises(AttributeError):
        permit._owner.transfer_id = str(uuid4())
    with pytest.raises(TypeError):
        bool(permit)
    with pytest.raises(TypeError):
        pickle.dumps(permit)


@pytest.mark.parametrize("field", ["site_id", "transfer_id"])
def test_another_site_or_transfer_cannot_use_a_permit(legacy, field):
    permit = _permit(legacy)
    with pytest.raises(authorization.LegacyMigrationAuthorizationError):
        _check(permit, **{field: str(uuid4())})
    with pytest.raises(authorization.LegacyMigrationAuthorizationError):
        _check(permit)


@pytest.mark.parametrize("change", ["password", "role", "disabled"])
def test_live_authority_changes_revoke_a_previously_verified_permit(legacy, change):
    _second_user(legacy)
    permit = _permit(legacy)
    users = UserRegistry(database_path=legacy.state.database_path, site_id=legacy.state.site_id)
    try:
        if change == "password":
            users.reset_bootstrap(legacy.account.id, actor="migration-test")
        else:
            users.update(
                legacy.account.id,
                UserPatch(role=UserRole.VIEWER) if change == "role" else UserPatch(enabled=False),
                actor="migration-test",
            )
    finally:
        users.close()
    with pytest.raises(authorization.LegacyMigrationAuthorizationError):
        _check(permit)


@pytest.mark.parametrize("state", ["viewer", "bootstrap", "disabled"])
def test_nonadministrator_bootstrap_and_disabled_credentials_cannot_authorize(legacy, state):
    account = _second_user(
        legacy,
        role=UserRole.VIEWER if state == "viewer" else UserRole.ADMINISTRATOR,
        active=state != "bootstrap",
    )
    if state == "disabled":
        users = UserRegistry(database_path=legacy.state.database_path, site_id=legacy.state.site_id)
        try:
            users.update(account.id, UserPatch(enabled=False), actor="migration-test")
        finally:
            users.close()
    before = _hashes(legacy.source)
    owner = _owner(legacy)
    with pytest.raises(authorization.LegacyMigrationAuthorizationError):
        owner.authorize(account.username, "second-user-password")
    assert _hashes(legacy.source) == before


def test_enabled_mfa_requires_a_new_factor_and_consumes_its_replay_counter_before_snapshot(legacy):
    code, _ = _enable_mfa(legacy)
    owner = _owner(legacy)
    before = _hashes(legacy.source)
    for value in ("", "not-a-factor"):
        with pytest.raises(authorization.LegacyMigrationAuthorizationError):
            _permit(legacy, owner, value)
    assert _hashes(legacy.source) == before
    permit = _permit(legacy, owner, code)
    _check(permit)
    assert (
        _hashes(legacy.source)["operator-account-security.sqlite3"]
        != before["operator-account-security.sqlite3"]
    )
    with pytest.raises(authorization.LegacyMigrationAuthorizationError):
        _permit(legacy, owner, code)
    with pytest.raises(authorization.LegacyMigrationAuthorizationError):
        _check(permit)


def test_single_use_recovery_factor_can_authorize_and_cannot_be_replayed(legacy):
    _, recovery = _enable_mfa(legacy)
    owner = _owner(legacy)
    permit = _permit(legacy, owner, recovery[0])
    _check(permit)
    with pytest.raises(authorization.LegacyMigrationAuthorizationError):
        _permit(legacy, owner, recovery[0])


def test_account_without_local_password_cannot_use_federation_metadata_as_authorization(legacy):
    users = UserRegistry(database_path=legacy.state.database_path, site_id=legacy.state.site_id)
    try:
        account = users.create(
            UserCreate(
                username="federated-user",
                display_name="Federated user",
                role=UserRole.ADMINISTRATOR,
            ),
            actor="migration-test",
        )
    finally:
        users.close()
    before = _hashes(legacy.source)
    with pytest.raises(authorization.LegacyMigrationAuthorizationError):
        # Complete-profile admission may already reject the missing local
        # credential. Federation metadata must not provide a fallback either.
        owner = _owner(legacy)
        owner.authorize(account.username, "unverified-provider-assertion")
    assert _hashes(legacy.source) == before


def test_boolean_second_factor_cannot_approve_a_migration(legacy):
    _enable_mfa(legacy)
    owner = _owner(legacy)
    before = _hashes(legacy.source)
    with pytest.raises(authorization.LegacyMigrationAuthorizationError):
        owner.authorize("legacy-admin", legacy.password, True)
    assert _hashes(legacy.source) == before


def test_copying_permit_fields_cannot_create_an_issued_authorization(legacy):
    permit = _permit(legacy)
    forged = authorization.LegacyMigrationPermit(permit._owner, permit._proof)
    with pytest.raises(authorization.LegacyMigrationAuthorizationError):
        _check(forged)


@pytest.mark.parametrize("change", ["enabled", "disabled", "replaced"])
def test_factor_generation_and_presence_changes_revoke_existing_authorization(legacy, change):
    code = ""
    if change != "enabled":
        code, _ = _enable_mfa(legacy)
    permit = _permit(legacy, code=code)
    security = AccountSecurityStore(legacy.source, legacy.state.site_id)
    try:
        if change != "enabled":
            security.disable(legacy.account.id)
        if change != "disabled":
            security.enable(legacy.account.id, b"replacement-secret!!", int(time.time() // 30))
    finally:
        security.close()
    with pytest.raises(authorization.LegacyMigrationAuthorizationError):
        _check(permit)


@pytest.mark.parametrize("change", ["permit-close", "owner-close", "lease-close", "manifest"])
def test_closed_or_changed_source_ownership_cannot_keep_a_permit_live(legacy, change):
    owner = _owner(legacy)
    permit = _permit(legacy, owner)
    if change == "permit-close":
        permit.close()
    elif change == "owner-close":
        owner.close()
    elif change == "lease-close":
        legacy.lease.close()
    else:
        (legacy.source / "identity.json").write_text('{"schema_version":1,"site_id":"changed"}')
    with pytest.raises(authorization.LegacyMigrationAuthorizationError):
        _check(permit)


def test_elapsed_expiration_and_attempt_budget_do_not_follow_wall_clock(legacy, monkeypatch):
    clock = SimpleNamespace(elapsed=100.0)
    monkeypatch.setattr(authorization, "time", SimpleNamespace(monotonic=lambda: clock.elapsed))
    owner = _owner(legacy, permit_seconds=10)
    for _ in range(9):
        with pytest.raises(authorization.LegacyMigrationAuthorizationError):
            owner.authorize("legacy-admin", "incorrect-private-password")
    permit = _permit(legacy, owner)
    _check(permit)
    with pytest.raises(authorization.LegacyMigrationAuthorizationError):
        _permit(legacy, owner)
    clock.elapsed = 110.0
    with pytest.raises(authorization.LegacyMigrationAuthorizationError):
        _check(permit)
    clock.elapsed = 401.0
    renewed = _permit(legacy, owner)
    _check(renewed)


@pytest.mark.parametrize("clock_value", [float("nan"), -1.0, 99.0])
def test_unavailable_or_backward_elapsed_clock_revokes_authorization(
    legacy, monkeypatch, clock_value
):
    clock = SimpleNamespace(elapsed=100.0)
    monkeypatch.setattr(authorization, "time", SimpleNamespace(monotonic=lambda: clock.elapsed))
    permit = _permit(legacy)
    clock.elapsed = clock_value
    with pytest.raises(authorization.LegacyMigrationAuthorizationError):
        _check(permit)


@pytest.mark.parametrize(
    "condition", ["security-missing", "anchor-missing", "schema-incomplete", "closed-wal"]
)
def test_incomplete_authority_refuses_without_repair_or_initialization(legacy, condition):
    if condition == "security-missing":
        (legacy.source / "operator-account-security.sqlite3").unlink()
    else:
        connection = sqlite3.connect(legacy.state.database_path)
        if condition == "anchor-missing":
            connection.execute(
                "DELETE FROM operator_component_anchors WHERE component='account_security'"
            )
        elif condition == "schema-incomplete":
            connection.execute("ALTER TABLE user_credentials DROP COLUMN issued_at")
        else:
            connection.execute("PRAGMA journal_mode=WAL")
        connection.commit()
        connection.close()
    before = _hashes(legacy.source)
    with pytest.raises(authorization.LegacyMigrationAuthorizationError):
        _owner(legacy)
    assert _hashes(legacy.source) == before


@pytest.mark.parametrize("seconds", [0, True, 86401])
def test_permit_lifetime_must_have_a_finite_bounded_policy(legacy, seconds):
    with pytest.raises(authorization.LegacyMigrationAuthorizationError):
        _owner(legacy, permit_seconds=seconds)


def test_failure_and_permit_repr_do_not_export_private_credentials_or_native_context(legacy):
    owner = _owner(legacy)
    password = "distinct-private-password"
    with pytest.raises(authorization.LegacyMigrationAuthorizationError) as caught:
        owner.authorize("legacy-admin", password)
    assert password not in repr(caught.value) and str(legacy.source) not in repr(caught.value)
    assert caught.value.__context__ is None and caught.value.__cause__ is None
    permit = _permit(legacy, owner)
    assert legacy.password not in repr(permit) and str(legacy.source) not in repr(permit)


@pytest.mark.parametrize("change", ["expired", "password-revoked"])
def test_authority_io_cannot_return_an_expired_or_newly_revoked_permit(legacy, monkeypatch, change):
    clock = SimpleNamespace(elapsed=100.0)
    monkeypatch.setattr(authorization, "time", SimpleNamespace(monotonic=lambda: clock.elapsed))
    owner = _owner(legacy, permit_seconds=1)
    permit = _permit(legacy, owner)
    status = owner._security.status

    def changed_during_factor_read(user_id):
        if change == "expired":
            clock.elapsed = 102.0
        else:
            users = UserRegistry(
                database_path=legacy.state.database_path, site_id=legacy.state.site_id
            )
            try:
                users.reset_bootstrap(legacy.account.id, actor="migration-test")
            finally:
                users.close()
        return status(user_id)

    monkeypatch.setattr(owner._security, "status", changed_during_factor_read)
    with pytest.raises(authorization.LegacyMigrationAuthorizationError):
        _check(permit)


@pytest.mark.parametrize("database", ["users.sqlite3", "operator-account-security.sqlite3"])
def test_readonly_preflight_refuses_closed_wal_before_the_controller_acquires_its_lease(
    legacy, database
):
    legacy.lease.close()
    connection = sqlite3.connect(legacy.source / database)
    assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    connection.close()
    before = _hashes(legacy.source)
    with pytest.raises(authorization.LegacyMigrationAuthorizationError):
        authorization.LegacyMigrationAuthorizer.preflight(legacy.source)
    assert _hashes(legacy.source) == before
