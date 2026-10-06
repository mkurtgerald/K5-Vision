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
    assert {
        "run_owned_queued_message_routing_on_owner_thread",
        "spec_from_file_location",
        "exec_module",
    } <= _calls(wrapper)
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
        "GetQueueStatus",
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


def _diagnostic_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "quit_diagnostic_source", _INTEGRATION / _WITNESS_NAME
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_quit_diagnostic_probe_is_bounded_nonremoving_and_revalidates_every_call():
    module = _diagnostic_module()
    events = []
    preserved = [(999, 0x8003), (0, 0x8001), (71, 0x8002)]

    def validate():
        events.append("validate")

    def status():
        events.append("status")
        return {"current": 0x100, "changed": 0x100}

    def peek(selector, minimum, maximum, flags):
        assert flags == 0
        events.append((selector, minimum, maximum, flags))
        for hwnd, message in preserved:
            if selector == -1 and hwnd != 0:
                continue
            if minimum and not minimum <= message <= maximum:
                continue
            scope = "thread" if hwnd == 0 else "foreign_window"
            return {"available": True, "message": message, "scope": scope}
        return {"available": False}

    result = module._collect_quit_diagnostics(validate, status, peek)
    assert events == [
        "validate",
        "status",
        "validate",
        "validate",
        (-1, 0x12, 0x12, 0),
        "validate",
        "validate",
        (None, 0x12, 0x12, 0),
        "validate",
        "validate",
        (-1, 0, 0, 0),
        "validate",
        "validate",
        (None, 0, 0, 0),
        "validate",
        "validate",
        "status",
        "validate",
    ]
    assert preserved == [(999, 0x8003), (0, 0x8001), (71, 0x8002)]
    assert result["probes"]["global_head"]["scope"] == "foreign_window"
    assert result["probes"]["thread_head"]["scope"] == "thread"
    assert result["peek_calls"] == 4
    assert result["removal_requested"] is False
    assert result["queued_dispatch_requested"] is False
    assert result["observed_after_product_results_frozen"]
    assert result["sent_callbacks_may_run"]
    assert result["virtual_messages_may_be_generated"]
    assert result["queue_status_change_flags_may_be_cleared"]
    assert result["observations_are_sequential_not_atomic"]


def test_quit_diagnostic_stops_after_reentrant_owner_retirement_in_status_or_peek():
    import pytest

    module = _diagnostic_module()
    for during in ("status", "peek"):
        valid = [True]
        events = []

        def validate(valid=valid):
            if not valid[0]:
                raise RuntimeError("owner retired")

        def status(events=events, during=during, valid=valid):
            events.append("status")
            if during == "status":
                valid[0] = False
            return {"current": 0, "changed": 0}

        def peek(*args, events=events, valid=valid):
            events.append("peek")
            valid[0] = False
            return {"available": False}

        with pytest.raises(RuntimeError, match="owner retired"):
            module._collect_quit_diagnostics(validate, status, peek)
        assert events == (["status"] if during == "status" else ["status", "peek"])


def test_native_diagnostics_follow_frozen_product_results_and_never_remove_foreign_work():
    tree = _tree(_WITNESS_NAME)
    helper = _function(tree, _HELPER_NAME)
    resource_try = next(node for node in helper.body if isinstance(node, ast.Try))
    sequence = [ast.unparse(node) for node in resource_try.body]
    observed = next(
        index for index, text in enumerate(sequence) if "quit_diagnostics = _collect" in text
    )
    sticky = next(index for index, text in enumerate(sequence) if text.startswith("sticky_quit ="))
    sentinel = next(
        index for index, text in enumerate(sequence) if text.startswith("sentinel_after_quit =")
    )
    assert observed > sentinel > sticky
    assert (
        sum(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "post_quit"
            for node in ast.walk(helper)
        )
        == 1
    )
    diagnostic_peek = next(
        node
        for node in helper.body
        if isinstance(node, ast.FunctionDef) and node.name == "diagnostic_peek"
    )
    assert "flags != _PM_NOREMOVE" in ast.unparse(diagnostic_peek)
    assert _calls(diagnostic_peek).isdisjoint({"dispatch", "native_dispatch", "_dispatch_message"})
    assert "wParam" not in ast.unparse(diagnostic_peek)
    assert "lParam" not in ast.unparse(diagnostic_peek)
    owner_check = next(
        node
        for node in helper.body
        if isinstance(node, ast.FunctionDef) and node.name == "validate_diagnostic_owners"
    )
    assert "registration=registrations[name]" in ast.unparse(owner_check)
    assert {"same_thread", "require_owned", "require"} <= _calls(owner_check)


def test_owned_thread_is_fresh_and_disposes_before_joined_result():
    import threading

    module = _diagnostic_module()
    parent = threading.current_thread()
    parent_queue = ["ambient foreign message"]
    calls = []
    owners = []

    def work(stopped):
        owner = threading.current_thread()
        owners.append(owner)
        assert owner is not parent and not stopped.is_set()
        for operation in ("create", "post_quit", "pump", "dispose"):
            calls.append((operation, owner))
        return {"cleanup_complete": True}

    assert module._run_on_owner_thread(work) == {"cleanup_complete": True}
    assert all(owner is owners[0] for _, owner in calls)
    assert [op for op, _ in calls] == ["create", "post_quit", "pump", "dispose"]
    assert not owners[0].is_alive()
    assert parent_queue == ["ambient foreign message"]


def test_owned_thread_propagates_assertions_and_base_exceptions_after_join():
    import threading

    import pytest

    module = _diagnostic_module()
    for kind in (AssertionError, RuntimeError, SystemExit):
        error = kind("synthetic witness failure")
        owners = []

        def work(stopped, error=error, owners=owners):
            owners.append(threading.current_thread())
            raise error

        with pytest.raises(kind) as caught:
            module._run_on_owner_thread(work)
        assert caught.value is error
        assert len(owners) == 1 and not owners[0].is_alive()


def test_owned_thread_deadline_requests_same_thread_cleanup_and_remains_failure():
    import threading

    import pytest

    module = _diagnostic_module()
    owners, disposed = [], []

    def work(stopped):
        owner = threading.current_thread()
        owners.append(owner)
        try:
            assert stopped.wait(2), "parent did not request cooperative stop"
        finally:
            disposed.append(threading.current_thread())
        return {"cleanup_complete": True}

    with pytest.raises(TimeoutError, match="owner thread joined after stop"):
        module._run_on_owner_thread(work, join_seconds=0.01, cleanup_seconds=2)
    assert disposed == owners
    assert len(owners) == 1 and not owners[0].is_alive()


def test_owned_thread_unresponsive_native_call_never_claims_cleanup(monkeypatch):
    import pytest

    module = _diagnostic_module()
    observations = []

    class UnresponsiveThread:
        def __init__(self, **kwargs):
            observations.append(("thread", kwargs["daemon"]))

        def start(self):
            observations.append("start")

        def join(self, seconds):
            observations.append(("join", seconds))

        def is_alive(self):
            return True

    monkeypatch.setattr(module.threading, "Thread", UnresponsiveThread)
    with pytest.raises(RuntimeError, match="still running; cleanup is unverified"):
        module._run_on_owner_thread(lambda _: None, join_seconds=0.01, cleanup_seconds=0.02)
    assert observations[:3] == [("thread", True), "start", ("join", 0.01)]
    assert len(observations) == 4 and observations[3][0] == "join"
    assert 0 < observations[3][1] <= 0.02


def test_thread_isolation_wraps_entire_native_witness_and_cancellation_does_not_block_cleanup():
    tree = _tree(_WITNESS_NAME)
    wrapped = _function(tree, "run_owned_queued_message_routing_on_owner_thread")
    assert {_HELPER_NAME, "_run_on_owner_thread"} <= _calls(wrapped)
    assert "_stop_event=stopped" in ast.unparse(wrapped)
    original = _function(tree, _HELPER_NAME)
    resource_try = next(node for node in original.body if isinstance(node, ast.Try))
    assert ast.unparse(resource_try.finalbody[0]) == "cleanup_started = True"
    same_thread = next(
        node
        for node in original.body
        if isinstance(node, ast.FunctionDef) and node.name == "same_thread"
    )
    checks = ast.unparse(same_thread)
    assert "get_thread()" in checks
    assert "not cleanup_started" in checks and "_stop_event.is_set()" in checks
    assert "remaining_registrations == 0" in ast.unparse(original)
    threaded = _function(tree, "_run_on_owner_thread")
    assert _calls(threaded).isdisjoint(
        {"PeekMessageW", "PostQuitMessage", "destroy_shell", "raw_destroy"}
    )
    assert "Queue(maxsize=1)" in ast.unparse(threaded)
    assert "owner.join(join_seconds)" in ast.unparse(threaded)
    assert "owner.join(remaining)" in ast.unparse(threaded)
    assert "cleanup_deadline = monotonic() + cleanup_seconds" in ast.unparse(threaded)


def test_parent_interruption_during_either_join_requests_creator_cleanup(monkeypatch):
    import threading

    import pytest

    module = _diagnostic_module()
    native_thread = threading.Thread

    def scenario(interruption_call):
        release_cleanup = threading.Event()
        owners, stopped_events, disposed, threads = [], [], [], []
        interrupted = KeyboardInterrupt("synthetic parent interruption")

        class InterruptibleThread:
            def __init__(self, **kwargs):
                self.native = native_thread(**kwargs)
                self.joins = []
                threads.append(self)

            def start(self):
                self.native.start()

            def is_alive(self):
                return self.native.is_alive()

            def join(self, seconds):
                self.joins.append(seconds)
                count = len(self.joins)
                if count == interruption_call:
                    raise interrupted
                if interruption_call == 2 and count == 1:
                    return  # Simulate initial observation deadline before stop.
                release_cleanup.set()
                self.native.join(seconds)

        def work(stopped):
            owner = threading.current_thread()
            owners.append(owner)
            stopped_events.append(stopped)
            try:
                assert stopped.wait(2)
            finally:
                assert release_cleanup.wait(2)
                disposed.append(threading.current_thread())
            return {"cleanup_complete": True}

        monkeypatch.setattr(module.threading, "Thread", InterruptibleThread)
        try:
            with pytest.raises(KeyboardInterrupt) as caught:
                module._run_on_owner_thread(work, join_seconds=0.01, cleanup_seconds=2)
            assert caught.value is interrupted
            assert len(stopped_events) == 1 and stopped_events[0].is_set()
            assert disposed == owners and not threads[0].is_alive()
            assert len(threads[0].joins) == interruption_call + 1
            assert 0 < threads[0].joins[-1] <= 2
            if interruption_call == 2:
                assert threads[0].joins[-1] <= threads[0].joins[-2]
        finally:
            release_cleanup.set()
            if threads:
                threads[0].native.join(2)
            monkeypatch.setattr(module.threading, "Thread", native_thread)

    scenario(1)
    scenario(2)
