"""Source-only installed composition tests; identity/database/runtime calls are mocked."""

from __future__ import annotations

import os
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest.mock import Mock

import pytest

from k5vision import identity_state, stage_one_app


@pytest.fixture
def profile_module():
    from k5vision import installed_profile

    return installed_profile


@pytest.fixture
def layout(tmp_path, monkeypatch, profile_module):
    app = tmp_path / "application"
    state = tmp_path / "private-state"
    identity = state / "identity"
    recordings = state / "recordings"
    for directory in (app, state, identity, recordings):
        directory.mkdir(mode=0o700)
    database = state / "devices.sqlite3"
    database.write_bytes(b"mocked-device-database")
    identity_value = identity_state.IdentityState(identity, "install-fixture-site")
    reader = Mock(return_value=identity_value)
    monkeypatch.setattr(profile_module, "load_identity_state", reader)
    values = {
        "application_root": app,
        "identity_directory": identity,
        "device_database": database,
        "recording_root": recordings,
    }
    return values, reader


def test_same_explicit_paths_and_identity_survive_composition_restart(profile_module, layout):
    values, reader = layout
    media = values["recording_root"] / "existing.k5r"
    media.write_bytes(b"existing-private-media-fixture")
    before = values["device_database"].read_bytes()
    first = profile_module.load_installed_profile(**values)
    second = profile_module.load_installed_profile(**values)
    assert first == second
    assert first.identity.directory == values["identity_directory"]
    assert first.device_database == values["device_database"]
    assert first.recording_root == values["recording_root"]
    assert reader.call_count == 2
    assert values["device_database"].read_bytes() == before
    assert list(values["recording_root"].iterdir()) == [media]
    assert media.read_bytes() == b"existing-private-media-fixture"


@pytest.mark.parametrize(
    "field", ["application_root", "identity_directory", "device_database", "recording_root"]
)
@pytest.mark.parametrize(
    "value", ["relative", ":memory:", "../state", "//network/share", "\\\\server\\share", ""]
)
def test_refuse_nonlocal_or_implicit_paths_before_identity_read(
    profile_module, layout, field, value
):
    values, reader = layout
    with pytest.raises(profile_module.InstalledProfileError):
        profile_module.load_installed_profile(**(values | {field: value}))
    reader.assert_not_called()


@pytest.mark.parametrize("field", ["identity_directory", "device_database", "recording_root"])
def test_refuse_state_inside_replaceable_application(profile_module, layout, field):
    values, reader = layout
    candidate = values["application_root"] / "replaceable"
    if field == "device_database":
        candidate.write_bytes(b"fixture")
    else:
        candidate.mkdir(mode=0o700)
    with pytest.raises(profile_module.InstalledProfileError):
        profile_module.load_installed_profile(**(values | {field: candidate}))
    reader.assert_not_called()


@pytest.mark.parametrize("field", ["identity_directory", "device_database", "recording_root"])
def test_missing_persistent_state_is_refused_without_recreation(profile_module, layout, field):
    values, reader = layout
    missing = values["device_database"].parent / "missing"
    with pytest.raises(profile_module.InstalledProfileError):
        profile_module.load_installed_profile(**(values | {field: missing}))
    assert not missing.exists()
    reader.assert_not_called()


@pytest.mark.parametrize(
    "field,target",
    [
        ("recording_root", "identity_directory"),
        ("device_database", "identity_directory"),
        ("identity_directory", "recording_root"),
    ],
)
def test_overlapping_identity_device_and_media_are_refused(profile_module, layout, field, target):
    values, reader = layout
    candidate = values[target]
    if field == "device_database":
        candidate = candidate / "devices.sqlite3"
        candidate.write_bytes(b"fixture")
    with pytest.raises(profile_module.InstalledProfileError):
        profile_module.load_installed_profile(**(values | {field: candidate}))
    reader.assert_not_called()


def test_identity_failure_is_sanitized_and_never_repaired(profile_module, layout):
    values, reader = layout
    reader.side_effect = identity_state.IdentityStateError("private-marker")
    with pytest.raises(profile_module.InstalledProfileError) as caught:
        profile_module.load_installed_profile(**values)
    assert "private-marker" not in str(caught.value)
    assert list(values["identity_directory"].iterdir()) == []


@pytest.mark.parametrize("field", ["identity_directory", "device_database", "recording_root"])
def test_linked_state_is_refused_before_identity_admission(profile_module, layout, field):
    values, reader = layout
    link = values["device_database"].parent / "linked-state"
    link.symlink_to(values[field], target_is_directory=field != "device_database")
    with pytest.raises(profile_module.InstalledProfileError):
        profile_module.load_installed_profile(**(values | {field: link}))
    reader.assert_not_called()


@pytest.mark.parametrize("suffix", ["", "-journal", "-wal", "-shm"])
def test_hardlinked_database_or_sidecar_is_refused(profile_module, layout, suffix):
    values, reader = layout
    database = values["device_database"]
    if suffix:
        candidate = Path(str(database) + suffix)
        candidate.write_bytes(b"fixture-sidecar")
    else:
        candidate = database
    os.link(candidate, database.parent / "extra-link")
    with pytest.raises(profile_module.InstalledProfileError):
        profile_module.load_installed_profile(**values)
    reader.assert_not_called()


def test_admission_is_read_only_and_never_scans_or_deletes_media(
    monkeypatch, profile_module, layout
):
    values, _ = layout
    unexpected = Mock(side_effect=AssertionError("admission must be read-only"))
    for name in ("mkdir", "unlink", "rename", "replace", "write_text", "write_bytes", "glob"):
        monkeypatch.setattr(Path, name, unexpected)
    profile_module.load_installed_profile(**values)
    unexpected.assert_not_called()


def test_runtime_environment_and_package_directories_are_independently_excluded(
    monkeypatch, profile_module, layout
):
    values, reader = layout
    monkeypatch.setattr(profile_module.sys, "prefix", str(values["identity_directory"]))
    with pytest.raises(profile_module.InstalledProfileError):
        profile_module.load_installed_profile(**values)
    reader.assert_not_called()


@pytest.mark.parametrize("root", ["python_environment", "package_directory"])
def test_trusted_runtime_alias_cannot_hide_real_persistent_path(
    monkeypatch, profile_module, layout, root
):
    values, reader = layout
    real_runtime = values["device_database"].parent
    alias = values["application_root"].parent / "runtime-alias"
    alias.symlink_to(real_runtime, target_is_directory=True)
    if root == "python_environment":
        monkeypatch.setattr(profile_module.sys, "prefix", str(alias))
    else:
        (real_runtime / "installed_profile.py").write_text("# trusted-root fixture\n")
        monkeypatch.setattr(profile_module, "__file__", str(alias / "installed_profile.py"))
    # Every supplied persistent path is an ordinary real path. Only the trusted
    # exclusion root is aliased; lexical comparison alone must not admit state.
    with pytest.raises(profile_module.InstalledProfileError):
        profile_module.load_installed_profile(**values)
    reader.assert_not_called()


def test_installed_environment_restores_exact_values_even_after_failure(profile_module, layout):
    values, _ = layout
    profile = profile_module.load_installed_profile(**values)
    environment = {"unrelated": "preserved", "K5_DEVICE_DB_PATH": ""}
    before = dict(environment)
    with pytest.raises(RuntimeError):
        with profile_module.installed_profile_environment(profile, environment):
            assert environment["K5_CONTROL_PLANE_SITE_ID"] == profile.identity.site_id
            assert environment["K5_USER_DB_PATH"] == str(profile.identity.database_path)
            assert environment["K5_DEVICE_DB_PATH"] == str(profile.device_database)
            assert environment["K5_STAGE_ONE_RECORDING_ROOT"] == str(profile.recording_root)
            raise RuntimeError("fixture")
    assert environment == before


@pytest.mark.parametrize(
    "name",
    [
        "K5_STAGE03_SOURCE",
        "K5_STAGE03_CAM_CRED",
        "K5_PUBLIC_TEST_RTSP_SOURCE",
        "K5_PUBLIC_TEST_SOURCE_IP",
        "K5_LOCAL_TEST_RTSP_SOURCE",
        "K5_DEVICE_DB_PATH",
        "K5_STAGE_ONE_RECORDING_ROOT",
        "K5_CONTROL_PLANE_SITE_ID",
        "K5_USER_DB_PATH",
    ],
)
def test_environment_conflicts_are_refused_without_mutation(profile_module, layout, name):
    values, _ = layout
    profile = profile_module.load_installed_profile(**values)
    environment = {name: "private-marker"}
    before = dict(environment)
    with pytest.raises(profile_module.InstalledProfileError) as caught:
        with profile_module.installed_profile_environment(profile, environment):
            pytest.fail("conflicting environment was accepted")
    assert environment == before
    assert "private-marker" not in str(caught.value)


def test_matching_explicit_environment_is_preserved(profile_module, layout):
    values, _ = layout
    profile = profile_module.load_installed_profile(**values)
    environment = {
        "K5_DEVICE_DB_PATH": str(profile.device_database),
        "K5_STAGE_ONE_RECORDING_ROOT": str(profile.recording_root),
        "K5_CONTROL_PLANE_SITE_ID": profile.identity.site_id,
        "K5_USER_DB_PATH": str(profile.identity.database_path),
        "ordinary-other-option": "preserved",
    }
    before = dict(environment)
    with profile_module.installed_profile_environment(profile, environment):
        assert environment == before
    assert environment == before


def _runtime():
    return SimpleNamespace(resolve=Mock()), SimpleNamespace(run=Mock())


def _application():
    names = (
        "user_registry",
        "device_registry",
        "operator_launch_coordinator",
        "operator_recording_coordinator",
        "operator_playback_coordinator",
        "operator_playback_timeline",
        "operator_export_coordinator",
        "operator_recording_catalog",
    )
    return SimpleNamespace(state=SimpleNamespace(**{name: Mock() for name in names}))


def test_internal_factory_requires_explicit_runtime_before_any_profile_or_app_read(monkeypatch):
    load = Mock(side_effect=AssertionError("profile read"))
    create = Mock(side_effect=AssertionError("application construction"))
    monkeypatch.setattr(stage_one_app, "create_app", create)
    # Missing runtime must not be hidden as an apparently running installed mode.
    factory = stage_one_app._create_installed_operator_app
    from k5vision import installed_profile

    monkeypatch.setattr(installed_profile, "load_installed_profile", load)
    with pytest.raises(installed_profile.InstalledProfileError):
        factory(
            application_root="unused",
            identity_directory="unused",
            device_database="unused",
            recording_root="unused",
        )
    load.assert_not_called()
    create.assert_not_called()


def test_internal_factory_uses_existing_injection_without_stage03_runtime(
    monkeypatch, layout, profile_module
):
    values, _ = layout
    resolver, launcher = _runtime()
    application = _application()
    observed = []
    monkeypatch.setattr(stage_one_app, "environ", MappingProxyType({}))
    monkeypatch.setattr(
        stage_one_app,
        "build_environment_operator_runtime",
        Mock(side_effect=AssertionError("Stage03 trial must not be used")),
    )

    def create(**kwargs):
        observed.append((kwargs, dict(stage_one_app.environ)))
        return application

    monkeypatch.setattr(stage_one_app, "create_app", create)
    for _ in range(2):
        assert (
            stage_one_app._create_installed_operator_app(
                **values, source_resolver=resolver, launcher=launcher
            )
            is application
        )
        assert stage_one_app.environ == {}
    assert observed[0] == observed[1]
    kwargs, environment = observed[0]
    assert kwargs == {
        "control_plane_site_id": "install-fixture-site",
        "user_db_path": values["identity_directory"] / "users.sqlite3",
        "device_db_path": values["device_database"],
        "operator_recording_root": values["recording_root"],
        "operator_source_resolver": resolver,
        "operator_launcher": launcher,
    }
    assert environment == {}
    resolver.resolve.assert_not_called()
    launcher.run.assert_not_called()


def test_internal_factory_refuses_trial_source_before_state_read(
    monkeypatch, layout, profile_module
):
    values, reader = layout
    resolver, launcher = _runtime()
    create = Mock(side_effect=AssertionError("application construction"))
    monkeypatch.setattr(stage_one_app, "environ", {"K5_STAGE03_CAM_CRED": "private-marker"})
    monkeypatch.setattr(stage_one_app, "create_app", create)
    with pytest.raises(profile_module.InstalledProfileError) as caught:
        stage_one_app._create_installed_operator_app(
            **values, source_resolver=resolver, launcher=launcher
        )
    reader.assert_not_called()
    create.assert_not_called()
    assert "private-marker" not in str(caught.value)


def test_internal_factory_restores_environment_when_application_raises(
    monkeypatch, layout, profile_module
):
    values, _ = layout
    resolver, launcher = _runtime()
    environment = {"K5_DEVICE_DB_PATH": "", "unrelated": "preserved"}
    before = dict(environment)
    monkeypatch.setattr(stage_one_app, "environ", environment)
    monkeypatch.setattr(stage_one_app, "create_app", Mock(side_effect=RuntimeError("fixture")))
    with pytest.raises(RuntimeError):
        stage_one_app._create_installed_operator_app(
            **values, source_resolver=resolver, launcher=launcher
        )
    assert environment == before


@pytest.mark.parametrize(
    "missing",
    [
        "user_registry",
        "device_registry",
        "operator_launch_coordinator",
        "operator_recording_coordinator",
        "operator_playback_coordinator",
        "operator_playback_timeline",
        "operator_export_coordinator",
        "operator_recording_catalog",
    ],
)
def test_incomplete_app_refuses_and_closes_owned_registries(
    monkeypatch, layout, profile_module, missing
):
    values, _ = layout
    resolver, launcher = _runtime()
    app = _application()
    registries = {
        name: getattr(app.state, name)
        for name in ("user_registry", "device_registry")
        if name != missing
    }
    setattr(app.state, missing, None)
    monkeypatch.setattr(stage_one_app, "environ", {})
    monkeypatch.setattr(stage_one_app, "create_app", Mock(return_value=app))
    with pytest.raises(profile_module.InstalledProfileError):
        stage_one_app._create_installed_operator_app(
            **values, source_resolver=resolver, launcher=launcher
        )
    assert stage_one_app.environ == {}
    for registry in registries.values():
        registry.close.assert_called_once_with()


def test_failed_registry_close_still_attempts_other_owned_close(
    monkeypatch, layout, profile_module
):
    values, _ = layout
    resolver, launcher = _runtime()
    app = _application()
    app.state.operator_playback_coordinator = None
    app.state.user_registry.close.side_effect = RuntimeError("private-marker")
    monkeypatch.setattr(stage_one_app, "environ", {})
    monkeypatch.setattr(stage_one_app, "create_app", Mock(return_value=app))
    with pytest.raises(profile_module.InstalledProfileError) as caught:
        stage_one_app._create_installed_operator_app(
            **values, source_resolver=resolver, launcher=launcher
        )
    app.state.device_registry.close.assert_called_once_with()
    assert "private-marker" not in str(caught.value)


def test_real_app_composition_binds_durable_paths_and_recovers_catalog_with_databases_mocked(
    monkeypatch, layout, profile_module
):
    from k5vision import main, user_admin_api
    from k5vision.operator_recording_catalog import BoundedRecordingCatalog
    from k5vision.services.device_registry import DeviceRegistry
    from k5vision.services.user_registry import UserRegistry

    values, _ = layout
    resolver, launcher = _runtime()
    environment = MappingProxyType({})
    for module in (main, user_admin_api, stage_one_app):
        monkeypatch.setattr(module, "environ", environment)
    # Exercise the actual app/factory/coordinator wiring; never open either DB,
    # scan media, create a camera session or invoke native presentation.
    device_open, user_open, recover = Mock(), Mock(), Mock()
    monkeypatch.setattr(DeviceRegistry, "_open", device_open)
    monkeypatch.setattr(UserRegistry, "_open", user_open)
    monkeypatch.setattr(BoundedRecordingCatalog, "recover", recover)
    applications = [
        stage_one_app._create_installed_operator_app(
            **values, source_resolver=resolver, launcher=launcher
        )
        for _ in range(2)
    ]
    assert device_open.call_count == user_open.call_count == recover.call_count == 2
    for app in applications:
        assert app.state.device_registry._database_path == str(values["device_database"])
        assert app.state.user_registry._database_path == str(
            values["identity_directory"] / "users.sqlite3"
        )
        assert app.state.device_registry.site_id == "install-fixture-site"
        assert app.state.operator_recording_catalog._root == values["recording_root"]
        assert app.state.operator_recording_coordinator._recording_root == values["recording_root"]
        paths = {route.path for route in app.routes}
        assert "/api/v1/auth/login" in paths
        assert "/api/v1/devices" in paths
        assert "/api/v1/operator/recordings" in paths
        app.state.user_registry.close()
        app.state.device_registry.close()
    assert (
        applications[0].state.user_session_manager is not applications[1].state.user_session_manager
    )
    assert environment == {}
    assert not (values["identity_directory"] / "users.sqlite3").exists()
    assert list(values["recording_root"].iterdir()) == []
    resolver.resolve.assert_not_called()
    launcher.run.assert_not_called()


@pytest.mark.parametrize("explicit", [False, True])
def test_main_user_database_forwarding_preserves_existing_environment_default(
    monkeypatch, tmp_path, explicit
):
    from k5vision import main, user_admin_api
    from k5vision.services.user_registry import UserRegistry

    inherited = tmp_path / "existing-default.sqlite3"
    selected = tmp_path / "explicit-installed.sqlite3"
    environment = MappingProxyType({"K5_USER_DB_PATH": str(inherited)})
    monkeypatch.setattr(main, "environ", environment)
    monkeypatch.setattr(user_admin_api, "environ", environment)
    opened = Mock()
    monkeypatch.setattr(UserRegistry, "_open", opened)
    options = {"user_db_path": selected} if explicit else {}
    app = main.create_app(control_plane_site_id="fixture-site", **options)
    assert app.state.user_registry._database_path == str(selected if explicit else inherited)
    opened.assert_called_once_with()
    assert dict(environment) == {"K5_USER_DB_PATH": str(inherited)}
    assert not selected.exists()
    assert not inherited.exists()
    app.state.user_registry.close()
