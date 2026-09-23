"""Authenticated source-free timeline metadata for Stage-One operator playback."""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from k5vision.domain.users import UserAccount
from k5vision.media.playback_schedule import PlaybackRate
from k5vision.operator_playback import (
    BoundedOperatorPlaybackCoordinator,
    OperatorPlaybackError,
    OperatorPlaybackErrorCode,
    _load_recording_pair,
)
from k5vision.services.device_registry import DeviceRegistry, DeviceRegistryStorageError

_MAX_PACKETS = 1_000_000
_MAX_DURATION_MS = 7 * 24 * 60 * 60 * 1000


class OperatorPlaybackTimelineReceipt(BaseModel):
    """Path/source/media-free metadata needed to render bounded playback controls."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "1"
    recording_id: UUID
    duration_ms: int = Field(ge=0, le=_MAX_DURATION_MS)
    packet_count: int = Field(ge=0, le=_MAX_PACKETS)
    seek_min_ms: int = Field(default=0, ge=0)
    seek_max_ms: int = Field(ge=0, le=_MAX_DURATION_MS)
    supported_rates: tuple[PlaybackRate, ...]
    metadata_validated: bool = True


class BoundedOperatorPlaybackTimeline:
    """Authorize one persisted recording and expose only bounded timeline metadata."""

    def __init__(self, registry: DeviceRegistry, recording_root: str | Path) -> None:
        if not isinstance(registry, DeviceRegistry):
            raise TypeError("registry must be a DeviceRegistry")
        root_text = str(recording_root).strip()
        if not root_text:
            raise ValueError("recording_root is required")
        self._registry = registry
        self._recording_root = Path(recording_root).expanduser().resolve(strict=False)

    async def inspect(
        self,
        principal: UserAccount,
        recording_id: UUID,
    ) -> OperatorPlaybackTimelineReceipt:
        """Return validated playback bounds without resolving a live source or media path."""
        BoundedOperatorPlaybackCoordinator._validate_principal(principal)
        if not isinstance(recording_id, UUID):
            raise TypeError("recording_id must be a UUID")

        _, descriptor = await asyncio.to_thread(
            _load_recording_pair,
            self._recording_root,
            recording_id,
        )
        try:
            device = self._registry.get(descriptor.source_id)
        except DeviceRegistryStorageError:
            raise OperatorPlaybackError(
                OperatorPlaybackErrorCode.REGISTRY_UNAVAILABLE,
                "device registry is unavailable",
            ) from None
        if device is None:
            raise OperatorPlaybackError(
                OperatorPlaybackErrorCode.DEVICE_NOT_FOUND,
                "recorded device was not found",
            )
        BoundedOperatorPlaybackCoordinator._validate_device(device)

        return OperatorPlaybackTimelineReceipt(
            recording_id=recording_id,
            duration_ms=descriptor.duration_ms,
            packet_count=descriptor.packet_count,
            seek_max_ms=descriptor.duration_ms,
            supported_rates=tuple(PlaybackRate),
        )
