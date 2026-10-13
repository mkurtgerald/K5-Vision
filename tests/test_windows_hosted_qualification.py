"""Portable fake API tests only. Never invokes NativeMetrics or a real Job."""

import ast
import contextlib
import ctypes
import hashlib
import importlib.util
import io
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
        with self.assertRaisesRegex(ResourceFailure, "final_survivor_missing"):
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

    def test_unexplained_departure_never_retries(self):
        owner = self.make()

        def changed(number, rows):
            self.native.pids.remove(11)
            return rows

        calls = self.during_listeners(changed)
        with self.assertRaisesRegex(ResourceFailure, "final_survivor_missing"):
            owner.sample()
        self.assertEqual(len(calls), 1)

    def test_unmatched_birth_or_unknown_member_never_qualifies(self):
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
                with self.assertRaisesRegex(ResourceFailure, "final_birth_evidence_gap"):
                    owner.sample()
                self.assertEqual(len(calls), 1 if total_increase else 3)

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

    def test_three_changing_attempts_exhaust_without_returning_mixed_snapshot(self):
        owner = self.make()
        self.native.pids.add(13)
        self.native.identities[13] = (103, False)
        self.native.total = self.job.total_processes = 3
        owner.sample()
        self.clock.value += 0.25
        self.native.read_count = 0

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
        with self.assertRaisesRegex(ResourceFailure, "resource_evidence_unavailable"):
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

    def test_third_arrival_pass_cannot_succeed_without_stable_final_pass(self):
        owner = self.make_witnessed()

        def changed(number, rows):
            if number % 2:
                pid = 13 + number // 2
                self.birth(pid, pid + 90)
            return rows

        calls = self.during_listeners(changed)
        with self.assertRaisesRegex(ResourceFailure, "resource_evidence_unstable"):
            owner.sample()
        self.assertEqual((len(calls), self.native.read_count), (6, 6))
        self.assertEqual(len(owner.witnessed_births), 5)
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

    def test_unmatched_closing_count_cannot_be_charged_to_cpu(self):
        owner = self.make_witnessed()

        def changed(number, rows):
            self.native.total = self.job.total_processes = 3
            self.native.cpu = 2.5
            return rows

        calls = self.during_listeners(changed)
        with self.assertRaisesRegex(ResourceFailure, "final_birth_evidence_gap"):
            owner.sample()
        self.assertEqual(len(calls), 1)
        self.assertEqual(owner.last_cpu, 2.5)
        self.assertTrue(owner.cleanup_complete)
        self.assertFalse(owner.birth_evidence_complete)

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

    def test_stale_low_initial_count_with_equal_active_turnover_requires_fresh_pass(self):
        owner, calls = self.stale_low_arrival(turnover=True)
        snapshot = owner.sample()
        self.assertEqual(calls, [1, 2, 3, 4])
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
        self.assertEqual(calls, [1, 2, 3, 4])
        self.assertEqual(observed, [13, 13])
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
                # This later fully live child cannot pay the historical deficit.
                self.during_listeners(
                    lambda number, rows: (self.birth(14, 104) if number == 1 else None) or rows
                )
                with self.assertRaisesRegex(ResourceFailure, "final_birth_evidence_gap"):
                    owner.sample()
                self.assertEqual(calls, [1, 2])
                self.assertEqual(owner.last_total, 5)
                self.assertEqual(len(owner.witnessed_births), 3)
                self.assertTrue(owner.cleanup_complete)
                self.assertFalse(owner.birth_evidence_complete)
                self.assertIsNone(owner.terminal_evidence_failure)

    def test_stale_low_census_cannot_enlarge_initial_gap_allowance(self):
        owner, calls = self.stale_low_arrival()
        self.native.total = self.job.total_processes = 4
        with self.assertRaisesRegex(ResourceFailure, "final_birth_evidence_gap"):
            owner.sample()
        self.assertEqual(calls, [1, 2])
        self.assertEqual(owner.last_total, 5)
        self.assertIn((13, 103), owner.witnessed_births)
        self.assertFalse(owner.birth_evidence_complete)

    def test_consistently_low_total_cannot_return_more_ledger_entries_than_accounted(self):
        owner, calls = self.stale_low_arrival()
        original = self.native.accounting

        def accounting(job):
            _, active, cpu = original(job)
            return 2, active, cpu

        self.native.accounting = accounting
        with self.assertRaisesRegex(ResourceFailure, "final_birth_evidence_gap"):
            owner.sample()
        self.assertEqual(calls, [1, 2, 3, 4])
        self.assertTrue(owner.failed and owner.cleanup_complete)

    def test_new_initial_arrival_every_attempt_exhausts_original_three_attempts(self):
        owner = self.make_witnessed()
        calls = self.during_members(
            lambda number: self.birth(13 + number // 2, 103 + number // 2) if number % 2 else None
        )
        with self.assertRaisesRegex(ResourceFailure, "initial_active_count_mismatch"):
            owner.sample()
        self.assertEqual(calls, [1, 2, 3, 4, 5, 6])
        self.assertEqual(len(owner.witnessed_births), 5)
        self.assertTrue(owner.cleanup_complete and owner.birth_evidence_complete)

    def test_equal_active_turnover_every_attempt_exhausts_original_three_attempts(self):
        owner = self.make_witnessed()

        def changed(number):
            if number % 2:
                self.expire(min(self.native.pids))
                self.birth(13 + number // 2, 103 + number // 2)

        calls = self.during_members(changed)
        with self.assertRaisesRegex(ResourceFailure, "resource_evidence_unstable"):
            owner.sample()
        self.assertEqual(calls, [1, 2, 3, 4, 5, 6])
        self.assertEqual(len(owner.witnessed_births), 5)

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
                    calls, [1, 2, 3, 4, 5, 6] if failure in ("exited", "incomplete") else [1]
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
        with self.assertRaisesRegex(ResourceFailure, "resource_evidence_unavailable"):
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

    def test_allowance_is_not_enlarged_by_a_later_attempt(self):
        owner = self.make_witnessed()
        self.native.total = self.job.total_processes = 3
        original = self.native.accounting

        def accounting(job):
            if self.native.read_count == 2:
                self.native.total = self.job.total_processes = 4
            return original(job)

        self.native.accounting = accounting
        self.during_listeners(lambda number, rows: (self.expire() if number == 1 else None) or rows)
        with self.assertRaisesRegex(ResourceFailure, "initial_birth_evidence_gap"):
            owner.sample()
        self.assertEqual(self.native.read_count, 3)
        self.assertTrue(owner.failed and owner.cleanup_complete)
        self.assertEqual((owner.final_total, len(owner.witnessed_births)), (4, 2))

    def test_initial_allowance_does_not_hide_unknown_birth_during_first_audit(self):
        owner = self.make_witnessed()
        self.native.total = self.job.total_processes = 3

        def changed(number, rows):
            self.native.total = self.job.total_processes = 4
            return rows

        calls = self.during_listeners(changed)
        with self.assertRaisesRegex(ResourceFailure, "final_birth_evidence_gap"):
            owner.sample()
        self.assertEqual(len(calls), 1)
        self.assertEqual(owner.final_total, 4)

    def test_failed_in_sample_verdict_cannot_be_rebased_by_next_sample(self):
        owner = self.make_witnessed()

        def changed(number, rows):
            self.native.total = self.job.total_processes = 3
            return rows

        self.during_listeners(changed)
        with self.assertRaisesRegex(ResourceFailure, "final_birth_evidence_gap"):
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
                self.assertIn("between-sample", receipt["resource_evidence"])
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

    def run_job(self, state, *, total=3, failure=None):
        clock, native = Clock(), FakeNative()
        instances = []
        implementation = guard.QualificationResources
        original_host = native.host

        def host(disk):
            available, size, times, free = original_host(disk)
            return available, size, (times[0] * 2, times[1], times[2]), free

        native.host = host
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

    def test_permanently_still_listed_exit_exhausts_three_attempts_without_acceptance(self):
        owner, events = self.timeline("rss", still_listed=True, disappear=False)
        with self.assertRaisesRegex(ResourceFailure, "departed_member_still_listed"):
            owner.sample()
        self.assertEqual(events["members"], 6)
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

    def test_incomplete_exit_cannot_explain_a_simultaneous_unknown_birth(self):
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
                with self.assertRaisesRegex(ResourceFailure, "final_birth_evidence_gap"):
                    owner.sample()
                self.assertEqual(owner.incomplete_exit_births, {(13, 103)})
                self.assertEqual(owner.final_total, 4 + initial_gap)
                self.assertTrue(owner.cleanup_complete)

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
        self.assertEqual((len(calls), self.native.read_count), (6, 6))
        self.assertEqual(len(owner.incomplete_exit_births), 3)
        self.assertTrue(owner.cleanup_complete and owner.association_counts_complete)

    def test_late_signaled_survivor_each_attempt_exhausts_without_stable_snapshot(self):
        owner = self.make()
        self.birth()
        owner.sample()
        self.clock.value += 0.25
        self.native.read_count = 0
        original = self.native.identity

        def identity(handle):
            read = self.native.read_count
            if read in (2, 4, 6) and handle == 10 + read // 2 and handle in self.native.pids:
                self.expire(handle)
            return original(handle)

        self.native.identity = identity
        with self.assertRaisesRegex(ResourceFailure, "final_survivor_signaled"):
            owner.sample()
        self.assertEqual(self.native.read_count, 6)

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
                if category == "initial_birth_evidence_gap":
                    self.native.total = self.job.total_processes = 3
                elif category == "retry_member_set_expanded":
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
                "initial_birth_evidence_gap",
                "retry_member_set_expanded",
                "departure_handle_missing",
                "departure_not_signaled",
            }:
                self.expire()
            elif category == "final_birth_evidence_gap":
                self.native.total = self.job.total_processes = 3
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
        return owner

    def test_each_fixed_membership_predicate_category_is_distinct_and_source_free(self):
        categories = (
            "initial_active_count_mismatch",
            "initial_members_exceed_limit",
            "initial_birth_evidence_gap",
            "retry_member_set_expanded",
            "departure_handle_missing",
            "departure_not_signaled",
            "final_birth_evidence_gap",
            "departed_member_still_listed",
            "final_survivor_missing",
            "final_active_count_mismatch",
        )
        self.assertEqual(len(set(categories)), 10)
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


if __name__ == "__main__":
    unittest.main()
