"""Runtime boundary for isolated analytics workers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from k5vision.analytics.contracts import AnalyticEvent, AnalyticHealth, AnalyticManifest


class AnalyticRuntime(Protocol):
    """Contract implemented by a process/container transport adapter.

    K5 core depends on this boundary rather than importing model code directly. A concrete
    transport may use a subprocess, local HTTP, a Unix socket, gRPC, or a remote inference
    service without changing event consumers.
    """

    @property
    def manifest(self) -> AnalyticManifest:
        """Return the immutable manifest for this runtime."""
        ...

    async def start(self) -> None:
        """Start or connect to the isolated worker."""
        ...

    async def stop(self) -> None:
        """Stop or disconnect from the isolated worker."""
        ...

    async def health(self) -> AnalyticHealth:
        """Return current worker health and throughput information."""
        ...

    def events(self) -> AsyncIterator[AnalyticEvent]:
        """Yield normalized analytic events from the worker."""
        ...
