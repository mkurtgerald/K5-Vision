"""The native regression must compile the actual, unambiguous source bodies."""

from __future__ import annotations

import hashlib
import runpy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PROBE = runpy.run_path(str(ROOT / "scripts/probe_windows_legacy_profiles.py"))


@pytest.fixture
def source_checkout(tmp_path):
    for name in PROBE["_SOURCE_FILES"]:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((ROOT / name).read_text(encoding="utf-8"), encoding="utf-8")
    return tmp_path


def test_native_regression_binds_each_actual_source_and_preserves_validation(source_checkout):
    generated, binding = PROBE["materialize"](source_checkout)
    for name in PROBE["_SOURCE_FILES"]:
        text = (source_checkout / name).read_text(encoding="utf-8")
        assert (
            binding["normalized_source_sha256"][name] == hashlib.sha256(text.encode()).hexdigest()
        )
    assert binding["compiled_probe_sha256"] == hashlib.sha256(generated.encode()).hexdigest()
    assert "private static HashSet<string> Current(string text)" in generated
    assert "private static int Existing(string text)" in generated
    assert "Custody.AdmitMigrationData" not in generated
    assert not binding["filesystem_and_acl_admission_exercised"]
    assert not binding["product_modified"] and not binding["profile_or_service_written"]
    assert not binding["bridge_process_launch_exercised"]
    assert "ProcessStartInfo start =" not in generated
    assert "serviceStatus = serviceWorker && !persistent" in generated


def test_native_extractor_ignores_braces_inside_strings_characters_and_comments():
    body = r"""class Wanted { string first = "}"; string second = @"{{";
        char close = '}'; /* } */ // }
        if (true) { } }"""
    assert PROBE["_extract"]("before " + body + " after", "class Wanted") == body


@pytest.mark.parametrize("failure", ["duplicate_class", "truncated_owners", "duplicate_validator"])
def test_ambiguous_or_incomplete_source_never_yields_a_native_receipt(source_checkout, failure):
    if failure == "duplicate_class":
        path = source_checkout / "windows/desktop/K5Vision.cs"
        text = path.read_text() + "\ninternal static class Json { }\n"
    else:
        path = source_checkout / "windows/desktop/K5RecorderHost.cs"
        text = path.read_text()
        if failure == "truncated_owners":
            anchor = "private static HashSet<string> Owners(string data)"
            text = text[: text.index(anchor)] + anchor + " { if (true) {"
        else:
            anchor = (
                "Dictionary<string, object> value = Json.Parse("
                "File.ReadAllText(marker, Encoding.UTF8));"
            )
            text = text.replace(anchor, anchor + "\n" + anchor)
    path.write_text(text)
    with pytest.raises(ValueError):
        PROBE["materialize"](source_checkout)


def test_ambiguous_existing_marker_cast_is_not_silently_replaced(source_checkout):
    path = source_checkout / "windows/desktop/K5ServiceInstallation.cs"
    text = path.read_text()
    anchor = 'foreach (object sid in (IList)previous["profiles"])'
    end = "if (!found.Contains((string)sid)) found.Add((string)sid);"
    start = text.index(anchor)
    traversal = text[start : text.index(end, start) + len(end)]
    path.write_text(text.replace(traversal, traversal + "\n" + traversal))
    with pytest.raises(ValueError):
        PROBE["materialize"](source_checkout)


def test_native_probe_refuses_linked_source_files(source_checkout):
    path = source_checkout / "windows/desktop/K5Vision.cs"
    alternate = path.with_name("linked-source.cs")
    path.rename(alternate)
    path.symlink_to(alternate)
    with pytest.raises(ValueError):
        PROBE["materialize"](source_checkout)


@pytest.mark.parametrize("failure", ["duplicate_receive", "ambiguous_constructor"])
def test_bridge_regression_requires_actual_unambiguous_context_and_receive(
    source_checkout, failure
):
    path = source_checkout / "windows/desktop/K5Vision.cs"
    text = path.read_text()
    if failure == "duplicate_receive":
        receive = PROBE["_extract"](
            text, "private void Receive(object sender, DataReceivedEventArgs args)"
        )
        text = text.replace(receive, receive + "\n" + receive)
    else:
        text = text.replace(
            "ProcessStartInfo start =", "ProcessStartInfo start = ProcessStartInfo start ="
        )
    path.write_text(text)
    with pytest.raises(ValueError):
        PROBE["materialize"](source_checkout)
