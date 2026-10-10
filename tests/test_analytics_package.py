"""Installed package identity tests use only synthetic Python and local wheel files."""

from __future__ import annotations

import csv
import hashlib
import importlib
import importlib.metadata
import importlib.util
import io
import json
import py_compile
import sys
import types
import zipfile
from pathlib import Path

import pytest

from k5vision import analytics_package as admission


@pytest.fixture
def builder():
    path = Path(__file__).resolve().parents[1] / "scripts/build_analytics_runtime_wheel.py"
    spec = importlib.util.spec_from_file_location("analytics_wheel_builder_tests", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def installed(tmp_path, monkeypatch, builder):
    source = tmp_path / "source"
    package = source / "analytics_lab"
    package.mkdir(parents=True)
    files = {
        "analytics_lab/__init__.py": b"VALUE = 1\n",
        "analytics_lab/tracking.py": b"TRACK = 2\n",
    }
    entries = []
    for name, data in files.items():
        (source / name).write_bytes(data)
        entries.append(
            {
                "source": "analytics",
                "source_path": name,
                "wheel_path": name,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "git_blob_sha1": hashlib.sha1(
                    b"blob " + str(len(data)).encode() + b"\0" + data
                ).hexdigest(),
            }
        )
    manifest = {"files": entries}
    manifest_bytes = json.dumps(manifest, sort_keys=True).encode()
    monkeypatch.setattr(admission, "_load_manifest", lambda: (manifest_bytes, manifest))
    monkeypatch.setattr(admission, "MANIFEST_SHA256", hashlib.sha256(manifest_bytes).hexdigest())
    monkeypatch.setattr(admission, "_admitted_root", None)
    monkeypatch.setattr(builder, "_admission", admission)
    for name in tuple(sys.modules):
        if name == "analytics_lab" or name.startswith("analytics_lab."):
            monkeypatch.delitem(sys.modules, name)
    wheel = builder.build_wheel(source, tmp_path / "wheels")
    site = tmp_path / "site"
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(site)
    monkeypatch.syspath_prepend(str(site))
    distributions = importlib.metadata.distributions
    monkeypatch.setattr(
        admission.importlib.metadata,
        "distributions",
        lambda **kwargs: distributions(path=[site], **kwargs),
    )
    yield types.SimpleNamespace(
        site=site,
        package=site / "analytics_lab",
        source=source,
        wheel=wheel,
        manifest=manifest,
        metadata=site / admission.DIST_INFO,
        builder=builder,
    )
    for name in tuple(sys.modules):
        if name == "analytics_lab" or name.startswith("analytics_lab."):
            sys.modules.pop(name)


def test_reviewed_manifest_is_complete_and_revision_bound():
    raw, manifest = admission._load_manifest()
    assert hashlib.sha256(raw).hexdigest() == admission.MANIFEST_SHA256
    assert manifest["source_revision"] == admission.ANALYTICS_REVISION
    assert manifest["distribution_version"] == admission.ANALYTICS_VERSION
    sources = [
        entry for entry in manifest["files"] if entry["wheel_path"].startswith("analytics_lab/")
    ]
    assert len(sources) == 67
    assert all(entry["source_path"] == entry["wheel_path"] for entry in sources)
    assert all(entry["wheel_path"].endswith(".py") for entry in sources)
    assert len({entry["wheel_path"] for entry in manifest["files"]}) == 71
    assert {Path(entry["wheel_path"]).name for entry in manifest["files"]} >= {
        "Analytics-Lab-LICENSE",
        "THIRD_PARTY.md",
        "ByteTrack-MIT.txt",
        "Apache-2.0.txt",
    }


def test_admission_does_not_import_analytics_or_native_dependencies(installed):
    before = set(sys.modules)
    assert admission.validate_installed_analytics() == installed.package
    assert not (set(sys.modules) - before) & {"analytics_lab", "openvino", "cv2", "numpy"}


def test_reproducible_wheel_bytes_records_and_timestamps(installed, tmp_path):
    second = installed.builder.build_wheel(installed.source, tmp_path / "second")
    assert installed.wheel.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(second) as archive:
        assert archive.namelist() == sorted(archive.namelist())
        assert all(item.date_time == (1980, 1, 1, 0, 0, 0) for item in archive.infolist())
        assert archive.testzip() is None
        assert b"Requires-Dist:" not in archive.read(f"{admission.DIST_INFO}/METADATA")


def test_repeated_admission_accepts_only_verified_loaded_modules_and_bytecode(installed):
    admission.validate_installed_analytics()
    importlib.import_module("analytics_lab.tracking")
    assert admission.validate_installed_analytics() == installed.package


@pytest.mark.parametrize("optimization", [0, 1, 2])
def test_generated_bytecode_is_verified(installed, optimization):
    py_compile.compile(str(installed.package / "tracking.py"), doraise=True, optimize=optimization)
    assert admission.validate_installed_analytics() == installed.package


@pytest.mark.parametrize("count", [0, 2])
def test_missing_or_duplicate_distribution_fails(installed, monkeypatch, count):
    distribution = next(importlib.metadata.distributions())
    monkeypatch.setattr(
        admission.importlib.metadata, "distributions", lambda **kw: [distribution] * count
    )
    with pytest.raises(admission.AnalyticsPackageError, match="Exactly one"):
        admission.validate_installed_analytics()


def test_wrong_version_fails(installed):
    path = installed.metadata / "METADATA"
    path.write_bytes(path.read_bytes().replace(admission.ANALYTICS_VERSION.encode(), b"0.0.1"))
    with pytest.raises(admission.AnalyticsPackageError, match="identity"):
        admission.validate_installed_analytics()


@pytest.mark.parametrize(
    "filename",
    [
        "analytics_lab/tracking.py",
        f"{admission.DIST_INFO}/analytics-runtime-manifest.json",
        f"{admission.DIST_INFO}/WHEEL",
    ],
)
@pytest.mark.parametrize("change", ["same-size", "size", "missing"])
def test_tampered_or_missing_payload_fails(installed, filename, change):
    path = installed.site / filename
    data = path.read_bytes()
    if change == "missing":
        path.unlink()
    elif change == "same-size":
        path.write_bytes(bytes([data[0] ^ 1]) + data[1:])
    else:
        path.write_bytes(data + b"x")
    with pytest.raises(admission.AnalyticsPackageError):
        admission.validate_installed_analytics()


@pytest.mark.parametrize(
    "filename",
    ["rogue.py", "rogue.so", "rogue.pyd", "__pycache__/rogue.pyc", "folder/unexpected.py"],
)
def test_unexpected_package_files_fail(installed, filename):
    path = installed.package / filename
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(b"unexpected")
    with pytest.raises(admission.AnalyticsPackageError, match="unexpected|unverified"):
        admission.validate_installed_analytics()


def test_tampered_cache_cannot_override_verified_source(installed):
    source = installed.package / "tracking.py"
    cache = Path(py_compile.compile(str(source), doraise=True))
    cache.write_bytes(cache.read_bytes()[:-1] + b"x")
    with pytest.raises(admission.AnalyticsPackageError, match="bytecode"):
        admission.validate_installed_analytics()


def test_checkout_shadowing_fails_without_executing_it(installed, tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    package = checkout / "analytics_lab"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("raise AssertionError('never execute this')")
    monkeypatch.syspath_prepend(str(checkout))
    with pytest.raises(admission.AnalyticsPackageError, match="shadowed"):
        admission.validate_installed_analytics()
    assert "analytics_lab" not in sys.modules


def test_import_before_admission_fails(installed):
    importlib.import_module("analytics_lab")
    with pytest.raises(admission.AnalyticsPackageError, match="before"):
        admission.validate_installed_analytics()


def test_loaded_module_origin_and_search_path_cannot_change(installed, monkeypatch):
    admission.validate_installed_analytics()
    module = importlib.import_module("analytics_lab")
    monkeypatch.setattr(module, "__file__", str(installed.source / "analytics_lab/__init__.py"))
    with pytest.raises(admission.AnalyticsPackageError, match="origin"):
        admission.validate_installed_analytics()
    monkeypatch.setattr(module, "__file__", str(installed.package / "__init__.py"))
    monkeypatch.setattr(module, "__path__", [str(installed.source / "analytics_lab")])
    with pytest.raises(admission.AnalyticsPackageError, match="search path"):
        admission.validate_installed_analytics()


@pytest.mark.parametrize("content", ["bogus", "duplicate", "wrong-hash", "outside", "no-self"])
def test_malformed_or_unbound_record_fails(installed, content):
    record = installed.metadata / "RECORD"
    rows = list(csv.reader(io.StringIO(record.read_text())))
    if content == "bogus":
        rows[0] = ["broken"]
    elif content == "duplicate":
        rows += rows[:1]
    elif content == "wrong-hash":
        rows[0][1] = "sha256=wrong"
    elif content == "outside":
        (installed.site / "outside.py").write_text("bad")
        rows.append(["outside.py", "", ""])
    else:
        rows = [row for row in rows if row[0] != f"{admission.DIST_INFO}/RECORD"]
    with record.open("w", newline="") as output:
        csv.writer(output).writerows(rows)
    with pytest.raises(admission.AnalyticsPackageError, match="record"):
        admission.validate_installed_analytics()


def test_editable_install_is_not_admitted(installed):
    (installed.metadata / "direct_url.json").write_text('{"dir_info":{"editable":true}}')
    with pytest.raises(admission.AnalyticsPackageError, match="Editable"):
        admission.validate_installed_analytics()


def test_symlinked_payload_is_not_admitted(installed, tmp_path):
    source = installed.package / "tracking.py"
    target = tmp_path / "elsewhere.py"
    target.write_bytes(source.read_bytes())
    source.unlink()
    try:
        source.symlink_to(target)
    except OSError:
        pytest.skip("Local operating system does not permit test symlinks")
    with pytest.raises(admission.AnalyticsPackageError, match="symbolic"):
        admission.validate_installed_analytics()


@pytest.mark.parametrize(
    "relative",
    ["../outside", "/absolute", "analytics_lab//tracking.py", "./analytics_lab/tracking.py"],
)
def test_unsafe_manifest_path_rejected(installed, relative):
    with pytest.raises(admission.AnalyticsPackageError, match="unsafe"):
        admission._safe_file(installed.site, relative)


def test_offline_wheel_builder_restores_only_pinned_crlf_donor_checkout(installed, tmp_path):
    source = installed.source / "analytics_lab/tracking.py"
    source.write_bytes(source.read_bytes().replace(b"\n", b"\r\n"))
    wheel = installed.builder.build_wheel(installed.source, tmp_path / "crlf-wheels")
    with zipfile.ZipFile(wheel) as archive:
        assert archive.read("analytics_lab/tracking.py") == b"TRACK = 2\n"
    assert source.read_bytes() == b"TRACK = 2\r\n"


@pytest.mark.parametrize(
    "unverified",
    [b"TRACK = 3\r\n", b"TRACK = 2\r\r\n", b"TRACK = 2\r\nx\n"],
)
def test_offline_wheel_builder_refuses_unpinned_or_mixed_crlf(installed, tmp_path, unverified):
    source = installed.source / "analytics_lab/tracking.py"
    source.write_bytes(unverified)
    output = tmp_path / "unverified-wheels"
    with pytest.raises(admission.AnalyticsPackageError):
        installed.builder.build_wheel(installed.source, output)
    assert not output.exists()


def test_reviewed_manifest_checkout_cannot_change_line_endings():
    root = Path(__file__).resolve().parents[1]
    rule = (root / ".gitattributes").read_text(encoding="ascii")
    assert "src/k5vision/data/analytics-runtime-manifest.json text eol=lf" in rule
    assert "src/k5vision/data/analytics-runtime-Apache-2.0.txt text eol=lf" in rule
    assert (
        hashlib.sha256(admission._MANIFEST_PATH.read_bytes()).hexdigest()
        == admission.MANIFEST_SHA256
    )
    notice = next(
        entry
        for entry in admission._load_manifest()[1]["files"]
        if entry["source"] == "supplemental-notice"
    )
    notice_bytes = (root / "src/k5vision/data" / notice["source_path"]).read_bytes()
    assert hashlib.sha256(notice_bytes).hexdigest() == notice["sha256"]


@pytest.mark.parametrize("change", ["changed", "missing", "extra", "git-blob"])
def test_offline_builder_rejects_unpinned_inputs(installed, tmp_path, change):
    path = installed.source / "analytics_lab/tracking.py"
    if change == "changed":
        path.write_text("TRACK = 3\n")
    elif change == "missing":
        path.unlink()
    elif change == "extra":
        (path.parent / "unexpected.py").write_text("bad")
    else:
        installed.manifest["files"][0]["git_blob_sha1"] = "0" * 40
    output = tmp_path / "rejected"
    with pytest.raises(admission.AnalyticsPackageError):
        installed.builder.build_wheel(installed.source, output)
    assert not output.exists()


def test_admission_manifest_is_itself_hash_bound(tmp_path, monkeypatch):
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}")
    monkeypatch.setattr(admission, "_MANIFEST_PATH", manifest)
    with pytest.raises(admission.AnalyticsPackageError, match="manifest"):
        admission._load_manifest()


def test_unreadable_admission_manifest_is_sanitized(tmp_path, monkeypatch):
    monkeypatch.setattr(admission, "_MANIFEST_PATH", tmp_path / "absent")
    with pytest.raises(admission.AnalyticsPackageError, match="cannot be verified"):
        admission.validate_installed_analytics()


def test_oversized_metadata_is_rejected_before_loading(installed):
    direct_url = installed.metadata / "direct_url.json"
    direct_url.write_bytes(b" " * 16_385)
    with pytest.raises(admission.AnalyticsPackageError, match="bound"):
        admission.validate_installed_analytics()
    direct_url.unlink()
    (installed.metadata / "RECORD").write_bytes(b" " * 128_001)
    with pytest.raises(admission.AnalyticsPackageError, match="bound"):
        admission.validate_installed_analytics()


def test_installer_cache_record_and_local_wheel_metadata_are_admitted(installed):
    source = installed.package / "tracking.py"
    cache = Path(py_compile.compile(str(source), doraise=True))
    with (installed.metadata / "RECORD").open("a", newline="") as output:
        csv.writer(output).writerow((cache.relative_to(installed.site).as_posix(), "", ""))
    (installed.metadata / "direct_url.json").write_text(
        '{"url":"file:///local/runtime.whl","archive_info":{}}'
    )
    assert admission.validate_installed_analytics() == installed.package


def test_unexpected_nested_cache_directory_is_rejected(installed):
    (installed.package / "__pycache__/__pycache__").mkdir(parents=True)
    with pytest.raises(admission.AnalyticsPackageError, match="directories"):
        admission.validate_installed_analytics()


def test_loaded_unmanifested_module_cannot_join_admitted_package(installed, monkeypatch):
    admission.validate_installed_analytics()
    importlib.import_module("analytics_lab")
    fake = types.ModuleType("analytics_lab.unknown")
    fake.__file__ = str(installed.package / "unknown.py")
    fake.__spec__ = importlib.util.spec_from_file_location("analytics_lab.unknown", fake.__file__)
    monkeypatch.setitem(sys.modules, "analytics_lab.unknown", fake)
    with pytest.raises(admission.AnalyticsPackageError, match="origin"):
        admission.validate_installed_analytics()


def test_loader_path_cannot_differ_from_admitted_origin(installed, monkeypatch):
    admission.validate_installed_analytics()
    module = importlib.import_module("analytics_lab")
    monkeypatch.setattr(
        module.__spec__.loader, "path", str(installed.source / "analytics_lab/__init__.py")
    )
    with pytest.raises(admission.AnalyticsPackageError, match="origin"):
        admission.validate_installed_analytics()


def test_interrupted_build_preserves_existing_wheel_and_removes_partial_output(
    installed, monkeypatch
):
    original = installed.wheel.read_bytes()

    def interrupted(*args, **kwargs):
        raise OSError("simulated interrupted write")

    monkeypatch.setattr(installed.builder.zipfile.ZipFile, "writestr", interrupted)
    with pytest.raises(OSError, match="interrupted"):
        installed.builder.build_wheel(installed.source, installed.wheel.parent)
    assert installed.wheel.read_bytes() == original
    assert list(installed.wheel.parent.glob("*.tmp")) == []


def test_cli_reports_wheel_and_identity(installed, monkeypatch, capsys):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "build",
            "--source-root",
            str(installed.source),
            "--output-dir",
            str(installed.wheel.parent),
        ],
    )
    assert installed.builder.main() == 0
    output = capsys.readouterr().out
    assert str(installed.wheel) in output
    assert hashlib.sha256(installed.wheel.read_bytes()).hexdigest() in output


def test_cli_rejects_wrong_source_without_wheel(installed, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "build",
            "--source-root",
            str(tmp_path / "missing"),
            "--output-dir",
            str(tmp_path / "out"),
        ],
    )
    with pytest.raises(SystemExit) as error:
        installed.builder.main()
    assert error.value.code == 2
    assert "build rejected" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_external_bytecode_cache_prefix_is_not_admitted(installed, monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "pycache_prefix", str(tmp_path / "external-cache"))
    with pytest.raises(admission.AnalyticsPackageError, match="External"):
        admission.validate_installed_analytics()
