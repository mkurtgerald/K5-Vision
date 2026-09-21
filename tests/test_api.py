import pytest
from fastapi.testclient import TestClient

from k5vision.main import CONTROL_PLANE_TOKEN_ENV, create_app

CONTROL_PLANE_TOKEN = "test-control-plane-token"


def _auth_headers(token: str = CONTROL_PLANE_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


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
        created = client.post(
            "/api/v1/devices",
            json={
                "name": "Synthetic Camera",
                "host": "192.0.2.10",
                "kind": "camera",
                "protocols": ["rtsp"],
                "tags": [],
            },
        )

    assert listed.status_code == 503
    assert created.status_code == 503


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
            json={
                "name": "Synthetic Camera",
                "host": "192.0.2.10",
                "kind": "camera",
                "protocols": ["rtsp"],
                "tags": [],
            },
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
