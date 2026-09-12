"""Bounded process-backed capture helpers for Stage 03 qualification."""

from __future__ import annotations

import asyncio
import subprocess
import time
from collections.abc import Sequence
from typing import Literal, Self

import psutil
from pydantic import BaseModel, ConfigDict, Field, model_validator

from k5vision.adapters.runtime import (
    QualificationPlan,
    QualificationResult,
    RuntimeCandidate,
    RuntimeSample,
    qualify_candidate,
)
from k5vision.adapters.stage03_evidence import ResourceMeasurement, ResourceProfile

_SOURCE_TOKEN = "{source}"
_MAX_CAPTURE_LOAD = 32


class ProcessCandidateSpec(BaseModel):
    """Safe argv-only process specification used for local qualification capture."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = "1"
    candidate: str = Field(min_length=1, max_length=128)
    argv: list[str] = Field(min_length=1, max_length=64)
    interruption_seconds: float = Field(default=0.25, gt=0, le=30)
    poll_interval_seconds: float = Field(default=0.05, gt=0, le=1)

    @model_validator(mode="after")
    def validate_argv(self) -> Self:
        if sum(arg == _SOURCE_TOKEN for arg in self.argv) != 1:
            raise ValueError("process argv must contain exactly one {source} token")
        if any(not arg for arg in self.argv):
            raise ValueError("process argv entries must not be empty")
        return self

    def build_argv(self, source_uri: str) -> list[str]:
        """Return argv with the source inserted as one argument, never through a shell."""
        if not source_uri.strip():
            raise ValueError("source_uri must not be empty")
        return [source_uri if arg == _SOURCE_TOKEN else arg for arg in self.argv]


class Stage03Capture(BaseModel):
    """Raw measured evidence captured before dependency review and final selection."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = "1"
    results: list[QualificationResult] = Field(min_length=2, max_length=32)
    resources: list[ResourceProfile] = Field(min_length=2, max_length=32)

    @model_validator(mode="after")
    def validate_candidate_sets(self) -> Self:
        result_names = [item.candidate for item in self.results]
        resource_names = [item.candidate for item in self.resources]
        if len(result_names) != len(set(result_names)):
            raise ValueError("capture contains duplicate candidate results")
        if len(resource_names) != len(set(resource_names)):
            raise ValueError("capture contains duplicate candidate resource profiles")
        if set(result_names) != set(resource_names):
            raise ValueError("capture results and resources must cover the same candidates")
        return self


class ProcessRuntimeCandidate(RuntimeCandidate):
    """Measure one external candidate through a bounded argv-only subprocess."""

    def __init__(self, spec: ProcessCandidateSpec) -> None:
        self.spec = spec

    def _stop_process(self, process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)

    def _measure_sync(
        self,
        source_uri: str,
        *,
        timeout_seconds: float,
        interrupt_after: float | None = None,
    ) -> RuntimeSample:
        argv = self.spec.build_argv(source_uri)
        started = time.perf_counter()
        process = subprocess.Popen(  # noqa: S603
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
        )
        monitored = psutil.Process(process.pid)
        first_observed: float | None = None
        peak_rss = 0
        cpu_seconds = 0.0
        io_bytes = 0
        interrupted = False

        try:
            while True:
                now = time.perf_counter()
                elapsed = now - started
                return_code = process.poll()

                try:
                    if first_observed is None:
                        monitored.status()
                        first_observed = now
                    memory = monitored.memory_info()
                    peak_rss = max(peak_rss, memory.rss)
                    cpu = monitored.cpu_times()
                    cpu_seconds = max(cpu_seconds, cpu.user + cpu.system)
                    try:
                        io = monitored.io_counters()
                        io_bytes = max(
                            io_bytes,
                            int(getattr(io, "read_bytes", 0))
                            + int(getattr(io, "write_bytes", 0)),
                        )
                    except (psutil.AccessDenied, NotImplementedError):
                        pass
                except (psutil.NoSuchProcess, psutil.ZombieProcess):
                    pass

                if interrupt_after is not None and not interrupted and elapsed >= interrupt_after:
                    if return_code is not None:
                        raise RuntimeError("candidate exited before the interruption boundary")
                    interrupted = True
                    self._stop_process(process)
                    return_code = process.returncode
                    break

                if return_code is not None:
                    break
                if elapsed >= timeout_seconds:
                    raise TimeoutError("candidate process exceeded its bounded timeout")
                time.sleep(self.spec.poll_interval_seconds)
        finally:
            self._stop_process(process)

        elapsed = max(time.perf_counter() - started, 1e-9)
        startup_ms = (
            max((first_observed or started) - started, 0.0) * 1000
        )
        completed = not interrupted and process.returncode == 0
        return RuntimeSample(
            candidate=self.spec.candidate,
            startup_ms=startup_ms,
            latency_ms=elapsed * 1000,
            cpu_percent=(cpu_seconds / elapsed) * 100,
            memory_mb=peak_rss / (1024 * 1024),
            bytes_processed=io_bytes,
            recovered=completed,
            completed=completed,
        )

    async def measure(self, source_uri: str, *, timeout_seconds: float) -> RuntimeSample:
        """Run one bounded candidate measurement without invoking a command shell."""
        return await asyncio.to_thread(
            self._measure_sync,
            source_uri,
            timeout_seconds=timeout_seconds,
        )

    async def measure_recovery(
        self,
        source_uri: str,
        *,
        timeout_seconds: float,
    ) -> RuntimeSample:
        """Interrupt one live process, then prove that a fresh bounded run can re-enter."""
        await asyncio.to_thread(
            self._measure_sync,
            source_uri,
            timeout_seconds=timeout_seconds,
            interrupt_after=min(self.spec.interruption_seconds, timeout_seconds * 0.5),
        )
        sample = await self.measure(source_uri, timeout_seconds=timeout_seconds)
        return sample.model_copy(update={"recovered": sample.completed})


async def measure_resource_profile(
    candidate_name: str,
    candidate: RuntimeCandidate,
    source_uri: str,
    *,
    load_ladder: Sequence[int],
    timeout_seconds: float,
) -> ResourceProfile:
    """Capture comparable aggregate resource measurements under bounded concurrent load."""
    loads = list(load_ladder)
    if len(loads) < 2:
        raise ValueError("load_ladder must contain at least two levels")
    if loads != sorted(loads) or len(loads) != len(set(loads)):
        raise ValueError("load_ladder must be unique and strictly increasing")
    if loads[-1] > _MAX_CAPTURE_LOAD:
        raise ValueError(f"load_ladder may not exceed {_MAX_CAPTURE_LOAD} concurrent units")

    measurements: list[ResourceMeasurement] = []
    for load in loads:
        if load < 1:
            raise ValueError("load_ladder values must be positive")
        samples = await asyncio.gather(
            *(candidate.measure(source_uri, timeout_seconds=timeout_seconds) for _ in range(load))
        )
        if any(sample.candidate != candidate_name for sample in samples):
            raise ValueError("resource sample candidate identity mismatch")
        measurements.append(
            ResourceMeasurement(
                candidate=candidate_name,
                load_units=load,
                cpu_percent=sum(sample.cpu_percent for sample in samples),
                memory_mb=sum(sample.memory_mb for sample in samples),
                completed=all(sample.completed for sample in samples),
            )
        )
    return ResourceProfile(candidate=candidate_name, samples=measurements)


async def capture_candidate_set(
    specs: Sequence[ProcessCandidateSpec],
    source_uri: str,
    *,
    plan: QualificationPlan | None = None,
    load_ladder: Sequence[int] = (1, 2),
) -> Stage03Capture:
    """Capture comparable qualification and resource evidence for two or more candidates."""
    if len(specs) < 2:
        raise ValueError("at least two process candidates are required")
    names = [spec.candidate for spec in specs]
    if len(names) != len(set(names)):
        raise ValueError("process candidate identities must be unique")

    plan = plan or QualificationPlan()
    results: list[QualificationResult] = []
    resources: list[ResourceProfile] = []
    for spec in specs:
        candidate = ProcessRuntimeCandidate(spec)
        results.append(
            await qualify_candidate(
                spec.candidate,
                candidate,
                source_uri,
                plan=plan,
            )
        )
        resources.append(
            await measure_resource_profile(
                spec.candidate,
                candidate,
                source_uri,
                load_ladder=load_ladder,
                timeout_seconds=plan.timeout_seconds,
            )
        )
    return Stage03Capture(results=results, resources=resources)
