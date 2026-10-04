import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from k5vision import analytics_config, cli


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


def _assert_preflight_output(capsys, *, enabled, status):
    captured = capsys.readouterr()
    expected = {"schema_version": "1", "analytics_enabled": enabled, "status": status}
    assert captured.out == json.dumps(expected, separators=(",", ":")) + "\n"
    assert captured.err == ""
    assert len(captured.out) < 128


def _forbid_service_start(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("analytics preflight must not start service or identity setup")

    for name in ("setup_administrator", "load_identity_state", "identity_environment"):
        monkeypatch.setattr(cli, name, unexpected)
    monkeypatch.setattr(cli.uvicorn, "run", unexpected)


def test_analytics_preflight_default_disabled_without_optional_runtime(monkeypatch, capsys):
    _forbid_service_start(monkeypatch)
    monkeypatch.delenv(analytics_config.ANALYTICS_CONFIG_ENV, raising=False)

    def unexpected(*args, **kwargs):
        pytest.fail("disabled analytics must not inspect files or optional packages")

    monkeypatch.setattr(analytics_config, "_local_path", unexpected)
    monkeypatch.setattr(analytics_config, "validate_analytics_runtime", unexpected)
    assert cli.main(["analytics-preflight"]) == 0
    _assert_preflight_output(capsys, enabled=False, status="disabled")


@pytest.fixture
def analytics_input(tmp_path, monkeypatch):
    models = tmp_path / "models"
    models.mkdir()
    path = tmp_path / "private-analytics-config.json"
    document = {
        "schema_version": 1,
        "provider": analytics_config.PROVIDER,
        "source_revision": analytics_config.ANALYTICS_REVISION,
        "artifact_root": str(models),
    }
    path.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setenv(analytics_config.ANALYTICS_CONFIG_ENV, str(path))
    return path, models, document


def test_analytics_preflight_ready_only_after_existing_runtime_admission(
    analytics_input, monkeypatch, capsys
):
    _forbid_service_start(monkeypatch)
    path, models, _ = analytics_input
    original = path.read_bytes()
    admitted = []
    monkeypatch.setattr(analytics_config, "validate_analytics_runtime", admitted.append)

    assert cli.main(["analytics-preflight"]) == 0
    assert admitted == [analytics_config.AnalyticsConfiguration(models)]
    assert path.read_bytes() == original
    assert list(models.iterdir()) == []
    _assert_preflight_output(capsys, enabled=True, status="ready")


@pytest.mark.parametrize(
    "value", ["", "relative-private-config", "https://private.example/config", "//private/config"]
)
def test_analytics_preflight_invalid_explicit_selection_refused(monkeypatch, capsys, value):
    _forbid_service_start(monkeypatch)
    monkeypatch.setenv(analytics_config.ANALYTICS_CONFIG_ENV, value)
    assert cli.main(["analytics-preflight"]) == 1
    _assert_preflight_output(capsys, enabled=False, status="refused")


@pytest.mark.parametrize("failure", ["missing", "malformed", "duplicate", "oversized", "revision"])
def test_analytics_preflight_config_refusal_is_fixed_and_read_only(
    analytics_input, monkeypatch, capsys, failure
):
    _forbid_service_start(monkeypatch)
    path, models, document = analytics_input
    if failure == "missing":
        path.unlink()
    elif failure == "malformed":
        path.write_text("private-invalid-json", encoding="utf-8")
    elif failure == "duplicate":
        path.write_text('{"private-key":1,"private-key":2}', encoding="utf-8")
    elif failure == "oversized":
        path.write_text("private" * 1000, encoding="utf-8")
    else:
        document["source_revision"] = "unreviewed-private-revision"
        path.write_text(json.dumps(document), encoding="utf-8")
    original = path.read_bytes() if path.exists() else None

    assert cli.main(["analytics-preflight"]) == 1
    assert (path.read_bytes() if path.exists() else None) == original
    assert list(models.iterdir()) == []
    _assert_preflight_output(capsys, enabled=False, status="refused")


@pytest.mark.parametrize("error_type", [analytics_config.AnalyticsConfigurationError, RuntimeError])
def test_analytics_preflight_runtime_refusal_does_not_leak_diagnostic(
    analytics_input, monkeypatch, capsys, error_type
):
    _forbid_service_start(monkeypatch)

    def reject(_):
        raise error_type("private-path-and-runtime-detail")

    monkeypatch.setattr(analytics_config, "validate_analytics_runtime", reject)
    assert cli.main(["analytics-preflight"]) == 1
    _assert_preflight_output(capsys, enabled=False, status="refused")


@pytest.mark.parametrize("selection", [None, "", "missing", "malformed", "runtime-missing"])
def test_analytics_preflight_source_subprocess_is_camera_free(tmp_path, selection):
    # This deliberately remains a source test; release acceptance must repeat it
    # with the exact built wheel installed in an isolated interpreter.
    source = Path(__file__).resolve().parents[1] / "src"
    # A clean consumer must not inherit parent coverage hooks or credentials.
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}
    }
    if selection is not None:
        path = tmp_path / "private-config.json"
        if selection == "malformed":
            path.write_text("private-invalid-json", encoding="utf-8")
        elif selection == "runtime-missing":
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "provider": analytics_config.PROVIDER,
                        "source_revision": analytics_config.ANALYTICS_REVISION,
                        "artifact_root": str(tmp_path),
                    }
                ),
                encoding="utf-8",
            )
        environment[analytics_config.ANALYTICS_CONFIG_ENV] = "" if selection == "" else str(path)
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    script = """
import atexit
import importlib.abc
import os
import sys

attempts = []
blocked = {"openvino", "cv2", "numpy", "k5vision.main", "k5vision.stage_one_app"}
if "K5_ANALYTICS_CONFIG" not in os.environ:
    blocked.add("analytics_lab")
class CameraFree(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + ".") for name in blocked):
            attempts.append(fullname)
            raise RuntimeError("forbidden application or native import")
sys.meta_path.insert(0, CameraFree())
def audit(event, args):
    if event in {"sqlite3.connect", "socket.connect", "subprocess.Popen", "os.system"}:
        attempts.append(event)
        raise RuntimeError("forbidden preflight side effect")
sys.addaudithook(audit)
def verify():
    assert not attempts, "preflight attempted forbidden imports or side effects"
atexit.register(verify)
sys.path.insert(0, sys.argv[1])
from k5vision.cli import main
raise SystemExit(main(["analytics-preflight"]))
"""
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script, str(source)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    expected_status = "disabled" if selection is None else "refused"
    assert result.returncode == (0 if selection is None else 1)
    assert result.stdout == (
        '{"schema_version":"1","analytics_enabled":false,"status":"' + expected_status + '"}\n'
    )
    assert result.stderr == ""
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == before
