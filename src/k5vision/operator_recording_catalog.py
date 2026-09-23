"""Durable source-safe catalog reconstructed from finalized recording pairs.

The catalog deliberately does not add another database or identity authority. Finalized
K5 recording descriptors remain the durable source of truth and are re-indexed after a
process restart. Public entries contain only bounded typed recording metadata; source
URIs, credentials, filesystem paths, and media payloads never enter catalog state.
"""

from __future__ import annotations

import itertools
import stat
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from k5vision.media.recording_descriptor import (
    RecordingDescriptorError,
    RecordingStreamDescriptor,
    parse_recording_descriptor,
)

_MAX_DESCRIPTOR_BYTES = 4096
_MAX_CATALOG_ENTRIES = 10_000
_MAX_SCAN_FILES = 20_000


class RecordingCatalogEntry(BaseModel):
    """Path-free durable metadata for one finalized recording segment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    recording_id: UUID
    source_id: UUID
    started_at_utc: datetime
    ended_at_utc: datetime
    duration_ms: int = Field(ge=0, le=7 * 24 * 60 * 60 * 1000)
    packet_count: int = Field(ge=1, le=1_000_000)
    payload_bytes: int = Field(ge=12, le=8 * 1024 * 1024 * 1024)


class RecordingCatalogSnapshot(BaseModel):
    """Aggregate restart-recovery result without exposing filesystem details."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    recovered_entries: int = Field(ge=0, le=_MAX_CATALOG_ENTRIES)
    rejected_entries: int = Field(ge=0, le=_MAX_SCAN_FILES)
    scan_truncated: bool = False


class RecordingCatalogPage(BaseModel):
    """Bounded newest-first catalog page."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    entries: tuple[RecordingCatalogEntry, ...]
    recovered_entries: int = Field(ge=0, le=_MAX_CATALOG_ENTRIES)
    rejected_entries: int = Field(ge=0, le=_MAX_SCAN_FILES)
    scan_truncated: bool = False


class BoundedRecordingCatalog:
    """Rebuild a bounded catalog from accepted descriptor/media pairs on disk."""

    def __init__(
        self,
        recording_root: str | Path,
        *,
        max_entries: int = _MAX_CATALOG_ENTRIES,
        max_scan_files: int = _MAX_SCAN_FILES,
    ) -> None:
        root_text = str(recording_root).strip()
        if not root_text:
            raise ValueError("recording_root is required")
        if not 1 <= max_entries <= _MAX_CATALOG_ENTRIES:
            raise ValueError("max_entries must be between 1 and 10000")
        if not 1 <= max_scan_files <= _MAX_SCAN_FILES:
            raise ValueError("max_scan_files must be between 1 and 20000")
        self._root = Path(recording_root).expanduser().resolve(strict=False)
        self._max_entries = max_entries
        self._max_scan_files = max_scan_files
        self._entries: tuple[RecordingCatalogEntry, ...] = ()
        self._snapshot = RecordingCatalogSnapshot(recovered_entries=0, rejected_entries=0)

    @property
    def snapshot(self) -> RecordingCatalogSnapshot:
        return self._snapshot

    @staticmethod
    def _entry(descriptor: RecordingStreamDescriptor) -> RecordingCatalogEntry:
        return RecordingCatalogEntry(
            recording_id=descriptor.recording_id,
            source_id=descriptor.source_id,
            started_at_utc=descriptor.started_at_utc,
            ended_at_utc=descriptor.ended_at_utc,
            duration_ms=descriptor.duration_ms,
            packet_count=descriptor.packet_count,
            payload_bytes=descriptor.payload_bytes,
        )

    def recover(self) -> RecordingCatalogSnapshot:
        """Reconstruct catalog state from finalized pairs after startup/restart."""
        if not self._root.exists():
            self._entries = ()
            self._snapshot = RecordingCatalogSnapshot(
                recovered_entries=0,
                rejected_entries=0,
            )
            return self._snapshot

        recovered: list[RecordingCatalogEntry] = []
        rejected = 0
        truncated = False
        try:
            limited = list(
                itertools.islice(
                    self._root.glob("*.k5d"),
                    self._max_scan_files + 1,
                )
            )
            if len(limited) > self._max_scan_files:
                truncated = True
                limited = limited[: self._max_scan_files]
            candidates = sorted(limited, key=lambda path: path.name)
        except OSError:
            candidates = []
            rejected = 1

        for descriptor_path in candidates:
            if len(recovered) >= self._max_entries:
                truncated = True
                break
            try:
                recording_id = UUID(descriptor_path.stem)
                descriptor_stat = descriptor_path.lstat()
                recording_path = self._root / f"{recording_id}.k5r"
                recording_stat = recording_path.lstat()
                if (
                    not stat.S_ISREG(descriptor_stat.st_mode)
                    or not stat.S_ISREG(recording_stat.st_mode)
                    or not 1 <= descriptor_stat.st_size <= _MAX_DESCRIPTOR_BYTES
                ):
                    raise ValueError
                descriptor = parse_recording_descriptor(descriptor_path.read_bytes())
                if (
                    descriptor.recording_id != recording_id
                    or descriptor.packet_count < 1
                    or recording_stat.st_size != descriptor.file_bytes
                ):
                    raise ValueError
                recovered.append(self._entry(descriptor))
            except (OSError, ValueError, RecordingDescriptorError):
                rejected += 1

        recovered.sort(
            key=lambda entry: (entry.started_at_utc, str(entry.recording_id)),
            reverse=True,
        )
        self._entries = tuple(recovered)
        self._snapshot = RecordingCatalogSnapshot(
            recovered_entries=len(self._entries),
            rejected_entries=rejected,
            scan_truncated=truncated,
        )
        return self._snapshot

    def page(
        self,
        *,
        source_id: UUID | None = None,
        limit: int = 100,
    ) -> RecordingCatalogPage:
        """Return a bounded page from freshly reconstructed durable state."""
        if source_id is not None and not isinstance(source_id, UUID):
            raise TypeError("source_id must be a UUID")
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        snapshot = self.recover()
        entries = self._entries
        if source_id is not None:
            entries = tuple(entry for entry in entries if entry.source_id == source_id)
        return RecordingCatalogPage(
            entries=entries[:limit],
            recovered_entries=snapshot.recovered_entries,
            rejected_entries=snapshot.rejected_entries,
            scan_truncated=snapshot.scan_truncated,
        )
