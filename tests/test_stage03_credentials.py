import os

import pytest

from k5vision import stage03_credential_probe, stage03_credentials, stage03_gst_candidate


def test_parse_cam_cred_ignores_blank_lines_and_preserves_bang() -> None:
    username, passwords = stage03_credentials.parse_cam_cred("viewer\n\n\n12345\n\n\n67890!\n")

    assert username == "viewer"
    assert passwords == ("12345", "67890!")


def test_credentialized_uri_replaces_stale_userinfo_and_preserves_bang() -> None:
    uri = stage03_credentials.credentialized_uri(
        "rtsp://old:wrong@192.0.2.10:554/stream1",
        "viewer",
        "12! 34",
    )

    assert uri == "rtsp://viewer:12!%2034@192.0.2.10:554/stream1"
    assert "old:wrong" not in uri


def test_selected_source_uri_rejects_invalid_index() -> None:
    with pytest.raises(ValueError):
        stage03_credentials.selected_source_uri(
            "rtsp://192.0.2.10/stream1",
            "viewer\n\none\n\ntwo!",
            2,
        )


def test_probe_returns_only_working_index(monkeypatch) -> None:
    seen: list[str] = []

    def fake_run(_transport: str, uri: str) -> int:
        seen.append(uri)
        return 41 if len(seen) == 1 else 0

    monkeypatch.setattr(stage03_credential_probe, "run_gst_uri", fake_run)

    index = stage03_credential_probe.resolve_credential_index(
        "rtsp://old:stale@192.0.2.10/stream1",
        "viewer\n\nfirst\n\nsecond!",
    )

    assert index == 1
    assert len(seen) == 2
    assert all("old:stale" not in uri for uri in seen)


def test_wrapper_applies_selected_credential_without_logging(monkeypatch) -> None:
    captured: dict[str, str] = {}
    monkeypatch.setenv("K5_STAGE03_CAM_CRED", "viewer\n\nfirst\n\nsecond!")
    monkeypatch.setenv("K5_STAGE03_CREDENTIAL_INDEX", "1")

    def fake_run(transport: str, uri: str) -> int:
        captured["transport"] = transport
        captured["uri"] = uri
        return 0

    monkeypatch.setattr(stage03_gst_candidate, "run_gst_uri", fake_run)

    result = stage03_gst_candidate.run_candidate(
        "tcp",
        "rtsp://old:stale@192.0.2.10/stream1",
    )

    assert result == 0
    assert captured["transport"] == "tcp"
    assert captured["uri"] == "rtsp://viewer:second!@192.0.2.10/stream1"
    assert os.getenv("K5_STAGE03_CREDENTIAL_INDEX") == "1"
