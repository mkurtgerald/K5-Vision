"""Real-store source/target flow; fake transport/copy is not native qualification."""

from __future__ import annotations

import hashlib
import shutil
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from test_legacy_identity_snapshot import legacy as crypto_legacy_fixture
from test_legacy_migration_authorization import legacy as legacy_fixture
from test_legacy_migration_authorization import private_profile as private_profile_fixture

from k5vision import identity_state, service_setup
from k5vision import legacy_migration_authorization as authorization
from k5vision import legacy_migration_service as service
from k5vision import legacy_migration_source as source
from k5vision import legacy_migration_target as target
from k5vision import operator_identity_security as security_module
from k5vision.legacy_migration_service import MigrationService
from k5vision.operator_identity_security import AccountSecurityStore, totp
from k5vision.operator_oidc import OIDCStore
from k5vision.services.user_registry import UserRegistry

private_profile = private_profile_fixture
legacy = legacy_fixture
crypto_legacy = crypto_legacy_fixture


class Channel:
    def __init__(self, legacy, root):
        self.legacy = legacy
        self.actor = MigrationService(self, root)
        self.requests = []
        self.result = None
        self.revoked = False
        self.hook = None
        self.code = ""

    def assert_current(self):
        if self.revoked:
            raise ValueError

    def receive(self):
        return {
            "schema_version": 1,
            "operation": "authorize",
            "username": "legacy-admin",
            "password": self.legacy.password,
            "code": self.code,
        }

    def exchange(self, request):
        self.assert_current()
        if request["operation"] == "progress":
            return {"schema_version": 1, "status": "ok"}
        assert request["operation"] == "service"
        nested = request["request"]
        self.requests.append(nested["operation"])
        if nested["operation"] == "begin":
            nested["binding"]["source_owner_sid"] = "S-1-5-21-1-2-3-1001"
        reply = self.actor.handle(nested)
        if self.hook is not None:
            self.hook(nested, reply)
        return reply

    def send(self, reply):
        self.result = reply


@pytest.fixture
def flow(legacy, private_profile, monkeypatch):
    legacy.lease.close()
    root = private_profile / "service-data"
    root.mkdir(mode=0o700)
    channel = Channel(legacy, root)

    def copy(path, entry, transfer_id, *, checkpoint):
        # Native production helper has independent fixed-root ACL/handle
        # admission. This portable callback supplies only the file-copy seam.
        checkpoint()
        destination = root / ".migration" / transfer_id / entry["area"] / entry["relative"]
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with Path(path).open("rb") as old, destination.open("xb") as new:
            shutil.copyfileobj(old, new)
        shutil.copystat(path, destination)

    monkeypatch.setitem(
        sys.modules, "k5vision.legacy_migration_copy", SimpleNamespace(copy_to_service_stage=copy)
    )
    yield SimpleNamespace(legacy=legacy, root=root, channel=channel)
    channel.actor.close()


def _tree_hashes(root):
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file() and path.name != "operator.lock"
    }


def test_real_stores_require_authenticated_copy_prepare_and_separate_commit(flow):
    before = _tree_hashes(flow.legacy.source)
    assert source.run_source(flow.channel, flow.legacy.source) == 0
    assert flow.channel.result["status"] == "ready"
    assert flow.channel.requests.index("prepare") < flow.channel.requests.index("commit")
    state = identity_state.load_identity_state(flow.root / "identity")
    assert state.site_id == flow.legacy.state.site_id
    users = UserRegistry(database_path=state.database_path, site_id=state.site_id)
    try:
        assert users.verify_password(username="legacy-admin", password=flow.legacy.password)
    finally:
        users.close()
    assert _tree_hashes(flow.legacy.source) == before


@pytest.mark.parametrize("failure", ["source_tamper", "authority_revoked", "native_peer_lost"])
def test_prepared_copy_never_commits_after_source_or_authority_loss(flow, failure):
    def hook(request, reply):
        if reply["status"] != "prepared":
            return
        if failure == "source_tamper":
            path = flow.legacy.source / "operator-account-security.sqlite3"
            with path.open("ab") as stream:
                stream.write(b"changed after preparation")
        elif failure == "authority_revoked":
            users = UserRegistry(
                database_path=flow.legacy.state.database_path, site_id=flow.legacy.state.site_id
            )
            try:
                users.reset_bootstrap(flow.legacy.account.id, actor="migration-test")
            finally:
                users.close()
        else:
            flow.channel.revoked = True

    flow.channel.hook = hook
    assert source.run_source(flow.channel, flow.legacy.source) == 1
    assert flow.channel.result["status"] == "refused"
    assert "commit" not in flow.channel.requests
    assert not (flow.root / "identity").exists()


def test_bad_existing_password_never_begins_target_or_initializes_replacement(flow):
    flow.legacy.password = "incorrect-existing-password"
    assert source.run_source(flow.channel, flow.legacy.source) == 1
    assert flow.channel.result["reason"] == "authentication_not_accepted"
    assert flow.channel.requests == []
    assert list(flow.root.iterdir()) == []


def test_insufficient_target_capacity_preserves_source_before_destination_file_copies(
    flow, monkeypatch
):
    before = _tree_hashes(flow.legacy.source)
    monkeypatch.setattr(service.shutil, "disk_usage", lambda path: SimpleNamespace(free=0))
    assert source.run_source(flow.channel, flow.legacy.source) == 1
    assert flow.channel.result["status"] == "refused"
    assert "capacity" in flow.channel.requests and "entry" not in flow.channel.requests
    assert not (flow.root / "identity").exists()
    assert _tree_hashes(flow.legacy.source) == before


@pytest.mark.parametrize("operation", ["status", "initialize", "serve"])
def test_legacy_guard_blocks_empty_initialization_and_startup(flow, operation, monkeypatch):
    import io
    import json

    marker = flow.root / "legacy-profiles.json"
    marker.write_text(json.dumps({"schema_version": 1, "profiles": ["S-1-5-21-1-2-3-1001"]}))
    marker.chmod(0o600)
    monkeypatch.setattr(
        service_setup, "_layout", lambda: service_setup._Layout(flow.root, flow.root)
    )
    request = {"schema_version": 1, "operation": operation}
    if operation == "initialize":
        request.update(
            username="replacement-admin",
            password="replacement-password",
            confirmation="replacement-password",
        )
    output = io.StringIO()
    code = service_setup.main(
        [], input_stream=io.BytesIO(json.dumps(request).encode()), output_stream=output
    )
    result = json.loads(output.getvalue())
    assert result["reason"] == "legacy_upgrade_required"
    assert result["status"] == ("migration_required" if operation == "status" else "refused")
    assert code == (0 if operation == "status" else 1)
    assert not (flow.root / "identity").exists()


def test_service_actor_rejects_unknown_fields_and_unprepared_commit(flow):
    binding = {
        "site_id": flow.legacy.state.site_id,
        "transfer_id": flow.legacy.transfer_id,
        "source_digest": "a" * 64,
        "source_owner_sid": "S-1-5-21-1-2-3-1001",
    }
    actor = flow.channel.actor
    assert (
        actor.handle({"schema_version": 1, "operation": "begin", "binding": binding})["status"]
        == "ok"
    )
    for request in [
        {"schema_version": 1, "operation": "commit"},
        {"schema_version": 1, "operation": "status", "source": "untrusted-file"},
        {"schema_version": True, "operation": "status"},
    ]:
        with pytest.raises(ValueError):
            actor.handle(request)
    assert not (flow.root / "identity").exists()


@pytest.mark.parametrize("operation", ["status", "initialize", "serve"])
def test_pending_journal_blocks_backend_even_when_valid_identity_manifest_is_visible(
    flow, operation, monkeypatch
):
    import io
    import json

    binding = {
        "site_id": flow.legacy.state.site_id,
        "transfer_id": flow.legacy.transfer_id,
        "source_digest": "a" * 64,
        "source_owner_sid": "S-1-5-21-1-2-3-1001",
    }
    flow.channel.actor.handle({"schema_version": 1, "operation": "begin", "binding": binding})
    shutil.copytree(flow.legacy.source, flow.root / "identity")
    identity_state.load_identity_state(flow.root / "identity")
    before = _tree_hashes(flow.root)
    monkeypatch.setattr(
        service_setup, "_layout", lambda: service_setup._Layout(flow.root, flow.root)
    )
    request = {"schema_version": 1, "operation": operation}
    if operation == "initialize":
        request.update(
            username="replacement-admin",
            password="replacement-password",
            confirmation="replacement-password",
        )
    output = io.StringIO()
    code = service_setup.main(
        [], input_stream=io.BytesIO(json.dumps(request).encode()), output_stream=output
    )
    result = json.loads(output.getvalue())
    assert result["reason"] == "legacy_upgrade_required"
    assert result["status"] == ("migration_required" if operation == "status" else "refused")
    assert code == (0 if operation == "status" else 1)
    assert _tree_hashes(flow.root) == before


def test_source_flow_preserves_mfa_and_maximum_unicode_oidc_in_distinct_crypto_contexts(
    crypto_legacy, private_profile, monkeypatch
):
    legacy = crypto_legacy
    legacy.lease.close()
    renamed = legacy.source.with_name("identity")
    legacy.source.rename(renamed)
    legacy.source = renamed
    legacy.password = "legacy-admin-password"
    legacy.state = identity_state.load_identity_state(legacy.source)
    source_security = AccountSecurityStore(
        legacy.source, legacy.state.site_id, protector=legacy.old
    )
    unicode_secret = "\U0001f600" * 4096
    OIDCStore(source_security).configure(
        enabled=True,
        display_name="Preserved issuer",
        issuer="https://id.example.test",
        client_id="preserved-client",
        client_secret=unicode_secret,
    )
    source_security.close()
    monkeypatch.setattr(
        authorization,
        "AccountSecurityStore",
        lambda path, site: AccountSecurityStore(path, site, protector=legacy.old),
    )
    monkeypatch.setattr(target, "ProfileSecretProtector", lambda site: legacy.new)
    monkeypatch.setattr(security_module, "ProfileSecretProtector", lambda site: legacy.new)
    root = private_profile / "service-crypto"
    root.mkdir(mode=0o700)
    channel = Channel(legacy, root)
    channel.code = totp(legacy.secret, int(time.time() // 30))
    sealed_source = {}

    def hook(request, reply):
        if request["operation"] == "begin":
            sealed_source.update(_tree_hashes(legacy.source))

    channel.hook = hook

    def copy(path, entry, transfer_id, *, checkpoint):
        checkpoint()
        destination = root / ".migration" / transfer_id / entry["area"] / entry["relative"]
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with Path(path).open("rb") as old, destination.open("xb") as new:
            shutil.copyfileobj(old, new)
        shutil.copystat(path, destination)

    monkeypatch.setitem(
        sys.modules, "k5vision.legacy_migration_copy", SimpleNamespace(copy_to_service_stage=copy)
    )
    try:
        assert source.run_source(channel, legacy.source) == 0
        state = identity_state.load_identity_state(root / "identity")
        migrated = AccountSecurityStore(state.directory, state.site_id, protector=legacy.new)
        try:
            assert OIDCStore(migrated).provider(private=True)["client_secret"] == unicode_secret
            assert migrated.status(UUID(legacy.user_id))["mfa_enabled"]
            assert not migrated.consume(UUID(legacy.user_id), channel.code)
            assert migrated.consume(
                UUID(legacy.user_id), totp(legacy.secret, int(time.time() // 30) + 1)
            )
        finally:
            migrated.close()
        assert _tree_hashes(legacy.source) == sealed_source
    finally:
        channel.actor.close()
