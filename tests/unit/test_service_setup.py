"""The unattended service bridge cannot select user paths or bypass identity custody."""

from __future__ import annotations

import ctypes
import io
import json
import os
import secrets
from types import SimpleNamespace

import pytest
from test_operator_time_admin import private_profile as private_profile_fixture

from k5vision import service_setup
from k5vision.identity_state import load_identity_state
from k5vision.services.user_registry import UserRegistry

private_profile = private_profile_fixture


def _payload(operation="status", **fields):
    return json.dumps({"schema_version": 1, "operation": operation, **fields}).encode()


def _call(payload, *, arguments=()):
    destination = io.StringIO()
    code = service_setup.main(
        arguments, input_stream=io.BytesIO(payload), output_stream=destination
    )
    return code, [json.loads(line) for line in destination.getvalue().splitlines()]


@pytest.fixture
def layout(tmp_path, private_profile, monkeypatch):
    root = private_profile / "machine-private"
    root.mkdir(mode=0o700)
    value = service_setup._Layout(root, tmp_path / "immutable-application")
    monkeypatch.setattr(service_setup, "_layout", lambda: value)
    return value


def _initialize():
    password = secrets.token_urlsafe(24)
    return password, _payload(
        "initialize", username="site-admin", password=password, confirmation=password
    )


def test_bootstrap_keeps_site_and_credentials_only_in_fixed_private_profile(layout):
    assert _call(_payload()) == (
        0,
        [{"schema_version": 1, "status": "new", "reason": "account_required"}],
    )
    assert list(layout.root.iterdir()) == []
    password, request = _initialize()
    assert _call(request) == (
        0,
        [{"schema_version": 1, "status": "ready", "reason": "account_initialized"}],
    )
    identity = load_identity_state(layout.root / "identity")
    registry = UserRegistry(database_path=identity.database_path, site_id=identity.site_id)
    try:
        assert registry.verify_password(username="site-admin", password=password) is not None
    finally:
        registry.close()
    before = {path.name: path.read_bytes() for path in identity.directory.iterdir()}
    assert _call(request)[1][0]["reason"] == "unsafe_state"
    assert {path.name: path.read_bytes() for path in identity.directory.iterdir()} == before
    assert _call(_payload())[1][0]["reason"] == "existing_identity"
    assert load_identity_state(identity.directory).site_id == identity.site_id
    assert all(password.encode() not in value for value in before.values())


@pytest.mark.parametrize(
    "field",
    [
        "identity_directory",
        "recording_root",
        "media_executable",
        "gateway_executable",
        "port",
        "recording_limit_gb",
        "legacy_profile",
    ],
)
@pytest.mark.parametrize("operation", ["status", "initialize", "serve"])
def test_client_cannot_select_paths_programs_or_listener(layout, field, operation):
    fields = {}
    if operation == "initialize":
        fields.update(username="site-admin", password="private-fixture", confirmation="x")
    assert _call(_payload(operation, **fields, **{field: "untrusted-selection"})) == (
        1,
        [{"schema_version": 1, "status": "refused", "reason": "invalid_request"}],
    )
    assert list(layout.root.iterdir()) == []


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"[]",
        b"\xff",
        b'{"schema_version":true,"operation":"status"}',
        b'{"schema_version":1,"operation":"status","operation":"initialize"}',
        b'{"schema_version":1,"operation":"migrate"}',
        b'{"schema_version":1,"operation":"status"}\nsecond request',
        b"{" + b"x" * service_setup.desktop_setup.MAX_REQUEST_BYTES,
    ],
    ids=[
        "empty",
        "array",
        "encoding",
        "boolean-schema",
        "duplicate",
        "migration",
        "extra-line",
        "size",
    ],
)
def test_malformed_or_unqualified_migration_requests_never_mutate(layout, payload):
    assert _call(payload) == (
        1,
        [{"schema_version": 1, "status": "refused", "reason": "invalid_request"}],
    )
    assert list(layout.root.iterdir()) == []


def test_worker_refuses_wrong_identity_without_touching_profile(layout, monkeypatch):
    def unavailable():
        raise RuntimeError("private native detail")

    monkeypatch.setattr(service_setup, "_layout", unavailable)
    assert _call(_initialize()[1]) == (
        1,
        [{"schema_version": 1, "status": "refused", "reason": "service_identity_unavailable"}],
    )
    assert list(layout.root.iterdir()) == []


def test_no_credential_or_configuration_arguments_are_accepted(layout):
    password, request = _initialize()
    code, responses = _call(request, arguments=("--password", password))
    assert code == 1 and responses[0]["reason"] == "invalid_request"
    assert password not in json.dumps(responses)
    assert list(layout.root.iterdir()) == []


def test_service_owns_serve_lifetime_and_uses_only_derived_paths(layout, monkeypatch):
    assert _call(_initialize()[1])[0] == 0
    observed = {}

    def run(**options):
        observed.update(options)
        options["ready_callback"]()
        # EOF of the service-owned control pipe stops the worker. A browser or
        # client process has no ownership of this pipe.
        assert options["shutdown_requested"].__self__.wait(2)

    monkeypatch.setattr("k5vision.operator_product.run_operator_product", run)
    assert _call(_payload("serve")) == (
        0,
        [
            {"schema_version": 1, "status": "running"},
            {"schema_version": 1, "status": "stopped", "reason": "operator_stopped"},
        ],
    )
    assert observed["identity_directory"] == layout.root / "identity"
    assert observed["recording_root"] == layout.root / "recordings"
    assert observed["media_executable"] == layout.application / "workers" / "mediamtx.exe"
    assert observed["gateway_executable"] == layout.application / "workers" / "caddy.exe"
    assert observed["port"] == 8000 and observed["open_browser"] is False


class _Function:
    def __init__(self, invoke):
        self.invoke = invoke

    def __call__(self, *args):
        return self.invoke(*args)


def test_program_data_uses_native_known_folder_and_releases_native_buffer(tmp_path, monkeypatch):
    value = ctypes.create_unicode_buffer(str(tmp_path))
    released = []

    def known_folder(identifier, flags, token, result):
        assert bytes(identifier)[:16] == service_setup._PROGRAM_DATA.bytes_le
        assert flags == 0 and token is None
        result._obj.value = ctypes.addressof(value)
        return 0

    shell = SimpleNamespace(SHGetKnownFolderPath=_Function(known_folder))
    ole = SimpleNamespace(CoTaskMemFree=_Function(lambda pointer: released.append(pointer.value)))
    monkeypatch.setattr(
        ctypes, "WinDLL", lambda name, **kwargs: shell if name == "shell32" else ole, raising=False
    )
    monkeypatch.setenv("ProgramData", str(tmp_path / "hostile-override"))
    assert service_setup._program_data() == tmp_path
    assert released == [ctypes.addressof(value)]


@pytest.mark.parametrize("current", ["service-account", "administrator", "local-service"])
def test_exact_virtual_token_user_is_required(current, monkeypatch):
    resolved = []

    def lookup(system, account, *args):
        resolved.append((system, account))
        return 1

    native = SimpleNamespace(
        security=SimpleNamespace(LookupAccountNameW=lookup),
        current_user=lambda: current,
        sid=lambda pointer: "service-account",
    )
    monkeypatch.setattr(service_setup, "_native_security", lambda: native)
    # Change os.name only during the native-only function: pathlib must retain
    # the actual test platform's path implementation.
    with monkeypatch.context() as patch:
        patch.setattr(os, "name", "nt")
        if current == "service-account":
            service_setup._require_service_token()
            assert service_setup.is_recorder_account()
        else:
            with pytest.raises(ValueError):
                service_setup._require_service_token()
            assert not service_setup.is_recorder_account()
    assert resolved == [(None, service_setup.SERVICE_ACCOUNT)] * 2


def test_unsupported_platform_has_no_recorder_account(monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(os, "name", "posix")
        assert not service_setup.is_recorder_account()
