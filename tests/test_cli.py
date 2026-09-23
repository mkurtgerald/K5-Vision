import pytest

from k5vision import cli


def _capture_run(monkeypatch) -> dict[str, object]:
    invocation: dict[str, object] = {}

    def fake_run(application: str, **kwargs: object) -> None:
        invocation["application"] = application
        invocation.update(kwargs)

    monkeypatch.setattr(cli.uvicorn, "run", fake_run)
    return invocation


def test_installed_cli_serves_local_control_plane_by_default(monkeypatch):
    invocation = _capture_run(monkeypatch)

    assert cli.main(["serve"]) == 0
    assert invocation == {
        "application": "k5vision.main:app",
        "factory": False,
        "host": "127.0.0.1",
        "port": 8000,
        "log_level": "info",
        "workers": 1,
    }


def test_installed_cli_can_select_existing_stage_one_operator_factory(monkeypatch):
    invocation = _capture_run(monkeypatch)

    assert cli.main(["serve", "--operator"]) == 0
    assert invocation == {
        "application": "k5vision.stage_one_app:create_stage_one_app",
        "factory": True,
        "host": "127.0.0.1",
        "port": 8000,
        "log_level": "info",
        "workers": 1,
    }


def test_operator_startup_preserves_explicit_service_options(monkeypatch):
    invocation = _capture_run(monkeypatch)

    assert cli.main(["serve", "--operator", "--port", "9000", "--log-level", "warning"]) == 0
    assert invocation["application"] == "k5vision.stage_one_app:create_stage_one_app"
    assert invocation["factory"] is True
    assert invocation["host"] == "127.0.0.1"
    assert invocation["port"] == 9000
    assert invocation["log_level"] == "warning"
    assert invocation["workers"] == 1


@pytest.mark.parametrize("port", ["0", "65536", "not-a-port"])
def test_operator_startup_rejects_invalid_port(monkeypatch, port):
    def unexpected_run(*args, **kwargs):
        pytest.fail("invalid command must not start the application")

    monkeypatch.setattr(cli.uvicorn, "run", unexpected_run)

    with pytest.raises(SystemExit) as caught:
        cli.main(["serve", "--operator", "--port", port])

    assert caught.value.code == 2
