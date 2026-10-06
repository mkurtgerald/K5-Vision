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
        "GetWindow",
        "GetWindowLongPtrW",
        "SetWindowLongPtrW",
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


def _native_fixture_function(name, bindings):
    """Exercise actual nested helper bytes with pure fake Win32 dependencies."""
    helper = _function(_tree(_WITNESS_NAME), _HELPER_NAME)
    node = next(n for n in helper.body if isinstance(n, ast.FunctionDef) and n.name == name)
    namespace = dict(bindings)
    exec(
        compile(ast.Module(body=[node], type_ignores=[]), "<native-fixture-source>", "exec"),
        namespace,
    )
    return namespace[name]


def test_sentinel_settle_is_bounded_and_revalidates_take_and_dispatch():
    from types import SimpleNamespace

    module = _diagnostic_module()
    messages = [SimpleNamespace(message=0x031F), None]
    events = []

    def validate():
        events.append("validate")

    def take():
        events.append("take")
        return messages.pop(0)

    def dispatch(message):
        events.append(("dispatch", message.message))

    assert module._settle_sentinel_queue(validate, take, dispatch, clock=lambda: 0) == (0x031F,)
    assert events == [
        "validate",
        "take",
        "validate",
        ("dispatch", 0x031F),
        "validate",
        "validate",
        "take",
        "validate",
    ]


def test_sentinel_settle_refuses_message_count_deadline_and_callback_retirement():
    from types import SimpleNamespace

    import pytest

    module = _diagnostic_module()
    calls = []
    message = SimpleNamespace(message=0x031F)
    with pytest.raises(RuntimeError, match="message bound"):
        module._settle_sentinel_queue(lambda: None, lambda: message, calls.append, clock=lambda: 0)
    assert len(calls) == module._MAX_CYCLES == 32
    for phase in ("validate", "take", "dispatch"):
        now = [0]
        events = []

        def operation(name, phase=phase, now=now, events=events):
            events.append(name)
            if name == phase:
                now[0] = 1.0
            return message

        with pytest.raises(RuntimeError, match="deadline"):
            module._settle_sentinel_queue(
                lambda: operation("validate"),
                lambda: operation("take"),
                lambda _: operation("dispatch"),
                clock=lambda now=now: now[0],
            )
        assert events[-1] == ("validate" if phase != "validate" else phase)
        assert events.count("take") <= 1 and events.count("dispatch") <= 1
    for phase in ("take", "dispatch"):
        valid = [True]
        events = []

        def validate(valid=valid):
            if not valid[0]:
                raise RuntimeError("generation retired")

        def take(valid=valid, phase=phase):
            if phase == "take":
                valid[0] = False
            return message

        def dispatch(_, valid=valid, events=events):
            events.append("dispatch")
            valid[0] = False

        with pytest.raises(RuntimeError, match="generation retired"):
            module._settle_sentinel_queue(validate, take, dispatch, clock=lambda: 0)
        assert events == ([] if phase == "take" else ["dispatch"])


def _sentinel_take_fixture(queue, *, mutate=None, validate=None):
    import ctypes

    class Message(ctypes.Structure):
        _fields_ = [("hwnd", ctypes.c_void_p), ("message", ctypes.c_uint32)]

    calls, removed = [], []

    def peek(pointer, hwnd, minimum, maximum, flags):
        calls.append((hwnd.value, minimum, maximum, flags))
        if mutate is not None:
            mutate(flags)
        for index, (target, kind) in enumerate(queue):
            # HWND selectors include children; generated WM_QUIT bypasses filters.
            if kind != 0x12 and (target not in (71, 72) or not minimum <= kind <= maximum):
                continue
            message = ctypes.cast(pointer, ctypes.POINTER(Message)).contents
            message.hwnd, message.message = target, kind
            if flags:
                removed.append(queue.pop(index))
            return 1
        return 0

    take = _native_fixture_function(
        "take_sentinel_message",
        {
            "ctypes": ctypes,
            "_Win32Message": Message,
            "sentinel": 71,
            "_SENTINEL_SYSTEM_MESSAGE": 0x031F,
            "_PM_NOREMOVE": 0,
            "_PM_REMOVE": 1,
            "raw_peek": peek,
            "validate_sentinel_phase": validate or (lambda: None),
        },
    )
    return take, calls, removed


def test_sentinel_exact_notification_is_settled_without_foreign_or_other_kind_removal():
    queue = [(999, 0x031F), (0, 0x8001), (71, 0x8002), (71, 0x031F)]
    take, calls, removed = _sentinel_take_fixture(queue)
    message = take()
    assert (message.hwnd, message.message) == (71, 0x031F)
    assert removed == [(71, 0x031F)]
    assert take() is None
    assert queue == [(999, 0x031F), (0, 0x8001), (71, 0x8002)]
    assert calls == [(71, 0x031F, 0x031F, 0), (71, 0x031F, 0x031F, 1), (71, 0x031F, 0x031F, 0)]


def test_sentinel_noremove_rejects_child_and_quit_even_when_quit_bypasses_range_filter():
    import pytest

    for value in ((72, 0x031F), (71, 0x12), (0, 0x12)):
        queue = [value]
        take, calls, removed = _sentinel_take_fixture(queue)
        with pytest.raises(RuntimeError, match="observation refused"):
            take()
        assert calls == [(71, 0x031F, 0x031F, 0)]
        assert queue == [value] and removed == []


def test_sentinel_generation_change_during_noremove_prevents_removal():
    import pytest

    valid = [True]

    def mutate(flags):
        valid[0] = False

    def validate():
        if not valid[0]:
            raise RuntimeError("retired generation")

    queue = [(71, 0x031F)]
    take, calls, removed = _sentinel_take_fixture(queue, mutate=mutate, validate=validate)
    with pytest.raises(RuntimeError, match="retired generation"):
        take()
    assert len(calls) == 1 and removed == [] and queue == [(71, 0x031F)]


def test_second_peek_target_mutation_is_nonatomic_and_fails_before_dispatch():
    import pytest

    # A sent callback inside the second PeekMessage can replace the observed
    # message. Win32 may already remove the replacement; rejection cannot undo it.
    for replacement in ((72, 0x031F), (0, 0x12)):
        queue = [(71, 0x031F)]

        def mutate(flags, queue=queue, replacement=replacement):
            if flags:
                queue[:] = [replacement]

        take, calls, removed = _sentinel_take_fixture(queue, mutate=mutate)
        dispatched = []
        with pytest.raises(RuntimeError, match="removal changed"):
            _diagnostic_module()._settle_sentinel_queue(
                lambda: None, take, dispatched.append, clock=lambda: 0
            )
        assert len(calls) == 2 and removed == [replacement] and queue == []
        assert dispatched == []


def test_sentinel_identity_requires_thread_live_standalone_and_full_width_generation():
    import ctypes

    import pytest

    generation = object()
    state = {"thread": True, "live": True, "parent": 0, "tag": id(generation)}

    def same_thread():
        if not state["thread"]:
            raise RuntimeError("wrong thread")

    def owned(hwnd):
        if not state["live"] or hwnd != 71:
            raise RuntimeError("not owned")

    require = _native_fixture_function(
        "require_sentinel",
        {
            "ctypes": ctypes,
            "same_thread": same_thread,
            "require_owned": owned,
            "sentinel": 71,
            "extra_windows": [71],
            "sentinel_generation": generation,
            "get_parent": lambda _: state["parent"],
            "_GWLP_USERDATA": -21,
            "get_user_data": lambda *_: state["tag"],
        },
    )
    require()
    for key, bad in (("thread", False), ("live", False), ("parent", 999), ("tag", 0), ("tag", 123)):
        prior = state[key]
        state[key] = bad
        with pytest.raises(RuntimeError):
            require()
        state[key] = prior
    assert id(generation) > 2**32


def test_sentinel_cleanup_identity_is_independent_of_peer_routes_but_phase_is_not():
    import pytest

    local_calls = []

    def local():
        local_calls.append("verified local sentinel")

    def peer():
        raise RuntimeError("peer generation retired")

    phase = _native_fixture_function(
        "validate_sentinel_phase",
        {
            "validate_diagnostic_owners": peer,
            "require_sentinel": local,
        },
    )
    with pytest.raises(RuntimeError, match="peer generation retired"):
        phase()
    local()
    assert local_calls == ["verified local sentinel"]
    helper = _function(_tree(_WITNESS_NAME), _HELPER_NAME)
    local_node = next(
        n for n in helper.body if isinstance(n, ast.FunctionDef) and n.name == "require_sentinel"
    )
    assert "validate_diagnostic_owners" not in _calls(local_node)
    cleanup = next(n for n in helper.body if isinstance(n, ast.Try)).finalbody
    assert "require_sentinel" in set().union(*(_calls(n) for n in cleanup))
    assert "validate_sentinel_phase" not in set().union(*(_calls(n) for n in cleanup))


def test_sentinel_phase_rejects_children_and_rechecks_identity_after_child_query():
    import ctypes

    import pytest

    for child in (0, 72):
        events = []
        phase = _native_fixture_function(
            "validate_sentinel_phase",
            {
                "ctypes": ctypes,
                "sentinel": 71,
                "_GW_CHILD": 5,
                "validate_diagnostic_owners": lambda events=events: events.append("peer"),
                "require_sentinel": lambda events=events: events.append("sentinel"),
                "get_window": lambda *_, child=child: child,
            },
        )
        if child:
            with pytest.raises(RuntimeError, match="unexpectedly has children"):
                phase()
            assert events == ["peer", "sentinel"]
        else:
            phase()
            assert events == ["peer", "sentinel", "sentinel"]


def test_sentinel_dispatch_validates_target_kind_and_identity_after_callback():
    import ctypes
    from types import SimpleNamespace

    import pytest

    events = []
    valid = [True]

    def validate():
        if not valid[0]:
            raise RuntimeError("retired after dispatch")

    def raw_dispatch(_):
        events.append("dispatch")
        valid[0] = False

    message = ctypes.c_int()
    message.hwnd, message.message = 71, 0x031F
    dispatch = _native_fixture_function(
        "dispatch_sentinel_message",
        {
            "ctypes": ctypes,
            "validate_sentinel_phase": validate,
            "sentinel": 71,
            "_SENTINEL_SYSTEM_MESSAGE": 0x031F,
            "raw_dispatch": raw_dispatch,
        },
    )
    for hwnd, kind in ((72, 0x031F), (71, 0x12)):
        with pytest.raises(RuntimeError, match="dispatch refused"):
            dispatch(SimpleNamespace(hwnd=hwnd, message=kind))
    assert events == []
    with pytest.raises(RuntimeError, match="retired after dispatch"):
        dispatch(message)
    assert events == ["dispatch"]


def test_sentinel_settling_follows_preservation_proof_and_precedes_original_postquit():
    helper = _function(_tree(_WITNESS_NAME), _HELPER_NAME)
    resource_try = next(n for n in helper.body if isinstance(n, ast.Try))
    sequence = [ast.unparse(n) for n in resource_try.body]
    proof = next(i for i, text in enumerate(sequence) if "proof failed before settling" in text)
    settled = next(
        i for i, text in enumerate(sequence) if text.startswith("sentinel_settled_kinds =")
    )
    quit_post = next(i for i, text in enumerate(sequence) if text == "post_quit(0)")
    assert proof < settled < quit_post
    assert (
        "sentinel_after_a == sentinel_after_b == sentinel_removed == sentinel_marker"
        in sequence[proof]
    )
    take = next(
        n
        for n in helper.body
        if isinstance(n, ast.FunctionDef) and n.name == "take_sentinel_message"
    )
    peeks = [
        n
        for n in ast.walk(take)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "raw_peek"
    ]
    assert len(peeks) == 2
    assert all(ast.unparse(n.args[1]) == "ctypes.c_void_p(sentinel)" for n in peeks)
    assert all(
        [ast.unparse(a) for a in n.args[2:4]] == ["_SENTINEL_SYSTEM_MESSAGE"] * 2 for n in peeks
    )
    assert [ast.unparse(n.args[4]) for n in peeks] == ["_PM_NOREMOVE", "_PM_REMOVE"]
    assert "validate_sentinel_phase()" in ast.unparse(take)


def test_actual_sentinel_cleanup_survives_peer_failure_but_refuses_failed_or_reused_tag():
    import ctypes

    generation = object()
    for tag in (id(generation), 0, 123):
        destroyed, errors = [], []
        namespace = {
            "ctypes": ctypes,
            "same_thread": lambda: None,
            "require_owned": lambda _: None,
            "sentinel": 71,
            "extra_windows": [71],
            "sentinel_generation": generation,
            "get_parent": lambda _: 0,
            "_GWLP_USERDATA": -21,
            "get_user_data": lambda *_, tag=tag: tag,
            "cleanup_errors": errors,
            "is_window": lambda _: True,
            "raw_destroy": lambda _, destroyed=destroyed: destroyed.append(71) or 1,
        }

        def retired_peer():
            raise RuntimeError("peer registration retired")

        namespace["validate_diagnostic_owners"] = retired_peer
        namespace["require_sentinel"] = _native_fixture_function("require_sentinel", namespace)
        helper = _function(_tree(_WITNESS_NAME), _HELPER_NAME)
        resource_try = next(n for n in helper.body if isinstance(n, ast.Try))
        destroy_loop = next(
            n
            for n in resource_try.finalbody
            if isinstance(n, ast.For) and ast.unparse(n.iter) == "reversed(extra_windows)"
        )
        exec(
            compile(
                ast.Module(body=[destroy_loop], type_ignores=[]), "<native-cleanup-source>", "exec"
            ),
            namespace,
        )
        if tag == id(generation):
            assert destroyed == [71] and errors == []
        else:
            # Failed SetWindowLongPtr (still zero) and reused/mismatched HWND
            # cannot justify destroying an unverified generation. Qualification
            # fails with cleanup errors; no successful cleanup receipt is possible.
            assert destroyed == [] and errors == ["generated child or sentinel cleanup raised"]
