"""Camera-free regressions for pip's Windows cache filename representation."""

import base64
import csv
import hashlib
import importlib.util
import io
import ntpath
import os
import py_compile
import subprocess
import sys
import zipfile
from pathlib import Path, PureWindowsPath

import pytest

from k5vision import analytics_package as admission


@pytest.fixture
def windows_cache(tmp_path, monkeypatch):
    """Use real compiler/file I/O; map only the logical Windows cache location."""
    source = PureWindowsPath(r"C:\generated\site-packages\analytics_lab\tracking.py")
    data = b'"""Generated fixture."""\nVALUE = 2\n'
    local_source = tmp_path / "tracking.py"
    local_source.write_bytes(data)
    original = importlib.util.cache_from_source
    caches = {key: tmp_path / f"tracking-{key or '0'}.pyc" for key in ("", "1", "2")}

    def cache_from_source(path, debug_override=None, *, optimization=None):
        if path == str(source):
            return str(caches[optimization or ""])
        return original(path, debug_override, optimization=optimization)

    monkeypatch.setattr(admission.importlib.util, "cache_from_source", cache_from_source)
    pip_filename = ntpath.join(
        str(source.parent.parent), source.relative_to(source.parent.parent).as_posix()
    )

    def generate(
        filename=pip_filename, optimization=0, mode=py_compile.PycInvalidationMode.TIMESTAMP
    ):
        key = str(optimization) if optimization else ""
        py_compile.compile(
            str(local_source),
            cfile=str(caches[key]),
            dfile=filename,
            doraise=True,
            optimize=optimization,
            invalidation_mode=mode,
        )
        return caches[key]

    return source, data, pip_filename, generate


@pytest.mark.parametrize("optimization", [0, 1, 2])
@pytest.mark.parametrize("mode", list(py_compile.PycInvalidationMode))
@pytest.mark.parametrize("spelling", ["native", "pip"])
def test_verified_windows_cache_spelling(windows_cache, optimization, mode, spelling):
    source, data, pip_filename, generate = windows_cache
    assert pip_filename != str(source)
    assert PureWindowsPath(pip_filename) == source
    filename = str(source) if spelling == "native" else pip_filename
    cache = generate(filename, optimization, mode)
    admission._verify_bytecode(cache, {source: data})


@pytest.mark.parametrize(
    "change", ["source", "foreign-name", "magic", "flags", "trailing", "truncated"]
)
def test_changed_windows_cache_stays_rejected(windows_cache, change):
    source, data, pip_filename, generate = windows_cache
    cache = generate(pip_filename if change != "foreign-name" else r"C:\elsewhere\tracking.py")
    raw = cache.read_bytes()
    if change == "source":
        data = data.replace(b"VALUE = 2", b"VALUE = 3")
    elif change == "magic":
        cache.write_bytes(b"BAD!" + raw[4:])
    elif change == "flags":
        cache.write_bytes(raw[:4] + (2).to_bytes(4, "little") + raw[8:])
    elif change == "trailing":
        cache.write_bytes(raw + b"unverified")
    elif change == "truncated":
        cache.write_bytes(raw[:-1])
    with pytest.raises(admission.AnalyticsPackageError, match="unverified bytecode"):
        admission._verify_bytecode(cache, {source: data})


def test_native_generated_cache_remains_verified(tmp_path):
    source = tmp_path / "tracking.py"
    data = b"VALUE = 2\n"
    source.write_bytes(data)
    cache = Path(py_compile.compile(str(source), doraise=True))
    admission._verify_bytecode(cache, {source: data})


@pytest.mark.skipif(os.name != "nt", reason="Actual pip Windows installation regression")
def test_real_pip_windows_package_cache_is_verified(tmp_path):
    sources = {
        "analytics_lab/__init__.py": b"VALUE = 1\n",
        "analytics_lab/tracking.py": b"TRACK = 2\n",
    }
    dist = "k5_cache_fixture-1.0.dist-info"
    files = {
        **sources,
        f"{dist}/METADATA": b"Metadata-Version: 2.1\nName: k5-cache-fixture\nVersion: 1.0\n",
        f"{dist}/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    record = io.StringIO(newline="")
    writer = csv.writer(record, lineterminator="\n")
    for name, data in sorted(files.items()):
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        writer.writerow((name, f"sha256={digest}", len(data)))
    writer.writerow((f"{dist}/RECORD", "", ""))
    files[f"{dist}/RECORD"] = record.getvalue().encode()
    wheel = tmp_path / "k5_cache_fixture-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    prefix = tmp_path / "installed runtime"
    # A prefix installs directly at its final location; --target would copy
    # a temporary install and leave unrelated temporary co_filename values.
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-m",
            "pip",
            "--isolated",
            "install",
            "--no-index",
            "--no-deps",
            "--ignore-installed",
            "--no-cache-dir",
            "--disable-pip-version-check",
            "--no-warn-script-location",
            "--prefix",
            str(prefix),
            str(wheel),
        ],
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, "Local synthetic wheel installation failed"
    packages = list(prefix.rglob("analytics_lab"))
    assert len(packages) == 1
    package = packages[0]
    assert len(list(package.glob("__pycache__/*.pyc"))) == 2
    expected = {package.parent / name: data for name, data in sources.items()}
    assert all(path.read_bytes() == data for path, data in expected.items())
    admission._verify_package_tree(package.parent, expected)
