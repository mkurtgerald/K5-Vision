"""Source-only selection/pin assertions; never execute the native helper."""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_INTEGRATION = _ROOT / "tests/integration"


def test_native_helper_is_lazy_and_selected_once_by_existing_windows_wrapper():
    helper_path = _INTEGRATION / "test_operator_playback_shell_windows.py"
    helper_tree = ast.parse(helper_path.read_text())
    assert all(
        not isinstance(node, ast.FunctionDef) or not node.name.startswith("test_")
        for node in helper_tree.body
    )
    helper = next(n for n in helper_tree.body if isinstance(n, ast.FunctionDef))
    assert helper.name == "run_owned_playback_shell_controls"
    assert ast.unparse(helper.body[1].test) == "sys.platform != 'win32'"
    wrapper = next(
        node
        for node in ast.parse(
            (_INTEGRATION / "test_current_gate_application_windows.py").read_text()
        ).body
        if isinstance(node, ast.FunctionDef)
        and node.name == "test_real_owned_playback_shell_controls"
    )
    assert (
        sum(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == helper.name
            for node in ast.walk(wrapper)
        )
        == 1
    )
    assert "test_operator_playback_shell_windows.py" in ast.unparse(wrapper)
    workflow = (_ROOT / ".github/workflows/current-gate-target-windows.yml").read_text()
    pytest_line = next(line for line in workflow.splitlines() if line.strip().startswith("pytest "))
    assert "tests/integration/test_current_gate_application_windows.py" in pytest_line.split()


def test_application_source_pin_is_exact_and_other_required_pins_are_preserved():
    tree = ast.parse((_INTEGRATION / "test_current_gate_application_windows.py").read_text())
    expected = {
        "windows_operator_message_routing",
        "viewport_client_projection",
        "windows_operator_application",
        "windows_operator_interaction",
        "windows_operator_catalog_ui",
        "windows_operator_catalog_overlay",
    }
    maps = [node for node in ast.walk(tree) if isinstance(node, ast.Dict)]
    source_map = next(
        node
        for node in maps
        if expected <= {key.value for key in node.keys if isinstance(key, ast.Constant)}
    )
    pins = ast.literal_eval(source_map)
    assert set(pins) == expected
    for name, digest in pins.items():
        assert (
            hashlib.sha256(
                (_ROOT / f"src/k5vision/media/{name}.py").read_bytes().replace(b"\r\n", b"\n")
            ).hexdigest()
            == digest
        )


def test_native_acceptance_uses_actual_button_messages_and_source_free_receipt():
    source = (_INTEGRATION / "test_operator_playback_shell_windows.py").read_text()
    tree = ast.parse(source)
    sends = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "send"
    ]
    assert (
        sum(
            isinstance(node.args[1], ast.Constant) and node.args[1].value == 0x00F5
            for node in sends
        )
        == 3
    )
    assert "SendMessageW" in source and "PostMessageW" not in source
    assert '"authenticated_media_acceptance": False' in source
    assert '"media_execution": False' in source
