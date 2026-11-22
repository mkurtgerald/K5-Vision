"""Actual readonly SQLite inspection handles must close before native cleanup."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _harness():
    spec = importlib.util.spec_from_file_location(
        "legacy_sqlite_lifecycle", ROOT / "tests/integration/test_windows_desktop_upgrade.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fixture(tmp_path):
    identity = tmp_path / "identity"
    identity.mkdir()
    with closing(sqlite3.connect(identity / "operator-account-security.sqlite3")) as database:
        database.executescript(
            "CREATE TABLE mfa(user_id TEXT,secret BLOB,generation INTEGER);"
            "CREATE TABLE oidc_provider(id INTEGER,client_secret BLOB);"
        )
        database.execute(
            "INSERT INTO mfa VALUES(?,?,?)", ("generated-user", b"dpapi-v1:fake-mfa", 1)
        )
        database.execute("INSERT INTO oidc_provider VALUES(?,?)", (1, b"dpapi-v1:fake-oidc"))
        database.commit()
    with closing(sqlite3.connect(identity / "operator-cameras.sqlite3")) as database:
        database.execute(
            "CREATE TABLE camera_sources(device_id TEXT,device_json TEXT,source_uri TEXT)"
        )
        database.execute("INSERT INTO camera_sources VALUES(?,?,?)", ("generated", "{}", "private"))
        database.commit()
    values = {
        "/api/v1/operator/admin/users": {
            "users": [{"id": "a", "camera_access": []}, {"id": "b", "camera_access": ["generated"]}]
        },
        "/api/v1/operator/cameras": {"cameras": [{"device_id": "generated"}]},
        "/api/v1/operator/admin/storage": {
            "revision": 1,
            "quota_bytes": 10000,
            "minimum_free_bytes": 100,
            "retention_seconds": 10,
        },
        "/api/v1/operator/admin/activity/export": {"site_id": "generated-site"},
    }
    return SimpleNamespace(json=lambda route: values[route])


@pytest.mark.parametrize("helper", ["_security", "_retained"])
@pytest.mark.parametrize("query_fails", [False, True])
def test_real_inspection_connection_is_closed_with_references_held(
    tmp_path, monkeypatch, helper, query_fails
):
    harness = _harness()
    client = _fixture(tmp_path)
    if query_fails:
        name = (
            "operator-account-security.sqlite3"
            if helper == "_security"
            else "operator-cameras.sqlite3"
        )
        table = "mfa" if helper == "_security" else "camera_sources"
        with closing(sqlite3.connect(tmp_path / "identity" / name)) as database:
            database.execute("DROP TABLE " + table)
            database.commit()
    references = []
    real_connect = sqlite3.connect

    def connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        references.append(connection)
        return connection

    monkeypatch.setattr(harness.sqlite3, "connect", connect)

    def inspect():
        return (
            harness._security(tmp_path)
            if helper == "_security"
            else harness._retained(client, tmp_path)
        )

    try:
        if query_fails:
            with pytest.raises(sqlite3.OperationalError):
                inspect()
        else:
            assert inspect()
        assert len(references) == 1
        # A SQLite connection context alone leaves this actual handle usable.
        # Holding the reference makes this control independent of cyclic GC.
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            references[0].execute("SELECT 1")
    finally:
        for connection in references:
            connection.close()


@pytest.mark.parametrize("code", [5, 32, None, True, -1, 65536])
def test_cleanup_failure_only_retains_finite_role_operation_kind_and_winerror(code):
    harness = _harness()
    error = PermissionError("private exception path and identity never retained")
    error.winerror = code
    diagnostic = {}
    harness._record_owned_cleanup_failure(diagnostic, "legacy_profile", "remove_tree", error)
    assert diagnostic == {
        "owned_file_cleanup_facts": [
            {
                "tree_role": "legacy_profile",
                "operation": "remove_tree",
                "error_type": "PermissionError",
                "winerror": code if type(code) is int and 0 < code <= 65535 else None,
            }
        ]
    }
    assert "private" not in json.dumps(diagnostic)


def test_cleanup_failure_facts_have_original_three_root_bound_and_no_exception_details():
    harness = _harness()
    diagnostic = {}
    for _ in range(5):
        harness._record_owned_cleanup_failure(
            diagnostic, "proof_parent", "root_identity", RuntimeError("private")
        )
    assert len(diagnostic["owned_file_cleanup_facts"]) == 3
    assert all(
        row["error_type"] == "Other" and row["winerror"] is None
        for row in diagnostic["owned_file_cleanup_facts"]
    )


def test_cleanup_observer_normalizes_unknown_roles_and_handles_unreadable_error_code():
    harness = _harness()

    class HiddenError(Exception):
        @property
        def winerror(self):
            raise RuntimeError("private failure detail")

    diagnostic = {}
    harness._record_owned_cleanup_failure(
        diagnostic, ["private path"], "private-operation", HiddenError()
    )
    assert diagnostic == {
        "owned_file_cleanup_facts": [
            {
                "tree_role": "unknown",
                "operation": "unknown",
                "error_type": "Other",
                "winerror": None,
            }
        ]
    }


def _cleanup_block(harness):
    import ast
    import inspect

    tree = ast.parse(
        inspect.getsource(harness.test_native_windows_legacy_service_migration_and_manual_reversal)
    )
    matches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If) and ast.unparse(node.test) == "not cleanup_errors"
    ]
    assert len(matches) == 1
    return compile(
        ast.fix_missing_locations(ast.Module(body=matches, type_ignores=[])),
        "<actual_owned_cleanup_block>",
        "exec",
    )


@pytest.mark.parametrize("failure_role", ["machine_data", "legacy_profile", "proof_parent"])
@pytest.mark.parametrize("failure_phase", ["root_identity", "remove_tree"])
def test_original_cleanup_rejects_the_same_failure_without_retry_or_weaker_root_admission(
    failure_role, failure_phase
):
    harness = _harness()
    roles = ["machine_data", "legacy_profile", "proof_parent"]

    # Object identity keeps these intentionally synthetic trees hashable.
    class Tree:
        def __init__(self, index):
            self.index = index

        def exists(self):
            return True

        def stat(self):
            return SimpleNamespace(st_dev=1, st_ino=self.index)

    trees = [Tree(index) for index in range(3)]
    owned = {tree: (1, tree.index) for tree in trees}
    if failure_phase == "root_identity":
        owned[trees[roles.index(failure_role)]] = (2, 999)
    removed = []

    def remove(tree):
        removed.append(tree.index)
        if failure_phase == "remove_tree" and roles[tree.index] == failure_role:
            error = PermissionError("private fixture path")
            error.winerror = 32
            raise error

    diagnostic, cleanup_errors = {}, []
    namespace = dict(
        machine_profile=trees[0],
        profile=trees[1],
        parent=trees[2],
        owned_trees=owned,
        d=SimpleNamespace(_remove_owned_temporary_tree=remove),
        diagnostic=diagnostic,
        cleanup_errors=cleanup_errors,
        _record_owned_cleanup_failure=harness._record_owned_cleanup_failure,
    )
    exec(_cleanup_block(harness), namespace)
    assert cleanup_errors == ["owned_files_cleanup"]
    assert diagnostic == {
        "owned_file_cleanup_facts": [
            {
                "tree_role": failure_role,
                "operation": failure_phase,
                "error_type": "PermissionError"
                if failure_phase == "remove_tree"
                else "AssertionError",
                "winerror": 32 if failure_phase == "remove_tree" else None,
            }
        ]
    }
    expected_removed = [
        index
        for index in range(3)
        if failure_phase != "root_identity" or roles[index] != failure_role
    ]
    assert removed == expected_removed
