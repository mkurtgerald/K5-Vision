"""Portable fake API tests only. Never loads Windows DLLs or invokes a real Job."""

import ast
import contextlib
import ctypes
import hashlib
import importlib.util
import io
import json
import subprocess
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


def association_state(total=0, observed=0, *, complete=True):
    return dict(
        evidence_mode=guard.EVIDENCE_MODE,
        association_counts_complete=complete,
        job_total_associations=total,
        fully_live_observed_associations=observed,
        unobserved_or_rejected_associations=total - observed,
    )


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
        with self.assertRaisesRegex(ResourceFailure, "initial_active_count_mismatch"):
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
        owner.sample()
        self.clock.value += 0.25
        self.native.identities[11] = (101, 222)
        with self.assertRaisesRegex(ResourceFailure, "departed_member_still_listed"):
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
        with self.assertRaisesRegex(ResourceFailure, "initial_members_exceed_limit"):
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

    def make_witnessed(self):
        owner = self.make()
        owner.sample()
        self.clock.value += 0.25
        self.native.read_count = 0
        return owner

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

    def test_newly_discovered_proved_exit_before_rss_is_incomplete_coverage(self):
        owner = self.make()
        original = self.native.open_member

        def opened(pid, job):
            value = original(pid, job)
            if pid == 11 and pid in self.native.pids:
                self.expire(pid)
            return value

        self.native.open_member = opened
        snapshot = owner.sample()
        self.assertEqual((snapshot.active_processes, snapshot.rss_bytes), (1, 100))
        self.assertEqual(owner.witnessed_births, {(12, 102)})
        self.assertEqual(owner.incomplete_exit_births, {(11, 101)})
        owner.close()
        self.assertTrue(owner.cleanup_complete)

    def test_previously_witnessed_exit_before_rss_can_stabilize(self):
        owner = self.make_witnessed()
        original = self.native.identity
        first = True

        def identity(handle):
            nonlocal first
            if handle == 11 and first:
                first = False
                self.expire(handle)
            return original(handle)

        self.native.identity = identity
        snapshot = owner.sample()
        self.assertEqual((snapshot.active_processes, snapshot.rss_bytes), (1, 100))
        self.assertEqual(self.native.read_count, 4)
        self.assertEqual(set(owner.handles), {11, 12})
        owner.close()
        self.assertEqual(self.native.closed_handles, [11, 11, 12])

    def test_exit_during_rss_preserves_observed_high_water(self):
        owner = self.make_witnessed()
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
        owner = self.make_witnessed()
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

    def test_exit_after_identity_pass_requires_retained_exit_proof_and_fresh_retry(self):
        owner = self.make()
        original = self.native.members
        calls = []

        def members(job):
            calls.append(1)
            if len(calls) == 2:
                self.expire()
            return original(job)

        self.native.members = members
        snapshot = owner.sample()
        self.assertEqual((snapshot.active_processes, snapshot.rss_bytes), (1, 200))
        self.assertEqual(len(calls), 4)
        self.assertEqual(owner.witnessed_births, {(11, 101), (12, 102)})
        self.assertEqual(owner.incomplete_exit_births, set())
        owner.close()
        self.assertTrue(self.job.drained)

    def test_survivor_exit_during_final_state_recheck_requires_fresh_stable_retry(self):
        owner = self.make()
        original = self.native.identity
        reads = {11: 0, 12: 0}

        def identity(handle):
            reads[handle] += 1
            if handle == 11 and reads[handle] == 3:
                self.expire(handle)
            return original(handle)

        self.native.identity = identity
        snapshot = owner.sample()
        self.assertEqual(snapshot.active_processes, 1)
        self.assertEqual(self.native.read_count, 4)
        self.assertIn((11, 101), owner.absent_births)

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

    def test_permanent_unexplained_departure_exhausts_without_success(self):
        owner = self.make()

        def changed(number, rows):
            self.native.pids.discard(11)
            return rows

        calls = self.during_listeners(changed)
        with self.assertRaisesRegex(ResourceFailure, "final_survivor_missing"):
            owner.sample()
        self.assertEqual(len(calls), 8)

    def test_unobserved_birth_is_disclosed_but_current_member_requires_count_conservation(self):
        for total_increase, new_pid in ((True, False), (False, True)):
            with self.subTest(total_increase=total_increase, new_pid=new_pid):
                owner = self.make_witnessed()

                def changed(number, rows, total_increase=total_increase, new_pid=new_pid):
                    if number != 1:
                        return rows
                    self.expire()
                    if total_increase:
                        self.native.total = 3
                        self.job.total_processes = 3
                    if new_pid:
                        self.native.pids.add(13)
                        self.native.identities[13] = (103, False)
                    return rows

                calls = self.during_listeners(changed)
                if total_increase:
                    self.assertEqual(owner.sample().total_births, 3)
                    owner.close()
                    state = association_state()
                    guard.accumulate_associations(state, owner)
                    self.assertEqual(state, association_state(3, 2))
                else:
                    with self.assertRaisesRegex(ResourceFailure, "association_count_underflow"):
                        owner.sample()
                    self.assertFalse(owner.association_counts_complete)
                self.assertEqual(len(calls), 2 if total_increase else 3)

    def test_reappearing_proved_departure_never_retries(self):
        owner = self.make_witnessed()
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
        with self.assertRaisesRegex(ResourceFailure, "retry_member_set_expanded"):
            owner.sample()
        self.assertEqual(len(calls), 1)

    def test_eight_changing_attempts_exhaust_without_returning_mixed_snapshot(self):
        owner = self.make()
        self.native.pids = set(range(11, 19))
        self.native.identities = {pid: (pid + 90, False) for pid in self.native.pids}
        self.native.total = self.job.total_processes = 8
        owner.sample()
        self.clock.value += 0.25
        self.native.read_count = 0

        def changed(number, rows):
            self.expire(10 + number)
            return rows

        calls = self.during_listeners(changed)
        with self.assertRaisesRegex(ResourceFailure, "resource_evidence_unstable"):
            owner.sample()
        self.assertEqual(len(calls), 8)
        self.assertEqual(self.native.read_count, 16)
        self.assertTrue(owner.cleanup_complete)

    def test_original_deadline_is_not_reset_between_attempts(self):
        owner = self.make_witnessed()

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
        owner = self.make_witnessed()
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
        owner = self.make_witnessed()

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
        owner = self.make_witnessed()
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
        with self.assertRaisesRegex(ResourceFailure, "job_accounting_regressed"):
            owner.sample()
        self.assertEqual(len(calls), 1)
        self.assertEqual(owner.last_cpu, 2.5)

    def test_cumulative_cpu_limit_survives_reconciled_departure(self):
        owner = self.make_witnessed()

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


class BirthLedgerFakeTests(unittest.TestCase):
    """Lifetime association counts require actual, completed live observations."""

    make = AdapterTests.make
    make_witnessed = StableSnapshotFakeTests.make_witnessed
    expire = StableSnapshotFakeTests.expire
    during_listeners = StableSnapshotFakeTests.during_listeners

    def birth(self, pid=13, birth=103):
        self.native.pids.add(pid)
        self.native.identities[pid] = (birth, False)
        self.native.total += 1
        self.job.total_processes = self.native.total

    def test_initial_empty_job_and_then_live_root_are_fully_charged(self):
        owner = self.make()
        self.native.pids = set()
        self.native.total = self.job.total_processes = 0
        self.assertEqual(owner.sample().total_births, 0)
        self.assertEqual(owner.witnessed_births, set())
        self.clock.value += 0.25
        self.birth(11, 101)
        snapshot = owner.sample()
        self.assertEqual(snapshot.total_births, 1)
        self.assertEqual(owner.witnessed_births, {(11, 101)})
        owner.close()
        self.assertTrue(owner.birth_evidence_complete and owner.cleanup_complete)

    def test_matched_live_birth_between_polls_is_fully_observed(self):
        owner = self.make_witnessed()
        self.birth()
        snapshot = owner.sample()
        self.assertEqual(snapshot.total_births, 3)
        self.assertEqual(snapshot.active_processes, 3)
        self.assertEqual(owner.witnessed_births, {(11, 101), (12, 102), (13, 103)})
        self.assertEqual(self.native.read_count, 2)

    def test_closing_live_birth_needs_rss_listeners_and_stable_resample(self):
        owner = self.make_witnessed()
        observed_rss = []
        original_rss = self.native.rss

        def rss(handle):
            observed_rss.append(handle)
            return original_rss(handle)

        def changed(number, rows):
            if number == 1:
                self.birth()
            elif number == 2:
                self.assertIn(13, observed_rss)
                self.assertNotIn((13, 103), owner.witnessed_births)
                return {(13, 2, b"\x7f\0\0\1", 9000)}
            return rows

        self.native.rss = rss
        calls = self.during_listeners(changed)
        snapshot = owner.sample()
        self.assertEqual(self.native.read_count, 4)
        self.assertEqual(calls, [{11, 12}, {11, 12, 13}, {11, 12, 13}])
        self.assertEqual(snapshot.rss_bytes, 300)
        self.assertEqual(snapshot.owned_loopback_listeners, 1)
        self.assertEqual(snapshot.total_births, len(owner.witnessed_births))
        self.assertIn((13, 103), owner.witnessed_births)

    def test_two_matched_arrivals_then_third_stable_pass_succeed(self):
        owner = self.make_witnessed()

        def changed(number, rows):
            if number in (1, 3):
                self.birth(13 if number == 1 else 14, 103 if number == 1 else 104)
            return rows

        calls = self.during_listeners(changed)
        snapshot = owner.sample()
        self.assertEqual((len(calls), self.native.read_count), (5, 6))
        self.assertEqual(snapshot.rss_bytes, 400)
        self.assertEqual(snapshot.total_births, 4)
        self.assertEqual(len(owner.witnessed_births), 4)

    def test_eighth_arrival_pass_cannot_succeed_without_stable_final_pass(self):
        owner = self.make_witnessed()

        def changed(number, rows):
            if number % 2:
                pid = 13 + number // 2
                self.birth(pid, pid + 90)
            return rows

        calls = self.during_listeners(changed)
        with self.assertRaisesRegex(ResourceFailure, "resource_evidence_unstable"):
            owner.sample()
        self.assertEqual((len(calls), self.native.read_count), (16, 16))
        self.assertEqual(len(owner.witnessed_births), 10)
        self.assertTrue(owner.cleanup_complete and owner.birth_evidence_complete)

    def test_birth_wholly_between_polls_is_retained_as_sampled_coverage_gap(self):
        owner = self.make_witnessed()
        self.native.total = self.job.total_processes = 3
        self.assertEqual(owner.sample().total_births, 3)
        self.assertEqual(owner.last_total, 3)
        self.assertEqual(len(owner.witnessed_births), 2)
        owner.close()
        self.assertTrue(owner.cleanup_complete and owner.association_counts_complete)
        self.assertFalse(owner.birth_evidence_complete)

    def test_initial_gap_never_exempts_a_visible_live_birth_from_audit(self):
        owner = self.make_witnessed()
        self.birth()
        self.native.total = self.job.total_processes = 4
        self.assertEqual(owner.sample().total_births, 4)
        self.assertIn((13, 103), owner.witnessed_births)
        owner.close()
        self.assertTrue(owner.cleanup_complete and owner.association_counts_complete)
        self.assertFalse(owner.birth_evidence_complete)

    def test_unmatched_closing_count_is_disclosed_and_cpu_evidence_is_preserved(self):
        owner = self.make_witnessed()

        def changed(number, rows):
            self.native.total = self.job.total_processes = 3
            self.native.cpu = 2.5
            return rows

        calls = self.during_listeners(changed)
        snapshot = owner.sample()
        self.assertEqual(len(calls), 1)
        self.assertEqual(owner.last_cpu, 2.5)
        self.assertEqual((snapshot.total_births, snapshot.job_cpu_seconds), (3, 2.5))
        owner.close()
        self.assertTrue(owner.cleanup_complete)
        self.assertFalse(owner.birth_evidence_complete)
        state = association_state()
        guard.accumulate_associations(state, owner)
        self.assertEqual(state, association_state(3, 2))

    def test_newly_discovered_signaled_handle_is_not_a_completed_ledger_entry(self):
        owner = self.make_witnessed()
        self.birth()
        self.native.identities[13] = (103, True)
        with self.assertRaisesRegex(ResourceFailure, "departed_member_still_listed"):
            owner.sample()
        self.assertNotIn((13, 103), owner.witnessed_births)
        self.assertIn(13, self.native.closed_handles)
        self.assertTrue(owner.cleanup_complete)

    def test_new_birth_exiting_after_rss_before_listener_completion_is_not_fully_observed(self):
        owner = self.make_witnessed()
        self.birth()
        original = self.native.rss

        def rss(handle):
            value = original(handle)
            if handle == 13:
                self.expire(handle)
            return value

        self.native.rss = rss
        snapshot = owner.sample()
        self.assertNotIn((13, 103), owner.witnessed_births)
        self.assertEqual(owner.incomplete_exit_births, {(13, 103)})
        self.assertEqual(snapshot.total_births, 3)
        self.assertEqual(owner.observed_peak_rss_bytes, 300)

    def test_new_birth_rss_failure_does_not_count_retained_handle(self):
        owner = self.make_witnessed()
        self.birth()
        original = self.native.rss

        def rss(handle):
            if handle == 13:
                raise ResourceFailure("resource_evidence_unavailable")
            return original(handle)

        self.native.rss = rss
        with self.assertRaisesRegex(ResourceFailure, "resource_evidence_unavailable"):
            owner.sample()
        self.assertNotIn((13, 103), owner.witnessed_births)
        self.assertTrue(owner.cleanup_complete)
        self.assertIsNone(owner.terminal_evidence_failure)

    def test_new_birth_listener_failure_does_not_count_retained_handle(self):
        owner = self.make_witnessed()
        self.birth()
        self.native.fail = "listener"
        with self.assertRaisesRegex(ResourceFailure, "owned_listener_not_loopback"):
            owner.sample()
        self.assertNotIn((13, 103), owner.witnessed_births)
        self.assertTrue(owner.cleanup_complete)

    def test_closing_birth_ownership_failure_keeps_precise_refusal(self):
        owner = self.make_witnessed()
        original = self.native.belongs

        def belongs(handle, job):
            return handle != 13 and original(handle, job)

        self.native.belongs = belongs
        self.during_listeners(lambda number, rows: (self.birth() if number == 1 else None) or rows)
        with self.assertRaisesRegex(ResourceFailure, "ownership_unproven"):
            owner.sample()
        self.assertTrue(owner.cleanup_complete)
        self.assertFalse(owner.birth_evidence_complete)
        self.assertIsNone(owner.terminal_evidence_failure)

    def test_new_birth_changed_identity_is_never_charged(self):
        owner = self.make_witnessed()
        self.birth()

        def changed(number, rows):
            self.native.identities[13] = (999, False)
            return rows

        self.during_listeners(changed)
        with self.assertRaisesRegex(ResourceFailure, "pid_reuse_or_exit"):
            owner.sample()
        self.assertNotIn((13, 103), owner.witnessed_births)

    def test_distinct_pids_with_same_creation_filetime_are_separate_births(self):
        owner = self.make_witnessed()
        self.birth(13, 101)
        snapshot = owner.sample()
        self.assertEqual(snapshot.total_births, 3)
        self.assertIn((11, 101), owner.witnessed_births)
        self.assertIn((13, 101), owner.witnessed_births)

    def test_newcomer_rss_adds_conservatively_to_prior_sum_before_cap(self):
        owner = self.make_witnessed()
        self.native.rss = lambda handle: 300_000_000 if handle == 13 else 950_000_000
        self.during_listeners(lambda number, rows: (self.birth() if number == 1 else None) or rows)
        with self.assertRaisesRegex(ResourceFailure, "owned_rss_exceeded"):
            owner.sample()
        self.assertEqual(owner.observed_peak_rss_bytes, 2_200_000_000)
        self.assertNotIn((13, 103), owner.witnessed_births)

    def test_birth_acquisition_does_not_reset_original_deadline(self):
        owner = self.make_witnessed()

        def changed(number, rows):
            self.clock.value += 0.6
            if number == 1:
                self.birth()
            return rows

        calls = self.during_listeners(changed)
        with self.assertRaisesRegex(ResourceFailure, "resource_evidence_stale"):
            owner.sample()
        self.assertEqual(len(calls), 2)

    def test_terminal_unknown_birth_records_coverage_gap_without_cleanup_failure(self):
        owner = self.make_witnessed()
        self.job.total_processes = 3
        for _ in range(2):
            owner.close()
        self.assertTrue(owner.closed and owner.cleanup_complete)
        self.assertFalse(owner.failed)
        self.assertEqual(owner.final_active, 0)
        self.assertFalse(owner.birth_evidence_complete)
        self.assertIsNone(owner.terminal_evidence_failure)
        self.assertTrue(owner.association_counts_complete)
        self.assertEqual(self.native.closed_handles, [11, 12])

    def test_job_cleanup_retains_terminal_count_for_outer_coverage_accounting(self):
        owner = self.make_witnessed()
        self.job.total_processes = 3
        self.job.close()
        self.assertTrue(owner.cleanup_complete)
        self.assertIsNone(owner.terminal_evidence_failure)
        self.assertEqual(owner.final_total, 3)
        owner.close()

    def test_run_owned_terminal_gap_preserves_cleanup_metrics_and_prior_error(self):
        implementation = guard.QualificationResources
        for sample_failure in (False, True):
            for prior_peak in (0, 1000):
                with self.subTest(sample_failure=sample_failure, prior_peak=prior_peak):
                    clock, native, instances = Clock(), FakeNative(), []
                    original_host = native.host

                    def host(disk, original_host=original_host):
                        available, total, times, free = original_host(disk)
                        return available, total, (times[0] * 2, times[1], times[2]), free

                    native.host = host

                    def belongs(handle, job, native=native, sample_failure=sample_failure):
                        return not (sample_failure and native.read_count >= 5)

                    native.belongs = belongs

                    def resources(common, disk, clock=clock, native=native, instances=instances):
                        owner = implementation(
                            common, disk, clock=clock, native_factory=lambda: native
                        )
                        instances.append(owner)
                        return owner

                    class Owned:
                        def __init__(self, *args, **kwargs):
                            self.job = kwargs["job_factory"]()
                            self.job.assign("fake-root")
                            self.process = types.SimpleNamespace(poll=lambda: 0, returncode=0)
                            # The process census never observes this third lifetime
                            # association. Only terminal Job accounting reveals it.
                            self.job.total_processes = 3

                        def close(self):
                            try:
                                self.job.close()
                            except BaseException:
                                # Match the common owner: actual close errors become
                                # cleanup errors. Evidence-only gaps must not raise here.
                                raise RuntimeError("wrapped_cleanup_failure") from None

                    with tempfile.TemporaryDirectory() as temporary:
                        state = dict(
                            **association_state(),
                            pending_payload=False,
                            cpu_seconds=2,
                            peak_rss_bytes=prior_peak,
                            started_at=time.time(),
                            all_owned_cleanup=True,
                        )
                        with (
                            patch.object(guard, "QualificationResources", side_effect=resources),
                            patch.object(
                                guard,
                                "common_module",
                                return_value=types.SimpleNamespace(
                                    WindowsJob=FakeJob, OwnedProcess=Owned
                                ),
                            ),
                            patch.object(guard.time, "monotonic", clock),
                            patch.object(
                                guard.time,
                                "sleep",
                                side_effect=lambda delta, clock=clock: setattr(
                                    clock, "value", clock.value + delta
                                ),
                            ),
                            self.assertRaisesRegex(ResourceFailure, "ownership_unproven")
                            if sample_failure
                            else contextlib.nullcontext(),
                        ):
                            guard.run_owned(
                                ["never-executed"],
                                cwd=ROOT,
                                env={},
                                work=Path(temporary),
                                state=state,
                                seconds=1,
                            )
                    owner = instances[0]
                    self.assertTrue(owner.cleanup_complete and state["all_owned_cleanup"])
                    self.assertFalse(owner.birth_evidence_complete or state["pending_payload"])
                    self.assertEqual(owner.final_active, 0)
                    self.assertEqual(state["cpu_seconds"], 5)
                    self.assertEqual(state["peak_rss_bytes"], max(prior_peak, 200))
                    self.assertEqual(
                        {field: state[field] for field in guard.ASSOCIATION_FIELDS},
                        {
                            field: association_state(3, 2)[field]
                            for field in guard.ASSOCIATION_FIELDS
                        },
                    )
                    self.assertTrue(state["association_counts_complete"])
                    self.assertEqual(native.closed_handles, [11, 12])


class NonAtomicCensusFakeTests(unittest.TestCase):
    """Non-atomic accounting/member reads never reset the completed birth ledger."""

    make = AdapterTests.make
    make_witnessed = StableSnapshotFakeTests.make_witnessed
    expire = StableSnapshotFakeTests.expire
    during_listeners = StableSnapshotFakeTests.during_listeners
    birth = BirthLedgerFakeTests.birth

    def during_members(self, callback):
        original = self.native.members
        calls = []

        def members(job):
            calls.append(self.native.read_count)
            callback(len(calls))
            return original(job)

        self.native.members = members
        return calls

    def stale_low_arrival(self, *, read=1, turnover=False):
        owner = self.make_witnessed()

        def changed(number):
            if number == read:
                if turnover:
                    self.expire()
                self.birth()

        calls = self.during_members(changed)
        return owner, calls

    def test_equal_active_turnover_before_members_can_return_fully_audited_current_census(self):
        owner, calls = self.stale_low_arrival(turnover=True)
        snapshot = owner.sample()
        self.assertEqual(calls, [1, 2])
        self.assertEqual((snapshot.total_births, snapshot.active_processes), (3, 2))
        self.assertEqual(owner.witnessed_births, {(11, 101), (12, 102), (13, 103)})
        owner.close()
        self.assertTrue(owner.cleanup_complete and owner.birth_evidence_complete)
        self.assertEqual(self.native.closed_handles, [11, 12, 13])

    def test_stale_low_total_two_active_one_then_new_live_child_is_fully_audited(self):
        owner, calls = self.stale_low_arrival(turnover=True)
        self.expire(12)
        observed = []
        original = self.native.rss

        def rss(handle):
            observed.append(handle)
            return original(handle)

        self.native.rss = rss
        self.native.listener_rows = {(13, 2, b"\x7f\0\0\1", 9000)}
        snapshot = owner.sample()
        self.assertEqual(calls, [1, 2])
        self.assertEqual(observed, [13])
        self.assertEqual((snapshot.total_births, snapshot.active_processes), (3, 1))
        self.assertEqual(snapshot.owned_loopback_listeners, 1)
        self.assertEqual(len(owner.witnessed_births), 3)

    def test_stale_low_initial_active_count_with_live_growth_can_stabilize(self):
        owner, calls = self.stale_low_arrival()
        snapshot = owner.sample()
        self.assertEqual(calls, [1, 2, 3, 4])
        self.assertEqual((snapshot.total_births, snapshot.active_processes), (3, 3))
        self.assertEqual(snapshot.rss_bytes, 300)

    def test_stale_high_initial_active_count_requires_proved_departure_and_fresh_pass(self):
        owner = self.make_witnessed()
        calls = self.during_members(lambda number: self.expire() if number == 1 else None)
        snapshot = owner.sample()
        self.assertEqual(calls, [1, 2, 3, 4])
        self.assertEqual((snapshot.total_births, snapshot.active_processes), (2, 1))

    def test_stale_low_closing_count_with_live_newcomer_requires_full_audit(self):
        owner, calls = self.stale_low_arrival(read=2)
        observed = []
        original = self.native.rss

        def rss(handle):
            observed.append(handle)
            return original(handle)

        self.native.rss = rss
        snapshot = owner.sample()
        self.assertEqual(calls, [1, 2, 3, 4])
        self.assertEqual(observed, [11, 12, 13, 11, 12, 13])
        self.assertEqual((snapshot.total_births, snapshot.active_processes), (3, 3))
        self.assertEqual(snapshot.rss_bytes, 300)

    def test_stale_low_snapshot_and_historical_gap_cannot_be_erased_by_later_births(self):
        for gap_before_accounting in (False, True):
            with self.subTest(gap_before_accounting=gap_before_accounting):
                owner = self.make_witnessed()
                if gap_before_accounting:
                    self.native.total = self.job.total_processes = 3

                def changed(number, gap_before_accounting=gap_before_accounting):
                    if number == 1:
                        if not gap_before_accounting:
                            self.native.total = self.job.total_processes = 3
                        self.expire()
                        self.birth()

                calls = self.during_members(changed)
                # Later live children are audited without erasing the historical gap.
                self.during_listeners(
                    lambda number, rows: (self.birth(14, 104) if number == 1 else None) or rows
                )
                self.assertEqual(owner.sample().total_births, 5)
                self.assertEqual(calls, [1, 2, 3, 4])
                self.assertEqual(owner.last_total, 5)
                self.assertEqual(len(owner.witnessed_births), 4)
                owner.close()
                self.assertTrue(owner.cleanup_complete)
                self.assertFalse(owner.birth_evidence_complete)
                self.assertIsNone(owner.terminal_evidence_failure)
                state = association_state()
                guard.accumulate_associations(state, owner)
                self.assertEqual(state, association_state(5, 4))

    def test_stale_low_census_preserves_historical_gap_and_audits_visible_arrival(self):
        owner, calls = self.stale_low_arrival()
        self.native.total = self.job.total_processes = 4
        self.assertEqual(owner.sample().total_births, 5)
        self.assertEqual(calls, [1, 2, 3, 4])
        self.assertEqual(owner.last_total, 5)
        self.assertIn((13, 103), owner.witnessed_births)
        owner.close()
        self.assertFalse(owner.birth_evidence_complete)
        state = association_state()
        guard.accumulate_associations(state, owner)
        self.assertEqual(state, association_state(5, 3))

    def test_consistently_low_total_cannot_return_more_ledger_entries_than_accounted(self):
        owner, calls = self.stale_low_arrival()
        original = self.native.accounting

        def accounting(job):
            _, active, cpu = original(job)
            return 2, active, cpu

        self.native.accounting = accounting
        with self.assertRaisesRegex(ResourceFailure, "association_count_underflow"):
            owner.sample()
        self.assertEqual(calls, [1, 2, 3, 4])
        self.assertTrue(owner.failed and owner.cleanup_complete)

    def test_new_initial_arrival_every_attempt_exhausts_eight_attempts(self):
        owner = self.make_witnessed()
        calls = self.during_members(
            lambda number: self.birth(13 + number // 2, 103 + number // 2) if number % 2 else None
        )
        with self.assertRaisesRegex(ResourceFailure, "initial_active_count_mismatch"):
            owner.sample()
        self.assertEqual(calls, list(range(1, 17)))
        self.assertEqual(len(owner.witnessed_births), 10)
        self.assertTrue(owner.cleanup_complete and owner.birth_evidence_complete)

    def test_current_census_is_stable_after_fully_proved_initial_equal_active_turnover(self):
        owner = self.make_witnessed()

        def changed(number):
            if number % 2:
                self.expire(min(self.native.pids))
                self.birth(13 + number // 2, 103 + number // 2)

        calls = self.during_members(changed)
        snapshot = owner.sample()
        self.assertEqual(calls, [1, 2])
        self.assertEqual(len(owner.witnessed_births), 3)
        self.assertEqual((snapshot.active_processes, snapshot.rss_bytes), (2, 200))
        self.assertEqual(owner.absent_births, {(11, 101)})

    def test_stale_census_does_not_reset_inclusive_original_deadline(self):
        for duration in (1.0, 1.0 + 2**-10):
            with self.subTest(duration=duration):
                owner, calls = self.stale_low_arrival()

                def changed(number, rows, duration=duration):
                    self.clock.value += duration / 2
                    return rows

                self.during_listeners(changed)
                if duration == 1.0:
                    snapshot = owner.sample()
                    self.assertEqual(snapshot.measured_at - snapshot.started_at, 1.0)
                else:
                    with self.assertRaisesRegex(ResourceFailure, "resource_evidence_stale"):
                        owner.sample()
                    self.assertTrue(owner.cleanup_complete)
                self.assertEqual(calls, [1, 2, 3, 4])

    def test_stale_low_new_identity_is_not_charged_if_exited_incomplete_or_reused(self):
        for failure in ("exited", "incomplete", "reused", "foreign", "rss", "listener"):
            with self.subTest(failure=failure):
                owner, calls = self.stale_low_arrival()
                original_identity = self.native.identity
                original_rss = self.native.rss
                original_belongs = self.native.belongs
                reads = []

                def identity(
                    handle, failure=failure, original_identity=original_identity, reads=reads
                ):
                    if handle == 13:
                        reads.append(handle)
                        if failure == "exited":
                            self.native.identities[13] = (103, True)
                        elif failure == "incomplete" and len(reads) == 2:
                            self.native.identities[13] = (103, True)
                        elif failure == "reused" and len(reads) == 2:
                            self.native.identities[13] = (999, False)
                    return original_identity(handle)

                def rss(handle, failure=failure, original_rss=original_rss):
                    if failure == "rss" and handle == 13:
                        raise ResourceFailure("resource_evidence_unavailable")
                    return original_rss(handle)

                def belongs(handle, job, failure=failure, original_belongs=original_belongs):
                    return not (failure == "foreign" and handle == 13) and original_belongs(
                        handle, job
                    )

                categories = dict(
                    exited="departed_member_still_listed",
                    incomplete="departed_member_still_listed",
                    reused="pid_reuse_or_exit",
                    foreign="ownership_unproven",
                    rss="resource_evidence_unavailable",
                    listener="owned_listener_not_loopback",
                )
                self.native.identity, self.native.rss = identity, rss
                self.native.belongs = belongs
                if failure == "listener":
                    self.native.fail = "listener"
                with self.assertRaisesRegex(ResourceFailure, categories[failure]):
                    owner.sample()
                self.assertEqual(
                    calls, list(range(1, 17)) if failure in ("exited", "incomplete") else [1]
                )
                self.assertNotIn((13, 103), owner.witnessed_births)
                self.assertTrue(owner.cleanup_complete)
                self.assertIn(13, self.native.closed_handles)

    def test_stale_low_reconciliation_does_not_retry_accounting_or_membership_api_errors(self):
        for api in ("accounting", "members"):
            with self.subTest(api=api):
                owner, calls = self.stale_low_arrival()
                original = getattr(self.native, api)

                def observe(job, api=api, original=original):
                    if self.native.read_count >= 2:
                        raise ResourceFailure("resource_evidence_unavailable")
                    return original(job)

                setattr(self.native, api, observe)
                with self.assertRaisesRegex(ResourceFailure, "resource_evidence_unavailable"):
                    owner.sample()
                self.assertEqual(self.native.read_count, 2)
                self.assertTrue(owner.cleanup_complete)

    def test_stale_low_retry_preserves_cpu_regression_refusal_and_watermark(self):
        owner, calls = self.stale_low_arrival()
        original = self.native.accounting

        def accounting(job):
            self.native.cpu = 2.5 if self.native.read_count < 2 else 2.0
            return original(job)

        self.native.accounting = accounting
        with self.assertRaisesRegex(ResourceFailure, "job_accounting_regressed"):
            owner.sample()
        self.assertEqual(calls, [1, 2])
        self.assertEqual(owner.last_cpu, 2.5)

    def test_stale_low_retry_preserves_cumulative_cpu_limit(self):
        owner, _ = self.stale_low_arrival()
        self.native.cpu = 2.5
        with patch.object(guard.time, "monotonic", self.clock):
            sample = guard.sample_values(owner)
        self.assertEqual(sample["cpu_seconds"], 2.5)
        with self.assertRaisesRegex(guard.Refusal, "cpu_limit"):
            guard.admit_sample(sample, previous_cpu=898)

    def test_stale_low_retry_preserves_rss_maximum_and_listener_union(self):
        owner, calls = self.stale_low_arrival()
        row = (13, 2, b"\x7f\0\0\1", 9000)
        self.native.rss_each = 300

        def changed(number, rows):
            self.native.rss_each = 25
            return {row} if number == 1 else set()

        self.during_listeners(changed)
        snapshot = owner.sample()
        self.assertEqual(calls, [1, 2, 3, 4])
        self.assertEqual(snapshot.rss_bytes, 900)
        self.assertEqual(owner.observed_peak_rss_bytes, 900)
        self.assertEqual(owner.observed_listeners, {row})
        self.assertEqual(snapshot.owned_loopback_listeners, 1)

    def test_stale_low_initial_and_closing_arrivals_cannot_hide_rss_cap(self):
        for read in (1, 2):
            with self.subTest(read=read):
                owner, _ = self.stale_low_arrival(read=read)
                self.native.rss = lambda handle: 300_000_000 if handle == 13 else 950_000_000
                with self.assertRaisesRegex(ResourceFailure, "owned_rss_exceeded"):
                    owner.sample()
                self.assertEqual(owner.observed_peak_rss_bytes, 2_200_000_000)
                self.assertNotIn((13, 103), owner.witnessed_births)
                self.assertTrue(owner.cleanup_complete)

    def test_stale_low_retry_cannot_hide_listener_union_cap(self):
        owner, _ = self.stale_low_arrival()
        self.during_listeners(
            lambda number, rows: (
                {(13, 2, b"\x7f\0\0\1", port) for port in range(1, 4097)}
                if number == 1
                else {(13, 2, b"\x7f\0\0\1", 5000)}
            )
        )
        with self.assertRaisesRegex(ResourceFailure, "listener_evidence_limit"):
            owner.sample()
        self.assertEqual(len(owner.observed_listeners), 4097)

    def test_stale_low_closing_member_census_must_still_obey_active_cap(self):
        owner = self.make_witnessed()

        def changed(number):
            if number == 2:
                for pid in range(13, 44):
                    self.birth(pid, pid + 90)

        calls = self.during_members(changed)
        with self.assertRaisesRegex(ResourceFailure, "final_members_exceed_limit"):
            owner.sample()
        self.assertEqual(calls, [1, 2])

    def test_stale_low_newcomer_cannot_exceed_retained_birth_storage_bound(self):
        owner, _ = self.stale_low_arrival()
        with (
            patch.object(guard, "MAX_RETAINED_BIRTHS", 2),
            self.assertRaisesRegex(ResourceFailure, "birth_evidence_limit"),
        ):
            owner.sample()
        self.assertNotIn((13, 103), owner.witnessed_births)
        self.assertTrue(owner.cleanup_complete)

    def test_stabilized_census_retains_unobserved_final_drain_association(self):
        owner, _ = self.stale_low_arrival()
        owner.sample()
        self.job.total_processes = 4
        owner.close()
        self.assertTrue(owner.cleanup_complete and owner.association_counts_complete)
        self.assertFalse(owner.failed)
        self.assertFalse(owner.birth_evidence_complete)
        self.assertIsNone(owner.terminal_evidence_failure)
        self.assertEqual(owner.final_total, 4)

    def test_stabilized_census_does_not_hide_cleanup_failures(self):
        for failure in ("listener", "handle", "job"):
            with self.subTest(failure=failure):
                owner, _ = self.stale_low_arrival()
                owner.sample()

                def failed(*args):
                    raise ResourceFailure("resource_handle_cleanup_failed")

                if failure == "listener":
                    self.native.fail = "listener_cleanup"
                    category = "owned_listener_cleanup_failed"
                elif failure == "handle":
                    self.native.close_handle = failed
                    category = "resource_handle_cleanup_failed"
                else:
                    self.native.assert_job_handle_closed = failed
                    category = "owned_job_cleanup_failed"
                with self.assertRaisesRegex(ResourceFailure, category):
                    owner.close()
                self.assertTrue(owner.failed and owner.closed)
                self.assertFalse(owner.cleanup_complete)


class SampledAllowanceFakeTests(unittest.TestCase):
    """Only the initial non-atomic census may establish a coverage allowance."""

    make = AdapterTests.make
    make_witnessed = StableSnapshotFakeTests.make_witnessed
    expire = StableSnapshotFakeTests.expire
    during_listeners = StableSnapshotFakeTests.during_listeners
    during_members = NonAtomicCensusFakeTests.during_members
    birth = BirthLedgerFakeTests.birth

    def test_initial_counted_child_exiting_before_members_is_an_allowed_coverage_gap(self):
        owner = self.make_witnessed()
        self.birth()
        calls = self.during_members(lambda number: self.expire(13) if number == 1 else None)
        snapshot = owner.sample()
        self.assertEqual(calls, [1, 2, 3, 4])
        self.assertEqual((snapshot.total_births, snapshot.active_processes), (3, 2))
        self.assertNotIn((13, 103), owner.witnessed_births)
        owner.close()
        self.assertTrue(owner.cleanup_complete and owner.association_counts_complete)
        self.assertFalse(owner.birth_evidence_complete)

    def test_unobserved_associations_on_later_attempt_remain_disclosed(self):
        owner = self.make_witnessed()
        self.native.total = self.job.total_processes = 3
        original = self.native.accounting

        def accounting(job):
            if self.native.read_count == 2:
                self.native.total = self.job.total_processes = 4
            return original(job)

        self.native.accounting = accounting
        self.during_listeners(lambda number, rows: (self.expire() if number == 1 else None) or rows)
        self.assertEqual(owner.sample().total_births, 4)
        self.assertEqual(self.native.read_count, 4)
        owner.close()
        self.assertTrue(owner.cleanup_complete and owner.association_counts_complete)
        self.assertEqual((owner.final_total, len(owner.witnessed_births)), (4, 2))
        state = association_state()
        guard.accumulate_associations(state, owner)
        self.assertEqual(state, association_state(4, 2))

    def test_unobserved_birth_during_first_audit_adds_to_disclosed_historical_gap(self):
        owner = self.make_witnessed()
        self.native.total = self.job.total_processes = 3

        def changed(number, rows):
            self.native.total = self.job.total_processes = 4
            return rows

        calls = self.during_listeners(changed)
        self.assertEqual(owner.sample().total_births, 4)
        self.assertEqual(len(calls), 1)
        owner.close()
        self.assertEqual(owner.final_total, 4)
        state = association_state()
        guard.accumulate_associations(state, owner)
        self.assertEqual(state, association_state(4, 2))

    def test_actual_api_failure_with_coverage_gap_cannot_be_rebased_by_next_sample(self):
        owner = self.make_witnessed()

        def changed(number, rows):
            self.native.total = self.job.total_processes = 3
            raise ResourceFailure("owned_listener_not_loopback")

        self.during_listeners(changed)
        with self.assertRaisesRegex(ResourceFailure, "owned_listener_not_loopback"):
            owner.sample()
        self.clock.value += 0.25
        with self.assertRaisesRegex(ResourceFailure, "resource_guard_failed"):
            owner.sample()
        self.assertTrue(owner.failed and owner.closed)
        self.assertEqual(owner.final_total, 3)

    def test_each_later_scheduled_sample_can_disclose_new_between_sample_gaps(self):
        owner = self.make_witnessed()
        for total in (3, 5, 8):
            self.native.total = self.job.total_processes = total
            self.assertEqual(owner.sample().total_births, total)
            self.clock.value += 0.25
        owner.close()
        state = association_state()
        guard.accumulate_associations(state, owner)
        self.assertEqual(state, association_state(8, 2))

    def test_allowance_never_exempts_visible_unobserved_member(self):
        for failure in ("foreign", "rss", "listener", "exited", "incomplete", "identity"):
            with self.subTest(failure=failure):
                owner = self.make_witnessed()
                self.birth()
                self.native.total = self.job.total_processes = 5
                original_rss, original_identity = self.native.rss, self.native.identity
                reads = []

                def rss(handle, failure=failure, original_rss=original_rss):
                    if handle == 13 and failure == "rss":
                        raise ResourceFailure("resource_evidence_unavailable")
                    return original_rss(handle)

                def identity(
                    handle, failure=failure, original_identity=original_identity, reads=reads
                ):
                    if handle == 13:
                        reads.append(handle)
                        if failure == "exited" or (failure == "incomplete" and len(reads) == 2):
                            self.native.identities[13] = (103, True)
                        elif failure == "identity" and len(reads) == 2:
                            self.native.identities[13] = (999, False)
                    return original_identity(handle)

                self.native.rss, self.native.identity = rss, identity
                if failure in ("foreign", "listener"):
                    self.native.fail = failure
                category = dict(
                    foreign="ownership_unproven",
                    rss="resource_evidence_unavailable",
                    listener="owned_listener_not_loopback",
                    exited="departed_member_still_listed",
                    incomplete="departed_member_still_listed",
                    identity="pid_reuse_or_exit",
                )[failure]
                with self.assertRaisesRegex(ResourceFailure, category):
                    owner.sample()
                self.assertNotIn((13, 103), owner.witnessed_births)
                self.assertTrue(owner.failed and owner.cleanup_complete)


class AssociationAccountingTests(unittest.TestCase):
    def resource(self, total=3, observed=2, *, complete=True):
        return types.SimpleNamespace(
            final_total=total,
            witnessed_births={(number, number + 100) for number in range(observed)},
            association_counts_complete=complete,
            association_counts_recorded=False,
        )

    def test_separate_job_totals_accumulate_exactly_once(self):
        state = association_state()
        for total, observed in ((3, 2), (5, 1), (0, 0)):
            owner = self.resource(total, observed)
            guard.accumulate_associations(state, owner)
            saved = dict(state)
            for _ in range(2):
                with self.assertRaisesRegex(guard.Refusal, "association_counts_already_recorded"):
                    guard.accumulate_associations(state, owner)
                self.assertEqual(state, saved)
        self.assertEqual(state, association_state(8, 3))

    def test_bounded_exact_integer_schema_and_mode_refuse_malformed_evidence(self):
        valid = association_state(3, 2)
        for field in (*guard.ASSOCIATION_FIELDS, "association_counts_complete", "evidence_mode"):
            bad = dict(valid)
            del bad[field]
            with self.subTest(missing=field), self.assertRaises(guard.Refusal):
                guard.association_evidence(bad)
        for field in guard.ASSOCIATION_FIELDS:
            for value in (None, True, False, 1.0, -1, "1", guard.MAX_ASSOCIATIONS + 1):
                with self.subTest(field=field, value=value), self.assertRaises(guard.Refusal):
                    guard.association_evidence({**valid, field: value})
        for field, value in (
            ("association_counts_complete", 1),
            ("association_counts_complete", "true"),
            ("evidence_mode", "strict"),
            ("evidence_mode", None),
            ("job_total_associations", 4),
            ("fully_live_observed_associations", 4),
            ("unobserved_or_rejected_associations", 0),
        ):
            with self.subTest(field=field, value=value), self.assertRaises(guard.Refusal):
                guard.association_evidence({**valid, field: value})
        guard.association_evidence(association_state(guard.MAX_ASSOCIATIONS, 2))

    def test_incomplete_aggregates_are_diagnostic_only_and_cannot_recover(self):
        state = association_state(3, 2, complete=False)
        guard.association_evidence(state, require_complete=False)
        with self.assertRaisesRegex(guard.Refusal, "association_counts_incomplete"):
            guard.association_evidence(state)
        owner = self.resource()
        with self.assertRaisesRegex(guard.Refusal, "association_counts_incomplete"):
            guard.accumulate_associations(state, owner)
        self.assertEqual(state, association_state(3, 2, complete=False))
        self.assertFalse(owner.association_counts_recorded)

    def test_invalid_native_totals_never_partially_commit(self):
        for total in (None, True, False, 3.0, -1, 1, 2**32):
            with self.subTest(total=total):
                state = association_state(8, 3)
                owner = self.resource(total)
                for _ in range(2):
                    with self.assertRaisesRegex(guard.Refusal, "association_counts"):
                        guard.accumulate_associations(state, owner)
                    self.assertEqual(state, association_state(8, 3))
                    self.assertFalse(owner.association_counts_recorded)
        state = association_state()
        guard.accumulate_associations(state, self.resource(2**32 - 1))
        self.assertEqual(state, association_state(2**32 - 1, 2))

    def test_overflow_and_unavailable_totals_never_partially_commit(self):
        for state, owner in (
            (association_state(guard.MAX_ASSOCIATIONS - 1, 2), self.resource()),
            (association_state(8, 3), self.resource(complete=False)),
        ):
            with self.subTest(state=state):
                saved = dict(state)
                for _ in range(2):
                    with self.assertRaisesRegex(guard.Refusal, "association_counts"):
                        guard.accumulate_associations(state, owner)
                    self.assertEqual(state, saved)
                    self.assertFalse(owner.association_counts_recorded)

    def test_save_state_rejects_invalid_counts_before_any_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            for bad in (
                {},
                {**association_state(), "evidence_mode": "unknown"},
                {**association_state(), "job_total_associations": True},
                {**association_state(), "association_counts_complete": 0},
            ):
                with self.subTest(state=bad), self.assertRaises(guard.Refusal):
                    guard.save_state(work, bad)
                self.assertEqual(list(work.iterdir()), [])
            state = association_state(8, 3, complete=False)
            guard.save_state(work, state)
            self.assertEqual(guard.load_json(work / "state.json"), state)

    def test_read_state_rejects_incomplete_admission_but_allows_cleanup_diagnostics(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            work = parent / "k5-hosted-abcdefgh"
            work.mkdir()
            state = {
                **association_state(8, 3, complete=False),
                "bootstrap_path": str(Path(sys.base_prefix) / "python.exe"),
                "bootstrap_sha256": "hash",
                "wrapper_sha256": "hash",
                "common_sha256": "hash",
                "started_at": time.time(),
            }
            guard.save_state(work, state)
            with (
                patch.dict(
                    guard.os.environ, {"K5_QUAL_ROOT": str(work), "RUNNER_TEMP": str(parent)}
                ),
                patch.object(guard, "context", return_value={}),
                patch.object(guard, "verify_bootstrap_selection"),
                patch.object(guard, "ordinary", side_effect=lambda path, **kw: Path(path)),
                patch.object(guard, "digest", return_value="hash"),
            ):
                with self.assertRaisesRegex(guard.Refusal, "association_counts_incomplete"):
                    guard.read_state("alpha")
                self.assertEqual(guard.read_state("alpha", cleanup=True), (work, state))
                for bad in (
                    {**state, "job_total_associations": -1},
                    {**state, "fully_live_observed_associations": True},
                    {**state, "evidence_mode": "unknown"},
                ):
                    (work / "state.json").write_text(json.dumps(bad))
                    with self.subTest(state=bad), self.assertRaises(guard.Refusal):
                        guard.read_state("alpha", cleanup=True)

    def test_receipt_requires_complete_valid_counts_but_allows_disclosed_coverage_gap(self):
        receipt = {**QualificationPolicyTests().receipt(), **association_state(8, 3)}
        guard.receipt_valid(receipt)
        for field in (*guard.ASSOCIATION_FIELDS, "evidence_mode", "association_counts_complete"):
            bad = dict(receipt)
            del bad[field]
            with self.subTest(missing=field), self.assertRaises(guard.Refusal):
                guard.receipt_valid(bad)
        for field, value in (
            ("job_total_associations", 9),
            ("fully_live_observed_associations", 9),
            ("unobserved_or_rejected_associations", -1),
            ("job_total_associations", guard.MAX_ASSOCIATIONS + 1),
            ("fully_live_observed_associations", True),
            ("association_counts_complete", False),
            ("association_counts_complete", 1),
            ("evidence_mode", "strict"),
        ):
            with self.subTest(field=field, value=value), self.assertRaises(guard.Refusal):
                guard.receipt_valid({**receipt, field: value})

    def test_finish_never_marks_incomplete_counters_as_completed(self):
        for complete in (True, False):
            with self.subTest(complete=complete), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                work = root / "owned"
                work.mkdir()
                workflow = root / guard.PROFILES["alpha"]
                workflow.parent.mkdir(parents=True)
                workflow.write_text("inert mock workflow")
                state = {
                    **QualificationPolicyTests().receipt(),
                    **association_state(8, 3, complete=complete),
                    "prepared": True,
                    "failed": False,
                    "finished_steps": [0],
                    "started_at": time.time(),
                    "all_owned_cleanup": True,
                    "pending_payload": False,
                }
                with (
                    patch.object(guard, "ROOT", root),
                    patch.object(guard, "read_state", return_value=(work, state)),
                    patch.object(guard, "workflow_steps", return_value=[{"body": "mock"}]),
                    contextlib.redirect_stdout(io.StringIO()),
                    contextlib.nullcontext()
                    if complete
                    else self.assertRaisesRegex(guard.Refusal, "association_counts_incomplete"),
                ):
                    guard.finish("alpha")
                receipt = json.loads((root / "hosted-alpha-qualification.json").read_text())
                self.assertEqual(receipt["completed"], complete)
                self.assertEqual(receipt["association_counts_complete"], complete)
                self.assertEqual(receipt["job_total_associations"], 8)
                self.assertEqual(receipt["fully_live_observed_associations"], 3)
                self.assertEqual(receipt["unobserved_or_rejected_associations"], 5)
                self.assertIn("between component observations", receipt["resource_evidence"])
                self.assertIn("including during acquisition", receipt["resource_evidence"])
                self.assertFalse(work.exists())
                self.assertFalse(
                    any(field in receipt for field in ("pids", "addresses", "handles"))
                )


class SampledRunOwnedTests(unittest.TestCase):
    """Actual adapter and supervisor with only fake Jobs, APIs, clocks and payloads."""

    def state(self, total=0, observed=0):
        return {
            **association_state(total, observed),
            "cpu_seconds": 0,
            "peak_rss_bytes": 0,
            "started_at": time.time(),
            "all_owned_cleanup": True,
            "pending_payload": False,
        }

    def run_job(self, state, *, total=3, failure=None, unobserved=0):
        clock, native = Clock(), FakeNative()
        instances = []
        implementation = guard.QualificationResources
        original_host = native.host

        def host(disk):
            available, size, times, free = original_host(disk)
            return available, size, (times[0] * 2, times[1], times[2]), free

        native.host = host
        original_listeners = native.listeners
        gap_recorded = False

        def listeners(pids):
            nonlocal gap_recorded
            rows = original_listeners(pids)
            if unobserved and not gap_recorded and native.read_count >= 5:
                native.total += unobserved
                if type(native.job.total_processes) is int:
                    native.job.total_processes += unobserved
                gap_recorded = True
            return rows

        native.listeners = listeners
        if failure == "rss":
            native.rss_each = GIB + 1
        elif failure == "ownership":
            native.belongs = lambda handle, job: native.read_count < 5
        elif failure == "temporary_cleanup":
            original_identity = native.identity
            original_open = native.open_member
            original_close = native.close_handle

            def identity(handle):
                if handle == 11 and native.read_count >= 5:
                    native.identities[11] = (101, True)
                return original_identity(handle)

            def opened(pid, job):
                if pid == 11 and native.read_count >= 5:
                    native.identities[111] = (101, True)
                    return 111, 101
                return original_open(pid, job)

            def close(handle):
                original_close(handle)
                if handle == 111:
                    raise ResourceFailure("resource_handle_cleanup_failed")

            native.identity, native.open_member, native.close_handle = identity, opened, close

        def resources(common, disk):
            owner = implementation(common, disk, clock=clock, native_factory=lambda: native)
            instances.append(owner)
            return owner

        class Owned:
            def __init__(self, *args, **kwargs):
                self.job = kwargs["job_factory"]()
                self.job.assign("fake-root")
                self.job.total_processes = total
                self.process = types.SimpleNamespace(
                    poll=lambda: 0, returncode=1 if failure == "payload" else 0
                )

            def close(self):
                if failure == "cleanup":
                    native.fail = "listener_cleanup"
                self.job.close()
                self.job.close()  # Supervisor also closes resources afterwards.

        error = None
        with tempfile.TemporaryDirectory() as temporary:
            with (
                patch.object(guard, "QualificationResources", side_effect=resources),
                patch.object(
                    guard,
                    "common_module",
                    return_value=types.SimpleNamespace(WindowsJob=FakeJob, OwnedProcess=Owned),
                ),
                patch.object(guard.time, "monotonic", clock),
                patch.object(
                    guard.time,
                    "sleep",
                    side_effect=lambda delta: setattr(clock, "value", clock.value + delta),
                ),
            ):
                try:
                    guard.run_owned(
                        ["never-executed"],
                        cwd=ROOT,
                        env={},
                        work=Path(temporary),
                        state=state,
                        seconds=1,
                    )
                except (guard.Refusal, ResourceFailure) as caught:
                    error = caught
        return error, instances, native

    def counts(self, state):
        return tuple(state[field] for field in guard.ASSOCIATION_FIELDS)

    def test_prepare_and_step_jobs_accumulate_without_double_counting_repeated_close(self):
        state = self.state()
        for total in (3, 5, 2):
            error, instances, native = self.run_job(state, total=total)
            self.assertIsNone(error)
            owner = instances[0]
            self.assertTrue(owner.association_counts_recorded and owner.cleanup_complete)
            saved = self.counts(state)
            owner.close()
            owner.job.close()
            self.assertEqual(self.counts(state), saved)
            self.assertEqual(native.closed_handles, [11, 12])
        self.assertEqual(self.counts(state), (10, 6, 4))
        self.assertEqual(state["cpu_seconds"], 9)
        self.assertEqual(state["peak_rss_bytes"], 200)
        self.assertTrue(state["association_counts_complete"])

    def test_refused_jobs_still_accumulate_valid_final_counts_and_keep_precise_errors(self):
        for failure, category in (
            ("payload", "child_exit"),
            ("ownership", "ownership_unproven"),
            ("cleanup", "cleanup_incomplete"),
            ("rss", "owned_rss_exceeded"),
        ):
            with self.subTest(failure=failure):
                state = self.state(8, 3)
                error, instances, _ = self.run_job(state, total=5, failure=failure)
                self.assertEqual(str(error), category)
                owner = instances[0]
                self.assertTrue(owner.association_counts_recorded)
                observed = 0 if failure == "rss" else 2
                total = 2 if failure == "rss" else 5  # RSS refused before payload construction.
                self.assertEqual(
                    self.counts(state), (8 + total, 3 + observed, 5 + total - observed)
                )
                self.assertTrue(state["association_counts_complete"])
                self.assertEqual(state["cpu_seconds"], 3)
                self.assertEqual(state["peak_rss_bytes"], 2 * GIB + 2 if failure == "rss" else 200)

    def test_unavailable_count_after_valid_cleanup_marks_previous_totals_partial(self):
        state = self.state(8, 3)
        error, instances, _ = self.run_job(state, total=3.0)
        self.assertEqual(str(error), "association_counts_unavailable")
        self.assertTrue(instances[0].cleanup_complete)
        self.assertFalse(instances[0].association_counts_recorded)
        self.assertEqual(self.counts(state), (8, 3, 5))
        self.assertFalse(state["association_counts_complete"])
        self.assertEqual((state["cpu_seconds"], state["peak_rss_bytes"]), (3, 200))
        for _ in range(2):
            error, instances, _ = self.run_job(state)
            self.assertEqual(str(error), "association_counts_incomplete")
            self.assertEqual(instances, [])
            self.assertEqual(self.counts(state), (8, 3, 5))

    def test_missing_final_count_cannot_hide_prior_error_or_claim_complete_aggregates(self):
        for failure in (None, "payload", "ownership"):
            with self.subTest(failure=failure):
                state = self.state(8, 3)
                error, instances, _ = self.run_job(state, total=None, failure=failure)
                # The original Job close cannot prove its final accounting, so
                # the existing cleanup failure precedence remains authoritative.
                self.assertEqual(str(error), "cleanup_incomplete")
                self.assertFalse(instances[0].association_counts_recorded)
                self.assertEqual(self.counts(state), (8, 3, 5))
                self.assertFalse(state["association_counts_complete"])
                self.assertEqual((state["cpu_seconds"], state["peak_rss_bytes"]), (3, 200))

    def test_unavailable_counts_preserve_prior_payload_or_ownership_refusal(self):
        for failure, category in (("payload", "child_exit"), ("ownership", "ownership_unproven")):
            with self.subTest(failure=failure):
                state = self.state(8, 3)
                error, instances, _ = self.run_job(state, total=3.0, failure=failure)
                self.assertEqual(str(error), category)
                self.assertTrue(instances[0].cleanup_complete)
                self.assertFalse(state["association_counts_complete"])
                self.assertEqual(self.counts(state), (8, 3, 5))

    def test_late_listener_cleanup_failure_does_not_erase_valid_terminal_counts(self):
        state = self.state(8, 3)
        error, instances, _ = self.run_job(state, total=5, failure="cleanup")
        self.assertEqual(str(error), "cleanup_incomplete")
        owner = instances[0]
        self.assertFalse(owner.cleanup_complete)
        self.assertTrue(owner.association_counts_complete and owner.association_counts_recorded)
        self.assertEqual(self.counts(state), (13, 5, 8))
        with self.assertRaisesRegex(ResourceFailure, "owned_job_cleanup_failed"):
            owner.close()
        self.assertEqual(self.counts(state), (13, 5, 8))

    def test_atomic_aggregate_overflow_preserves_cpu_peak_and_previous_counts(self):
        state = self.state(guard.MAX_ASSOCIATIONS - 1, 2)
        error, instances, _ = self.run_job(state)
        self.assertEqual(str(error), "association_counts")
        self.assertFalse(state["association_counts_complete"])
        self.assertFalse(instances[0].association_counts_recorded)
        self.assertEqual(
            self.counts(state), (guard.MAX_ASSOCIATIONS - 1, 2, guard.MAX_ASSOCIATIONS - 3)
        )
        self.assertEqual((state["cpu_seconds"], state["peak_rss_bytes"]), (3, 200))

    def test_temporary_close_failure_keeps_counts_but_never_claims_cleanup_or_receipt(self):
        state = self.state(8, 3)
        error, instances, native = self.run_job(state, total=5, failure="temporary_cleanup")
        owner = instances[0]
        self.assertEqual(str(error), "cleanup_incomplete")
        self.assertTrue(owner.unretained_handle_cleanup_failed and owner.job.drained)
        self.assertFalse(owner.cleanup_complete or state["all_owned_cleanup"])
        self.assertIs(state["pending_payload"], True)
        self.assertTrue(owner.association_counts_complete and owner.association_counts_recorded)
        self.assertEqual(self.counts(state), (13, 5, 8))
        self.assertEqual((state["cpu_seconds"], state["peak_rss_bytes"]), (3, 200))
        self.assertEqual(native.closed_handles, [111, 11, 12])
        with self.assertRaisesRegex(ResourceFailure, "owned_job_cleanup_failed"):
            owner.close()
        self.assertEqual(native.closed_handles, [111, 11, 12])
        self.assertEqual(self.counts(state), (13, 5, 8))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            work = root / "owned"
            work.mkdir()
            workflow = root / guard.PROFILES["alpha"]
            workflow.parent.mkdir(parents=True)
            workflow.write_text("inert mock workflow")
            state.update(prepared=True, failed=False, finished_steps=[0])
            with (
                patch.object(guard, "ROOT", root),
                patch.object(guard, "read_state", return_value=(work, state)),
                patch.object(guard, "workflow_steps", return_value=[{"body": "mock"}]),
                self.assertRaisesRegex(guard.Refusal, "cleanup_unproven"),
            ):
                guard.finish("alpha")
            self.assertTrue(work.exists())
            self.assertFalse((root / "hosted-alpha-qualification.json").exists())

    def test_cpu_and_wall_refusal_still_retain_final_coverage_counts(self):
        for limit, category in (("cpu", "cpu_limit"), ("wall", "deadline")):
            with self.subTest(limit=limit):
                state = self.state(8, 3)
                if limit == "cpu":
                    state["cpu_seconds"] = 898
                else:
                    state["started_at"] = time.time() - 901
                error, instances, _ = self.run_job(state)
                self.assertEqual(str(error), category)
                self.assertTrue(instances[0].association_counts_recorded)
                self.assertEqual(self.counts(state), (11, 5, 6))

    def test_malformed_evidence_is_rejected_before_constructing_any_job(self):
        for field, value in (
            ("evidence_mode", "unknown"),
            ("association_counts_complete", False),
            ("association_counts_complete", 0),
            ("job_total_associations", True),
            ("fully_live_observed_associations", 1),
        ):
            with self.subTest(field=field, value=value):
                state = {**self.state(), field: value}
                saved = dict(state)
                error, instances, _ = self.run_job(state)
                self.assertIsInstance(error, guard.Refusal)
                self.assertEqual(instances, [])
                self.assertEqual(state, saved)

    def test_pending_missing_or_malformed_prior_payload_never_launches_or_mutates(self):
        for marker in (True, None, 0, 1, "false", [], {}):
            with self.subTest(marker=marker):
                state = {**self.state(8, 3), "pending_payload": marker}
                saved = dict(state)
                error, instances, _ = self.run_job(state)
                self.assertEqual(str(error), "payload_pending")
                self.assertEqual(instances, [])
                self.assertEqual(state, saved)
        state = self.state(8, 3)
        del state["pending_payload"]
        saved = dict(state)
        error, instances, _ = self.run_job(state)
        self.assertEqual(str(error), "payload_pending")
        self.assertEqual(instances, [])
        self.assertEqual(state, saved)
        state["pending_payload"] = False
        error, instances, _ = self.run_job(state)
        self.assertIsNone(error)
        self.assertEqual(len(instances), 1)
        self.assertEqual(self.counts(state), (11, 5, 6))
        self.assertIs(state["pending_payload"], False)

    def test_missing_terminal_count_without_samples_is_not_zero_coverage(self):
        case = AdapterTests()
        owner = case.make()
        case.job.total_processes = None
        owner.close()
        self.assertTrue(owner.cleanup_complete)
        self.assertFalse(owner.association_counts_complete)
        self.assertEqual(owner.terminal_evidence_failure, "association_counts_unavailable")
        state = association_state(8, 3)
        with self.assertRaisesRegex(guard.Refusal, "association_counts_unavailable"):
            guard.accumulate_associations(state, owner)
        self.assertEqual(state, association_state(8, 3))


class ProvenExitTimingTests(unittest.TestCase):
    make = AdapterTests.make
    make_witnessed = StableSnapshotFakeTests.make_witnessed
    expire = StableSnapshotFakeTests.expire
    birth = BirthLedgerFakeTests.birth
    during_members = NonAtomicCensusFakeTests.during_members
    during_listeners = StableSnapshotFakeTests.during_listeners

    def timeline(self, timing, *, closing=False, still_listed=False, disappear=True):
        owner = self.make_witnessed()
        if not closing:
            self.birth()
        originals = {
            name: getattr(self.native, name)
            for name in ("open_member", "identity", "rss", "listeners", "members")
        }
        events = dict(opens=0, identities=0, rss=[], listeners=0, members=0, exited=False)
        row = (13, 2, b"\x7f\0\0\1", 9000)

        def expire():
            if events["exited"]:
                return
            events["exited"] = True
            self.native.identities[13] = (103, True)
            if not still_listed:
                self.native.pids.remove(13)

        def opened(pid, job):
            value = originals["open_member"](pid, job)
            if pid == 13:
                events["opens"] += 1
                if timing == "open" and events["opens"] == 1:
                    expire()
            return value

        def identity(handle):
            if handle == 13:
                events["identities"] += 1
                if (timing, events["identities"]) in {
                    ("before_rss", 1),
                    ("post_listener_check", 2),
                    ("final_live_check", 3),
                }:
                    expire()
            value = originals["identity"](handle)
            if handle == 13 and (timing, events["identities"]) in {
                ("post_listener_return", 2),
                ("final_live_return", 3),
            }:
                expire()
            return value

        def rss(handle):
            events["rss"].append(handle)
            value = originals["rss"](handle)
            if handle == 13 and timing == "rss":
                expire()
            return value

        def listeners(pids):
            rows = originals["listeners"](pids)
            if self.job.drained:
                return rows
            events["listeners"] += 1
            if closing and events["listeners"] == 1:
                self.birth()
            if 13 in pids and timing == "listeners":
                expire()
            return rows | ({row} if 13 in pids else set())

        def members(job):
            events["members"] += 1
            if timing == "closing_members" and events["members"] == 2:
                expire()
            if still_listed and disappear and events["members"] == 3:
                self.native.pids.remove(13)
            return originals["members"](job)

        self.native.open_member, self.native.identity = opened, identity
        self.native.rss, self.native.listeners, self.native.members = rss, listeners, members
        return owner, events

    def test_all_same_object_exit_timings_preserve_evidence_and_require_stability(self):
        incomplete = {"open", "before_rss", "rss", "listeners", "post_listener_check"}
        timings = sorted(incomplete) + [
            "post_listener_return",
            "closing_members",
            "final_live_check",
            "final_live_return",
        ]
        for timing in timings:
            with self.subTest(timing=timing):
                owner, events = self.timeline(timing)
                snapshot = owner.sample()
                self.assertEqual(snapshot.total_births, 3)
                self.assertEqual(events["members"], 2 if timing == "final_live_return" else 4)
                self.assertEqual(
                    snapshot.active_processes, 3 if timing == "final_live_return" else 2
                )
                self.assertEqual(
                    snapshot.rss_bytes, 200 if timing in {"open", "before_rss"} else 300
                )
                self.assertEqual(snapshot.owned_loopback_listeners, 1)
                self.assertEqual((13, 103) in owner.incomplete_exit_births, timing in incomplete)
                self.assertEqual((13, 103) in owner.witnessed_births, timing not in incomplete)
                if timing == "final_live_return":
                    self.clock.value += 0.25
                    self.assertEqual(owner.sample().active_processes, 2)
                self.assertIn((13, 103), owner.absent_births)
                owner.close()
                state = association_state()
                guard.accumulate_associations(state, owner)
                self.assertEqual(state, association_state(3, 2 if timing in incomplete else 3))

    def test_closing_newcomer_incomplete_exits_always_consume_a_fresh_retry(self):
        for timing in ("open", "before_rss", "rss", "listeners", "post_listener_check"):
            with self.subTest(timing=timing):
                owner, events = self.timeline(timing, closing=True)
                snapshot = owner.sample()
                self.assertEqual((snapshot.total_births, snapshot.active_processes), (3, 2))
                self.assertEqual(events["members"], 4)
                self.assertEqual(owner.incomplete_exit_births, {(13, 103)})
                self.assertEqual(owner.witnessed_births, {(11, 101), (12, 102)})

    def test_signaled_but_still_listed_requires_fresh_identity_then_actual_absence(self):
        for closing in (False, True):
            with self.subTest(closing=closing):
                owner, events = self.timeline("rss", closing=closing, still_listed=True)
                snapshot = owner.sample()
                self.assertEqual(snapshot.active_processes, 2)
                # Removal follows the next accounting read, so that stale active
                # count consumes another retry before an exact stable census.
                self.assertEqual(events["members"], 6)
                if not closing:
                    self.assertGreater(events["opens"], 1)
                self.assertIn((13, 103), owner.absent_births)
                owner.close()
                self.assertEqual(self.native.closed_handles.count(13), events["opens"])

    def test_permanently_still_listed_exit_exhausts_eight_attempts_without_acceptance(self):
        owner, events = self.timeline("rss", still_listed=True, disappear=False)
        with self.assertRaisesRegex(ResourceFailure, "departed_member_still_listed"):
            owner.sample()
        self.assertEqual(events["members"], 16)
        self.assertEqual(owner.incomplete_exit_births, {(13, 103)})
        self.assertNotIn((13, 103), owner.absent_births)
        self.assertTrue(owner.cleanup_complete)

    def test_absent_identity_cannot_reappear_in_later_sample_even_with_positive_allowance(self):
        for timing in ("rss", "post_listener_return"):
            for extra in (0, 2):
                with self.subTest(timing=timing, extra=extra):
                    owner, events = self.timeline(timing)
                    owner.sample()
                    self.clock.value += 0.25
                    self.native.pids.add(13)
                    self.native.total += extra
                    self.job.total_processes = self.native.total
                    opens = events["opens"]
                    with self.assertRaisesRegex(ResourceFailure, "retry_member_set_expanded"):
                        owner.sample()
                    self.assertEqual(events["opens"], opens)

    def test_full_witness_pid_reuse_without_any_observed_absence_is_not_an_allowance(self):
        owner = self.make_witnessed()
        self.native.identities[11] = (101, True)
        self.native.identities[111] = (999, False)
        self.native.total = self.job.total_processes = 3
        original = self.native.open_member
        self.native.open_member = lambda pid, job: (111, 999) if pid == 11 else original(pid, job)
        with self.assertRaisesRegex(ResourceFailure, "pid_reuse_or_exit"):
            owner.sample()
        self.assertEqual(owner.absent_births, set())
        self.assertEqual(self.native.closed_handles.count(111), 1)
        self.assertTrue(owner.cleanup_complete)

    def test_incomplete_exit_pid_reuse_without_any_observed_absence_is_refused(self):
        owner, _ = self.timeline("rss", still_listed=True, disappear=False)
        self.native.total = self.job.total_processes = 4  # One initial unexplained association.
        original_members, original_open = self.native.members, self.native.open_member
        reused = False

        def members(job):
            nonlocal reused
            if self.native.read_count == 3:
                reused = True
                self.native.total = self.job.total_processes = 5
                self.native.identities[113] = (999, False)
            return original_members(job)

        def opened(pid, job):
            return (113, 999) if pid == 13 and reused else original_open(pid, job)

        self.native.members, self.native.open_member = members, opened
        with self.assertRaisesRegex(ResourceFailure, "pid_reuse_or_exit"):
            owner.sample()
        self.assertEqual(owner.incomplete_exit_births, {(13, 103)})
        self.assertNotIn((13, 103), owner.absent_births)
        self.assertEqual(self.native.closed_handles.count(113), 1)

    def test_incomplete_exit_and_simultaneous_unobserved_birth_remain_coverage_gaps(self):
        for initial_gap in (0, 2):
            with self.subTest(initial_gap=initial_gap):
                owner, _ = self.timeline("rss")
                self.native.total += initial_gap
                self.job.total_processes = self.native.total
                original = self.native.rss

                def rss(handle, original=original):
                    result = original(handle)
                    if handle == 13:
                        self.native.total += 1
                        self.job.total_processes = self.native.total
                    return result

                self.native.rss = rss
                self.assertEqual(owner.sample().total_births, 4 + initial_gap)
                self.assertEqual(owner.incomplete_exit_births, {(13, 103)})
                owner.close()
                self.assertEqual(owner.final_total, 4 + initial_gap)
                self.assertTrue(owner.cleanup_complete)
                state = association_state()
                guard.accumulate_associations(state, owner)
                self.assertEqual(state, association_state(4 + initial_gap, 2))

    def test_incomplete_exit_is_classified_once_across_retries_and_later_samples(self):
        owner, _ = self.timeline("rss")
        owner.sample()
        for total in (3, 3, 5):
            self.clock.value += 0.25
            self.native.total = self.job.total_processes = total
            self.assertEqual(owner.sample().total_births, total)
            self.assertEqual(owner.incomplete_exit_births, {(13, 103)})
        owner.close()
        state = association_state()
        guard.accumulate_associations(state, owner)
        self.assertEqual(state, association_state(5, 2))

    def test_full_and_incomplete_identity_sets_must_be_disjoint(self):
        owner = self.make_witnessed()
        owner.incomplete_exit_births.add((11, 101))
        with self.assertRaisesRegex(ResourceFailure, "birth_evidence_overlap"):
            owner.sample()
        self.assertFalse(owner.association_counts_complete)

    def test_incomplete_exit_never_waives_rss_listener_identity_or_ownership_api_errors(self):
        for failure, category in (
            ("rss", "resource_evidence_unavailable"),
            ("listeners", "owned_listener_not_loopback"),
            ("identity", "process_wait_failed"),
            ("birth", "pid_reuse_or_exit"),
            ("ownership", "ownership_unproven"),
        ):
            with self.subTest(failure=failure):
                owner = self.make_witnessed()
                self.birth()
                original_rss = self.native.rss
                original_identity = self.native.identity
                original_belongs = self.native.belongs
                reads = []

                def rss(handle, failure=failure, original_rss=original_rss):
                    value = original_rss(handle)
                    if handle == 13:
                        self.expire(13)
                        if failure == "rss":
                            raise ResourceFailure("resource_evidence_unavailable")
                    return value

                def identity(
                    handle, failure=failure, original_identity=original_identity, reads=reads
                ):
                    if handle == 13:
                        reads.append(handle)
                        if len(reads) == 2:
                            if failure == "identity":
                                raise ResourceFailure("process_wait_failed")
                            if failure == "birth":
                                return 999, True
                    return original_identity(handle)

                def belongs(handle, job, failure=failure, original_belongs=original_belongs):
                    return not (
                        failure == "ownership" and handle == 13 and self.native.identities[13][1]
                    ) and original_belongs(handle, job)

                self.native.rss, self.native.identity, self.native.belongs = rss, identity, belongs
                if failure == "listeners":
                    self.native.fail = "listener"
                with self.assertRaisesRegex(ResourceFailure, category):
                    owner.sample()
                self.assertEqual(owner.incomplete_exit_births, set())
                self.assertNotIn((13, 103), owner.witnessed_births)
                self.assertTrue(owner.cleanup_complete)

    def test_late_missing_member_needs_signaled_same_identity_and_successful_recheck(self):
        for failure, category in (
            ("live", "final_survivor_missing"),
            ("birth", "pid_reuse_or_exit"),
            ("identity", "process_wait_failed"),
            ("ownership", "ownership_unproven"),
        ):
            with self.subTest(failure=failure):
                owner = self.make_witnessed()
                original_identity = self.native.identity
                original_belongs = self.native.belongs
                missing = []

                def members(number, failure=failure, missing=missing):
                    if number == 2:
                        missing.append(True)
                        self.native.pids.remove(11)
                        if failure != "live":
                            self.native.identities[11] = (999 if failure == "birth" else 101, True)

                def identity(
                    handle, failure=failure, missing=missing, original_identity=original_identity
                ):
                    if missing and handle == 11 and failure == "identity":
                        raise ResourceFailure("process_wait_failed")
                    return original_identity(handle)

                self.native.identity = identity

                def belongs(
                    handle, job, failure=failure, missing=missing, original_belongs=original_belongs
                ):
                    return not (
                        missing and handle == 11 and failure == "ownership"
                    ) and original_belongs(handle, job)

                self.native.belongs = belongs
                self.during_members(members)
                with self.assertRaisesRegex(ResourceFailure, category):
                    owner.sample()

    def test_incomplete_exit_does_not_erase_rss_or_listener_limits(self):
        for limit in ("rss", "listeners"):
            with self.subTest(limit=limit):
                owner, _ = self.timeline("rss")
                if limit == "rss":
                    self.native.rss_each = GIB
                    category = "owned_rss_exceeded"
                else:
                    original = self.native.listeners

                    def listeners(pids, original=original):
                        rows = original(pids)
                        return (
                            rows
                            if self.job.drained
                            else rows | {(13, 2, b"\x7f\0\0\1", port) for port in range(1, 4097)}
                        )

                    self.native.listeners = listeners
                    category = "listener_evidence_limit"
                with self.assertRaisesRegex(ResourceFailure, category):
                    owner.sample()
                self.assertTrue(owner.cleanup_complete)

    def test_closing_incomplete_exit_each_attempt_cannot_return_without_stability(self):
        owner = self.make_witnessed()
        original = self.native.rss

        def rss(handle):
            value = original(handle)
            if handle >= 13:
                self.expire(handle)
            return value

        self.native.rss = rss
        calls = self.during_listeners(
            lambda number, rows: (
                (self.birth(13 + number // 2, 103 + number // 2) if number % 2 else None) or rows
            )
        )
        with self.assertRaisesRegex(ResourceFailure, "resource_evidence_unstable"):
            owner.sample()
        self.assertEqual((len(calls), self.native.read_count), (16, 16))
        self.assertEqual(len(owner.incomplete_exit_births), 8)
        self.assertTrue(owner.cleanup_complete and owner.association_counts_complete)

    def test_late_signaled_survivor_each_attempt_exhausts_without_stable_snapshot(self):
        owner = self.make()
        for pid in range(13, 19):
            self.birth(pid, pid + 90)
        owner.sample()
        self.clock.value += 0.25
        self.native.read_count = 0
        original = self.native.identity

        def identity(handle):
            read = self.native.read_count
            if read in range(2, 17, 2) and handle == 10 + read // 2 and handle in self.native.pids:
                self.expire(handle)
            return original(handle)

        self.native.identity = identity
        with self.assertRaisesRegex(ResourceFailure, "final_survivor_signaled"):
            owner.sample()
        self.assertEqual(self.native.read_count, 16)

    def test_original_inclusive_one_second_deadline_covers_exit_retries(self):
        for duration in (1.0, 1.0 + 2**-10):
            with self.subTest(duration=duration):
                owner, _ = self.timeline("rss")
                original = self.native.listeners

                def listeners(pids, original=original, duration=duration):
                    rows = original(pids)
                    if not self.job.drained:
                        self.clock.value += duration / 2
                    return rows

                self.native.listeners = listeners
                if duration == 1:
                    snapshot = owner.sample()
                    self.assertEqual(snapshot.measured_at - snapshot.started_at, 1)
                else:
                    with self.assertRaisesRegex(ResourceFailure, "resource_evidence_stale"):
                        owner.sample()

    def test_fresh_binding_failures_close_temporary_handle_and_never_qualify(self):
        for failure, category in (
            ("open", "resource_evidence_unavailable"),
            ("ownership", "ownership_unproven"),
            ("birth", "pid_reuse_or_exit"),
            ("live", "pid_reuse_or_exit"),
            ("identity", "process_wait_failed"),
            ("close", "resource_handle_cleanup_failed"),
        ):
            with self.subTest(failure=failure):
                owner = self.make_witnessed()
                self.native.identities[11] = (101, True)
                self.native.identities[111] = (101, failure != "live")
                original_open, original_identity = self.native.open_member, self.native.identity
                original_belongs, original_close = self.native.belongs, self.native.close_handle

                def opened(pid, job, failure=failure, original_open=original_open):
                    if pid != 11:
                        return original_open(pid, job)
                    if failure == "open":
                        raise ResourceFailure("resource_evidence_unavailable")
                    return 111, 999 if failure == "birth" else 101

                def identity(handle, failure=failure, original_identity=original_identity):
                    if handle == 111 and failure == "identity":
                        raise ResourceFailure("process_wait_failed")
                    return original_identity(handle)

                def close(handle, failure=failure, original_close=original_close):
                    original_close(handle)
                    if handle == 111 and failure == "close":
                        raise ResourceFailure("resource_handle_cleanup_failed")

                self.native.open_member, self.native.identity, self.native.close_handle = (
                    opened,
                    identity,
                    close,
                )
                self.native.belongs = (
                    lambda handle, job, failure=failure, original_belongs=original_belongs: (
                        not (handle == 111 and failure == "ownership")
                        and original_belongs(handle, job)
                    )
                )
                with self.assertRaisesRegex(ResourceFailure, category):
                    owner.sample()
                self.assertEqual(
                    self.native.closed_handles.count(111), 0 if failure == "open" else 1
                )
                self.assertEqual(owner.cleanup_complete, failure != "close")
                self.assertTrue(self.job.drained)

    def test_terminal_total_must_cover_full_and_incomplete_even_after_accounting_error(self):
        owner = self.make_witnessed()
        original_accounting, original_members = self.native.accounting, self.native.members
        original_rss = self.native.rss

        def accounting(job):
            if self.native.read_count == 1:
                raise ResourceFailure("resource_evidence_unavailable")
            return original_accounting(job)

        def members(job):
            self.birth()
            self.job.total_processes = 2
            return original_members(job)

        def rss(handle):
            value = original_rss(handle)
            if handle == 13:
                self.expire(handle)
            return value

        self.native.accounting, self.native.members, self.native.rss = accounting, members, rss
        with self.assertRaisesRegex(ResourceFailure, "resource_evidence_unavailable"):
            owner.sample()
        self.assertEqual(owner.last_total, 2)
        self.assertEqual(owner.incomplete_exit_births, {(13, 103)})
        self.assertEqual(len(owner.witnessed_births), 2)
        self.assertEqual(owner.final_total, 2)
        self.assertTrue(owner.cleanup_complete)
        self.assertFalse(owner.association_counts_complete)
        state = association_state(8, 3)
        with self.assertRaisesRegex(guard.Refusal, "association_counts_unavailable"):
            guard.accumulate_associations(state, owner)
        self.assertEqual(state, association_state(8, 3))

    def test_unregistered_open_cleanup_failure_is_sticky_and_still_drains_owned_job(self):
        owner = self.make()

        def opened(pid, job):
            raise ResourceFailure("resource_handle_cleanup_failed")

        self.native.open_member = opened
        with self.assertRaisesRegex(ResourceFailure, "resource_handle_cleanup_failed"):
            owner.sample()
        self.assertTrue(owner.unretained_handle_cleanup_failed and self.job.drained)
        self.assertFalse(owner.cleanup_complete)
        self.assertEqual(self.native.closed_handles, [])


class FiniteAttemptBoundTests(unittest.TestCase):
    make = AdapterTests.make
    make_witnessed = StableSnapshotFakeTests.make_witnessed
    expire = StableSnapshotFakeTests.expire
    birth = BirthLedgerFakeTests.birth

    def churn(self, waves, *, incomplete=True, duration=None):
        owner = self.make_witnessed()
        original_rss, original_listeners = self.native.rss, self.native.listeners
        events = {"listeners": 0}
        started = self.clock.value

        def rss(handle):
            value = original_rss(handle)
            if incomplete and handle >= 13:
                self.expire(handle)
            return value

        def listeners(pids):
            rows = original_listeners(pids)
            if self.job.drained:
                return rows
            events["listeners"] += 1
            number = events["listeners"]
            if number % 2 and number <= 2 * waves:
                self.birth(13 + number // 2, 103 + number // 2)
            if duration is not None and number == 2 * waves + 1:
                self.clock.value = started + duration
            return rows

        self.native.rss, self.native.listeners = rss, listeners
        return owner, events

    def test_bound_is_exactly_eight_and_original_stale_limit_is_unchanged(self):
        self.assertEqual(guard.MAX_SAMPLE_ATTEMPTS, 8)
        self.assertEqual(guard.STALE_SECONDS, 1.0)

    def test_three_churn_passes_refuse_old_bound_but_fourth_stable_pass_succeeds(self):
        for incomplete in (False, True):
            for bound in (3, 8):
                with self.subTest(incomplete=incomplete, bound=bound):
                    owner, events = self.churn(3, incomplete=incomplete)
                    with patch.object(guard, "MAX_SAMPLE_ATTEMPTS", bound):
                        if bound == 3:
                            with self.assertRaisesRegex(
                                ResourceFailure, "resource_evidence_unstable"
                            ):
                                owner.sample()
                            self.assertEqual((self.native.read_count, events["listeners"]), (6, 6))
                        else:
                            snapshot = owner.sample()
                            self.assertEqual((self.native.read_count, events["listeners"]), (8, 7))
                            self.assertEqual(snapshot.total_births, 5)
                            self.assertEqual(snapshot.active_processes, 2 if incomplete else 5)
                            owner.close()
                    self.assertEqual(len(owner.witnessed_births), 2 if incomplete else 5)
                    self.assertEqual(len(owner.incomplete_exit_births), 3 if incomplete else 0)
                    self.assertTrue(owner.cleanup_complete and owner.association_counts_complete)
                    state = association_state()
                    guard.accumulate_associations(state, owner)
                    self.assertEqual(state, association_state(5, 2 if incomplete else 5))

    def test_seven_churn_passes_need_eighth_stability_but_eight_churn_passes_exhaust(self):
        for waves in (7, 8):
            with self.subTest(waves=waves):
                owner, events = self.churn(waves)
                if waves == 7:
                    self.assertEqual(owner.sample().active_processes, 2)
                    self.assertEqual(events["listeners"], 15)
                    owner.close()
                else:
                    with self.assertRaisesRegex(ResourceFailure, "resource_evidence_unstable"):
                        owner.sample()
                    self.assertEqual(events["listeners"], 16)
                self.assertEqual(self.native.read_count, 16)
                self.assertEqual(len(owner.incomplete_exit_births), waves)
                self.assertTrue(owner.cleanup_complete and owner.association_counts_complete)

    def test_stable_sample_returns_early_without_spending_extra_attempts(self):
        owner, events = self.churn(0)
        owner.sample()
        self.assertEqual((self.native.read_count, events["listeners"]), (2, 1))

    def test_fourth_pass_still_has_original_inclusive_acquisition_deadline(self):
        for duration in (1.0, 1.0 + 2**-10):
            with self.subTest(duration=duration):
                owner, events = self.churn(3, duration=duration)
                if duration == 1:
                    snapshot = owner.sample()
                    self.assertEqual(snapshot.measured_at - snapshot.started_at, 1)
                else:
                    with self.assertRaisesRegex(ResourceFailure, "resource_evidence_stale"):
                        owner.sample()
                self.assertEqual((self.native.read_count, events["listeners"]), (8, 7))

    def test_original_scheduled_sleep_and_start_gap_refuse_over_750ms_acquisition(self):
        for duration in (0.75, 0.75 + 2**-10):
            with self.subTest(duration=duration):
                owner, events = self.churn(3, duration=duration)
                snapshot = owner.sample()
                self.assertEqual(snapshot.measured_at - snapshot.started_at, duration)
                self.clock.value += 0.25
                if duration == 0.75:
                    owner.sample()
                    self.assertEqual(self.native.read_count, 10)
                else:
                    with self.assertRaisesRegex(ResourceFailure, "resource_evidence_stale"):
                        owner.sample()
                    self.assertEqual((self.native.read_count, events["listeners"]), (8, 7))

    def test_late_accounting_or_member_api_failure_is_never_retried(self):
        for api in ("accounting", "members"):
            with self.subTest(api=api):
                owner, events = self.churn(3)
                original = getattr(self.native, api)
                calls = []

                def failing(job, original=original, calls=calls):
                    calls.append(True)
                    if len(calls) == 7:
                        raise ResourceFailure("resource_evidence_unavailable")
                    return original(job)

                setattr(self.native, api, failing)
                with self.assertRaisesRegex(ResourceFailure, "resource_evidence_unavailable"):
                    owner.sample()
                self.assertEqual((len(calls), events["listeners"]), (7, 6))
                self.assertTrue(owner.cleanup_complete and owner.association_counts_complete)

    def test_late_unobserved_birth_is_disclosed_with_zero_or_positive_prior_gap(self):
        for allowance in (0, 2):
            with self.subTest(allowance=allowance):
                owner, events = self.churn(3)
                self.native.total += allowance
                self.job.total_processes = self.native.total
                original = self.native.listeners

                def listeners(pids, original=original, events=events):
                    rows = original(pids)
                    if not self.job.drained and events["listeners"] == 7:
                        self.native.total += 1
                        self.job.total_processes = self.native.total
                    return rows

                self.native.listeners = listeners
                self.assertEqual(owner.sample().total_births, 6 + allowance)
                self.assertEqual((self.native.read_count, events["listeners"]), (8, 7))
                owner.close()
                self.assertEqual(owner.final_total, 6 + allowance)
                self.assertTrue(owner.cleanup_complete and owner.association_counts_complete)
                state = association_state()
                guard.accumulate_associations(state, owner)
                self.assertEqual(state, association_state(6 + allowance, 2))


class SampledCurrentCensusTests(unittest.TestCase):
    make = AdapterTests.make
    make_witnessed = StableSnapshotFakeTests.make_witnessed
    expire = StableSnapshotFakeTests.expire
    birth = BirthLedgerFakeTests.birth
    during_members = NonAtomicCensusFakeTests.during_members

    BOUNDARIES = (
        ("accounting", 1),
        ("members", 1),
        ("identity", 1),
        ("rss", 1),
        ("listeners", 1),
        ("accounting", 2),
        ("members", 2),
        ("identity", 5),
        ("host", 1),
    )

    def at_read(self, owner, method, ordinal, when, action):
        original = getattr(self.native, method)
        calls = []
        fired = []

        def instrumented(*args):
            if self.job.drained:
                return original(*args)
            calls.append(True)
            trigger = len(calls) == ordinal and not fired
            if trigger and when == "before":
                fired.append(True)
                action()
            value = original(*args)
            if trigger and when == "after":
                fired.append(True)
                action()
            return value

        setattr(self.native, method, instrumented)
        return fired

    def test_historical_gap_plus_nonatomic_initial_arrival_never_invents_or_erases_coverage(self):
        for gap in (0, 1, 5):
            for turnover in (False, True):
                with self.subTest(gap=gap, turnover=turnover):
                    owner = self.make_witnessed()
                    self.native.total = self.job.total_processes = 2 + gap

                    def arrived(turnover=turnover):
                        if turnover:
                            self.expire()
                        self.birth()

                    self.at_read(owner, "accounting", 1, "after", arrived)
                    snapshot = owner.sample()
                    self.assertEqual(snapshot.total_births, 3 + gap)
                    self.assertIn((13, 103), owner.witnessed_births)
                    self.assertEqual(snapshot.active_processes, 2 if turnover else 3)
                    owner.close()
                    state = association_state()
                    guard.accumulate_associations(state, owner)
                    self.assertEqual(state, association_state(3 + gap, 3))

    def test_unseen_or_rejected_association_at_each_read_boundary_is_only_disclosed(self):
        for method, ordinal in self.BOUNDARIES:
            for when in ("before", "after"):
                with self.subTest(method=method, ordinal=ordinal, when=when):
                    owner = self.make_witnessed()

                    def unobserved():
                        self.native.total = self.job.total_processes = 3

                    fired = self.at_read(owner, method, ordinal, when, unobserved)
                    snapshot = owner.sample()
                    self.assertEqual(fired, [True])
                    self.assertEqual((snapshot.active_processes, snapshot.rss_bytes), (2, 200))
                    self.assertEqual(owner.witnessed_births, {(11, 101), (12, 102)})
                    self.assertEqual(owner.incomplete_exit_births, set())
                    owner.close()
                    state = association_state()
                    guard.accumulate_associations(state, owner)
                    self.assertEqual(state, association_state(3, 2))

    def test_visible_arrival_at_each_read_is_audited_or_remains_after_last_captured_census(self):
        for method, ordinal in self.BOUNDARIES:
            for when in ("before", "after"):
                with self.subTest(method=method, ordinal=ordinal, when=when):
                    owner = self.make_witnessed()
                    rss_reads = []
                    original_rss = self.native.rss

                    def rss(handle, original_rss=original_rss, rss_reads=rss_reads):
                        rss_reads.append(handle)
                        return original_rss(handle)

                    self.native.rss = rss
                    fired = self.at_read(owner, method, ordinal, when, self.birth)
                    snapshot = owner.sample()
                    self.assertEqual(fired, [True])
                    if (13, 103) in owner.witnessed_births:
                        self.assertIn(13, rss_reads)
                        self.assertEqual(snapshot.active_processes, 3)
                    else:
                        # A birth after the final returned membership list cannot
                        # be reconstructed by a sampled census. Do not promote it.
                        self.assertEqual(snapshot.active_processes, 2)
                        self.assertNotIn(13, rss_reads)
                        self.clock.value += 0.25
                        self.assertEqual(owner.sample().active_processes, 3)
                        self.assertIn(13, rss_reads)
                        self.assertIn((13, 103), owner.witnessed_births)
                    owner.close()
                    state = association_state()
                    guard.accumulate_associations(state, owner)
                    self.assertEqual(state, association_state(3, 3))

    def test_positive_gap_never_exempts_any_current_live_api_or_identity_failure(self):
        for failure, category in (
            ("open_member", "resource_evidence_unavailable"),
            ("belongs", "ownership_unproven"),
            ("identity", "process_wait_failed"),
            ("rss", "resource_evidence_unavailable"),
            ("listeners", "owned_listener_not_loopback"),
            ("members", "membership_incomplete"),
            ("accounting", "resource_evidence_unavailable"),
        ):
            with self.subTest(failure=failure):
                owner = self.make_witnessed()
                self.birth()
                self.native.total = self.job.total_processes = 7
                original = getattr(self.native, failure)
                calls = []

                def failed(
                    *args, original=original, failure=failure, category=category, calls=calls
                ):
                    if self.job.drained:
                        return original(*args)
                    if failure in {"members", "accounting", "listeners"} or args[0] == 13:
                        calls.append(True)
                        raise ResourceFailure(category)
                    return original(*args)

                setattr(self.native, failure, failed)
                with self.assertRaisesRegex(ResourceFailure, category):
                    owner.sample()
                self.assertEqual(calls, [True])
                self.assertNotIn((13, 103), owner.witnessed_births)
                self.assertTrue(owner.cleanup_complete and owner.association_counts_complete)
                state = association_state()
                guard.accumulate_associations(state, owner)
                self.assertEqual(state, association_state(7, 2))

    def test_stable_conservation_includes_proven_incomplete_identities(self):
        owner = self.make_witnessed()
        original_accounting, original_rss = self.native.accounting, self.native.rss

        def accounting(job):
            _, active, cpu = original_accounting(job)
            return 2, active, cpu

        def rss(handle):
            value = original_rss(handle)
            if handle == 13:
                self.expire(handle)
            return value

        self.native.accounting, self.native.rss = accounting, rss
        self.at_read(owner, "members", 1, "before", self.birth)
        with self.assertRaisesRegex(ResourceFailure, "association_count_underflow"):
            owner.sample()
        self.assertEqual(owner.incomplete_exit_births, {(13, 103)})
        self.assertEqual(owner.witnessed_births, {(11, 101), (12, 102)})
        self.assertTrue(owner.cleanup_complete and owner.association_counts_complete)
        self.assertEqual(owner.final_total, 3)

    def test_monotonic_lifetime_accounting_remains_fatal_despite_accepted_prior_gap(self):
        owner = self.make_witnessed()
        self.native.total = self.job.total_processes = 4
        owner.sample()
        self.clock.value += 0.25
        self.native.total = 3
        before = self.native.read_count
        with self.assertRaisesRegex(ResourceFailure, "job_accounting_regressed"):
            owner.sample()
        self.assertEqual(self.native.read_count, before + 1)
        self.assertEqual(owner.last_total, 4)
        self.assertTrue(owner.cleanup_complete and owner.association_counts_complete)

    def test_gap_never_erases_observed_rss_peak_or_listener_union(self):
        case = ProvenExitTimingTests()
        owner, _ = case.timeline("rss")
        original = case.native.rss

        def rss(handle):
            value = original(handle)
            if handle == 13:
                case.native.total = case.job.total_processes = 5
            return value

        case.native.rss = rss
        snapshot = owner.sample()
        self.assertEqual((snapshot.rss_bytes, snapshot.owned_loopback_listeners), (300, 1))
        self.assertEqual(owner.observed_peak_rss_bytes, 300)
        self.assertEqual(len(owner.observed_listeners), 1)
        owner.close()
        state = association_state()
        guard.accumulate_associations(state, owner)
        self.assertEqual(state, association_state(5, 2))

    def test_in_acquisition_gaps_accumulate_exactly_once_across_jobs_and_payload_failure(self):
        case = SampledRunOwnedTests()
        state = case.state()
        for gap, failure in ((1, None), (2, None), (3, "payload")):
            error, owners, _ = case.run_job(state, total=2, failure=failure, unobserved=gap)
            self.assertEqual(str(error) if error else None, "child_exit" if failure else None)
            saved = dict(state)
            owners[0].close()
            owners[0].job.close()
            self.assertEqual(state, saved)
        self.assertEqual(case.counts(state), (12, 6, 6))
        self.assertEqual((state["cpu_seconds"], state["peak_rss_bytes"]), (9, 200))
        self.assertTrue(state["association_counts_complete"] and state["all_owned_cleanup"])

    def test_gap_with_unavailable_final_count_preserves_partial_aggregates_and_blocks_launch(self):
        case = SampledRunOwnedTests()
        state = case.state(8, 3)
        error, owners, _ = case.run_job(state, total=5.0, unobserved=3)
        self.assertEqual(str(error), "association_counts_unavailable")
        self.assertEqual(case.counts(state), (8, 3, 5))
        self.assertFalse(state["association_counts_complete"])
        self.assertTrue(owners[0].cleanup_complete)
        self.assertEqual((state["cpu_seconds"], state["peak_rss_bytes"]), (3, 200))
        error, owners, _ = case.run_job(state)
        self.assertEqual(str(error), "association_counts_incomplete")
        self.assertEqual(owners, [])

    def test_gap_never_hides_cleanup_failure_or_discards_recovered_counts(self):
        case = SampledRunOwnedTests()
        state = case.state(8, 3)
        error, owners, _ = case.run_job(state, total=2, unobserved=3, failure="cleanup")
        self.assertEqual(str(error), "cleanup_incomplete")
        self.assertEqual(case.counts(state), (13, 5, 8))
        self.assertTrue(state["association_counts_complete"])
        self.assertFalse(owners[0].cleanup_complete or state["all_owned_cleanup"])
        self.assertIs(state["pending_payload"], True)


class PendingOmissionTests(unittest.TestCase):
    make = AdapterTests.make
    make_witnessed = StableSnapshotFakeTests.make_witnessed
    birth = BirthLedgerFakeTests.birth

    def omitted(self, *, initial=False, signal_read=None, previously_witnessed=True):
        owner = self.make_witnessed() if previously_witnessed else self.make()
        original_members, original_rss = self.native.members, self.native.rss
        events = {"members": 0, "rss": [], "listeners": []}
        original_listeners = self.native.listeners

        def members(job):
            events["members"] += 1
            if events["members"] == (1 if initial else 2):
                self.native.pids.remove(11)
            if events["members"] == signal_read:
                self.native.identities[11] = (101, True)
            return original_members(job)

        def rss(handle):
            events["rss"].append((events["members"], handle))
            return original_rss(handle)

        def listeners(pids):
            if not self.job.drained:
                events["listeners"].append(set(pids))
            return original_listeners(pids)

        self.native.members, self.native.rss, self.native.listeners = members, rss, listeners
        return owner, events

    def test_initial_and_closing_pending_omission_need_signal_then_fresh_stable_pass(self):
        for initial in (False, True):
            with self.subTest(initial=initial):
                owner, events = self.omitted(initial=initial, signal_read=3)
                snapshot = owner.sample()
                self.assertEqual((events["members"], snapshot.active_processes), (6, 1))
                self.assertEqual(snapshot.rss_bytes, 200)
                self.assertIn((11, 101), owner.absent_births)
                self.assertEqual(owner.incomplete_exit_births, set())
                self.assertEqual(len(owner.witnessed_births), 2)
                owner.close()
                state = association_state()
                guard.accumulate_associations(state, owner)
                self.assertEqual(state, association_state(2, 2))

    def test_signal_immediately_after_false_read_is_deferred_without_accepting_that_attempt(self):
        for initial in (False, True):
            with self.subTest(initial=initial):
                owner, events = self.omitted(initial=initial)
                original = self.native.identity
                fired = []

                def identity(
                    handle, original=original, fired=fired, initial=initial, events=events
                ):
                    value = original(handle)
                    if handle == 11 and events["members"] == (1 if initial else 2) and not fired:
                        fired.append(True)
                        self.native.identities[11] = (101, True)
                    return value

                self.native.identity = identity
                snapshot = owner.sample()
                self.assertEqual(fired, [True])
                self.assertEqual(snapshot.active_processes, 1)
                self.assertEqual(events["members"], 4 if initial else 6)
                self.assertIn((11, 101), owner.absent_births)

    def test_pending_signal_during_rss_or_listener_read_requires_subsequent_stability(self):
        for initial in (False, True):
            for method in ("rss", "listeners"):
                with self.subTest(initial=initial, method=method):
                    owner, events = self.omitted(initial=initial)
                    original = getattr(self.native, method)
                    fired = []

                    def signal(
                        *args,
                        original=original,
                        fired=fired,
                        initial=initial,
                        events=events,
                        owner=owner,
                    ):
                        value = original(*args)
                        if events["members"] == (1 if initial else 3) and not fired:
                            self.assertNotIn((11, 101), owner.absent_births)
                            fired.append(True)
                            self.native.identities[11] = (101, True)
                        return value

                    setattr(self.native, method, signal)
                    snapshot = owner.sample()
                    self.assertEqual(snapshot.active_processes, 1)
                    self.assertEqual(events["members"], 4 if initial else 6)
                    self.assertEqual(owner.observed_peak_rss_bytes, 200)

    def test_permanently_missing_live_handle_is_audited_once_per_attempt_and_never_accepted(self):
        for initial, category in (
            (True, "departure_not_signaled"),
            (False, "final_survivor_missing"),
        ):
            with self.subTest(initial=initial):
                owner, events = self.omitted(initial=initial)
                self.native.rss_each = 3 * GIB // 4
                with self.assertRaisesRegex(ResourceFailure, category):
                    owner.sample()
                self.assertEqual(events["members"], 16)
                self.assertEqual([handle for _, handle in events["rss"]].count(11), 8)
                self.assertEqual([handle for _, handle in events["rss"]].count(12), 8)
                self.assertEqual(events["listeners"], [{11, 12}] * 8)
                self.assertEqual(owner.observed_peak_rss_bytes, 3 * GIB // 2)
                self.assertNotIn((11, 101), owner.absent_births)
                self.assertTrue(owner.failed and owner.cleanup_complete)

    def test_resolving_on_seventh_attempt_allows_eighth_but_eighth_resolution_cannot_return(self):
        for signal_read in (13, 15):
            with self.subTest(signal_read=signal_read):
                owner, events = self.omitted(signal_read=signal_read)
                if signal_read == 13:
                    self.assertEqual(owner.sample().active_processes, 1)
                else:
                    with self.assertRaisesRegex(ResourceFailure, "resource_evidence_unstable"):
                        owner.sample()
                self.assertEqual(events["members"], 16)
                self.assertIn((11, 101), owner.absent_births)

    def test_pending_reappearance_on_each_next_census_refuses_even_same_live_identity(self):
        for initial in (False, True):
            for reappear in (2, 3) if initial else (3, 4):
                for reused in (False, True):
                    with self.subTest(initial=initial, reappear=reappear, reused=reused):
                        owner, events = self.omitted(initial=initial)
                        original = self.native.members

                        def members(
                            job, original=original, reappear=reappear, reused=reused, events=events
                        ):
                            if events["members"] + 1 == reappear:
                                self.native.pids.add(11)
                                if reused:
                                    self.native.identities[11] = (999, False)
                            return original(job)

                        self.native.members = members
                        with self.assertRaisesRegex(ResourceFailure, "retry_member_set_expanded"):
                            owner.sample()
                        self.assertEqual(events["members"], reappear)
                        self.assertTrue(owner.cleanup_complete)

    def test_pending_api_identity_or_ownership_failure_never_waits_for_later_signal(self):
        for initial in (False, True):
            for method, category in (
                ("identity", "process_wait_failed"),
                ("belongs", "ownership_unproven"),
                ("rss", "resource_evidence_unavailable"),
                ("listeners", "owned_listener_not_loopback"),
            ):
                with self.subTest(initial=initial, method=method):
                    owner, events = self.omitted(initial=initial)
                    original = getattr(self.native, method)
                    failures = []

                    def failed(
                        *args,
                        original=original,
                        method=method,
                        category=category,
                        initial=initial,
                        events=events,
                        failures=failures,
                    ):
                        if not self.job.drained and events["members"] >= (1 if initial else 3):
                            if method == "listeners" or args[0] == 11:
                                failures.append(True)
                                raise ResourceFailure(category)
                        return original(*args)

                    setattr(self.native, method, failed)
                    with self.assertRaisesRegex(ResourceFailure, category):
                        owner.sample()
                    self.assertEqual(failures, [True])
                    self.assertNotIn((11, 101), owner.absent_births)
                    self.assertTrue(owner.cleanup_complete)

    def test_pending_handle_rss_and_listener_caps_remain_fatal_with_evidence_preserved(self):
        for resource in ("rss", "listeners"):
            with self.subTest(resource=resource):
                owner, events = self.omitted()
                if resource == "rss":
                    original = self.native.rss

                    def rss(handle, original=original, events=events):
                        return GIB + 1 if events["members"] >= 3 else original(handle)

                    self.native.rss = rss
                    category = "owned_rss_exceeded"
                else:
                    original = self.native.listeners

                    def listeners(pids, original=original, events=events):
                        rows = original(pids)
                        if not self.job.drained and events["members"] >= 3:
                            return {(11, 2, b"\x7f\0\0\1", port) for port in range(1, 4098)}
                        return rows

                    self.native.listeners = listeners
                    category = "listener_evidence_limit"
                with self.assertRaisesRegex(ResourceFailure, category):
                    owner.sample()
                if resource == "rss":
                    self.assertEqual(owner.observed_peak_rss_bytes, 2 * GIB + 2)
                else:
                    self.assertEqual(len(owner.observed_listeners), 4097)
                self.assertTrue(owner.cleanup_complete)

    def test_combined_visible_and_pending_live_set_must_fit_active_cap_at_both_censuses(self):
        for initial, category in (
            (True, "initial_members_exceed_limit"),
            (False, "final_members_exceed_limit"),
        ):
            with self.subTest(initial=initial):
                owner = self.make()
                self.native.pids = set(range(11, 43))
                self.native.identities = {pid: (pid + 90, False) for pid in self.native.pids}
                self.native.total = self.job.total_processes = 32
                owner.sample()
                self.clock.value += 0.25
                original = self.native.members
                calls = []

                def members(job, original=original, calls=calls, initial=initial):
                    calls.append(True)
                    if len(calls) == (1 if initial else 2):
                        self.native.pids.remove(11)
                        self.birth(43, 133)
                    return original(job)

                self.native.members = members
                with self.assertRaisesRegex(ResourceFailure, category):
                    owner.sample()
                self.assertEqual(len(self.native.pids), 32)
                self.assertEqual(len(calls), 1 if initial else 2)
                self.assertTrue(owner.cleanup_complete)

    def test_visible_newcomer_is_fully_audited_while_other_identity_is_pending(self):
        for foreign in (False, True):
            with self.subTest(foreign=foreign):
                owner, events = self.omitted(signal_read=5)
                original_members, original_belongs = self.native.members, self.native.belongs

                def members(job, original_members=original_members, events=events):
                    if events["members"] == 2:
                        self.birth()
                    return original_members(job)

                self.native.members = members
                self.native.belongs = (
                    lambda handle, job, foreign=foreign, original_belongs=original_belongs: (
                        not (foreign and handle == 13) and original_belongs(handle, job)
                    )
                )
                if foreign:
                    with self.assertRaisesRegex(ResourceFailure, "ownership_unproven"):
                        owner.sample()
                    self.assertNotIn((13, 103), owner.witnessed_births)
                else:
                    snapshot = owner.sample()
                    self.assertEqual((snapshot.active_processes, snapshot.rss_bytes), (2, 300))
                    self.assertIn((13, 103), owner.witnessed_births)
                    self.assertEqual(events["members"], 8)

    def test_previously_proved_terminal_identity_cannot_revive_into_pending_state(self):
        for incomplete in (False, True):
            with self.subTest(incomplete=incomplete):
                case = ProvenExitTimingTests()
                owner, _ = case.timeline("rss" if incomplete else "post_listener_return")
                owner.sample()
                case.clock.value += 0.25
                case.native.identities[13] = (103, False)
                with self.assertRaisesRegex(ResourceFailure, "pid_reuse_or_exit"):
                    owner.sample()
                self.assertTrue(owner.cleanup_complete)

    def test_signaled_still_listed_object_cannot_revive_when_later_omitted(self):
        owner, events = self.omitted()
        original_listeners, original_members = self.native.listeners, self.native.members

        def listeners(pids):
            rows = original_listeners(pids)
            if not self.job.drained and events["members"] == 1:
                self.native.identities[12] = (102, True)
            return rows

        def members(job):
            if events["members"] == 2:
                self.native.pids.remove(12)
                self.native.identities[12] = (102, False)
            return original_members(job)

        self.native.listeners, self.native.members = listeners, members
        with self.assertRaisesRegex(ResourceFailure, "pid_reuse_or_exit"):
            owner.sample()
        self.assertEqual(events["members"], 3)

    def test_pending_resolution_does_not_reset_inclusive_deadline(self):
        for duration in (1.0, 1.0 + 2**-10):
            with self.subTest(duration=duration):
                owner, events = self.omitted(signal_read=3)
                started = self.clock.value
                original = self.native.members

                def members(
                    job, original=original, duration=duration, started=started, events=events
                ):
                    value = original(job)
                    if events["members"] == 5:
                        self.clock.value = started + duration
                    return value

                self.native.members = members
                if duration == 1:
                    snapshot = owner.sample()
                    self.assertEqual(snapshot.measured_at - snapshot.started_at, 1)
                else:
                    with self.assertRaisesRegex(ResourceFailure, "resource_evidence_stale"):
                        owner.sample()
                self.assertEqual(events["members"], 6)

    def test_newly_witnessed_closing_omission_requires_same_pending_exit_proof(self):
        owner, events = self.omitted(signal_read=3, previously_witnessed=False)
        self.assertEqual(owner.witnessed_births, set())
        snapshot = owner.sample()
        self.assertEqual((snapshot.active_processes, events["members"]), (1, 6))
        self.assertEqual(owner.witnessed_births, {(11, 101), (12, 102)})
        self.assertEqual(owner.absent_births, {(11, 101)})
        self.assertEqual(owner.incomplete_exit_births, set())

    def test_pending_retry_observations_preserve_rss_peak_and_endpoint_union(self):
        owner, events = self.omitted(signal_read=5)
        original_rss, original_listeners = self.native.rss, self.native.listeners
        row = (11, 2, b"\x7f\0\0\1", 9000)

        def rss(handle):
            value = original_rss(handle)
            return 300 if handle == 11 and events["members"] == 3 else value

        def listeners(pids):
            rows = original_listeners(pids)
            return rows | {row} if not self.job.drained and events["members"] == 3 else rows

        self.native.rss, self.native.listeners = rss, listeners
        snapshot = owner.sample()
        self.assertEqual((snapshot.active_processes, snapshot.rss_bytes), (1, 400))
        self.assertEqual(snapshot.owned_loopback_listeners, 1)
        self.assertEqual(owner.observed_peak_rss_bytes, 400)
        self.assertEqual(owner.observed_listeners, {row})
        self.assertEqual(events["members"], 8)
        owner.close()
        self.assertTrue(owner.cleanup_complete and owner.association_counts_complete)

    def test_pending_exhaustion_never_masks_cleanup_failure_or_loses_recovered_counts(self):
        owner, events = self.omitted()
        self.native.fail = "listener_cleanup"
        with self.assertRaisesRegex(ResourceFailure, "owned_listener_cleanup_failed"):
            owner.sample()
        self.assertEqual(events["members"], 16)
        self.assertFalse(owner.cleanup_complete)
        self.assertTrue(owner.association_counts_complete)
        state = association_state(8, 3)
        guard.accumulate_associations(state, owner)
        self.assertEqual(state, association_state(10, 5))
        with self.assertRaisesRegex(ResourceFailure, "owned_job_cleanup_failed"):
            owner.close()
        self.assertEqual(state, association_state(10, 5))


class MembershipPredicateFakeTests(unittest.TestCase):
    """Fixed categories for retained gates under explicit birth reconciliation."""

    make = AdapterTests.make
    expire = StableSnapshotFakeTests.expire
    during_listeners = StableSnapshotFakeTests.during_listeners
    make_witnessed = StableSnapshotFakeTests.make_witnessed

    def configure(self, category):
        owner = self.make_witnessed()
        original_accounting = self.native.accounting
        original_identity = self.native.identity
        identity_reads = {}

        def accounting(job):
            read = self.native.read_count + 1
            if read == 3:
                if category == "retry_member_set_expanded":
                    self.native.pids.add(11)
                elif category == "departure_handle_missing":
                    # Deliberately corrupt fake evidence to cover a defensive leaf;
                    # normal source control flow does not discard retained handles.
                    del owner.handles[11]
                elif category == "departure_not_signaled":
                    self.native.identities[11] = (101, False)
            total, active, cpu = original_accounting(job)
            if category == "initial_active_count_mismatch" and read % 2 == 1:
                active -= 1
            if category == "final_active_count_mismatch" and read % 2 == 0:
                active -= 1
            return total, active, cpu

        def identity(handle):
            identity_reads[handle] = identity_reads.get(handle, 0) + 1
            if (
                category == "final_survivor_signaled"
                and handle == 11
                and identity_reads[handle] == 3
            ):
                self.expire(handle)
            return original_identity(handle)

        def changed(number, rows):
            if number != 1:
                return rows
            if category in {
                "retry_member_set_expanded",
                "departure_handle_missing",
            }:
                self.expire()
            elif category == "departed_member_still_listed":
                self.native.identities[11] = (101, True)
            elif category == "final_survivor_missing":
                self.native.pids.remove(11)
            return rows

        self.native.accounting = accounting
        self.native.identity = identity
        self.during_listeners(changed)
        if category == "initial_members_exceed_limit":
            self.native.pids = set(range(1, 34))
            self.native.total = self.job.total_processes = 33
        elif category == "association_count_underflow":
            self.native.pids.add(13)
            self.native.identities[13] = (103, False)
        elif category == "departure_not_signaled":
            self.native.pids.remove(11)
        return owner

    def test_each_fixed_membership_predicate_category_is_distinct_and_source_free(self):
        categories = (
            "initial_active_count_mismatch",
            "initial_members_exceed_limit",
            "retry_member_set_expanded",
            "departure_handle_missing",
            "departure_not_signaled",
            "association_count_underflow",
            "departed_member_still_listed",
            "final_survivor_missing",
            "final_active_count_mismatch",
        )
        self.assertEqual(len(set(categories)), 9)
        for category in categories:
            with self.subTest(category=category):
                owner = self.configure(category)
                with self.assertRaises(ResourceFailure) as refused:
                    owner.sample()
                self.assertEqual(str(refused.exception), category)
                self.assertRegex(str(refused.exception), r"\A[a-z_]{1,64}\Z")
                self.assertTrue(owner.failed and owner.cleanup_complete and self.job.drained)

    def test_survivor_equality_decomposition_is_logically_identical(self):
        subsets = [{value for value in range(4) if bits & (1 << value)} for bits in range(16)]
        for members in subsets:
            for departed in subsets:
                for survivors in subsets:
                    with self.subTest(members=members, departed=departed, survivors=survivors):
                        original = survivors == members - departed
                        diagnostic = (
                            survivors <= members
                            and not survivors & departed
                            and members - departed <= survivors
                        )
                        self.assertEqual(diagnostic, original)


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
        with self.assertRaisesRegex(ResourceFailure, "endpoint_table_probe_invalid"):
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
                with self.assertRaisesRegex(ResourceFailure, "endpoint_table_query_failed"):
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


class FixedResourceDiagnosticTests(unittest.TestCase):
    """Actual emitters with inert ctypes callbacks; no DLL or native Job calls."""

    CATEGORIES = {
        "job_membership_query_failed": "belongs",
        "job_limits_set_failed": "configure",
        "job_limits_query_failed": "configure",
        "job_accounting_query_failed": "accounting",
        "job_accounting_invalid": "accounting",
        "job_members_query_failed": "members",
        "job_member_ids_invalid": "members",
        "process_open_initial_other": "open_member",
        "process_times_query_failed": "identity",
        "process_birth_invalid": "identity",
        "process_rss_query_failed": "rss",
        "host_memory_query_failed": "host",
        "host_cpu_query_failed": "host",
        "host_disk_query_failed": "host",
        "host_metrics_invalid": "host",
        "endpoint_table_probe_invalid": "listeners",
        "endpoint_table_query_failed": "listeners",
        "endpoint_table_size_invalid": "listeners",
        "endpoint_table_rows_invalid": "listeners",
        "endpoint_port_invalid": "listeners",
        "final_job_accounting_regressed": "final_accounting",
        "job_accounting_regressed": "sample_accounting",
        "host_cpu_delta_invalid": "sample_host",
    }

    @classmethod
    def restored(cls):
        tree = ast.parse((ROOT / "scripts/windows_hosted_qualification.py").read_text())
        removed = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "resource_need"
                and len(node.args) == 2
            ):
                code = node.args[1]
                if isinstance(code, ast.Constant) and code.value in cls.CATEGORIES:
                    removed.append(node.args.pop().value)
                elif isinstance(code, ast.IfExp) and {
                    item.value
                    for item in ast.walk(code)
                    if isinstance(item, ast.Constant) and isinstance(item.value, str)
                } == {
                    "process_open_initial_invalid_parameter",
                    "process_open_initial_access_denied",
                    "process_open_initial_other",
                }:
                    node.args.pop()
                    removed.append("process_open_initial_other")
        namespace = dict(vars(guard))
        classes = ast.Module(
            body=[
                node
                for node in tree.body
                if isinstance(node, ast.ClassDef)
                and node.name in {"NativeMetrics", "QualificationResources"}
            ],
            type_ignores=[],
        )
        exec(compile(classes, "<inert diagnostic equivalence>", "exec"), namespace)
        return types.SimpleNamespace(**namespace), tree, removed

    def native_fixture(self, module, category, *, fail=True):
        native = module.NativeMetrics.__new__(module.NativeMetrics)
        events, saved = [], []
        fault = category if fail else ""

        def write(pointer, value, kind=guard.U64):
            ctypes.cast(pointer, ctypes.POINTER(kind))[0] = value

        def belongs(process, job, pointer):
            events.append("membership")
            write(pointer, 0, ctypes.c_int32)
            return fault != "job_membership_query_failed"

        def set_limits(handle, kind, pointer, size):
            events.append(("set_limits", kind, size))
            saved.append(ctypes.string_at(pointer, size))
            return fault != "job_limits_set_failed"

        def query(handle, kind, pointer, size, length):
            events.append(("query", kind, size))
            if kind == 9:
                ctypes.memmove(pointer, saved[0], size)
                return fault != "job_limits_query_failed"
            if kind == 1:
                value = Accounting()
                value.total, value.active, value.user = 2, 2, 10_000_000
                if fault == "job_accounting_invalid":
                    value.total = 1
                ctypes.memmove(pointer, ctypes.byref(value), size)
                return fault != "job_accounting_query_failed"
            value = ProcessIds()
            value.assigned = value.count = 2
            value.pids[0], value.pids[1] = 11, 12
            if fault == "job_member_ids_invalid":
                value.pids[1] = 11
            ctypes.memmove(pointer, ctypes.byref(value), size)
            return fault != "job_members_query_failed"

        def open_process(rights, inherit, pid):
            events.append(("open", rights, inherit))
            return 0 if fault == "process_open_initial_other" else 77

        def times(handle, birth, exited, kernel, user):
            events.append("process_times")
            write(birth, 0 if fault == "process_birth_invalid" else 101)
            return fault != "process_times_query_failed"

        def wait(handle, timeout):
            events.append(("wait", timeout))
            return 258

        def rss(handle, pointer, size):
            events.append("rss")
            value = MemoryCounters()
            value.cb, value.working_set = size, 100
            ctypes.memmove(pointer, ctypes.byref(value), size)
            return fault != "process_rss_query_failed"

        def memory(pointer):
            events.append("host_memory")
            value = guard.HostMemory()
            value.length = ctypes.sizeof(value)
            value.total, value.available = 8 * GIB, 5 * GIB
            if fault == "host_metrics_invalid":
                value.total = 0
            ctypes.memmove(pointer, ctypes.byref(value), ctypes.sizeof(value))
            return fault != "host_memory_query_failed"

        def system_times(idle, kernel, user):
            events.append("host_cpu")
            for pointer, value in ((idle, 100), (kernel, 200), (user, 100)):
                write(pointer, value)
            return fault != "host_cpu_query_failed"

        def disk(root, available, total, free):
            events.append("host_disk")
            for pointer, value in ((available, 10 * GIB), (total, 20 * GIB), (free, 10 * GIB)):
                write(pointer, value)
            return fault != "host_disk_query_failed"

        def table(buffer, size, ordered, family, table_class, reserved):
            events.append(("endpoint", buffer is not None, family, table_class))
            row = Tcp4()
            row.pid, row.state, row.port = 11, 2, 0x2823
            row.local[:] = b"\x7f\0\0\1"
            if fault == "endpoint_port_invalid":
                row.port = 0
            payload = b"\1\0\0\0" + bytes(row)
            if family != 2 or table_class != 3:
                payload = b"\0" * 4
            write(size, len(payload), guard.U32)
            if buffer is None:
                return 5 if fault == "endpoint_table_probe_invalid" else 122
            if fault == "endpoint_table_query_failed":
                return 5
            ctypes.memmove(buffer, payload, len(payload))
            if fault == "endpoint_table_size_invalid":
                write(size, len(payload) + 1, guard.U32)
            if fault == "endpoint_table_rows_invalid":
                ctypes.memmove(buffer, b"\xff" * 4, 4)
            return 0

        native.k = types.SimpleNamespace(
            IsProcessInJob=belongs,
            SetInformationJobObject=set_limits,
            QueryInformationJobObject=query,
            OpenProcess=open_process,
            GetProcessTimes=times,
            WaitForSingleObject=wait,
            K32GetProcessMemoryInfo=rss,
            GlobalMemoryStatusEx=memory,
            GetSystemTimes=system_times,
            GetDiskFreeSpaceExW=disk,
            GetCurrentProcess=lambda: 99,
        )
        native.ip = types.SimpleNamespace(GetExtendedTcpTable=table, GetExtendedUdpTable=table)
        job = types.SimpleNamespace(handle=100)
        operation = self.CATEGORIES[category]
        args = {
            "belongs": (77, job),
            "configure": (job,),
            "accounting": (job,),
            "members": (job,),
            "open_member": (11, job),
            "identity": (77,),
            "rss": (77,),
            "host": (Path("inert-private-path"),),
            "listeners": ({11},),
        }
        if operation == "open_member":
            native.belongs = lambda handle, job: events.append("belongs") or True

        def invoke():
            with patch.object(module.c, "get_last_error", return_value=1, create=True):
                return getattr(native, operation)(*args[operation])

        return invoke, events

    def fixture(self, module, category, *, fail=True):
        if category not in {
            "final_job_accounting_regressed",
            "job_accounting_regressed",
            "host_cpu_delta_invalid",
        }:
            return self.native_fixture(module, category, fail=fail)
        clock, native, events = Clock(), FakeNative(), []
        for name in (
            "configure",
            "parent_outside",
            "accounting",
            "members",
            "open_member",
            "belongs",
            "identity",
            "rss",
            "host",
            "listeners",
            "close_handle",
        ):
            original = getattr(native, name)

            def traced(*args, name=name, original=original):
                events.append(name)
                return original(*args)

            setattr(native, name, traced)
        owner = module.QualificationResources(
            types.SimpleNamespace(WindowsJob=FakeJob),
            "inert-private-path",
            clock=clock,
            native_factory=lambda: native,
        )
        job = owner.job_factory()
        clock.value += 0.25
        if category == "final_job_accounting_regressed":
            owner.last_cpu = 4 if fail else 1
            return job.accounting, events
        if category == "job_accounting_regressed":
            owner.last_cpu = 2 if fail else 1
        elif fail:
            owner.host_seed = (1000, 2000, 1000)
        return owner._sample, events

    def test_all_generic_emitters_have_unique_fixed_sanitized_codes(self):
        source = ast.parse((ROOT / "scripts/windows_hosted_qualification.py").read_text())
        calls = [
            node
            for node in ast.walk(source)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "resource_need"
        ]
        self.assertTrue(all(len(node.args) == 2 and not node.keywords for node in calls))
        _, _, removed = self.restored()
        self.assertEqual(len(removed), 23)
        self.assertEqual(set(removed), self.CATEGORIES.keys())
        for category in removed:
            self.assertRegex(category, r"\A[a-z_]{1,64}\Z")

    def test_stripping_diagnostic_codes_preserves_reviewed_predicate_ast(self):
        _, restored, _ = self.restored()
        self.assertEqual(
            hashlib.sha256(ast.dump(restored, include_attributes=False).encode()).hexdigest(),
            "7c9614a1bfa2b32bf51eef4af2ae4f032933497b5e38ffecc28bfd2a147f89bd",
        )

    def test_all_twenty_three_actual_failures_keep_call_order_and_type(self):
        baseline, _, _ = self.restored()
        for category in self.CATEGORIES:
            with self.subTest(category=category):
                original, old_events = self.fixture(baseline, category)
                diagnostic, new_events = self.fixture(guard, category)
                with self.assertRaisesRegex(ResourceFailure, r"\Aresource_evidence_unavailable\Z"):
                    original()
                with self.assertRaises(ResourceFailure) as refused:
                    diagnostic()
                self.assertEqual(str(refused.exception), category)
                self.assertEqual(new_events, old_events)

    def test_diagnostics_preserve_success_values_and_call_order(self):
        baseline, _, _ = self.restored()
        for category in self.CATEGORIES:
            with self.subTest(category=category):
                original, old_events = self.fixture(baseline, category, fail=False)
                diagnostic, new_events = self.fixture(guard, category, fail=False)
                first, second = original(), diagnostic()
                if isinstance(first, types.SimpleNamespace):
                    first, second = vars(first), vars(second)
                self.assertEqual(first, second)
                self.assertEqual(new_events, old_events)

    def test_actual_failures_reach_only_sanitized_main_output(self):
        for category in self.CATEGORIES:
            with self.subTest(category=category):
                operation, _ = self.fixture(guard, category)
                output = io.StringIO()
                with (
                    patch.object(sys, "argv", ["guard", "--prepare", "--profile", "alpha"]),
                    patch.object(
                        guard,
                        "prepare",
                        side_effect=lambda profile, operation=operation: operation(),
                    ),
                    contextlib.redirect_stderr(output),
                ):
                    self.assertEqual(guard.main(), 1)
                self.assertEqual(
                    output.getvalue(), "K5_HOSTED_QUALIFICATION_REFUSED=" + category + "\n"
                )

    def test_default_fallback_and_external_error_redaction_are_unchanged(self):
        guard.resource_need(True)
        with self.assertRaisesRegex(ResourceFailure, r"\Aresource_evidence_unavailable\Z"):
            guard.resource_need(False)
        for error in (ResourceFailure("private/path pid=77"), OSError("private credential")):
            output = io.StringIO()
            with (
                patch.object(sys, "argv", ["guard", "--prepare", "--profile", "alpha"]),
                patch.object(guard, "prepare", side_effect=error),
                contextlib.redirect_stderr(output),
            ):
                self.assertEqual(guard.main(), 1)
            self.assertEqual(
                output.getvalue(), "K5_HOSTED_QUALIFICATION_REFUSED=qualification_failed\n"
            )


class OpenFailureDiagnosticTests(unittest.TestCase):
    SITES = (
        "initial_new",
        "initial_vanished",
        "closing_new",
        "closing_vanished",
        "initial_new_signaled",
        "initial_retained_signaled",
        "closing_departed_listed",
        "closing_late_signal",
        "closing_new_signaled",
    )

    @staticmethod
    def suffix(error):
        return "invalid_parameter" if error == 87 else "access_denied" if error == 5 else "other"

    def scenario(self, site, module=guard):
        clock, native, events = Clock(), FakeNative(), []
        owner = module.QualificationResources(
            types.SimpleNamespace(WindowsJob=FakeJob),
            "inert-owned",
            clock=clock,
            native_factory=lambda: native,
        )
        job = owner.job_factory()
        clock.value += 0.25
        reads = 0
        original_members, original_open = native.members, native.open_member
        original_listeners = native.listeners
        target = 13 if site.startswith("closing_new") or site == "closing_vanished" else 11
        retained = "signaled" in site or site in {"closing_departed_listed", "closing_late_signal"}
        if site in {"initial_new_signaled", "initial_retained_signaled"}:
            native.identities[11] = (101, True)
        if site == "initial_retained_signaled":
            owner.handles.update({11: (11, 101), 12: (12, 102)})
            owner.witnessed_births.update({(11, 101), (12, 102)})

        def members(job):
            nonlocal reads
            reads += 1
            events.append("members")
            if reads == 2:
                if site in {"closing_new", "closing_vanished", "closing_new_signaled"}:
                    native.pids.add(13)
                    native.identities[13] = (103, site == "closing_new_signaled")
                    native.total = job.total_processes = 3
                if site == "closing_late_signal":
                    native.identities[11] = (101, True)
            return original_members(job)

        def listeners(pids):
            events.append("listeners")
            if site == "closing_departed_listed" and not job.drained:
                native.identities[11] = (101, True)
            return original_listeners(pids)

        backend = module.NativeMetrics.__new__(module.NativeMetrics)

        def failed_open(rights, inherit, pid):
            events.append(("OpenProcess", rights, inherit))
            if site.endswith("vanished"):
                native.pids.discard(pid)
                native.identities[pid] = (native.identities[pid][0], True)
            return 0

        backend.k = types.SimpleNamespace(OpenProcess=failed_open)

        def open_member(pid, current_job):
            revalidation = pid in owner.handles
            events.append("revalidation_open" if revalidation else "first_open")
            if pid == target and (not retained or revalidation):
                return backend.open_member(pid, current_job)
            return original_open(pid, current_job)

        native.members, native.listeners, native.open_member = members, listeners, open_member
        return owner, native, events, retained

    def test_nondeferrable_open_failures_keep_each_fixed_error_class(self):
        for site in self.SITES:
            for error in (87, 5, 0, 6, 1168, 2**32 - 1):
                if error == 87 and site in {
                    "initial_new",
                    "initial_vanished",
                    "closing_new",
                    "closing_vanished",
                }:
                    # These four transitions are covered by UnopenedCensusTests.
                    continue
                with self.subTest(site=site, error=error):
                    owner, native, events, retained = self.scenario(site)

                    def saved_error(error=error, events=events):
                        events.append("saved_error")
                        return error

                    with (
                        patch.object(
                            guard.c, "get_last_error", side_effect=saved_error, create=True
                        ),
                        self.assertRaises(ResourceFailure) as refused,
                    ):
                        owner.sample()
                    phase = "revalidation" if retained else "initial"
                    self.assertEqual(
                        str(refused.exception), "process_open_" + phase + "_" + self.suffix(error)
                    )
                    opening = next(i for i, event in enumerate(events) if isinstance(event, tuple))
                    self.assertEqual(events[opening], ("OpenProcess", 0x00100410, False))
                    self.assertEqual(events[opening + 1], "saved_error")
                    self.assertEqual(events.count("saved_error"), 1)
                    self.assertLessEqual(native.read_count, 2)
                    self.assertTrue(owner.failed and owner.cleanup_complete and owner.job.drained)
                    self.assertTrue(owner.association_counts_complete)
                    self.assertLessEqual(len(owner.witnessed_births), owner.final_total)

    def test_null_open_reads_saved_error_once_before_any_other_operation(self):
        for error in (87, 5, 0, 6, 1168):
            with self.subTest(error=error):
                events = []
                native = NativeMetrics.__new__(NativeMetrics)
                native.k = types.SimpleNamespace(
                    OpenProcess=lambda *args, events=events: events.append("open") or 0
                )
                native.belongs = lambda *args: self.fail("No handle means no ownership query")
                native.identity = lambda *args: self.fail("No handle means no identity query")
                native.close_handle = lambda *args: self.fail("NULL is not a handle to close")
                with (
                    patch.object(
                        guard.c,
                        "get_last_error",
                        side_effect=lambda events=events, error=error: (
                            events.append("saved_error") or error
                        ),
                        create=True,
                    ),
                    self.assertRaises(ResourceFailure) as refused,
                ):
                    native.open_member(11, object())
                self.assertEqual(
                    str(refused.exception), "process_open_initial_" + self.suffix(error)
                )
                self.assertEqual(events, ["open", "saved_error"])

    def test_successful_open_never_reads_last_error_or_changes_rights_and_order(self):
        for signaled in (False, True):
            with self.subTest(signaled=signaled):
                events = []
                native = NativeMetrics.__new__(NativeMetrics)
                native.k = types.SimpleNamespace(
                    OpenProcess=lambda rights, inherit, pid, events=events: (
                        events.append(("open", rights, inherit)) or 77
                    )
                )
                native.belongs = lambda *args, events=events: events.append("belongs") or True
                native.identity = lambda *args, events=events, signaled=signaled: (
                    events.append("identity") or (101, signaled)
                )
                native.close_handle = lambda *args: self.fail("Successful handle stays retained")
                with patch.object(guard.c, "get_last_error", create=True) as saved_error:
                    self.assertEqual(native.open_member(11, object()), (77, 101))
                saved_error.assert_not_called()
                self.assertEqual(events, [("open", 0x00100410, False), "belongs", "identity"])

    def test_later_ownership_identity_and_close_failures_keep_original_categories(self):
        for failure in ("ownership_unproven", "process_times_query_failed", "process_wait_failed"):
            for close_fails in (False, True):
                with self.subTest(failure=failure, close_fails=close_fails):
                    events = []
                    native = NativeMetrics.__new__(NativeMetrics)
                    native.k = types.SimpleNamespace(OpenProcess=lambda *args: 77)
                    native.belongs = lambda *args, failure=failure: failure != "ownership_unproven"

                    def identity(handle, failure=failure):
                        raise ResourceFailure(failure)

                    def close(handle, close_fails=close_fails, events=events):
                        events.append("close")
                        if close_fails:
                            raise ResourceFailure("resource_handle_cleanup_failed")

                    native.identity, native.close_handle = identity, close
                    with (
                        patch.object(guard.c, "get_last_error", create=True) as saved_error,
                        self.assertRaises(ResourceFailure) as refused,
                    ):
                        native.open_member(11, object())
                    saved_error.assert_not_called()
                    self.assertEqual(
                        str(refused.exception),
                        "resource_handle_cleanup_failed" if close_fails else failure,
                    )
                    self.assertEqual(events, ["close"])

    def test_wrapper_does_not_mask_later_errors_or_lose_cleanup_poison(self):
        for failure in (
            "ownership_unproven",
            "process_wait_failed",
            "resource_handle_cleanup_failed",
        ):
            case = AdapterTests()
            owner = case.make()
            case.native.open_member = lambda *args, failure=failure: (_ for _ in ()).throw(
                ResourceFailure(failure)
            )
            with (
                self.subTest(failure=failure),
                patch.object(guard.c, "get_last_error", create=True) as saved_error,
                self.assertRaises(ResourceFailure) as refused,
            ):
                owner.sample()
            saved_error.assert_not_called()
            self.assertEqual(str(refused.exception), failure)
            self.assertEqual(owner.cleanup_complete, failure != "resource_handle_cleanup_failed")
            self.assertEqual(
                owner.unretained_handle_cleanup_failed, failure == "resource_handle_cleanup_failed"
            )

    def test_all_six_open_categories_are_fixed_sanitized_and_main_visible(self):
        categories = {
            "process_open_" + phase + "_" + suffix
            for phase in ("initial", "revalidation")
            for suffix in ("invalid_parameter", "access_denied", "other")
        }
        source = ast.parse((ROOT / "scripts/windows_hosted_qualification.py").read_text())
        literals = {
            n.value
            for n in ast.walk(source)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
        }
        self.assertTrue(categories <= literals)
        for category in categories:
            self.assertRegex(category, r"\A[a-z_]{1,64}\Z")
            output = io.StringIO()
            with (
                patch.object(sys, "argv", ["guard", "--prepare", "--profile", "alpha"]),
                patch.object(guard, "prepare", side_effect=ResourceFailure(category)),
                contextlib.redirect_stderr(output),
            ):
                self.assertEqual(guard.main(), 1)
            self.assertEqual(
                output.getvalue(), "K5_HOSTED_QUALIFICATION_REFUSED=" + category + "\n"
            )


class UnopenedCensusTests(unittest.TestCase):
    scenario = OpenFailureDiagnosticTests.scenario

    @staticmethod
    def sample(owner):
        with patch.object(guard.c, "get_last_error", return_value=87, create=True):
            return owner.sample()

    def test_both_omission_sites_require_an_extra_full_stable_attempt(self):
        for site, reads, pid, total in (
            ("initial_vanished", 4, 11, 2),
            ("closing_vanished", 6, 13, 3),
        ):
            with self.subTest(site=site):
                owner, native, events, _ = self.scenario(site)
                targets = []
                original = native.listeners

                def listeners(pids, original=original, targets=targets, owner=owner):
                    targets.append((owner.job.drained, set(pids)))
                    return original(pids)

                native.listeners = listeners
                snapshot = self.sample(owner)
                self.assertEqual(native.read_count, reads)
                self.assertEqual(snapshot.active_processes, total - 1)
                self.assertEqual(owner.unbound_endpoint_pids, {pid})
                self.assertNotIn(pid, owner.handles)
                for ledger in (
                    owner.witnessed_births,
                    owner.incomplete_exit_births,
                    owner.absent_births,
                ):
                    self.assertFalse(any(identity[0] == pid for identity in ledger))
                self.assertTrue(
                    all(pid in pids for _, pids in targets[int(site.startswith("closing")) :])
                )
                owner.close()
                self.assertEqual(targets[-1], (True, set(range(11, 11 + total))))
                self.assertNotIn(pid, native.closed_handles)
                state = association_state()
                guard.accumulate_associations(state, owner)
                self.assertEqual(state, association_state(total, total - 1))
                owner.close()
                with self.assertRaisesRegex(guard.Refusal, "association_counts_already_recorded"):
                    guard.accumulate_associations(state, owner)
                self.assertEqual(state, association_state(total, total - 1))

    def test_next_complete_census_must_omit_without_reopen_recovery(self):
        for site, reads in (("initial_new", 2), ("closing_new", 3)):
            with self.subTest(site=site):
                owner, native, events, _ = self.scenario(site)
                with self.assertRaisesRegex(ResourceFailure, "unopened_member_still_listed"):
                    self.sample(owner)
                self.assertEqual(native.read_count, reads)
                self.assertEqual(sum(isinstance(x, tuple) for x in events), 1)
                self.assertTrue(owner.cleanup_complete)

    def test_any_reappearance_after_observed_omission_is_fatal(self):
        for site, pid, reappear_reads in (
            ("initial_vanished", 11, (3, 4)),
            ("closing_vanished", 13, (4, 5, 6)),
        ):
            for reappear in reappear_reads:
                for reused in (False, True):
                    with self.subTest(site=site, read=reappear, reused=reused):
                        owner, native, events, _ = self.scenario(site)
                        original = native.members
                        reads = []

                        def members(
                            job,
                            original=original,
                            reads=reads,
                            reappear=reappear,
                            native=native,
                            pid=pid,
                            reused=reused,
                        ):
                            rows = original(job)
                            reads.append(1)
                            if len(reads) == reappear:
                                native.pids.add(pid)
                                native.identities[pid] = (999 if reused else pid + 90, False)
                                rows.add(pid)
                            return rows

                        native.members = members
                        with self.assertRaisesRegex(ResourceFailure, "unopened_member_reappeared"):
                            self.sample(owner)
                        self.assertEqual(len(reads), reappear)
                        self.assertFalse(
                            any(identity[0] == pid for identity in owner.witnessed_births)
                        )
                        self.assertEqual(sum(isinstance(x, tuple) for x in events), 1)

    def delayed(self, *, initial, failure_attempt):
        case = AdapterTests()
        owner = case.make()
        native = case.native
        target = 11 if initial else 13
        if initial:
            native.pids.remove(11)
        original_members, original_open = native.members, native.open_member
        original_accounting = native.accounting
        reads = []

        def members(job):
            reads.append(1)
            if len(reads) == 2 * failure_attempt - int(initial):
                native.pids.add(target)
                native.identities[target] = (target + 90, False)
                native.total = job.total_processes = 2 if initial else 3
            return original_members(job)

        def accounting(job):
            total, active, cpu = original_accounting(job)
            if (native.read_count + 1) // 2 < failure_attempt and native.read_count % 2:
                active -= 1  # Retryable stale-low count, never an impossible over-total count.
            return total, active, cpu

        def opened(pid, job):
            if pid == target:
                native.pids.remove(pid)
                raise ResourceFailure("process_open_initial_invalid_parameter")
            return original_open(pid, job)

        native.members, native.accounting, native.open_member = members, accounting, opened
        return owner, native, reads

    def test_resolution_needs_a_spare_attempt_without_increasing_eight(self):
        for initial, failure_attempt, accepted in (
            (True, 7, True),
            (True, 8, False),
            (False, 6, True),
            (False, 7, False),
            (False, 8, False),
        ):
            with self.subTest(initial=initial, attempt=failure_attempt):
                owner, native, reads = self.delayed(
                    initial=initial, failure_attempt=failure_attempt
                )
                if accepted:
                    self.assertEqual(self.sample(owner).active_processes, 1 if initial else 2)
                    owner.close()
                else:
                    with self.assertRaises(ResourceFailure):
                        self.sample(owner)
                self.assertEqual(len(reads), 16)
                self.assertTrue(owner.cleanup_complete)

    def test_resolution_keeps_the_original_inclusive_one_second_deadline(self):
        for site, final_read in (("initial_vanished", 4), ("closing_vanished", 6)):
            for duration in (1.0, 1.0 + 2**-10):
                with self.subTest(site=site, duration=duration):
                    owner, native, events, _ = self.scenario(site)
                    start, original = owner.clock.value, native.members
                    reads = []

                    def members(
                        job,
                        original=original,
                        reads=reads,
                        final_read=final_read,
                        owner=owner,
                        start=start,
                        duration=duration,
                    ):
                        rows = original(job)
                        reads.append(1)
                        if len(reads) == final_read:
                            owner.clock.value = start + duration
                        return rows

                    native.members = members
                    if duration == 1:
                        self.assertEqual(self.sample(owner).measured_at - start, 1)
                        owner.close()
                    else:
                        with self.assertRaisesRegex(ResourceFailure, "resource_evidence_stale"):
                            self.sample(owner)
                    self.assertEqual(len(reads), final_read)

    def test_partial_rss_and_current_unbound_endpoint_union_survive_retry(self):
        owner, native, events, _ = self.scenario("closing_vanished")
        original_rss, original_listeners = native.rss, native.listeners
        row = (13, 2, b"\x7f\0\0\1", 9000)
        targets = []

        def rss(handle):
            return 400 if native.read_count <= 2 else original_rss(handle)

        def listeners(pids):
            targets.append(set(pids))
            rows = original_listeners(pids)
            if not owner.job.drained and 13 in pids:
                rows.add(row)
            return rows

        native.rss, native.listeners = rss, listeners
        snapshot = self.sample(owner)
        self.assertEqual(snapshot.rss_bytes, 800)
        self.assertEqual(owner.observed_peak_rss_bytes, 800)
        self.assertEqual(owner.observed_listeners, {row})
        self.assertEqual(snapshot.owned_loopback_listeners, 1)
        self.assertTrue(all(13 in pids for pids in targets[1:]))
        owner.close()
        self.assertTrue(owner.cleanup_complete)

    def test_unbound_rows_cannot_hide_other_current_member_or_endpoint_api_failures(self):
        failures = {
            "members": "job_members_query_failed",
            "accounting": "job_accounting_query_failed",
            "rss": "process_rss_query_failed",
            "listeners": "endpoint_table_query_failed",
            "belongs": "ownership_unproven",
            "identity": "pid_reuse_or_exit",
        }
        for method, category in failures.items():
            with self.subTest(method=method):
                owner, native, events, _ = self.scenario("initial_vanished")
                original = getattr(native, method)
                calls = []

                def failed(
                    *args,
                    method=method,
                    original=original,
                    category=category,
                    calls=calls,
                    owner=owner,
                ):
                    calls.append(1)
                    trigger = not owner.job.drained and (
                        method not in {"members", "accounting"} or len(calls) == 2
                    )
                    if trigger:
                        if method == "belongs":
                            return False
                        if method == "identity":
                            return (999, False)
                        raise ResourceFailure(category)
                    return original(*args)

                setattr(native, method, failed)
                with self.assertRaisesRegex(ResourceFailure, category):
                    self.sample(owner)
                self.assertTrue(owner.cleanup_complete)

    def test_rss_endpoint_and_active_caps_still_refuse(self):
        for cap, category in (
            ("rss", "owned_rss_exceeded"),
            ("endpoints", "listener_evidence_limit"),
            ("active", "final_members_exceed_limit"),
        ):
            with self.subTest(cap=cap):
                owner, native, events, _ = self.scenario("initial_vanished")
                if cap == "rss":
                    native.rss_each = guard.RSS_LIMIT + 1
                elif cap == "endpoints":
                    native.listener_rows = {(11, 2, b"\x7f\0\0\1", x) for x in range(1, 4098)}
                else:
                    original = native.members
                    reads = []

                    def members(job, original=original, reads=reads):
                        rows = original(job)
                        reads.append(1)
                        return set(range(100, 133)) if len(reads) == 2 else rows

                    native.members = members
                with self.assertRaisesRegex(ResourceFailure, category):
                    self.sample(owner)
                if cap == "rss":
                    self.assertGreater(owner.observed_peak_rss_bytes, guard.RSS_LIMIT)
                if cap == "endpoints":
                    self.assertEqual(len(owner.observed_listeners), 4097)

    def test_numeric_storage_uses_existing_union_limit(self):
        owner, native, events, _ = self.scenario("initial_vanished")
        with patch.object(guard, "MAX_RETAINED_BIRTHS", 1):
            with self.assertRaisesRegex(ResourceFailure, "birth_evidence_limit"):
                self.sample(owner)
        self.assertEqual(owner.unbound_endpoint_pids, {11})
        self.assertEqual(owner.witnessed_births, set())
        self.assertTrue(owner.cleanup_complete)

    def test_old_numeric_targets_are_cleanup_only_and_not_an_identity_blacklist(self):
        owner, native, events, _ = self.scenario("initial_vanished")
        self.sample(owner)
        owner.clock.value += 0.25
        targets = []
        original_listeners = native.listeners

        def listeners(pids):
            targets.append((owner.job.drained, set(pids)))
            return original_listeners(pids)

        native.listeners = listeners
        self.sample(owner)
        self.assertTrue(all(11 not in pids for _, pids in targets))
        # A later acquisition must bind a current row afresh, never use the marker.
        owner.clock.value += 0.25
        native.pids.add(11)
        native.identities[11] = (333, False)
        native.total = owner.job.total_processes = 3
        native.open_member = lambda pid, job: FakeNative.open_member(native, pid, job)
        self.sample(owner)
        self.assertIn((11, 333), owner.witnessed_births)
        self.assertNotIn((11, 101), owner.witnessed_births)
        self.assertEqual(owner.incomplete_exit_births, set())
        self.assertEqual(owner.absent_births, set())
        owner.close()
        self.assertEqual(targets[-1], (True, {11, 12}))
        state = association_state()
        guard.accumulate_associations(state, owner)
        self.assertEqual(state, association_state(3, 2))

    def test_final_numeric_endpoint_query_failure_or_reuse_never_claims_cleanup(self):
        for failure in ("endpoint", "api"):
            with self.subTest(failure=failure):
                owner, native, events, _ = self.scenario("initial_vanished")
                self.sample(owner)
                original = native.listeners
                targets = []

                def listeners(
                    pids,
                    targets=targets,
                    owner=owner,
                    failure=failure,
                    original=original,
                ):
                    targets.append(set(pids))
                    if owner.job.drained:
                        if failure == "api":
                            raise ResourceFailure("endpoint_table_query_failed")
                        # A later unrelated reuse may conservatively refuse; no control.
                        return {(11, 2, b"\x7f\0\0\1", 9000)}
                    return original(pids)

                native.listeners = listeners
                with self.assertRaisesRegex(ResourceFailure, "owned_listener_cleanup_failed"):
                    owner.close()
                self.assertEqual(targets, [{11, 12}])
                self.assertFalse(owner.cleanup_complete)
                self.assertTrue(owner.job.drained and owner.association_counts_complete)
                self.assertNotIn(11, native.closed_handles)
                with self.assertRaisesRegex(ResourceFailure, "owned_job_cleanup_failed"):
                    owner.close()

    def test_retained_live_omission_keeps_its_separate_signal_requirement(self):
        for signal in (False, True):
            with self.subTest(signal=signal):
                owner, native, events, _ = self.scenario("closing_vanished")
                original = native.members
                reads = []

                def members(job, original=original, reads=reads, native=native, signal=signal):
                    rows = original(job)
                    reads.append(1)
                    if len(reads) >= 3:
                        native.pids.discard(11)
                        rows.discard(11)
                        native.identities[11] = (101, signal)
                    return rows

                native.members = members
                if signal:
                    self.sample(owner)
                    self.assertEqual(owner.absent_births, {(11, 101)})
                    self.assertEqual(owner.unbound_endpoint_pids, {13})
                    owner.close()
                else:
                    with self.assertRaisesRegex(ResourceFailure, "departure_not_signaled"):
                        self.sample(owner)
                    self.assertEqual(len(reads), 16)
                    self.assertEqual(owner.absent_births, set())

    def run_owned_gap(self, state, *, final_total=3, cleanup=False):
        base = FakeNative

        class Native(base):
            def members(self, job):
                if self.read_count == 5:
                    self.pids.add(13)
                    self.identities[13] = (103, False)
                    self.total = 3
                return super().members(job)

            def open_member(self, pid, job):
                if pid == 13:
                    self.pids.remove(13)
                    raise ResourceFailure("process_open_initial_invalid_parameter")
                return super().open_member(pid, job)

            def listeners(self, pids):
                if self.job is not None and self.job.drained and cleanup:
                    return {(13, 2, b"\x7f\0\0\1", 9000)} if 13 in pids else set()
                return super().listeners(pids)

        with patch.dict(globals(), {"FakeNative": Native}):
            return SampledRunOwnedTests().run_job(state, total=final_total)

    def test_multiple_owned_jobs_record_native_gap_once_despite_repeated_close(self):
        state = SampledRunOwnedTests().state()
        for number in range(1, 4):
            error, owners, native = self.run_owned_gap(state)
            self.assertIsNone(error)
            self.assertEqual(
                tuple(state[field] for field in guard.ASSOCIATION_FIELDS),
                (3 * number, 2 * number, number),
            )
            self.assertTrue(owners[0].association_counts_recorded)
            self.assertEqual(owners[0].unbound_endpoint_pids, {13})
            self.assertTrue(state["all_owned_cleanup"] and not state["pending_payload"])

    def test_owned_cleanup_failure_and_missing_final_accounting_stay_truthful(self):
        for missing in (False, True):
            with self.subTest(missing=missing):
                state = SampledRunOwnedTests().state(7, 4)
                error, owners, native = self.run_owned_gap(
                    state, final_total=None if missing else 3, cleanup=not missing
                )
                self.assertIsNotNone(error)
                self.assertFalse(state["all_owned_cleanup"])
                self.assertTrue(owners[0].failed)
                if missing:
                    self.assertFalse(state["association_counts_complete"])
                    self.assertEqual(state["job_total_associations"], 7)
                else:
                    self.assertTrue(state["association_counts_complete"])
                    self.assertEqual(state["job_total_associations"], 10)
                    self.assertEqual(state["fully_live_observed_associations"], 6)
                self.assertGreater(state["peak_rss_bytes"], 0)
                self.assertGreater(state["cpu_seconds"], 0)


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
            **association_state(),
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

    def assert_original_body(self, kind, original, body):
        if kind == "alpha" and original["name"] == RECOVERY_STEP:
            body = normalize_recovery_body(body)
        check = 'if ($LASTEXITCODE -ne 0) { throw "Qualification dependency command failed." }\n'
        if kind == "alpha" and original["name"] in {
            "Install targeted test dependencies",
            "Verify clean Windows alpha runtime dependency install",
        }:
            body = body.replace(check, "")
        if kind == "alpha" and original["name"] == "Run Windows alpha boundary regressions":
            suffix = " -o verbosity_test_cases=1\n"
            self.assertEqual(body.count("verbosity_test_cases"), 1)
            self.assertTrue(body.endswith(suffix))
            body = body[: -len(suffix)] + "\n"
        else:
            self.assertNotIn("verbosity_test_cases", body)
        self.assertEqual(hashlib.sha256(body.encode()).hexdigest(), original["body_sha256"])

    def test_original_executable_bodies_are_preserved(self):
        profile = guard.load_json(ROOT / "scripts/qualification/windows-hosted-profile.json")
        for kind, workflow in guard.PROFILES.items():
            steps = {r["name"]: r for r in guard.workflow_steps((ROOT / workflow).read_text())}
            for original in profile["original_steps"][kind]:
                row = steps[original["name"]]
                self.assert_original_body(kind, original, row["body"])
                self.assertEqual(row["timeout"], original["timeout"])
                self.assertEqual(row["always"], original["always"])

    def test_boundary_logging_allowance_rejects_other_body_changes(self):
        profile = guard.load_json(ROOT / "scripts/qualification/windows-hosted-profile.json")
        original = next(
            row
            for row in profile["original_steps"]["alpha"]
            if row["name"] == "Run Windows alpha boundary regressions"
        )
        body = next(
            row["body"]
            for row in guard.workflow_steps((ROOT / guard.PROFILES["alpha"]).read_text())
            if row["name"] == original["name"]
        )
        for changed in (
            body.replace("tests/test_cli.py ", ""),
            body.replace("pytest ", "pytest --maxfail=1 ", 1),
            body.replace(" -q ", " -s ", 1),
            "Write-Output 'extra command'\n" + body,
            body.replace(" -o verbosity_test_cases=1", " -o verbosity_test_cases=1" * 2),
            body.replace("verbosity_test_cases=1", "verbosity_test_cases=2"),
            body.replace(" -o verbosity_test_cases=1", ""),
            body.replace(" -o verbosity_test_cases=1", "").replace(
                "pytest ", "pytest -o verbosity_test_cases=1 ", 1
            ),
        ):
            with self.subTest(body=changed), self.assertRaises(AssertionError):
                self.assert_original_body("alpha", original, changed)
        for kind, name in (("operator", original["name"]), ("alpha", "Another step")):
            with self.subTest(kind=kind, name=name), self.assertRaises(AssertionError):
                self.assert_original_body(kind, {**original, "name": name}, body)

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
                self.terminal_evidence_failure = None
                self.final_total = 2
                self.witnessed_births = {(11, 101), (12, 102)}
                self.association_counts_complete = True
                self.association_counts_recorded = False

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
                **association_state(),
                pending_payload=False,
                cpu_seconds=2,
                peak_rss_bytes=0,
                started_at=time.time(),
                all_owned_cleanup=True,
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
                            self.terminal_evidence_failure = None
                            self.final_cpu_seconds = 1
                            self.final_active = 0
                            self.cleanup_complete = True
                            self.final_total = 2
                            self.witnessed_births = {(11, 101), (12, 102)}
                            self.association_counts_complete = True
                            self.association_counts_recorded = False

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
                            **association_state(),
                            pending_payload=False,
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
                **association_state(),
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


RECOVERY_ORIGINAL_INLINE = """import os
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import uuid4
from k5vision.identity_state import validate_new_identity_directory

root = Path(os.environ["LOCALAPPDATA"]) / ("K5 Recovery pytest-" + uuid4().hex)
validate_new_identity_directory(root)
root.mkdir(mode=0o700)
try:
    base = root / "pytest"
    validate_new_identity_directory(base)
    result = subprocess.run([
        sys.executable, "-m", "pytest",
        "tests/test_recovery_admin.py",
        "tests/test_recovery_admin_windows.py",
        "tests/test_windows_identity_security.py",
        "-q", "-rP", "--no-cov", "-o", "junit_family=xunit1",
        "--junitxml=recovery-windows-results.xml", "--basetemp", str(base),
    ], check=False, timeout=600)
    raise SystemExit(result.returncode)
finally:
    shutil.rmtree(root)
"""

RECOVERY_RETURN_MARKER = """try:
    if type(result) is subprocess.CompletedProcess:
        _k5_returncode = dict.get(vars(result), "returncode")
        if type(_k5_returncode) is int:
            if _k5_returncode == 0:
                print("K5_RECOVERY_DIAGNOSTIC=pytest_return:zero", flush=True)
            else:
                print("K5_RECOVERY_DIAGNOSTIC=pytest_return:nonzero", flush=True)
except BaseException:
    pass
"""

RECOVERY_STEP = "Qualify Windows local recovery identity"
RECOVERY_INLINE_SHA256 = "734467b71c051796729256607e64ea4d7990ab6550c26799ce5ff7b32ade3fd3"
RECOVERY_HELPER_AST_SHA256 = "ac13e7457f25a5dcfe282b38708898248a0f8870c2a2ab8341d27fbc5b12f37c"
RECOVERY_PHASES = {
    "identity_import",
    "root_admission",
    "root_create",
    "base_admission",
    "pytest_launch",
    "cleanup",
}
RECOVERY_CATEGORIES = {
    "unexpected",
    "identity_other",
    "identity_volume",
    "identity_security",
    "identity_path",
    "identity_parent",
    "volume_path",
    "volume_type",
    "volume_query",
    "volume_alias",
    "security_owner",
    "security_access",
    "security_inheritance",
    "security_query",
    "security_unsupported",
    "timeout",
    "import_error",
    "permission_error",
    "missing_file",
    "existing_file",
    "os_error",
}


def recovery_body():
    return next(
        row["body"]
        for row in guard.workflow_steps((ROOT / guard.PROFILES["alpha"]).read_text())
        if row["name"] == RECOVERY_STEP
    )


def split_recovery_inline(body):
    assert body.count("$qualification = @'\n") == 1
    before, remainder = body.split("$qualification = @'\n")
    inline, after = remainder.split("\n'@", 1)
    return before + "$qualification = @'\n", inline + "\n", "'@" + after


def normalize_recovery_body(body):
    """Allow exactly the reviewed diagnostics, never a changed original operation."""
    before, inline, after = split_recovery_inline(body)
    assert hashlib.sha256(inline.encode()).hexdigest() == RECOVERY_INLINE_SHA256
    tree = ast.parse(inline)
    helpers = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
    assert len(helpers) == 1
    helper = helpers[0]
    assert helper.name == "_k5_recovery_diagnostic"
    assert hashlib.sha256(ast.dump(helper).encode()).hexdigest() == RECOVERY_HELPER_AST_SHA256
    tree.body.remove(helper)
    entered = ast.parse(
        'try:\n    print("K5_RECOVERY_DIAGNOSTIC=inline:entered", flush=True)\n'
        "except BaseException:\n    pass\n"
    ).body[0]
    entries = [n for n in tree.body if ast.dump(n) == ast.dump(entered)]
    assert len(entries) == 1
    tree.body.remove(entries[0])
    phases = []
    return_markers = []
    return_marker = ast.dump(ast.parse(RECOVERY_RETURN_MARKER).body[0])

    class RemoveDiagnostics(ast.NodeTransformer):
        def visit_Try(self, node):
            if ast.dump(node) == return_marker:
                return_markers.append(node)
                return None
            if len(node.handlers) == 1 and node.handlers[0].name == "_k5_error":
                handler = node.handlers[0]
                assert len(node.body) == 1 and not node.orelse and not node.finalbody
                assert ast.dump(handler.type) == ast.dump(ast.Name(id="Exception", ctx=ast.Load()))
                assert len(handler.body) == 2
                call = handler.body[0].value
                assert call.func.id == "_k5_recovery_diagnostic" and not call.keywords
                assert len(call.args) == 2 and call.args[1].id == "_k5_error"
                phase = call.args[0].value
                assert phase in RECOVERY_PHASES
                phases.append(phase)
                assert isinstance(handler.body[1], ast.Raise)
                assert handler.body[1].exc is None and handler.body[1].cause is None
                return node.body[0]
            return self.generic_visit(node)

    tree = RemoveDiagnostics().visit(tree)
    assert len(return_markers) == 1
    assert len(phases) == 6 and set(phases) == RECOVERY_PHASES
    assert ast.dump(tree) == ast.dump(ast.parse(RECOVERY_ORIGINAL_INLINE))
    return before + RECOVERY_ORIGINAL_INLINE + after


class RecoveryIdentityError(RuntimeError):
    pass


class RecoverySecurityError(RuntimeError):
    pass


class RecoveryTimeoutError(Exception):
    pass


class RecoveryInlineHarness:
    """Only inert Python callbacks; no product import, filesystem write or child."""

    def __init__(self, failures=None, returncode=0, print_error=None, completed=False):
        self.events, self.output = [], []
        self.failures = failures or {}
        self.print_error = print_error
        owner = self

        def op(name, *args):
            owner.events.append((name, args))
            if name in owner.failures:
                raise owner.failures[name]

        class InertPath:
            def __init__(self, value):
                self.value = value
                op("path", value)

            def __truediv__(self, value):
                op("join", value)
                result = object.__new__(InertPath)
                result.value = self.value + "/" + value
                return result

            def mkdir(self, **kwargs):
                op("mkdir", self.value, kwargs)

            def __str__(self):
                op("path_string", self.value)
                return self.value

        def validate(path):
            op("base" if path.value.endswith("/pytest") else "root", path.value)

        def run(argv, **kwargs):
            op("run", argv, kwargs)
            if completed:
                return subprocess.CompletedProcess(argv, returncode)

            class Result:
                @property
                def returncode(self):
                    op("returncode")
                    return returncode

            return Result()

        def cleanup(path):
            op("cleanup", path.value)

        def uuid4():
            op("uuid")
            return types.SimpleNamespace(hex="inert")

        class Environment(dict):
            def __getitem__(self, key):
                op("environment", key)
                return super().__getitem__(key)

        identity = types.SimpleNamespace(
            IdentityStateError=RecoveryIdentityError, validate_new_identity_directory=validate
        )
        security = types.SimpleNamespace(WindowsIdentitySecurityError=RecoverySecurityError)
        self.modules = {
            "os": types.SimpleNamespace(environ=Environment(LOCALAPPDATA="PRIVATE_LOCATION")),
            "shutil": types.SimpleNamespace(rmtree=cleanup),
            "subprocess": types.SimpleNamespace(
                run=run,
                TimeoutExpired=RecoveryTimeoutError,
                CompletedProcess=subprocess.CompletedProcess,
            ),
            "sys": types.SimpleNamespace(
                executable="inert-python",
                modules={
                    "k5vision.identity_state": identity,
                    "k5vision.windows_identity_security": security,
                },
            ),
            "pathlib": types.SimpleNamespace(Path=InertPath),
            "uuid": types.SimpleNamespace(uuid4=uuid4),
            "k5vision.identity_state": identity,
        }

        def importing(name, globals=None, locals=None, fromlist=(), level=0):
            op("import:" + name, tuple(fromlist or ()), level)
            return self.modules[name]

        def printing(value, *, flush):
            if self.print_error is not None:
                raise self.print_error
            assert type(value) is str and flush is True
            self.output.append(value)

        import builtins

        self.namespace = {
            "__builtins__": {**vars(builtins), "__import__": importing, "print": printing}
        }

    def execute(self, inline):
        try:
            exec(compile(inline, "<inert-recovery-inline>", "exec"), self.namespace)
        except BaseException as error:
            self.error = error
        else:
            self.error = None
        self.context = self.error.__context__ if self.error is not None else None
        self.cause = self.error.__cause__ if self.error is not None else None
        return self


class RecoveryInlineDiagnosticTests(unittest.TestCase):
    def inline(self):
        return split_recovery_inline(recovery_body())[1]

    def assert_closed(self, output):
        for row in output:
            self.assertLess(len(row), 100)
            self.assertNotIn("PRIVATE", row)
            self.assertTrue(row.startswith("K5_RECOVERY_DIAGNOSTIC="))
            phase, category = row.split("=", 1)[1].split(":")
            if phase == "inline":
                self.assertEqual(category, "entered")
            elif phase == "pytest_return":
                self.assertIn(category, {"zero", "nonzero"})
            else:
                self.assertIn(phase, RECOVERY_PHASES)
                self.assertIn(category, RECOVERY_CATEGORIES)

    def paired(self, failures=None, code=0, print_error=None, completed=False):
        failures = failures or {}
        old = RecoveryInlineHarness(failures, code, completed=completed).execute(
            RECOVERY_ORIGINAL_INLINE
        )
        for error in failures.values():
            error.__traceback__ = None
            error.__context__ = None
        new = RecoveryInlineHarness(failures, code, print_error, completed=completed).execute(
            self.inline()
        )
        self.assertEqual(old.events, new.events)
        self.assertIs(type(old.error), type(new.error))
        if any(old.error is item for item in failures.values()):
            self.assertIs(old.error, new.error)
        else:
            self.assertIs(old.error.code, new.error.code)
        if isinstance(old.context, SystemExit):
            self.assertIs(type(old.context), type(new.context))
            self.assertEqual(old.context.code, new.context.code)
        else:
            self.assertIs(old.context, new.context)
        self.assertIs(old.cause, new.cause)
        self.assert_closed(new.output)
        return new

    def test_exact_normalization_restores_original_hash(self):
        normalized = normalize_recovery_body(recovery_body())
        profile = guard.load_json(ROOT / "scripts/qualification/windows-hosted-profile.json")
        expected = next(
            row for row in profile["original_steps"]["alpha"] if row["name"] == RECOVERY_STEP
        )
        self.assertEqual(hashlib.sha256(normalized.encode()).hexdigest(), expected["body_sha256"])

    def test_normalization_rejects_changed_operations_and_duplicate_instrumentation(self):
        body = recovery_body()
        mutations = [
            body.replace("timeout=600", "timeout=601"),
            body.replace("check=False", "check=True"),
            body.replace("mode=0o700", "mode=0o777"),
            body.replace('"-q", "-rP"', '"-v", "-rP"'),
            body.replace("raise SystemExit(result.returncode)", "raise SystemExit(0)"),
            body.replace("shutil.rmtree(root)", "pass"),
            body.replace("except Exception as _k5_error:", "except BaseException as _k5_error:", 1),
            body.replace('"root_admission", _k5_error', '"base_admission", _k5_error', 1),
            body.replace("    raise\n", "    raise _k5_error\n", 1),
            body.replace(
                'print("K5_RECOVERY_DIAGNOSTIC=inline:entered", flush=True)',
                'print("K5_RECOVERY_DIAGNOSTIC=inline:entered", flush=True)\n'
                '    print("duplicate")',
            ),
            body.replace('category = "unexpected"', "category = str(error)"),
        ]
        for changed in mutations:
            with self.subTest(mutation=mutations.index(changed)):
                self.assertNotEqual(body, changed)
                with self.assertRaises(AssertionError):
                    normalize_recovery_body(changed)

    def test_success_and_nonzero_exit_keep_original_calls_and_cleanup(self):
        for code in (0, 1, 2, 5):
            result = self.paired(code=code)
            self.assertEqual(result.output, ["K5_RECOVERY_DIAGNOSTIC=inline:entered"])
            self.assertEqual(sum(name == "cleanup" for name, _ in result.events), 1)

    def test_each_failure_phase_preserves_exception_and_original_operations(self):
        for operation, phase in (
            ("import:k5vision.identity_state", "identity_import"),
            ("root", "root_admission"),
            ("mkdir", "root_create"),
            ("base", "base_admission"),
            ("run", "pytest_launch"),
            ("cleanup", "cleanup"),
        ):
            error = RuntimeError("PRIVATE_ERROR")
            result = self.paired({operation: error})
            self.assertEqual(result.output[-1], "K5_RECOVERY_DIAGNOSTIC=" + phase + ":unexpected")
            frames = []
            tb = error.__traceback__
            while tb:
                frames.append(tb.tb_frame.f_code.co_name)
                tb = tb.tb_next
            self.assertEqual(frames.count("op"), 1)

    def test_cleanup_wins_body_failure_with_original_context(self):
        for operation in ("base", "run", "returncode"):
            primary, cleanup = RuntimeError("PRIVATE_BODY"), OSError("PRIVATE_CLEANUP")
            result = self.paired({operation: primary, "cleanup": cleanup})
            self.assertIs(result.error, cleanup)
            self.assertIs(result.context, primary)
            self.assertEqual(result.output[-1], "K5_RECOVERY_DIAGNOSTIC=cleanup:os_error")

    def test_base_exceptions_and_uncovered_operations_preserve_behavior(self):
        for operation in ("root", "mkdir", "base", "run", "cleanup"):
            for error in (KeyboardInterrupt("PRIVATE"), SystemExit(17)):
                result = self.paired({operation: error})
                self.assertEqual(result.output, ["K5_RECOVERY_DIAGNOSTIC=inline:entered"])
        for operation in (
            "import:os",
            "import:uuid",
            "environment",
            "path",
            "uuid",
            "join",
            "returncode",
        ):
            result = self.paired({operation: RuntimeError("PRIVATE")})
            self.assertLessEqual(len(result.output), 1)

    def test_print_failures_never_mask_original_exception_or_exit(self):
        for printing_error in (OSError("PRIVATE_PRINT"), KeyboardInterrupt(), SystemExit(99)):
            for operation in (None, "root", "base", "run", "cleanup"):
                failures = {} if operation is None else {operation: RuntimeError("PRIVATE")}
                self.paired(failures, print_error=printing_error)

    def test_builtin_exception_classes_are_closed_and_exact(self):
        for error, category in (
            (RecoveryTimeoutError("PRIVATE"), "timeout"),
            (ImportError("PRIVATE"), "import_error"),
            (ModuleNotFoundError("PRIVATE"), "import_error"),
            (PermissionError("PRIVATE"), "permission_error"),
            (FileNotFoundError("PRIVATE"), "missing_file"),
            (FileExistsError("PRIVATE"), "existing_file"),
            (OSError("PRIVATE"), "os_error"),
            (ValueError("PRIVATE"), "unexpected"),
        ):
            result = self.paired({"run": error})
            self.assertEqual(result.output[-1], "K5_RECOVERY_DIAGNOSTIC=pytest_launch:" + category)

        class PermissionSubclass(PermissionError):
            pass

        self.assertTrue(
            self.paired({"run": PermissionSubclass("PRIVATE")}).output[-1].endswith(":unexpected")
        )

    def test_all_literal_identity_maps_are_source_owned_and_executable(self):
        helper = ast.parse(self.inline()).body[6]
        self.assertIsInstance(helper, ast.FunctionDef)
        maps = [node for node in ast.walk(helper) if isinstance(node, ast.Dict)]
        self.assertEqual(len(maps), 3)
        source_strings = {}
        for name in ("identity_state", "windows_identity_security"):
            source_strings[name] = {
                node.value
                for node in ast.walk(
                    ast.parse((ROOT / "src/k5vision" / (name + ".py")).read_text())
                )
                if isinstance(node, ast.Constant) and type(node.value) is str
            }
        for index, mapping in enumerate(maps):
            for key, value in zip(mapping.keys, mapping.values, strict=True):
                self.assertIn(
                    key.value,
                    source_strings["identity_state" if index == 0 else "windows_identity_security"],
                )
                self.assertIn(value.value, RECOVERY_CATEGORIES)
                if index == 0:
                    error = RecoveryIdentityError(key.value)
                else:
                    outer = (
                        "Windows identity requires a supported fixed local volume"
                        if index == 1
                        else "Windows identity ownership or access is unsafe or unsupported"
                    )
                    error = RecoveryIdentityError(outer)
                    error.__cause__ = RecoverySecurityError(key.value)
                result = self.paired({"root": error})
                self.assertEqual(
                    result.output[-1], "K5_RECOVERY_DIAGNOSTIC=root_admission:" + value.value
                )

    def test_private_unknown_malformed_and_hostile_values_never_escape(self):
        class Hostile:
            def __str__(self):
                raise AssertionError("untrusted str")

            def __repr__(self):
                raise AssertionError("untrusted repr")

        for args in (
            ("PRIVATE" * 10000,),
            ("PRIVATE", "extra"),
            (Hostile(),),
            (),
            ("unknown PRIVATE",),
        ):
            result = self.paired({"root": RecoveryIdentityError(*args)})
            self.assertTrue(result.output[-1].endswith(":identity_other"))

        class HostileIdentity(RecoveryIdentityError):
            def __str__(self):
                raise AssertionError("untrusted str")

            def __repr__(self):
                raise AssertionError("untrusted repr")

            @property
            def args(self):
                raise AssertionError("untrusted args")

            @property
            def __cause__(self):
                raise AssertionError("untrusted cause")

        error = HostileIdentity("Windows identity requires a supported fixed local volume")
        harness = RecoveryInlineHarness({"root": error})
        harness.modules["k5vision.identity_state"].IdentityStateError = HostileIdentity
        # Do not use harness.execute's reporting reads on the hostile exception.
        with self.assertRaises(HostileIdentity) as raised:
            exec(compile(self.inline(), "<inert-inline>", "exec"), harness.namespace)
        self.assertIs(raised.exception, error)
        self.assertEqual(
            harness.output[-1], "K5_RECOVERY_DIAGNOSTIC=root_admission:identity_volume"
        )
        self.assert_closed(harness.output)

    def test_only_explicit_exact_cause_is_classified(self):
        outer = "Windows identity ownership or access is unsafe or unsupported"
        for cause in (
            OSError("Windows identity path has unsupported ownership"),
            RecoverySecurityError("PRIVATE" * 10000),
        ):
            error = RecoveryIdentityError(outer)
            error.__cause__ = cause
            self.assertTrue(self.paired({"root": error}).output[-1].endswith(":identity_security"))
        error = RecoveryIdentityError(outer)
        error.__context__ = RecoverySecurityError("Windows identity path has unsupported ownership")
        result = RecoveryInlineHarness({"root": error}).execute(self.inline())
        self.assertTrue(result.output[-1].endswith(":identity_security"))

    def test_real_completed_process_emits_only_fixed_zero_or_nonzero(self):
        for code in (0, 1, 5, -9):
            result = self.paired(code=code, completed=True)
            category = "zero" if code == 0 else "nonzero"
            self.assertEqual(result.output[-1], "K5_RECOVERY_DIAGNOSTIC=pytest_return:" + category)
        result = self.paired({"run": OSError("PRIVATE")}, completed=True)
        self.assertFalse(any("pytest_return" in row for row in result.output))
        result = self.paired({"cleanup": OSError("PRIVATE")}, completed=True)
        self.assertEqual(
            result.output[-2:],
            [
                "K5_RECOVERY_DIAGNOSTIC=pytest_return:zero",
                "K5_RECOVERY_DIAGNOSTIC=cleanup:os_error",
            ],
        )
        self.paired(completed=True, print_error=OSError("PRIVATE"))

    def test_return_marker_skips_properties_and_non_exact_integer_codes(self):
        result = self.paired(code=5)
        self.assertEqual(sum(name == "returncode" for name, _ in result.events), 1)
        self.assertFalse(any("pytest_return" in row for row in result.output))

        class HostileCode:
            def __eq__(self, other):
                raise AssertionError("untrusted equality")

            def __str__(self):
                raise AssertionError("untrusted string")

        for code in (True, None, "PRIVATE", HostileCode()):
            result = self.paired(code=code, completed=True)
            self.assertIs(result.error.code, code)
            self.assertFalse(any("pytest_return" in row for row in result.output))

    def test_classifier_failure_and_unknown_phase_cannot_change_control_flow(self):
        class BrokenModules:
            def get(self, key):
                raise RuntimeError("PRIVATE")

        error = RuntimeError("PRIVATE")
        harness = RecoveryInlineHarness({"root": error})
        harness.modules["sys"].modules = BrokenModules()
        harness.execute(self.inline())
        self.assertIs(harness.error, error)
        self.assertEqual(harness.output, ["K5_RECOVERY_DIAGNOSTIC=inline:entered"])
        before = list(harness.output)
        harness.namespace["_k5_recovery_diagnostic"]("PRIVATE_PHASE", error)
        self.assertEqual(before, harness.output)


if __name__ == "__main__":
    unittest.main()
