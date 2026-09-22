from k5vision import cli


def test_installed_cli_serves_local_control_plane_by_default(monkeypatch):
    invocation: dict[str, object] = {}

    def fake_run(application: str, **kwargs: object) -> None:
        invocation["application"] = application
        invocation.update(kwargs)

    monkeypatch.setattr(cli.uvicorn, "run", fake_run)

    assert cli.main(["serve"]) == 0
    assert invocation == {
        "application": "k5vision.main:app",
        "host": "127.0.0.1",
        "port": 8000,
        "log_level": "info",
        "workers": 1,
    }
