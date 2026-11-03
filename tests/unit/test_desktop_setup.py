"""The GUI account bridge preserves identity custody and owns server lifetime."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import secrets
import threading
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from k5vision import desktop_setup
from k5vision.identity_state import initialize_identity_state, load_identity_state
from k5vision.services.user_registry import UserRegistry


@pytest.fixture
def password():
    return secrets.token_urlsafe(24)


def _payload(directory, *, operation="status", **fields):
    return json.dumps(
        {
            "schema_version": 1,
            "operation": operation,
            "identity_directory": str(directory),
            **fields,
        }
    ).encode("utf-8")


def _initialize(directory, credential, **overrides):
    return _payload(
        directory,
        operation="initialize",
        **{
            "username": "recovery-admin",
            "password": credential,
            "confirmation": credential,
            **overrides,
        },
    )


def _call(payload, *, arguments=(), expected_lines=1):
    output = io.StringIO()
    code = desktop_setup.main(arguments, input_stream=io.BytesIO(payload), output_stream=output)
    response = output.getvalue()
    assert response.count("\n") == expected_lines
    lines = [json.loads(line) for line in response.splitlines()]
    if expected_lines == 2:
        assert lines[0] == {"schema_version": 1, "status": "running"}
    return code, lines[-1]


def test_gui_setup_creates_normal_persistent_auth_without_exposing_credentials(tmp_path, password):
    directory = tmp_path / "identity"
    before = list(tmp_path.iterdir())
    assert _call(_payload(directory)) == (
        0,
        {"schema_version": 1, "status": "new", "reason": "account_required"},
    )
    assert list(tmp_path.iterdir()) == before
    code, response = _call(_initialize(directory, password))
    assert code == 0 and response["reason"] == "account_initialized"
    assert password not in json.dumps(response)
    assert str(directory) not in json.dumps(response)
    state = load_identity_state(directory)
    registry = UserRegistry(database_path=state.database_path, site_id=state.site_id)
    try:
        account = registry.verify_password(username="recovery-admin", password=password)
        assert account is not None and account.role.value == "administrator"
    finally:
        registry.close()
    assert _call(_payload(directory)) == (
        0,
        {"schema_version": 1, "status": "ready", "reason": "existing_identity"},
    )
    assert password.encode() not in state.database_path.read_bytes()
    assert password not in (directory / "identity.json").read_text()


def test_repeat_and_partial_setup_preserve_every_existing_byte(tmp_path, password):
    complete = tmp_path / "complete"
    initialize_identity_state(complete, username="recovery-admin", password=password)
    partial = tmp_path / "partial"
    partial.mkdir(mode=0o700)
    (partial / "sentinel").write_bytes(b"preserve partial setup")
    for directory in (complete, partial):
        before = {p.name: p.read_bytes() for p in directory.iterdir()}
        code, result = _call(_initialize(directory, secrets.token_urlsafe(24)))
        assert code == 1 and result["reason"] == "unsafe_state"
        assert {p.name: p.read_bytes() for p in directory.iterdir()} == before
    assert _call(_payload(partial))[1]["reason"] == "unsafe_state"


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("username", " user", "invalid_username"),
        ("username", "../user", "invalid_username"),
        ("username", True, "invalid_username"),
        ("password", "too short", "invalid_password"),
        ("password", "a" * 257, "invalid_password"),
        ("password", 12345, "invalid_password"),
        ("password", "a" * 12 + "\ud800", "invalid_password"),
        ("confirmation", "a" * 24, "password_mismatch"),
    ],
)
def test_invalid_credentials_do_not_touch_disk(tmp_path, password, field, value, reason):
    directory = tmp_path / "identity"
    code, result = _call(_initialize(directory, password, **{field: value}))
    assert code == 1 and result["reason"] == reason
    assert not directory.exists()


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"[]",
        b"\xff",
        b"{",
        b'{"schema_version":1,"schema_version":1}',
        b'{"schema_version":true,"operation":"status","identity_directory":"/x"}',
        b'{"schema_version":1,"operation":"status","identity_directory":"/x","extra":1}',
        b'{"schema_version":1,"operation":"status","identity_directory":null}',
        b'{"schema_version":1,"operation":"status","identity_directory":"\\ud800"}',
        b"{" + b"x" * desktop_setup.MAX_REQUEST_BYTES,
        b"[" * 2000 + b"]" * 2000,
    ],
    ids=[
        "empty",
        "array",
        "invalid-utf8",
        "truncated-json",
        "duplicate-field",
        "boolean-schema",
        "extra-field",
        "null-directory",
        "invalid-unicode",
        "oversized-request",
        "deeply-nested-json",
    ],
)
def test_malformed_duplicate_large_or_unsupported_input_has_finite_safe_output(payload):
    code, result = _call(payload)
    assert code == 1
    assert result == {"schema_version": 1, "status": "refused", "reason": "invalid_request"}


def test_extra_line_or_secret_argument_is_not_echoed_or_processed(tmp_path, password):
    directory = tmp_path / "identity"
    for payload, arguments in (
        (_initialize(directory, password) + b"\nsecond request", ()),
        (_initialize(directory, password), ("--password", password)),
    ):
        code, result = _call(payload, arguments=arguments)
        assert code == 1 and result["reason"] == "invalid_request"
        assert password not in json.dumps(result)
        assert not directory.exists()


def test_tty_input_is_refused_before_read_or_setup(tmp_path):
    class Terminal(io.BytesIO):
        def isatty(self):
            return True

        def readline(self, _limit):
            pytest.fail("interactive input must not be read")

    output = io.StringIO()
    assert desktop_setup.main([], input_stream=Terminal(), output_stream=output) == 1
    assert json.loads(output.getvalue())["reason"] == "invalid_request"


def test_discard_stream_supports_actual_uvicorn_formatter_without_terminal_output(capsys):
    from uvicorn.logging import DefaultFormatter

    sink = desktop_setup._DiscardOutput()
    with redirect_stdout(sink), redirect_stderr(sink):
        formatter = DefaultFormatter()
        record = logging.LogRecord("uvicorn.error", logging.WARNING, "", 0, "suppressed", (), None)
        message = formatter.format(record)
        assert "\x1b" not in message
        assert sink.write(message) == len(message)
        sink.flush()
    output = capsys.readouterr()
    assert output.out == output.err == ""


def test_setup_failure_preserves_partial_state_and_never_exposes_exception(
    tmp_path, password, monkeypatch
):
    directory = tmp_path / "identity"

    def fail(path, **_kwargs):
        Path(path).mkdir(mode=0o700)
        (Path(path) / "partial").write_text("keep")
        raise RuntimeError(password + " " + str(path))

    monkeypatch.setattr(desktop_setup, "initialize_identity_state", fail)
    code, result = _call(_initialize(directory, password))
    assert code == 1 and result["reason"] == "setup_failed"
    assert password not in json.dumps(result) and str(directory) not in json.dumps(result)
    assert (directory / "partial").read_text() == "keep"
    assert _call(_initialize(directory, password))[1]["reason"] == "unsafe_state"


@pytest.mark.skipif(os.name != "posix", reason="POSIX link and replaceable-parent fixture")
def test_linked_or_replaceable_identity_parent_is_not_adopted(tmp_path, password):
    foreign = tmp_path / "foreign"
    foreign.mkdir(mode=0o700)
    link = tmp_path / "linked"
    link.symlink_to(foreign, target_is_directory=True)
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o777)
    shared.chmod(0o777)
    for directory in (link / "identity", shared / "identity"):
        assert _call(_payload(directory))[1]["reason"] == "unsafe_state"
        assert _call(_initialize(directory, password))[1]["reason"] == "unsafe_state"
    assert list(foreign.iterdir()) == [] and list(shared.iterdir()) == []


def _serve_payload(tmp_path, **overrides):
    return _payload(
        tmp_path / "identity",
        operation="serve",
        **{
            "recording_root": str(tmp_path / "recordings"),
            "media_executable": str(tmp_path / "media.exe"),
            "gateway_executable": str(tmp_path / "gateway.exe"),
            "port": 8000,
            "recording_limit_gb": 20,
            **overrides,
        },
    )


@pytest.mark.parametrize("control", [b"stop\n", b"", b"invalid input\n"])
def test_pipe_stop_or_owner_exit_requests_cleanup_and_suppresses_raw_logs(
    tmp_path, password, monkeypatch, capsys, control
):
    import k5vision.operator_product as product

    state = initialize_identity_state(
        tmp_path / "identity", username="recovery-admin", password=password
    )
    calls = []

    def run(**kwargs):
        calls.append(kwargs)
        print(password + str(state.directory))
        kwargs["ready_callback"]()
        assert kwargs["shutdown_requested"].__self__.wait(timeout=2)

    monkeypatch.setattr(product, "run_operator_product", run)
    code, result = _call(_serve_payload(tmp_path) + b"\n" + control, expected_lines=2)
    assert code == 0 and result["reason"] == "operator_stopped"
    assert calls[0]["identity_directory"] == state.directory
    assert calls[0]["open_browser"] is False
    output = capsys.readouterr()
    assert password not in output.out + output.err + json.dumps(result)
    assert str(state.directory) not in output.out + output.err + json.dumps(result)


@pytest.mark.parametrize(
    "overrides",
    [
        {"port": True},
        {"port": 0},
        {"port": 65536},
        {"recording_limit_gb": 4097},
        {"recording_root": "relative"},
        {"media_executable": None},
    ],
)
def test_serve_validation_precedes_any_worker_creation(tmp_path, monkeypatch, overrides):
    monkeypatch.setattr(
        desktop_setup, "load_identity_state", lambda _path: pytest.fail("no admission yet")
    )
    assert _call(_serve_payload(tmp_path, **overrides))[1]["reason"] == "invalid_request"


def test_serve_refuses_partial_identity_before_starting_workers(tmp_path, monkeypatch):
    import k5vision.operator_product as product

    (tmp_path / "identity").mkdir(mode=0o700)
    monkeypatch.setattr(product, "run_operator_product", lambda **_kwargs: pytest.fail("no worker"))
    assert _call(_serve_payload(tmp_path))[1]["reason"] == "unsafe_state"


def test_serve_exception_is_finite_and_does_not_expose_paths(tmp_path, password, monkeypatch):
    import k5vision.operator_product as product

    initialize_identity_state(tmp_path / "identity", username="recovery-admin", password=password)

    def fail(**_kwargs):
        raise RuntimeError(password + str(tmp_path))

    monkeypatch.setattr(product, "run_operator_product", fail)
    code, result = _call(_serve_payload(tmp_path))
    assert code == 1 and result["reason"] == "startup_failed"
    assert password not in json.dumps(result) and str(tmp_path) not in json.dumps(result)


def test_uvicorn_shutdown_hook_exits_through_server_cleanup_without_lingering_task():
    from k5vision.operator_product import _serve_with_shutdown

    async def exercise():
        started = asyncio.Event()
        stop = threading.Event()

        class Server:
            should_exit = False
            cleaned = False

            async def serve(self, *, sockets):
                assert sockets == []
                started.set()
                try:
                    while not self.should_exit:
                        await asyncio.sleep(0.001)
                finally:
                    self.cleaned = True

        server = Server()
        task = asyncio.create_task(
            _serve_with_shutdown(server, sockets=[], shutdown_requested=stop.is_set)
        )
        await started.wait()
        assert not task.done()
        stop.set()
        await asyncio.wait_for(task, timeout=2)
        assert server.should_exit and server.cleaned
        assert not any(
            item.get_name() == "operator-desktop-owner-lifetime" and not item.done()
            for item in asyncio.all_tasks()
        )

    asyncio.run(exercise())


@pytest.mark.parametrize("fail_start", [False, True])
def test_ready_signal_requires_successful_native_start_and_owned_profile(
    tmp_path, password, monkeypatch, fail_start
):
    import k5vision.operator_product as product
    from k5vision.operator_profile_lock import OperatorProfileLock, OperatorProfileLockError

    identity = initialize_identity_state(
        tmp_path / "identity", username="recovery-admin", password=password
    )
    events = []

    class Cameras:
        def __init__(self, *_args, **_kwargs):
            pass

        def list_sources(self):
            return ()

        def close(self):
            events.append("cameras_closed")

    async def start(_sources):
        events.append("native_start")
        if fail_start:
            raise RuntimeError("native startup refused")

    async def stop():
        events.append("native_stop")

    def ready():
        assert events == ["native_start"]
        lease = OperatorProfileLock(identity.directory)
        with pytest.raises(OperatorProfileLockError):
            lease.acquire()
        lease.close()
        events.append("ready")

    monkeypatch.setattr(product, "OperatorCameraStore", Cameras)
    service = SimpleNamespace(site_id=identity.site_id, public_port=8080, start=start, stop=stop)
    app = product.create_operator_product_app(
        identity_directory=identity.directory, service=service, ready_callback=ready
    )
    if fail_start:
        with pytest.raises(RuntimeError, match="startup"):
            with TestClient(app):
                pass
        assert "ready" not in events
    else:
        with TestClient(app):
            assert "ready" in events
    assert events[-2:] == ["native_stop", "cameras_closed"]
    lease = OperatorProfileLock(identity.directory)
    lease.acquire()
    lease.close()
