"""Windows runtime checks. Portable skips are NOT native facade qualification."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "_native_facade", ROOT / "scripts/installed_alpha_facade_witness.py"
)
assert SPEC is not None and SPEC.loader is not None
witness = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(witness)
common = witness.common


@pytest.mark.skipif(os.name != "nt", reason="Requires Windows owned Job image observation")
def test_windows_facade_process_observer_real_owned_births(tmp_path, monkeypatch):
    """Low-level hosted test only. This does not execute or qualify Test/Run."""
    env = common.clean_environment(dict(os.environ))
    base = common.admitted_gate_python(env)
    resources, jobs = [], []
    hash_entered, release_hash = threading.Event(), threading.Event()
    hashes = []
    raw_hash = witness.boundary.file_hash

    def controlled_hash(path):
        hashes.append(path)
        if len(hashes) == 1:
            hash_entered.set()
            assert release_hash.wait(15), "Owned-Python hash barrier did not release"
        return raw_hash(path)

    monkeypatch.setattr(witness.boundary, "file_hash", controlled_hash)

    class ObservedJob(common.WindowsJob):
        def __init__(self):
            super().__init__()
            self.observer = None
            jobs.append(self)
            try:
                self.observer = witness.FacadeProcessObserver(
                    self, {"base_python": (base, common.file_hash(base))}, resources
                )
            except BaseException:
                super().close()
                raise

        def abort(self):
            self.api.TerminateJobObject(self.handle, 1)

        def close(self):
            try:
                total = self.accounting().total_processes
                if self.observer is not None:
                    self.observer.finish(total)
            finally:
                super().close()

    owned = None
    try:
        owned = common.OwnedProcess(
            [str(base), "-I", "-B", "-S", "-c", "import time; time.sleep(1)"],
            cwd=tmp_path,
            env=env,
            operation="probe_admission",
            stdout=subprocess.DEVNULL,
            job_factory=ObservedJob,
        )
        observed = owned.job.observer
        assert hash_entered.wait(15)
        with observed.condition:
            assert observed.condition.wait_for(lambda: len(observed.reservations) >= 2, 15)
            # The owned relay and its admitted Python child are already captured
            # on their one limited handle, while the first raw hash remains held.
            assert not observed.births and len(hashes) == 1
            assert len(observed.handles) >= 2
        release_hash.set()
        owned.wait(15)
        accounting = owned.job.accounting()
        assert accounting.active_processes == 0
        observed = owned.job.observer
        owned.close()
        owned = None
        assert observed.error == "none"
        assert len(observed.births) == accounting.total_processes
        assert set(observed.births.values()) == {"base_python"}
        assert observed.release_complete and not observed.handles
        assert not observed.thread.is_alive() and not observed.admission_thread.is_alive()
        assert observed.capture_done.is_set() and observed.admission_done.is_set()
        assert not observed.pending and observed.validating is None
        assert observed.reconciled(accounting.total_processes)
        assert len(hashes) == len(observed.reservations) == len(observed.births)
        # Actual hosted owned-Python births qualify diagnostic initialization and
        # collection only, never an installed facade success profile.
        assert observed.admission_ordinal == len(observed.births)
        assert len(observed.births) <= observed.notification_count <= witness.MAX_EVENTS
        assert observed.primary_error == observed.cleanup_error == "none"
        assert observed.failure_phase == "not_started"
        assert observed.admission_phase == "admitted_birth"
        assert observed.capture_error == observed.admission_error == "none"
        assert observed.failure_actor == "none"
        assert observed.duplicate_count <= observed.notification_count
    finally:
        release_hash.set()
        if owned is not None:
            owned.close()
        assert witness.boundary.observers_quiescent(resources)


@pytest.mark.skipif(
    os.name != "nt" or not os.environ.get("K5_FACADE_NATIVE_INPUTS"),
    reason="Requires explicitly allocated native lane and independently admitted offline inputs",
)
def test_windows_actual_installed_test_and_two_run_facades():
    """Full real-facade test, deliberately opt-in and never a hosted substitute.

    K5_FACADE_NATIVE_INPUTS names a private JSON object of existing CLI input paths.
    Preparation/provisioning belongs to the separately reviewed native workflow.
    This test performs no download and never changes the product scripts.
    """
    names = {
        "expectations",
        "admitted_expectations",
        "output",
        "repo",
        "analytics_source",
        "k5_wheel",
        "wheelhouse",
        "evidence_root",
        "local_appdata",
        "git",
        "temp_root",
        "work_root",
    }
    inputs = common.read_json(common.local_path(Path(os.environ["K5_FACADE_NATIVE_INPUTS"])))
    common.require(inputs.keys() == names and all(type(value) is str for value in inputs.values()))
    env = common.clean_environment(dict(os.environ))
    base = common.admitted_gate_python(env)
    command = [str(base), "-I", "-B", "-S", str(ROOT / "scripts/installed_alpha_facade_witness.py")]
    for name in sorted(names):
        command.extend(["--" + name.replace("_", "-"), inputs[name]])
    common.run(command, cwd=ROOT, env=env, operation="launch_1", seconds=600)
    witness.validate_receipt(
        common.read_json(Path(inputs["output"])),
        common.read_json(Path(inputs["admitted_expectations"])),
    )
    assert not Path(inputs["work_root"]).exists()
