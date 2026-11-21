"""An approving helper is admitted by its own native identity, never its PPID."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from test_windows_time_staging import _HELPERS as time_helpers

_INSTALLED = r"C:\Program Files\K5 Vision"
_HELPER = _INSTALLED + r"\K5Vision.exe"
_COMMAND = r"C:\Windows\System32\w32tm.exe"
_ADMIN = b"exact current administrator SID"


@pytest.fixture
def tracking(monkeypatch):
    state = SimpleNamespace(processes=[], births={}, facts={}, admitted=[], reads={})

    class Native:
        def birth(self, pid):
            return state.births.get(pid)

        def details(self, identity):
            if self.birth(identity[0]) != identity[1] or identity not in state.facts:
                raise ProcessLookupError()
            state.reads[identity] = state.reads.get(identity, 0) + 1
            return state.facts[identity]

        def parents(self):
            return {pid: value["parent_pid"] for (pid, _), value in state.facts.items()}

        def system_directory(self):
            return r"C:\Windows\System32"

    native = Native()

    def add_process(pid, birth, image, arguments, *, parent=0, **changes):
        state.births[pid] = birth
        state.facts[(pid, birth)] = {
            "pid": pid,
            "created_100ns": birth,
            "parent_pid": parent,
            "image": image,
            "sid": _ADMIN,
            "session": 2,
            "elevated": True,
            **changes,
        }
        process = SimpleNamespace(pid=pid, cmdline=lambda: [image, *arguments])
        state.processes.append(process)
        return process

    add_process(os.getpid(), 1000, r"C:\development\python.exe", [])
    # Ordinary GUI and historical command already exist before the click.
    add_process(10, 100, _HELPER, ["--no-browser"])
    add_process(11, 110, _COMMAND, ["/config", "/update"], parent=20)

    def add(process, *, role, expected_identity):
        if native.birth(process.pid) != expected_identity[1]:
            return None
        state.admitted.append((process.pid, role, expected_identity))
        return expected_identity

    cohort = SimpleNamespace(add=add)
    monkeypatch.setattr(time_helpers, "_time_processes", lambda: list(state.processes))
    tracker = time_helpers._TimeHelperTracker(cohort, _INSTALLED, native)
    return SimpleNamespace(
        state=state, native=native, cohort=cohort, tracker=tracker, add_process=add_process
    )


def test_appinfo_parent_does_not_hide_exact_elevated_approval_helper(tracking):
    tracking.add_process(20, 200, _HELPER, ["--approve-time"], parent=777)
    # Clock correction may invert displayed parent/child birth order.
    tracking.add_process(30, 50, _COMMAND, ["/config", "/update"], parent=20)
    tracking.tracker.sample()
    assert tracking.state.admitted == [
        (20, "time_helper", (20, 200)),
        (30, "time_helper", (30, 50)),
    ]
    assert tracking.tracker.evidence("configure") == {
        "operation": "configure",
        "helper_count": 1,
        "helper_observed": True,
        "native_command_count": 1,
        "native_command_observed": True,
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"sid": b"foreign administrator SID"},
        {"session": 0},
        {"elevated": False},
        {"image": r"C:\foreign\K5Vision.exe"},
    ],
)
def test_foreign_image_token_session_or_unelevated_helper_is_never_owned(tracking, changes):
    image = changes.pop("image", _HELPER)
    tracking.add_process(20, 200, image, ["--approve-time"], **changes)
    tracking.tracker.sample()
    assert not tracking.state.admitted
    assert tracking.tracker.evidence("configure")["helper_observed"] is False


@pytest.mark.parametrize("arguments", [[], ["--no-browser"], ["--time-admin", "request", "result"]])
def test_other_installed_commands_cannot_be_inferred_as_approval(tracking, arguments):
    tracking.add_process(20, 200, _HELPER, arguments)
    tracking.tracker.sample()
    assert not tracking.state.admitted


def test_historical_w32tm_with_reused_parent_pid_is_excluded_by_preclick_baseline(tracking):
    tracking.add_process(20, 200, _HELPER, ["--approve-time"])
    tracking.tracker.sample()
    assert tracking.state.admitted == [(20, "time_helper", (20, 200))]
    assert tracking.tracker.evidence("configure")["native_command_observed"] is False


def test_reused_approving_parent_cannot_authorize_new_command(tracking):
    tracking.add_process(20, 200, _HELPER, ["--approve-time"])
    tracking.tracker.sample()
    tracking.state.births[20] = 999
    tracking.add_process(30, 300, _COMMAND, ["/resync", "/rediscover"], parent=20)
    tracking.tracker.sample()
    assert tracking.state.admitted == [(20, "time_helper", (20, 200))]


def test_unobserved_short_command_is_reported_honestly(tracking):
    tracking.add_process(20, 200, _HELPER, ["--approve-time"])
    tracking.tracker.sample()
    note = tracking.tracker.evidence("resync")
    assert note["helper_observed"] is True
    assert note["native_command_count"] == 0 and note["native_command_observed"] is False


def test_short_command_exit_during_second_native_query_is_not_invented(tracking, monkeypatch):
    tracking.add_process(20, 200, _HELPER, ["--approve-time"])
    tracking.add_process(30, 300, _COMMAND, ["/resync", "/rediscover"], parent=20)
    original = tracking.native.details

    def details(identity):
        if identity == (30, 300) and tracking.state.reads.get(identity) == 1:
            raise ProcessLookupError()
        return original(identity)

    monkeypatch.setattr(tracking.native, "details", details)
    tracking.tracker.sample()
    assert tracking.state.admitted == [(20, "time_helper", (20, 200))]


def test_cohort_recheck_refusal_cannot_be_recorded_as_admitted_helper(tracking):
    tracking.add_process(20, 200, _HELPER, ["--approve-time"])
    tracking.cohort.add = lambda *args, **kwargs: None
    tracking.tracker.sample()
    assert tracking.tracker.evidence("configure")["helper_observed"] is False


def test_unreadable_baseline_cannot_later_be_claimed_as_new_helper(tracking, monkeypatch):
    original = tracking.native.birth

    def birth(pid):
        if pid == 10:
            raise OSError("private native failure")
        return original(pid)

    monkeypatch.setattr(tracking.native, "birth", birth)
    with pytest.raises(RuntimeError, match="baseline identity was unavailable"):
        time_helpers._TimeHelperTracker(tracking.cohort, _INSTALLED, tracking.native)
