"""Fresh-process ownership checks for factory imports and the public ASGI app."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_SETUP = """
import importlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from fastapi import FastAPI
from fastapi.testclient import TestClient
from uvicorn.importer import import_from_string

connections = []
connect = sqlite3.connect
def record_connect(*args, **kwargs):
    connection = connect(*args, **kwargs)
    connections.append(connection)
    return connection
sqlite3.connect = record_connect

def open_count():
    count = 0
    for connection in connections:
        try:
            connection.execute('SELECT 1')
        except sqlite3.ProgrammingError:
            pass
        else:
            count += 1
    return count
"""


def test_existing_windows_smoke_qualifies_exact_app_lifetime_paths() -> None:
    workflow = (
        Path(__file__).resolve().parents[1] / ".github/workflows/windows-alpha-script-smoke.yml"
    ).read_text()
    triggers = workflow.split("permissions:", 1)[0]
    for path in (
        "src/k5vision/main.py",
        "src/k5vision/stage_one_app.py",
        "tests/test_app_resource_lifetime.py",
    ):
        assert triggers.count(f'- "{path}"') == 2
    regression = workflow.split("- name: Run Windows alpha boundary regressions", 1)[1]
    regression = regression.split("- name:", 1)[0]
    for path in (
        "tests/test_one_file_windows_bootstrap.py",
        "tests/test_windows_alpha_bootstrap.py",
        "tests/test_public_test_operator_runtime.py",
        "tests/test_windows_alpha_direct_witness.py",
        "tests/test_app_resource_lifetime.py",
    ):
        assert regression.count(path) == 1
    assert "runs-on: windows-latest" in workflow
    assert workflow.count("runs-on:") == 1
    assert "permissions:\n  contents: read" in workflow
    assert "--no-cov" in regression


def _fresh_process(tmp_path: Path, script: str) -> None:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}
    }
    environment.update(
        PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"),
        K5_CONTROL_PLANE_SITE_ID="disposable-resource-lifetime",
        K5_DEVICE_DB_PATH=str(tmp_path / "devices.sqlite3"),
        K5_USER_DB_PATH=str(tmp_path / "users.sqlite3"),
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", _SETUP + script],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "resource-lifetime-ok"


@pytest.mark.parametrize("entrypoint", ["main", "stage_one"])
@pytest.mark.parametrize("interrupted", [False, True])
def test_explicit_factory_owns_only_requested_registries_and_closes_lifespan(
    tmp_path: Path, entrypoint: str, interrupted: bool
) -> None:
    factory_import = (
        "from k5vision.main import create_app as factory"
        if entrypoint == "main"
        else "from k5vision.stage_one_app import create_stage_one_app as factory"
    )
    _fresh_process(
        tmp_path,
        f"""
{factory_import}
assert len(connections) == 0, 'factory import opened unowned registries'
for attempt in range(2):
    application = factory()
    assert len(connections) == 2 * (attempt + 1)
    assert open_count() == 2
    try:
        with TestClient(application) as client:
            assert client.get('/api/v1/health').status_code == 200
            if {interrupted!r}:
                raise RuntimeError('disposable interruption')
    except RuntimeError as error:
        assert {interrupted!r} and str(error) == 'disposable interruption'
    assert open_count() == 0, 'lifespan left a registry handle open'
import k5vision.main as main
assert 'app' not in vars(main), 'explicit factory created an unused default app'
print('resource-lifetime-ok')
""",
    )


def test_public_asgi_attribute_from_import_and_reload_share_one_owned_app(tmp_path: Path) -> None:
    _fresh_process(
        tmp_path,
        """
import k5vision.main as main
assert len(connections) == 0
assert 'app' in dir(main)
for _ in range(2):
    main = importlib.reload(main)
    assert len(connections) == 0, 'reload instantiated the default app'
from k5vision.main import app
assert isinstance(app, FastAPI)
assert len(connections) == 2
assert main.app is app
assert import_from_string('k5vision.main:app') is app
assert importlib.import_module('k5vision.main').app is app
assert importlib.reload(main).app is app
assert len(connections) == 2, 'reload replaced or reopened the owned default app'
with TestClient(app) as client:
    assert client.get('/api/v1/health').status_code == 200
assert open_count() == 0
assert main.app is app
assert len(connections) == 2, 'attribute lookup reopened a closed cached app'
try:
    main.unknown_resource_attribute
except AttributeError:
    pass
else:
    raise AssertionError('unknown module attribute was accepted')
assert len(connections) == 2
print('resource-lifetime-ok')
""",
    )


def test_concurrent_public_asgi_lookup_constructs_only_one_registry_pair(tmp_path: Path) -> None:
    _fresh_process(
        tmp_path,
        """
import k5vision.main as main
assert len(connections) == 0
with ThreadPoolExecutor(max_workers=8) as executor:
    applications = list(executor.map(lambda _: getattr(main, 'app'), range(32)))
assert all(application is applications[0] for application in applications)
assert len(connections) == 2
with TestClient(applications[0]) as client:
    assert client.get('/api/v1/health').status_code == 200
assert open_count() == 0
print('resource-lifetime-ok')
""",
    )


def test_failed_lazy_construction_does_not_cache_an_invalid_default_app(tmp_path: Path) -> None:
    _fresh_process(
        tmp_path,
        """
import k5vision.main as main
assert len(connections) == 0
factory = main.create_app
def refuse():
    raise ValueError('disposable construction failure')
main.create_app = refuse
try:
    main.app
except ValueError:
    pass
else:
    raise AssertionError('failed construction was accepted')
assert 'app' not in vars(main)
assert len(connections) == 0
main.create_app = factory
app = main.app
assert len(connections) == 2
with TestClient(app):
    pass
assert open_count() == 0
print('resource-lifetime-ok')
""",
    )
