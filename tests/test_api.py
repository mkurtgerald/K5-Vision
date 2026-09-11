from fastapi.testclient import TestClient

from k5vision.main import create_app


def test_health_reports_version() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["version"] == "0.1.0"


def test_device_registration_round_trip() -> None:
    payload = {
        "name": "Gate Camera 01",
        "host": "192.168.10.21",
        "kind": "camera",
        "protocols": ["onvif", "rtsp"],
        "tags": ["gate", "perimeter"],
    }

    with TestClient(create_app()) as client:
        created = client.post("/api/v1/devices", json=payload)
        listed = client.get("/api/v1/devices")
        fetched = client.get(f"/api/v1/devices/{created.json()['id']}")

    assert created.status_code == 201
    assert created.json()["name"] == payload["name"]
    assert listed.status_code == 200
    assert len(listed.json()) == 1
    assert fetched.status_code == 200
    assert fetched.json()["id"] == created.json()["id"]


def test_unknown_device_returns_404() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/api/v1/devices/00000000-0000-0000-0000-000000000000")

    assert response.status_code == 404
