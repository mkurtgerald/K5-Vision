"""Source-only contracts for gate selection and the generated routing witness.

These checks do not import product/native modules or execute the Win32 witness.
They complement, and cannot substitute for, the Windows qualification itself.
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_INTEGRATION = _ROOT / "tests" / "integration"
_WITNESS_NAME = "test_current_gate_message_routing_windows.py"
_WRAPPER_NAME = "test_real_owned_shell_queued_message_routing"
_HELPER_NAME = "run_owned_queued_message_routing"


def _tree(name: str) -> ast.Module:
    return ast.parse((_INTEGRATION / name).read_text(encoding="utf-8"))


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    return next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name
    )


def _calls(node: ast.AST) -> set[str]:
    return {
        call.func.attr if isinstance(call.func, ast.Attribute) else call.func.id
        for call in ast.walk(node)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name | ast.Attribute)
    }


def test_existing_windows_gate_selects_all_three_routing_variants() -> None:
    wrapper = _function(_tree("test_current_gate_application_windows.py"), _WRAPPER_NAME)
    parametrization = next(
        decorator
        for decorator in wrapper.decorator_list
        if isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Attribute)
        and decorator.func.attr == "parametrize"
    )
    assert ast.literal_eval(parametrization.args[0]) == "shell_kind"
    assert ast.literal_eval(parametrization.args[1]) == ("base", "interactive", "catalog")
    strings = {
        node.value
        for node in ast.walk(wrapper)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert _WITNESS_NAME in strings
    assert {_HELPER_NAME, "spec_from_file_location", "exec_module"} <= _calls(wrapper)
    workflow = (_ROOT / ".github/workflows/current-gate-target-windows.yml").read_text()
    pytest_line = next(line for line in workflow.splitlines() if line.strip().startswith("pytest "))
    assert "tests/integration/test_current_gate_application_windows.py" in pytest_line.split()


def test_witness_is_lazy_and_does_not_add_a_second_collected_native_test() -> None:
    tree = _tree(_WITNESS_NAME)
    assert all(
        not isinstance(node, ast.FunctionDef) or not node.name.startswith("test_")
        for node in tree.body
    )
    helper = _function(tree, _HELPER_NAME)
    assert isinstance(helper.body[1], ast.If)
    assert ast.unparse(helper.body[1].test) == "sys.platform != 'win32'"
    assert all(
        not isinstance(node, ast.Expr) or isinstance(node.value, ast.Constant) for node in tree.body
    )
    assert {"create_shell", "pump_messages", "destroy_shell", "native_peek", "native_dispatch"} <= (
        _calls(helper)
    )


def test_witness_uses_inert_queued_markers_without_global_input_or_capture() -> None:
    tree = _tree(_WITNESS_NAME)
    helper = _function(tree, _HELPER_NAME)
    imports = {
        node.name
        for statement in ast.walk(tree)
        if isinstance(statement, ast.Import)
        for node in statement.names
    }
    assert not imports & {"socket", "subprocess", "pyautogui"}
    native_names = {
        ast.literal_eval(call.args[1])
        for call in ast.walk(helper)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "bind"
    }
    assert native_names == {
        "PostMessageW",
        "PostQuitMessage",
        "IsWindow",
        "IsChild",
        "GetParent",
        "GetWindowThreadProcessId",
        "GetCurrentThreadId",
        "ShowWindow",
    }
    assert not _calls(helper) & {
        "SetCapture",
        "ReleaseCapture",
        "SendInput",
        "SendMessageW",
        "PostThreadMessageW",
        "GetDC",
        "GetWindowDC",
        "BitBlt",
        "PrintWindow",
        "Thread",
    }
    assert {"_WM_CLOSE", "_WM_LBUTTONDOWN", "_WM_LBUTTONUP", "_WM_COMMAND"}.isdisjoint(
        node.id for node in ast.walk(helper) if isinstance(node, ast.Name)
    )


def test_cleanup_precedes_behavior_assertions_and_covers_all_owned_windows() -> None:
    helper = _function(_tree(_WITNESS_NAME), _HELPER_NAME)
    resource_try = next(node for node in helper.body if isinstance(node, ast.Try))
    assert resource_try.finalbody
    assert not any(
        isinstance(node, ast.Assert)
        for statement in resource_try.body
        for node in ast.walk(statement)
    )
    cleanup_calls = set().union(*(_calls(statement) for statement in resource_try.finalbody))
    assert {"destroy_shell", "raw_destroy", "raw_peek", "is_window", "same_thread"} <= cleanup_calls
    after_cleanup = helper.body[helper.body.index(resource_try) + 1 :]
    asserted_names = {
        node.id
        for statement in after_cleanup
        if isinstance(statement, ast.Assert)
        for node in ast.walk(statement.test)
        if isinstance(node, ast.Name)
    }
    assert {
        "cleanup_errors",
        "cleanup_markers",
        "remaining_windows",
        "a_after_a",
        "a_after_b",
        "expected_a",
        "expected_b",
        "sentinel_after_a",
        "sentinel_after_b",
        "sentinel_after_quit",
        "sentinel_removed",
        "owned_targets_only",
        "routing_results",
        "quit_results",
        "quit_a",
        "quit_b",
        "sticky_quit",
    } <= asserted_names


def test_qualification_keeps_x64_message_abi_and_bounded_calls_explicit() -> None:
    tree = _tree(_WITNESS_NAME)
    constants = {
        node.targets[0].id: ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, ast.Constant)
    }
    assert constants["_PUMP_BOUND"] == 2
    assert constants["_MAX_CYCLES"] == 32
    helper = _function(tree, _HELPER_NAME)
    post = next(
        call
        for call in ast.walk(helper)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "bind"
        and isinstance(call.args[1], ast.Constant)
        and call.args[1].value == "PostMessageW"
    )
    assert isinstance(post.args[2], ast.List)
    assert [ast.unparse(value) for value in post.args[2].elts] == [
        "ctypes.c_void_p",
        "ctypes.c_uint32",
        "ctypes.c_size_t",
        "ctypes.c_ssize_t",
    ]
    assert any(
        isinstance(node, ast.Assert)
        and "ctypes.sizeof(_Win32Message) == 48" == ast.unparse(node.test)
        for node in helper.body
    )
    assert all(
        isinstance(call.args[0], ast.Name) and call.args[0].id == "_MAX_CYCLES"
        for call in ast.walk(helper)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "range"
    )
