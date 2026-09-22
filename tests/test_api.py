import json

import pytest
from fastapi.testclient import TestClient

from k5vision.main import (
    CONTROL_PLANE_READ_TOKEN_ENV,
    CONTROL_PLANE_SITE_ENV,
    CONTROL_PLANE_TOKEN_ENV,
    DEVICE_DB_PATH_ENV,
    MAX_DEVICE_REQUEST_BYTES,
    create_app,
)

CONTROL_PLANE_TOKEN = "test-control-plane-token"
CONTROL_PLANE_READ_TOKEN = "test-control-plane-read-token"
CONTROL_PLANE_SITE = "test-site"


@pytest.fixture(autouse=True)
def configure_device_state(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv(CONTROL_PLANE_SITE_ENV, CONTROL_PLANE_SITE)
    monkeypatch.setenv(DEVICE_DB_PATH_ENV, str(tmp_path / "devices.sqlite3"))
    monkeypatch.delenv(CONTROL_PLANE_READ_TOKEN_ENV, raising=False)


def _auth_headers(token: str = CONTROL_PLANE_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _device_payload(index: int = 1) -> dict[str, object]:
    return {
        "name": f"Synthetic Camera {index}",
        "host": f"192.0.2.{index}",
        "kind": "camera",
        "protocols": ["rtsp"],
        "tags": ["synthetic"],
    }


def test_health_reports_version() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["version"] == "0.1.0"


def test_device_api_fails_closed_without_auth_configuration(monkeypatch) -> None:
    monkeypatch.delenv(CONTROL_PLANE_TOKEN_ENV, raising=False)

    with TestClient(create_app()) as client:
        listed = client.get("/api/v1/devices")
        created = client.post("/api/v1/devices", json=_device_payload())

    assert listed.status_code == 503
    assert created.status_code == 503


@pytest.mark.parametrize("missing_env", [CONTROL_PLANE_SITE_ENV, DEVICE_DB_PATH_ENV])
def test_device_api_fails_closed_without_durable_scope_configuration(
    monkeypatch: pytest.MonkeyPatch, missing_env: str
) -> None:
    monkeypatch.delenv(missing_env, raising=False)
    with TestClient(create_app(control_plane_token=CONTROL_PLANE_TOKEN)) as client:
        response = client.get("/api/v1/devices", headers=_auth_headers())
        health = client.get("/api/v1/health")

    assert response.status_code == 503
    assert response.json() == {"detail": "Control-plane device state is not configured"}
    assert health.status_code == 200


def test_device_api_rejects_missing_or_invalid_bearer_token() -> None:
    with TestClient(create_app(control_plane_token=CONTROL_PLANE_TOKEN)) as client:
        missing = client.get("/api/v1/devices")
        invalid = client.get(
            "/api/v1/devices",
            headers=_auth_headers("wrong-control-plane-token"),
        )

    assert missing.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"
    assert invalid.status_code == 401


def test_read_only_device_credential_can_read_but_cannot_enroll() -> None:
    payload = _device_payload()
    write_headers = _auth_headers()
    read_headers = _auth_headers(CONTROL_PLANE_READ_TOKEN)

    with TestClient(
        create_app(
            control_plane_token=CONTROL_PLANE_TOKEN,
            control_plane_read_token=CONTROL_PLANE_READ_TOKEN,
        )
    ) as client:
        created = client.post("/api/v1/devices", json=payload, headers=write_headers)
        listed = client.get("/api/v1/devices", headers=read_headers)
        fetched = client.get(f"/api/v1/devices/{created.json()['id']}", headers=read_headers)
        rejected = client.post("/api/v1/devices", json=_device_payload(2), headers=read_headers)
        remaining = client.get("/api/v1/devices", headers=write_headers)

    assert created.status_code == 201
    assert listed.status_code == 200
    assert listed.json() == [created.json()]
    assert fetched.status_code == 200
    assert fetched.json() == created.json()
    assert rejected.status_code == 403
    assert rejected.json() == {"detail": "Insufficient device permission"}
    assert remaining.json() == [created.json()]


def test_device_api_fails_closed_for_ambiguous_permission_tokens() -> None:
    with TestClient(
        create_app(
            control_plane_token=CONTROL_PLANE_TOKEN,
            control_plane_read_token=CONTROL_PLANE_TOKEN,
        )
    ) as client:
        read = client.get("/api/v1/devices", headers=_auth_headers())
        write = client.post("/api/v1/devices", json=_device_payload(), headers=_auth_headers())

    assert read.status_code == 503
    assert write.status_code == 503
    assert read.json() == {"detail": "Control-plane authentication is not configured"}
    assert write.json() == {"detail": "Control-plane authentication is not configured"}


def test_device_api_fails_closed_for_invalid_read_token_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(CONTROL_PLANE_TOKEN_ENV, CONTROL_PLANE_TOKEN)
    monkeypatch.setenv(CONTROL_PLANE_READ_TOKEN_ENV, "invalid-\u00e9")

    with TestClient(create_app()) as client:
        response = client.get("/api/v1/devices", headers=_auth_headers())

    assert response.status_code == 503
    assert response.json() == {"detail": "Control-plane authentication is not configured"}


def test_device_registration_round_trip() -> None:
    payload = {
        "name": "Gate Camera 01",
        "host": "192.168.10.21",
        "kind": "camera",
        "protocols": ["onvif", "rtsp"],
        "tags": ["gate", "perimeter"],
    }
    headers = _auth_headers()

    with TestClient(create_app(control_plane_token=CONTROL_PLANE_TOKEN)) as client:
        created = client.post("/api/v1/devices", json=payload, headers=headers)
        listed = client.get("/api/v1/devices", headers=headers)
        fetched = client.get(
            f"/api/v1/devices/{created.json()['id']}",
            headers=headers,
        )

    assert created.status_code == 201
    assert created.json()["name"] == payload["name"]
    assert listed.status_code == 200
    assert len(listed.json()) == 1
    assert fetched.status_code == 200
    assert fetched.json()["id"] == created.json()["id"]


def test_device_enrollment_is_idempotent_across_application_restart() -> None:
    payload = _device_payload()
    headers = _auth_headers()

    with TestClient(create_app(control_plane_token=CONTROL_PLANE_TOKEN)) as client:
        created = client.post("/api/v1/devices", json=payload, headers=headers)
    with TestClient(create_app(control_plane_token=CONTROL_PLANE_TOKEN)) as client:
        repeated = client.post("/api/v1/devices", json=payload, headers=headers)
        listed = client.get("/api/v1/devices", headers=headers)

    assert created.status_code == 201
    assert repeated.status_code == 200
    assert repeated.json()["id"] == created.json()["id"]
    assert [device["id"] for device in listed.json()] == [created.json()["id"]]


def test_conflicting_reenrollment_does_not_mutate_existing_device() -> None:
    payload = _device_payload()
    headers = _auth_headers()

    with TestClient(create_app(control_plane_token=CONTROL_PLANE_TOKEN)) as client:
        created = client.post("/api/v1/devices", json=payload, headers=headers)
        conflict_payload = {**payload, "name": "Different Camera"}
        conflict = client.post("/api/v1/devices", json=conflict_payload, headers=headers)
        listed = client.get("/api/v1/devices", headers=headers)

    assert created.status_code == 201
    assert conflict.status_code == 409
    assert conflict.json() == {"detail": "Device endpoint already enrolled with different metadata"}
    assert listed.json() == [created.json()]


def test_device_state_is_isolated_by_authenticated_site(tmp_path) -> None:
    database = tmp_path / "shared-devices.sqlite3"
    payload = _device_payload()

    with TestClient(
        create_app(
            control_plane_token="site-a-token",
            control_plane_site_id="site-a",
            device_db_path=database,
        )
    ) as client:
        site_a = client.post(
            "/api/v1/devices",
            json=payload,
            headers=_auth_headers("site-a-token"),
        )
    with TestClient(
        create_app(
            control_plane_token="site-b-token",
            control_plane_site_id="site-b",
            device_db_path=database,
        )
    ) as client:
        site_b_list = client.get("/api/v1/devices", headers=_auth_headers("site-b-token"))
        site_b_get = client.get(
            f"/api/v1/devices/{site_a.json()['id']}",
            headers=_auth_headers("site-b-token"),
        )
        site_b = client.post(
            "/api/v1/devices",
            json=payload,
            headers=_auth_headers("site-b-token"),
        )
    with TestClient(
        create_app(
            control_plane_token="site-a-token",
            control_plane_site_id="site-a",
            device_db_path=database,
        )
    ) as client:
        site_a_list = client.get("/api/v1/devices", headers=_auth_headers("site-a-token"))

    assert site_a.status_code == 201
    assert site_b_list.status_code == 200
    assert site_b_list.json() == []
    assert site_b_get.status_code == 404
    assert site_b.status_code == 201
    assert site_b.json()["id"] != site_a.json()["id"]
    assert [device["id"] for device in site_a_list.json()] == [site_a.json()["id"]]


def test_unknown_device_returns_404() -> None:
    with TestClient(create_app(control_plane_token=CONTROL_PLANE_TOKEN)) as client:
        response = client.get(
            "/api/v1/devices/00000000-0000-0000-0000-000000000000",
            headers=_auth_headers(),
        )

    assert response.status_code == 404


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v1/devices"),
        ("POST", "/api/v1/devices"),
        ("GET", "/api/v1/devices/00000000-0000-0000-0000-000000000000"),
    ],
)
@pytest.mark.parametrize(
    "headers",
    [
        [(b"authorization", b"Bearer invalid-\xff")],
        [
            (b"authorization", b"Bearer test-control-plane-token"),
            (b"authorization", b"Bearer wrong-token"),
        ],
        [
            (b"authorization", b"Bearer wrong-token"),
            (b"authorization", b"Bearer test-control-plane-token"),
        ],
        [
            (b"authorization", b"Bearer test-control-plane-token"),
            (b"authorization", b"Bearer test-control-plane-token"),
        ],
    ],
    ids=["non-ascii", "multiple-first-valid", "multiple-last-valid", "multiple-identical"],
)
def test_device_api_rejects_ambiguous_credentials(
    method: str, path: str, headers: list[tuple[bytes, bytes]]
) -> None:
    application = create_app(control_plane_token=CONTROL_PLANE_TOKEN)
    with TestClient(application, raise_server_exceptions=False) as client:
        response = client.request(
            method,
            path,
            headers=headers,
            json=_device_payload(),
        )
        remaining = client.get("/api/v1/devices", headers=_auth_headers())
        health = client.get("/api/v1/health")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {"detail": "Unauthorized"}
    assert remaining.status_code == 200
    assert remaining.json() == []
    assert health.status_code == 200


@pytest.mark.parametrize("from_environment", [False, True])
def test_device_api_rejects_unusable_token_configuration(
    monkeypatch: pytest.MonkeyPatch, from_environment: bool
) -> None:
    if from_environment:
        monkeypatch.setenv(CONTROL_PLANE_TOKEN_ENV, "invalid-\u00e9")
        application = create_app()
    else:
        application = create_app(control_plane_token="invalid-\u00e9")

    with TestClient(application, raise_server_exceptions=False) as client:
        response = client.get("/api/v1/devices", headers=_auth_headers())
        health = client.get("/api/v1/health")

    assert response.status_code == 503
    assert response.json() == {"detail": "Control-plane authentication is not configured"}
    assert health.status_code == 200


def test_device_api_accepts_configured_environment_token(monkeypatch) -> None:
    monkeypatch.setenv(CONTROL_PLANE_TOKEN_ENV, CONTROL_PLANE_TOKEN)
    with TestClient(create_app()) as client:
        response = client.get(
            "/api/v1/devices", headers={"Authorization": f"bEaReR {CONTROL_PLANE_TOKEN}"}
        )

    assert response.status_code == 200
    assert response.json() == []


def test_device_api_explicit_blank_token_does_not_fall_back_to_environment(monkeypatch) -> None:
    monkeypatch.setenv(CONTROL_PLANE_TOKEN_ENV, CONTROL_PLANE_TOKEN)
    with TestClient(create_app(control_plane_token=" ")) as client:
        response = client.get("/api/v1/devices", headers=_auth_headers())

    assert response.status_code == 503


def test_device_registration_rejects_unexpected_and_oversized_fields_without_mutation() -> None:
    headers = _auth_headers()
    unexpected = _device_payload()
    unexpected["unexpected"] = "ignored-before-hardening"
    overlong_tag = _device_payload()
    overlong_tag["tags"] = ["x" * 65]
    too_many_tags = _device_payload()
    too_many_tags["tags"] = [f"tag-{index}" for index in range(33)]

    with TestClient(create_app(control_plane_token=CONTROL_PLANE_TOKEN)) as client:
        unexpected_response = client.post("/api/v1/devices", json=unexpected, headers=headers)
        overlong_response = client.post("/api/v1/devices", json=overlong_tag, headers=headers)
        too_many_response = client.post("/api/v1/devices", json=too_many_tags, headers=headers)
        remaining = client.get("/api/v1/devices", headers=headers)

    assert unexpected_response.status_code == 422
    assert overlong_response.status_code == 422
    assert too_many_response.status_code == 422
    assert remaining.status_code == 200
    assert remaining.json() == []


def test_device_registration_accepts_maximum_bounded_tags() -> None:
    payload = _device_payload()
    payload["tags"] = [f"tag-{index:02d}-" + "x" * 57 for index in range(32)]

    with TestClient(create_app(control_plane_token=CONTROL_PLANE_TOKEN)) as client:
        response = client.post("/api/v1/devices", json=payload, headers=_auth_headers())

    assert response.status_code == 201
    assert len(response.json()["tags"]) == 32
    assert max(len(tag) for tag in response.json()["tags"]) == 64


def test_device_registration_body_is_bounded_before_model_parsing() -> None:
    payload = _device_payload()
    payload["tags"] = ["x" * (MAX_DEVICE_REQUEST_BYTES + 1)]
    encoded = json.dumps(payload).encode("utf-8")
    assert len(encoded) > MAX_DEVICE_REQUEST_BYTES

    with TestClient(create_app(control_plane_token=CONTROL_PLANE_TOKEN)) as client:
        response = client.post(
            "/api/v1/devices",
            content=encoded,
            headers={**_auth_headers(), "Content-Type": "application/json"},
        )
        remaining = client.get("/api/v1/devices", headers=_auth_headers())

    assert response.status_code == 413
    assert response.json() == {"detail": "Request body too large"}
    assert remaining.status_code == 200
    assert remaining.json() == []


def test_device_registry_capacity_rejects_extra_write_without_mutation() -> None:
    headers = _auth_headers()
    with TestClient(
        create_app(control_plane_token=CONTROL_PLANE_TOKEN, device_capacity=2)
    ) as client:
        first = client.post("/api/v1/devices", json=_device_payload(1), headers=headers)
        second = client.post("/api/v1/devices", json=_device_payload(2), headers=headers)
        rejected = client.post("/api/v1/devices", json=_device_payload(3), headers=headers)
        remaining = client.get("/api/v1/devices", headers=headers)

    assert first.status_code == 201
    assert second.status_code == 201
    assert rejected.status_code == 409
    assert rejected.json() == {"detail": "Device registry capacity reached"}
    assert [device["name"] for device in remaining.json()] == [
        "Synthetic Camera 1",
        "Synthetic Camera 2",
    ]


def test_device_list_paginates_and_rejects_unbounded_queries() -> None:
    headers = _auth_headers()
    with TestClient(create_app(control_plane_token=CONTROL_PLANE_TOKEN)) as client:
        for index in range(1, 4):
            response = client.post("/api/v1/devices", json=_device_payload(index), headers=headers)
            assert response.status_code == 201

        page = client.get("/api/v1/devices?offset=1&limit=2", headers=headers)
        over_limit = client.get("/api/v1/devices?limit=101", headers=headers)
        negative_offset = client.get("/api/v1/devices?offset=-1", headers=headers)

    assert page.status_code == 200
    assert [device["name"] for device in page.json()] == [
        "Synthetic Camera 2",
        "Synthetic Camera 3",
    ]
    assert over_limit.status_code == 422
    assert negative_offset.status_code == 422
