"""Bounded Stage-05 readiness workload execution."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from k5vision.media.readiness import (
    ReadinessEvidence,
    ReadinessObservation,
    ReadinessOutcome,
    ReadinessPlan,
)
from k5vision.media.session import MediaRuntime, MediaSession, MediaSessionState

RuntimeFactory = Callable[[], MediaRuntime]


async def _best_effort_cleanup(sessions: list[MediaSession]) -> bool:
    """Return whether every session reached a source-free clean terminal state."""
    clean = True
    for session in sessions:
        try:
            if session.snapshot.state == MediaSessionState.FAILED:
                await session.recover()
            await session.close()
        except (Exception, asyncio.CancelledError):
            clean = False
    return clean


async def _run_observation(
    runtime_factory: RuntimeFactory,
    source_uri: str,
    *,
    level: int,
    cycle: int,
    timeout_seconds: float,
) -> ReadinessObservation:
    """Run one bounded start/stop/re-entry/stop observation at a concurrency level."""
    sessions = [MediaSession(runtime_factory()) for _ in range(level)]
    started = time.perf_counter()
    completed = 0
    outcome = ReadinessOutcome.PASS

    try:
        async with asyncio.timeout(timeout_seconds):
            first_start = await asyncio.gather(
                *(session.start(source_uri) for session in sessions),
                return_exceptions=True,
            )
            if any(isinstance(item, BaseException) for item in first_start):
                outcome = ReadinessOutcome.START_FAILURE
            else:
                first_stop = await asyncio.gather(
                    *(session.stop() for session in sessions),
                    return_exceptions=True,
                )
                if any(isinstance(item, BaseException) for item in first_stop):
                    outcome = ReadinessOutcome.STOP_FAILURE
                else:
                    second_start = await asyncio.gather(
                        *(session.start(source_uri) for session in sessions),
                        return_exceptions=True,
                    )
                    if any(isinstance(item, BaseException) for item in second_start):
                        outcome = ReadinessOutcome.START_FAILURE
                    else:
                        second_stop = await asyncio.gather(
                            *(session.stop() for session in sessions),
                            return_exceptions=True,
                        )
                        if any(isinstance(item, BaseException) for item in second_stop):
                            outcome = ReadinessOutcome.STOP_FAILURE
                        else:
                            completed = level
    except TimeoutError:
        outcome = ReadinessOutcome.TIMEOUT
    finally:
        cleanup_ok = await _best_effort_cleanup(sessions)
        if not cleanup_ok and outcome != ReadinessOutcome.TIMEOUT:
            outcome = ReadinessOutcome.RECOVERY_FAILURE

    elapsed_ms = max(0, round((time.perf_counter() - started) * 1000))
    return ReadinessObservation(
        level=level,
        cycle=cycle,
        attempted_sessions=level,
        completed_sessions=completed,
        outcome=outcome,
        elapsed_ms=elapsed_ms,
    )


async def qualify_readiness(
    runtime_factory: RuntimeFactory,
    source_uri: str,
    *,
    revision: str,
    plan: ReadinessPlan | None = None,
) -> ReadinessEvidence:
    """Execute the complete deterministic workload and return source-free evidence."""
    if not source_uri.strip():
        raise ValueError("source_uri must not be empty")
    active_plan = plan or ReadinessPlan()
    observations: list[ReadinessObservation] = []

    for level in active_plan.concurrency_ladder:
        for cycle in range(1, active_plan.cycles_per_level + 1):
            observations.append(
                await _run_observation(
                    runtime_factory,
                    source_uri,
                    level=level,
                    cycle=cycle,
                    timeout_seconds=active_plan.operation_timeout_seconds,
                )
            )

    return ReadinessEvidence(
        revision=revision,
        plan=active_plan,
        observations=tuple(observations),
    )
