"""Disposable recovery setup fixtures; no usable credential is checked in."""

from __future__ import annotations

import getpass
import json
import os
import secrets
import sqlite3
import warnings
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from k5vision import cli, recovery_admin
from k5vision.domain.users import UserCreate, UserPatch, UserRole
from k5vision.identity_state import (
    IdentityStateError,
    identity_environment,
    initialize_identity_state,
    load_identity_state,
)
from k5vision.main import create_app
from k5vision.services.user_registry import (
    UserRegistry,
    UserRegistryConflictError,
    UserRegistryStorageError,
)


@pytest.fixture
def password() -> str:
    return secrets.token_urlsafe(24)


def _request(username: str = "recovery-admin", **overrides) -> UserCreate:
    return UserCreate(
        username=username,
        display_name="Recovery administrator",
        **{"role": UserRole.ADMINISTRATOR, **overrides},
    )


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def clean_environment(monkeypatch):
    for name in (
        "K5_CONTROL_PLANE_SITE_ID",
        "K5_USER_DB_PATH",
        "K5_DEVICE_DB_PATH",
        "K5_CONTROL_PLANE_TOKEN",
        "K5_CONTROL_PLANE_ADMIN_TOKEN",
        "K5_CONTROL_PLANE_READ_TOKEN",
        "K5_LOCAL_TEST_RTSP_SOURCE",
        "K5_PUBLIC_TEST_RTSP_SOURCE",
    ):
        monkeypatch.delenv(name, raising=False)


def test_local_admin_uses_ordinary_auth_and_survives_restart(
    tmp_path,
    password,
    clean_environment,
):
    state = initialize_identity_state(
        tmp_path / "identity", username="recovery-admin", password=password
    )
    tokens = []
    for _ in range(2):
        assert load_identity_state(state.directory) == state
        with identity_environment(state, os.environ), TestClient(create_app()) as client:
            response = client.post(
                "/api/v1/auth/login", json={"username": "recovery-admin", "password": password}
            )
            assert response.status_code == 200
            token = response.json()["session_token"]
            assert response.json()["account"]["role"] == "administrator"
            assert client.get("/api/v1/users", headers=_headers(token)).status_code == 200
            audit = client.get("/api/v1/users/audit", headers=_headers(token))
            assert audit.status_code == 200
            assert [event["action"] for event in audit.json()] == [
                "created",
                "password-initialized",
            ]
            assert all(event["actor"] == "local-admin-setup" for event in audit.json())
            assert password not in audit.text and token not in audit.text
            if tokens:
                assert (
                    client.get("/api/v1/auth/me", headers=_headers(tokens[-1])).status_code == 401
                )
            tokens.append(token)
    assert "K5_CONTROL_PLANE_SITE_ID" not in os.environ
    assert "K5_USER_DB_PATH" not in os.environ
    assert password.encode() not in state.database_path.read_bytes()
    assert password not in (state.directory / "identity.json").read_text()


def test_new_setup_does_not_disable_existing_authorization(tmp_path, password, clean_environment):
    state = initialize_identity_state(
        tmp_path / "identity", username="recovery-admin", password=password
    )
    with identity_environment(state, os.environ), TestClient(create_app()) as client:
        assert client.get("/api/v1/users").status_code == 401
        wrong = client.post(
            "/api/v1/auth/login",
            json={"username": "recovery-admin", "password": secrets.token_urlsafe(24)},
        )
        assert wrong.status_code == 401
        assert client.post("/api/v1/auth/recovery").status_code == 404
        assert client.post("/api/v1/auth/bootstrap-admin").status_code == 404


def test_setup_is_atomic_and_refuses_all_existing_identity_scopes(tmp_path, password):
    path = tmp_path / "users.sqlite3"
    first = UserRegistry(database_path=path, site_id="first")
    second = UserRegistry(database_path=path, site_id="second")
    try:
        first.bootstrap_administrator(_request(), password=password)
        with pytest.raises(UserRegistryConflictError):
            second.bootstrap_administrator(
                _request("different-admin"), password=secrets.token_urlsafe(24)
            )
        assert len(first.list()) == 1 and second.list() == []
        assert first.verify_password(username="recovery-admin", password=password) is not None
    finally:
        first.close()
        second.close()


def test_audit_failure_rolls_back_user_and_password(tmp_path, password, monkeypatch):
    registry = UserRegistry(database_path=tmp_path / "users.sqlite3")
    original = registry._append_audit

    def fail_second(connection, **kwargs):
        if kwargs["action"] == "password-initialized":
            raise sqlite3.OperationalError("synthetic disk failure")
        return original(connection, **kwargs)

    try:
        monkeypatch.setattr(registry, "_append_audit", fail_second)
        with pytest.raises(UserRegistryStorageError):
            registry.bootstrap_administrator(_request(), password=password)
        assert registry.list() == [] and registry.audit_events() == []
        assert (
            registry._require_connection()
            .execute("SELECT COUNT(*) FROM user_credentials")
            .fetchone()[0]
            == 0
        )
        assert not registry._require_connection().in_transaction
    finally:
        registry.close()


def test_two_registry_connections_cannot_both_bootstrap(tmp_path, password):
    path = tmp_path / "users.sqlite3"
    registries = [UserRegistry(database_path=path) for _ in range(2)]

    def attempt(index):
        try:
            registries[index].bootstrap_administrator(_request(f"admin-{index}"), password=password)
            return "created"
        except UserRegistryConflictError:
            return "refused"

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(attempt, range(2))) == ["created", "refused"]
        assert len(registries[0].list()) == 1
        assert len(registries[0].audit_events()) == 2
    finally:
        for registry in registries:
            registry.close()


@pytest.mark.parametrize("override", [{"role": "operator"}, {"enabled": False}])
def test_first_setup_rejects_non_administrator(tmp_path, password, override):
    registry = UserRegistry(database_path=tmp_path / "users.sqlite3")
    try:
        with pytest.raises(ValueError):
            registry.bootstrap_administrator(_request(**override), password=password)
        assert registry.list() == []
    finally:
        registry.close()


def test_existing_or_partial_directory_is_never_reset(tmp_path, password):
    path = tmp_path / "identity"
    path.mkdir()
    sentinel = path / "foreign.txt"
    sentinel.write_text("preserve")
    with pytest.raises(IdentityStateError):
        initialize_identity_state(path, username="recovery-admin", password=password)
    assert list(path.iterdir()) == [sentinel] and sentinel.read_text() == "preserve"


def test_repeating_setup_preserves_original_credential(tmp_path, password):
    path = tmp_path / "identity"
    state = initialize_identity_state(path, username="recovery-admin", password=password)
    before = {p.name: p.read_bytes() for p in path.iterdir()}
    with pytest.raises(IdentityStateError):
        initialize_identity_state(
            path, username="recovery-admin", password=secrets.token_urlsafe(24)
        )
    assert {p.name: p.read_bytes() for p in path.iterdir()} == before
    assert load_identity_state(path) == state


@pytest.mark.parametrize(
    "replacement",
    [
        "not-json",
        "[]",
        '{"schema_version":true,"site_id":"x"}',
        '{"schema_version":1,"site_id":"../x"}',
        '{"schema_version":1,"site_id":"x","extra":1}',
        "x" * 1100,
    ],
)
def test_invalid_manifest_fails_closed(tmp_path, password, replacement):
    state = initialize_identity_state(
        tmp_path / "identity", username="recovery-admin", password=password
    )
    manifest = state.directory / "identity.json"
    manifest.write_text(replacement)
    before = state.database_path.read_bytes()
    with pytest.raises(IdentityStateError):
        load_identity_state(state.directory)
    assert state.database_path.read_bytes() == before
    assert manifest.read_text() == replacement


def test_manifest_duplicate_fields_are_not_accepted(tmp_path, password):
    state = initialize_identity_state(
        tmp_path / "identity", username="recovery-admin", password=password
    )
    (state.directory / "identity.json").write_text(
        f'{{"schema_version":1,"site_id":"{state.site_id}","site_id":"{state.site_id}"}}'
    )
    with pytest.raises(IdentityStateError):
        load_identity_state(state.directory)


def test_foreign_site_database_is_rejected(tmp_path, password):
    state = initialize_identity_state(
        tmp_path / "identity", username="recovery-admin", password=password
    )
    with sqlite3.connect(state.database_path) as connection:
        connection.execute("UPDATE users SET site_id = 'foreign'")
    with pytest.raises(IdentityStateError):
        load_identity_state(state.directory)


@pytest.mark.parametrize("target", ["identity.json", "users.sqlite3"])
def test_missing_identity_files_are_never_recreated(tmp_path, password, target):
    state = initialize_identity_state(
        tmp_path / "identity", username="recovery-admin", password=password
    )
    (state.directory / target).unlink()
    with pytest.raises(IdentityStateError):
        load_identity_state(state.directory)
    assert not (state.directory / target).exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX ownership/mode contract")
def test_shared_identity_directory_is_rejected(tmp_path, password):
    state = initialize_identity_state(
        tmp_path / "identity", username="recovery-admin", password=password
    )
    state.directory.chmod(0o755)
    with pytest.raises(IdentityStateError):
        load_identity_state(state.directory)


@pytest.mark.skipif(os.name != "posix", reason="POSIX link fixture")
@pytest.mark.parametrize("target", ["identity.json", "users.sqlite3", "users.sqlite3-wal"])
def test_symlinked_identity_files_are_rejected(tmp_path, password, target):
    state = initialize_identity_state(
        tmp_path / "identity", username="recovery-admin", password=password
    )
    original = state.directory / target
    foreign = tmp_path / "foreign"
    if original.exists():
        original.rename(foreign)
    else:
        foreign.write_text("preserve")
    original.symlink_to(foreign)
    before = foreign.read_bytes()
    with pytest.raises(IdentityStateError):
        load_identity_state(state.directory)
    assert foreign.read_bytes() == before


@pytest.mark.skipif(os.name != "posix", reason="POSIX link fixture")
def test_symlinked_parent_is_never_followed(tmp_path, password):
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    link = tmp_path / "link"
    link.symlink_to(foreign, target_is_directory=True)
    with pytest.raises(IdentityStateError):
        initialize_identity_state(link / "identity", username="recovery-admin", password=password)
    assert list(foreign.iterdir()) == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX link fixture")
def test_hardlinked_database_is_refused(tmp_path, password):
    state = initialize_identity_state(
        tmp_path / "identity", username="recovery-admin", password=password
    )
    os.link(state.database_path, tmp_path / "foreign-db")
    with pytest.raises(IdentityStateError):
        load_identity_state(state.directory)


@pytest.mark.parametrize(
    "name",
    [
        "K5_CONTROL_PLANE_SITE_ID",
        "K5_USER_DB_PATH",
        "K5_LOCAL_TEST_RTSP_SOURCE",
        "K5_PUBLIC_TEST_RTSP_SOURCE",
    ],
)
def test_conflicting_or_test_environment_is_preserved(tmp_path, password, name):
    state = initialize_identity_state(
        tmp_path / "identity", username="recovery-admin", password=password
    )
    environment = {name: "existing-value"}
    with pytest.raises(IdentityStateError), identity_environment(state, environment):
        pytest.fail("must not start")
    assert environment == {name: "existing-value"}


def test_environment_restored_after_server_failure(tmp_path, password):
    state = initialize_identity_state(
        tmp_path / "identity", username="recovery-admin", password=password
    )
    environment = {"K5_USER_DB_PATH": "", "unrelated": "preserved"}
    with pytest.raises(RuntimeError), identity_environment(state, environment):
        assert environment["K5_CONTROL_PLANE_SITE_ID"] == state.site_id
        raise RuntimeError("synthetic server failure")
    assert environment == {"K5_USER_DB_PATH": "", "unrelated": "preserved"}


def _interactive(monkeypatch, replies):
    monkeypatch.setattr(recovery_admin.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(recovery_admin.sys.stderr, "isatty", lambda: True)
    iterator = iter(replies)
    monkeypatch.setattr(recovery_admin.getpass, "getpass", lambda _prompt: next(iterator))


def test_interactive_setup_never_prints_password(tmp_path, password, monkeypatch, capsys):
    _interactive(monkeypatch, [password, password])
    assert cli.main(["setup-admin", "--identity-dir", str(tmp_path / "identity")]) == 0
    output = capsys.readouterr()
    assert password not in output.out + output.err
    assert load_identity_state(tmp_path / "identity")


def test_password_mismatch_writes_nothing(tmp_path, password, monkeypatch, capsys):
    second = secrets.token_urlsafe(24)
    _interactive(monkeypatch, [password, second])
    assert cli.main(["setup-admin", "--identity-dir", str(tmp_path / "identity")]) == 1
    assert not (tmp_path / "identity").exists()
    output = capsys.readouterr()
    assert password not in output.out + output.err and second not in output.out + output.err


def test_noninteractive_setup_fails_before_password_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr(recovery_admin.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(recovery_admin.getpass, "getpass", lambda _: pytest.fail("no prompt"))
    assert cli.main(["setup-admin", "--identity-dir", str(tmp_path / "identity")]) == 1
    assert not (tmp_path / "identity").exists()


def test_insecure_getpass_fallback_is_refused(tmp_path, monkeypatch):
    _interactive(monkeypatch, [])

    def insecure(_prompt):
        warnings.warn("synthetic unavailable terminal", getpass.GetPassWarning, stacklevel=2)
        pytest.fail("must not echo")

    monkeypatch.setattr(recovery_admin.getpass, "getpass", insecure)
    assert cli.main(["setup-admin", "--identity-dir", str(tmp_path / "identity")]) == 1
    assert not (tmp_path / "identity").exists()


def test_credential_arguments_are_rejected_without_echo(tmp_path, password, capsys):
    with pytest.raises(SystemExit) as caught:
        cli.main(
            ["setup-admin", "--identity-dir", str(tmp_path / "identity"), "--password", password]
        )
    assert caught.value.code == 2
    output = capsys.readouterr()
    assert password not in output.out + output.err
    assert not (tmp_path / "identity").exists()


def test_durable_serve_uses_same_state_and_fresh_app_factory(
    tmp_path, password, monkeypatch, clean_environment
):
    state = initialize_identity_state(
        tmp_path / "identity", username="recovery-admin", password=password
    )
    calls = []

    def run(application, **kwargs):
        calls.append((application, kwargs, dict(os.environ)))

    monkeypatch.setattr(cli.uvicorn, "run", run)
    assert cli.main(["serve", "--identity-dir", str(state.directory)]) == 0
    application, kwargs, environment = calls[0]
    assert application == "k5vision.main:create_app" and kwargs["factory"] is True
    assert environment["K5_CONTROL_PLANE_SITE_ID"] == state.site_id
    assert environment["K5_USER_DB_PATH"] == str(state.database_path)
    assert "K5_DEVICE_DB_PATH" not in environment
    assert "K5_CONTROL_PLANE_ADMIN_TOKEN" not in environment
    assert "K5_USER_DB_PATH" not in os.environ


def test_durable_serve_fails_closed_on_nonloopback(tmp_path, password, monkeypatch):
    state = initialize_identity_state(
        tmp_path / "identity", username="recovery-admin", password=password
    )
    monkeypatch.setattr(cli.uvicorn, "run", lambda *a, **k: pytest.fail("no start"))
    assert cli.main(["serve", "--identity-dir", str(state.directory), "--host", "0.0.0.0"]) == 1


def test_manifest_contains_only_typed_identity(tmp_path, password):
    state = initialize_identity_state(
        tmp_path / "identity", username="recovery-admin", password=password
    )
    manifest = json.loads((state.directory / "identity.json").read_text())
    assert manifest == {"schema_version": 1, "site_id": state.site_id}
    assert set(p.name for p in state.directory.iterdir()) == {"identity.json", "users.sqlite3"}


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM user_credentials",
        "DELETE FROM user_audit",
        "UPDATE users SET role = 'invalid-role'",
        "UPDATE users SET enabled = 2",
        "UPDATE user_credentials SET verifier = X'00'",
        "UPDATE user_credentials SET credential_kind = 'foreign'",
        "UPDATE user_credentials SET expires_at = 'unexpected'",
        "UPDATE user_credentials SET scheme = 'foreign'",
        "UPDATE user_audit SET actor = 'foreign' WHERE action = 'password-initialized'",
    ],
)
def test_damaged_registry_is_rejected_without_mutation(tmp_path, password, sql):
    state = initialize_identity_state(
        tmp_path / "identity", username="recovery-admin", password=password
    )
    with sqlite3.connect(state.database_path) as connection:
        connection.execute(sql)
    before = state.database_path.read_bytes()
    with pytest.raises(IdentityStateError):
        load_identity_state(state.directory)
    assert state.database_path.read_bytes() == before


def test_ordinary_account_lifecycle_and_pending_users_remain_valid(tmp_path, password):
    state = initialize_identity_state(
        tmp_path / "identity", username="recovery-admin", password=password
    )
    registry = UserRegistry(database_path=state.database_path, site_id=state.site_id)
    try:
        account = registry.list()[0]
        registry.update(
            account.id, UserPatch(role=UserRole.OPERATOR, enabled=False), actor="current-admin"
        )
        registry.create_with_bootstrap(_request("second-admin"), actor="current-admin")
    finally:
        registry.close()
    assert load_identity_state(state.directory) == state


@pytest.mark.skipif(os.name != "posix", reason="POSIX ancestor permissions")
def test_replaceable_ancestor_is_refused_before_setup(tmp_path, password):
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o777)
    shared.chmod(0o777)
    parent = shared / "private-parent"
    parent.mkdir(mode=0o700)
    with pytest.raises(IdentityStateError):
        initialize_identity_state(parent / "identity", username="recovery-admin", password=password)
    assert list(parent.iterdir()) == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX sticky parent semantics")
def test_trusted_sticky_ancestor_does_not_reject_private_child(tmp_path, password):
    shared = tmp_path / "sticky"
    shared.mkdir()
    shared.chmod(0o1777)
    parent = shared / "private-parent"
    parent.mkdir(mode=0o700)
    state = initialize_identity_state(
        parent / "identity", username="recovery-admin", password=password
    )
    assert load_identity_state(state.directory) == state


def test_failed_local_setup_leaves_no_reusable_completed_installation(
    tmp_path, password, monkeypatch
):
    def fail(*args, **kwargs):
        raise UserRegistryStorageError("synthetic failure")

    monkeypatch.setattr(UserRegistry, "bootstrap_administrator", fail)
    path = tmp_path / "identity"
    with pytest.raises(UserRegistryStorageError):
        initialize_identity_state(path, username="recovery-admin", password=password)
    with pytest.raises(IdentityStateError):
        load_identity_state(path)
    with pytest.raises(IdentityStateError):
        initialize_identity_state(path, username="recovery-admin", password=password)
