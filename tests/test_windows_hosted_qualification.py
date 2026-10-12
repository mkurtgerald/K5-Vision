"""Portable fake API tests only. Never invokes NativeMetrics or a real Job."""

import ast
import ctypes
import hashlib
import importlib.util
import json
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "_hosted_qualification_test", ROOT / "scripts/windows_hosted_qualification.py"
)
guard = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = guard
SPEC.loader.exec_module(guard)
QualificationResources, ResourceFailure, GIB = (
    guard.QualificationResources,
    guard.ResourceFailure,
    guard.GIB,
)
Accounting, ExtendedLimits, ProcessIds = guard.Accounting, guard.ExtendedLimits, guard.ProcessIds
Tcp4, Tcp6, Udp4, Udp6 = guard.Tcp4, guard.Tcp6, guard.Udp4, guard.Udp6
MemoryCounters, NativeMetrics = guard.MemoryCounters, guard.NativeMetrics


class Clock:
    value = 10.0

    def __call__(self):
        return self.value


class FakeJob:
    def __init__(self):
        self.handle = 100
        self.assigned = []
        self.drained = False

    def assign(self, process):
        self.assigned.append(process)

    def accounting(self):
        return types.SimpleNamespace(
            total_user_time=20_000_000,
            total_kernel_time=10_000_000,
            active_processes=0,
            total_processes=getattr(self, "total_processes", 2),
        )

    def close(self):
        self.drained = True
        self.accounting()


class FakeNative:
    def __init__(self):
        self.pids = {11, 12}
        self.identities = {11: (101, 0), 12: (102, 0)}
        self.rss_each = 100
        self.cpu = 1.5
        self.total = 2
        self.times = 0
        self.closed_handles = []
        self.configured = False
        self.listener_rows = set()
        self.parent_in_job = False
        self.fail = None
        self.job = None
        self.read_count = 0

    def parent_outside(self, job):
        if self.parent_in_job:
            raise ResourceFailure("supervisor_inside_payload_job")

    def configure(self, job):
        self.configured, self.job = True, job
        self.parent_outside(job)

    def accounting(self, job):
        if self.fail == "accounting":
            raise ResourceFailure("resource_evidence_unavailable")
        self.read_count += 1
        return (self.total, len(self.pids), self.cpu)

    def members(self, job):
        if self.fail == "membership" and self.read_count > 1:
            return {11}
        return set(self.pids)

    def open_member(self, pid, job):
        if self.fail == "open":
            raise ResourceFailure("resource_evidence_unavailable")
        return pid, self.identities[pid][0]

    def belongs(self, handle, job):
        return self.fail != "foreign"

    def identity(self, handle):
        return self.identities[handle]

    def rss(self, handle):
        return self.rss_each

    def host(self, disk_root):
        self.times += 1
        return (5 * GIB, 8 * GIB, (self.times * 100, self.times * 200, self.times * 100), 20 * GIB)

    def listeners(self, pids):
        if self.job is not None and self.job.drained:
            return set() if self.fail != "listener_cleanup" else {(11, 2, b"\x7f\0\0\1", 9000)}
        if self.fail == "listener":
            raise ResourceFailure("owned_listener_not_loopback")
        return self.listener_rows

    def assert_job_handle_closed(self, job):
        if not job.drained:
            raise ResourceFailure("owned_job_handle_cleanup_failed")

    def close_handle(self, handle):
        self.closed_handles.append(handle)


class AdapterTests(unittest.TestCase):
    def make(self, before_release=None):
        self.clock, self.native = Clock(), FakeNative()
        self.owner = QualificationResources(
            types.SimpleNamespace(WindowsJob=FakeJob),
            "owned",
            clock=self.clock,
            native_factory=lambda: self.native,
            before_release=before_release,
        )
        self.job = self.owner.job_factory()
        self.clock.value += 0.25
        return self.owner

    def test_factory_reuses_original_assignment_and_close(self):
        self.make()
        self.job.assign("fake-root")
        self.assertEqual(self.job.assigned, ["fake-root"])
        self.assertTrue(self.native.configured)
        self.job.close()
        self.assertTrue(self.job.drained)

    def test_before_release_runs_after_assignment_before_return(self):
        events = []

        def check(owner):
            self.assertEqual(owner.job.assigned, ["fake-root"])
            self.assertFalse(owner.closed)
            events.append("before_release")

        self.make(before_release=check)
        self.job.assign("fake-root")
        events.append("assign_returned")
        self.assertEqual(events, ["before_release", "assign_returned"])

    def test_before_release_failure_drains_owned_job_and_never_returns_success(self):
        def check(owner):
            raise ResourceFailure("fresh_host_admission_failed")

        owner = self.make(before_release=check)
        with self.assertRaisesRegex(ResourceFailure, "fresh_host_admission_failed"):
            self.job.assign("fake-root")
        self.assertTrue(owner.failed and self.job.drained and owner.cleanup_complete)

    def test_invalid_before_release_callback_rejected_before_native_construction(self):
        with self.assertRaisesRegex(ResourceFailure, "invalid_before_release"):
            self.make(before_release=7)

    def test_snapshot_minimal_and_payload_scoped(self):
        snap = self.make().sample()
        self.assertEqual(snap.rss_bytes, 200)
        self.assertEqual(snap.active_processes, 2)
        self.assertEqual(snap.job_cpu_seconds, 1.5)
        self.assertAlmostEqual(snap.host_cpu_percent, 200 / 3)
        self.assertEqual(snap.scope, "owned-job-payload-only")
        self.assertFalse(
            any(key in vars(snap) for key in ("pids", "handles", "addresses", "paths"))
        )

    def test_api_failure_aborts_owned_job(self):
        owner = self.make()
        self.native.fail = "accounting"
        with self.assertRaises(ResourceFailure):
            owner.sample()
        self.assertTrue(self.job.drained and owner.failed)

    def test_open_failure_aborts_owned_job(self):
        owner = self.make()
        self.native.fail = "open"
        with self.assertRaises(ResourceFailure):
            owner.sample()
        self.assertTrue(self.job.drained)

    def test_foreign_process_aborts_owned_job(self):
        owner = self.make()
        self.native.fail = "foreign"
        with self.assertRaisesRegex(ResourceFailure, "ownership"):
            owner.sample()
        self.assertTrue(self.job.drained)

    def test_membership_race_fails_closed(self):
        owner = self.make()
        self.native.fail = "membership"
        with self.assertRaisesRegex(ResourceFailure, "membership"):
            owner.sample()

    def test_pid_reuse_fails_closed(self):
        owner = self.make()
        owner.sample()
        self.clock.value += 0.25
        self.native.identities[11] = (999, 0)
        with self.assertRaisesRegex(ResourceFailure, "pid_reuse"):
            owner.sample()
        self.assertTrue(self.job.drained)

    def test_signaled_process_still_in_final_membership_fails_closed(self):
        owner = self.make()
        self.native.identities[11] = (101, 222)
        with self.assertRaisesRegex(ResourceFailure, "membership_changed"):
            owner.sample()

    def test_sampled_rss_ceiling(self):
        owner = self.make()
        self.native.rss_each = GIB + 1
        with self.assertRaisesRegex(ResourceFailure, "rss_exceeded"):
            owner.sample()

    def test_stale_sampling_fails_closed(self):
        owner = self.make()
        owner.sample()
        self.clock.value += 1.01
        with self.assertRaisesRegex(ResourceFailure, "stale"):
            owner.sample()

    def test_stale_consumption_aborts(self):
        owner = self.make()
        snap = owner.sample()
        self.clock.value += 1.01
        with self.assertRaisesRegex(ResourceFailure, "stale"):
            owner.assert_fresh(snap)
        self.assertTrue(self.job.drained)

    def test_non_loopback_listener_aborts(self):
        owner = self.make()
        self.native.fail = "listener"
        with self.assertRaisesRegex(ResourceFailure, "loopback"):
            owner.sample()

    def test_cleanup_checks_listeners_and_releases_handles(self):
        owner = self.make()
        owner.sample()
        self.native.fail = "listener_cleanup"
        with self.assertRaisesRegex(ResourceFailure, "listener_cleanup"):
            owner.close()
        self.assertEqual(self.native.closed_handles, [11, 12])

    def test_parent_must_be_outside_job(self):
        self.make()
        self.native.parent_in_job = True
        with self.assertRaisesRegex(ResourceFailure, "supervisor_inside"):
            self.job.assign("fake-root")
        self.assertEqual(self.job.assigned, [])

    def test_cpu_accounting_must_be_monotonic(self):
        owner = self.make()
        owner.sample()
        self.clock.value += 0.25
        self.native.cpu = 0.5
        with self.assertRaises(ResourceFailure):
            owner.sample()

    def test_factory_one_job_only(self):
        owner = self.make()
        with self.assertRaisesRegex(ResourceFailure, "factory_reused"):
            owner.job_factory()

    def test_terminal_cpu_and_zero_active_captured_before_original_close(self):
        owner = self.make()
        owner.sample()
        owner.close()
        self.assertTrue(owner.cleanup_complete)
        self.assertEqual(
            (owner.final_cpu_seconds, owner.final_active, owner.final_total), (3.0, 0, 2)
        )

    def test_over_active_cap_denied(self):
        owner = self.make()
        self.native.pids = set(range(1, 34))
        with self.assertRaisesRegex(ResourceFailure, "membership"):
            owner.sample()

    def test_fixed_width_windows_layout_x64(self):
        self.assertEqual(ctypes.sizeof(Accounting), 48)
        self.assertEqual(ctypes.sizeof(Tcp4), 24)
        self.assertEqual(ctypes.sizeof(Tcp6), 56)
        self.assertEqual(ctypes.sizeof(Udp4), 12)
        self.assertEqual(ctypes.sizeof(Udp6), 28)
        if ctypes.sizeof(ctypes.c_void_p) == 8:
            self.assertEqual(ctypes.sizeof(ExtendedLimits), 144)
            self.assertEqual(ProcessIds.pids.offset, 8)
            self.assertEqual(ctypes.sizeof(MemoryCounters), 72)

    def test_native_interface_imported_but_not_constructed(self):
        # NativeMetrics constructor must never be invoked by this portable suite.
        self.assertTrue(callable(NativeMetrics))


class StableSnapshotFakeTests(unittest.TestCase):
    """All lifecycle changes are scripted fake observations; no native processes."""

    make = AdapterTests.make

    def expire(self, pid=11):
        birth, _ = self.native.identities[pid]
        self.native.identities[pid] = (birth, True)
        self.native.pids.remove(pid)

    def during_listeners(self, callback):
        original = self.native.listeners
        calls = []

        def listeners(pids):
            rows = original(pids)
            if not self.job.drained:
                calls.append(set(pids))
                return callback(len(calls), rows)
            return rows

        self.native.listeners = listeners
        return calls

    def test_exit_before_rss_requires_absence_and_then_stable_snapshot(self):
        owner = self.make()
        original = self.native.open_member

        def opened(pid, job):
            value = original(pid, job)
            if pid == 11:
                self.expire(pid)
            return value

        self.native.open_member = opened
        snapshot = owner.sample()
        self.assertEqual((snapshot.active_processes, snapshot.rss_bytes), (1, 100))
        self.assertEqual(self.native.read_count, 4)
        self.assertEqual(set(owner.handles), {11, 12})
        self.assertEqual(self.native.closed_handles, [])
        owner.close()
        self.assertEqual(self.native.closed_handles, [11, 12])

    def test_exit_during_rss_preserves_observed_high_water(self):
        owner = self.make()
        original = self.native.rss

        def rss(handle):
            result = original(handle)
            if handle == 11:
                self.expire(handle)
            return result

        self.native.rss = rss
        snapshot = owner.sample()
        self.assertEqual((snapshot.active_processes, snapshot.rss_bytes), (1, 200))
        self.assertEqual(self.native.read_count, 4)

    def test_exit_after_listener_read_preserves_listener_union_and_cumulative_cpu(self):
        owner = self.make()
        row = (11, 2, b"\x7f\0\0\1", 9000)

        def changed(number, rows):
            if number == 1:
                self.expire()
                self.native.cpu = 2.5
                return {row}
            return set()

        calls = self.during_listeners(changed)
        snapshot = owner.sample()
        self.assertEqual(snapshot.rss_bytes, 200)
        self.assertEqual(snapshot.owned_loopback_listeners, 1)
        self.assertEqual(snapshot.job_cpu_seconds, 2.5)
        self.assertEqual(snapshot.total_births, 2)
        self.assertEqual(owner.observed_listeners, {row})
        self.assertEqual(calls, [{11, 12}, {11, 12}])
        owner.close()
        self.assertEqual(owner.final_cpu_seconds, 3)

    def test_exit_after_identity_pass_is_not_an_unproved_retry(self):
        owner = self.make()
        original = self.native.members
        calls = []

        def members(job):
            calls.append(1)
            if len(calls) == 2:
                self.expire()
            return original(job)

        self.native.members = members
        with self.assertRaisesRegex(ResourceFailure, "membership_changed"):
            owner.sample()
        self.assertEqual(len(calls), 2)
        self.assertTrue(self.job.drained)

    def test_survivor_exit_during_final_state_recheck_refuses(self):
        owner = self.make()
        original = self.native.identity
        reads = {11: 0, 12: 0}

        def identity(handle):
            reads[handle] += 1
            if handle == 11 and reads[handle] == 3:
                self.expire(handle)
            return original(handle)

        self.native.identity = identity
        with self.assertRaisesRegex(ResourceFailure, "membership_changed"):
            owner.sample()
        self.assertEqual(self.native.read_count, 2)

    def test_changed_creation_on_signaled_handle_never_retries(self):
        owner = self.make()

        def changed(number, rows):
            self.expire()
            self.native.identities[11] = (999, True)
            return rows

        calls = self.during_listeners(changed)
        with self.assertRaisesRegex(ResourceFailure, "pid_reuse"):
            owner.sample()
        self.assertEqual(len(calls), 1)

    def test_unexplained_departure_never_retries(self):
        owner = self.make()

        def changed(number, rows):
            self.native.pids.remove(11)
            return rows

        calls = self.during_listeners(changed)
        with self.assertRaisesRegex(ResourceFailure, "membership_changed"):
            owner.sample()
        self.assertEqual(len(calls), 1)

    def test_new_birth_or_unknown_member_never_retries(self):
        for total_increase, new_pid in ((True, False), (False, True), (True, True)):
            with self.subTest(total_increase=total_increase, new_pid=new_pid):
                owner = self.make()

                def changed(number, rows, total_increase=total_increase, new_pid=new_pid):
                    self.expire()
                    if total_increase:
                        self.native.total = 3
                        self.job.total_processes = 3
                    if new_pid:
                        self.native.pids.add(13)
                        self.native.identities[13] = (103, False)
                    return rows

                calls = self.during_listeners(changed)
                with self.assertRaisesRegex(ResourceFailure, "membership_changed"):
                    owner.sample()
                self.assertEqual(len(calls), 1)

    def test_reappearing_proved_departure_never_retries(self):
        owner = self.make()
        original = self.native.accounting

        def accounting(job):
            if self.native.read_count == 2:
                self.native.pids.add(11)
                self.native.identities[11] = (101, False)
            return original(job)

        self.native.accounting = accounting
        calls = self.during_listeners(
            lambda number, rows: (self.expire() if number == 1 else None) or rows
        )
        with self.assertRaisesRegex(ResourceFailure, "membership_changed"):
            owner.sample()
        self.assertEqual(len(calls), 1)

    def test_three_changing_attempts_exhaust_without_returning_mixed_snapshot(self):
        owner = self.make()
        self.native.pids.add(13)
        self.native.identities[13] = (103, False)
        self.native.total = self.job.total_processes = 3

        def changed(number, rows):
            self.expire(10 + number)
            return rows

        calls = self.during_listeners(changed)
        with self.assertRaisesRegex(ResourceFailure, "resource_evidence_unstable"):
            owner.sample()
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.native.read_count, 6)
        self.assertTrue(owner.cleanup_complete)

    def test_original_deadline_is_not_reset_between_attempts(self):
        owner = self.make()

        def changed(number, rows):
            self.clock.value += 0.6 if number == 1 else 0.5
            if number == 1:
                self.expire()
            return rows

        calls = self.during_listeners(changed)
        with self.assertRaisesRegex(ResourceFailure, "resource_evidence_stale"):
            owner.sample()
        self.assertEqual(len(calls), 2)
        self.assertTrue(self.job.drained)

    def test_rss_api_error_during_exit_is_fatal_without_retry(self):
        owner = self.make()
        reads = []

        def rss(handle):
            reads.append(handle)
            self.expire(handle)
            raise ResourceFailure("resource_evidence_unavailable")

        self.native.rss = rss
        with self.assertRaisesRegex(ResourceFailure, "resource_evidence_unavailable"):
            owner.sample()
        self.assertEqual(reads, [11])
        self.assertEqual(self.native.read_count, 1)

    def test_api_failure_after_proved_departure_is_not_retried(self):
        owner = self.make()

        def changed(number, rows):
            self.expire()
            raise ResourceFailure("process_wait_failed")

        calls = self.during_listeners(changed)
        with self.assertRaisesRegex(ResourceFailure, "process_wait_failed"):
            owner.sample()
        self.assertEqual(len(calls), 1)

    def test_discarded_attempt_cannot_hide_rss_limit(self):
        owner = self.make()
        self.native.rss_each = GIB + 1
        original = self.native.rss

        def rss(handle):
            value = original(handle)
            if handle == 12:
                self.expire(handle)
            return value

        self.native.rss = rss
        with self.assertRaisesRegex(ResourceFailure, "owned_rss_exceeded"):
            owner.sample()
        self.assertEqual(self.native.read_count, 1)
        self.assertEqual(owner.observed_peak_rss_bytes, 2 * GIB + 2)

    def test_failed_retry_preserves_prior_attempt_peak(self):
        owner = self.make()
        self.native.rss_each = 300
        original = self.native.rss
        reads = []

        def rss(handle):
            reads.append(handle)
            if len(reads) == 3:
                raise ResourceFailure("resource_evidence_unavailable")
            return original(handle)

        self.native.rss = rss
        self.during_listeners(lambda number, rows: (self.expire() if number == 1 else None) or rows)
        with self.assertRaisesRegex(ResourceFailure, "resource_evidence_unavailable"):
            owner.sample()
        self.assertEqual(owner.observed_peak_rss_bytes, 600)
        self.assertTrue(owner.cleanup_complete)

    def test_observed_peak_never_decreases_across_samples(self):
        owner = self.make()
        self.assertEqual(owner.sample().rss_bytes, 200)
        self.clock.value += 0.25
        self.native.rss_each = 25
        self.assertEqual(owner.sample().rss_bytes, 50)
        self.assertEqual(owner.observed_peak_rss_bytes, 200)

    def test_discarded_attempt_listener_union_limit_is_preserved(self):
        owner = self.make()

        def changed(number, rows):
            if number == 1:
                self.expire()
                return {(11, 2, b"\x7f\0\0\1", port) for port in range(1, 4097)}
            return {(12, 2, b"\x7f\0\0\1", 5000)}

        calls = self.during_listeners(changed)
        with self.assertRaisesRegex(ResourceFailure, "listener_evidence_limit"):
            owner.sample()
        self.assertEqual(len(calls), 2)

    def test_departing_process_nonloopback_listener_is_not_retryable(self):
        owner = self.make()

        def changed(number, rows):
            self.expire()
            raise ResourceFailure("owned_listener_not_loopback")

        calls = self.during_listeners(changed)
        with self.assertRaisesRegex(ResourceFailure, "owned_listener_not_loopback"):
            owner.sample()
        self.assertEqual(len(calls), 1)

    def test_cpu_regression_after_discarded_attempt_is_fatal(self):
        owner = self.make()
        original = self.native.accounting

        def accounting(job):
            if self.native.read_count == 2:
                self.native.cpu = 2.0
            return original(job)

        self.native.accounting = accounting

        def changed(number, rows):
            self.expire()
            self.native.cpu = 2.5
            return rows

        calls = self.during_listeners(changed)
        with self.assertRaisesRegex(ResourceFailure, "resource_evidence_unavailable"):
            owner.sample()
        self.assertEqual(len(calls), 1)
        self.assertEqual(owner.last_cpu, 2.5)

    def test_cumulative_cpu_limit_survives_reconciled_departure(self):
        owner = self.make()

        def changed(number, rows):
            if number == 1:
                self.expire()
                self.native.cpu = 2.5
            return rows

        self.during_listeners(changed)
        with patch.object(guard.time, "monotonic", self.clock):
            sample = guard.sample_values(owner)
        self.assertEqual(sample["cpu_seconds"], 2.5)
        with self.assertRaisesRegex(guard.Refusal, "cpu_limit"):
            guard.admit_sample(sample, previous_cpu=898)


class NativeBoundaryFakeTests(unittest.TestCase):
    def native(self):
        return NativeMetrics.__new__(NativeMetrics)

    def process_native(self, *, wait=258, exit_time=987654321):
        native = self.native()
        calls = []

        def times(handle, birth, exited, kernel, user):
            for pointer, value in ((birth, 101), (exited, exit_time), (kernel, 0), (user, 0)):
                ctypes.cast(pointer, ctypes.POINTER(guard.U64))[0] = value
            return 1

        def wait_for(handle, timeout):
            calls.append((handle, timeout))
            return wait

        native.k = types.SimpleNamespace(GetProcessTimes=times, WaitForSingleObject=wait_for)
        return native, calls

    def test_live_state_uses_zero_time_wait_not_undefined_exit_filetime(self):
        for undefined_exit in (0, 987654321, 2**64 - 1):
            with self.subTest(exit_time=undefined_exit):
                native, calls = self.process_native(exit_time=undefined_exit)
                self.assertEqual(native.identity(77), (101, False))
                self.assertEqual(calls, [(77, 0)])

    def test_signaled_process_uses_zero_time_wait(self):
        native, calls = self.process_native(wait=0)
        self.assertEqual(native.identity(77), (101, True))
        self.assertEqual(calls, [(77, 0)])

    def test_failed_or_unexpected_process_wait_fails_closed(self):
        for status in (0xFFFFFFFF, 0x80, 1, 259):
            with self.subTest(status=status):
                native, calls = self.process_native(wait=status)
                with self.assertRaisesRegex(ResourceFailure, "process_wait_failed"):
                    native.identity(77)
                self.assertEqual(calls, [(77, 0)])

    def test_open_member_only_adds_synchronize_and_retains_proven_exited_handle(self):
        for wait in (0, 258):
            with self.subTest(wait=wait):
                native, waits = self.process_native(wait=wait)
                opened = []
                native.k.OpenProcess = lambda rights, inherit, pid, opened=opened: (
                    opened.append((rights, inherit, pid)) or 77
                )
                native.belongs = lambda handle, job: handle == 77
                native.close_handle = lambda handle: self.fail("Proven handle must be retained")
                self.assertEqual(native.open_member(11, object()), (77, 101))
                self.assertEqual(opened, [(0x00100410, False, 11)])
                self.assertEqual(waits, [(77, 0)])

    def test_open_member_wait_failure_closes_new_handle_and_refuses(self):
        native, waits = self.process_native(wait=0xFFFFFFFF)
        native.k.OpenProcess = lambda rights, inherit, pid: 77
        native.belongs = lambda handle, job: True
        closed = []
        native.close_handle = closed.append
        with self.assertRaisesRegex(ResourceFailure, "process_wait_failed"):
            native.open_member(11, object())
        self.assertEqual(closed, [77])
        self.assertEqual(waits, [(77, 0)])

    def test_job_backstops_set_and_read_back(self):
        COMMIT_LIMIT, ACTIVE_LIMIT = guard.COMMIT_LIMIT, guard.ACTIVE_LIMIT
        native = self.native()
        saved = []

        def write(handle, kind, pointer, size):
            saved.append(ctypes.string_at(pointer, size))
            return 1

        def read(handle, kind, pointer, size, length):
            ctypes.memmove(pointer, saved[0], size)
            return 1

        native.k = types.SimpleNamespace(
            SetInformationJobObject=write, QueryInformationJobObject=read
        )
        native.parent_outside = lambda job: None
        native.configure(types.SimpleNamespace(handle=100))
        limits = ExtendedLimits.from_buffer_copy(saved[0])
        self.assertEqual(limits.basic.flags, 0x2208)
        self.assertEqual(limits.basic.active, ACTIVE_LIMIT)
        self.assertEqual(limits.job_memory, COMMIT_LIMIT)

    def test_partial_member_list_denied(self):
        native = self.native()

        def read(handle, kind, pointer, size, length):
            value = ProcessIds()
            value.assigned, value.count = 2, 1
            value.pids[0] = 11
            ctypes.memmove(pointer, ctypes.byref(value), ctypes.sizeof(value))
            return 1

        native.k = types.SimpleNamespace(QueryInformationJobObject=read)
        with self.assertRaisesRegex(ResourceFailure, "membership_incomplete"):
            native.members(types.SimpleNamespace(handle=100))

    def tcp_native(self, rows4=(), rows6=(), udp4=(), udp6=(), udp_error=0):
        native = self.native()

        def get_table(buffer, length, sorted_rows, family, table_class, reserved):
            self.assertEqual(table_class, 3)
            rows = rows4 if family == 2 else rows6
            payload = len(rows).to_bytes(4, "little") + b"".join(bytes(row) for row in rows)
            ctypes.cast(length, ctypes.POINTER(ctypes.c_uint32))[0] = len(payload)
            if buffer is None:
                return 122
            ctypes.memmove(buffer, payload, len(payload))
            return 0

        def get_udp_table(buffer, length, sorted_rows, family, table_class, reserved):
            self.assertEqual(table_class, 1)
            if udp_error:
                return udp_error
            rows = udp4 if family == 2 else udp6
            payload = len(rows).to_bytes(4, "little") + b"".join(bytes(row) for row in rows)
            ctypes.cast(length, ctypes.POINTER(ctypes.c_uint32))[0] = len(payload)
            if buffer is None:
                return 122
            ctypes.memmove(buffer, payload, len(payload))
            return 0

        native.ip = types.SimpleNamespace(
            GetExtendedTcpTable=get_table, GetExtendedUdpTable=get_udp_table
        )
        return native

    def row4(self, pid, address):
        row = Tcp4()
        row.state, row.pid = 2, pid
        row.local[:] = address
        row.port = int.from_bytes((9000).to_bytes(2, "big") + b"\0\0", "little")
        return row

    def test_foreign_listener_rows_not_returned(self):
        native = self.tcp_native([self.row4(77, b"\0\0\0\0"), self.row4(11, b"\x7f\0\0\1")])
        self.assertEqual(native.listeners({11}), {(11, 2, b"\x7f\0\0\1", 9000)})

    def test_owned_wildcard_listener_denied(self):
        native = self.tcp_native([self.row4(11, b"\0\0\0\0")])
        with self.assertRaisesRegex(ResourceFailure, "loopback"):
            native.listeners({11})

    def test_owned_ipv6_loopback(self):
        row = Tcp6()
        row.state, row.pid, row.port = 2, 11, 0x2823
        row.local[:] = b"\0" * 15 + b"\1"
        native = self.tcp_native(rows6=[row])
        self.assertEqual(native.listeners({11}), {(11, 23, b"\0" * 15 + b"\1", 9000)})

    def udp_row(self, pid, address):
        row = Udp4() if len(address) == 4 else Udp6()
        row.pid, row.port = pid, 0x2823
        row.local[:] = address
        return row

    def test_foreign_udp_excluded(self):
        native = self.tcp_native(
            udp4=[self.udp_row(77, b"\0" * 4)], udp6=[self.udp_row(88, b"\0" * 16)]
        )
        self.assertEqual(native.listeners({11}), set())

    def test_owned_udp_ipv4_loopback(self):
        native = self.tcp_native(udp4=[self.udp_row(11, b"\x7f\0\0\1")])
        self.assertEqual(native.listeners({11}), {(11, 2, b"\x7f\0\0\1", 9000, "udp")})

    def test_owned_udp_ipv6_loopback(self):
        address = b"\0" * 15 + b"\1"
        native = self.tcp_native(udp6=[self.udp_row(11, address)])
        self.assertEqual(native.listeners({11}), {(11, 23, address, 9000, "udp")})

    def test_owned_udp_wildcard_denied_both_families(self):
        for size in (4, 16):
            with self.subTest(size=size):
                row = self.udp_row(11, b"\0" * size)
                native = self.tcp_native(**({"udp4": [row]} if size == 4 else {"udp6": [row]}))
                with self.assertRaisesRegex(ResourceFailure, "loopback"):
                    native.listeners({11})

    def test_unreadable_udp_fails_closed(self):
        native = self.tcp_native(udp_error=5)
        with self.assertRaisesRegex(ResourceFailure, "resource_evidence_unavailable"):
            native.listeners({11})

    def test_unreadable_udp_second_read_fails_closed_both_families(self):
        for blocked_family in (2, 23):
            with self.subTest(family=blocked_family):
                native = self.tcp_native()
                original = native.ip.GetExtendedUdpTable

                def unreadable(
                    buffer,
                    length,
                    ordered,
                    family,
                    table_class,
                    reserved,
                    blocked_family=blocked_family,
                    original=original,
                ):
                    if buffer is not None and family == blocked_family:
                        return 5
                    return original(buffer, length, ordered, family, table_class, reserved)

                native.ip.GetExtendedUdpTable = unreadable
                with self.assertRaisesRegex(ResourceFailure, "resource_evidence_unavailable"):
                    native.listeners({11})

    def test_tcp_and_udp_same_address_remain_distinct(self):
        address = b"\x7f\0\0\1"
        native = self.tcp_native([self.row4(11, address)], udp4=[self.udp_row(11, address)])
        self.assertEqual(len(native.listeners({11})), 2)

    def test_cleanup_failure_remains_failure_on_second_close(self):
        case = AdapterTests()
        owner = case.make()
        owner.sample()
        case.native.fail = "listener_cleanup"
        with self.assertRaises(ResourceFailure):
            owner.close()
        with self.assertRaises(ResourceFailure):
            owner.close()


class QualificationPolicyTests(unittest.TestCase):
    def sample(self):
        return dict(
            logical_cpus=4,
            physical_bytes=8 * GIB,
            available_bytes=5 * GIB,
            free_disk_bytes=12 * GIB,
            host_cpu_percent=10,
            rss_bytes=100,
            active_processes=2,
            cpu_seconds=1,
            sample_age_seconds=0.1,
            loopback_only=True,
        )

    def receipt(self):
        return dict(
            source_sha="a" * 40,
            profile_sha256="b" * 64,
            workflow_sha256="c" * 64,
            run_id="1",
            attempt="1",
            completed=True,
            cleanup_complete=True,
            owned_active=0,
            owned_listeners=0,
            peak_rss_bytes=100,
            cpu_seconds=2,
            stage_count=6,
        )

    def test_valid_proposed_admission(self):
        guard.admit_sample(self.sample(), initial=True)

    def test_resource_refusals(self):
        changes = dict(
            logical_cpus=1,
            physical_bytes=5 * GIB,
            available_bytes=GIB,
            free_disk_bytes=GIB,
            host_cpu_percent=90,
            rss_bytes=2 * GIB + 1,
            active_processes=33,
            cpu_seconds=901,
            sample_age_seconds=2,
            loopback_only=False,
        )
        for name, value in changes.items():
            with self.subTest(field=name), self.assertRaises(guard.Refusal):
                guard.admit_sample({**self.sample(), name: value}, initial=True)

    def test_missing_or_nonfinite_measurement(self):
        for value in (None, True, float("nan"), float("inf"), -1):
            with self.subTest(value=value), self.assertRaises(guard.Refusal):
                guard.admit_sample({**self.sample(), "rss_bytes": value})
        sample = self.sample()
        del sample["available_bytes"]
        with self.assertRaises(guard.Refusal):
            guard.admit_sample(sample)

    def test_cumulative_cpu_is_charged(self):
        with self.assertRaisesRegex(guard.Refusal, "cpu_limit"):
            guard.admit_sample(self.sample(), previous_cpu=900)

    def test_receipt_incompleteness_is_failure(self):
        guard.receipt_valid(self.receipt())
        for field, value in {
            "completed": False,
            "cleanup_complete": False,
            "owned_active": 1,
            "owned_listeners": 1,
            "stage_count": 0,
            "source_sha": "wrong",
            "cpu_seconds": 901,
        }.items():
            with self.subTest(field=field), self.assertRaises(guard.Refusal):
                guard.receipt_valid({**self.receipt(), field: value})

    def test_wheel_hash_size_and_extra_files_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wheel = root / "fixture.whl"
            wheel.write_bytes(b"known")
            records = [
                dict(filename=wheel.name, size=5, sha256=hashlib.sha256(b"known").hexdigest())
            ]
            guard.validate_wheelhouse(root, records)
            wheel.write_bytes(b"drift")
            with self.assertRaises(guard.Refusal):
                guard.validate_wheelhouse(root, records)
            wheel.write_bytes(b"known")
            (root / "extra.whl").write_bytes(b"extra")
            with self.assertRaises(guard.Refusal):
                guard.validate_wheelhouse(root, records)

    def test_duplicate_json_fields_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "record.json"
            path.write_text('{"key":1,"key":2}')
            with self.assertRaisesRegex(guard.Refusal, "duplicate_field"):
                guard.load_json(path)

    def test_script_admission_exact_body_and_ordinary_temp_path(self):
        steps = guard.workflow_steps((ROOT / guard.PROFILES["alpha"]).read_text())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "11111111-2222-3333-4444-555555555555.ps1"
            wrapped = guard.POWERSHELL_PREFIX + steps[1]["body"] + guard.POWERSHELL_SUFFIX
            path.write_bytes(wrapped.replace("\n", "\r\n").encode())
            self.assertEqual(guard.admitted_body("alpha", 1, path, root)["name"], steps[1]["name"])
            path.write_text(steps[1]["body"] + "Write-Output extra\n")
            with self.assertRaisesRegex(guard.Refusal, "step_body"):
                guard.admitted_body("alpha", 1, path, root)
            with self.assertRaises(guard.Refusal):
                guard.admitted_body("alpha", -1, path, root)
            with self.assertRaises(guard.Refusal):
                guard.admitted_body("alpha", 999, path, root)

    def test_shell_fields_obey_authoritative_literal_string_schema(self):
        # actions/runner5f0b3ddc workflow schema blobc51b9f66e995edea6896f7c18b3337e2271d0942:
        # run-step.shell -> non-empty-string, no context on this field or its ancestors.
        schema = {"string": {"require-non-empty": True}}
        self.assertNotIn("context", schema)

        def admitted(value):
            return isinstance(value, str) and bool(value) and "${{" not in value

        self.assertFalse(admitted("${{ env.K5_BOOTSTRAP_PYTHON }} -I guard.py"))
        for workflow in guard.PROFILES.values():
            for line in (ROOT / workflow).read_text().splitlines():
                if line.strip().startswith("shell:"):
                    self.assertTrue(admitted(line.split("shell:", 1)[1].strip()), line)

    def test_literal_launcher_format_and_materialization(self):
        for profile, workflow in guard.PROFILES.items():
            text = (ROOT / workflow).read_text()
            shells = [
                line.strip().split("shell: ", 1)[1]
                for line in text.splitlines()
                if "shell: powershell -NoProfile -NonInteractive -Command" in line
            ]
            self.assertEqual(len(shells), 6 if profile == "alpha" else 9)
            for index, shell in enumerate(shells, 1):
                path = "C:\\runner temp\\11111111-2222-3333-4444-555555555555.ps1"
                # Runner uses C# string.Format: literal scriptblock braces must be doubled.
                materialized = shell.format(path)
                self.assertNotIn("{{", materialized)
                self.assertIn("{ throw 'Cached bootstrap changed' }", materialized)
                self.assertIn(f"--profile {profile} --step {index} '{path}'", materialized)
                self.assertEqual(materialized.count(path), 1)
                self.assertNotIn(". '" + path, materialized)
                self.assertIn("& $env:K5_BOOTSTRAP_PYTHON -I -B -S", materialized)
                self.assertIn("Get-FileHash", materialized)
                self.assertLess(
                    materialized.index("Get-FileHash"),
                    materialized.index("& $env:K5_BOOTSTRAP_PYTHON"),
                )
                self.assertTrue(materialized.endswith('exit $LASTEXITCODE"'))
            self.assertIn("Runner temporary path cannot be represented safely", text)

    def test_original_executable_bodies_are_preserved(self):
        profile = guard.load_json(ROOT / "scripts/qualification/windows-hosted-profile.json")
        check = 'if ($LASTEXITCODE -ne 0) { throw "Qualification dependency command failed." }\n'
        for kind, workflow in guard.PROFILES.items():
            steps = {r["name"]: r for r in guard.workflow_steps((ROOT / workflow).read_text())}
            for original in profile["original_steps"][kind]:
                row = steps[original["name"]]
                body = row["body"]
                if kind == "alpha" and original["name"] in {
                    "Install targeted test dependencies",
                    "Verify clean Windows alpha runtime dependency install",
                }:
                    body = body.replace(check, "")
                self.assertEqual(hashlib.sha256(body.encode()).hexdigest(), original["body_sha256"])
                self.assertEqual(row["timeout"], original["timeout"])
                self.assertEqual(row["always"], original["always"])

    def test_workflows_have_one_real_guard_per_original_run_step(self):
        for kind, workflow in guard.PROFILES.items():
            text = (ROOT / workflow).read_text()
            self.assertEqual(text.count("runs-on:"), 1)
            self.assertIn("timeout-minutes: 15", text)
            self.assertIn("Python\\3.12.10\\x64", text)
            self.assertNotIn("actions/setup-python@", text)
            self.assertIn("shell: powershell -NoProfile -NonInteractive -Command", text)
            self.assertNotIn("cache: pip", text)
            for index, row in enumerate(guard.workflow_steps(text)):
                if "scripts/windows_hosted_qualification.py" in row["body"]:
                    continue
                self.assertIn(f"--profile {kind} --step {index} '{{0}}'", text)

    def test_powershell_wrappers_and_dot_sourcing_are_explicit(self):
        self.assertEqual(guard.POWERSHELL_PREFIX, "$ErrorActionPreference = 'stop'\n")
        self.assertIn("variable:\\LASTEXITCODE", guard.POWERSHELL_SUFFIX)
        source = (ROOT / "scripts/windows_hosted_qualification.py").read_text()
        self.assertIn('[str(shell), "-command", ". \'"', source)
        self.assertNotIn('"-ExecutionPolicy", "Unrestricted"', source)

    def test_existing_source_contract_functions_unchanged(self):
        # Execute only these original pure source assertions, with no pytest import,
        # fixtures, native functions, or rewritten assertion bodies.
        cases = {
            "test_windows_alpha_analytics.py": (
                "test_windows_smoke_selects_analytics_and_preserves_lifetime_coverage"
            ),
            "test_app_resource_lifetime.py": (
                "test_existing_windows_smoke_qualifies_exact_app_lifetime_paths"
            ),
            "test_installed_alpha_facade_witness.py": (
                "test_hosted_workflow_selects_facade_contracts_and_only_nonmedia_observer"
            ),
            "test_installed_analytics_candidate_workflow.py": (
                "test_new_windows_cases_are_added_without_replacing_existing_selection"
            ),
            "test_windows_alpha_preflight_boundary.py": (
                "test_hosted_reproduction_paths_cannot_trigger_native_candidate_job"
            ),
        }
        for filename, name in cases.items():
            with self.subTest(file=filename):
                path = ROOT / "tests" / filename
                module = ast.parse(path.read_text())
                node = next(
                    n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == name
                )
                namespace = {"ROOT": ROOT, "Path": Path, "__file__": str(path)}
                exec(
                    compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), namespace
                )
                namespace[name]()

    def test_runtime_profile_preserves_all27_version_pins(self):
        profile = guard.load_json(ROOT / "scripts/qualification/windows-hosted-profile.json")
        original = {
            line.split("==")[0]: line.split("==")[1]
            for line in (ROOT / "scripts/windows-alpha/runtime-requirements.txt")
            .read_text()
            .splitlines()
            if line and not line.startswith("#")
        }
        actual = {r["name"]: r["version"] for r in profile["wheels"]}
        self.assertEqual(len(original), 27)
        self.assertTrue(original.items() <= actual.items())
        self.assertEqual(len(actual), 45)
        self.assertLessEqual(sum(r["size"] for r in profile["wheels"]), 40 * 1024**2)

    def test_terminal_cpu_not_last_sample_is_accumulated(self):
        clock = [0.0]
        instances = []

        class Resources:
            def __init__(self, *args):
                instances.append(self)
                self.last_sample_at = 0.0
                self.final_cpu_seconds = 3
                self.final_active = 0
                self.cleanup_complete = True
                self.observed_peak_rss_bytes = 0

            def job_factory(self):
                return object()

            def sample(self):
                if clock[0] - self.last_sample_at < 0.25:
                    raise AssertionError("host CPU sample has zero accounting interval")
                self.last_sample_at = clock[0]
                self.observed_peak_rss_bytes = max(self.observed_peak_rss_bytes, 100)
                return types.SimpleNamespace(
                    started_at=time.monotonic(),
                    host_total_bytes=8 * GIB,
                    host_available_bytes=5 * GIB,
                    disk_free_bytes=12 * GIB,
                    host_cpu_percent=10,
                    rss_bytes=100,
                    active_processes=0,
                    job_cpu_seconds=1,
                )

            def assert_fresh(self, item):
                pass

            def close(self):
                pass

        class Owned:
            def __init__(self, *args, **kwargs):
                instances[-1].before_release(instances[-1])
                self.process = types.SimpleNamespace(poll=lambda: 0, returncode=0)

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as temporary:
            state = dict(
                cpu_seconds=2, peak_rss_bytes=0, started_at=time.time(), all_owned_cleanup=True
            )
            with (
                patch.object(guard, "QualificationResources", Resources),
                patch.object(
                    guard, "common_module", return_value=types.SimpleNamespace(OwnedProcess=Owned)
                ),
                patch.object(
                    guard.time,
                    "sleep",
                    side_effect=lambda delta: clock.__setitem__(0, clock[0] + delta),
                ),
            ):
                guard.run_owned(
                    ["never-executed"],
                    cwd=ROOT,
                    env={},
                    work=Path(temporary),
                    state=state,
                    seconds=1,
                )
            self.assertEqual(state["cpu_seconds"], 5)
            self.assertFalse(state["pending_payload"])

    def test_run_owned_preserves_peak_when_sampling_admission_or_cleanup_fails(self):
        for failure in ("sampling", "admission", "cleanup"):
            for previous_peak in (100, 4 * GIB):
                with self.subTest(failure=failure, previous_peak=previous_peak):
                    observed_peak = 2 * GIB + 1 if failure == "admission" else 700
                    instances = []

                    class Resources:
                        def __init__(self, *args, instances=instances):
                            instances.append(self)
                            self.observed_peak_rss_bytes = 0
                            self.final_cpu_seconds = 1
                            self.final_active = 0
                            self.cleanup_complete = True

                        def job_factory(self):
                            return object()

                        def close(self, failure=failure):
                            if failure == "cleanup":
                                self.cleanup_complete = False
                                raise ResourceFailure("owned_job_cleanup_failed")

                    class Owned:
                        def __init__(self, *args, instances=instances, **kwargs):
                            instances[-1].before_release(instances[-1])
                            self.process = types.SimpleNamespace(poll=lambda: 0, returncode=0)

                        def close(self):
                            pass

                    def sample(resources, failure=failure, observed_peak=observed_peak):
                        resources.observed_peak_rss_bytes = max(
                            resources.observed_peak_rss_bytes, observed_peak
                        )
                        if failure == "sampling":
                            raise ResourceFailure("resource_evidence_unavailable")
                        result = self.sample()
                        result["rss_bytes"] = observed_peak
                        return result

                    with tempfile.TemporaryDirectory() as temporary:
                        state = dict(
                            cpu_seconds=0,
                            peak_rss_bytes=previous_peak,
                            started_at=time.time(),
                            all_owned_cleanup=True,
                        )
                        with (
                            patch.object(guard, "QualificationResources", Resources),
                            patch.object(
                                guard,
                                "common_module",
                                return_value=types.SimpleNamespace(OwnedProcess=Owned),
                            ),
                            patch.object(guard, "sample_values", side_effect=sample),
                            patch.object(guard.time, "sleep"),
                            self.assertRaises((ResourceFailure, guard.Refusal)),
                        ):
                            guard.run_owned(
                                ["never-executed"],
                                cwd=ROOT,
                                env={},
                                work=Path(temporary),
                                state=state,
                                seconds=1,
                            )
                        self.assertEqual(state["peak_rss_bytes"], max(previous_peak, observed_peak))


class CacheBootstrapTests(unittest.TestCase):
    def test_cached_present_absent_partial_version_arch_and_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            base = cache / "Python/3.12.10/x64"
            base.mkdir(parents=True)
            marker = base.parent / "x64.complete"
            marker.touch()
            (base / "LICENSE.txt").write_text("inert license fixture")
            payload = bytearray(256)
            payload[:2] = b"MZ"
            payload[0x3C:0x40] = (0x80).to_bytes(4, "little")
            payload[0x80:0x86] = b"PE\0\0\x64\x86"
            payload[0x98:0x9A] = b"\x0b\x02"
            exe, dll = base / "python.exe", base / "python312.dll"
            exe.write_bytes(payload)
            dll.write_bytes(payload)
            kwargs = dict(version=(3, 12, 10), bits=64, loaded_dll=dll)

            def validate(**changes):
                guard.validate_bootstrap_cache(
                    cache, exe, expected, expected, **{**kwargs, **changes}
                )

            expected = guard.digest(exe)
            validate()
            for changes in ({"version": (3, 12, 9)}, {"bits": 32}, {"loaded_dll": exe}):
                with self.assertRaises(guard.Refusal):
                    validate(**changes)
            marker.unlink()
            with self.assertRaises(guard.Refusal):
                validate()
            marker.touch()
            dll.unlink()
            with self.assertRaises(guard.Refusal):
                validate()
            dll.write_bytes(payload)
            dll.write_bytes(bytes(payload) + b"changed")
            with self.assertRaisesRegex(guard.Refusal, "bootstrap_identity"):
                validate()
            dll.write_bytes(payload)
            (base / "python312._pth").write_text("unreviewed")
            with self.assertRaisesRegex(guard.Refusal, "bootstrap_path_hook"):
                validate()
            (base / "python312._pth").unlink()
            payload[0x84:0x86] = b"\x4c\x01"
            exe.write_bytes(payload)
            with self.assertRaisesRegex(guard.Refusal, "bootstrap_pe"):
                validate()

    def test_bootstrap_selector_is_cache_only_and_binds_loaded_dll(self):
        bodies = []
        for workflow in guard.PROFILES.values():
            text = (ROOT / workflow).read_text()
            body = guard.workflow_steps(text)[0]["body"]
            bodies.append(body.split("--prepare")[0])
            for required in (
                "x64.complete",
                "python312.dll",
                "LICENSE.txt",
                "ReparsePoint",
                "ProductVersion",
                "FileVersion",
                "0x8664",
                "0x20b",
                "K5_BOOTSTRAP_DLL_SHA256",
                "Get-FileHash",
            ):
                self.assertIn(required, body)
            for excluded in (
                "actions/setup-python",
                "Invoke-WebRequest",
                "setup.ps1",
                "pip install",
                "GITHUB_PATH",
            ):
                self.assertNotIn(excluded, body)
            self.assertNotIn("shell: python -", text)
        self.assertEqual(bodies[0], bodies[1])


class ReviewRegressionTests(unittest.TestCase):
    def test_child_environment_drops_unreviewed_hooks_and_credentials(self):
        with patch.dict(
            guard.os.environ,
            {
                "PATH": "unreviewed",
                "AWS_SECRET_ACCESS_KEY": "mock-secret",
                "PSModulePath": "hook",
                "PYTHONSTARTUP": "hook",
                "GITHUB_TOKEN": "mock-token",
                "K5_UNREVIEWED": "value",
                "SYSTEMROOT": "C:/Windows",
            },
            clear=True,
        ):
            env = guard.child_environment(
                Path("owned"),
                {
                    "host_temp": "temp",
                    "tool_directories": ["reviewed"],
                    "bootstrap_path": "bootstrap",
                    "bootstrap_sha256": "hash",
                },
            )
        self.assertFalse(
            {
                "AWS_SECRET_ACCESS_KEY",
                "PSModulePath",
                "PYTHONSTARTUP",
                "GITHUB_TOKEN",
                "K5_UNREVIEWED",
            }
            & env.keys()
        )
        self.assertNotIn("unreviewed", env["PATH"])

    def test_runtime_inventory_rejects_changed_and_extra_files(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            (work / "python").mkdir()
            file = work / "python/python.exe"
            file.write_bytes(b"mock data only")
            spec = {
                "runtime_files": {
                    "python/python.exe": {
                        "bytes": file.stat().st_size,
                        "sha256": guard.digest(file),
                    }
                }
            }
            guard.verify_runtime(work, spec)
            extra = work / "python/extra.pth"
            extra.write_text("hook")
            with self.assertRaisesRegex(guard.Refusal, "runtime_extra_file"):
                guard.verify_runtime(work, spec)
            extra.unlink()
            file.write_bytes(b"tampered")
            with self.assertRaisesRegex(guard.Refusal, "runtime_payload"):
                guard.verify_runtime(work, spec)

    def test_secondary_seals_can_be_created_once_never_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            file = work / "runtime-dependencies.json"
            file.write_text("inert original")
            state = {}
            guard.verify_secondary_seals(work, state, initialize=True)
            original = dict(state)
            file.write_text("changed matching metadata")
            for initialize in (False, True):
                with self.assertRaisesRegex(guard.Refusal, "secondary_seal_drift"):
                    guard.verify_secondary_seals(work, state, initialize=initialize)
                self.assertEqual(state, original)

    def test_venv_binding_refuses_config_and_redirector_before_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            (work / "python").mkdir()
            prefix = work / "venv"
            (prefix / "Scripts").mkdir(parents=True)
            files = {}
            for name in ("python.exe", "pythonw.exe"):
                path = prefix / "Scripts" / name
                path.write_bytes(b"inert redirector fixture")
                files["python/Lib/venv/scripts/nt/" + name] = {
                    "bytes": path.stat().st_size,
                    "sha256": guard.digest(path),
                }
            config = prefix / "pyvenv.cfg"
            config.write_text(
                f"home = {work / 'python'}\n"
                "include-system-site-packages = false\nversion = 3.12.15\n"
            )
            original = guard.load_json

            def load(path):
                return (
                    {"runtime_files": files}
                    if path.name == "windows-hosted-profile.json"
                    else original(path)
                )

            with patch.object(guard, "load_json", side_effect=load):
                guard.verify_venv_binding(prefix, work)
                (work / "dependencies.json").write_text(
                    json.dumps({"pyvenv.cfg": guard.digest(config)})
                )
                config.write_text(config.read_text() + "prompt = unexpected\n")
                with self.assertRaisesRegex(guard.Refusal, "venv_config_seal"):
                    guard.verify_venv_binding(prefix, work)
                config.write_text(config.read_text().replace("false", "true"))
                with self.assertRaisesRegex(guard.Refusal, "venv_base"):
                    guard.verify_venv_binding(prefix, work)
            with (
                patch.object(
                    guard, "verify_venv_binding", side_effect=guard.Refusal("venv_redirector")
                ),
                patch.object(guard.subprocess, "run") as execute,
                self.assertRaisesRegex(guard.Refusal, "venv_redirector"),
            ):
                guard.verify_environment_process(
                    prefix / "Scripts/python.exe", {"K5_QUAL_ROOT": str(work)}
                )
            execute.assert_not_called()

    def test_expired_job_still_cleans_owned_root_and_refuses_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work = root / "owned"
            work.mkdir()
            state = dict(
                prepared=True,
                failed=False,
                finished_steps=[0],
                started_at=time.time() - 901,
                all_owned_cleanup=True,
                pending_payload=False,
                source_sha="a" * 40,
                profile_sha256="b" * 64,
                workflow_sha256="c" * 64,
                run_id="1",
                attempt="1",
                peak_rss_bytes=1,
                cpu_seconds=1,
            )
            workflow = root / guard.PROFILES["alpha"]
            workflow.parent.mkdir(parents=True)
            workflow.write_text("inert workflow fixture")
            with (
                patch.object(guard, "ROOT", root),
                patch.object(guard, "read_state", return_value=(work, state)) as read,
                patch.object(guard, "workflow_steps", return_value=[{"body": "mock"}]),
                self.assertRaises(guard.Refusal),
            ):
                guard.finish("alpha")
            read.assert_called_once_with("alpha", cleanup=True)
            self.assertFalse(work.exists())
            receipt = json.loads((root / "hosted-alpha-qualification.json").read_text())
            self.assertFalse(receipt["completed"])
            self.assertTrue(receipt["cleanup_complete"])

    def test_owned_bytecode_is_removed_but_sourceless_cache_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "module.py").write_text("# inert fixture")
            (root / "__pycache__").mkdir()
            cache = root / "__pycache__/module.cpython-312.pyc"
            cache.write_bytes(b"disposable mock cache")
            guard.purge_owned_bytecode(root)
            self.assertFalse(cache.exists())
            self.assertTrue((root / "module.py").exists())
            (root / "__pycache__/missing.cpython-312.pyc").write_bytes(b"unbound")
            with self.assertRaises((guard.Refusal, OSError)):
                guard.purge_owned_bytecode(root)

    def test_verified_archive_members_fit_explicit_extraction_limits(self):
        profile = guard.load_json(guard.PROFILE_ROOT / "windows-hosted-profile.json")
        sizes = [entry["bytes"] for entry in profile["runtime_files"].values()]
        self.assertGreater(max(sizes), 16 * 1024**2)
        self.assertLessEqual(max(sizes), 32 * 1024**2)
        self.assertLessEqual(sum(sizes), 192 * 1024**2)

    def test_source_revision_dirty_and_untracked_refusals(self):
        state = {"tools": {"git": {"path": "mock-git", "sha256": "hash"}}, "source_sha": "a" * 40}
        cases = [
            ([types.SimpleNamespace(stdout="b" * 40)], "source_revision"),
            (
                [types.SimpleNamespace(stdout="a" * 40), types.SimpleNamespace(returncode=1)],
                "source_dirty",
            ),
            (
                [
                    types.SimpleNamespace(stdout="a" * 40),
                    types.SimpleNamespace(returncode=0),
                    types.SimpleNamespace(stdout=b"src/extra.py"),
                ],
                "source_untracked",
            ),
        ]
        for outputs, expected in cases:
            with (
                self.subTest(expected=expected),
                patch.object(guard, "ordinary", side_effect=lambda x: x),
                patch.object(guard, "digest", return_value="hash"),
                patch.object(guard.subprocess, "run", side_effect=outputs),
                self.assertRaisesRegex(guard.Refusal, expected),
            ):
                guard.verify_source(state)

    def test_pinned_editable_hook_requires_exact_name_and_source_path(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            prefix = work / "venv"
            site = prefix / "Lib/site-packages"
            metadata = site / "k5_vision-0.1.0.dist-info"
            metadata.mkdir(parents=True)
            (prefix / "Scripts").mkdir()
            (prefix / "pyvenv.cfg").write_text("inert mock configuration")
            (metadata / "METADATA").write_text("Name: k5-vision\nVersion: 0.1.0\n")
            hook = site / "_editable_impl_k5_vision.pth"
            expected = str(guard.ROOT / "src")
            hook.write_text(expected)
            original = guard.load_json

            def load(path):
                return (
                    {"wheels": []} if path.name == "windows-hosted-profile.json" else original(path)
                )

            with (
                patch.dict(guard.os.environ, {"K5_QUAL_ROOT": str(work)}),
                patch.object(guard, "load_json", side_effect=load),
            ):
                guard.verify_environment(prefix)
                for content in (str(work / "other"), "import malicious_hook", expected + "\nother"):
                    hook.write_text(content)
                    with (
                        self.subTest(content=content),
                        self.assertRaisesRegex(guard.Refusal, "project_editable_path"),
                    ):
                        guard.verify_environment(prefix)
                hook.write_text(expected)
                for name in ("_k5_vision.pth", "unexpected.pth", "_editable_impl_k5_vision.py"):
                    extra = site / name
                    extra.write_text(expected)
                    with (
                        self.subTest(name=name),
                        self.assertRaisesRegex(guard.Refusal, "dependency_extra_file"),
                    ):
                        guard.verify_environment(prefix)
                    extra.unlink()
                guard.verify_environment(prefix)

    def test_wheel_bytes_and_extra_startup_hook_are_authenticated(self):
        import zipfile

        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            prefix = work / "venv"
            site = prefix / "Lib/site-packages"
            (site / "example-1.0.dist-info").mkdir(parents=True)
            (prefix / "Scripts").mkdir()
            (prefix / "pyvenv.cfg").write_text("inert mock configuration")
            (work / "wheels").mkdir()
            payload = {
                "example.py": b"# inert test fixture\n",
                "example-1.0.dist-info/METADATA": b"Name: example\nVersion: 1.0\n",
                "example-1.0.dist-info/RECORD": b"mock record\n",
            }
            wheel = work / "wheels/example.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                for name, contents in payload.items():
                    archive.writestr(name, contents)
                    (site / name).write_bytes(contents)
            specification = {
                "wheels": [
                    {
                        "name": "example",
                        "version": "1.0",
                        "filename": wheel.name,
                        "size": wheel.stat().st_size,
                        "sha256": guard.digest(wheel),
                    }
                ]
            }
            original = guard.load_json

            def load(path):
                return (
                    specification if path.name == "windows-hosted-profile.json" else original(path)
                )

            with (
                patch.dict(guard.os.environ, {"K5_QUAL_ROOT": str(work)}),
                patch.object(guard, "load_json", side_effect=load),
            ):
                guard.verify_environment(prefix)
                (site / "example.py").write_bytes(b"changed")
                (site / "example-1.0.dist-info/RECORD").write_bytes(b"rewritten matching record")
                with self.assertRaisesRegex(guard.Refusal, "dependency_bytes"):
                    guard.verify_environment(prefix)
                for name, contents in payload.items():
                    (site / name).write_bytes(contents)
                (site / "unexpected.pth").write_text("hook")
                with self.assertRaisesRegex(guard.Refusal, "dependency_extra_file"):
                    guard.verify_environment(prefix)


if __name__ == "__main__":
    unittest.main()
