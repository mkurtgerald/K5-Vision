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
