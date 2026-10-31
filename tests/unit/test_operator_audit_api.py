from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse, StreamingResponse
from test_operator_audit import audit_identity, audit_store  # noqa: F401

from k5vision.domain.users import UserAccount, UserRole
from k5vision.operator_audit import OperatorAuditError, set_audit_actor
from k5vision.operator_audit_api import install_operator_audit_api
from k5vision.operator_audit_middleware import OperatorAuditMiddleware

_URL = "/api/v1/operator/admin/activity"


@pytest.fixture
def audit_api(audit_store):  # noqa: F811
    account = UserAccount(
        username="audit-admin", display_name="Audit administrator", role=UserRole.ADMINISTRATOR
    )
    state = {"account": account, "revoked": False, "calls": 0}
    app = FastAPI()

    async def authenticate(token):
        if token != "generated-audit-session" or state["revoked"]:
            raise HTTPException(401, "Unauthorized")
        set_audit_actor(state["account"])
        return state["account"], token

    async def require_admin(request: Request):
        _, _, token = request.headers.get("authorization", "").partition(" ")
        current = await authenticate(token)
        if current[0].role is not UserRole.ADMINISTRATOR:
            raise HTTPException(403, "Denied")
        return current

    install_operator_audit_api(
        app, store=audit_store, require_admin=require_admin, authenticate=authenticate
    )

    @app.post("/api/v1/operator/cameras")
    async def camera(request: Request):
        await require_admin(request)
        state["calls"] += 1
        return {"created": True}

    @app.post("/api/v1/auth/login")
    async def login():
        return JSONResponse({"detail": "Unauthorized"}, status_code=state.get("login_status", 401))

    @app.post("/api/v1/auth/mfa")
    @app.post("/api/v1/auth/oidc/start")
    @app.get("/api/v1/auth/oidc/callback")
    @app.post("/api/v1/operator/security/mfa/enroll")
    @app.put("/api/v1/operator/admin/sso/provider")
    async def denied_security_operation():
        return JSONResponse({"detail": "Unauthorized"}, status_code=403)

    @app.get("/media/exports/{camera_id}/{export_id}.mp4")
    async def stream(camera_id: str, export_id: str):
        set_audit_actor(state["account"])

        async def content():
            yield b"generated prefix"
            raise RuntimeError("private decoder failure")

        return StreamingResponse(content(), media_type="video/mp4")

    app.add_middleware(OperatorAuditMiddleware, store=audit_store)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield SimpleNamespace(
            client=client,
            store=audit_store,
            state=state,
            headers={"Authorization": "Bearer generated-audit-session"},
        )


def test_admin_reads_filters_paginates_and_downloads_normal_json(audit_api):
    audit_api.store.record("camera.enroll", actor=audit_api.state["account"], target_id=uuid4())
    audit_api.store.record("auth.login", outcome="denied")
    response = audit_api.client.get(_URL, headers=audit_api.headers, params={"limit": 1})
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json()["events"][0]["id"] == 2
    assert response.json()["next_before"] == 2
    filtered = audit_api.client.get(
        _URL, headers=audit_api.headers, params={"action": "camera.enroll"}
    )
    assert len(filtered.json()["events"]) == 1
    download = audit_api.client.get(
        _URL + "/export", headers=audit_api.headers, params={"limit": 1}
    )
    assert download.status_code == 200
    assert download.headers["content-disposition"] == 'attachment; filename="k5-activity.json"'
    data = json.loads(download.content)
    assert data["has_more"] and len(data["events"]) == 1
    assert data["schema_version"] == 1 and data["time_basis"] == "host_observed_utc"
    assert [
        item["outcome"] for item in audit_api.store.events(action="audit.export")["events"]
    ] == ["success", "started"]


@pytest.mark.parametrize("path", [_URL, _URL + "/export"])
def test_unprivileged_or_missing_sessions_cannot_read_or_export(audit_api, path):
    assert audit_api.client.get(path).status_code == 401
    audit_api.state["account"] = audit_api.state["account"].model_copy(
        update={"role": UserRole.OPERATOR}
    )
    assert audit_api.client.get(path, headers=audit_api.headers).status_code == 403
    audit_api.state["revoked"] = True
    assert audit_api.client.get(path, headers=audit_api.headers).status_code == 401


def test_revocation_during_background_query_prevents_export(audit_api, monkeypatch):
    original = audit_api.store.events

    def revoke(**options):
        result = original(**options)
        audit_api.state["revoked"] = True
        return result

    monkeypatch.setattr(audit_api.store, "events", revoke)
    response = audit_api.client.get(_URL + "/export", headers=audit_api.headers)
    assert response.status_code == 401 and "content-disposition" not in response.headers


def test_read_permission_denial_is_audited_without_logging_routine_polling(audit_api):
    assert audit_api.client.get(_URL, headers=audit_api.headers).status_code == 200
    assert audit_api.store.events()["total_rows"] == 0
    assert audit_api.client.get(_URL).status_code == 401
    event = audit_api.store.events()["events"][0]
    assert event["action"] == "security.access" and event["outcome"] == "denied"
    assert event["status_code"] == 401


@pytest.mark.parametrize(
    "query",
    [
        {"limit": 101},
        {"before": 0},
        {"action": "private-secret"},
        {"outcome": "private-secret"},
        {"start": "2026-10-31T12:00:00"},
        {"start": "2026-10-31T12:00:00", "end": "2026-10-31T13:00:00"},
        {"start": "2026-10-31T12:00:00Z", "end": "2026-10-30T12:00:00Z"},
    ],
)
def test_invalid_filters_are_finite_errors(audit_api, query):
    response = audit_api.client.get(_URL, headers=audit_api.headers, params=query)
    assert response.status_code == 422 and "private-secret" not in response.text


def test_http_audit_records_success_denial_without_bodies_headers_queries(audit_api):
    response = audit_api.client.post(
        "/api/v1/operator/cameras?secret=private-query",
        headers={**audit_api.headers, "X-Private": "private-header"},
        json={"source": "rtsp://camera:private-password@private"},
    )
    assert response.status_code == 200 and audit_api.state["calls"] == 1
    camera = audit_api.store.events(action="camera.enroll")["events"]
    assert [item["outcome"] for item in camera] == ["success", "started"]
    assert camera[0]["actor_id"] == str(audit_api.state["account"].id)
    assert camera[0]["request_id"] == camera[1]["request_id"]
    assert (
        audit_api.client.post("/api/v1/auth/login", json={"password": "private-login"}).status_code
        == 401
    )
    login = audit_api.store.events(action="auth.login")["events"]
    assert login[0]["outcome"] == "denied" and login[0]["actor_id"] is None
    assert login[0]["request_id"] != camera[0]["request_id"]
    assert b"private" not in audit_api.store._path.read_bytes()


def test_failed_audit_preflight_prevents_camera_side_effect(audit_api, monkeypatch):
    def unavailable(*args, **kwargs):
        raise OperatorAuditError("Audit state unavailable")

    monkeypatch.setattr(audit_api.store, "record", unavailable)
    response = audit_api.client.post("/api/v1/operator/cameras", headers=audit_api.headers)
    assert response.status_code == 503 and response.json()["detail"] == "audit_unavailable"
    assert audit_api.state["calls"] == 0


@pytest.mark.parametrize(
    "method,path,action",
    [
        ("POST", "/api/v1/auth/mfa", "auth.mfa"),
        ("POST", "/api/v1/auth/oidc/start", "auth.sso"),
        ("GET", "/api/v1/auth/oidc/callback", "auth.sso"),
        ("POST", "/api/v1/operator/security/mfa/enroll", "security.mfa.configure"),
        ("PUT", "/api/v1/operator/admin/sso/provider", "security.sso.configure"),
    ],
)
def test_security_denials_are_recorded_on_real_identity_route_shapes(
    audit_api, method, path, action
):
    response = audit_api.client.request(method, path, params={"code": "private-provider-code"})
    assert response.status_code == 403
    events = audit_api.store.events(action=action)["events"]
    assert [event["outcome"] for event in events] == ["denied", "started"]
    assert b"private-provider" not in audit_api.store._path.read_bytes()


@pytest.mark.parametrize("code", [202, 303])
def test_primary_factor_and_redirect_do_not_claim_completed_authentication(audit_api, code):
    audit_api.state["login_status"] = code
    assert audit_api.client.post("/api/v1/auth/login").status_code == code
    assert {event["outcome"] for event in audit_api.store.events()["events"]} == {"started"}


def test_interrupted_stream_is_audited_as_incomplete_not_success(audit_api):
    camera, export = uuid4(), uuid4()
    audit_api.client.get(f"/media/exports/{camera}/{export}.mp4")
    entries = audit_api.store.events(action="export.download")["events"]
    assert [item["outcome"] for item in entries] == ["interrupted", "started"]
    assert entries[0]["target_id"] == str(camera) and entries[0]["status_code"] == 200
    assert b"private decoder" not in audit_api.store._path.read_bytes()


def test_completion_storage_failure_preserves_started_evidence_and_blocks_next_action(audit_api):
    connection = sqlite3.connect(audit_api.store._path)
    connection.execute(
        "CREATE TRIGGER refuse_completion BEFORE INSERT ON audit_events "
        "WHEN NEW.outcome!='started' BEGIN SELECT RAISE(ABORT,'test-storage-failure'); END"
    )
    connection.close()
    assert (
        audit_api.client.post("/api/v1/operator/cameras", headers=audit_api.headers).status_code
        == 200
    )
    assert audit_api.store.events()["events"][0]["outcome"] == "started"
    assert audit_api.store.events()["logging_available"] is False
    assert (
        audit_api.client.post("/api/v1/operator/cameras", headers=audit_api.headers).status_code
        == 503
    )
    assert audit_api.state["calls"] == 1
